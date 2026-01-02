# -*- coding: utf-8 -*-
"""
Трекер видимости башен по миникарте 100x100.
- Координаты башен загружаются ЖЁСТКО из data/minimap_landmarks.json
  формат: { "data": { "tower_radiant": [ {"x":..,"y":..}, ... ],
                       "tower_dire":    [ {"x":..,"y":..}, ... ] } }

- Цветовая проверка СТРОГАЯ (ровно (0,255,0) и (255,0,0)) в радиусе 3 пикселей.
- Если видим внешний тир (T1 → T2 → T3), то внутренний (следующий) считаем цел,
  даже если он закрыт (до таймаута).
- Таймаут: если башню не видели >= 60 c — считаем сломанной.
- Автоматическая расстановка тиров при отсутствии в JSON:
  "чем дальше от центра (50,50), тем ВЫШЕ тир" с раскладкой T1×3 (ближе к центру),
  T2×3, T3×3, T4×2 (дальше от центра).

Захват:
- Ищем основное окно "Dota 2".
- Берём клиентский прямоугольник, вырезаем 100x100 справа-снизу со смещениями dx=4, dy=4.
- Захват только через pyautogui.

Печать:
- Раз в секунду сводка по ALLY/ENEMY (с учётом вашей стороны my_side="radiant"|"dire").
"""

import os
import json
import time
import math
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Union

import numpy as np
import cv2
import pyautogui as p

# ------------------ Константы ------------------

JSON_PATH = r"../../config/minimap_landmarks.json"  # ЖЁСТКИЙ путь
MINIMAP_W = 100
MINIMAP_H = 100
DX = 4  # смещение от правого края внутрь
DY = 4  # смещение от нижнего края вверх

TIMEOUT_SEC = 60.0
COLOR_RADIUS = 3

RGB_GREEN = (0, 255, 0)  # союзники на миникарте
RGB_RED   = (255, 0, 0)  # враги на миникарте

# Ваша сторона: "radiant" или "dire"
MY_SIDE = "dire"

# ------------------ Win32 helpers (минимум) ------------------

import ctypes
import win32gui


user32 = ctypes.windll.user32
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000

def _title(hwnd: int) -> str:
    try: return win32gui.GetWindowText(hwnd) or ""
    except: return ""

def _is_main_visible(hwnd: int) -> bool:
    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.GetParent(hwnd) and bool(_title(hwnd).strip())
    except:
        return False

def _area(hwnd: int) -> int:
    try:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return max(0,R-L)*max(0,B-T)
    except:
        return 0

def find_dota_hwnd() -> Optional[int]:
    cands = []
    def cb(hwnd, _):
        if not _is_main_visible(hwnd):
            return
        t = _title(hwnd)
        if "Dota 2" in t or "Dota" in t:
            cands.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not cands:
        return None
    cands.sort(key=_area, reverse=True)
    return cands[0]

def client_rect_screen(hwnd: int) -> Tuple[int,int,int,int]:
    """Клиентская область в координатах экрана."""
    try:
        l,t,r,b = win32gui.GetClientRect(hwnd)
        x,y = win32gui.ClientToScreen(hwnd, (0,0))
        return x,y,max(1,r-l),max(1,b-t)
    except:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return L,T,max(1,R-L),max(1,B-T)

# ------------------ Захват только pyautogui ------------------

def grab_region_rgb(x: int, y: int, w: int, h: int) -> Optional[np.ndarray]:
    try:
        shot = p.screenshot(region=(x, y, w, h))  # RGB PIL
        arr = np.array(shot)  # RGB uint8
        if arr.ndim == 3 and arr.shape[2] == 3:
            return arr
        if arr.ndim == 2:
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB)
    except Exception:
        return None
    return arr

# ------------------ Трекер башен ------------------

TowerInput = Union[Tuple[int,int], Tuple[int,int,int], Dict[str,int]]

@dataclass
class TowerState:
    x: int
    y: int
    tier: int                  # 1..4
    alive: bool = False
    visible_now: bool = False
    inferred: bool = False
    last_seen: float = -1.0    # -1: ни разу не видели

