import win32gui
import win32api
import win32con
import win32process
import ctypes
from typing import Dict, List, Tuple, Optional, Union
import logging
from functools import wraps
from ctypes import wintypes

user32 = ctypes.WinDLL('user32', use_last_error=True)
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000
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
# ==== Win32 utils ====
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
def _title(hwnd:int)->str:
    try: return win32gui.GetWindowText(hwnd) or ""
    except: return ""

def _is_main_visible(hwnd:int)->bool:
    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.GetParent(hwnd) and bool(_title(hwnd).strip())
    except: return False

def _area(hwnd:int)->int:
    try:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return max(0,R-L)*max(0,B-T)
    except: return 0

def find_dota_hwnd()->Optional[int]:
    c=[]
    def cb(hwnd,_):
        if not _is_main_visible(hwnd): return
        t=_title(hwnd)
        if "Dota 2" in t or "Dota" in t:
            c.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not c: return None
    c.sort(key=_area, reverse=True)
    return c[0]

def client_rect_screen(hwnd:int)->Tuple[int,int,int,int]:
    """Клиентская область в координатах экрана (device pixels)."""
    try:
        l,t,r,b = win32gui.GetClientRect(hwnd)
        x,y = win32gui.ClientToScreen(hwnd, (0,0))
        return x,y,max(1,r-l),max(1,b-t)
    except:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return L,T,max(1,R-L),max(1,B-T)

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

        # формируем строку с аргументами (без гигантских дампов)
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