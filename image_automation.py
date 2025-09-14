# image_automation.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import logging
import ctypes
from ctypes import wintypes
from typing import List, Tuple, Optional, Callable, Union

import pyautogui as p
import win32gui
import win32con
import win32api
import win32process

Region = Tuple[int, int, int, int]  # (x, y, w, h)

# --- Win32 helpers for focus/hung checks ---
user32 = ctypes.windll.user32
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000

STEAMID64_OFFSET = 76561197960265728  # для конверсии в steam32/friend id


def steam64_to_friend_id_local(steamid64: Union[int, str, None]) -> Optional[str]:
    """
    Конвертирует steamid64 в Friend ID (цифры, steam32) = steamid64 - 76561197960265728.
    Вернёт None, если не удалось распарсить.
    """
    if steamid64 is None:
        return None
    try:
        v = int(str(steamid64).strip())
        acc_id = v - STEAMID64_OFFSET
        if acc_id > 0:
            return str(acc_id)
    except Exception:
        pass
    return None




def _client_region(hwnd: int) -> Region:
    """
    Возвращает регион клиентской области окна:
      (left, top, width, height) в экранных координатах.
    """
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)  # (0,0,w,h)
        (sx, sy) = win32gui.ClientToScreen(hwnd, (0, 0))
        w, h = max(1, r - l), max(1, b - t)
        return (sx, sy, w, h)
    except Exception:
        try:
            L, T, R, B = win32gui.GetWindowRect(hwnd)
            return (L, T, max(1, R - L), max(1, B - T))
        except Exception:
            return (0, 0, 1, 1)


def _is_window_responsive(hwnd: int, timeout_ms: int = 800) -> bool:
    """Пинг окна через SendMessageTimeout(WM_NULL). False — если зависло/не отвечает."""
    try:
        result = ctypes.c_ulong()
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            WM_NULL,
            0,
            0,
            SMTO_ABORTIFHUNG,
            timeout_ms,
            ctypes.byref(result),
        )
        return bool(ok)
    except Exception:
        # запасной вариант — просто IsWindow
        try:
            return bool(win32gui.IsWindow(hwnd))
        except Exception:
            return False


def _is_hung(hwnd: int) -> bool:
    try:
        return bool(user32.IsHungAppWindow(wintypes.HWND(hwnd)))
    except Exception:
        return False


def _window_ok(hwnd: int) -> bool:
    try:
        if not win32gui.IsWindow(hwnd):
            return False
    except Exception:
        return False
    if _is_hung(hwnd):
        return False
    return _is_window_responsive(hwnd)


def _focus_window(hwnd: int):
    """Фокусируем окно максимально жёстко (насколько позволяет ОС)."""
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
    time.sleep(0.05)


# --- PyAutoGUI helpers ---
def _loc_center(img_path: str, confidence: float = 0.87, region: Optional[Region] = None):
    try:
        return p.locateCenterOnScreen(img_path, confidence=confidence, region=region, grayscale=True)
    except Exception:
        return None


def _loc_count(img_path: str, confidence: float = 0.87, region: Optional[Region] = None) -> int:
    try:
        it = p.locateAllOnScreen(img_path, confidence=confidence, region=region, grayscale=True)
        return sum(1 for _ in it)
    except Exception:
        return 0


def _click_point(pt, delay: float = 0.0):
    try:
        p.moveTo(pt)
        if delay > 0:
            time.sleep(delay)
        p.leftClick()
    except Exception:
        pass


