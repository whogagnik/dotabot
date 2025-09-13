from CONSTANTS import *
from typing import Optional, List, Callable, Tuple
import os, time, logging, subprocess, threading, shutil
from stat import S_IWRITE
from pathlib import Path
import psutil
from windowPlacer import WindowPlacer
import win32gui, win32api, win32con, win32process
import ctypes
import pyautogui as p

"""
Адаптация под «Avast Sandbox» (или любой автосэндбокс), где:
  • мы НЕ используем Sandboxie Start.exe и /box:<id>;
  • Steam будет запускаться пользователем заранее сконфигурированным «в песочнице»;
  • мы просто стартуем steam.exe напрямую и работаем с ЕГО деревом процессов.

ВАЖНО: Для совместимости с существующим кодом мы сохраняем те же публичные
методы/сигнатуры (get_box_pids(box_id), _find_login_hwnd_in_box, wait_for_process_in_box и т.д.).
Теперь они игнорируют box_id и опираются на «дерево процессов» текущего аккаунта.
"""

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

GetSystemMetrics = user32.GetSystemMetrics
GetSystemMetrics.argtypes = [ctypes.c_int]
GetSystemMetrics.restype  = ctypes.c_int

GetCurrentThreadId = kernel32.GetCurrentThreadId
GetCurrentThreadId.restype = ctypes.c_uint


def _get_screen_size() -> Tuple[int,int]:
    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)

def _reposition_window_keep_size(hwnd: int, x: int, y: int):
    try:
        sw, sh = _get_screen_size()
        x = max(0, min(x, sw)); y = max(0, min(y, sh))
        win32gui.SetWindowPos(hwnd, None, x, y, 0, 0,
                              win32con.SWP_NOZORDER|win32con.SWP_NOSIZE|win32con.SWP_SHOWWINDOW)
    except Exception: pass

def _find_main_window_for_pid(pid: int) -> Optional[int]:
    result = None
    def cb(hwnd,_):
        nonlocal result
        if result is not None or not win32gui.IsWindowVisible(hwnd): return
        if _window_pid(hwnd)==pid: result = hwnd
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None
    return result

def _force_foreground(hwnd: int):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
        fore = win32gui.GetForegroundWindow()
        ftid = win32process.GetWindowThreadProcessId(fore)[0] if fore else 0
        ctid = win32api.GetCurrentThreadId()
        user32.AttachThreadInput(ftid, ctid, True)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetActiveWindow(hwnd)
    except Exception:
        try: win32gui.SetForegroundWindow(hwnd)
        except Exception: pass
    finally:
        try: user32.AttachThreadInput(ftid, ctid, False)
        except Exception: pass

def _hwnd_exists(hwnd: int) -> bool:
    try: return bool(win32gui.IsWindow(hwnd))
    except Exception: return False

def _window_pid(hwnd: int) -> Optional[int]:
    try: return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception: return None

def _login_window_title_match(hwnd: int) -> bool:
    try: title = (win32gui.GetWindowText(hwnd) or "").lower()
    except Exception: return False
    for m in ("войти в стим","войти в steam","вход в steam","sign in to steam"):
        if m in title: return True
    return False

def _any_login_hwnd_for_pids(pids: set[int]) -> Optional[int]:
    found = None
    def cb(hwnd,_):
        nonlocal found
        if found is not None or not win32gui.IsWindowVisible(hwnd): return
        if not _login_window_title_match(hwnd): return
        pid = _window_pid(hwnd)
        if pid and pid in pids: found = hwnd
    win32gui.EnumWindows(cb, None); return found

def _client_region(hwnd: int) -> Tuple[int,int,int,int]:
    try:
        l,t,r,b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0,0))
        w, h = max(1, r-l), max(1, b-t)
        return sx, sy, w, h
    except Exception:
        try:
            L,T,R,B = win32gui.GetWindowRect(hwnd)
            return L, T, max(1,R-L), max(1,B-T)
        except Exception:
            return 0,0,1,1


