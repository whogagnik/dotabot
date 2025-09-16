# game_automation_merged.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import math
import logging
import ctypes
from ctypes import wintypes
from typing import List, Tuple, Optional, Callable, Union, Sequence

import threading

from CONSTANTS import *           # STATUS_LABELS / STATUS_COLORS и прочие константы — из общего файла
from painter import paint_wtih_coords  # ваша отрисовка поверх игры

# === External deps ===
import pyautogui as p
import win32gui
import win32con
import win32api
import win32process

# Optional OpenCV matcher
try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:
    cv2 = None
    np = None

Region = Tuple[int, int, int, int]  # (x, y, w, h)

# --- Win32 / DPI helpers ---
user32 = ctypes.windll.user32
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000
_UI_REF_W, _UI_REF_H = 1920, 1080  # эталон под который сняты PNG
def _enable_dpi_awareness():
    """Make process DPI-aware so coordinates/screenshots match at 125/150/...% scale."""
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

def _dpi_scale_hint() -> float:
    """Approximate system scale (1.0 = 100%). Used as anchor for template scales."""
    try:
        return float(user32.GetDpiForSystem()) / 96.0
    except Exception:
        try:
            hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
            LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            ctypes.windll.gdi32.DeleteDC(hdc)
            return float(dpi) / 96.0
        except Exception:
            return 1.0

_enable_dpi_awareness()
_SCALE_HINT = _dpi_scale_hint()

# --- Steam ID utils ---
STEAMID64_OFFSET = 76561197960265728
def steam64_to_friend_id_local(steamid64: Union[int, str, None]) -> Optional[str]:
    try:
        if steamid64 is None:
            return None
        v = int(str(steamid64).strip())
        acc_id = v - STEAMID64_OFFSET
        return str(acc_id) if acc_id > 0 else None
    except Exception:
        return None

# --- Geometry / window helpers ---
def _client_region(hwnd: int) -> Region:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        w, h = max(1, r - l), max(1, b - t)
        return sx, sy, w, h
    except Exception:
        try:
            L, T, R, B = win32gui.GetWindowRect(hwnd)
            return (L, T, max(1, R - L), max(1, B - T))
        except Exception:
            return (0, 0, 1, 1)

def _is_window_responsive(hwnd: int, timeout_ms: int = 800) -> bool:
    try:
        result = ctypes.c_ulong()
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), WM_NULL, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
        )
        return bool(ok)
    except Exception:
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

# --- Win32 click backend (parallelizable) + fallback ---
WM_MOUSEMOVE   = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP   = 0x0202
MK_LBUTTON     = 0x0001

def _make_lparam(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)

def _screen_to_client(hwnd: int, pt_screen: Tuple[int, int]) -> Tuple[int, int]:
    try:
        return win32gui.ScreenToClient(hwnd, pt_screen)
    except Exception:
        cx, cy, _, _ = _client_region(hwnd)
        return (max(0, pt_screen[0] - cx), max(0, pt_screen[1] - cy))

def _post_click(hwnd: int, x_client: int, y_client: int, delay: float = 0.02) -> bool:
    try:
        lp = _make_lparam(x_client, y_client)
        win32api.PostMessage(hwnd, WM_MOUSEMOVE, 0, lp)
        time.sleep(delay)
        win32api.PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.02)
        win32api.PostMessage(hwnd, WM_LBUTTONUP, 0, lp)
        return True
    except Exception:
        return False

def _click_hwnd_point_win32(hwnd: int, pt_screen: Tuple[int, int], delay: float = 0.02) -> bool:
    try:
        x_client, y_client = _screen_to_client(hwnd, pt_screen)
        return _post_click(hwnd, x_client, y_client, delay=delay)
    except Exception:
        return False

def _click_pyautogui(pt_screen: Tuple[int, int], delay: float = 0.02) -> bool:
    try:
        p.moveTo(pt_screen)
        time.sleep(delay)
        p.leftClick()
        return True
    except Exception:
        return False

# ----------------------------- ROBUST LOCATE -----------------------------
def _default_scales(anchor: float) -> List[float]:
    # теперь включаем мелкие масштабы сразу
    base = [0.30, 0.33, 0.36, 0.40, 0.45, 0.50, 0.60, 0.70,
            0.80, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00]
    extra = [anchor, anchor * 0.9, anchor * 1.1, anchor * 1.25]
    uniq = sorted({round(x, 3) for x in base + extra})
    uniq.sort(key=lambda s: abs(math.log(s / max(anchor, 1e-6))))
    return uniq