def _wait_until(cond: Callable[[], bool], timeout_s: float, poll: float = 0.25,
                stop_flag: Optional[Callable[[], bool]] = None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if stop_flag and stop_flag():
            return False
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


class ImageAutomation:
    """
    Высокоуровневый сценарий лобби/поиска матчей на чистом image matching.
    Все действия выполняются в контексте конкретного окна (hwnd, region):
    — принудительный фокус, проверка отклика, обработка «Добро пожаловать…».
    """

    def __init__(self, logger: logging.Logger, images_root: str = "images", confidence: float = 0.87):
        self.log = logger
        self.images = images_root
        self.conf = confidence

        # ассеты
        self.PNG = {
            "play": os.path.join(self.images, "lobby", "play.png"),
            "continue": os.path.join(self.images, "lobby", "continue.png"),
            "queue_again": os.path.join(self.images, "lobby", "queue.png"),
            "accept_eng": os.path.join(self.images, "lobby", "accept-eng.png"),
            "accept_ru": os.path.join(self.images, "lobby", "accept-ru.png"),
            "accept_invite_eng": os.path.join(self.images, "lobby", "accept-invite-eng.png"),
            "accept_invite_ru": os.path.join(self.images, "lobby", "accept-invite-ru.png"),
            "add_party": os.path.join(self.images, "lobby", "add-party.png"),
            "id_field_eng": os.path.join(self.images, "lobby", "id-field-eng.png"),
            "id_field_ru": os.path.join(self.images, "lobby", "id-field-ru.png"),
            "search_eng": os.path.join(self.images, "lobby", "search-eng.png"),
            "search_ru": os.path.join(self.images, "lobby", "search-ru.png"),
            "add": os.path.join(self.images, "lobby", "add.png"),
            "dota": os.path.join(self.images, "lobby", "dota.png"),
            "rank": os.path.join(self.images, "lobby", "rank.png"),
            "friend_id": os.path.join(self.images, "lobby", "friend-id.png"),
            "ok": os.path.join(self.images, "lobby", "ok.png"),
            "accept_reward": os.path.join(self.images, "lobby", "accept-reward.png"),

            # детект загрузки обеих сторон (радиант/даер)
            "detect_radiant": os.path.join(self.images, "game", "detect-radiant.png"),
            "detect_dire": os.path.join(self.images, "game", "detect-dire.png"),

            # приветственный экран / первый вход
            "welcome_not_new_ru": os.path.join(self.images, "welcome", "not_new_ru.png"),
            "welcome_not_new_en": os.path.join(self.images, "welcome", "not_new_en.png"),
            "welcome_continue": os.path.join(self.images, "welcome", "continue.png"),
            "welcome_ok": os.path.join(self.images, "welcome", "ok.png"),
            "welcome_got_it": os.path.join(self.images, "welcome", "got_it.png"),
        }

    # ---------- regions/windows ----------
    def regions_from_hwnds(self, hwnds: List[int]) -> List[Region]:
        return [_client_region(h) for h in hwnds]

    def windows_from_hwnds(self, hwnds: List[int]) -> List[Tuple[int, Region]]:
        return [(h, _client_region(h)) for h in hwnds]

    # ---------- readiness / welcome ----------
    def _wait_window_ready(self, hwnd: int, region: Region,
                           timeout: float = 25.0,
                           anchors: Optional[List[str]] = None,
                           stop_flag: Optional[Callable[[], bool]] = None) -> bool:
        """
        Ждём, пока окно начнёт отвечать; по возможности — появление якорных элементов UI.
        """
        if anchors is None:
            anchors = ["play", "add_party", "accept", "rank"]

        def _cond():
            if not _window_ok(hwnd):
                return False
            # если есть якоря — отлично, иначе считаем «готово», раз окно не висит
            for k in anchors:
                path = self.PNG.get(k)
                if not path or not os.path.exists(path):
                    continue
                if _loc_center(path, self.conf, region):
                    return True
            return True

        _focus_window(hwnd)
        ok = _wait_until(_cond, timeout, 0.3, stop_flag)
        if not ok:
            self.log.warning(f"[IMG] Окно {hex(hwnd)} не готово за {timeout:.0f}s (продолжаю).")
        return ok

    def _dismiss_welcome_if_present(self, hwnd: int, region: Region,
                                    timeout: float = 8.0,
                                    stop_flag: Optional[Callable[[], bool]] = None):
        """
        Если при первом запуске «Добро пожаловать…» — жмём «Я не новичок»/OK/Continue/Got it.
        """
        buttons = [
            "welcome_not_new_ru",
            "welcome_not_new_en",
            "welcome_continue",
            "welcome_got_it",
            "welcome_ok",
        ]

        t0 = time.time()
        while time.time() - t0 < timeout:
            if stop_flag and stop_flag():
                return
            if not _window_ok(hwnd):
                return

            _focus_window(hwnd)
            acted = False
            for key in buttons:
                path = self.PNG.get(key)
                if not path or not os.path.exists(path):
                    continue
                pt = _loc_center(path, self.conf, region)
                if pt:
                    _click_point(pt, delay=0.1)
                    self.log.info(f"[IMG] Welcome dismissed by '{key}'.")
                    time.sleep(0.6)
                    acted = True
            if acted:
                return
            time.sleep(0.3)

    # ---------- примитивы (с учётом hwnd) ----------
    def _type_text(self, text: str, interval: float = 0.02):
        try:
            if hasattr(p, "write"):
                p.write(text, interval=interval)
            else:
                p.typewrite(text, interval=interval)
        except Exception:
            pass

    def start_game(self, hwnd: int, region: Region):
        try:
            if not _window_ok(hwnd):
                return
            _focus_window(hwnd)
            pt = _loc_center(self.PNG["play"], self.conf, region)
            if pt:
                _click_point(pt)
                time.sleep(0.5)
                _click_point(pt)  # двойной клик
                time.sleep(0.8)
                cont = _loc_center(self.PNG["continue"], self.conf, region)
                if cont:
                    _click_point(cont)
        except Exception:
            pass

    def queue_again(self, hwnd: int, region: Region):
        try:
            if not _window_ok(hwnd):
                return
            _focus_window(hwnd)
            pt = _loc_center(self.PNG["queue_again"], self.conf, region)
            if pt:
                _click_point(pt, delay=0.2)
                time.sleep(0.2)
        except Exception:
            pass

    def accept_rewards_once(self, hwnd: int, region: Region):
        for key in ("accept_reward", "ok"):
            try:
                if not _window_ok(hwnd):
                    return
                _focus_window(hwnd)
                pt = _loc_center(self.PNG[key], self.conf, region)
                if pt:
                    _click_point(pt)
                    time.sleep(0.3)
            except Exception:
                pass

    def skip_rewards(self, windows: List[Tuple[int, Region]]):
        self.log.info("[IMG] Accepting rewards…")
        for _ in range(4):
            for hwnd, region in windows:
                self.accept_rewards_once(hwnd, region)
            time.sleep(1)
        self.log.info("[IMG] Rewards accepted")

    # ---------- пати/инвайты (без get_id — только по friend_id/steam64) ----------
    def invite_to_party(
        self,
        leader: Tuple[int, Region],
        friend_id: Optional[str] = None,
        steamid64: Optional[Union[int, str]] = None,
    ):
        """
        Инвайт строго по friend_id (или steamid64 → friend_id).
        Никаких UI-копирований.
        """
        l_hwnd, l_reg = leader

        fid = friend_id or steam64_to_friend_id_local(steamid64)
        if not fid:
            self.log.warning("[IMG] invite_to_party: friend_id не задан (и steamid64 нет) — пропуск игрока.")
            return

        self.log.info(f"[IMG] Inviting player by friend_id={fid}…")
        try:
            if not _window_ok(l_hwnd):
                return
            _focus_window(l_hwnd)
            time.sleep(0.2)
            add_party = _loc_center(self.PNG["add_party"], self.conf, l_reg)
            if add_party:

                _click_point(add_party)
                time.sleep(0.2)
            id_field = _loc_center(self.PNG["id_field_ru"], self.conf, l_reg)

            if id_field:
                time.sleep(0.1)
                _click_point(id_field)
                time.sleep(0.2)
                try:
                    p.hotkey("ctrl", "a")
                    p.press("backspace")
                except Exception:
                    pass
                self._type_text(fid, interval=0.02)

            search = _loc_center(self.PNG["search_ru"], self.conf, l_reg)
            if search:
                time.sleep(0.1)
                _click_point(search)
                time.sleep(0.5)

            add = _loc_center(self.PNG["add"], self.conf, l_reg)
            if add:
                time.sleep(0.1)
                _click_point(add)
                time.sleep(0.5)

            dota = _loc_center(self.PNG["dota"], self.conf, l_reg)
            if dota:
                time.sleep(0.1)
                _click_point(dota)
                time.sleep(1.0)

        except Exception as e:
            print(e)
            pass
        self.log.info("[IMG] Player invited")

    def make_parties(
        self,
        windows: List[Tuple[int, Region]],
        friend_ids: Optional[List[Optional[str]]] = None,
        steamids64: Optional[List[Optional[Union[int, str]]]] = None,
    ):
        """
        Формирует 1 или 2 стака по friend_ids / steamids64.
        Если ни friend_ids, ни steamids64 нет — пати не собираем (UI-копирования больше нет).
        """
        n = len(windows)
        if n < 5:
            self.log.info("[IMG] Недостаточно аккаунтов для пати (<5) — пропуск make_parties")
            return

        # собрать список fid для индексов 0..n-1
        def _fid(i: int) -> Optional[str]:
            v = None
            if friend_ids and i < len(friend_ids):
                v = friend_ids[i]
            if (not v) and steamids64 and i < len(steamids64):
                v = steam64_to_friend_id_local(steamids64[i])
            return v or None

        if not friend_ids and not steamids64:
            self.log.warning("[IMG] friend_ids/steamids64 не переданы — пропускаю сбор пати.")
            return

        if n >= 10:
            self.log.info("[IMG] Inviting players for stack #1")
            leader1 = windows[0]
            for idx in range(1, 5):
                fid = _fid(idx)
                if fid:
                    self.invite_to_party(leader1, friend_id=fid)

            self.log.info("[IMG] Inviting players for stack #2")
            leader2 = windows[5]
            for idx in range(6, 10):
                fid = _fid(idx)
                if fid:
                    self.invite_to_party(leader2, friend_id=fid)
        else:
            self.log.info("[IMG] Inviting players for single stack")
            leader = windows[0]
            for idx in range(1, min(5, n)):
                fid = _fid(idx)
                if fid:
                    self.invite_to_party(leader, friend_id=fid)

        # принять инвайты всеми окнами
        self.log.info("[IMG] Accepting invitations")
        for hwnd, region in windows:
            try:
                if not _window_ok(hwnd):
                    continue
                _focus_window(hwnd)
                pt = _loc_center(self.PNG["accept_invite_ru"], self.conf, region)
                if pt:
                    _click_point(pt)
                    time.sleep(0.3)
            except Exception:
                pass

    # ---------- основной сценарий поиска (по окнам) ----------
    def search_games(self, windows: List[Tuple[int, Region]],
                     should_make_party: bool = False,
                     stop_flag: Optional[Callable[[], bool]] = None):
        """
        Логика:
          - опционально собрать пати
          - старт поиска у лидеров (0 и 5) или только 0, если окон меньше 10
          - ждать «accept» (10 из 10) / при “застревании” — переочередить
          - принять матч всеми
          - ждать загрузки обеих сторон (5/5)
        """
        if should_make_party:
            self.log.info("[IMG] make_parties=True передан сюда, но friend_ids задаются в run_with_hwnds — игнорирую.")
            # make_parties вызывается в run_with_hwnds, чтобы иметь доступ к friend_ids

        time.sleep(2)
        self.log.info("[IMG] Starting search")

        if len(windows) >= 10:
            self.start_game(*windows[0])
            self.start_game(*windows[5])
        else:
            self.start_game(*windows[0])

        self.log.info("[IMG] Waiting for games…")

        # Ожидание подборов и переочередь
        while True:
            if stop_flag and stop_flag():
                return
            time.sleep(1)

            founded_games = _loc_count(self.PNG["accept_ru"], self.conf)  # глобально

            if founded_games == 5:
                self.log.info("[IMG] 5 players found the game; waiting for another 5…")
                time.sleep(1.5)
                if _loc_count(self.PNG["accept"], self.conf) == 5:
                    self.log.info("[IMG] Another 5 players didn't find the game — requeue")
                    if len(windows) >= 10:
                        self.queue_again(*windows[0])
                        self.queue_again(*windows[5])
                    else:
                        self.queue_again(*windows[0])
                    self.log.info("[IMG] Searching again…")

            if founded_games >= 10 or (len(windows) < 10 and founded_games >= 5):
                self.log.info("[IMG] Game is found")
                break

        # принимаем матч во всех окнах
        self.log.info("[IMG] Accepting games")
        time.sleep(1)
        for hwnd, region in windows:
            if stop_flag and stop_flag():
                return
            try:
                if not _window_ok(hwnd):
                    continue
                _focus_window(hwnd)
                pt = _loc_center(self.PNG["accept"], self.conf, region)
                if pt:
                    p.moveTo(pt)
                    time.sleep(0.25)
                    p.leftClick()
                    time.sleep(0.25)
            except Exception:
                pass

        self.log.info("[IMG] Games accepted")
        self.log.info("[IMG] Waiting for players to load…")

        # ждём когда обе стороны 5/5
        while True:
            if stop_flag and stop_flag():
                return
            time.sleep(1)
            try:
                radiant_count = _loc_count(self.PNG["detect_radiant"], self.conf)
                dire_count = _loc_count(self.PNG["detect_dire"], self.conf)
                if radiant_count >= 5 and dire_count >= 5:
                    break
            except Exception:
                pass

        self.log.info("[IMG] All players are loaded")

    # ---------- high-level ----------
    def run_with_hwnds(self,
                       hwnds: List[int],
                       make_party: bool = True,
                       stop_flag: Optional[Callable[[], bool]] = None,
                       friend_ids: Optional[List[Optional[str]]] = None,
                       steamids64: Optional[List[Optional[Union[int, str]]]] = None):
        """
        Построить (hwnd,region), убедиться что окна живые и готовы,
        убрать приветствие, принять награды, при необходимости собрать пати
        (по friend_ids / steamids64), затем — поиск/акцепт/ожидание.
        """
        windows = self.windows_from_hwnds(hwnds)
        if not windows:
            self.log.warning("[IMG] Нет окон — выходим")
            return

        # Готовность + welcome
        for hwnd, region in windows:
            if stop_flag and stop_flag():
                return
            #self._wait_window_ready(hwnd, region, timeout=25.0, stop_flag=stop_flag)
            #self._dismiss_welcome_if_present(hwnd, region, timeout=8.0, stop_flag=stop_flag)

        # Награды/попапы
        #self.skip_rewards(windows)

        # Пати (только по friend_ids/steamids64)
        if make_party:
            self.make_parties(windows, friend_ids=friend_ids, steamids64=steamids64)

        # Основной сценарий поиска
        self.search_games(windows, should_make_party=False, stop_flag=stop_flag)

# --- temporary main() using Sandboxie PIDs & mafiles ---
import re
import json
import subprocess
import shutil
from glob import glob

def _sandboxie_path() -> str:
    env = os.environ.get("SANDBOXIE_START_EXE")
    candidates = [
        env,
        "Start.exe",
        r"C:\Program Files\Sandboxie-Plus\Start.exe",
        r"C:\Program Files\Sandboxie\Start.exe",
    ]
    for c in candidates:
        if not c:
            continue
        # абсолютный путь
        if os.path.isabs(c) and os.path.isfile(c):
            return c
        # поиск в PATH
        w = shutil.which(c)
        if w:
            return w
    raise FileNotFoundError("Sandboxie Start.exe not found. "
                            "Set SANDBOXIE_START_EXE or add Start.exe to PATH.")


def get_box_pids(box_id: int) -> List[int]:
    try:
        start_exe = _sandboxie_path()
    except Exception as e:
        logging.getLogger("image_automation").error(f"[SBIE] {e}")
        return []
    try:
        cp = subprocess.run(
            [start_exe, f"/box:{box_id}", "/listpids"],
            text=True, capture_output=True, check=True
        )
        out = (cp.stdout or "").strip()
        if not out:
            logging.getLogger("image_automation").warning(
                f"[SBIE] Empty output for box {box_id}; stderr='{(cp.stderr or '').strip()}'")
            return []
        lines = out.splitlines()
        # Первая строка — количество PIDs
        try:
            count = int(lines[0])
        except Exception:
            logging.getLogger("image_automation").warning(
                f"[SBIE] Unexpected header for box {box_id}: '{lines[0] if lines else ''}'")
            return []
        pids = []
        for s in lines[1:1+count]:
            try:
                pids.append(int(s))
            except Exception:
                logging.getLogger("image_automation").warning(f"[SBIE] Bad PID token: '{s}'")
        if len(pids) != count:
            logging.getLogger("image_automation").warning(
                f"[SBIE] Parsed {len(pids)} of {count} PIDs for box {box_id}")
        return pids
    except subprocess.CalledProcessError as e:
        logging.getLogger("image_automation").error(
            f"[SBIE] Start.exe failed for box {box_id} (rc={e.returncode}). "
            f"stderr='{(e.stderr or '').strip()}'")
        return []
    except Exception:
        logging.getLogger("image_automation").exception(f"[SBIE] get_box_pids({box_id}) failed")
        return []
def collect_windows_by_titles_from_mafiles(account_names_lower: set[str]) -> List[Tuple[int, str, int]]:
    """
    Возвращает [(hwnd, title_login, pid)] для всех видимых топ-левел окон,
    у которых заголовок (без пробелов по краям) совпадает с account_name (case-insensitive).
    """
    result: List[Tuple[int, str, int]] = []

    def _enum_cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetParent(hwnd):
                return
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return
            if title.lower() in account_names_lower:
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                result.append((hwnd, title, pid))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_enum_cb, None)
    except Exception:
        pass
    return result

