from CONSTANTS import *
import win32api
from typing import Tuple


def _get_screen_size() -> Tuple[int,int]:
    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)


class WindowPlacer:
    def __init__(self,
                 mode=TILE_MODE,
                 columns=TILE_COLUMNS,
                 gap=TILE_GAP,
                 bottom_h=TILE_BOTTOM_HEIGHT,
                 wrap_at=GRID_WRAP_AT,
                 tile_w=640, tile_h=480):  # <— фиксированный размер окна доты
        self.mode = mode
        self.columns = max(1, int(columns))
        self.gap = max(0, int(gap))
        self.bottom_h = max(200, int(bottom_h))
        self.wrap_at = wrap_at
        self.tile_w = int(tile_w)
        self.tile_h = int(tile_h)

    def rect_for(self, index: int) -> Tuple[int, int, int, int]:
        sw, sh = _get_screen_size()
        gap = self.gap

        if self.mode == "grid":
            # фиксированный размер 640×480
            w, h = self.tile_w, self.tile_h

            # если заданы колонки — используем их, иначе считаем как раньше
            if self.columns:
                cols = max(1, int(self.columns))
            else:
                wrap = min(sw, self.wrap_at if self.wrap_at else sw)
                cols = max(1, (wrap - gap) // (w + gap))

            col = index % cols
            row = index // cols
            x = gap + col * (w + gap)
            y = gap + row * (h + gap)

            # клэмпы, чтобы не выезжать за экран
            x = min(x, max(0, sw - w - gap))
            y = min(y, max(0, sh - h - gap))
            return x, y, w, h

        if self.mode == "bottom":
            cols = max(self.columns, 2)
            w = max(self.tile_w, (sw - gap * (cols + 1)) // cols)
            h = self.tile_h
            col = index % cols
            x = gap + col * (w + gap)
            y = sh - self.bottom_h + gap
            return x, y, w, h

        # right
        half = sw // 2
        usable = sw - half - gap * 2
        w = max(self.tile_w, (usable - gap * (self.columns - 1)) // self.columns)
        rows = max(1, sh // max(self.tile_h, 360))
        h = self.tile_h
        col = index % self.columns
        row = index // self.columns
        x = half + gap + col * (w + gap)
        y = gap + row * (h + gap)
        if y + h + gap > sh:
            y = sh - h - gap
        return x, y, w, h