def _pyautogui_locate_center(img_path: str, confidence: float, region: Optional[Region]):
    try:
        return p.locateCenterOnScreen(img_path, confidence=confidence, region=region, grayscale=True)
    except Exception:
        return None
# ✅ БАЗОВЫЕ КОНСТАНТЫ ДЛЯ ОЦЕНКИ МАСШТАБА UI (вставь рядом с _SCALE_HINT)


def _scales_for_region(region: Tuple[int,int,int,int],
                       templ_wh: Tuple[int,int],
                       *,
                       dpi_anchor: float,
                       min_px: int = 8) -> List[float]:
    """
    Динамический набор масштабов:
      • якорь ≈ (region_w / UI_REF_W) * dpi_anchor
      • гарантируем «мелкие» шкалы для 640x480 (0.30…0.70)
      • выкидываем масштабы, где шаблон < min_px или > окна
    """
    _, _, rw, rh = region
    tw, th = templ_wh
    # якорь от соотношения окна к эталону * DPI
    anchor = max(0.3, min(2.0, min(rw / _UI_REF_W, rh / _UI_REF_H) * dpi_anchor))

    # база + мелкие масштабы для маленьких окон
    base = [0.30, 0.33, 0.36, 0.40, 0.45, 0.50, 0.60, 0.70,
            0.80, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00]
    # плотнее вокруг якоря
    around = [anchor * f for f in (0.80, 0.90, 1.00, 1.10, 1.25)]
    # итоговый набор
    cand = sorted({round(s, 3) for s in (base + around)})

    # фильтруем по допустимому размеру шаблона
    out = []
    for s in cand:
        w = int(tw * s)
        h = int(th * s)
        if w < min_px or h < min_px:
            continue
        if w >= rw or h >= rh:
            continue
        out.append(s)

    # сортируем по близости к anchor, чтобы быстрее находить
    out.sort(key=lambda s: abs(math.log(max(s, 1e-6) / anchor)))
    return out or [0.50, 0.60, 0.70, 0.80, 0.90]  # safety

