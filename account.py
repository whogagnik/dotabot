# accounts.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, List, Callable, Tuple
from pathlib import Path
from stat import S_IWRITE
import os
import time
import logging
import shutil
import subprocess
import threading
import ctypes

import psutil
import pyautogui as p
import win32gui
import win32con
import win32api
import win32process

from CONSTANTS import *
from windowPlacer import WindowPlacer
try:
    from threadRegistry import ThreadRegistry  # опционально
except Exception:
    ThreadRegistry = None  # type: ignore[assignment]

# ---------------- Win32 helpers ----------------

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

def _get_screen_size() -> Tuple[int, int]:
    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)

def _reposition_window_keep_size(hwnd: int, x: int, y: int):
    try:
        sw, sh = _get_screen_size()
        x = max(0, min(x, sw))
        y = max(0, min(y, sh))
        win32gui.SetWindowPos(
            hwnd, None, x, y, 0, 0,
            win32con.SWP_NOZORDER | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        )
    except Exception:
        pass

def _window_pid(hwnd: int) -> Optional[int]:
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return None

def _hwnd_exists(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False

def _find_main_window_for_pid(pid: int) -> Optional[int]:
    result = None
    def cb(hwnd, _):
        nonlocal result
        if result is not None:
            return
        if not win32gui.IsWindowVisible(hwnd):
            return
        if _window_pid(hwnd) == pid:
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if title:
                result = hwnd
            elif result is None:
                result = hwnd
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
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        try:
            user32.AttachThreadInput(ftid, ctid, False)  # type: ignore[name-defined]
        except Exception:
            pass

def _client_region(hwnd: int) -> Tuple[int, int, int, int]:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        w, h = max(1, r - l), max(1, b - t)
        return sx, sy, w, h
    except Exception:
        try:
            L, T, R, B = win32gui.GetWindowRect(hwnd)
            return L, T, max(1, R - L), max(1, B - T)
        except Exception:
            return (0, 0, 1, 1)

def _login_window_title_match(hwnd: int) -> bool:
    try:
        title = (win32gui.GetWindowText(hwnd) or "").lower()
    except Exception:
        return False
    for m in ("войти в стим", "войти в steam", "вход в steam", "sign in to steam"):
        if m in title:
            return True
    return False

def _any_login_hwnd_for_pids(pids: set[int]) -> Optional[int]:
    found = None
    def cb(hwnd, _):
        nonlocal found
        if found is not None or not win32gui.IsWindowVisible(hwnd):
            return
        if not _login_window_title_match(hwnd):
            return
        pid = _window_pid(hwnd)
        if pid and pid in pids:
            found = hwnd
    win32gui.EnumWindows(cb, None)
    return found

# ---------------- Image helpers ----------------

def _loc_center(img_path: str, confidence: float = 0.87, region: Optional[Tuple[int, int, int, int]] = None):
    try:
        return p.locateCenterOnScreen(img_path, confidence=confidence, region=region, grayscale=True)
    except Exception:
        return None

def _click_point(pt, delay: float = 0.0):
    try:
        p.moveTo(pt)
        if delay:
            p.sleep(delay)
        p.leftClick()
    except Exception:
        pass

# ------------------------------------------------

class Account:
    """
    Авто-песочница:
      • Запуск steam.exe напрямую (автосэндбокс/Avast и т.п.)
      • До логина: обрабатываем «сервисное» окно грубым кликом по координатам (Отмена).
      • После логина: возвращаем «старую» обработку блокера — ищем кнопку «Продолжить всё равно»
        в окнах Steam(steamwebhelper) по шаблонам и нажимаем, затем закрываем окно.
    """
    def __init__(
        self,
        username: str,
        password: str,
        logger: logging.Logger,
        placer: WindowPlacer,
        status_cb: Callable[[str, str], None],
        *,
        thread_registry: Optional[ThreadRegistry] = None,
    ):
        self.username = username
        self.password = password
        self.logger = logger
        self.placer = placer
        self.status_cb = status_cb
        self.thread_registry = thread_registry

        self.mafile_data: Optional[dict] = None
        self.mafile_path: Optional[str] = None
        self.steam_id: Optional[str] = None

        self._qr_proc: Optional[subprocess.Popen] = None
        self.login_hwnd: Optional[int] = None
        self.box_id: Optional[int] = None  # используется как индекс раскладки
        self.dota_pid: Optional[int] = None
        self.dota_hwnd: Optional[int] = None
        self.hours_played: Optional[int] = None
        self.status = "idle"
        self.session_seconds = 0

        self._root_pid: Optional[int] = None
        self._steam_path: Optional[str] = None
        self._app_id: Optional[int] = None

    # ---------- state ----------
    def set_status(self, s: str):
        self.status = s
        try:
            self.status_cb(self.username, s)
        except Exception:
            pass

    def attach_mafile(self, path: str, data: dict):
        self.mafile_path = path
        self.mafile_data = data

    # ---------- process tree ----------
    def _proc_tree_pids(self) -> List[int]:
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
                res.append(ch.pid)
        except Exception:
            pass
        return res

    # совместимость с «box»-API контроллера
    def get_box_pids(self, box_id: int) -> List[int]:
        return self._proc_tree_pids()

    # ---------- cleanup ----------
    def kill_box_processes(self, box_id: int):
        pids = self._proc_tree_pids()
        if not pids:
            return
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
        for pid in list(pids):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()
            except Exception:
                pass

    def clear_real_steam_auth(self, steam_path: str):
        try:
            root = Path(steam_path).resolve().parent
            cfg = root / "config"
            userdata = root / "userdata"
            for t in (cfg / "loginusers.vdf", cfg / "loginusers.vdf.bak"):
                if t.exists():
                    try:
                        t.chmod(S_IWRITE)
                        t.unlink()
                    except Exception as e:
                        self.logger.warning(f"Не удалось удалить {t}: {e}")
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

    def clear_sandboxie_cache(self, box_id: int):
        self.logger.debug(f"{self.username}: clear_sandboxie_cache — пропуск (автосэндбокс).")

    # ---------- window scans ----------
    def _any_login_hwnd_now(self, box_id: int) -> Optional[int]:
        pids = set(self._proc_tree_pids())
        return _any_login_hwnd_for_pids(pids) if pids else None

    def _login_window_absent_stably(self, box_id: int, duration: int = LOGIN_GONE_GRACE_SEC) -> bool:
        t0 = time.time()
        while time.time() - t0 < duration:
            if self._any_login_hwnd_now(box_id):
                return False
            time.sleep(0.5)
        return True

    def _find_login_hwnd_in_box(
        self,
        box_id: int,
        timeout_s: int = WAIT_LOGIN_WIN_TIMEOUT,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[int]:
        end = time.time() + timeout_s
        while time.time() < end:
            if stop_event and stop_event.is_set():
                return None
            pids = set(self._proc_tree_pids())
            if not pids:
                time.sleep(0.4)
                continue
            found = _any_login_hwnd_for_pids(pids)
            if found:
                return found
            # до логина — «новая» грубая обработка (координатами)
            self._handle_blockers_prelogin_once()
            time.sleep(0.4)
        return None

    # ---------- blockers ----------
    def _steamwebhelper_hwnds_in_tree(self, only_title_steam: bool = True) -> List[int]:
        """
        Возвращает окна steamwebhelper.exe из нашего дерева.
        """
        pids = set(self._proc_tree_pids())
        if not pids:
            return []
        hwnds: List[int] = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            pid = _window_pid(hwnd)
            if not pid or pid not in pids:
                return
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

    # --- старая (шаблонная) обработка: Continue Anyway ---
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
                    pt = _loc_center(img, confidence=confidence, region=region)
                except Exception:
                    pt = None
                if pt:
                    _click_point(pt, delay=0.05)
                    self.logger.info(
                        f"{self.username}: клик по '{os.path.basename(img)}' в окне {hex(hwnd)}."
                    )
                    p.sleep(0.25)
                    if close_after:
                        try:
                            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                        except Exception:
                            try:
                                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                            except Exception:
                                pass
                    return True
        except Exception:
            pass
        return False

    # --- новая (грубая) обработка до логина: «Отмена» по координатам ---
    def _click_cancel_by_coords(self, hwnd: int) -> bool:
        try:
            _force_foreground(hwnd)
            time.sleep(0.5)
            L, T, R, B = win32gui.GetWindowRect(hwnd)
            p.moveTo(L + (R-L)/2 + 70, B - 40)
            time.sleep(0.1)
            p.leftClick()
            self.logger.info(f"{self.username}: нажал «Отмена» в окне {hex(hwnd)} (по координатам).")
            return True
        except Exception:
            return False

    # --- предлогин: грубая обработка сервисных/безымянных окон ---
    def _handle_blockers_prelogin_once(self):
        """
        До логина: кликаем «Отмена» на безымянных/сервисных окнах по координатам.
        """
        pids = set(self._proc_tree_pids())
        if not pids:
            return

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            pid = _window_pid(hwnd)
            if not pid or pid not in pids:
                return
            title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
            # безымянные диалоги или текст про сервис
            if not title or "service" in title or "служб" in title or "ошибка службы" in title:
                self._click_cancel_by_coords(hwnd)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass


    def _handle_blockers_postlogin_once(self):
        blockers = self._steamwebhelper_hwnds_in_tree(only_title_steam=True)
        for hwnd in blockers:
            if self._click_continue_anyway_in_hwnd(
                hwnd, images_root="images/steam", confidence=0.88, close_after=True
            ):
                self.logger.info(f"{self.username}: подтвердил 'Продолжить' и закрыл окно Steam.")

    # ---------- wait-for-process ----------
    def wait_for_process_in_box(
        self,
        box_id: int,
        names: List[str],
        max_attempts: int = WAIT_STEAM_PROC_ATTEMPTS,
        interval: float = WAIT_STEAM_PROC_INTERVAL,
        match_fn: Optional[Callable[[psutil.Process], bool]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[psutil.Process]:
        target = {n.lower() for n in names if n}
        for _ in range(max_attempts):
            if stop_event and stop_event.is_set():
                return None
            for pid in self._proc_tree_pids():
                try:
                    pinfo = psutil.Process(pid)
                    if (pinfo.name() or "").lower() in target and (match_fn is None or match_fn(pinfo)):
                        return pinfo
                except Exception:
                    continue
            time.sleep(interval)
        return None

    # ---------- Stage 1: Launch → Login window ----------
    def launch_until_login_window(
        self,
        steam_path: str,
        app_id: int,
        box_id: int,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[int]:
        """
        Запуск steam.exe напрямую (автосэндбокс настроен снаружи).
        До логина снимаем сервисные блокеры грубо (координаты «Отмена»).
        """
        self.box_id = box_id
        self._steam_path = steam_path
        self._app_id = app_id

        try:
            self.kill_box_processes(box_id)
        except Exception:
            pass
        try:
            self.clear_real_steam_auth(steam_path)
        except Exception:
            pass

        if stop_event and stop_event.is_set():
            return None

        cmd = [steam_path, "-applaunch", str(app_id)]
        cmd.extend(DOTA_LAUNCH_OPTS)
        try:
            proc = subprocess.Popen(cmd)
            self._root_pid = int(proc.pid)
            self.logger.info(f"{self.username}: запуск Steam+Dota (PID={self._root_pid}) (автосэндбокс).")
        except Exception as e:
            self.logger.error(f"{self.username}: ошибка запуска → {e}")
            return None

        steam_proc = self.wait_for_process_in_box(box_id, ["steam.exe"], stop_event=stop_event)
        if not steam_proc or (stop_event and stop_event.is_set()):
            self.logger.error(f"{self.username}: steam.exe не появился")
            return None

        self.logger.info(f"{self.username}: жду окно входа Steam…")
        end = time.time() + WAIT_LOGIN_WIN_TIMEOUT
        hwnd = None
        while time.time() < end and not hwnd:
            if stop_event and stop_event.is_set():
                return None
            hwnd = self._find_login_hwnd_in_box(box_id, timeout_s=1, stop_event=stop_event)
            if hwnd:
                break
            self._handle_blockers_prelogin_once()
            time.sleep(0.4)

        if not hwnd:
            self.logger.error(f"{self.username}: окно входа не найдено")
            return None

        x, y, _, _ = self.placer.rect_for(box_id)
        _reposition_window_keep_size(hwnd, x, y)
        self.login_hwnd = hwnd
        self.set_status("ready")
        self.logger.info(f"{self.username}: окно входа найдено: HWND={hex(hwnd)} — готов к скану.")
        return hwnd

    # ---------- full restart on failed scan ----------
    def _full_restart_to_login(self, stop_event: Optional[threading.Event] = None) -> Optional[int]:
        if self._steam_path is None or self._app_id is None:
            return None
        try:
            self.kill_box_processes(self.box_id or -1)
        except Exception:
            pass
        try:
            self.clear_real_steam_auth(self._steam_path)
        except Exception:
            pass

        if stop_event and stop_event.is_set():
            return None

        cmd = [self._steam_path, "-applaunch", str(self._app_id)]
        cmd.extend(DOTA_LAUNCH_OPTS)
        try:
            proc = subprocess.Popen(cmd)
            self._root_pid = int(proc.pid)
            self.logger.info(f"{self.username}: перезапуск Steam+Dota (PID={self._root_pid})")
        except Exception as e:
            self.logger.error(f"{self.username}: ошибка перезапуска → {e}")
            time.sleep(RELAUNCH_DELAY_SEC)
            return None

        end = time.time() + WAIT_LOGIN_WIN_TIMEOUT
        hwnd = None
        while time.time() < end and not hwnd:
            if stop_event and stop_event.is_set():
                return None
            hwnd = self._find_login_hwnd_in_box(self.box_id or -1, timeout_s=1, stop_event=stop_event)
            if hwnd:
                break
            self._handle_blockers_prelogin_once()
            time.sleep(0.4)

        if not hwnd:
            self.logger.error(f"{self.username}: после рестарта окно входа не найдено")
            return None

        x, y, _, _ = self.placer.rect_for(self.box_id or 0)
        _reposition_window_keep_size(hwnd, x, y)
        self.login_hwnd = hwnd
        self.set_status("ready")
        self.logger.info(f"{self.username}: новое окно входа после рестарта: HWND={hex(hwnd)}")
        return hwnd

    # ---------- Stage 2: QR scan (ПОСЛЕ ЛОГИНА — старая обработка блокера) ----------
    def run_qr_scanner(self, stop_event: threading.Event) -> int:
        if self.box_id is None:
            self.set_status("error")
            return 1

        def _spawn_once(hwnd: int) -> int:
            import sys
            script = os.path.join(os.path.dirname(__file__), "qrLoger.py")
            if not os.path.exists(script):
                self.logger.error(f"{self.username}: нет qrLoger.py")
                self.set_status("error")
                return 1
            if not self.mafile_path:
                self.logger.error(f"{self.username}: нет maFile")
                self.set_status("error")
                return 1

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
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=env,
            )

            def _tail_stdout():
                try:
                    if self._qr_proc and self._qr_proc.stdout:
                        for line in self._qr_proc.stdout:
                            if not line:
                                break
                            self.logger.info(line.rstrip("\r\n"))
                except Exception:
                    pass

            t = threading.Thread(target=_tail_stdout, daemon=True)
            if self.thread_registry:
                try:
                    self.thread_registry.set(f"qr-tail-{self.username}", t, manage_stop=False)  # type: ignore[union-attr]
                except Exception:
                    pass
            t.start()

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
                            try:
                                self._qr_proc.terminate()
                            except Exception:
                                pass
                            break
                    else:
                        absent_since = None
                        # ПОСЛЕ ЛОГИНА: старая обработка блокера
                        self._handle_blockers_postlogin_once()
                    time.sleep(0.25)
            finally:
                if self._qr_proc and self._qr_proc.poll() is None:
                    try:
                        self._qr_proc.wait(timeout=3)
                    except Exception:
                        try:
                            self._qr_proc.kill()
                        except Exception:
                            pass

            rc = 0 if early_success else (
                self._qr_proc.returncode if self._qr_proc and self._qr_proc.returncode is not None else 1
            )
            self._qr_proc = None
            self.logger.info(f"{self.username}: qrLoger.py завершился с кодом {rc}.")
            return rc

        restarts_used = 0
        max_restarts = max(1, MAX_LAUNCH_RETRIES)

        hwnd = self.login_hwnd
        while not stop_event.is_set():
            if not hwnd or not _hwnd_exists(hwnd):
                self.logger.info(f"{self.username}: HWND невалиден — переобнаруживаю окно логина…")
                hwnd = self._find_login_hwnd_in_box(self.box_id, timeout_s=20, stop_event=stop_event)
                if not hwnd:
                    if self._login_window_absent_stably(self.box_id, duration=max(LOGIN_GONE_GRACE_SEC, 25)):
                        self.logger.info(f"{self.username}: окно логина устойчиво отсутствует — вход успешен.")
                        self.set_status("success")
                        return 0
                    continue

            rc = _spawn_once(hwnd)
            if rc == 0:
                self.set_status("success")
                return 0

            if restarts_used >= max_restarts:
                break
            restarts_used += 1
            self.logger.info(
                f"{self.username}: неудачный скан — перезапуск Steam (#{restarts_used}/{max_restarts})…"
            )
            hwnd = self._full_restart_to_login(stop_event)

        self.set_status("error")
        return 1

    # ---------- CPU limit ----------
    def _apply_cpu_limit(self, pid: int, percent: int):
        if percent <= 0:
            return
        try:
            proc = psutil.Process(pid)
            try:
                proc.nice(psutil.IDLE_PRIORITY_CLASS)  # мягкий лимит
            except Exception:
                pass
            self.logger.info(f"{self.username}: CPU ограничен мягко (приоритет=IDLE)")
        except Exception as e:
            self.logger.debug(f"{self.username}: не удалось применить мягкий лимит CPU: {e}")

    # ---------- Stage 3: wait Dota → arrange ----------
    def wait_dota_and_arrange(
        self,
        index_for_layout: int,
        cpu_limit_percent: int,
        max_wait: int = 180,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> bool:
        # ждём dota2.exe; ПОСЛЕ ЛОГИНА используем старую обработку блокера
        deadline = time.time() + max_wait
        while time.time() < deadline and not self.dota_pid:
            if stop_event and stop_event.is_set():
                return False

            proc = self.wait_for_process_in_box(
                self.box_id or 0, ["dota2.exe"], max_attempts=1, interval=0.2, stop_event=stop_event
            )
            if proc:
                self.dota_pid = proc.pid
                self.logger.info(f"{self.username}: Dota2 PID {self.dota_pid}")
                break

            self._handle_blockers_postlogin_once()
            time.sleep(0.5)

        if not self.dota_pid:
            self.logger.warning(f"{self.username}: не дождался dota2.exe")
            return False

        if stop_event and stop_event.is_set():
            return False

        t1 = time.time()
        hwnd = None
        while time.time() - t1 < 30:
            if stop_event and stop_event.is_set():
                return False
            hwnd = _find_main_window_for_pid(self.dota_pid)
            if hwnd:
                break
            time.sleep(0.4)
        if not hwnd:
            self.logger.warning(f"{self.username}: не нашёл главное окно Dota2")
            return False
        self.dota_hwnd = hwnd

        x, y, _, _ = self.placer.rect_for(index_for_layout)
        _reposition_window_keep_size(hwnd, x, y)
        try:
            win32gui.SetWindowText(hwnd, self.username)
        except Exception:
            pass

        self._apply_cpu_limit(self.dota_pid, cpu_limit_percent)
        self.set_status("ingame")
        return True

    # ---------- stop ----------
    def stop_and_cleanup_box(self):
        self.set_status("stopping")
        try:
            if self._qr_proc and self._qr_proc.poll() is None:
                self._qr_proc.terminate()
        except Exception:
            pass
        try:
            self.kill_box_processes(self.box_id or -1)
            self.clear_sandboxie_cache(self.box_id or -1)
        except Exception:
            pass
        self.set_status("idle")
