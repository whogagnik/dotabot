from __future__ import annotations

from typing import Optional, List, Tuple
import win32gui


def _enum_windows() -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if title.strip():
            out.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    return out


def find_dota_hwnd(title_contains: tuple[str, ...] = ("Dota 2", "dota 2")) -> Optional[int]:
    for hwnd, title in _enum_windows():
        t = title.lower()
        if any(s.lower() in t for s in title_contains):
            return hwnd
    return None


def get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """
    Возвращает клиентскую область окна в экранных координатах:
    (screen_x, screen_y, width, height)
    """
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        return sx, sy, max(1, r - l), max(1, b - t)
    except Exception:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return L, T, max(1, R - L), max(1, B - T)