class Account:
    def __init__(self, username:str, password:str, logger:logging.Logger, placer:WindowPlacer,
                 status_cb:Callable[[str,str],None]):
        self.username=username; self.password=password; self.logger=logger; self.placer=placer; self.status_cb=status_cb
        self.mafile_data:Optional[dict]=None; self.mafile_path:Optional[str]=None
        self.steam_id:Optional[str] = None
        self._qr_proc:Optional[subprocess.Popen]=None
        self.login_hwnd:Optional[int]=None; self.box_id:Optional[int]=None  # box_id теперь только для раскладки (индекс)
        self.dota_pid:Optional[int]=None; self.dota_hwnd:Optional[int]=None
        self.hours_played:Optional[int] = None
        self.status="idle"; self.session_seconds=0

        # новое: корневой процесс steam.exe, который мы запустили (держим pid)
        self._root_pid: Optional[int] = None
        self._steam_path: Optional[str] = None
        self._app_id: Optional[int] = None

    def set_status(self, s:str):
        self.status=s
        try: self.status_cb(self.username,s)
        except Exception: pass

    def attach_mafile(self, path:str, data:dict): self.mafile_path=path; self.mafile_data=data

    # ================== helpers: процессное дерево для текущего аккаунта ==================
    def _proc_tree_pids(self) -> List[int]:
        """
        PID'ы корневого процесса и всех его потомков (на момент вызова).
        Используется вместо «песочницы».
        """
        res: List[int] = []
        if not self._root_pid:
            return res
        try:
            root = psutil.Process(self._root_pid)
        except Exception:
            return res
        try:
            res.append(root.pid)
            for ch in root.children(recursive=True):
                try:
                    res.append(ch.pid)
                except Exception:
                    pass
        except Exception:
            pass
        return res

    # Совместимость для Controller._box_proc_list(...)
    def get_box_pids(self, box_id:int) -> List[int]:
        return self._proc_tree_pids()

    # ================== «очистки» (минимально-неопасные) ==================
    def kill_box_processes(self, box_id:int):
        """Завершаем дерево процессов, запущенных нами (steam.exe и дети)."""
        pids = self._proc_tree_pids()
        if not pids: return
        # Сначала мягко
        for pid in pids:
            try:
                psutil.Process(pid).terminate()
            except Exception:
                pass
        t0 = time.time()
        while time.time() - t0 < 3:
            if not any(psutil.pid_exists(pid) for pid in pids):
                break
            time.sleep(0.2)
        # Жёстко оставшихся
        for pid in list(pids):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()
            except Exception:
                pass

    def clear_real_steam_auth(self, steam_path:str):
        """Как и раньше: чистим реальный Steam (config/loginusers + userdata)."""
        try:
            root=Path(steam_path).resolve().parent
            cfg=root/"config"; userdata=root/"userdata"
            for t in (cfg/"loginusers.vdf", cfg/"loginusers.vdf.bak"):
                if t.exists():
                    try: t.chmod(S_IWRITE); t.unlink()
                    except Exception as e: self.logger.warning(f"Не удалось удалить {t}: {e}")
            if userdata.exists():
                for child in userdata.iterdir():
                    if child.is_dir():
                        try:
                            shutil.rmtree(child, ignore_errors=True)
                        except Exception as e:
                            self.logger.warning(f"Не удалось удалить {child}: {e}")
            self.logger.info("Очистка реального Steam: config/loginusers + userdata — ОК")
        except Exception as e:
            self.logger.warning(f"Очистка реального Steam не удалась: {e}")

    # Заглушка для старого API (ничего не делаем — Avast сам управляет песочницей)
    def clear_sandboxie_cache(self, box_id:int):
        self.logger.debug(f"{self.username}: Avast режим — clear_sandboxie_cache пропущен.")

    # ================== поиск окна логина, привязка к нашему дереву ==================
    def _find_login_hwnd_in_box(self, box_id:int, timeout_s:int=WAIT_LOGIN_WIN_TIMEOUT) -> Optional[int]:
        """Совместимость: ищем логин-окно в ДЕРЕВЕ ПРОЦЕССОВ текущего аккаунта."""
        end=time.time()+timeout_s
        while time.time()<end:
            pids=set(self._proc_tree_pids())
            if not pids:
                time.sleep(0.5); continue
            found=_any_login_hwnd_for_pids(pids)
            if found: return found
            time.sleep(0.5)
        return None

    def _any_login_hwnd_now(self, box_id:int) -> Optional[int]:
        pids=set(self._proc_tree_pids())
        return _any_login_hwnd_for_pids(pids) if pids else None

    def _login_window_absent_stably(self, box_id:int, duration:int=LOGIN_GONE_GRACE_SEC)->bool:
        t0=time.time()
        while time.time()-t0<duration:
            if self._any_login_hwnd_now(box_id): return False
            time.sleep(0.5)
        return True

    # ================== стадия 1: запуск до окна входа ==================
    def wait_for_process_in_box(
            self,
            box_id: int,
            names: List[str],
            max_attempts: int = WAIT_STEAM_PROC_ATTEMPTS,
            interval: float = WAIT_STEAM_PROC_INTERVAL,
            match_fn: Optional[Callable[[psutil.Process], bool]] = None
    ) -> Optional[psutil.Process]:
        """
        Совместимостьная обёртка: ищем процесс с заданным именем в нашем дереве.
        """
        target_names = {n.lower() for n in names if n}
        for _ in range(max_attempts):
            pids = self._proc_tree_pids()
            for pid in pids:
                try:
                    p = psutil.Process(pid)
                    pname = (p.name() or "").lower()
                    if pname in target_names and (match_fn is None or match_fn(p)):
                        return p
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            time.sleep(interval)
        return None

    def launch_until_login_window(self, steam_path:str, app_id:int, box_id:int)->Optional[int]:
        """
        Запускаем steam.exe напрямую (пользователь настроил «всегда в песочнице»).
        Дальше — ждём окно входа в пределах НАШЕГО дерева процессов.
        """
        self.box_id=box_id
        self._steam_path = steam_path
        self._app_id = app_id

        # Полная зачистка от предыдущего запуска
        try: self.kill_box_processes(box_id)
        except Exception: pass
        try: self.clear_real_steam_auth(steam_path)
        except Exception: pass

        # Старт
        cmd=[steam_path, "-applaunch", str(app_id)]
        cmd.extend(DOTA_LAUNCH_OPTS)
        try:
            proc = subprocess.Popen(cmd)
            self._root_pid = int(proc.pid)
            self.logger.info(f"{self.username}: запуск Steam+Dota (PID={self._root_pid}) (автосэндбокс извне)")
        except Exception as e:
            self.logger.error(f"{self.username}: ошибка запуска → {e}")
            return None

        # Ждём steam.exe (в нашем дереве это обычно уже root)
        steam_proc=self.wait_for_process_in_box(box_id, ["steam.exe"])
        if not steam_proc:
            self.logger.error(f"{self.username}: steam.exe не появился")
            return None

        # Ищем окно входа
        self.logger.info(f"{self.username}: жду окно входа Steam…")
        hwnd=self._find_login_hwnd_in_box(box_id, timeout_s=WAIT_LOGIN_WIN_TIMEOUT)
        if not hwnd:
            self.logger.error(f"{self.username}: окно входа не найдено")
            return None

        # Раскладка окна логина
        x,y,_,_=self.placer.rect_for(box_id); _reposition_window_keep_size(hwnd,x,y)
        self.login_hwnd=hwnd; self.set_status("ready")
        self.logger.info(f"{self.username}: окно входа найдено: HWND={hex(hwnd)} — готов к скану.")
        return hwnd

    # ================== полный рестарт и ожидание окна входа ==================
    def _full_restart_to_login(self) -> Optional[int]:
        if self._steam_path is None or self._app_id is None:
            return None
        try: self.kill_box_processes(self.box_id or -1)
        except Exception: pass
        try: self.clear_real_steam_auth(self._steam_path)
        except Exception: pass

        cmd = [self._steam_path, "-applaunch", str(self._app_id)]
        cmd.extend(DOTA_LAUNCH_OPTS)
        try:
            proc = subprocess.Popen(cmd)
            self._root_pid = int(proc.pid)
            self.logger.info(f"{self.username}: перезапуск Steam+Dota (PID={self._root_pid})")
        except Exception as e:
            self.logger.error(f"{self.username}: ошибка перезапуска → {e}")
            time.sleep(RELAUNCH_DELAY_SEC); return None

        hwnd = self._find_login_hwnd_in_box(self.box_id or -1, timeout_s=WAIT_LOGIN_WIN_TIMEOUT)
        if not hwnd:
            self.logger.error(f"{self.username}: после рестарта окно входа не найдено"); return None

        x,y,_,_=self.placer.rect_for(self.box_id or 0); _reposition_window_keep_size(hwnd,x,y)
        self.login_hwnd = hwnd; self.set_status("ready")
        self.logger.info(f"{self.username}: новое окно входа после рестарта: HWND={hex(hwnd)}")
        return hwnd

    # ================== окна steamwebhelper в нашем дереве ==================
    def _steamwebhelper_hwnds_in_box(self, only_title_steam: bool = True) -> List[int]:
        """
        Видимые окна steamwebhelper.exe только из нашего процессного дерева.
        """
        pids = set(self._proc_tree_pids())
        if not pids: return []
        hwnds: List[int] = []

        def cb(hwnd,_):
            if not win32gui.IsWindowVisible(hwnd): return
            pid = _window_pid(hwnd)
            if not pid or pid not in pids: return
            try:
                pname = (psutil.Process(pid).name() or "").lower()
            except Exception:
                return
            if pname != "steamwebhelper.exe":
                return
            if only_title_steam:
                try:
                    title = (win32gui.GetWindowText(hwnd) or "").strip()
                except Exception:
                    title = ""
                if not title or "steam" not in title.lower():
                    return
            hwnds.append(hwnd)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return []
        return hwnds

    # ================== клик по «Всё равно продолжить» в steamwebhelper-окне ==================
    def _click_continue_anyway_in_hwnd(
        self,
        hwnd: int,
        images_root: str = "images/steam",
        confidence: float = 0.88,
        close_after: bool = True,
    ) -> bool:
        patterns = [
            "continue_anyway_ru.png",
            "continue_anyway_en.png",
            "continue_ru.png",
            "ok.png",
        ]
        paths = [os.path.join(images_root, f) for f in patterns]
        paths = [ph for ph in paths if os.path.exists(ph)]
        if not paths:
            return False

        try:
            _force_foreground(hwnd)
            region = _client_region(hwnd)
            for img in paths:
                try:
                    pt = p.locateCenterOnScreen(img, confidence=confidence, region=region, grayscale=True)
                except Exception:
                    pt = None
                if pt:
                    p.moveTo(pt); p.leftClick()
                    self.logger.info(f"{self.username}: клик по '{os.path.basename(img)}' в окне Steam (steamwebhelper).")
                    if close_after:
                        try:
                            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                        except Exception:
                            try: win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                            except Exception: pass
                    return True
        except Exception:
            pass
        return False

    # ================== стадия 2: QR-скан ==================
    def run_qr_scanner(self, stop_event: threading.Event) -> int:
        """
        Логика сохранена, только наблюдение за окном идёт через наше дерево, а не box_id.
        """
        if self.box_id is None:
            self.set_status("error"); return 1

        def _spawn_once(hwnd: int) -> int:
            import sys
            script = os.path.join(os.path.dirname(__file__), "qrLoger.py")
            if not os.path.exists(script):
                self.logger.error(f"{self.username}: нет qrLoger.py"); self.set_status("error"); return 1
            if not self.mafile_path:
                self.logger.error(f"{self.username}: нет maFile"); self.set_status("error"); return 1

            env = os.environ.copy()
            env.setdefault("ZBAR_DEBUG", "0")
            cmd = [
                sys.executable, script,
                "--mafile", self.mafile_path,
                "--login", self.username,
                "--password", self.password,
                "--timeout", str(QR_TIMEOUT_SEC),
                "--poll-seconds", str(POLL_SECONDS),
                "--hwnd", hex(hwnd),
                "--log-level", "INFO",
                "--debug-payload",
            ]

            _force_foreground(hwnd)
            self.set_status("scanning")
            self.logger.info(f"{self.username}: запускаю qrLoger.py с HWND={hex(hwnd)}…")

            self._qr_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", env=env,
            )

            def _tail_stdout():
                try:
                    if self._qr_proc and self._qr_proc.stdout:
                        for line in self._qr_proc.stdout:
                            if not line: break
                            self.logger.info(line.rstrip("\r\n"))
                except Exception: pass
            threading.Thread(target=_tail_stdout, daemon=True).start()

            absent_since = None
            grace = max(LOGIN_GONE_GRACE_SEC, 6)
            early_success = False
            try:
                while self._qr_proc and self._qr_proc.poll() is None and not stop_event.is_set():
                    if self._any_login_hwnd_now(self.box_id) is None:
                        if absent_since is None:
                            absent_since = time.time()
                        elif time.time() - absent_since >= grace:
                            early_success = True
                            try: self._qr_proc.terminate()
                            except Exception: pass
                            break
                    else:
                        absent_since = None
                    time.sleep(0.25)
            finally:
                if self._qr_proc and self._qr_proc.poll() is None:
                    try: self._qr_proc.wait(timeout=3)
                    except Exception:
                        try: self._qr_proc.kill()
                        except Exception: pass

            rc = 0 if early_success else (
                self._qr_proc.returncode if self._qr_proc and self._qr_proc.returncode is not None else 1)
            self._qr_proc = None
            self.logger.info(f"{self.username}: qrLoger.py завершился с кодом {rc}.")
            return rc

        restarts_used = 0
        max_restarts = max(1, MAX_LAUNCH_RETRIES)

        hwnd = self.login_hwnd
        while not stop_event.is_set():
            if not hwnd or not _hwnd_exists(hwnd):
                self.logger.info(f"{self.username}: HWND невалиден — переобнаруживаю окно логина (без рестарта)…")
                hwnd = self._find_login_hwnd_in_box(self.box_id, timeout_s=20)

            rc = _spawn_once(hwnd)
            if rc == 0:
                self.set_status("success"); return 0

            if restarts_used >= max_restarts: break
            restarts_used += 1
            self.logger.info(f"{self.username}: неудачный скан — перезапуск Steam/Dota (#{restarts_used}/{max_restarts})…")
            hwnd = self._full_restart_to_login()

        self.set_status("error"); return 1

    # ================== CPU limit ==================
    def _apply_cpu_limit(self, pid:int, percent:int):
        if percent<=0: return
        try:
            p=psutil.Process(pid)
            try: p.nice(psutil.IDLE_PRIORITY_CLASS)
            except Exception: pass
            self.logger.info(f"{self.username}: CPU ограничен мягко (приоритет=IDLE)")
        except Exception as e:
            self.logger.debug(f"{self.username}: не удалось применить мягкий лимит CPU: {e}")

    # ================== стадия 3: Dota, раскладка, лимит ==================
    def wait_dota_and_arrange(self, index_for_layout:int, cpu_limit_percent:int, max_wait:int=180)->bool:
        # Главный цикл: ждём dota2.exe; ПОКА её нет — обрабатываем блокирующие окна steamwebhelper
        deadline = time.time() + max_wait
        while time.time() < deadline and not self.dota_pid:
            # 1) Dota появилась?
            proc = self.wait_for_process_in_box(self.box_id or 0, ["dota2.exe"], max_attempts=1, interval=0.2)
            if proc:
                self.dota_pid = proc.pid
                self.logger.info(f"{self.username}: Dota2 PID {self.dota_pid}")
                break

            # 2) Обработать блокирующие окна steamwebhelper с заголовком "Steam"
            blockers = self._steamwebhelper_hwnds_in_box(only_title_steam=True)
            for hwnd in blockers:
                ok = self._click_continue_anyway_in_hwnd(hwnd, images_root="images/steam", confidence=0.88, close_after=True)
                if ok:
                    self.logger.info(f"{self.username}: клик выполнен, окно Steam закрыто.")

            time.sleep(0.5)

        if not self.dota_pid:
            self.logger.warning(f"{self.username}: не дождался dota2.exe")
            return False

        # Главное окно Dota
        t1=time.time(); hwnd=None
        while time.time()-t1<30:
            hwnd=_find_main_window_for_pid(self.dota_pid)
            if hwnd: break
            time.sleep(0.5)
        if not hwnd:
            self.logger.warning(f"{self.username}: не нашёл главное окно Dota2")
            return False
        self.dota_hwnd=hwnd

        # Раскладка + заголовок
        x,y,_,_=self.placer.rect_for(index_for_layout); _reposition_window_keep_size(hwnd,x,y)
        try: win32gui.SetWindowText(hwnd, self.username)
        except Exception: pass

        # CPU
        self._apply_cpu_limit(self.dota_pid, cpu_limit_percent)

        self.set_status("ingame"); return True

    def stop_and_cleanup_box(self):
        self.set_status("stopping")
        try:
            if self._qr_proc and self._qr_proc.poll() is None:
                self._qr_proc.terminate()
        except Exception: pass
        try:
            self.kill_box_processes(self.box_id or -1)
            self.clear_sandboxie_cache(self.box_id or -1)  # no-op в Avast-режиме
        except Exception: pass
        self.set_status("idle")
