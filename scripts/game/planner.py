# planner.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from collections import deque

import time
import json

from scripts.vision.screen_hp_scanner import scan_hp_bars_on_screen,HpBarBox
from scripts.core.config import *
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any
from scripts.vision.hud.hud_scanner import SelfHud
from scripts.core.utils import _force_foreground,debug_log_result,find_main_hwnd_for_pid,find_dota_hwnd
import numpy as np
import cv2
from PIL import Image
import pyautogui as p
# === системные штуки для окна ===
from time import perf_counter

import win32gui
import win32api
import win32con

# === NN и пики ===

from scripts.ml.infer import find_peaks_per_channel,infer_one_minimap,load_minimap_model
from scripts.vision.tower_detector import TowerVisibilityTracker,load_landmarks  # type: ignore

from scripts.game.client_game_brain import Brain

import logging



# ---------------------------------------------------------------------
# Геометрия миникарты
# ---------------------------------------------------------------------



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
                 roles: List[str],
                 side: str = "radiant",
                 *,
                 full_frame_min_dt: float = 0.01,
                 win_crop_min_dt: float  = 0.01,
                 logger=None):
        self.hwnds = hwnds
        self.side  = side.lower().strip()  # 'radiant' | 'dire'
        self.log   = logger
        self.self_hp = SelfHud()
        self.roles = roles
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
        with open(DEFAULT_LANDMARKS_DIR, "r", encoding="utf-8") as f:
            _landmarks_raw = json.load(f)

        # а это — версия без башен, которую будем отдавать наружу
        if isinstance(_landmarks_raw, dict):
            self.landmarks = {k: v for k, v in _landmarks_raw.items() if k != "towers"}
        else:
            # на случай странного формата — просто не трогаем
            self.landmarks = _landmarks_raw

        # башни (используй свою реализацию внутри tower_decetor.py)
        # Ожидается интерфейс update(mm_rgb, now_s, side) -> dict с ally/enemy и полями alive/last_seen/tier/flags
        radiant_pts, dire_pts, ancient_r, ancient_d, lanes = load_landmarks(DEFAULT_LANDMARKS_DIR)

        self.tower_tracker  = TowerVisibilityTracker(
            radiant_pts, dire_pts,
            radiant_ancient=ancient_r,
            dire_ancient=ancient_d,
            lanes=lanes,
            timeout_sec=TIMEOUT_SEC_TOWER_DETECTOR,
            color_radius=COLOR_RADIUS_TOWER_DETECTOR
        )

        # NN
        self.net, self.classes, self.size = load_minimap_model(DEFAULT_ML_MINIMAP_DIR, device='cuda')
        self.cls2idx = {c: i for i, c in enumerate(self.classes)}  # {'self','ally','enemy'}
        self.brains: Dict[int, Brain] = {
            hwnd: Brain(hwnd, planner=self, logger=logger, role=role)
            for hwnd, role in zip(hwnds, self.roles)
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

        # --- input gate / hotkeys ---
        self.block_input: bool = True   # True -> brain.update() разрешён, False -> мозг не исполняется
        self._hk_p_prev_down: bool = False
        self._hk_last_toggle_ts: float = 0.0
        self._hk_toggle_cooldown: float = 0.25  # антидребезг (сек)
        self._last_center_ts_by_hwnd: Dict[int, float] = {}
        self._center_cooldown_sec: float = 0.15


    # --- вспомогательные ---
    # ----------------- НОРМАЛИЗАЦИЯ HP-БАРОВ КРИПОВ -----------------
    @debug_log_result
    def tick_one(self) -> Dict[int, Snapshot]:

        self._poll_hotkeys()
        out: Dict[int, Snapshot] = {}
        for hwnd in list(self.hwnds):
            # перед сбором с этого окна всегда жмём F1 (с cooldown)
            if self.block_input:
                self.center_screen_on_self(hwnd, force_fg=True)

            snap = self.collect_for_hwnd(hwnd)
            if snap is not None:
                self.last_by_hwnd[hwnd] = snap
                out[hwnd] = snap

                brain = self.brains.get(hwnd)
                if brain is None:
                    brain = Brain(hwnd, planner=self, logger=self.log)
                    self.brains[hwnd] = brain

                brain_snap = Snapshot(
                    ts=snap.ts,
                    hwnd=snap.hwnd,
                    combined=snap.combined,
                )

                # <-- вот это ключевое: гейт на мозг
                if self.block_input:
                    brain.tick_one(brain_snap)
                else:
                    # опционально: можно логировать/ничего не делать
                    pass

        return out

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
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            time.sleep(0.002)
            win32api.keybd_event(VK_A, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            # обычный ПКМ
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

        # возвращаем курсор назад
        if ox is not None and oy is not None:
            win32api.SetCursorPos((ox, oy))
    @debug_log_result
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
            time.sleep(0.02)
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
            time.sleep(0.02)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)

        # возвращаем курсор назад
        if ox is not None and oy is not None:
            win32api.SetCursorPos((ox, oy))

    # ----------------- KEY INPUT -----------------
    def _press_vk_global(self, vk: int, *, hold_ms: int = 25) -> None:
        """Глобальный keypress (работает если окно в фокусе)."""
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(max(0.0, hold_ms) / 1000.0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _press_vk_for_hwnd(self, hwnd: int, vk: int, *, hold_ms: int = 25, force_fg: bool = True) -> None:
        if force_fg:
            try:
                _force_foreground(hwnd)
            except Exception:
                pass
        self._press_vk_global(vk, hold_ms=hold_ms)

    def center_screen_on_self(self, hwnd: int, *, force_fg: bool = True, cooldown_sec: Optional[float] = 1) -> None:
        """
        Центруем камеру на себе через горячую клавишу KEY_FOR_CENTER_SCREEN.
        Есть per-hwnd cooldown чтобы не спамить каждый тик.
        """
        now = time.time()
        cd = self._center_cooldown_sec if cooldown_sec is None else float(cooldown_sec)
        last = self._last_center_ts_by_hwnd.get(hwnd, 0.0)
        if cd > 0 and (now - last) < cd:
            return

        self._press_vk_for_hwnd(hwnd, KEY_FOR_CENTER_SCREEN, hold_ms=0, force_fg=force_fg)
        self._press_vk_for_hwnd(hwnd, KEY_FOR_CENTER_SCREEN, hold_ms=0, force_fg=force_fg)
        self._last_center_ts_by_hwnd[hwnd] = now

    def _stabilize_creeps_for_hwnd(
            self,
            hwnd: int,
            new_creeps: Dict[str, List[HpBarBox]],
    ) -> Dict[str, List[HpBarBox]]:
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

        stabilized: Dict[str, List[HpBarBox]] = {
            "ally": [],
            "enemy": [],
        }

        # helper для центра бокса
        def _center(box: HpBarBox):
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
        rx = x + w - MM_W - MM_DX
        ry = y + h - MM_H - MM_DY
        # локальные координаты в hay_pil
        # hay_pil = клиентская область (w,h)
        rx_l = w - MM_W - MM_DX
        ry_l = h - MM_H - MM_DY
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
        # Сбор данных с экрана + тайминги
        t_total0 = perf_counter()

        # --- minimap NN ---
        t0 = perf_counter()
        device = next(self.net.parameters()).device.type
        t_dev = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        prob = infer_one_minimap(self.net, mm_rgb, size=self.size, device=device)  # CxHxW
        t_infer_mm = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        peaks = find_peaks_per_channel(prob, thr=DEFAULT_THR, nms_kernel=DEFAULT_NMS)
        t_peaks = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        units = _filter_units_from_peaks(peaks, self.classes)
        t_units = (perf_counter() - t0) * 1000.0

        # --- HP / Gold ---
        t0 = perf_counter()
        hp_cur, hp_max = self.self_hp.get_hp(np.array(hay))  # hp = (cur, max) или (None, None)
        t_hp = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        #gold = self.self_hp.get_gold(np.array(hay))
        t_gold = (perf_counter() - t0) * 1000.0

        # --- towers tracker ---
        now_s = time.time()
        t_game = now_s - self.game_start_ts

        t0 = perf_counter()
        towers = self.tower_tracker.tick_one(mm_rgb, now=now_s, side=self.side)
        t_towers = (perf_counter() - t0) * 1000.0

        # --- screen scan (heroes/creeps hp bars) ---
        t0 = perf_counter()
        frame = np.array(hay)
        frame_masked = self.mask_rects_white(frame, self.forbidden_ui_rects)
        t_mask = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        screen_info = scan_hp_bars_on_screen(frame_masked)
        t_scan = (perf_counter() - t0) * 1000.0

        t0 = perf_counter()
        raw_heroes = screen_info["heroes"]
        raw_creeps = screen_info["creeps"]
        stable_creeps = self._stabilize_creeps_for_hwnd(hwnd, raw_creeps)
        t_stabilize = (perf_counter() - t0) * 1000.0

        # --- alive/hp_ratio ---
        t0 = perf_counter()
        landmarks_pack = self.landmarks
        if hp_cur is None or hp_max is None:
            alive = False
            hp_ratio = None
        else:
            alive = hp_cur > 0
            hp_ratio = float(hp_cur) / float(hp_max) if hp_max > 0 else None
        t_alive = (perf_counter() - t0) * 1000.0

        t_total = (perf_counter() - t_total0) * 1000.0

        if self.log:
            self.log.debug(
                f"[TIMERS] hwnd={hex(hwnd)} "
                f"dev={t_dev:.2f}ms "
                f"mm_infer={t_infer_mm:.2f}ms "
                f"peaks={t_peaks:.2f}ms "
                f"units={t_units:.2f}ms "
                f"hp={t_hp:.2f}ms "
                f"gold={t_gold:.2f}ms "
                f"towers={t_towers:.2f}ms "
                f"mask={t_mask:.2f}ms "
                f"scan={t_scan:.2f}ms "
                f"stabilize={t_stabilize:.2f}ms "
                f"alive={t_alive:.2f}ms "
                f"TOTAL={t_total:.2f}ms"
            )

        combined = {
            "map": units,
            "towers": towers,
            "landmarks": landmarks_pack,

            "hp_pair": (hp_cur, hp_max),
            "hp_ratio": hp_ratio,
            "gold": 123,
            "alive": alive,
            "t_game": t_game,

            "heroes": raw_heroes,
            "creeps": stable_creeps,
        }
        self._last_roi_by_hwnd[hwnd] = roi
        return Snapshot(ts=now_s, hwnd=hwnd, combined=combined)
    def _poll_hotkeys(self) -> None:
        """
        Хоткей: toggle self.block_input.
        Работает по фронту нажатия (down transition), чтобы не переключалось при удержании.
        """

        now = perf_counter()

        try:
            is_down = (win32api.GetAsyncKeyState(PAUSE_BRAINS) & 0x8000) != 0
        except Exception:
            return

        # фронт нажатия + небольшой cooldown
        if is_down and (not self._hk_p_prev_down):
            if (now - self._hk_last_toggle_ts) >= self._hk_toggle_cooldown:
                self.block_input = not self.block_input
                self._hk_last_toggle_ts = now
                if self.log:
                    self.log.warning(f"[HOTKEY] Pause pressed -> block_input={self.block_input}")

        self._hk_p_prev_down = is_down





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
            #win32api.SetCursorPos((ox, oy))




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
    mm_x0 = win_w - MM_W - MM_DX
    mm_y0 = win_h - MM_H - MM_DY
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
    img = cv2.resize(
        img,
        None,
        fx=2.0,
        fy=2.0,
        interpolation=cv2.INTER_NEAREST,
    )

    return img





if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Planner demo / debugger (one frame overlay)")

    parser.add_argument("--side", type=str, default="radiant", help="radiant|dire")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    log = logging.getLogger("SteamManager")

    # --- hwnd по PID ---


    hwnd = find_dota_hwnd()
    if hwnd is None:
        log.error(f"Не нашёл главное окно ")
        raise SystemExit(1)

    log.info(f"Нашёл hwnd={hex(hwnd)} для , side={args.side}")

    pl = Planner(hwnds=[hwnd], side=args.side, logger=log,roles=['unknown'])

    prev_time = time.time()
    fps_smooth = 0.0

    while True:

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

