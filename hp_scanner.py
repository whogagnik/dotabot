# hp_scanner.py
# -*- coding: utf-8 -*-

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict

import numpy as np
import cv2
import pyautogui as p
import win32gui, win32process

# === Цветовые пороги ===
ENEMY_RED_HEROES_LOWER = np.array([150,  30,   0], np.uint8)
ENEMY_RED_HEROES_UPPER = np.array([215, 55,  10], np.uint8)

ALLY_GREEN_HEROES_LOWER = np.array([ 60, 140,  20], np.uint8)
ALLY_GREEN_HEROES_UPPER = np.array([ 110, 215,  55], np.uint8)

SELF_GREEN_HEROES_LOWER = np.array([ 165, 220,  90], np.uint8)
SELF_GREEN_HEROES_UPPER = np.array([ 180, 255,  110], np.uint8)

ALLY_GREEN_CREEPS_LOWER = np.array([ 50, 95,  35], np.uint8)
ALLY_GREEN_CREEPS_UPPER = np.array([ 90, 160,  65], np.uint8)

ENEMY_RED_CREEPS_LOWER = np.array([ 90, 45,  35], np.uint8)
ENEMY_RED_CREEPS_UPPER = np.array([ 140, 80,  60], np.uint8)

HP_BROWN_HEROES_LOWER = np.array([1, 1, 1],   np.uint8)
HP_BROWN_HEROES_UPPER = np.array([15, 5, 5], np.uint8)

HP_BROWN_HEROES_SELF_LOWER = np.array([0, 0, 0],   np.uint8)
HP_BROWN_HEROES_SELF_UPPER = np.array([1, 1, 1], np.uint8)

HP_BROWN_ENEMY_CREEPS_LOWER = np.array([20, 4, 2],   np.uint8)
HP_BROWN_ENEMY_CREEPS_UPPER = np.array([30, 15, 10], np.uint8)

HP_BROWN_ALLY_CREEPS_LOWER = np.array([5, 20, 4],   np.uint8)
HP_BROWN_ALLY_CREEPS_UPPER = np.array([15, 35, 10], np.uint8)

# FIXED MANA COLOR (RGB)
MANA_COLOR = np.array([79, 120, 249], dtype=np.uint8)
MANA_TOL = 3  # допуск +-3 вокруг каждого канала (чтобы не страдать от сглаживания)


# ======================================================================
#                                 CLASS
# ======================================================================

@dataclass
class HpBarBox:
    x0: int
    y0: int
    x1: int
    y1: int
    hp_ratio: float

    # добавляем ману
    mana_ratio: float = 0.0
    has_mana: bool = False

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1


# ======================================================================
#                         HP ESTIMATION
# ======================================================================
def _check_bar_right_side(
    roi: np.ndarray,
    mode: Optional[str],  # "hero", "creep" или None
    brown_range: Tuple[np.ndarray, np.ndarray],
    black_thresh: int = 20,
    right_part: float = 0.4,
    min_ratio: float = 0.01,
) -> bool:
    """
    Проверяет состав ПРАВОЙ части полоски HP.

    mode:
      - "hero"  -> справа должно быть чёрное (пустая часть), коричневого мало
      - "creep" -> справа должно быть коричневое (пустая часть), чёрного мало
      - None    -> ничего не проверяем (всегда True)
    """
    if mode is None:
        return True

    if roi is None or roi.size == 0:
        return False

    h, w = roi.shape[:2]
    if w < 4:
        return False

    # берём правую часть полоски (по умолчанию 40% ширины)
    x_start = int(w * (1.0 - right_part))
    x_start = max(0, min(w - 1, x_start))
    roi_r = roi[:, x_start:]

    if roi_r.size == 0:
        return False

    # маска коричневого
    lower_brown, upper_brown = brown_range
    mask_brown = cv2.inRange(roi_r, lower_brown, upper_brown) > 0
    brown_ratio = float(mask_brown.mean())

    # маска чёрного
    gray_r = cv2.cvtColor(roi_r, cv2.COLOR_RGB2GRAY)
    mask_black = gray_r < black_thresh
    black_ratio = float(mask_black.mean())



    # если вдруг непонятный mode
    return True