class TowerVisibilityTracker:
    """
    Трек видимости башен 100x100.
    Если в исходных точках нет tier — проставляет автоматически:
      ближе к центру → меньший тир (T1), дальше → больший тир (T4),
      раскладка T1×3, T2×3, T3×3, T4×2 (если 11 точек). Иначе — квартильное разбиение.
    """

    def __init__(self,
                 radiant_towers: List[TowerInput],
                 dire_towers: List[TowerInput],
                 *,
                 timeout_sec: float = TIMEOUT_SEC,
                 color_radius: int = COLOR_RADIUS):
        self.timeout_sec = float(timeout_sec)
        self.radius = int(color_radius)
        self.radiant = self._normalize_towers(radiant_towers)
        self.dire    = self._normalize_towers(dire_towers)

    # ---------- нормализация входа ----------

    @staticmethod
    def _to_xy_t(item: TowerInput) -> Tuple[int,int,Optional[int]]:
        if isinstance(item, tuple):
            if len(item) == 3:
                x,y,t = item
                return int(x), int(y), int(t)
            elif len(item) == 2:
                x,y = item
                return int(x), int(y), None
            else:
                raise ValueError(f"Bad tower tuple: {item}")
        elif isinstance(item, dict):
            x = int(item["x"]); y = int(item["y"])
            t = int(item["tier"]) if "tier" in item else None
            return x, y, t
        else:
            raise ValueError(f"Unsupported tower item: {item}")

    @staticmethod
    def _dist_from_center(x: int, y: int) -> float:
        return math.hypot(x - 50.0, y - 50.0)

    def _infer_tiers_by_distance(self, pts: List[Tuple[int,int]]) -> List[int]:
        n = len(pts)
        dists = [(self._dist_from_center(x, y), i) for i, (x, y) in enumerate(pts)]
        dists.sort(key=lambda t: t[0])  # ближе к центру — раньше
        tiers = [0]*n
        if n == 11:
            idx = [i for _, i in dists]
            # T1: 3 ближайшие; T2: следующие 3; T3: следующие 3; T4: 2 самые дальние
            for i in idx[0:3]:   tiers[i]=1
            for i in idx[3:6]:   tiers[i]=2
            for i in idx[6:9]:   tiers[i]=3
            for i in idx[9:11]:  tiers[i]=4
        else:
            # квартильное разбиение
            vals = [d for d,_ in dists]
            def q(p):
                k = (n - 1) * p
                f = math.floor(k); c = math.ceil(k)
                if f == c: return vals[f]
                return vals[f] + (vals[c]-vals[f])*(k-f)
            q1, q2, q3 = q(0.25), q(0.50), q(0.75)
            for d,i in dists:
                tiers[i] = 1 if d <= q1 else 2 if d <= q2 else 3 if d <= q3 else 4
        return tiers

    def _normalize_towers(self, items: List[TowerInput]) -> List[TowerState]:
        parsed = [self._to_xy_t(it) for it in items]
        if any(t is None for *_, t in parsed):
            pts = [(x,y) for (x,y,_) in parsed]
            inferred = self._infer_tiers_by_distance(pts)
            out = []
            for idx, (x,y,t) in enumerate(parsed):
                tier = inferred[idx] if t is None else int(t)
                out.append(TowerState(x=int(x), y=int(y), tier=tier))
            return out
        return [TowerState(x=int(x), y=int(y), tier=int(t)) for (x,y,t) in parsed]

    # ---------- апдейт от кадра ----------

    @staticmethod
    def _in_bounds(x: int, y: int) -> bool:
        return 0 <= x < MINIMAP_W and 0 <= y < MINIMAP_H

    def _has_color_radius(self, mm_rgb: np.ndarray, cx: int, cy: int, color: Tuple[int,int,int]) -> bool:
        h,w,_ = mm_rgb.shape
        r = self.radius
        x0, x1 = max(0, cx-r), min(w-1, cx+r)
        y0, y1 = max(0, cy-r), min(h-1, cy+r)
        R,G,B = color
        roi = mm_rgb[y0:y1+1, x0:x1+1, :]
        mask = (roi[:,:,0]==R) & (roi[:,:,1]==G) & (roi[:,:,2]==B)
        return bool(mask.any())

    @staticmethod
    def _reset_flags(lst: List[TowerState]):
        for s in lst:
            s.visible_now = False
            s.inferred = False

    def _apply_vis(self, lst: List[TowerState], mm_rgb: np.ndarray, color: Tuple[int,int,int], now: float):
        for s in lst:
            if self._in_bounds(s.x, s.y) and self._has_color_radius(mm_rgb, s.x, s.y, color):
                s.visible_now = True
                s.alive = True
                s.last_seen = now

    def _apply_timeouts(self, lst: List[TowerState], now: float):
        for s in lst:
            if s.last_seen < 0:
                s.alive = False
            elif (now - s.last_seen) >= self.timeout_sec:
                s.alive = False

    @staticmethod
    def _any_recently_seen_of_tier(lst: List[TowerState], tier: int, now: float, timeout_sec: float) -> bool:
        for s in lst:
            if s.tier != tier: continue
            if s.last_seen >= 0 and (now - s.last_seen) < timeout_sec and s.alive:
                return True
        return False

    def _infer_inner(self, lst: List[TowerState], min_tier: int, now: float):
        # Если внешний тир был виден недавно — внутренний может быть скрыт, но считаем живым
        for s in lst:
            if s.tier >= min_tier and not s.visible_now:
                if s.last_seen < 0 or (now - s.last_seen) >= self.timeout_sec:
                    s.alive = True
                    s.inferred = True

    def tick_one(self, mm_rgb: np.ndarray, side: str, now: Optional[float] = None) -> Dict[str, List[Dict]]:
        """
        mm_rgb — RGB 100x100; my_side: "radiant" | "dire"
        Возвращает словарь с состояниями по сторонам с учётом вашей стороны (ALLY/ENEMY).
        """
        assert isinstance(mm_rgb, np.ndarray) and mm_rgb.shape == (MINIMAP_H, MINIMAP_W, 3), "Ожидаю RGB 100x100"
        tnow = time.time() if now is None else float(now)

        self._reset_flags(self.radiant)
        self._reset_flags(self.dire)

        # Относительные цвета
        if side == "radiant":
            radiant_color = RGB_GREEN  # союзный зелёный
            dire_color    = RGB_RED    # вражеский красный
        else:
            radiant_color = RGB_RED    # вы за Dire: Radiant = враги → красный
            dire_color    = RGB_GREEN  # Dire = союзники → зелёный

        # Обновляем видимость
        self._apply_vis(self.radiant, mm_rgb, radiant_color, tnow)
        self._apply_vis(self.dire,    mm_rgb, dire_color,    tnow)

        # Таймауты
        self._apply_timeouts(self.radiant, tnow)
        self._apply_timeouts(self.dire,    tnow)

        # Инференс: видели T1 → считаем жив T2, видели T2 → T3, видели T3 → T4
        def infer_chain(lst: List[TowerState]):
            if self._any_recently_seen_of_tier(lst, 1, tnow, self.timeout_sec):
                self._infer_inner(lst, 2, tnow)
            if self._any_recently_seen_of_tier(lst, 2, tnow, self.timeout_sec):
                self._infer_inner(lst, 3, tnow)
            if self._any_recently_seen_of_tier(lst, 3, tnow, self.timeout_sec):
                self._infer_inner(lst, 4, tnow)

        infer_chain(self.radiant)
        infer_chain(self.dire)

        # Пакуем с учётом вашей стороны: ally/enemy
        def pack(lst: List[TowerState], side_label: str) -> List[Dict]:
            out = []
            for s in lst:
                out.append({
                    "tier": s.tier, "x": s.x, "y": s.y,
                    "alive": bool(s.alive),
                    "visible_now": bool(s.visible_now),
                    "inferred": bool(s.inferred),
                    "last_seen": float(s.last_seen),
                    "side": side_label,
                })
            return out

        if side == "radiant":
            return {"ally": pack(self.radiant, "radiant"), "enemy": pack(self.dire, "dire")}
        else:
            return {"ally": pack(self.dire, "dire"), "enemy": pack(self.radiant, "radiant")}

    @staticmethod
    def pretty_report(report: Dict[str, List[Dict]], now: Optional[float] = None) -> str:
        tnow = time.time() if now is None else float(now)

        def fmt_age(ts: float) -> str:
            if ts < 0: return "never"
            dt = max(0.0, tnow - ts)
            m, s = divmod(int(dt), 60)
            return f"{m:02d}:{s:02d} ago"

        def fmt_block(label: str, lst: List[Dict]) -> str:
            lines = [f"{label.upper()}: "]
            for s in sorted(lst, key=lambda d: (d["tier"], d["x"], d["y"])):
                flags = []
                if s["inferred"]:    flags.append("inf")
                if s["visible_now"]: flags.append("vis")
                fl = ",".join(flags) if flags else "-"
                state = "ALIVE" if s["alive"] else "DEAD"
                lines.append(f"  T{s['tier']} @({s['x']:02d},{s['y']:02d}) {state:<5}  last_seen={fmt_age(s['last_seen']):>9}  flags={fl}")
            return "\n".join(lines)

        return fmt_block("ally", report["ally"]) + "\n" + fmt_block("enemy", report["enemy"])

