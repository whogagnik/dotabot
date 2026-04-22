from __future__ import annotations

from typing import Optional
import numpy as np
import dxcam

from dota_window import get_client_rect


class DotaCapture:
    def __init__(self):
        self.cam = dxcam.create(output_idx=0, output_color="RGB")

    def grab_window_rgb(self, hwnd: int) -> Optional[np.ndarray]:
        x, y, w, h = get_client_rect(hwnd)
        region = (x, y, x + w, y + h)

        frame = self.cam.grab(region=region)
        if frame is None:
            return None
        if not isinstance(frame, np.ndarray):
            return None
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)

        return frame.copy()