def collect_windows_from_boxes_or_fallback(mafile_index: dict[str, tuple[str, str]],
                                           box_ids: List[int]) -> List[Tuple[int, str, int]]:
    """
    Сначала пробуем Sandboxie (box_ids), если пусто — fallback по заголовкам, совпадающим с account_name.
    """
    logger = logging.getLogger("image_automation")

    # 1) Sandboxie путь
    triplets: List[Tuple[int, str, int]] = []
    seen_hwnds: set[int] = set()
    for box_id in box_ids:
        pids = get_box_pids(box_id)
        logger.info(f"[SBIE] box {box_id}: PIDs={pids}")
        for pid in pids:
            hwnd = find_main_hwnd_for_pid(pid)
            if hwnd and hwnd not in seen_hwnds:
                title = _get_window_title(hwnd).strip()
                if title:
                    seen_hwnds.add(hwnd)
                    triplets.append((hwnd, title, pid))

    if triplets:
        return triplets

    # 2) Fallback по заголовкам
    logger.warning("[MAIN] Не нашли окон по Sandboxie. Пробую резервный поиск по заголовкам (account_name).")
    names_lower = set(mafile_index.keys())
    return collect_windows_by_titles_from_mafiles(names_lower)

def parse_steamid64_from_filename(path: str) -> Optional[str]:
    """
    Берём steamid64 только из имени файла (как вы и сказали).
    Ищем любую 15-20-значную последовательность цифр в stem.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"\d{15,20}", stem)
    return m.group(0) if m else None

def load_mafile_index_from_filenames(mafiles_dir: str) -> dict[str, tuple[str, str]]:
    """
    Строим индекс по account_name -> (steamid64, full_path).
    steamid64 берём ТОЛЬКО из имени файла.
    """
    index: dict[str, tuple[str, str]] = {}
    patterns = [os.path.join(mafiles_dir, "*.maFile"),
                os.path.join(mafiles_dir, "*.mafile"),
                os.path.join(mafiles_dir, "*.json")]
    files: List[str] = []
    for pat in patterns:
        files.extend(glob(pat))
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            acc = (data.get("account_name") or "").strip()
            if not acc:
                continue
            sid64 = parse_steamid64_from_filename(path)
            if not sid64:
                continue
            index[acc.lower()] = (sid64, path)
        except Exception:
            continue
    return index

def _get_window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""

def _is_main_candidate(hwnd: int) -> bool:
    try:
        if not win32gui.IsWindow(hwnd):
            return False
        if not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.GetParent(hwnd):
            return False  # только топ-левел
        title = _get_window_title(hwnd).strip()
        if not title:
            return False
        return True
    except Exception:
        return False

def _window_area(hwnd: int) -> int:
    try:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return max(0, R - L) * max(0, B - T)
    except Exception:
        return 0



def collect_windows_from_boxes(box_ids: List[int]) -> List[Tuple[int, str, int]]:
    """
    Возвращает [(hwnd, login_title, pid)] для всех боксов в порядке box_ids.
    Заголовок окна используется как логин (без 'Dota 2').
    """
    result: List[Tuple[int, str, int]] = []
    seen_hwnds: set[int] = set()
    for box_id in box_ids:
        pids = get_box_pids(box_id)
        for pid in pids:
            hwnd = find_main_hwnd_for_pid(pid)
            if not hwnd or hwnd in seen_hwnds:
                continue
            title = _get_window_title(hwnd).strip()
            if not title:
                continue
            seen_hwnds.add(hwnd)
            result.append((hwnd, title, pid))
    return result

def find_main_hwnd_for_pid(pid: int) -> Optional[int]:
    """
    Ищем главное окно процесса по PID: берём самый «крупный» видимый топ-левел с ненулевым заголовком.
    """
    candidates: List[int] = []
    def _enum_cb(hwnd, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid and _is_main_candidate(hwnd):
                candidates.append(hwnd)
        except Exception:
            pass
    try:
        win32gui.EnumWindows(_enum_cb, None)
    except Exception:
        pass
    if not candidates:
        return None
    # выбираем окно с максимальной площадью как главное
    candidates.sort(key=_window_area, reverse=True)
    return candidates[0]
def main():
    """
    1) читаем mafiles -> {account_name: steamid64} (steamid64 только из имени файла)
    2) собираем окна по PID’ам из Sandboxie боксов 0..4; title окна = steam login
    3) матчим login (из title) с account_name (из mafile) -> steamid64
    4) запускаем ImageAutomation.run_with_hwnds(hwnds, steamids64)
    """
    # Логгер
    logger = logging.getLogger("image_automation")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(ch)

    # 1) mafiles
    mafiles_dir = os.path.join(os.getcwd(), "mafiles")
    idx = load_mafile_index_from_filenames(mafiles_dir)
    if not idx:
        logger.warning(f"[MAIN] Не нашёл подходящих mafile в '{mafiles_dir}'.")
    else:
        logger.info(f"[MAIN] Индекс mafile: {len(idx)} аккаунтов.")

    # 2) окна из боксов 0..4
    box_ids = list(range(5))
    win_triplets = collect_windows_from_boxes_or_fallback(idx, box_ids)

    if not win_triplets:
        logger.warning("[MAIN] Не нашёл окон в указанных боксах 0..4.")
        return

    # 3) сопоставляем логины -> steamid64
    hwnds: List[int] = []
    steamids64: List[Optional[str]] = []
    for hwnd, title_login, pid in win_triplets:
        login_key = title_login.strip().lower()
        hwnds.append(hwnd)
        pair = idx.get(login_key)
        if pair:
            sid64, path = pair
            steamids64.append(sid64)
            logger.info(f"[MAIN] '{title_login}' -> steamid64={sid64} (file: {os.path.basename(path)})")
        else:
            steamids64.append(None)
            logger.warning(f"[MAIN] Для '{title_login}' mafile/account_name не найден.")

    # 4) поехали
    ia = ImageAutomation(logger, images_root="images", confidence=0.87)
    ia.run_with_hwnds(
        hwnds=hwnds,
        make_party=True,
        stop_flag=None,
        friend_ids=None,
        steamids64=steamids64
    )

if __name__ == "__main__":
    hwnd = find_main_hwnd_for_pid(16244)
    print(hwnd)
    #main()