# ------------------ Загрузка JSON координат ------------------

def load_landmarks(json_path: str) -> Tuple[List[Dict[str,int]], List[Dict[str,int]]]:
    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    data = raw.get("data", {})
    rad = data.get("tower_radiant", [])[0]
    dire = data.get("tower_dire", [])[0]
    # Ожидаем список словарей {"x":..,"y":..}

    rad_pts  = [{"x": int(d["x"]), "y": int(d["y"])} for d in rad]
    dire_pts = [{"x": int(d["x"]), "y": int(d["y"])} for d in dire]
    return rad_pts, dire_pts

# ------------------ Основной пример цикла (1 Гц) ------------------

def main():
    # 1) Окно Dota
    hwnd = find_dota_hwnd()
    if not hwnd:
        print("[!] Окно Dota не найдено")
        return
    print(f"[i] Dota hwnd: {hex(hwnd)} title='{_title(hwnd)}'")

    # 2) Загрузка координат башен
    if not os.path.exists(JSON_PATH):
        print(f"[!] Не найден JSON с координатами: {JSON_PATH}")
        return
    radiant_pts, dire_pts = load_landmarks(JSON_PATH)

    # 3) Трекер
    tracker = TowerVisibilityTracker(radiant_pts, dire_pts, timeout_sec=TIMEOUT_SEC, color_radius=COLOR_RADIUS)

    # 4) Предпросмотр
    cv2.namedWindow("Minimap", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Minimap", 300, 300)

    # 5) Цикл раз в 1 секунду
    while True:
        # актуальные клиентские координаты
        cx, cy, cw, ch = client_rect_screen(hwnd)

        # правая-нижняя вырезка 100x100 + смещения DX,DY
        x = cx + cw - MINIMAP_W - DX
        y = cy + ch - MINIMAP_H - DY

        mm_rgb = grab_region_rgb(x, y, MINIMAP_W, MINIMAP_H)
        if mm_rgb is None or mm_rgb.shape[:2] != (MINIMAP_H, MINIMAP_W):
            time.sleep(0.2)
            continue

        # апдейт трекера
        report = tracker.tick_one(mm_rgb, my_side=MY_SIDE)
        print(report)
        # печать
        #print(TowerVisibilityTracker.pretty_report(report))

        # визуализация (увеличим)
        vis = cv2.resize(cv2.cvtColor(mm_rgb, cv2.COLOR_RGB2BGR), (300, 300), interpolation=cv2.INTER_NEAREST)
        # отметим точки (ALLY зелёные, ENEMY красные) — только видимые сейчас (для дебага)
        def draw_side(lst: List[Dict], color_bgr: Tuple[int,int,int]):
            for s in lst:
                if s["alive"]:
                    # рисуем кружок в месте башни
                    x0 = int(s["x"] * 3); y0 = int(s["y"] * 3)
                    cv2.circle(vis, (x0, y0), 3, color_bgr, -1, cv2.LINE_AA)
        if MY_SIDE == "radiant":
            draw_side(report["ally"],  (0,255,0))   # Radiant ally → зелёный
            draw_side(report["enemy"], (0,0,255))   # Dire enemy   → красный (BGR)
        else:
            draw_side(report["ally"],  (0,255,0))   # Dire ally → зелёный
            draw_side(report["enemy"], (0,0,255))   # Radiant enemy → красный

        cv2.imshow("Minimap", vis)
        key = cv2.waitKey(1000) & 0xFF  # 1 Гц
        if key in (27, ord('q')):
            break

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
