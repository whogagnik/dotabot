from __future__ import annotations

from typing import Optional
import numpy as np
import dxcam
import win32api
import win32gui

from dota_window import get_client_rect


class DotaCapture:
    def __init__(self):
        self.cam = dxcam.create(output_idx=0, output_color="RGB")

    @staticmethod
    def _desktop_bounds() -> tuple[int, int, int, int]:
        """
        Virtual desktop bounds:
        left, top, right, bottom
        """
        left = win32api.GetSystemMetrics(76)    # SM_XVIRTUALSCREEN
        top = win32api.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
        width = win32api.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        height = win32api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN

        return int(left), int(top), int(left + width), int(top + height)

    @staticmethod
    def _clamp_region_to_desktop(
        region: tuple[int, int, int, int],
    ) -> Optional[tuple[int, int, int, int]]:
        left, top, right, bottom = region
        desk_left, desk_top, desk_right, desk_bottom = DotaCapture._desktop_bounds()

        left = max(left, desk_left)
        top = max(top, desk_top)
        right = min(right, desk_right)
        bottom = min(bottom, desk_bottom)

        if right <= left or bottom <= top:
            return None

        # dxcam обычно ждёт координаты относительно origin virtual desktop.
        # Если virtual desktop начинается не с 0,0 — нормализуем.
        if desk_left != 0 or desk_top != 0:
            left -= desk_left
            right -= desk_left
            top -= desk_top
            bottom -= desk_top

        if right <= left or bottom <= top:
            return None

        return int(left), int(top), int(right), int(bottom)

    @staticmethod
    def _window_ok(hwnd: int) -> bool:
        try:
            return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
        except Exception:
            return False

    def grab_window_rgb(self, hwnd: int) -> Optional[np.ndarray]:
        if not self._window_ok(int(hwnd)):
            return None

        try:
            x, y, w, h = get_client_rect(int(hwnd))
        except Exception:
            return None

        x = int(x)
        y = int(y)
        w = int(w)
        h = int(h)

        if w <= 0 or h <= 0:
            return None

        region = self._clamp_region_to_desktop((x, y, x + w, y + h))
        if region is None:
            return None

        try:
            frame = self.cam.grab(region=region)
        except ValueError:
            return None
        except Exception:
            return None

        if frame is None:
            return None

        if not isinstance(frame, np.ndarray):
            return None

        if frame.ndim != 3 or frame.shape[2] != 3:
            return None

        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)

        return frame.copy()