def _estimate_hp_ratio_from_roi(
    roi: np.ndarray,
    hp_color_ranges: List[Tuple[np.ndarray, np.ndarray]],
    max_gap: int = 2,
    min_hp_len_pixels: int = 1,   # <-- ВАЖНО: допускаем совсем маленький HP (1 колонка)
    bg_mode: str = "black",       # "black" для героев, "brown" для крипов
    bg_color_ranges: Optional[List[Tuple[np.ndarray, np.ndarray]]] = None,
    return_masks: bool = False,
) -> Tuple[float, int, int] | Tuple[float, int, int, np.ndarray, np.ndarray]:
    """
    КЛЮЧЕВОЙ FIX:
    - считаем, что hp_color_ranges = [fill, bg] (как ты и передаёшь: красн/зелён + коричн)
    - mask_hp = ТОЛЬКО заливка (fill)
    - mask_bg = фон (коричневый/чёрный)
    - hp_ratio считаем по длине непрерывного участка fill слева внутри прямоугольника бара.

    Это работает идеально, когда ROI — уже "белый прямоугольник" (полный бар).
    """
    if roi is None or roi.size == 0:
        if return_masks:
            return 0.0, 0, 0, None, None  # type: ignore
        return 0.0, 0, 0

    h, w = roi.shape[:2]
    if h == 0 or w == 0:
        if return_masks:
            return 0.0, 0, 0, None, None  # type: ignore
        return 0.0, 0, 0

    if not hp_color_ranges:
        if return_masks:
            return 0.0, 0, 0, None, None  # type: ignore
        return 0.0, 0, 0

    # --- 1) fill = ПЕРВЫЙ диапазон (красн/зелён)
    fill_lo, fill_up = hp_color_ranges[0]
    mask_fill = (cv2.inRange(roi, fill_lo, fill_up) > 0)  # bool

    # --- 2) bg = либо второй диапазон (если есть), либо bg_color_ranges, либо black/brown по mode
    mask_bg = np.zeros((h, w), dtype=bool)

    # 2.1) если дали второй диапазон в hp_color_ranges — считаем его частью фона
    if len(hp_color_ranges) >= 2:
        bg_lo2, bg_up2 = hp_color_ranges[1]
        mask_bg |= (cv2.inRange(roi, bg_lo2, bg_up2) > 0)

    # 2.2) если отдельно дали bg_color_ranges — тоже добавим
    if bg_color_ranges:
        for lo, up in bg_color_ranges:
            mask_bg |= (cv2.inRange(roi, lo, up) > 0)

    # 2.3) если hero (bg_mode=black) — добавим "чёрный фон"
    if bg_mode == "black":
        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        mask_bg |= (gray < 25)

    # Итоговые маски
    mask_hp = mask_fill  # ВАЖНО: HP = только заливка
    hp_pixels = int(mask_hp.sum())
    bg_pixels = int(mask_bg.sum())

    # --- 3) определяем реальные границы бара по (fill OR bg)
    bar_cols = (mask_hp | mask_bg).any(axis=0)
    xs_bar = np.where(bar_cols)[0]
    if xs_bar.size == 0:
        if return_masks:
            return 0.0, hp_pixels, bg_pixels, mask_hp, mask_bg  # type: ignore
        return 0.0, hp_pixels, bg_pixels

    bar_x0 = int(xs_bar[0])
    bar_x1 = int(xs_bar[-1])
    bar_w = bar_x1 - bar_x0 + 1
    if bar_w <= 0:
        if return_masks:
            return 0.0, hp_pixels, bg_pixels, mask_hp, mask_bg  # type: ignore
        return 0.0, hp_pixels, bg_pixels

    # --- 4) колонки заливки (любая заливка в колонке) — без доп. фильтров
    fill_cols = mask_hp.any(axis=0)
    xs_fill = np.where(fill_cols & (np.arange(w) >= bar_x0) & (np.arange(w) <= bar_x1))[0]
    if xs_fill.size == 0:
        # нет заливки => HP=0
        if return_masks:
            return 0.0, hp_pixels, bg_pixels, mask_hp, mask_bg  # type: ignore
        return 0.0, hp_pixels, bg_pixels

    # старт заполнения: первая колонка с заливкой внутри бара
    start = int(xs_fill[0])

    # --- 5) длина HP: идём вправо пока заливка есть, допускаем небольшие дырки
    hp_len = 0
    gap = 0
    for x in range(start, bar_x1 + 1):
        if fill_cols[x]:
            hp_len += 1 + gap
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break

    if hp_len < min_hp_len_pixels:
        if return_masks:
            return 0.0, hp_pixels, bg_pixels, mask_hp, mask_bg  # type: ignore
        return 0.0, hp_pixels, bg_pixels

    # hp_ratio относительно полной ширины бара
    hp_ratio = hp_len / float(bar_w)
    hp_ratio = max(0.0, min(1.0, hp_ratio))

    if return_masks:
        return hp_ratio, hp_pixels, bg_pixels, mask_hp, mask_bg  # type: ignore
    return hp_ratio, hp_pixels, bg_pixels






