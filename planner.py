# planner.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from collections import deque
import os

os.environ["TCL_LIBRARY"] = r"C:\Users\bajojo\AppData\Local\Programs\Python\Python313\tcl\tcl8.6"
os.environ["TK_LIBRARY"]  = r"C:\Users\bajojo\AppData\Local\Programs\Python\Python313\tcl\tk8.6"

import win32api
import win32con
import win32gui
import win32process
import time
import json
import ctypes
import hp_scanner
from hp_scanner import scan_hp_bars_on_screen
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any
from hud_ocr import HudOCR
import numpy as np
import cv2
from PIL import Image
import pyautogui as p
import logging
# === системные штуки для окна ===

import win32gui

# === NN и пики ===
from train_minimap_heatmap import load_model, infer_image, find_peaks_per_channel
from tower_detector import TowerVisibilityTracker  # type: ignore

from brain import Brain

import logging
from functools import wraps
# ---------------------------------------------------------------------
# Константы путей (жёсткие)
# ---------------------------------------------------------------------
LANDMARKS_JSON = "data/minimap_landmarks.json"   # ориентиры/линии/кемпы/башни
CKPT_PATH      = "runs/minimap/best.pt"          # чекпоинт модели миникарты

# ---------------------------------------------------------------------
# Геометрия миникарты
# ---------------------------------------------------------------------
MM_W = 100
MM_H = 100
DX = 4  # от правого края внутрь на 4 px
DY = 4  # от нижнего края вверх на 4 px

DEFAULT_THR = 0.9
DEFAULT_NMS = 7

user32 = ctypes.WinDLL('user32', use_last_error=True)
# ---------------------------------------------------------------------
# Глобальный dxcam (poll-only)
# ---------------------------------------------------------------------
_DXCAM = {"cam": None}

# максимум объектов на каналы
MAX_UNITS = {
    "self": 1,
    "ally": 10,
    "enemy": 10,
}

# радиус схлопывания близких пиков (в % миникарты 0..100)
MERGE_RADIUS_PCT = 3.0  # можно подстроить 2..5