# Цветной OpenCV + поддержка альфа-маски, ТОЛЬКО при достижении порога.
def _opencv_locate_center_multiscale(
    img_path: str,
    confidence: float,
    region: Optional[Region],
    scales: Optional[Sequence[float]] = None,
):
    """Цветной multiscale с динамическими масштабами (включая мелкие)."""
    if cv2 is None or np is None:
        return None
    if not os.path.exists(img_path):
        return None

    # 1) скрин ROI (цвет)
    try:
        shot = p.screenshot(region=region)  # PIL
        hay = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
    except Exception:
        return None

    # 2) шаблон (+маска из альфы, если есть)
    templ_rgba = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
    if templ_rgba is None:
        return None
    if templ_rgba.ndim == 3 and templ_rgba.shape[2] == 4:
        templ = templ_rgba[:, :, :3]
        mask  = templ_rgba[:, :, 3]
    else:
        templ = templ_rgba if templ_rgba.ndim == 3 else cv2.cvtColor(templ_rgba, cv2.COLOR_GRAY2BGR)
        mask  = None

    H, W = hay.shape[:2]
    # 3) динамические масштабы: для маленьких окон дадут 0.30…0.70 и т.п.
    if scales is None:
        # region гарантированно есть (его всегда даём из вызывающих)
        scales = _scales_for_region(region or (0,0,W,H),
                                    templ_wh=(templ.shape[1], templ.shape[0]),
                                    dpi_anchor=_SCALE_HINT,
                                    min_px=8)

    best_val, best_center = -1.0, None

    for s in scales:
        h = int(templ.shape[0] * s)
        w = int(templ.shape[1] * s)
        if h < 8 or w < 8 or h >= H or w >= W:
            continue

        templ_s = cv2.resize(templ, (w, h), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR)
        mask_s  = None
        if mask is not None:
            mask_s = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)

        # лёгкий блюр при сильном даунскейле → стабильнее корреляция мелочи
        if s < 0.7:
            hay_use   = cv2.GaussianBlur(hay,   (3,3), 0)
            templ_use = cv2.GaussianBlur(templ_s, (3,3), 0)
        else:
            hay_use, templ_use = hay, templ_s

        try:
            if mask_s is not None:
                res = cv2.matchTemplate(hay_use, templ_use, cv2.TM_CCOEFF_NORMED, mask=mask_s)
            else:
                res = cv2.matchTemplate(hay_use, templ_use, cv2.TM_CCOEFF_NORMED)
        except Exception:
            res = cv2.matchTemplate(hay_use, templ_use, cv2.TM_CCOEFF_NORMED)

        _minV, maxV, _minL, maxL = cv2.minMaxLoc(res)
        if maxV > best_val:
            cx = maxL[0] + w // 2
            cy = maxL[1] + h // 2
            best_val, best_center = maxV, (cx, cy)

        if maxV >= confidence:
            best_center = (maxL[0] + w // 2, maxL[1] + h // 2)
            break

    if best_center is None:
        return None

    if region:
        return (region[0] + best_center[0], region[1] + best_center[1])
    return best_center


def _loc_center_robust(img_path: str, confidence: float = 0.87, region: Optional[Region] = None):
    pt = _pyautogui_locate_center(img_path, confidence, region)
    if pt:
        return pt
    return _opencv_locate_center_multiscale(img_path, confidence, region)

def _loc_count_robust_one(img_path: str, confidence: float = 0.87, region: Optional[Region] = None) -> int:
    return 1 if _loc_center_robust(img_path, confidence, region) else 0

def _loc_count_any(paths: Sequence[str], confidence: float = 0.87, region: Optional[Region] = None) -> int:
    total = 0
    for ph in paths:
        if os.path.exists(ph):
            total += _loc_count_robust_one(ph, confidence, region)
    return total

# ------------------------------------------------------------------------
class GameAutomation:
    """High-level lobby/search/party automation + picking/macros. Все публичные методы принимают hwnd."""

    def __init__(self, logger: logging.Logger, images_root: str = "images", confidence: float = 0.87, click_backend: str = "auto"):
        self.log = logger
        self.images = images_root
        self.conf = confidence
        self.click_backend = (click_backend or "auto").lower()  # 'auto' | 'win32' | 'pyautogui'

        # status (pull-based for Controller polling)
        self._status_lock = threading.Lock()
        self._status_value = "idle"
        # в __init__ класса GameAutomation:
        self._templ_cache: dict[str, tuple[np.ndarray, Optional[np.ndarray], tuple[int, int]]] = {}
        self._scales: List[float] = _default_scales(_SCALE_HINT)

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
    # ---------- status helpers ----------
    def _load_template(self, path: str) -> Optional[tuple[np.ndarray, Optional[np.ndarray], tuple[int, int]]]:
        """
        Кэшируем шаблон.
        Возвращает (templ_bgr, mask_or_None, (w,h)).
        Маска берётся из альфа-канала если он есть.
        """
        if cv2 is None or np is None:
            return None
        if path in self._templ_cache:
            return self._templ_cache[path]
        if not os.path.exists(path):
            return None
        templ_rgba = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if templ_rgba is None:
            return None
        mask = None
        if templ_rgba.ndim == 3 and templ_rgba.shape[2] == 4:
            mask = templ_rgba[:, :, 3]
            templ = templ_rgba[:, :, :3]  # BGR
        else:
            templ = templ_rgba if templ_rgba.ndim == 3 else cv2.cvtColor(templ_rgba, cv2.COLOR_GRAY2BGR)
        wh = (templ.shape[1], templ.shape[0])
        self._templ_cache[path] = (templ, mask, wh)
        return self._templ_cache[path]

    def _loc_center_strict(
            self,
            hwnd: int,
            img_path: str,
            *,
            confidence: float = 0.92,
            region: Optional[Tuple[int, int, int, int]] = None,  # экранные координаты ROI
            scales: Optional[Sequence[float]] = None,
            coarse_step: int = 2,  # шаг coarse-поиска по массиву масштабов
            refine: bool = True,  # включить второй (точный) проход
            stop_flag: Optional[Callable[[], bool]] = None,
            return_score: bool = False,  # если True → вернуть (pt, score); иначе только pt
    ):
        """
        Быстрый и строгий локатор:
          - Только OpenCV (никакого PyAutoGUI locate).
          - Coarse→Refine: сначала быстрый проход по масштабам с шагом,
            потом локальная донастройка вокруг лучшего кандидата (если надо).
          - Возврат только если score >= confidence.
          - Работает строго в пределах окна hwnd/region.
        """
        if cv2 is None or np is None:
            return (None, float("nan")) if return_score else None
        if not os.path.exists(img_path):
            return (None, float("nan")) if return_score else None

        reg = region or _client_region(hwnd)
        if reg[2] <= 1 or reg[3] <= 1:
            return (None, float("nan")) if return_score else None

        # Снимок ROI
        try:
            shot = p.screenshot(region=reg)  # PIL.Image
            hay = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)  # BGR
        except Exception:
            return (None, float("nan")) if return_score else None

        # Шаблон из кэша
        tpl_pack = self._load_template(img_path)
        if tpl_pack is None:
            return (None, float("nan")) if return_score else None
        templ, mask, _ = tpl_pack

        H, W = hay.shape[:2]
        scale_list = list(scales or self._scales)

        best_val = -1.0
        best_loc = None
        best_wh = None
        best_s = 1.0

        # --- 1) COARSE PASS: быстрый проход по масштабам с шагом ---
        for idx in range(0, len(scale_list), max(1, int(coarse_step))):
            if stop_flag and stop_flag():
                return (None, float("nan")) if return_score else None

            s = scale_list[idx]
            h = int(templ.shape[0] * s)
            w = int(templ.shape[1] * s)
            if h < 12 or w < 12 or h >= H or w >= W:
                continue

            templ_s = cv2.resize(templ, (w, h), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR)
            if mask is not None:
                mask_s = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            else:
                mask_s = None

            # Пытаемся с маской; если не поддерживается — без маски
            try:
                if mask_s is not None:
                    res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED, mask=mask_s)
                else:
                    res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)
            except Exception:
                res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)

            _minV, maxV, _minL, maxL = cv2.minMaxLoc(res)
            if maxV > best_val:
                best_val, best_loc, best_wh, best_s = maxV, maxL, (w, h), s

            # Ранний выход: достигли порога — возвращаем
            if maxV >= confidence:
                cx = reg[0] + maxL[0] + w // 2
                cy = reg[1] + maxL[1] + h // 2
                return ((cx, cy), float(maxV)) if return_score else (cx, cy)

        # --- 2) REFINE PASS: уточняем окрестность лучшего кандидата ---
        if refine and best_loc is not None and best_wh is not None:
            # если уже почти порог — пробуем более плотные масштабы вокруг best_s
            if best_val >= (confidence - 0.06):
                dense_scales = sorted({best_s * f for f in (0.9, 0.95, 1.0, 1.05, 1.1)})
            else:
                dense_scales = sorted({best_s * f for f in (0.85, 0.92, 1.0, 1.08, 1.15)})

            for s in dense_scales:
                if stop_flag and stop_flag():
                    return (None, float("nan")) if return_score else None
                h = int(templ.shape[0] * s)
                w = int(templ.shape[1] * s)
                if h < 12 or w < 12 or h >= H or w >= W:
                    continue

                templ_s = cv2.resize(templ, (w, h), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR)
                if mask is not None:
                    mask_s = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
                else:
                    mask_s = None

                try:
                    if mask_s is not None:
                        res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED, mask=mask_s)
                    else:
                        res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)
                except Exception:
                    res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)

                _minV, maxV, _minL, maxL = cv2.minMaxLoc(res)
                if maxV > best_val:
                    best_val, best_loc, best_wh, best_s = maxV, maxL, (w, h), s
                if maxV >= confidence:
                    cx = reg[0] + maxL[0] + w // 2
                    cy = reg[1] + maxL[1] + h // 2
                    return ((cx, cy), float(maxV)) if return_score else (cx, cy)

        # Порог так и не достигнут — считаем, что совпадения нет
        return ((None, float(best_val)) if return_score else None)

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

    # central click router
    def _click(self, hwnd: int, pt_screen: Tuple[int, int], delay: float = 0.02) -> bool:
        if self.click_backend == "win32":
            return _click_hwnd_point_win32(hwnd, pt_screen, delay=delay)
        if self.click_backend == "pyautogui":
            return _click_pyautogui(pt_screen, delay=delay)
        # auto
        if not _click_hwnd_point_win32(hwnd, pt_screen, delay=delay):
            return _click_pyautogui(pt_screen, delay=delay)
        return True

    # ---------- readiness / welcome ----------
    def wait_window_ready(self, hwnd: int, timeout: float = 25.0,
                          anchors: Optional[List[str]] = None,
                          stop_flag: Optional[Callable[[], bool]] = None) -> bool:
        region = _client_region(hwnd)
        if anchors is None:
            anchors = ["play", "add_party", "rank"]

        def _cond():
            if not _window_ok(hwnd):
                return False
            for k in anchors:
                path = self.PNG.get(k)
                if path and os.path.exists(path) and _loc_center_robust(path, self.conf, region):
                    return True
            return True

        ok = self._wait_until(_cond, timeout, 0.3, stop_flag)

        if not ok:
            self.log.warning(f"[IMG] Window {hex(hwnd)} not ready in {timeout:.0f}s (continuing).")
        return ok

    def dismiss_welcome_if_present(self, hwnd: int, timeout: float = 8.0,
                                   stop_flag: Optional[Callable[[], bool]] = None):
        region = _client_region(hwnd)
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
                pt = _loc_center_robust(path, self.conf, region)
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
        region = _client_region(hwnd)
        pt = _loc_center_robust(self.PNG["play"], self.conf, region)
        if pt:
            self._click(hwnd, pt); time.sleep(0.20)
            self._click(hwnd, pt)  # double tap
            time.sleep(0.30)
            cont = _loc_center_robust(self.PNG["continue"], self.conf, region)
            if cont:
                self._click(hwnd, cont)

    def queue_again(self, hwnd: int):
        if not _window_ok(hwnd):
            return
        region = _client_region(hwnd)
        pt = _loc_center_robust(self.PNG["queue_again"], self.conf, region)
        if pt:
            self._click(hwnd, pt, delay=0.06)

    def accept_rewards_once(self, hwnd: int):
        if not _window_ok(hwnd):
            return
        region = _client_region(hwnd)
        for key in ("accept_reward", "ok"):
            path = self.PNG.get(key)
            if not path:
                continue
            pt = _loc_center_robust(path, self.conf, region)
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
        region = _client_region(leader_hwnd)
        fid = friend_id or steam64_to_friend_id_local(steamid64)
        if not fid:
            self.log.warning("[IMG] invite_to_party: friend_id missing — skip.")
            return

        add_party = self.PNG.get("add_party")
        if add_party:
            pt = _loc_center_robust(add_party, self.conf, region)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.08)

        id_field = self.PNG.get("id_field_ru") or self.PNG.get("id_field_eng")
        if id_field:
            pt = _loc_center_robust(id_field, self.conf, region)
            if pt:
                self._click(leader_hwnd, pt); time.sleep(0.06)
                try:
                    p.hotkey("ctrl", "a"); p.press("backspace")
                except Exception:
                    pass
                for ch in fid: p.press(ch)

        search_btn = self.PNG.get("search_ru") or self.PNG.get("search_eng")
        if search_btn:
            pt = _loc_center_robust(search_btn, self.conf, region)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.20)

        add = self.PNG.get("add")
        if add:
            pt = _loc_center_robust(add, self.conf, region)
            if pt: self._click(leader_hwnd, pt); time.sleep(0.20)

        dota = self.PNG.get("dota")
        if dota:
            pt = _loc_center_robust(dota, self.conf, region)
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
            region = _client_region(hwnd)
            paths = [self.PNG.get("accept_invite_ru", ""), self.PNG.get("accept_invite_eng", "")]
            paths = [p for p in paths if p]
            if _loc_count_any(paths, self.conf, region) > 0:
                for ph in paths:
                    pt = _loc_center_robust(ph, self.conf, region)
                    if pt: self._click(hwnd, pt); time.sleep(0.18); break

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
            if stop_flag and stop_flag(): return
            time.sleep(0.6)
            founded_games = _loc_count_any(accept_paths, self.conf)  # global scan

            if founded_games == 5:
                self.log.info("[IMG] 5 players found; waiting for another 5…")
                time.sleep(1.0)
                if _loc_count_any(accept_paths, self.conf) == 5:
                    self.log.info("[IMG] Another 5 didn't find — requeue")
                    if len(hwnds) >= 10:
                        self.queue_again(hwnds[0]); self.queue_again(hwnds[5])
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
            region = _client_region(hwnd)
            for ph in accept_paths:
                pt = _loc_center_robust(ph, self.conf, region)
                if pt: self._click(hwnd, pt, delay=0.04); break

        self.log.info("[IMG] Games accepted")
        self.log.info("[IMG] Waiting for players to load…")

        # wait until both sides 5/5
        while True:
            if stop_flag and stop_flag(): return
            time.sleep(0.6)
            try:
                radiant_count = _loc_count_robust_one(self.PNG["detect_radiant"], self.conf)
                dire_count    = _loc_count_robust_one(self.PNG["detect_dire"], self.conf)
                if radiant_count >= 5 and dire_count >= 5: break
            except Exception:
                pass

        self._set_status("ingame")
        self.log.info("[IMG] All players are loaded")

    # ---------- side detection / hero pick / macros / debug ----------


    def get_minimap_region(self, hwnd: int, corner: str = "left",
                           search_frac_w: float = 0.35, search_frac_h: float = 0.45,
                           fallback_size_h: float = 0.33) -> Optional[Tuple[int,int,int,int]]:
        """Возвращает (x,y,w,h) миникарты в экранных координатах."""
        if not _window_ok(hwnd): return None
        cx, cy, cw, ch = _client_region(hwnd)
        sw = max(40, int(cw * search_frac_w))
        sh = max(40, int(ch * search_frac_h))
        rx0 = 0 if corner == "left" else (cw - sw)
        ry0 = ch - sh
        if cv2 is not None and np is not None:
            try:
                shot = p.screenshot(region=(cx + rx0, cy + ry0, sw, sh))
                roi_bgr = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (3,3), 0)
                edges = cv2.Canny(gray, 40, 120)
                edges = cv2.dilate(edges, np.ones((3,3), np.uint8), iterations=1)
                cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                best = None; roi_area = float(sw*sh)
                for c in cnts:
                    x,y,w,h = cv2.boundingRect(c)
                    area = w*h
                    if area < 0.02*roi_area: continue
                    ar = w/float(max(1,h))
                    squareish   = 0.85 <= ar <= 1.15
                    near_bottom = (y + h) > (sh * 0.60)
                    near_side   = (x < sw * 0.40) if corner=="left" else ((x+w) > sw*0.60)
                    if not (squareish and near_bottom and near_side): continue
                    squareness = 1.0 - abs(1.0 - ar)
                    score = area * (0.6 + 0.4*squareness)
                    if (best is None) or (score > best[0]): best = (score, (x,y,w,h))
                if best is not None:
                    x,y,w,h = best[1]
                    pad = int(min(w,h)*0.03)
                    x = max(0, x-pad); y = max(0, y-pad)
                    w = min(sw-x, w + pad*2); h = min(sh-y, h + pad*2)
                    return (cx + rx0 + x, cy + ry0 + y, w, h)
            except Exception:
                pass
        # fallback
        size = int(ch * fallback_size_h)
        size = max(80, min(size, int(min(cw, ch) * 0.45)))
        margin_x = int(cw * 0.015)
        margin_y = int(ch * 0.05)
        x = cx + margin_x if corner=="left" else cx + cw - margin_x - size
        y = cy + ch - margin_y - size
        return (x, y, size, size)

    def save_minimap_crop(self, hwnd: int, out_path: str, corner: str = "left") -> bool:
        reg = self.get_minimap_region(hwnd, corner=corner)
        if not reg:
            self.log.warning(f"[IMG] {hex(hwnd)}: minimap region not found")
            return False
        try:
            shot = p.screenshot(region=reg)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            shot.save(out_path)
            self.log.info(f"[IMG] saved minimap crop → {out_path} ({reg})")
            return True
        except Exception as e:
            self.log.error(f"[IMG] save_minimap_crop failed: {e}")
            return False

    def detect_side(self, hwnd: int, *, confidence: float = 0.90,
                    timeout_s: float = 6.0, poll: float = 0.25,
                    stop_flag: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """Ищет индикаторы Radiant/Dire в пределах окна hwnd. Возвращает 'radiant'/'dire' или None."""
        if not _window_ok(hwnd): return None
        reg = _client_region(hwnd)
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            if stop_flag and stop_flag(): return None
            pt_r = self._loc_center_strict(hwnd, self.PNG["detect_radiant"], confidence=confidence, region=reg)
            if pt_r: self.log.info(f"[IMG] {hex(hwnd)} side: Radiant"); return "radiant"
            pt_d = self._loc_center_strict(hwnd, self.PNG["detect_dire"], confidence=confidence, region=reg)
            if pt_d: self.log.info(f"[IMG] {hex(hwnd)} side: Dire");    return "dire"
            time.sleep(poll)
        self.log.info(f"[IMG] {hex(hwnd)} side: not detected")
        return None

    def get_hero_grid_region(self, hwnd: int) -> Tuple[int,int,int,int]:
        """Эвристическая область сетки героев (экранные координаты)."""
        x, y, w, h = _client_region(hwnd)
        left   = int(x + w * 0.07)
        top    = int(y + h * 0.18)
        width  = int(w * 0.58)
        height = int(h * 0.55)
        return (left, top, width, height)

    def wait_lock_enabled(self, hwnd: int, timeout_s: float = 30.0, poll: float = 0.2) -> bool:
        """Ждёт, пока кнопка Lock станет активной. Работает с PNG lock_in / lock_disabled (если есть)."""
        reg = _client_region(hwnd)
        t0 = time.time()
        key_enabled  = self.PNG.get("lock_in")
        key_disabled = self.PNG.get("lock_disabled")
        while time.time() - t0 < timeout_s:
            if key_enabled and os.path.exists(key_enabled):
                pt_en = self._loc_center_strict(hwnd, key_enabled, confidence=0.90, region=reg)
                if pt_en: return True
            if key_disabled and os.path.exists(key_disabled):
                pt_dis = self._loc_center_strict(hwnd, key_disabled, confidence=0.92, region=reg)
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
        grid = self.get_hero_grid_region(hwnd)
        for hero in list(heroes):
            path = os.path.join(self.images, "heroes", f"{hero}.png")
            t_end = time.time() + per_hero_timeout
            found_icon = None
            while time.time() < t_end:
                pt = self._loc_center_strict(hwnd, path, confidence=icon_confidence, region=grid)
                if pt:
                    found_icon = pt
                    _click_hwnd_point_win32(hwnd, found_icon, delay=0.05)
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
            reg = _client_region(hwnd)
            pt_lock = _loc_center_robust(self.PNG["lock_in_ru"], confidence=lock_confidence, region=reg)
            print(pt_lock)
            if pt_lock:
                _click_hwnd_point_win32(hwnd, pt_lock, delay=0.05)
                self.log.info(f"[IMG] {hex(hwnd)}: locked \"{hero}\" @ {pt_lock}")
                return hero
            self.log.info(f"[IMG] {hex(hwnd)}: failed to lock \"{hero}\" (no button?). Next…")
        self.log.warning(f"[IMG] {hex(hwnd)}: no hero could be locked")
        return None

    def wait_game_start(self, hwnd: int, timeout_s: float = 240.0, poll_s: float = 0.5,
                        stop_flag: Optional[Callable[[], bool]] = None) -> bool:
        """Ждём появления PNG инвентаря в окне hwnd."""
        if not _window_ok(hwnd): return False
        region = _client_region(hwnd)
        inv_path = self.PNG.get("inventory")
        if not inv_path or not os.path.exists(inv_path):
            self.log.warning("[IMG] inventory PNG path is not configured")
            return False
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if stop_flag and stop_flag(): return False
            pt = _loc_center_robust(inv_path, self.conf, region)
            if pt:
                self._set_status("ingame")
                self.log.info(f"[IMG] {hex(hwnd)}: inventory detected → game started")
                return True
            time.sleep(poll_s)
        self.log.warning(f"[IMG] {hex(hwnd)}: wait_game_start timeout (no inventory)")
        return False

    # ——— отладочный прогон всех PNG через OpenCV с отрисовкой ———
    def _opencv_best_match(self, hwnd: int, img_path: str,
                           region: Optional[Tuple[int,int,int,int]] = None,
                           scales: Optional[Sequence[float]] = None) -> Tuple[float, Optional[Tuple[int,int]], float]:
        """Возвращает (best_score, (x,y)|None, used_scale), без порога — для диагностики."""
        if cv2 is None or np is None or not os.path.exists(img_path):
            return (float("nan"), None, 1.0)
        reg = region or _client_region(hwnd)
        if reg[2] <= 1 or reg[3] <= 1: return (float("nan"), None, 1.0)
        try:
            shot = p.screenshot(region=reg); hay = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        except Exception:
            return (float("nan"), None, 1.0)
        templ_rgba = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
        if templ_rgba is None: return (float("nan"), None, 1.0)
        if templ_rgba.ndim == 3 and templ_rgba.shape[2] == 4:
            templ = templ_rgba[:, :, :3]; mask = templ_rgba[:, :, 3]
        else:
            templ = templ_rgba if templ_rgba.ndim == 3 else cv2.cvtColor(templ_rgba, cv2.COLOR_GRAY2BGR)
            mask = None
        H, W = hay.shape[:2]
        scales = list(scales or _default_scales(_SCALE_HINT))
        best_val = -1.0; best_loc = None; best_wh = None; best_s = 1.0
        for s in scales:
            h = int(templ.shape[0]*s); w = int(templ.shape[1]*s)
            if h < 12 or w < 12 or h >= H or w >= W: continue
            templ_s = cv2.resize(templ, (w,h), interpolation=cv2.INTER_AREA if s<1.0 else cv2.INTER_LINEAR)
            mask_s  = cv2.resize(mask, (w,h), interpolation=cv2.INTER_NEAREST) if mask is not None else None
            try:
                res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED, mask=mask_s) if mask_s is not None \
                      else cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)
            except Exception:
                res = cv2.matchTemplate(hay, templ_s, cv2.TM_CCOEFF_NORMED)
            _, maxVal, _, maxLoc = cv2.minMaxLoc(res)
            if maxVal > best_val:
                best_val, best_loc, best_wh, best_s = maxVal, maxLoc, (w,h), s
        if best_loc is None or best_wh is None:
            return (float("nan"), None, 1.0)
        w,h = best_wh
        cx = reg[0] + best_loc[0] + w//2
        cy = reg[1] + best_loc[1] + h//2
        return (float(best_val), (cx,cy), float(best_s))

    def debug_scan_all_assets_opencv(self, hwnd: int, *,
                                     confidence: float = 0.0,
                                     region: Optional[Tuple[int,int,int,int]] = None,
                                     annotate_fn: Optional[Callable[[int,int,str], None]] = None,
                                     sort_by_score: bool = True
                                     ) -> List[Tuple[str, float, Optional[Tuple[int,int]], float]]:
        """
        Прогоняет ВСЕ self.PNG через OpenCV и вызывает paint_wtih_coords(x, y, text).
        Возвращает список [(key, score, (x,y)|None, scale), ...]
        """
        if annotate_fn is None:
            try:
                annotate_fn = paint_wtih_coords  # type: ignore[name-defined]
            except Exception:
                annotate_fn = None
        reg = region or _client_region(hwnd)
        results: List[Tuple[str, float, Optional[Tuple[int,int]], float]] = []
        for key, path in self.PNG.items():
            if not path or not os.path.exists(path): continue
            score, pt, used_scale = self._opencv_best_match(hwnd, path, region=reg)
            results.append((key, score, pt, used_scale))
        if sort_by_score:
            results.sort(key=lambda x: (-(x[1] if (x[1] == x[1]) else -1.0)))
        for key, score, pt, used_scale in results:
            if (pt is not None) and (score >= confidence):
                if annotate_fn:
                    try: annotate_fn(pt[0], pt[1], f"{key}: {score:.3f} x{used_scale:.2f}")
                    except Exception: pass
                try: self.log.info(f"[DBG] {key}: score={score:.3f} scale={used_scale:.2f} pt={pt}")
                except Exception: pass
            else:
                try: self.log.info(f"[DBG] {key}: score={score:.3f} (below {confidence}) pt={pt}")
                except Exception: pass
        return results

    # ---------- simple macros ----------
    def start_buy(self, hwnd: int):
        region = _client_region(hwnd)
        try:
            inventory = _loc_center_robust(self.PNG["inventory"], self.conf, region)
            if not inventory: return
            self._click(hwnd, inventory)
            p.press("f4")
            time.sleep(0.25)
            shop_search = _loc_center_robust(self.PNG["shop_search"], self.conf, region)
            if not shop_search: return
            self._click(hwnd, shop_search)
            for key in list("maelstrom"): p.press(key)
            p.keyDown("shift"); p.keyDown("ctrl")
            p.move(0, 20); p.leftClick()
            p.keyUp("shift"); p.keyUp("ctrl")
        except Exception:
            pass

    def run_mid(self, hwnd: int, side: str, i: int) -> bool:
        region = _client_region(hwnd)
        try:
            inventory = _loc_center_robust(self.PNG["inventory"], self.conf, region)
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

# --- helper: find main hwnd by PID ---
def _get_window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""

def _is_main_candidate(hwnd: int) -> bool:
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd): return False
        if win32gui.GetParent(hwnd): return False
        title = _get_window_title(hwnd).strip()
        return bool(title)
    except Exception:
        return False

def _window_area(hwnd: int) -> int:
    try:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return max(0, R - L) * max(0, B - T)
    except Exception:
        return 0

def find_main_hwnd_for_pid(pid: int) -> Optional[int]:
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
    if not candidates: return None
    candidates.sort(key=_window_area, reverse=True)
    return candidates[0]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("img")
    log.info(f"DPI scale hint: {_SCALE_HINT:.3f}")

    pid = 19684
    hwnd = find_main_hwnd_for_pid(pid)
    if hwnd:
        game = GameAutomation(log, click_backend="win32", confidence=0.8)
        game.pick_hero_grid(hwnd,heroes=heroes)
        #game.debug_scan_all_assets_opencv(hwnd, confidence=0.0)  # покажет все найденные ассеты
    pass