# ======================================================================
#                         MANA ESTIMATION
# ======================================================================

def _estimate_mana_ratio_under_hp(
    frame_rgb: np.ndarray,
    x0: int,
    x1: int,
    hp_y1: int,
    max_mana_h: int = 5,
    black_gap: int = 0,
    max_gap: int = 1,
) -> Tuple[bool, float]:
    """
    Ищем МАНУ ПОД HP.
    - ищем ровно через один черный пиксель
    - ищем полосу строго цвета (79,120,249) ± tol
    - без дырок (max_gap=1)
    Возвращает: (has_mana, mana_ratio)
    """
    h, w = frame_rgb.shape[:2]

    y_start = hp_y1 + black_gap
    y_end = min(h, y_start + max_mana_h)

    if y_end < y_start:
        return False, 0.0

    roi = frame_rgb[y_start:y_end, x0:x1]
    if roi.size == 0:
        return False, 0.0

    # создаём маску для маны (жёсткий цвет ± допуск)
    lower = np.clip(MANA_COLOR - MANA_TOL, 0, 255).astype(np.uint8)
    upper = np.clip(MANA_COLOR + MANA_TOL, 0, 255).astype(np.uint8)
    mask_blue = cv2.inRange(roi, lower, upper) > 0

    if not mask_blue.any():
        return False, 0.0

    h_roi, w_roi = mask_blue.shape

    # "Есть ли синий в колонке"
    col_blue = mask_blue.any(axis=0)
    xs = np.where(col_blue)[0]
    if xs.size == 0:
        return False, 0.0

    start = int(xs[0])

    mana_len = 0
    gap = 0

    for v in col_blue[start:]:
        if v:
            mana_len += 1 + gap
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break

    if mana_len < 2:
        return False, 0.0

    mana_ratio = mana_len / float(w_roi)
    mana_ratio = max(0.0, min(1.0, mana_ratio))

    return True, mana_ratio


def _check_bar_gradient_structure(
    mask_hp: np.ndarray,
    max_edge_jitter: int = 2,      # макс. гуляние левого/правого края по X
    min_rows_with_hp: int = 1,     # минимум строк, где есть HP
) -> bool:
    """
    Строгая проверка: HP-полоса должна быть почти идеальным прямоугольником.

    Условия:
      - в каждой строке не более одного непрерывного HP-сегмента;
      - внутри этого сегмента не должно быть дыр;
      - левые и правые края сегмента мало гуляют по строкам (<= max_edge_jitter).
    """
    h, w = mask_hp.shape
    if w < 3 or h < 1:
        return False

    lefts: list[int] = []
    rights: list[int] = []
    rows_with_hp = 0

    for y in range(h):
        row = mask_hp[y]          # (w,)
        xs = np.where(row)[0]
        if xs.size == 0:
            continue

        rows_with_hp += 1
        left = int(xs[0])
        right = int(xs[-1])
        if right <= left:
            return False

        # Проверяем, что внутри [left:right] нет дыр (HP непрерывный)
        seg = row[left:right + 1]
        # если есть хоть один False (дыра) внутри сегмента — не идеальный бар
        if not seg.all():
            return False

        lefts.append(left)
        rights.append(right)

    if rows_with_hp < min_rows_with_hp:
        return False

    # Края должны быть стабильны по строкам
    if max(lefts) - min(lefts) > max_edge_jitter:
        return False
    if max(rights) - min(rights) > max_edge_jitter:
        return False

    return True