def debug_log_result(fn):
    """
    Декоратор для методов Brain:
    логирует имя функции, аргументы и результат через self.log.debug,
    если логгер есть и включён DEBUG.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # пытаемся вытащить self.log, если это метод
        logger = None
        if args and hasattr(args[0], "log"):
            logger = getattr(args[0], "log", None)

        # если логгера нет или DEBUG не включён — просто вызываем функцию
        if not logger or not logger.isEnabledFor(logging.DEBUG):
            return fn(*args, **kwargs)

        # аккуратно формируем строку с аргументами (без гигантских дампов)
        def _short_repr(x, max_len=120):
            r = repr(x)
            if len(r) > max_len:
                return r[:max_len] + "..."
            return r

        arg_strs = [_short_repr(a) for a in args[1:]]  # args[0] == self
        kw_strs  = [f"{k}={_short_repr(v)}" for k, v in kwargs.items()]
        joined   = ", ".join(arg_strs + kw_strs)

        logger.debug(f"[PLANNER] {fn.__name__}(...): args={joined}")
        res = fn(*args, **kwargs)
        logger.debug(f"[PLANNER] {fn.__name__}(...): result -> { _short_repr(res) }")
        return res

    return wrapper
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
def _ensure_dxcam(logger=None):
    """Создаём глобальную dxcam-камеру один раз. Без .start()!"""
    if _DXCAM["cam"] is not None:
        return
    try:
        import dxcam
        cam = dxcam.create(output_idx=0, output_color="RGB")  # RGB удобнее дальше
        _DXCAM["cam"] = cam
        if logger:
            logger.info("[DX] created poll-only camera (no background thread)")
    except Exception as e:
        if logger:
            logger.error(f"[DX] create failed: {e}", exc_info=True)
        raise

def _grab_fullscreen_rgb(cache: Dict[str, Any], min_dt: float = 0.030, log: logging.Logger = None) -> Optional[np.ndarray]:
    """
    Берём фуллскрин RGB кадр.
    - Кэшируем в cache["rgb"], cache["ts"] с антидребезгом по времени (min_dt).
    - Пытаемся через dxcam.grab(); если пусто — падаем на pyautogui.screenshot().
    """
    now = time.time()
    last_ts = cache.get("ts", 0.0)
    if "rgb" in cache and (now - last_ts) < min_dt:
        return cache["rgb"]

    _ensure_dxcam(log)
    frame = None

    # 1) dxcam.grab()
    if _DXCAM["cam"] is not None:
        try:
            frame = _DXCAM["cam"].grab()
            if frame is None or not hasattr(frame, "shape"):
                if log: log.debug("[DX] grab returned empty frame")
                frame = None
        except Exception as e:
            if log: log.error(f"[DX] grab error: {e}")
            frame = None
            # не удаляем девайс; просто дадим фолбэк

    # 2) fallback: pyautogui
    if frame is None:
        try:
            shot = p.screenshot()  # PIL RGB
            frame = np.array(shot)  # RGB uint8
            if log: log.debug("[DX] fallback to pyautogui.screenshot()")
        except Exception as e:
            if log: log.error(f"[DX] fallback failed: {e}")
            frame = None

    if frame is not None:
        cache["rgb"] = frame
        cache["ts"]  = now
    return frame

# ---------------------------------------------------------------------
# Гео / утилиты
# ---------------------------------------------------------------------
def get_client_rect(hwnd: int) -> Tuple[int,int,int,int]:
    """Клиентская область в экранных координатах."""
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)  # (0,0,w,h)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        return sx, sy, max(1, r - l), max(1, b - t)
    except Exception:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return L, T, max(1, R - L), max(1, B - T)

def _safe_crop(full_rgb: np.ndarray, x: int, y: int, w: int, h: int) -> Optional[np.ndarray]:
    """Обрезает full_rgb до указанного прямоугольника с учётом границ экрана."""
    H, W = full_rgb.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(x + w, W); y1 = min(y + h, H)
    if x1 <= x0 or y1 <= y0:
        return None
    return full_rgb[y0:y1, x0:x1].copy()

def _to_rgb_array(img: Any) -> np.ndarray:
    if isinstance(img, np.ndarray):
        arr = img
        if arr.ndim == 2:
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB)
        return arr
    return np.array(img.convert("RGB"))

def _pct_to_px(u: float, size: int) -> int:
    u = max(0.0, min(100.0, u))
    return int((u / 100.0) * (size - 1))
def _merge_close_points_uv(pts: list[tuple[float, float, float]],
                           radius_pct: float) -> list[tuple[float, float, float]]:
    """
    pts: [(u,v,score)], u,v в [0..1]
    Схлопывает близкие точки по евклиду в UV-пространстве.
    Возвращает список центроидов (по максимуму score).
    """
    if not pts:
        return []
    # сортируем по убыванию score, чтобы сначала забирать сильные пики
    pts_sorted = sorted(pts, key=lambda t: t[2], reverse=True)
    rad = max(1e-6, radius_pct / 100.0)

    kept: list[tuple[float, float, float]] = []
    for u, v, s in pts_sorted:
        too_close = False
        for U, V, S in kept:
            du = u - U
            dv = v - V
            if (du*du + dv*dv) ** 0.5 <= rad:
                too_close = True
                break
        if not too_close:
            kept.append((u, v, s))
    return kept

def _euclid2(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    dx = a[0] - b[0]; dy = a[1] - b[1]
    return dx*dx + dy*dy

def _nearest_point(poly: List[Dict[str, float]], pt: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    """Просто ближайшая вершина из полилинии (достаточно для клика)."""
    if not poly:
        return None
    x, y = pt
    best = None
    best_d2 = 1e18
    for pnt in poly:
        px, py = float(pnt["x"]), float(pnt["y"])
        d2 = _euclid2((x, y), (px, py))
        if d2 < best_d2:
            best_d2 = d2
            best = (px, py)
    return best
# --- Геометрия: ближайшая точка на отрезке / полилинии (в координатах 0..100) ---

def _closest_point_on_segment(ax: float, ay: float, bx: float, by: float,
                              px: float, py: float) -> Tuple[float, float, float]:
    """
    Ближайшая точка от P(px,py) к отрезку AB(ax,ay)-(bx,by).
    Возвращает (qx, qy, dist2), где Q — проекция, dist2 — квадрат расстояния.
    """
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    if ab2 <= 1e-9:
        # вырожденный отрезок — возвращаем A
        qx, qy = ax, ay
        dx, dy = px - qx, py - qy
        return qx, qy, dx * dx + dy * dy
    t = (apx * abx + apy * aby) / ab2
    if t < 0.0:   t = 0.0
    if t > 1.0:   t = 1.0
    qx, qy = ax + t * abx, ay + t * aby
    dx, dy = px - qx, py - qy
    return qx, qy, dx * dx + dy * dy


def _closest_point_on_polyline(poly: List[Dict[str, float]],
                               p: Tuple[float, float]) -> Tuple[float, float, float]:
    """
    poly: [{x,y}, ...] — одна ломаная (линия).
    p: (px,py) — текущая позиция.
    Возвращает (qx, qy, dist2) — ближайшая точка на всей ломаной.
    """
    if not poly or len(poly) == 1:
        pt = poly[0] if poly else {"x": 50.0, "y": 50.0}
        dx, dy = p[0] - float(pt["x"]), p[1] - float(pt["y"])
        return float(pt["x"]), float(pt["y"]), dx * dx + dy * dy

    px, py = p
    best_q = (float(poly[0]["x"]), float(poly[0]["y"]))
    best_d2 = 1e18

    for i in range(len(poly) - 1):
        ax, ay = float(poly[i]["x"]), float(poly[i]["y"])
        bx, by = float(poly[i + 1]["x"]), float(poly[i + 1]["y"])
        qx, qy, d2 = _closest_point_on_segment(ax, ay, bx, by, px, py)
        if d2 < best_d2:
            best_d2 = d2
            best_q = (qx, qy)

    return best_q[0], best_q[1], best_d2

def _filter_units_from_peaks(
    peaks: dict[int, list[tuple[float, float, float]]],
    classes: list[str],
    *,
    max_units: dict[str, int] = MAX_UNITS,
    merge_radius_pct: float = MERGE_RADIUS_PCT,
) -> dict[str, list[dict]]:
    """
    peaks: {ci: [(u,v,score), ...]} как из find_peaks_per_channel (u,v в [0..1])
    classes: список имён каналов, порядок соответствует ci
    Возвращает:
      {'self': [{'x':..., 'y':..., 'score':...}], 'ally': [...], 'enemy': [...]}
      где x,y уже в диапазоне 0..100 (проценты миникарты).
    """
    out = {c: [] for c in classes}
    for ci, name in enumerate(classes):
        pts = peaks.get(ci, [])
        # схлопываем «слишком рядом» стоящие пики
        pts = _merge_close_points_uv(pts, merge_radius_pct)
        # берём топ-N по score
        n = max_units.get(name, len(pts))
        pts = sorted(pts, key=lambda t: t[2], reverse=True)[:n]
        # в проценты 0..100
        for (u, v, s) in pts:
            out[name].append({"x": u * 100.0, "y": v * 100.0, "score": float(s)})
    return out


# ---------------------------------------------------------------------
# Снапшот миникарты на тике
# ---------------------------------------------------------------------
@dataclass
class Snapshot:
    ts: float
    hwnd: int
    combined: Dict[str, Any]              # единый словарь (units+towers+landmarks)

# ---------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------
class Planner:
    """
    - В конструкторе: hwnds, side ('radiant'|'dire'), настраиваемые дельты.
    - Внутри поднимает:
        * dxcam (poll-only)
        * модель MiniMapDet (из runs/minimap/best.pt)
        * TowerVisibilityTracker (из tower_decetor.py) и landmarks из data/minimap_landmarks.json
    - tick_one(hwnd):
        * берёт fullscreen RGB через dxcam (осверху дросселится full_frame_min_dt)
        * кропает клиентскую область окна (дросселится win_crop_min_dt)
        * вырезает миникарту 100x100 (правый-нижний угол, DX=4 DY=4)
        * NN + трекинг башен → возвращает MinimapSnapshot
    """

    def __init__(self,
                 hwnds: List[int],
                 side: str = "radiant",
                 *,
                 full_frame_min_dt: float = 0.01,
                 win_crop_min_dt: float  = 0.01,
                 logger=None):
        self.hwnds = hwnds
        self.side  = side.lower().strip()  # 'radiant' | 'dire'
        self.log   = logger
        self.hud_ocr = HudOCR()
        self.game_start_ts: float = time.time()  # пока просто "момент запуска бота"

        # частоты обновления
        self.full_frame_min_dt = max(0.0, float(full_frame_min_dt))
        self.win_crop_min_dt   = max(0.0, float(win_crop_min_dt))

        # кеши
        self._full_cache: Dict[str, Any] = {"rgb": None, "ts": 0.0}
        self._hay_pil_by_hwnd: Dict[int, Image.Image] = {}
        self._hay_ts_by_hwnd: Dict[int, float] = {}

        self.last_by_hwnd: Dict[int, Snapshot] = {}
        self.history_len_creeps = 5  # сколько кадров держим историю крипов
        self.max_keep_creep_frames = 2  # на сколько кадров можно "протянуть" пропавшего крипа

        # {hwnd: deque[Dict[str, List[HpBarBox]]]}  creeps_history[hwnd][0] — самый свежий
        self._creep_history_by_hwnd: Dict[int, deque] = {}

        # landmarks
        # --- стало: фильтруем towers из landmarks ---
        with open(LANDMARKS_JSON, "r", encoding="utf-8") as f:
            _landmarks_raw = json.load(f)

        # а это — версия без башен, которую будем отдавать наружу
        if isinstance(_landmarks_raw, dict):
            self.landmarks = {k: v for k, v in _landmarks_raw.items() if k != "towers"}
        else:
            # на случай странного формата — просто не трогаем
            self.landmarks = _landmarks_raw

        # башни (используй свою реализацию внутри tower_decetor.py)
        # Ожидается интерфейс update(mm_rgb, now_s, side) -> dict с ally/enemy и полями alive/last_seen/tier/flags
        self.tower_tracker = TowerVisibilityTracker(radiant_towers=self.landmarks['data']['tower_radiant'][0], dire_towers=self.landmarks['data']['tower_dire'][0])

        # NN
        self.net, self.classes, self.size = load_model(CKPT_PATH, device='cpu')
        self.cls2idx = {c: i for i, c in enumerate(self.classes)}  # {'self','ally','enemy'}
        self.brains: Dict[int, Brain] = {
            hwnd: Brain(hwnd, planner=self, logger=logger)
            for hwnd in hwnds
        }

        # последняя миникартная ROI (экранные координаты)
        self._last_roi_by_hwnd: Dict[int, Tuple[int,int,int,int]] = {}


        self.forbidden_ui_rects = [
            (240, 0, 610, 20),  # HUD внизу
            (0, 0, 90, 20),
            (0,430,130,480),
            (220,400,620,480),
            (710,370,850,480)
        ]

        # dxcam инициализируем сразу (если упадёт — узнаем сразу)
        _ensure_dxcam(self.log)

    # --- вспомогательные ---
    # ----------------- НОРМАЛИЗАЦИЯ HP-БАРОВ КРИПОВ -----------------

    @staticmethod
    def _box_center(b) -> tuple[float, float]:
        return ( (b.x0 + b.x1) * 0.5, (b.y0 + b.y1) * 0.5 )

    @staticmethod
    def _center_dist2(b1, b2) -> float:
        x1, y1 = Planner._box_center(b1)
        x2, y2 = Planner._box_center(b2)
        dx = x1 - x2
        dy = y1 - y2
        return dx*dx + dy*dy
    @staticmethod
    def mask_rects_white(img: np.ndarray,
                         rects: list[tuple[int, int, int, int]]) -> np.ndarray:
        """
        img      — numpy-изображение (H,W) или (H,W,C) dtype=uint8.
        rects    — список прямоугольников (x0,y0,x1,y1) в координатах картинки.
        Возвращает НОВЫЙ массив с замазанными областями (белым 255,255,255).
        """
        masked = img.copy()
        h, w = masked.shape[:2]

        for (x0, y0, x1, y1) in rects:
            # нормализуем и ограничиваем границы
            x0 = max(0, min(w, int(x0)))
            x1 = max(0, min(w, int(x1)))
            y0 = max(0, min(h, int(y0)))
            y1 = max(0, min(h, int(y1)))

            if x1 <= x0 or y1 <= y0:
                continue

            if masked.ndim == 2:
                # grayscale
                masked[y0:y1, x0:x1] = 255
            else:
                # цветное (BGR или RGB — не важно, белый везде 255)
                masked[y0:y1, x0:x1, :3] = 255

        return masked
    @debug_log_result
    def _point_in_forbidden(self, x: int, y: int) -> bool:
        """
        Проверяем, попадает ли (x,y) в какой-нибудь запретный UI-прямоугольник.
        Координаты x,y — КЛИЕНТСКИЕ.
        """
        for (x0, y0, x1, y1) in self.forbidden_ui_rects:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return False

    @debug_log_result
    def _adjust_click_for_forbidden(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        """
        x,y — желаемая точка в КЛИЕНТСКИХ координатах.

        Если точка попадает в UI-прямоугольник, мы считаем направление луча
        от центра окна к этой точке и двигаемся НАЗАД по этому лучу
        (в сторону центра), пока не выйдем из запретной зоны.
        """
        _, _, w, h = get_client_rect(hwnd)

        x = max(0, min(w - 1, int(x)))
        y = max(0, min(h - 1, int(y)))

        if not self._point_in_forbidden(x, y):
            return x, y

        cx = w / 2.0
        cy = h / 2.0

        dx = x - cx
        dy = y - cy

        if abs(dx) < 1e-3 and abs(dy) < 1e-3:
            dx, dy = 0.0, -1.0  # двигаем вверх

        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-3:
            return x, y

        step_len = 5.0
        step_x = -dx / length * step_len
        step_y = -dy / length * step_len

        cur_x = float(x)
        cur_y = float(y)

        max_iters = int(max(w, h) / step_len) + 5

        for _ in range(max_iters):
            cur_x += step_x
            cur_y += step_y

            cur_x = max(0.0, min(w - 1, cur_x))
            cur_y = max(0.0, min(h - 1, cur_y))

            ix = int(round(cur_x))
            iy = int(round(cur_y))

            if not self._point_in_forbidden(ix, iy):
                return ix, iy

        # если ничего не нашли — fallback: центр окна
        return int(cx), int(cy)

    @debug_log_result
    def click_on_screen_walk(self, hwnd: int, x: int, y: int, *, attack: bool = False):
        win_x, win_y, win_w, win_h = get_client_rect(hwnd)

        x = max(0, min(win_w - 1, int(x)))
        y = max(0, min(win_h - 1, int(y)))

        x_adj, y_adj = self._adjust_click_for_forbidden(hwnd, x, y)
        sx = win_x + x_adj
        sy = win_y + y_adj


        # сохраняем текущую позицию курсора
        try:
            ox, oy = win32api.GetCursorPos()
        except Exception:
            ox = oy = None

        win32api.SetCursorPos((sx, sy))
        time.sleep(0.002)

        if attack:
            VK_A = 0x41  # 'A'
            # A + ЛКМ
            win32api.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.002)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.07)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.002)
            win32api.keybd_event(VK_A, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            # обычный ПКМ
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.07)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

        # возвращаем курсор назад
        if ox is not None and oy is not None:
            win32api.SetCursorPos((ox, oy))

    def click_on_screen(self,
                        hwnd: int,
                        x: int,
                        y: int,
                        *,
                        mouse_button: str = "right",
                        attack: bool = False) -> None:
        """
        Клик в экран по КЛИЕНТСКИМ координатам (x, y) окна hwnd.

        - x, y: клиентские координаты окна (0..w-1, 0..h-1)
        - mouse_button: "right" / "left" / "middle" (по умолчанию ПКМ)
        - attack: если True — выполняется A+ЛКМ (attack-click), mouse_button игнорируется.
        """

        win_x, win_y, win_w, win_h = get_client_rect(hwnd)

        # clamp в границы клиентской области
        x = max(0, min(win_w - 1, int(x)))
        y = max(0, min(win_h - 1, int(y)))

        # учёт запрещённых UI-областей (ожидает клиентские координаты)
        x_adj, y_adj = self._adjust_click_for_forbidden(hwnd, x, y)

        # переводим в экранные координаты
        sx = win_x + x_adj
        sy = win_y + y_adj

        # сохраняем текущую позицию курсора
        try:
            ox, oy = win32api.GetCursorPos()
        except Exception:
            ox = oy = None

        # ставим курсор
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.002)

        if attack:
            # A + ЛКМ (attack-click)
            VK_A = 0x41  # 'A'
            win32api.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.002)

            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.07)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            time.sleep(0.002)
            win32api.keybd_event(VK_A, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            # обычный клик указанной кнопкой
            btn = (mouse_button or "right").lower()

            if btn == "left":
                down_flag = win32con.MOUSEEVENTF_LEFTDOWN
                up_flag = win32con.MOUSEEVENTF_LEFTUP
            elif btn == "middle":
                down_flag = win32con.MOUSEEVENTF_MIDDLEDOWN
                up_flag = win32con.MOUSEEVENTF_MIDDLEUP
            else:
                # по умолчанию ПКМ
                down_flag = win32con.MOUSEEVENTF_RIGHTDOWN
                up_flag = win32con.MOUSEEVENTF_RIGHTUP

            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.07)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)

        # возвращаем курсор назад
        if ox is not None and oy is not None:
            win32api.SetCursorPos((ox, oy))

    def _stabilize_creeps_for_hwnd(
            self,
            hwnd: int,
            new_creeps: Dict[str, List[hp_scanner.HpBarBox]],
    ) -> Dict[str, List[hp_scanner.HpBarBox]]:
        """
        Нормализует детекты крипов по истории:
          - база: текущий кадр,
          - добавляем крипов из прошлых кадров, если:
              * рядом нет нынешнего бокса (с учётом большого радиуса совпадения),
              * бокс лежит в окрестности текущей пачки (по экрану),
              * кадр достаточно свежий.
        """

        # инициализируем историю для этого hwnd
        hist = self._creep_history_by_hwnd.setdefault(
            hwnd,
            deque(maxlen=self.history_len_creeps),
        )

        # добавляем новый кадр в начало истории
        hist.appendleft({
            "ally": list(new_creeps.get("ally", [])),
            "enemy": list(new_creeps.get("enemy", [])),
        })

        # если история только одна — просто возвращаем текущий кадр
        if len(hist) == 1:
            return new_creeps

        # базовые параметры
        base_dist_thr_px = 45.0  # базовый радиус совпадения центров
        age_step_increase = 10.0  # на сколько увеличиваем радиус на каждый "шаг" в прошлое
        max_keep = self.max_keep_creep_frames
        roi_margin_px = 80.0  # расширение области вокруг текущей пачки

        stabilized: Dict[str, List[hp_scanner.HpBarBox]] = {
            "ally": [],
            "enemy": [],
        }

        # helper для центра бокса
        def _center(box: hp_scanner.HpBarBox):
            return ((box.x0 + box.x1) * 0.5,
                    (box.y0 + box.y1) * 0.5)

        current_frame = hist[0]

        for side in ("ally", "enemy"):
            current = list(current_frame[side])
            kept = list(current)  # уже выбранные боксы (начинаем с текущего кадра)

            # если в текущем кадре крипов нет — тогда особо стабилизировать нечего
            if not current:
                stabilized[side] = []
                continue

            # --- считаем bounding box по текущим крипам ---
            cx_list = []
            cy_list = []
            for b in current:
                cx, cy = _center(b)
                cx_list.append(cx)
                cy_list.append(cy)

            min_cx = min(cx_list) - roi_margin_px
            max_cx = max(cx_list) + roi_margin_px
            min_cy = min(cy_list) - roi_margin_px
            max_cy = max(cy_list) + roi_margin_px

            # --- добираем пропавших крипов из прошлых кадров ---
            for age, past in enumerate(list(hist)[1:], start=1):
                if age > max_keep:
                    break

                # радиус совпадения растёт с возрастом кадра,
                # чтобы крип, который немного сдвинулся, всё равно считался тем же
                dist_thr_px = base_dist_thr_px + age_step_increase * age
                dist_thr2 = dist_thr_px * dist_thr_px

                for old_box in past[side]:
                    ocx, ocy = _center(old_box)

                    # если старый бокс далеко от общей области текущей пачки —
                    # считаем, что это уже другая сцена (камера уехала / пачка сильно ушла)
                    if not (min_cx <= ocx <= max_cx and min_cy <= ocy <= max_cy):
                        continue

                    # проверяем, есть ли рядом (по центру) какой-то уже сохранённый бокс
                    has_match = False
                    for b in kept:
                        if self._center_dist2(old_box, b) <= dist_thr2:
                            has_match = True
                            break
                    if has_match:
                        # этот старый крип уже "покрыт" текущим (или более новым) боксом
                        continue

                    # старого бокса рядом ни с кем нет, но он всё ещё
                    # в области текущей пачки → добавляем
                    kept.append(old_box)

            stabilized[side] = kept

        if self.log:
            nc_a = len(new_creeps.get("ally", []))
            nc_e = len(new_creeps.get("enemy", []))
            sc_a = len(stabilized["ally"])
            sc_e = len(stabilized["enemy"])
            self.log.debug(
                f"[CREEPS] hwnd={hex(hwnd)} raw A/E={nc_a}/{nc_e}, "
                f"stabilized A/E={sc_a}/{sc_e}, hist_len={len(hist)}"
            )

        return stabilized


    def _grab_window_pil(self, hwnd: int) -> Optional[Image.Image]:
        """
        Возвращает PIL «haystack» = клиентскую область hwnd, с кешем по окну и частотным лимитом.
        """
        now = time.time()
        last = self._hay_ts_by_hwnd.get(hwnd, 0.0)

        if hwnd in self._hay_pil_by_hwnd and (now - last) < self.win_crop_min_dt:
            return self._hay_pil_by_hwnd[hwnd]

        full = _grab_fullscreen_rgb(self._full_cache, self.full_frame_min_dt, self.log)
        if full is None:
            return None

        L, T, W, H = get_client_rect(hwnd)
        crop = _safe_crop(full, L, T, W, H)
        if crop is None:
            if self.log:
                self.log.debug(f"[CROP] hwnd={hex(hwnd)} OOB win=({L},{T},{W},{H})")
            return None

        pil = Image.fromarray(crop, mode="RGB")
        self._hay_pil_by_hwnd[hwnd] = pil
        self._hay_ts_by_hwnd[hwnd]  = now
        if self.log:
            self.log.debug(f"[CROP] hwnd={hex(hwnd)} haystack NEW {pil.size} @ {L},{T}")
        return pil


    def _crop_minimap_from_window(self, hwnd: int, hay_pil: Image.Image) -> Tuple[np.ndarray, Tuple[int,int,int,int]]:
        """
        Из PIL окна вырезаем миникарту 100x100 (правый-нижний угол) с DX,DY.
        Возвращаем (mm_rgb: ndarray RGB 100x100, roi_xywh: экранные координаты).
        """
        # экранные координаты окна
        x, y, w, h = get_client_rect(hwnd)
        # экранные координаты миникарты
        rx = x + w - MM_W - DX
        ry = y + h - MM_H - DY
        # локальные координаты в hay_pil
        # hay_pil = клиентская область (w,h)
        rx_l = w - MM_W - DX
        ry_l = h - MM_H - DY
        if rx_l < 0 or ry_l < 0 or (rx_l + MM_W) > w or (ry_l + MM_H) > h:
            # fallback на numpy кроп (на всякий)
            arr = np.array(hay_pil)
            sub = arr[max(0, ry_l):max(0, ry_l)+MM_H, max(0, rx_l):max(0, rx_l)+MM_W, :]
            if sub.shape[0] != MM_H or sub.shape[1] != MM_W:
                raise RuntimeError(f"minimap crop OOB: local=({rx_l},{ry_l},{MM_W},{MM_H}) win=({w},{h})")
            return sub.copy(), (rx, ry, MM_W, MM_H)

        sub = hay_pil.crop((rx_l, ry_l, rx_l + MM_W, ry_l + MM_H))  # box=(left, top, right, bottom)
        mm_rgb = np.array(sub.convert("RGB"))
        return mm_rgb, (rx, ry, MM_W, MM_H)


    # --- публичное API ---
    @staticmethod
    def _pack_nn(peaks: Dict[int, List[Tuple[float, float, float]]], classes: List[str]) -> Dict[
        str, List[Dict[str, float]]]:
        """
        CxHxW -> {'self':[{'x','y','score'}], 'ally':[], 'enemy':[]}, координаты в 0..100
        """
        out: Dict[str, List[Dict[str, float]]] = {c: [] for c in classes}
        for ci, pts in peaks.items():
            cname = classes[ci]
            for (u, v, s) in pts:
                out[cname].append({"x": float(u * 100.0), "y": float(v * 100.0), "score": float(s)})
        return out

    @debug_log_result
    def collect_for_hwnd(self, hwnd: int) -> Optional[Snapshot]:
        """
        Полный сбор для одного окна:
          - PIL кадр окна
          - кроп миникарты 100x100 (DX=4, DY=4)
          - NN + фильтрация (cap/merge)
          - трекинг башен
          - сбор combined = {'units','towers','landmarks'}
        """
        #_force_foreground(hwnd)
        hay = self._grab_window_pil(hwnd)
        if hay is None:
            return None

        try:
            mm_rgb, roi = self._crop_minimap_from_window(hwnd, hay)
        except Exception as e:
            if self.log:
                self.log.error(f"[MM] crop failed hwnd={hex(hwnd)}: {e}", exc_info=True)
            return None

        # Сбор данных с экрана
        device = next(self.net.parameters()).device.type
        prob  = infer_image(self.net, mm_rgb, size=self.size, device=device)  # CxHxW
        peaks = find_peaks_per_channel(prob, thr=DEFAULT_THR, nms_kernel=DEFAULT_NMS)
        units = _filter_units_from_peaks(peaks, self.classes)
        hp_cur, hp_max = self.hud_ocr.get_hp(np.array(hay))  # hp = (cur, max) или (None, None)
        gold = self.hud_ocr.get_gold(np.array(hay))
        now_s = time.time()
        t_game = now_s - self.game_start_ts
        towers = self.tower_tracker.tick_one(mm_rgb, now=now_s, side=self.side)

        frame = np.array(hay)
        # вырезаем их белым
        frame_masked = self.mask_rects_white(frame, self.forbidden_ui_rects)
        # отправляем уже очищенный кадр в сканер
        screen_info = hp_scanner.scan_hp_bars_on_screen(frame_masked)



        raw_heroes = screen_info["heroes"]
        raw_creeps = screen_info["creeps"]
        stable_creeps = self._stabilize_creeps_for_hwnd(hwnd, raw_creeps)

        landmarks_pack = self.landmarks
        if hp_cur is None or hp_max is None:
            # трактуем как "мы мертвы / HUD не виден"
            alive = False
            hp_ratio = None
        else:
            alive = hp_cur > 0
            hp_ratio = float(hp_cur) / float(hp_max) if hp_max > 0 else None

        combined = {
            "map": units,
            "towers": towers,
            "landmarks": landmarks_pack,

            "hp_pair": (hp_cur, hp_max),
            "hp_ratio": hp_ratio,
            "gold": gold,
            "alive": alive,
            "t_game": t_game,

            "heroes": raw_heroes,
            "creeps": stable_creeps,
        }
        self._last_roi_by_hwnd[hwnd] = roi
        return Snapshot(ts=now_s, hwnd=hwnd, combined=combined)

    @debug_log_result
    def tick_one(self) -> Dict[int, Snapshot]:

        out: Dict[int, Snapshot] = {}
        for hwnd in list(self.hwnds):
            #_force_foreground(hwnd)
            snap = self.collect_for_hwnd(hwnd)
            if snap is not None:
                self.last_by_hwnd[hwnd] = snap
                out[hwnd] = snap

                # --- дергаем мозг ---
                brain = self.brains.get(hwnd)
                if brain is None:
                    brain = Brain(hwnd, planner=self, logger=self.log)
                    self.brains[hwnd] = brain

                brain_snap = Snapshot(
                    ts=snap.ts,
                    hwnd=snap.hwnd,
                    combined=snap.combined,
                )
                brain.update(brain_snap)

        return out

    @debug_log_result
    def goto_nearest_camp(self,
                          hwnd: int,
                          camp_kind: str = "малый",
                          *,
                          from_pos: Optional[tuple[float, float]] = None,
                          attack: bool = True):
        """
        Идём на ближайший кемп заданного вида:
          camp_kind: 'малый' / 'средний' / 'большой'
                     (поддерживаются и английские small/medium/large).

        Логика:
          1) Берём текущую позицию self из NN, либо from_pos, либо (50,50).
          2) Берём из landmarks список кемпов нужного типа:
             ожидаемые ключи в landmarks['data'] или просто в landmarks:
                'camp_small', 'camp_medium', 'camp_large'
                формата:
                    "camp_small": [
                      [ { "x": 25, "y": 21 }, ... ]
                    ]
          3) Находим ближайший кемп по евклиду.
          4) Кликаем по нему на миникарте (ПКМ или A+ПКМ, если attack=True).
        """


        # 1) текущая позиция героя
        cur: Optional[tuple[float, float]] = None
        snap = self.last_by_hwnd.get(hwnd)
        if snap:
            me = snap.combined.get("units", {}).get("self", [])
            if me:
                cur = (float(me[0]["x"]), float(me[0]["y"]))
        if cur is None:
            cur = from_pos if from_pos is not None else (50.0, 50.0)

        # 2) достаём кемпы из landmarks
        # сначала пытаемся через .get("data"), если его нет — берём сам словарь
        root = self.landmarks.get("data", self.landmarks)
        key = f"camp_{camp_kind}"          # camp_small / camp_medium / camp_large

        arr = root.get(key)
        if not arr:
            if self.log:
                self.log.debug(f"[CAMP] no camps for key={key}")
            return

        # твой формат: "camp_small": [ [ {x,y}, ... ] ]
        points_raw = arr[0] if isinstance(arr, list) and len(arr) > 0 else arr

        candidates: list[tuple[float, float]] = []
        if isinstance(points_raw, list):
            for pt in points_raw:
                try:
                    px = float(pt["x"])
                    py = float(pt["y"])
                    candidates.append((px, py))
                except Exception:
                    continue

        if not candidates:
            if self.log:
                self.log.debug(f"[CAMP] empty camps for key={key}")
            return

        # 3) выбираем ближайший кемп к текущей позиции
        best = None
        best_d2 = 1e18
        for (px, py) in candidates:
            d2 = _euclid2(cur, (px, py))
            if d2 < best_d2:
                best_d2 = d2
                best = (px, py)

        if best is None:
            return

        bx, by = best

        # 4) кликаем по кемпу
        self.click_minimap_pct(hwnd, bx + 1, by + 1, attack=attack)

        if self.log:
            self.log.debug(
                f"[CAMP] hwnd={hex(hwnd)} -> kind={camp_kind} @ ({bx:.1f},{by:.1f}), "
                f"from=({cur[0]:.1f},{cur[1]:.1f}), attack={attack}"
            )

    @debug_log_result
    def _send_mouse_click_client(self, hwnd: int, x: int, y: int, button: str = "right"):
        """
        Отправляет клик мышью в окно hwnd в КЛИЕНТСКИХ координатах (x,y) через Win32 сообщения.
        button: 'left' или 'right'.
        """
        if button == "left":
            down = win32con.WM_LBUTTONDOWN
            up   = win32con.WM_LBUTTONUP
            wparam_down = win32con.MK_LBUTTON
        else:
            down = win32con.WM_RBUTTONDOWN
            up   = win32con.WM_RBUTTONUP
            wparam_down = win32con.MK_RBUTTON

        lparam = win32api.MAKELONG(x, y)

        # кнопка вниз
        win32api.PostMessage(hwnd, down, wparam_down, lparam)
        # кнопка вверх
        win32api.PostMessage(hwnd, up, 0, lparam)

    @debug_log_result
    def _send_key_to_hwnd(self, hwnd: int, vk_code: int, down: bool):
        """
        Отправка WM_KEYDOWN/WM_KEYUP в окно hwnd.
        Важно: это НЕ гарантирует работу в играх, которые слушают RawInput/DirectInput.
        """
        msg = win32con.WM_KEYDOWN if down else win32con.WM_KEYUP
        win32api.PostMessage(hwnd, msg, vk_code, 0)

    # ----------------- ДЕЙСТВИЯ (клики по миникарте) -----------------
    @debug_log_result
    def click_minimap_pct(self, hwnd: int, u: float, v: float, *, attack: bool = False):
        """
        Телепортируем курсор в нужную точку миникарты, жмём клик, возвращаем назад.
        u,v в 0..100 (проценты по миникарте).
        """
        if hwnd not in self._last_roi_by_hwnd:
            raise RuntimeError("ROI unknown; call collect_for_hwnd() first.")

        rx, ry, rw, rh = self._last_roi_by_hwnd[hwnd]
        px = rx + _pct_to_px(u, rw)
        py = ry + _pct_to_px(v, rh)
        _force_foreground(hwnd)
        # сохраняем текущую позицию
        try:
            ox, oy = win32api.GetCursorPos()
        except Exception as e:
            ox = oy = None

        # ставим курсор в нужную точку
        win32api.SetCursorPos((px, py))
        time.sleep(0.015)  # небольшой sleep, чтобы игра точно увидела позицию
        if attack:
            VK_A = 0x41  # 'A'
            # зажимаем A
            win32api.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.005)
            # ЛКМ
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.005)
            # отпускаем A
            win32api.keybd_event(VK_A, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            # 3× ПКМ
            for _ in range(3):
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                time.sleep(0.075)
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
                time.sleep(0.075)

        # возвращаем курсор назад
        if ox is not None and oy is not None:
            time.sleep(0.015)
            win32api.SetCursorPos((ox, oy))

    @debug_log_result
    def _pick_fountain_point(self) -> Tuple[float, float]:
        """
        Ищем точку фонтана из landmarks.
        Приоритет: fountain_<side> → ancient_<side>.
        """
        d = self.landmarks.get("data", {})
        side = self.side
        keys = d.get(f"ancient_{side}",{})[0][0]
        return float(keys["x"]), float(keys["y"])

    @debug_log_result
    def goto_fountain(self, hwnd: int):
        x, y = self._pick_fountain_point()
        self.click_minimap_pct(hwnd, x, y, attack=False)

    @debug_log_result
    def goto_nearest_lane(self,
                          hwnd: int,
                          *,
                          from_pos: Optional[Tuple[float, float]] = None,
                          attack: bool = False):
        """
        Идём на ближайшую линию (top/mid/bot), НЕ указывая имя.
        Берём текущую позицию из NN (units['self'][0]) → проецируем на каждую линию → выбираем минимум.
        Клик по ближайшей точке на выбранной линии.
        """
        # 1) источник позиции: self из последнего снапшота, иначе from_pos, иначе центр
        cur: Optional[Tuple[float, float]] = None
        snap = self.last_by_hwnd.get(hwnd)
        if snap:
            me = snap.combined.get("units", {}).get("self", [])
            if me:
                cur = (float(me[0]["x"]), float(me[0]["y"]))
        if cur is None:
            cur = from_pos if from_pos is not None else (50.0, 50.0)

        # 2) набор линий из landmarks
        d = self.landmarks.get("data", {})
        lane_keys = ["lane_top", "lane_mid", "lane_bot"]
        candidates: List[Tuple[str, List[Dict[str, float]]]] = []
        for k in lane_keys:
            arr = d.get(k, [])
            if arr and arr[0]:
                # формат: lane_*: [[{x,y}, ...]]
                candidates.append((k, arr[0]))

        if not candidates:
            return  # нет лейнов — ничего не делаем

        # 3) ищем ближайшую точку среди всех линий
        best_lane = None
        best_q = None
        best_d2 = 1e18

        for lname, poly in candidates:
            qx, qy, d2 = _closest_point_on_polyline(poly, cur)
            if d2 < best_d2:
                best_d2 = d2
                best_q = (qx, qy)
                best_lane = lname

        if best_q is None:
            return

        # 4) клик по ближайшей точке (с атакой или без)
        self.click_minimap_pct(hwnd, best_q[0], best_q[1], attack=attack)

        # (опционально) можно логировать выбранную линию/цель
        if self.log:
            self.log.debug(f"[LANE] hwnd={hex(hwnd)} -> {best_lane} @ ({best_q[0]:.1f},{best_q[1]:.1f})")

    @debug_log_result
    def goto_nearest_tower(self, hwnd: int, ally_or_enemy: str = "enemy", *, only_alive: bool = True, attack: bool = False):
        """
        Находим ближайшую (живую) башню запрошенной стороны к текущей позиции self и кликаем по ней.
        """
        snap = self.last_by_hwnd.get(hwnd)
        if not snap:
            return
        tws = snap.combined.get("towers", {}).get(ally_or_enemy, [])
        if not tws:
            return

        # текущая позиция self
        me = snap.combined.get("units", {}).get("self", [])
        cur = (float(me[0]["x"]), float(me[0]["y"])) if me else (50.0, 50.0)

        best = None
        best_d2 = 1e18
        for t in tws:
            if only_alive and not t.get("alive", False):
                continue
            px, py = float(t["x"]), float(t["y"])
            d2 = _euclid2(cur, (px, py))
            if d2 < best_d2:
                best_d2 = d2
                best = (px, py)

        if best is None:
            return
        self.click_minimap_pct(hwnd, best[0] + 1, best[1] + 1, attack=attack)


def visualize_full_frame(pl: "Planner",
                         hwnd: int,
                         snap: Snapshot,
                         fps: float = 0.0) -> Optional[np.ndarray]:
    """
    Одна общая визуализация:
      - берём скрин клиентской области окна (hay),
      - рисуем:
          * HP-бары героев/крипов,
          * юнитов миникарты,
          * башни,
          * HUD HP,
          * FPS.
    Возвращает BGR-изображение или None.
    """
    # 1) берём текущий кадр окна
    hay = pl._grab_window_pil(hwnd)
    if hay is None:
        if pl.log:
            pl.log.debug(f"[VIS] _grab_window_pil() вернул None для hwnd={hex(hwnd)}")
        return None

    frame_rgb = np.array(hay)            # RGB
    img = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

    H, W = img.shape[:2]

    combined = snap.combined
    heroes = combined.get("heroes", {})
    creeps = combined.get("creeps", {})
    units  = combined.get("map", {})        # {'self','ally','enemy'} в 0..100 по миникарте
    towers = combined.get("towers", {})     # {'ally':[...], 'enemy':[...]} в 0..100 по миникарте
    hp_pair = combined.get("hp")

    # ------------------------------------------------------------------
    # 2) HP-бары на всём экране (герои + крипы)
    # ------------------------------------------------------------------
    col_heroes = {
        "enemy": (0,   0, 255),   # красный
        "ally":  (0, 255,   0),   # зелёный
        "self":  (255, 0,   0),   # синий
    }
    col_creeps = {
        "enemy": (0, 255, 255),   # бирюзовый
        "ally":  (255,255,  0),   # жёлтый
    }

    # --- герои ---
    for kind, color in col_heroes.items():
        for b in heroes.get(kind, []):
            cv2.rectangle(img, (b.x0, b.y0), (b.x1, b.y1),
                          color, 1, lineType=cv2.LINE_AA)

    # --- крипы ---
    for kind, color in col_creeps.items():
        for b in creeps.get(kind, []):
            cv2.rectangle(img, (b.x0, b.y0), (b.x1, b.y1),
                          color, 1, lineType=cv2.LINE_AA)

    # ------------------------------------------------------------------
    # 3) Миникарта: считаем её положение в клиентских координатах
    #    (ровно как при кропе: правый-нижний угол, DX,DY, MM_W,MM_H)
    # ------------------------------------------------------------------
    # get_client_rect -> клиентские координаты окна (в пикселях)
    _, _, win_w, win_h = get_client_rect(hwnd)
    mm_x0 = win_w - MM_W - DX
    mm_y0 = win_h - MM_H - DY
    mm_x1 = mm_x0 + MM_W - 1
    mm_y1 = mm_y0 + MM_H - 1

    # на всякий: проверяем, что миникарта влезла в img
    if mm_x0 < 0 or mm_y0 < 0 or mm_x1 >= W or mm_y1 >= H:
        if pl.log:
            pl.log.debug(f"[VIS] minimap ROI OOB: ({mm_x0},{mm_y0})-({mm_x1},{mm_y1}) in frame {W}x{H}")
    else:
        # ------------------------------------------------------------------
        # 3.1) Рисуем юнитов миникарты (из combined['map'])
        # ------------------------------------------------------------------
        unit_colors = {
            "self":  (40, 215, 255),   # как CLR
            "ally":  (80, 220,  80),
            "enemy": (40,  40, 240),
        }

        def _mm_to_px(x_pct: float, y_pct: float) -> tuple[int, int]:
            """0..100 по миникарте -> пиксели в img."""
            px = int(round(mm_x0 + (x_pct / 100.0) * (MM_W - 1)))
            py = int(round(mm_y0 + (y_pct / 100.0) * (MM_H - 1)))
            return px, py

        # юниты
        for kind, color in unit_colors.items():
            for det in units.get(kind, []):
                ux = float(det["x"])  # 0..100
                uy = float(det["y"])
                px, py = _mm_to_px(ux, uy)
                cv2.rectangle(img,
                              (px - 3, py - 3), (px + 3, py + 3),
                              color, 1, lineType=cv2.LINE_AA)
                cv2.circle(img, (px, py), 1, color, -1, lineType=cv2.LINE_AA)

        # ------------------------------------------------------------------
        # 3.2) Башни
        # towers: {'ally':[{'x','y','alive',...}], 'enemy':[...]}
        # ------------------------------------------------------------------
        tower_colors = {
            "ally":  (0, 180, 0),
            "enemy": (0, 0, 180),
        }
        for side in ("ally", "enemy"):
            for t in towers.get(side, []):
                try:
                    tx = float(t["x"])
                    ty = float(t["y"])
                except Exception:
                    continue
                alive = bool(t.get("alive", True))
                px, py = _mm_to_px(tx, ty)
                col = tower_colors.get(side, (200, 200, 200))
                thickness = 2 if alive else 1
                # кружок башни
                cv2.circle(img, (px, py), 5, col, thickness, lineType=cv2.LINE_AA)
                if not alive:

                    # зачёркиваем мёртвую
                    cv2.line(img, (px-4, py-4), (px+4, py+4), col, 1, cv2.LINE_AA)
                    cv2.line(img, (px-4, py+4), (px+4, py-4), col, 1, cv2.LINE_AA)

        # (опционально) лендмарки — если захочешь, можно тут же рисовать линии / кемпы
        # root = combined["landmarks"].get("data", combined["landmarks"])
        # ...

    # ------------------------------------------------------------------
    # 4) Текст: счётчики, HUD HP, FPS
    # ------------------------------------------------------------------
    heroes_counts = {k: len(heroes.get(k, [])) for k in ("enemy", "ally", "self")}
    creeps_counts = {k: len(creeps.get(k, [])) for k in ("enemy", "ally")}

    y = 20
    cv2.putText(img, f"Heroes: E={heroes_counts['enemy']} A={heroes_counts['ally']} S={heroes_counts['self']}",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1, cv2.LINE_AA)
    y += 20
    cv2.putText(img, f"Creeps: E={creeps_counts['enemy']} A={creeps_counts['ally']}",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1, cv2.LINE_AA)
    y += 20
    if hp_pair is not None:
        cv2.putText(img, f"HUD HP: {hp_pair}",
                    (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200,200,200), 1, cv2.LINE_AA)

    # FPS в правом верхнем углу
    fps_text = f"{fps:5.1f} FPS"
    cv2.putText(img, fps_text,
                (W - 150, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2, cv2.LINE_AA)

    if pl.log:
        pl.log.debug(
            f"[VIS] frame {W}x{H}, heroes E/A/S={heroes_counts['enemy']}/{heroes_counts['ally']}/{heroes_counts['self']}, "
            f"creeps E/A={creeps_counts['enemy']}/{creeps_counts['ally']}, fps={fps:.1f}"
        )

    return img

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
    import argparse

    parser = argparse.ArgumentParser(description="Planner demo / debugger (one frame overlay)")
    parser.add_argument("--pid", type=int, default=0, help="PID игры (0 = использовать жёстко прописанный)")
    parser.add_argument("--side", type=str, default="radiant", help="radiant|dire")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    log = logging.getLogger("planner-demo")

    # --- hwnd по PID ---
    pid = args.pid if args.pid != 0 else 12360  # сюда свой дефолтный PID по умолчанию
    hwnd = find_main_hwnd_for_pid(pid)
    if hwnd is None:
        log.error(f"Не нашёл главное окно для PID={pid}")
        raise SystemExit(1)

    log.info(f"Нашёл hwnd={hex(hwnd)} для PID={pid}, side={args.side}")

    pl = Planner(hwnds=[hwnd], side=args.side, logger=log)

    prev_time = time.time()
    fps_smooth = 0.0

    while True:
        if win32gui.GetForegroundWindow() != hwnd:
            continue
        t_tick_start = time.time()
        res_by_hwnd = pl.tick_one()


        snap = res_by_hwnd.get(hwnd)
        if snap is None:
            # ничего не пришло — чуть подождать
            time.sleep(0.005)
            continue

        # --- FPS ---
        now = time.time()
        dt = now - prev_time
        prev_time = now
        inst_fps = 1.0 / dt if dt > 1e-6 else 0.0
        fps_smooth = inst_fps if fps_smooth == 0.0 else fps_smooth * 0.9 + inst_fps * 0.1

        # --- Визуализация поверх ОДНОГО кадра ---
        img = visualize_full_frame(pl, hwnd, snap, fps=fps_smooth)
        if img is not None:
            cv2.imshow("Planner Debug", img)

        # клавиатура
        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            log.info("ESC pressed, exiting demo")
            break

        if log:
            log.debug(f"[MAIN] tick dt={time.time() - t_tick_start:.4f}s, fps={fps_smooth:.1f}")

    cv2.destroyAllWindows()

