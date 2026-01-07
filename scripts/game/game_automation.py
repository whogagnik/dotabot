# game_automation_merged.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import time
import logging

from typing import List, Tuple, Optional, Callable, Union
import threading
import dxcam
import numpy as np
from PIL import Image
from scripts.core.CONSTANTS import *           # STATUS_LABELS / STATUS_COLORS и прочие константы — из общего файла
from scripts.core.utils import _force_foreground,_window_area,find_main_hwnd_for_pid, _window_ok, steam64_to_friend_id_local
# === External deps ===
import pyautogui as p

Region = Tuple[int, int, int, int]  # (x, y, w, h)


_UI_REF_W, _UI_REF_H = 1920, 1080  # эталон под который сняты PNG


# --- Global DXCam singleton (poll-only, без start())
_DXCAM = {
    "cam": None,       # объект dxcam
    "running": False,  # мы не используем .start(), оставляем False
}








# ------------------------------------------------------------------------
class GameAutomation:
    """High-level lobby/search/party automation + picking/macros. Все публичные методы принимают hwnd."""

    def __init__(self, logger: logging.Logger, images_root: str = "images", confidence: float = 0.87):

        self.cam = dxcam.create(output_idx=0, output_color="RGB")
        self.log = logger
        self.images = images_root
        self.conf = confidence


        self._status_lock = threading.Lock()
        self._status_value = "idle"

        # image assets
        self.PNG = {
            # lobby / search
            "play": os.path.join(self.images, "lobby", "play.png"),
            "continue": os.path.join(self.images, "lobby", "continue.png"),
            "queue_again": os.path.join(self.images, "lobby", "queue.png"),
            # RU/EN "ACCEPT"
            "accept_ru": os.path.join(self.images, "lobby", "accept-ru.png"),
            "accept_eng": os.path.join(self.images, "lobby", "accept-eng.png"),
            # invites
            "accept_invite_ru": os.path.join(self.images, "lobby", "accept-invite-ru.png"),
            "accept_invite_eng": os.path.join(self.images, "lobby", "accept-invite-eng.png"),
            "add_party": os.path.join(self.images, "lobby", "add-party.png"),
            "id_field_ru": os.path.join(self.images, "lobby", "id-field-ru.png"),
            "id_field_eng": os.path.join(self.images, "lobby", "id-field-eng.png"),
            "search_ru": os.path.join(self.images, "lobby", "search-ru.png"),
            "search_eng": os.path.join(self.images, "lobby", "search-eng.png"),
            "add": os.path.join(self.images, "lobby", "add.png"),
            "dota": os.path.join(self.images, "lobby", "dota.png"),
            "rank": os.path.join(self.images, "lobby", "rank.png"),
            "friend_id": os.path.join(self.images, "lobby", "friend-id.png"),
            "ok": os.path.join(self.images, "lobby", "ok.png"),
            "accept_reward": os.path.join(self.images, "lobby", "accept-reward.png"),
            # game loading / sides
            "detect_radiant": os.path.join(self.images, "game", "detect-radiant.png"),
            "detect_dire": os.path.join(self.images, "game", "detect-dire.png"),
            # pick phase (заведи при желании lock_enabled/lock_disabled)
            "lock_in_ru": os.path.join(self.images, "game", "lock-in-ru.png"),
            "lock_disabled_ru": os.path.join(self.images, "game", "lock-disabled-ru.png"),
            "inventory": os.path.join(self.images, "game", "inventory.png"),
            "shop_search": os.path.join(self.images, "game", "shop-search.png"),
            "random_draft": os.path.join(self.images, "game", "random-draft.png"),
            # welcome/popups
            "welcome_not_new_ru": os.path.join(self.images, "welcome", "not_new_ru.png"),
            "welcome_not_new_en": os.path.join(self.images, "welcome", "not_new_en.png"),
            "welcome_continue": os.path.join(self.images, "welcome", "continue.png"),
            "welcome_ok": os.path.join(self.images, "welcome", "ok.png"),
            "welcome_got_it": os.path.join(self.images, "welcome", "got_it.png"),
        }
        self.last_full_rgb = None  # последний полный кадр (RGB, numpy)
        self.last_full_ts = 0.0
        self.full_frame_min_dt = 1.0 / 30  # не чаще ~30 FPS

        # ---- кэши/тайминги для pyautogui locate ----
        self.hwnd_last_grab_ts = {}  # hwnd -> ts последнего обновления кропа
        self.hwnd_haystack_pil = {}  # hwnd -> PIL.Image (кроп окна в RGB)
        self.needle_cache = {}  # img_path -> PIL.Image (RGB)

    def _ensure_dxcam(self):
        """Создаём глобальную dxcam-камеру один раз. Без .start()!"""
        global _DXCAM
        if _DXCAM["cam"] is not None:
            return
        try:

            cam = dxcam.create(output_idx=0, output_color="RGB")  # RGB удобнее дальше
            _DXCAM["cam"] = cam
            _DXCAM["running"] = False  # принципиально не вызываем .start()
            if self.log:
                self.log.info("[DX] created poll-only camera (no background thread)")
        except Exception as e:
            if self.log:
                self.log.error(f"[DX] create failed: {e}", exc_info=True)
            raise

    def _grab_fullscreen_rgb(self, force: bool = False):
        """
        Берём кадр в poll-режиме: сначала пытаемся get_latest_frame() (вдруг кто-то запустил),
        иначе синхронно grab(). Частотный лимит через self.full_frame_min_dt.
        """
        self._ensure_dxcam()
        cam = _DXCAM["cam"]
        now = time.time()
        if (not force) and self.last_full_rgb is not None and (now - self.last_full_ts) < self.full_frame_min_dt:
            return self.last_full_rgb
        frame = cam.grab()

        if frame is None or not hasattr(frame, "shape"):
            if self.log:
                self.log.warning("[DX] grab returned empty frame")
            return None

        self.last_full_rgb = frame
        self.last_full_ts = now
        if self.log:
            h, w = frame.shape[:2]
            self.log.debug(f"[DX] fullscreen RGB grabbed: {w}x{h} ts={now:.3f}")
        return self.last_full_rgb

    @staticmethod
    def _safe_crop(full_rgb: np.ndarray, x: int, y: int, w: int, h: int) -> Optional[np.ndarray]:
        """Обрезает full_rgb до указанного прямоугольника с учётом границ экрана."""
        H, W = full_rgb.shape[:2]
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(x + w, W)
        y1 = min(y + h, H)
        if x1 <= x0 or y1 <= y0:
            return None
        return full_rgb[y0:y1, x0:x1].copy()

    @staticmethod
    def _np_rgb_to_pil(img_rgb: np.ndarray):
        """RGB numpy -> PIL.Image"""
        return Image.fromarray(img_rgb, mode="RGB")

    def _loc_center(self, hwnd, img_path, confidence: float = 0.87, delta: float = 0.01):
        """
        dxcam → один полный кадр (RGB numpy), кроп client-области hwnd,
        конверт в PIL и матчинг через pyautogui/pyscreeze.locate().

        Возвращает экранные координаты центра (cx, cy) или None.
        """
        try:
            # 1) client-область окна (в экранных коорд.)
            L, T, W, H = _window_area(hwnd)
            if W <= 0 or H <= 0:
                if self.log:
                    self.log.debug(f"[LOC] hwnd={hex(hwnd)} invalid client rect: {(L, T, W, H)}")
                return None

            # 2) лимит частоты обновления кропа на окно
            now = time.time()
            last_ts = self.hwnd_last_grab_ts.get(hwnd, 0.0)
            need_new = (now - last_ts) > max(0.0, delta)

            haystack_pil = None
            if (not need_new) and (hwnd in self.hwnd_haystack_pil):
                haystack_pil = self.hwnd_haystack_pil[hwnd]
                if self.log:
                    self.log.debug(f"[LOC] hwnd={hex(hwnd)} haystack CACHED {W}x{H} @ {L},{T}")
            else:
                # 3) берём полный кадр и вырезаем client-область

                full = self._grab_fullscreen_rgb(force=False)

                if full is None:
                    return None
                crop_rgb = self._safe_crop(full, L, T, W, H)

                if crop_rgb is None:
                    if self.log:
                        self.log.debug(f"[LOC] hwnd={hex(hwnd)} crop out-of-bounds win=({L},{T},{W},{H})")
                    return None
                haystack_pil = self._np_rgb_to_pil(crop_rgb)
                self.hwnd_haystack_pil[hwnd] = haystack_pil
                self.hwnd_last_grab_ts[hwnd] = now
                if self.log:
                    cw, ch = haystack_pil.size
                    self.log.debug(f"[LOC] hwnd={hex(hwnd)} haystack NEW {cw}x{ch} @ {L},{T} (delta={delta:.3f}s)")

            # 4) кэшируем needle (PIL.Image RGB)
            needle = self.needle_cache.get(img_path)
            if needle is None:
                try:
                    needle = Image.open(img_path).convert("RGB")
                    self.needle_cache[img_path] = needle
                    if self.log:
                        self.log.debug(f"[LOC] needle loaded: '{img_path}' size={needle.size}")
                except Exception as e:
                    if self.log:
                        self.log.warning(f"[LOC] cannot load needle '{img_path}': {e}")
                    return None

            # 5) locate через pyscreeze (под капотом OpenCV) — работает с PIL-объектами
            box = p.locate(needle, haystack_pil, confidence=float(confidence), grayscale=True)

            if not box:
                if self.log:
                    self.log.debug(f"[LOC] hwnd={hex(hwnd)} NO MATCH conf={confidence}")
                return None

            cx_local, cy_local = p.center(box)  # координаты внутри haystack (кропа окна)
            cx_screen = int(L + cx_local)
            cy_screen = int(T + cy_local)

            if self.log:
                self.log.debug(
                    f"[LOC] hwnd={hex(hwnd)} HIT local=({cx_local},{cy_local}) "
                    f"screen=({cx_screen},{cy_screen}) box={box} conf={confidence}"
                )

            return (cx_screen, cy_screen)

        except Exception as e:
            if self.log:
                self.log.error(f"[LOC] _loc_center failed hwnd={hex(hwnd)}: {e}", exc_info=True)
            return None

    @staticmethod
    def _wait_until(
            cond: Callable[[], bool],
            timeout_s: float,
            poll: float = 0.25,
            stop_flag: Optional[Callable[[], bool]] = None,
    ) -> bool:
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

    def _click(self, hwnd: int, pt_screen, delay: float = 0.02) :
        try:
            _force_foreground(hwnd)
            p.moveTo(x = pt_screen[0], y= pt_screen[1])
            if delay > 0:
                time.sleep(delay)
            p.leftClick()
        except Exception:
            pass

    def _set_status(self, value: str):
        try:
            with self._status_lock:
                self._status_value = value
        except Exception:
            pass

    def get_status(self) -> str:
        try:
            with self._status_lock:
                return self._status_value
        except Exception:
            return "idle"

    def get_status_label(self) -> str:
        s = self.get_status()
        return STATUS_LABELS.get(s, s)

    def get_status_color(self) -> str:
        s = self.get_status()
        return STATUS_COLORS.get(s, STATUS_COLORS["idle"])




    # ---------- readiness / welcome ----------
    def wait_window_ready(self, hwnd: int, timeout: float = 25.0,
                          anchors: Optional[List[str]] = None,
                          stop_flag: Optional[Callable[[], bool]] = None) -> bool:
        region = _window_area(hwnd)
        if anchors is None:
            anchors = ["play", "add_party", "rank"]

        def _cond():
            if not _window_ok(hwnd):
                return False
            for k in anchors:
                path = self.PNG.get(k)
                if path and os.path.exists(path) and self._loc_center(hwnd, path):
                    return True
            return True

        ok = self._wait_until(_cond, timeout, 0.3, stop_flag)

        if not ok:
            self.log.warning(f"[IMG] Window {hex(hwnd)} not ready in {timeout:.0f}s (continuing).")
        return ok

    def dismiss_welcome_if_present(self, hwnd: int, timeout: float = 8.0,
                                   stop_flag: Optional[Callable[[], bool]] = None):
        region = _window_area(hwnd)
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
            acted = False
            for key in buttons:
                path = self.PNG.get(key)
                if not path or not os.path.exists(path):
                    continue
                pt = self._loc_center(hwnd,path)
                if pt:
                    self._click(hwnd, pt, delay=0.05)
                    self.log.info(f"[IMG] Welcome dismissed by '{key}'.")
                    time.sleep(0.25)
                    acted = True
            if acted:
                return
            time.sleep(0.15)

    # ---------- lobby/search primitives ----------
    def start_game(self, hwnd: int):
        if not _window_ok(hwnd):
            return
        region = _window_area(hwnd)
        pt = self._loc_center(hwnd, self.PNG["play"])
        if pt:
            self._click(hwnd, pt); time.sleep(0.20)
            self._click(hwnd, pt)  # double tap
            time.sleep(0.30)
            cont = self._loc_center(hwnd, self.PNG["continue"])
            if cont:
                self._click(hwnd, cont)

    def queue_again(self, hwnd: int):
        if not _window_ok(hwnd):
            return

        pt = self._loc_center(hwnd, self.PNG["queue_again"])
        if pt:
            self._click(hwnd, pt, delay=0.06)

    def accept_rewards_once(self, hwnd: int):
        if not _window_ok(hwnd):
            return

        for key in ("accept_reward", "ok"):
            path = self.PNG.get(key)
            if not path:
                continue
            pt = self._loc_center(hwnd, path)
            if pt:
                self._click(hwnd, pt); time.sleep(0.12)

    def skip_rewards(self, hwnds: List[int]):
        self.log.info("[IMG] Accepting rewards…")
        for _ in range(3):
            for hwnd in hwnds:
                self.accept_rewards_once(hwnd)
            time.sleep(0.4)
        self.log.info("[IMG] Rewards accepted")




    # ---------- party/invites by friend_id ----------
    def invite_to_party(self, leader_hwnd: int,
                        friend_id: Optional[str] = None,
                        steamid64: Optional[Union[int, str]] = None):
        if not _window_ok(leader_hwnd):
            return
        fid = friend_id or steam64_to_friend_id_local(steamid64)
        if not fid:
            self.log.warning("[IMG] invite_to_party: friend_id missing — skip.")
            return

        add_party = self.PNG.get("add_party")
        if add_party:
            pt = self._loc_center(hwnd, add_party)
            if pt:

                self._click(leader_hwnd, pt); time.sleep(0.08)

        id_field = self.PNG.get("id_field_ru") or self.PNG.get("id_field_eng")
        if id_field:
            pt = self._loc_center(hwnd, id_field)
            if pt:
                self._click(leader_hwnd, pt); time.sleep(0.06)
                try:
                    p.hotkey("ctrl", "a"); p.press("backspace")
                except Exception:
                    pass
                for ch in fid: p.press(ch)

        search_btn = self.PNG.get("search_ru") or self.PNG.get("search_eng")
        if search_btn:
            pt = self._loc_center(hwnd, search_btn)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.20)

        add = self.PNG.get("add")
        if add:
            pt = self._loc_center(hwnd, add)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.20)

        dota = self.PNG.get("dota")
        if dota:
            pt = self._loc_center(hwnd, dota)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.35)

    def make_parties(self, hwnds: List[int],
                     friend_ids: Optional[List[Optional[str]]] = None,
                     steamids64: Optional[List[Optional[Union[int, str]]]] = None):
        n = len(hwnds)
        if n < 5:
            self.log.info("[IMG] Not enough accounts for party (<5) — skipping make_parties")
            return

        def _fid(i: int) -> Optional[str]:
            v = None
            if friend_ids and i < len(friend_ids):
                v = friend_ids[i]
            if (not v) and steamids64 and i < len(steamids64):
                v = steam64_to_friend_id_local(steamids64[i])
            return v or None

        if not friend_ids and not steamids64:
            self.log.warning("[IMG] friend_ids/steamids64 not provided — skipping party build.")
            return

        if n >= 10:
            self.log.info("[IMG] Inviting players for stack #1")
            leader1 = hwnds[0]
            for idx in range(1, 5):
                fid = _fid(idx)
                if fid: self.invite_to_party(leader1, friend_id=fid)
            self.log.info("[IMG] Inviting players for stack #2")
            leader2 = hwnds[5]
            for idx in range(6, 10):
                fid = _fid(idx)
                if fid: self.invite_to_party(leader2, friend_id=fid)
        else:
            self.log.info("[IMG] Inviting players for single stack")
            leader = hwnds[0]
            for idx in range(1, min(5, n)):
                fid = _fid(idx)
                if fid: self.invite_to_party(leader, friend_id=fid)

        # accept invites in all windows
        self.log.info("[IMG] Accepting invitations")
        for hwnd in hwnds:
            if not _window_ok(hwnd): continue
            region = _window_area(hwnd)
            paths = [self.PNG.get("accept_invite_ru", ""), self.PNG.get("accept_invite_eng", "")]
            paths = [p for p in paths if p]

            for ph in paths:
                    pt = self._loc_center(hwnd, ph)
                    if pt: self._click(hwnd, pt); time.sleep(0.18); break

    def _count_in_hwnds(
            self,
            hwnds: List[int],
            png_keys: List[str],
            *,
            confidence: Optional[float] = None,
    ) -> int:
        """
        Считает, в скольких окнах из hwnds найден ХОТЯ БЫ ОДИН ключ из png_keys.
        Поиск выполняется строго в границах окна (client region) и через self._loc_center().
        Никаких self._loc_center()_robust и cv2.
        """
        conf = self.conf if confidence is None else confidence
        key_paths: List[str] = []
        for k in png_keys:
            path = self.PNG.get(k)
            if path and os.path.exists(path):
                key_paths.append(path)
        if not key_paths:
            return 0

        total = 0
        for hwnd in hwnds:
            if not _window_ok(hwnd):
                continue
            region = _window_area(hwnd)
            for path in key_paths:
                pt = self._loc_center(hwnd, path)
                if pt:
                    total += 1
                    break
        return total

    # ---------- main search scenario ----------
    def search_games(self, hwnds: List[int],
                     should_make_party: bool = False,
                     stop_flag: Optional[Callable[[], bool]] = None):
        if should_make_party:
            self.log.info("[IMG] make_parties=True passed here — ignored; parties are built in run_with_hwnds.")

        time.sleep(0.8)
        self.log.info("[IMG] Starting search")
        self._set_status("queueing")

        if len(hwnds) >= 10:
            self.start_game(hwnds[0]); self.start_game(hwnds[5])
        else:
            self.start_game(hwnds[0])

        self.log.info("[IMG] Waiting for games…")
        accept_paths = [self.PNG.get("accept_ru", ""), self.PNG.get("accept_eng", "")]
        accept_paths = [ph for ph in accept_paths if ph]

        while True:
            if stop_flag and stop_flag():
                return
            time.sleep(0.6)

            founded_games = self._count_in_hwnds(hwnds, self.PNG['accept'])

            if founded_games == 5:
                self.log.info("[IMG] 5 players found; waiting for another 5…")
                time.sleep(1.0)
                if self._count_in_hwnds(hwnds, self.PNG['accept']) == 5:
                    self.log.info("[IMG] Another 5 didn't find — requeue")
                    if len(hwnds) >= 10:
                        self.queue_again(hwnds[0])
                        self.queue_again(hwnds[5])
                    else:
                        self.queue_again(hwnds[0])
                    self._set_status("queueing")

            if founded_games >= 10 or (len(hwnds) < 10 and founded_games >= 5):
                self._set_status("gc_ready")
                self.log.info("[IMG] Game is found")
                break

        # accept match in all windows
        self.log.info("[IMG] Accepting games")
        time.sleep(0.3)
        for hwnd in hwnds:
            if stop_flag and stop_flag(): return
            if not _window_ok(hwnd): continue
            region = _window_area(hwnd)
            for ph in accept_paths:
                pt = self._loc_center(hwnd, ph)
                if pt: self._click(hwnd, pt, delay=0.04); break

        self.log.info("[IMG] Games accepted")
        self.log.info("[IMG] Waiting for players to load…")

        # wait until both sides 5/5
        while True:
            if stop_flag and stop_flag():
                return
            time.sleep(0.6)

            radiant_count = self._count_in_hwnds(hwnds, ["detect_radiant"])
            dire_count = self._count_in_hwnds(hwnds, ["detect_dire"])

            if radiant_count >= 5 and dire_count >= 5:
                break

        self._set_status("ingame")
        self.log.info("[IMG] All players are loaded")

    # ---------- side detection / hero pick / macros / debug ----------






    def detect_side(self, hwnd: int, *, confidence: float = 0.90,
                    timeout_s: float = 6.0, poll: float = 0.25,
                    stop_flag: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """Ищет индикаторы Radiant/Dire в пределах окна hwnd. Возвращает 'radiant'/'dire' или None."""
        if not _window_ok(hwnd): return None
        reg = _window_area(hwnd)
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            if stop_flag and stop_flag(): return None
            pt_r = self._loc_center(hwnd, self.PNG["detect_radiant"])
            if pt_r: self.log.info(f"[IMG] {hex(hwnd)} side: Radiant"); return "radiant"
            pt_d = self._loc_center(hwnd, self.PNG["detect_dire"])
            if pt_d: self.log.info(f"[IMG] {hex(hwnd)} side: Dire");    return "dire"
            time.sleep(poll)
        self.log.info(f"[IMG] {hex(hwnd)} side: not detected")
        return None



    def wait_lock_enabled(self, hwnd: int, timeout_s: float = 30.0, poll: float = 0.2) -> bool:
        """Ждёт, пока кнопка Lock станет активной. Работает с PNG lock_in / lock_disabled (если есть)."""
        reg = _window_area(hwnd)
        t0 = time.time()
        key_enabled  = self.PNG.get("lock_in_ru")
        key_disabled = self.PNG.get("lock_disabled_ru")
        while time.time() - t0 < timeout_s:
            if key_enabled and os.path.exists(key_enabled):
                pt_en = self._loc_center(hwnd, key_enabled  )
                if pt_en: return True
            if key_disabled and os.path.exists(key_disabled):
                pt_dis = self._loc_center(hwnd, key_disabled)
                if pt_dis:
                    time.sleep(poll); continue
            time.sleep(poll)
        return False

    def pick_hero_grid(self, hwnd: int, heroes: List[str],
                       *, icon_confidence: float = 0.78,
                       lock_confidence: float = 0.80,
                       per_hero_timeout: float = 10.0) -> Optional[str]:
        """
        Ищет иконку героя ТОЛЬКО внутри сетки, кликает, ждёт разблокировки lock и кликает lock.
        Возвращает имя героя при успехе, иначе None.
        """

        for hero in list(heroes):
            path = os.path.join(self.images, "heroes", f"{hero}.png")
            t_end = time.time() + per_hero_timeout
            found_icon = None
            while time.time() < t_end:
                pt = self._loc_center(hwnd, path)
                if pt:
                    found_icon = pt
                    self._click(hwnd, pt)
                    break
                time.sleep(0.15)
            if not found_icon:
                self.log.info(f"[IMG] {hex(hwnd)}: hero \"{hero}\" unavailable (banned/picked). Next…")
                continue

            self.log.info(f"[IMG] {hex(hwnd)}: icon \"{hero}\" @ {found_icon}")
            if not self.wait_lock_enabled(hwnd, timeout_s=12.0):
                self.log.info(f"[IMG] {hex(hwnd)}: failed to lock \"{hero}\" (lock not enabled?). Next…")
                continue
            # Клик по активному lock
            time.sleep(0.12)
            pt_lock = self._loc_center(hwnd, self.PNG["lock_in_ru"])

            if pt_lock:
                self._click(hwnd, pt_lock)
                self.log.info(f"[IMG] {hex(hwnd)}: locked \"{hero}\" @ {pt_lock}")
                return hero
            self.log.info(f"[IMG] {hex(hwnd)}: failed to lock \"{hero}\" (no button?). Next…")
        self.log.warning(f"[IMG] {hex(hwnd)}: no hero could be locked")
        return None

    def wait_game_start(self, hwnd: int, timeout_s: float = 240.0, poll_s: float = 0.5,
                        stop_flag: Optional[Callable[[], bool]] = None) -> bool:
        """Ждём появления PNG инвентаря в окне hwnd."""
        if not _window_ok(hwnd): return False

        inv_path = self.PNG.get("inventory")
        if not inv_path or not os.path.exists(inv_path):
            self.log.warning("[IMG] inventory PNG path is not configured")
            return False
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if stop_flag and stop_flag(): return False
            pt = self._loc_center(hwnd, inv_path)
            if pt:
                self._set_status("ingame")
                self.log.info(f"[IMG] {hex(hwnd)}: inventory detected → game started")
                return True
            time.sleep(poll_s)
        self.log.warning(f"[IMG] {hex(hwnd)}: wait_game_start timeout (no inventory)")
        return False


    # ---------- simple macros ----------
    def start_buy(self, hwnd: int):

        try:
            inventory = self._loc_center(hwnd, self.PNG["inventory"])
            if not inventory: return
            self._click(hwnd, inventory)
            p.press("f4")
            time.sleep(0.25)
            shop_search = self._loc_center(hwnd, self.PNG["shop_search"])
            if not shop_search: return
            self._click(hwnd, shop_search)
            for key in list("maelstrom"): p.press(key)
            p.keyDown("shift"); p.keyDown("ctrl")
            p.move(0, 20); p.leftClick()
            p.keyUp("shift"); p.keyUp("ctrl")
        except Exception:
            pass

    def run_mid(self, hwnd: int, side: str, i: int) -> bool:

        try:
            inventory = self._loc_center(hwnd, self.PNG["inventory"])
            if not inventory: return False
            self._click(hwnd, inventory)
            if side == "radiant":
                p.move(182, -45); p.press("a"); p.leftClick()
                self.log.info(f"Player {i+1} is attacking Dire Throne");   return True
            elif side == "dire":
                p.move(112, 17);  p.press("a"); p.leftClick()
                self.log.info(f"Player {i+1} is attacking Radiant Throne"); return True
            else:
                self.log.info(f"Player {i+1} side unknown; skipping run_mid"); return False
        except Exception:
            self.log.info(f"Player {i+1} attacking failed"); return False

    def type_gg(self):
        time.sleep(0.2); p.press("enter"); time.sleep(0.05)
        p.press("tab");   time.sleep(0.05)
        p.press("g");     time.sleep(0.05)
        p.press("g");     time.sleep(0.05)
        p.press("enter")

    # ---------- high-level orchestration ----------
    def run_with_hwnds(self, hwnds: List[int], make_party: bool = True,
                       stop_flag: Optional[Callable[[], bool]] = None,
                       friend_ids: Optional[List[Optional[str]]] = None,
                       steamids64: Optional[List[Optional[Union[int, str]]]] = None):
        if not hwnds:
            self.log.warning("[IMG] No windows — exit"); return
        # readiness + welcome
        for hwnd in hwnds:
            if stop_flag and stop_flag(): return
            self.wait_window_ready(hwnd, timeout=20.0, stop_flag=stop_flag)
            self.dismiss_welcome_if_present(hwnd, timeout=6.0, stop_flag=stop_flag)
        # rewards (opt)
        # self.skip_rewards(hwnds)
        # party
        if make_party:
            self.make_parties(hwnds, friend_ids=friend_ids, steamids64=steamids64)
        # search / accept
        self.search_games(hwnds, should_make_party=False, stop_flag=stop_flag)



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("img")

    pid = 19480
    hwnd = find_main_hwnd_for_pid(pid)
    print(hwnd)
    _force_foreground(hwnd)

    if hwnd:
        game = GameAutomation(log, confidence=0.8)
        #game._ensure_dxcam()
        #game.invite_to_party(hwnd,friend_id='0123')

        #game.pick_hero_grid(hwnd, heroes=heroes)
        #game.debug_scan_all_assets_opencv(hwnd, confidence=0.0)  # покажет все найденные ассеты
    pass