def _check_rect_strict(mask_hp: np.ndarray,
                       min_aspect: float = 4.0,
                       max_h: int = 6) -> bool:
    """
    Строгая проверка:
    - маска должна быть одним прямоугольным блоком без дыр
    - ширина сильно больше высоты
    - высота не слишком большая
    mask_hp — bool или 0/1 маска HP в ROI.
    """
    if mask_hp is None or mask_hp.size == 0:
        return False

    # приводим к bool на всякий
    m = mask_hp.astype(bool)
    ys, xs = np.where(m)
    if xs.size == 0:
        return False

    x0, x1 = xs.min(), xs.max()
    y0, y1 = ys.min(), ys.max()

    w = x1 - x0 + 1
    h = y1 - y0 + 1
    if w <= 0 or h <= 0:
        return False

    # аспект для HP бара: ширина >> высота
    aspect = w / float(h)
    if aspect < min_aspect:
        return False

    # ограничим максимальную высоту (чисто safety)
    if h > max_h:
        return False

    # вырезаем bounding rect и проверяем, что внутри НЕТ дыр
    rect = m[y0:y1+1, x0:x1+1]
    # если хоть один False внутри – не литой прямоугольник
    if not rect.all():
        return False

    # а ещё проверим, что снаружи в mask_hp ничего нет
    m_copy = m.copy()
    m_copy[y0:y1+1, x0:x1+1] = False
    if m_copy.any():
        # значит, есть лишние пиксели вне прямоугольника
        return False

    return True
def largest_solid_rect(comp_mask: np.ndarray):
    """
    Ищет максимальный (по площади) прямоугольник из 1 в бинарной матрице.
    comp_mask — bool или uint8 (0/1).
    Возвращает (x0, y0, x1, y1) в ЛОКАЛЬНЫХ координатах comp_mask
    или None, если единиц нет.
    """


    m = comp_mask.astype(bool)
    h, w = m.shape

    heights = np.zeros(w, dtype=int)
    max_area = 0
    best = None

    for y in range(h):
        # обновляем "высоты" столбцов (сколько подряд 1 вверх)
        for x in range(w):
            if m[y, x]:
                heights[x] += 1
            else:
                heights[x] = 0

        # ищем максимальный прямоугольник в гистограмме heights
        stack = []
        x = 0
        while x <= w:
            cur_h = heights[x] if x < w else 0
            if not stack or cur_h >= heights[stack[-1]]:
                stack.append(x)
                x += 1
            else:
                top = stack.pop()
                h_rect = heights[top]
                if h_rect == 0:
                    continue
                x_right = x - 1
                x_left = stack[-1] + 1 if stack else 0
                width = x_right - x_left + 1
                area = h_rect * width
                if area > max_area:
                    max_area = area
                    y1 = y
                    y0 = y - h_rect + 1
                    best = (x_left, y0, x_right, y1)

    return best  # None, если единиц не было вообще





# ======================================================================
#                        MAIN HP BAR FINDER
# ======================================================================

