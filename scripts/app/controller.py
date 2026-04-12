# controller.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import time
import logging
import threading
from queue import Queue, Empty
from typing import Optional, List, Dict, Tuple, Callable

import psutil
import win32gui
import win32con
import win32api
import win32process

from scripts.core.account import Account
from scripts.core.config import *
from scripts.app.window_placer import WindowPlacer
from scripts.game.game_automation import GameAutomation  # <-- новый модуль
from scripts.app.thread_registry import ThreadRegistry

user32 = ctypes.windll.user32


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


def _get_screen_size() -> Tuple[int, int]:
    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)


def _reposition_window_keep_size(hwnd: int, x: int, y: int):
    try:
        sw, sh = _get_screen_size()
        x = max(0, min(x, sw - 50))
        y = max(0, min(y, sh - 50))
        win32gui.SetWindowPos(
            hwnd, None, x, y, 0, 0, win32con.SWP_NOZORDER | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        )
    except Exception:
        pass


class Controller:
    def __init__(self, logger: logging.Logger, placer: WindowPlacer, status_cb: Callable[[str, str], None], *, thread_registry: Optional[ThreadRegistry] = None):
        self.logger = logger
        self.placer = placer
        self.status_cb = status_cb

        self.thread_registry = thread_registry or ThreadRegistry()

        self.accounts: List[Account] = []
        self.mafile_index: Dict[str, Tuple[str, dict]] = {}
        self.stop_event = threading.Event()

        self.state_file = "../../state.json"

        self._ready_lock = threading.Lock()
        self._ready_count = 0
        self._target_ready = 0
        self._all_ready_event = threading.Event()

        self.session_started_at: Optional[float] = None
        self.cpu_limit_percent: int = 5

        self.per_proc_cpu: Dict[str, float] = {}
        self.per_box_cpu: Dict[str, float] = {}

        self.farm_running: bool = False
        self.farming_accounts : List[Account] = []
        # автоматика по картинкам
        self._auto_started = False
        self._arranged_lock = threading.Lock()
        self._arranged_hwnds: List[int] = []  # hwnd'ы Dota, которые уже выстроены

        self.load_state()

    # -------- utils: процессы бокса / CPU watch (как было) --------
    def _box_proc_list(self, acc: "Account", all_procs: bool = True) -> List[psutil.Process]:
        procs: List[psutil.Process] = []
        if acc.box_id is None:
            return procs
        pids = set(acc.get_box_pids(acc.box_id))
        if not pids:
            return procs
        for pid in list(pids):
            try:
                p = psutil.Process(pid)
                if all_procs:
                    procs.append(p)
            except Exception:
                pass
        return procs

    def _box_cpu_percent_once(self, procs: List[psutil.Process]) -> float:
        total = 0.0
        for p in procs:
            try:
                total += p.cpu_percent(interval=0.0)
            except Exception:
                continue
        cores = psutil.cpu_count(logical=True) or 1
        total_norm = max(0.0, min(100.0, total / cores))
        return total_norm

    def start_cpu_watch(self):
        def loop(stop_event: threading.Event):
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
            dota_proc: Dict[str, Optional[psutil.Process]] = {}
            while not stop_event.is_set():
                for acc in self.accounts:
                    if acc.dota_pid:
                        proc = dota_proc.get(acc.username)
                        if (proc is None) or (proc and not proc.is_running()):
                            try:
                                dota_proc[acc.username] = psutil.Process(acc.dota_pid)
                            except Exception:
                                dota_proc[acc.username] = None
                for name, proc in list(dota_proc.items()):
                    val = 0.0
                    try:
                        if proc:
                            val = proc.cpu_percent(interval=0.0)
                    except Exception:
                        val = 0.0
                    cores = psutil.cpu_count(logical=True) or 1
                    self.per_proc_cpu[name] = max(0.0, min(100.0, val / cores))

                for acc in self.accounts:
                    procs = self._box_proc_list(acc, all_procs=True)
                    for p in procs:
                        try:
                            p.cpu_percent(interval=None)
                        except Exception:
                            pass
                    self.per_box_cpu[acc.username] = self._box_cpu_percent_once(procs)
                time.sleep(2.0)

        self.thread_registry.add("cpu_watch", loop)

    def stop_cpu_watch(self):
        self.thread_registry.remove("cpu_watch", signal_stop=True)

    # -------- аккаунты / mafiles / состояние (как было) --------
    def load_accounts_from_txt(self, path: str, append=False):
        if not append:
            self.accounts.clear()
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                login, password = line.split(":", 1)
                if any(a.username == login for a in self.accounts):
                    continue
                acc = Account(login, password, self.logger, self.placer, self.status_cb, thread_registry=self.thread_registry)
                self.accounts.append(acc)
                count += 1
                self.logger.info(f"Добавлен аккаунт: {login}")
        self.logger.info(f"Загружено аккаунтов из TXT: {count}")

    def build_mafile_index(self, folder: str):
        self.mafile_index.clear()
        total = ok = 0
        for root, _, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith((".mafile", ".json")):
                    continue
                total += 1
                path = os.path.join(root, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    acc_name = (data.get("account_name") or "").lower()
                    if acc_name:
                        self.mafile_index[acc_name] = (path, data)
                        ok += 1
                except Exception as e:
                    self.logger.error(f"Ошибка maFile '{path}': {e}")
        self.logger.info(f"maFiles просканировано: {total}, пригодных: {ok}")

    def match_mafiles_to_accounts(self):
        matched = 0
        for acc in self.accounts:
            tpl = self.mafile_index.get(acc.username.lower())
            if tpl:
                path, data = tpl
                acc.attach_mafile(path, data)
                matched += 1
        self.logger.info(f"Привязано maFile: {matched}/{len(self.accounts)}")

    def selected_accounts(self, names: List[str]) -> List[Account]:
        by = {a.username: a for a in self.accounts}
        return [by[n] for n in names if n in by]

    def remove_accounts(self, usernames: List[str]):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.username not in usernames]
        self.logger.info(f"Удалено аккаунтов: {before - len(self.accounts)}. Текущих: {len(self.accounts)}")

    def save_state(self):
        data = {"steam_path": getattr(self, "steam_path", ""), "accounts": []}
        for acc in self.accounts:
            data["accounts"].append(
                {"username": acc.username, "password": acc.password, "mafile_path": acc.mafile_path, "mafile_data": acc.mafile_data}
            )
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.logger.info("Состояние аккаунтов сохранено")

    def load_state(self):
        if not os.path.exists(self.state_file):
            return
        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.accounts.clear()
        self.steam_path = data.get("steam_path", r"C:\Program Files (x86)\Steam\steam.exe")
        for accd in data.get("accounts", []):
            acc = Account(accd["username"], accd["password"], self.logger, WindowPlacer(), self.status_cb, thread_registry=self.thread_registry)
            acc.mafile_path = accd.get("mafile_path")
            acc.mafile_data = accd.get("mafile_data")
            if acc.mafile_path and not acc.mafile_data:
                try:
                    with open(acc.mafile_path, "r", encoding="utf-8") as f:
                        acc.mafile_data = json.load(f)
                except Exception:
                    pass
            if acc.mafile_data:
                try:
                    acc.attach_mafile(acc.mafile_path, acc.mafile_data)
                except Exception:
                    pass
            self.accounts.append(acc)
        self.logger.info(f"Загружено аккаунтов из состояния: {len(self.accounts)}")

    # -------- внутр. синхронизация готовности логин-окон (оставлено) --------
    def _mark_ready(self):
        with self._ready_lock:
            self._ready_count += 1
            self.logger.info(f"Окно логина готово {self._ready_count}/{self._target_ready}")
            if self._ready_count >= self._target_ready:
                self._all_ready_event.set()

    def _wait_all_ready_or_timeout(self):
        self.logger.info(f"Ожидаю окна входа у всех ({self._target_ready}) или таймаут {STARTUP_SYNC_TIMEOUT}s…")
        self._all_ready_event.wait(timeout=STARTUP_SYNC_TIMEOUT)
        if self._all_ready_event.is_set():
            self.logger.info("Все клиенты подготовлены — начинаю скан через пару секунд…")
        else:
            self.logger.warning("Не все успели — начинаю скан имеющихся.")
        time.sleep(DELAY_BEFORE_SCANNING_ALL_READY)

    # --------- запуск image-automation один раз, когда готово нужное число окон ---------
    def _try_start_image_automation(self, total_planned: int, make_party: bool):
        """
        Стартует ImageAutomation один раз, когда набралось достаточно окон:
          • если >=10 — берём первые 10 (2 стака), иначе если >=5 — первые 5 (1 стак), иначе — все.
        """
        if self._auto_started:
            return

        with self._arranged_lock:
            count = len(self._arranged_hwnds)
            if count == 0:
                return

            need = 10 if total_planned >= 10 else (5 if total_planned >= 5 else total_planned)
            if count < need:
                return

            hwnds = self._arranged_hwnds[:need]
            self._auto_started = True

        self.logger.info(f"[IMG] Запускаю автоматизацию по картинкам для {len(hwnds)} окон (need={need})")

        def _runner(stop_event: threading.Event):
            try:
                # какие аккаунты задействованы в этой автоматики (по hwnd'ам)
                accounts_for_ga = [acc for acc in self.farming_accounts
                                   if getattr(acc, "dota_hwnd", None) in hwnds]

                steamids = [acc.steam_id for acc in accounts_for_ga if getattr(acc, "steam_id", None)]

                game = GameAutomation(self.logger, images_root="images", confidence=0.87)

                ga_done = threading.Event()

                def _ga():
                    try:
                        game.run_with_hwnds(
                            hwnds,
                            make_party=make_party,
                            stop_flag=lambda: stop_event.is_set() or self.stop_event.is_set() or (
                                not self.farm_running),
                            steamids64=steamids,
                        )
                    finally:
                        ga_done.set()

                t = threading.Thread(target=_ga, daemon=True)
                t.start()

                # PULL: опрашиваем локальный статус и транслируем в UI аккаунтов
                last = None
                while not ga_done.is_set() and not stop_event.is_set() and self.farm_running:
                    s = game.get_status()
                    if s and s != last:
                        last = s
                        for acc in accounts_for_ga:
                            try:
                                acc.set_status(s)
                            except Exception:
                                pass
                    time.sleep(0.5)

            except Exception as e:
                self.logger.error(f"[IMG] Ошибка автоматики: {e}")
                for acc in self.farming_accounts:
                    acc.set_status("error")
            finally:
                self.logger.info("[IMG] Автоматика завершилась")

        self.thread_registry.add("image_auto", _runner)

    # --------- главный конвейер ---------
    def start_farming(self, steam_path: str, app_id: int, selected_accounts: List[Account], max_parallel: int,
                      make_party: bool = True):
        """
        • каждому аккаунту свой box_id = индекс в selected_accounts
        • воркеры до max_parallel: Steam → окно логина → QR → Dota/раскладка/лимит
        • как только собрались нужные окна — стартуем image-automation (один раз)
        """
        self.thread_registry.exit()
        if self.farm_running:
            self.logger.warning("Фарм уже активен")
            return
        if not selected_accounts:
            self.logger.warning("Нет аккаунтов для запуска")
            return

        # init
        self.farm_running = True
        self.stop_event.clear()
        self.session_started_at = time.time()
        self.start_cpu_watch()
        self.steam_path = steam_path
        self.farming_accounts = selected_accounts
        # сброс автоматики
        self._auto_started = False
        with self._arranged_lock:
            self._arranged_hwnds = []

        # очередь задач (box_id = индекс)
        tasks: Queue[Tuple[Account, int]] = Queue()
        for idx, acc in enumerate(selected_accounts):
            tasks.put((acc, idx))

        # резервные аккаунты, которые не были изначально выбраны
        reserve: Queue[Account] = Queue()
        for acc in self.accounts:
            if acc not in selected_accounts:
                reserve.put(acc)

        def _schedule_replacement(box_id: int):
            """Подставить следующий аккаунт из резерва вместо ошибочного."""
            if not self.farm_running or self.stop_event.is_set():
                return
            try:
                next_acc = reserve.get_nowait()
            except Empty:
                return
            self.farming_accounts.append(next_acc)
            tasks.put((next_acc, box_id))

        workers = max(1, int(max_parallel))
        workers = min(workers, tasks.qsize())
        self.logger.info(f"Фарм: параллельность={workers}, аккаунтов={tasks.qsize()}")

        def worker(worker_id: int):
            while self.farm_running and not self.stop_event.is_set():
                try:
                    acc, box_id = tasks.get_nowait()
                except Empty:
                    break
                try:
                    if not self.farm_running or self.stop_event.is_set():
                        break

                    # 1) запуск Steam+Dota
                    acc.set_status("launching")
                    self.logger.info(f"{acc.username}: бокс #{box_id}")
                    hwnd = acc.launch_until_login_window(
                        self.steam_path, app_id, box_id=box_id, stop_event=self.stop_event
                    )
                    if not hwnd:
                        acc.set_status("error")
                        _schedule_replacement(box_id)
                        continue

                    # 2) (опц.) локальный calm
                    try:
                        self.wait_cpu_calm([acc])
                        self._refresh_login_windows_after_calm([acc], stop_event=self.stop_event)
                    except Exception:
                        pass

                    # 3) QR
                    rc = acc.run_qr_scanner(self.stop_event)
                    if rc != 0:
                        self.logger.error(f"{acc.username}: QR rc={rc}")
                        acc.set_status("error")
                        _schedule_replacement(box_id)
                        continue
                    self.logger.info(f"{acc.username}: QR подтверждён")

                    # 4) Dota окно/раскладка/лимит
                    ok = acc.wait_dota_and_arrange(
                        index_for_layout=box_id,
                        cpu_limit_percent=self.cpu_limit_percent,
                        stop_event=self.stop_event,
                    )
                    if not ok:
                        acc.set_status("error")
                        _schedule_replacement(box_id)
                        continue

                    # зарегистрируем hwnd для автоматики
                    if acc.dota_hwnd:
                        with self._arranged_lock:
                            if acc.dota_hwnd not in self._arranged_hwnds:
                                self._arranged_hwnds.append(acc.dota_hwnd)

                    # пробуем стартануть image-automation, когда готово достаточно окон
                    self._try_start_image_automation(total_planned=len(selected_accounts), make_party=make_party)

                except Exception as e:
                    self.logger.error(f"{acc.username}: исключение воркера: {e}")
                    acc.set_status("error")
                    _schedule_replacement(box_id)

                finally:
                    try:
                        tasks.task_done()
                    except Exception:
                        pass

            self.logger.debug(f"Воркер #{worker_id} завершён")

        for wid in range(workers):
            t = threading.Thread(target=worker, args=(wid,), daemon=True)
            self.thread_registry.set(f"worker-{wid}", t, stop_event=self.stop_event)
            t.start()

        # финишер: ждём, пока развернём все окна; автоматика работает отдельно
        def finisher():
            while not self.stop_event.is_set():
                if tasks.unfinished_tasks == 0:
                    break
                time.sleep(0.5)
            self.logger.info("Все аккаунты обработаны конвейером (окна подняты/разложены). Автоматика — в отдельном потоке.")

        fin = threading.Thread(target=finisher, daemon=True)
        self.thread_registry.set("finisher", fin, stop_event=self.stop_event)
        fin.start()

    def stop_farming(self):
        self.stop_event.set()
        for acc in self.accounts:
            try:
                acc.stop_and_cleanup_box()
            except Exception:
                pass
        self.stop_cpu_watch()
        self.thread_registry.exit()
        self.session_started_at = None
        self.farm_running = False

        # автоматика
        self._auto_started = False
        with self._arranged_lock:
            self._arranged_hwnds = []

        self.logger.info("Фарм остановлен")

    # --- вспомогательное (после calm — переобнаружение логин-окон/перестановка) ---
    def _refresh_login_windows_after_calm(
        self, accounts: List["Account"], *, stop_event: Optional[threading.Event] = None
    ):
        for acc in accounts:
            if stop_event and stop_event.is_set():
                break
            if acc.box_id is None:
                continue
            new_hwnd = acc._find_login_hwnd_in_box(acc.box_id, timeout_s=5, stop_event=stop_event)
            if new_hwnd and (acc.login_hwnd != new_hwnd):
                acc.login_hwnd = new_hwnd
                x, y, _, _ = self.placer.rect_for(acc.box_id)
                try:
                    _reposition_window_keep_size(new_hwnd, x, y)
                    _force_foreground(new_hwnd)
                except Exception:
                    pass
                self.logger.info(f"{acc.username}: логин-окно переобнаружено → {hex(new_hwnd)}")

    # --- заглушка «часы» для UI, можешь оставить ---
    def mm_hours_stub(self, accounts: List[Account]) -> Dict[str, float]:
        res: Dict[str, float] = {}
        now = time.time()
        for acc in accounts:
            if self.session_started_at and acc.status in ("ingame", "gc_ready", "queueing", "success"):
                res[acc.username] = (now - self.session_started_at) / 3600.0
            else:
                res[acc.username] = 0.0
        return res