def find_hp_bars(
    frame_rgb: np.ndarray,
    hp_color_ranges: List[Tuple[np.ndarray, np.ndarray]],
    w_min: int = 40,
    w_max: int = 50,
    h_min: int = 1,
    h_max: int = 5,
    target_w: int | None = None,
    require_mana: bool = True,
    right_side_mode: Optional[str] = None,  # "hero", "creep" или None
    relaxed_merge: bool = False,
    bg_mode: str = "black",  # "black" или "brown"
    bg_color_ranges: Optional[List[Tuple[np.ndarray, np.ndarray]]] = None,
    right_side_brown_range: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> List[HpBarBox]:



    h, w = frame_rgb.shape[:2]
    if h == 0 or w == 0:
        return []

    if target_w is None:
        target_w = (w_min + w_max) // 2  # 45

    # 1) Маска HP (первичный "семенной" цвет/цвета)
    mask = np.zeros((h, w), dtype=np.uint8)
    for lower, upper in hp_color_ranges:
        mask |= cv2.inRange(frame_rgb, lower, upper)

    # 2) Морфология — стягиваем полоски в сплошные сегменты
    if right_side_mode == 'hero':
        kernel_h = np.ones((1, 3), np.uint8)

        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_h, iterations=1)




    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)

    prelim: List[Tuple[HpBarBox, float]] = []

    for label_id in range(1, num_labels):
        x, y, ww, hh, area = stats[label_id]

        # отсечь совсем мелкий мусор
        if ww < 4 or hh < 1 or hh > 10:
            continue

        labels_roi = labels[y:y + hh, x:x + ww]
        comp_mask = (labels_roi == label_id)
        if right_side_mode == "creep":
            core = largest_solid_rect(comp_mask)
            if core is None:
                continue

            x0_loc, y0_loc, x1_loc, y1_loc = core
        else:
            x0_loc, y0_loc = 0, 0
            x1_loc, y1_loc = ww - 1, hh - 1
        # переводим локальные координаты в координаты кадра (ИНКЛЮЗИВНО)
        x0_bar = x + x0_loc
        x1_bar = x + x1_loc
        y0_bar = y + y0_loc
        y1_bar = y + y1_loc

        bar_w = x1_bar - x0_bar + 1
        bar_h = y1_bar - y0_bar + 1

        if not (w_min <= bar_w <= w_max):
            continue
        if not (h_min <= bar_h <= h_max):
            continue




        # ---------- ROI ДЛЯ ОЦЕНКИ HP (УЖЕ ПО УТОЧНЁННОМУ БОКСУ) ----------
        roi = frame_rgb[y0_bar:y1_bar + 1, x0_bar:x1_bar + 1]  # +1, чтобы включить нижнюю/правую границу
        if roi.size == 0:
            continue

        hp_ratio, hp_pixels, bg_pixels, mask_hp, mask_bg = _estimate_hp_ratio_from_roi(
            roi,
            hp_color_ranges=hp_color_ranges,
            max_gap=2,
            min_hp_len_pixels=1,
            bg_mode=bg_mode,
            bg_color_ranges=bg_color_ranges,
            return_masks=True,
        )

        # если вообще не набрали HP — дальше смысла нет
        if hp_ratio <= 0.0 or mask_hp is None or mask_bg is None:
            continue

        if right_side_mode is not None:
            if right_side_mode == "creep" and right_side_brown_range is None:
                continue

            if not _check_bar_right_side(
                    roi,
                    mode=right_side_mode,
                    brown_range=(
                            right_side_brown_range
                            if right_side_brown_range is not None
                            else (HP_BROWN_HEROES_LOWER, HP_BROWN_HEROES_UPPER)
                    ),
            ):
                continue

        if right_side_mode == 'creep':
            mask_bar = (mask_hp | mask_bg)
            if not _check_rect_strict(mask_bar, min_aspect=4.0, max_h=h_max):
                continue

        # ---------- МАНА ПОД УТОЧНЁННЫМ HP (если нужна) ----------
        if require_mana:

            has_mana, mana_ratio = _estimate_mana_ratio_under_hp(
                frame_rgb,
                x0=x0_bar + 1,
                x1=x1_bar ,  # x1 в функции ИСКЛЮЗИВНЫЙ
                hp_y1=y1_bar,  # нижняя граница HP-бара (инклюзив)
            )
        else:
            has_mana = False
            mana_ratio = 0.0

        # ========= СВЯЗКА HP <-> MANA =========

        exists_mana = (has_mana or not require_mana)
        if not (exists_mana):
            continue



        # ВАЖНО: используем ИМЕННО x0_bar / y0_bar / x1_bar / y1_bar
        box = HpBarBox(
            x0=int(x0_bar),
            y0=int(y0_bar),
            x1=int(x1_bar),
            y1=int(y1_bar),
            hp_ratio=float(hp_ratio),
            mana_ratio=float(mana_ratio),
            has_mana=bool(has_mana),
        )
        prelim.append((box, hp_ratio))

    result = [box for (box, score) in prelim]
    return result
def find_enemy_heroes_hp_bars(
    frame_rgb: np.ndarray,
    w_min: int = 40,
    w_max: int = 50,
    h_min: int = 4,
    h_max: int = 5,
    target_w: int | None = None,
) -> List[HpBarBox]:
    hp_ranges = [
        (ENEMY_RED_HEROES_LOWER, ENEMY_RED_HEROES_UPPER),
        (HP_BROWN_HEROES_LOWER,  HP_BROWN_HEROES_UPPER),
    ]
    return find_hp_bars(
        frame_rgb,
        hp_color_ranges=hp_ranges,
        w_min=w_min, w_max=w_max,
        h_min=h_min, h_max=h_max,
        target_w=target_w,
        require_mana=True,
        right_side_mode="hero",
        relaxed_merge=False,
        bg_mode="black",
        bg_color_ranges=None,
        right_side_brown_range=(HP_BROWN_HEROES_LOWER, HP_BROWN_HEROES_UPPER),
    )



def find_ally_heroes_hp_bars(
    frame_rgb: np.ndarray,
    w_min: int = 40,
    w_max: int = 50,
    h_min: int = 4,
    h_max: int = 5,
    target_w: int | None = None,
) -> List[HpBarBox]:
    # союзник: зелёная HP, мана под ним опциональна (если в игре её нет)
    hp_ranges = [
        (ALLY_GREEN_HEROES_LOWER, ALLY_GREEN_HEROES_UPPER),
        (HP_BROWN_HEROES_LOWER, HP_BROWN_HEROES_UPPER),
    ]
    return find_hp_bars(
        frame_rgb,
        hp_color_ranges=hp_ranges,
        w_min=w_min, w_max=w_max,
        h_min=h_min, h_max=h_max,
        target_w=target_w,
        require_mana=True,
        right_side_mode="hero",
        relaxed_merge=False,
        bg_mode="black",
        bg_color_ranges=None,
        right_side_brown_range=(HP_BROWN_HEROES_LOWER, HP_BROWN_HEROES_UPPER),
    )

def find_self_heroes_hp_bars(
    frame_rgb: np.ndarray,
    w_min: int = 40,
    w_max: int = 50,
    h_min: int = 4,
    h_max: int = 6,
    target_w: int | None = None,
) -> List[HpBarBox]:
    # союзник: зелёная HP, мана под ним опциональна (если в игре её нет)
    hp_ranges = [
        (SELF_GREEN_HEROES_LOWER, SELF_GREEN_HEROES_UPPER),
        (HP_BROWN_HEROES_SELF_LOWER, HP_BROWN_HEROES_SELF_UPPER)
    ]
    return find_hp_bars(
        frame_rgb,
        hp_color_ranges=hp_ranges,
        w_min=w_min, w_max=w_max,
        h_min=h_min, h_max=h_max,
        target_w=target_w,
        require_mana=True,
        right_side_mode="hero",
        relaxed_merge=False,
        bg_mode="black",
        bg_color_ranges=None,
        right_side_brown_range=(HP_BROWN_HEROES_LOWER, HP_BROWN_HEROES_UPPER),
    )

def find_ally_creeps_hp_bars(
    frame_rgb: np.ndarray,
    w_min: int = 30,
    w_max: int = 40,
    h_min: int = 1,
    h_max: int = 2,
    target_w: int | None = None,
) -> List[HpBarBox]:
    hp_ranges = [
        (ALLY_GREEN_CREEPS_LOWER, ALLY_GREEN_CREEPS_UPPER),
        (HP_BROWN_ALLY_CREEPS_LOWER, HP_BROWN_ALLY_CREEPS_UPPER)
    ]
    return find_hp_bars(
        frame_rgb,
        hp_color_ranges=hp_ranges,
        w_min=w_min, w_max=w_max,
        h_min=h_min, h_max=h_max,
        target_w=target_w,
        require_mana=False,
        right_side_mode="creep",
        relaxed_merge=True,
        bg_mode="brown",
        bg_color_ranges=[(
            HP_BROWN_ALLY_CREEPS_LOWER,
            HP_BROWN_ALLY_CREEPS_UPPER,
        )],
        right_side_brown_range=(
            HP_BROWN_ALLY_CREEPS_LOWER,
            HP_BROWN_ALLY_CREEPS_UPPER,
        ),
    )

def find_enemy_creeps_hp_bars(
    frame_rgb: np.ndarray,
    w_min: int = 30,
    w_max: int = 41,
    h_min: int = 1,
    h_max: int = 2,
    target_w: int | None = None,
) -> List[HpBarBox]:
    hp_ranges = [
        (ENEMY_RED_CREEPS_LOWER, ENEMY_RED_CREEPS_UPPER),
        (HP_BROWN_ENEMY_CREEPS_LOWER, HP_BROWN_ENEMY_CREEPS_UPPER)
    ]
    return find_hp_bars(
        frame_rgb,
        hp_color_ranges=hp_ranges,
        w_min=w_min, w_max=w_max,
        h_min=h_min, h_max=h_max,
        target_w=target_w,
        require_mana=False,
        right_side_mode="creep",
        relaxed_merge=True,
        bg_mode="brown",
        bg_color_ranges=[(
            HP_BROWN_ENEMY_CREEPS_LOWER,
            HP_BROWN_ENEMY_CREEPS_UPPER,
        )],
        right_side_brown_range=(
            HP_BROWN_ENEMY_CREEPS_LOWER,
            HP_BROWN_ENEMY_CREEPS_UPPER,
        ),
    )

def _filter_creeps_touching_self(
    creeps: List[HpBarBox],
    self_bars: List[HpBarBox],
    max_y_gap: int = 3,              # максимум пикселей по вертикали между верхом self и низом крипа
    use_x_overlap: bool = False,     # если False — игнорируем X, режем всё над self в коридоре dy
    min_x_overlap_ratio: float = 0.3 # если use_x_overlap=True: минимум перекрытия по X (доля от меньшей ширины)
) -> List[HpBarBox]:
    """
    Убираем крипов, чьи HP-бары "прилипают" к self-герою сверху.

    Критерий по вертикали (всегда):
      - крип лежит ВЫШЕ self-бара: c.y1 <= s.y0
      - вертикальный зазор небольшой: s.y0 - c.y1 <= max_y_gap

    Дополнительно (если use_x_overlap=True):
      - по X есть существенное перекрытие:
          overlap_x >= min(c.width, s.width) * min_x_overlap_ratio

    Если use_x_overlap=False — учитываем только вертикальный коридор над self,
    вообще не смотрим пересечение по X.
    """
    if not creeps or not self_bars:
        return creeps

    filtered: List[HpBarBox] = []

    for c in creeps:
        remove = False

        for s in self_bars:
            # --- вертикальный критерий: "над self и в коридоре dy" ---
            if not (c.y1 <= s.y0):
                continue

            dy = s.y0 - c.y1
            if dy > max_y_gap:
                continue  # слишком далеко сверху

            if use_x_overlap:
                # --- доп. проверка по X ---
                overlap_x = max(0, min(c.x1, s.x1) - max(c.x0, s.x0) + 1)
                if overlap_x <= 0:
                    continue

                min_w = min(c.width, s.width)
                if overlap_x < min_w * min_x_overlap_ratio:
                    continue

            # сюда дошли — крип считается "прилипшим" к self
            remove = True
            break

        if not remove:
            filtered.append(c)

    return filtered


def scan_hp_bars_on_screen(frame_rgb: np.ndarray) -> Dict[str, Dict[str, List[HpBarBox]]]:
    """
    Общий сканер: на вход полный скрин (RGB),
    на выход словарь вида:
    {
        "heroes": {
            "enemy": [...],
            "ally":  [...],
            "self":  [...],
        },
        "creeps": {
            "enemy": [...],
            "ally":  [...],  # уже отфильтрованные от "прилипших" к self
        }
    }
    """

    # --- герои ---
    bars_enemy_heroes = find_enemy_heroes_hp_bars(frame_rgb)
    bars_ally_heroes  = find_ally_heroes_hp_bars(frame_rgb)
    bars_self_heroes  = find_self_heroes_hp_bars(frame_rgb)

    # --- крипы ---
    bars_ally_creeps  = find_ally_creeps_hp_bars(frame_rgb)
    bars_enemy_creeps = find_enemy_creeps_hp_bars(frame_rgb)

    # фильтруем крипов, которые вплотную контактят с self-героем сверху
    bars_ally_creeps_filtered = _filter_creeps_touching_self(
        bars_ally_creeps,
        bars_self_heroes,
        max_y_gap=4,  # настраиваемый dy
        use_x_overlap=False,  # режем ВСЁ над self в коридоре dy, X не смотрим
        # min_x_overlap_ratio=0.1  # тут уже неважен, но параметр есть на будущее
    )

    result: Dict[str, Dict[str, List[HpBarBox]]] = {
        "heroes": {
            "enemy": bars_enemy_heroes,
            "ally":  bars_ally_heroes,
            "self":  bars_self_heroes,
        },
        "creeps": {
            "enemy": bars_enemy_creeps,
            "ally":  bars_ally_creeps_filtered,
        },
    }
    return result




# ======================================================================
#                             WINDOW UTILS
# ======================================================================

def _get_window_title(hwnd: int) -> str:
    try: return win32gui.GetWindowText(hwnd) or ""
    except: return ""

def _is_main_candidate(hwnd: int) -> bool:
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd): return False
        if win32gui.GetParent(hwnd): return False
        t = _get_window_title(hwnd).strip()
        return bool(t)
    except:
        return False

def _window_area(hwnd: int) -> int:
    try:
        L,T,R,B=win32gui.GetWindowRect(hwnd)
        return max(0,R-L)*max(0,B-T)
    except:
        return 0

def find_main_hwnd_for_pid(pid: int) -> Optional[int]:
    arr=[]
    def _cb(h,_):
        try:
            _,wpid=win32process.GetWindowThreadProcessId(h)
            if wpid==pid and _is_main_candidate(h):
                arr.append(h)
        except: pass
    try: win32gui.EnumWindows(_cb,None)
    except: pass
    if not arr: return None
    arr.sort(key=_window_area,reverse=True)
    return arr[0]

def client_rect_screen(hwnd: int):
    try:
        l,t,r,b=win32gui.GetClientRect(hwnd)
        x,y=win32gui.ClientToScreen(hwnd,(0,0))
        return x,y,max(1,r-l),max(1,b-t)
    except:
        L,T,R,B=win32gui.GetWindowRect(hwnd)
        return L,T,max(1,R-L),max(1,B-T)

def grab_roi_rgb_from_window(hwnd: int):
    cx,cy,cw,ch=client_rect_screen(hwnd)
    try:
        arr=np.array(p.screenshot(region=(cx,cy,cw,ch)))
        return arr  # RGB
    except Exception as e:
        print("[grab]",e)
        return None


# ======================================================================
#                               DEMO
# ======================================================================

if __name__=="__main__":
    pid = 24104
    hwnd = find_main_hwnd_for_pid(pid)
    if hwnd is None:
        print("Не нашёл окно!")
        exit()

    while True:
        frame = grab_roi_rgb_from_window(hwnd)
        if frame is None:
            continue

        bars_enemy = find_enemy_heroes_hp_bars(frame)
        bars_ally = find_ally_heroes_hp_bars(frame)
        bars_self = find_self_heroes_hp_bars(frame)
        bars_creeps_ally = find_ally_creeps_hp_bars(frame)
        bars_creeps_enemy = find_enemy_creeps_hp_bars(frame)
        print(f"Bars_enemy: {len(bars_enemy)}")
        for i,b in enumerate(bars_enemy):
            print(f"{i}: {b.as_tuple()}, HP={b.hp_ratio:.3f}, MANA={b.mana_ratio:.3f}, has_mana={b.has_mana}")
            cv2.rectangle(frame, (b.x0,b.y0), (b.x1,b.y1), (0,255,0),1)
        print(f"Bars_ally: {len(bars_ally)}")
        for i,b in enumerate(bars_ally):
            #print(f"{i}: {b.as_tuple()}, HP={b.hp_ratio:.3f}, MANA={b.mana_ratio:.3f}, has_mana={b.has_mana}")
            cv2.rectangle(frame, (b.x0,b.y0), (b.x1,b.y1), (255,0,0),1)
        #print(f"Bars_self: {len(bars_self)}")
        for i, b in enumerate(bars_self):
            #print(f"{i}: {b.as_tuple()}, HP={b.hp_ratio:.3f}, MANA={b.mana_ratio:.3f}, has_mana={b.has_mana}")
            cv2.rectangle(frame, (b.x0, b.y0), (b.x1, b.y1), (255, 0, 0), 1)
        print(f"Bars_creeps_ally: {len(bars_creeps_ally)}")
        for i, b in enumerate(bars_creeps_ally):
            print(f"{i}: {b.as_tuple()}, HP={b.hp_ratio:.3f}, MANA={b.mana_ratio:.3f}, has_mana={b.has_mana}")
            cv2.rectangle(frame, (b.x0, b.y0), (b.x1, b.y1), (255, 0, 0), 1)
        print(f"Bars_creeps_enemy: {len(bars_creeps_enemy)}")
        for i, b in enumerate(bars_creeps_enemy):
            print(f"{i}: {b.as_tuple()}, HP={b.hp_ratio:.3f}, MANA={b.mana_ratio:.3f}, has_mana={b.has_mana}")
            cv2.rectangle(frame, (b.x0, b.y0), (b.x1, b.y1), (255, 0, 0), 1)

        img=cv2.cvtColor(frame,cv2.COLOR_RGB2BGR)
        cv2.imshow("HP+MANA",img)
        if cv2.waitKey(1)&0xFF==27:
            break
    cv2.destroyAllWindows()
