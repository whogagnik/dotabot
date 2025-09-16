# overlay_text_lib.py (v3) — multi-text overlay (Windows only)
# API:
#   init_overlay(keep_on_top_ms=300, max_items=20)
#   paint_with_coords(x, y, text, color_hex="#ffffff", font_name="Segoe UI", font_px=24)
#   paint_wtih_coords(...)  # алиас
#   set_keep_on_top_interval(ms)
#   set_max_items(n)
#   clear_overlay()
#   hide_overlay()
#   close_overlay()

import ctypes, threading, sys, atexit
from ctypes import wintypes
import time
if sys.platform != "win32":
    raise OSError("overlay_text_lib: Windows only")

# ---------- defaults ----------
DEFAULT_FONT_NAME = "Segoe UI"
DEFAULT_FONT_PX   = 10
DEFAULT_COLOR_HEX = "#ffffff"
PADDING           = 8
MAGENTA_BGR0      = 0x00FF00FF  # colorkey
KEEP_TIMER_ID     = 1001

# ---------- type shims ----------
HANDLE    = wintypes.HANDLE
HINSTANCE = getattr(wintypes, "HINSTANCE", HANDLE)
HCURSOR   = getattr(wintypes, "HCURSOR",   HANDLE)
HICON     = getattr(wintypes, "HICON",     HANDLE)
HBRUSH    = getattr(wintypes, "HBRUSH",    HANDLE)
HBITMAP   = getattr(wintypes, "HBITMAP",   HANDLE)
HDC       = getattr(wintypes, "HDC",       HANDLE)
HMENU     = getattr(wintypes, "HMENU",     HANDLE)
LPVOID    = ctypes.c_void_p

user32 = ctypes.windll.user32
gdi32  = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# DPI-aware
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------- consts ----------
WS_POPUP          = 0x80000000
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_NOACTIVATE  = 0x08000000

SW_SHOWNOACTIVATE = 4
SW_HIDE           = 0
SWP_NOSIZE        = 0x0001
SWP_NOMOVE        = 0x0002
SWP_NOZORDER      = 0x0004
SWP_NOACTIVATE    = 0x0010
SWP_SHOWWINDOW    = 0x0040
HWND_TOPMOST      = wintypes.HWND(-1)

LWA_COLORKEY      = 0x00000001

WM_DESTROY      = 0x0002
WM_ERASEBKGND   = 0x0014
WM_PAINT        = 0x000F
WM_NCHITTEST    = 0x0084
WM_TIMER        = 0x0113
HTTRANSPARENT   = -1
WM_APP          = 0x8000
WM_APP_REBUILD  = WM_APP + 1
WM_APP_HIDE     = WM_APP + 2
WM_APP_SETKEEP  = WM_APP + 3
WM_APP_CLEAR    = WM_APP + 4

# GDI / DrawText
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
TRANSPARENT = 1
FW_NORMAL = 400
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
ANTIALIASED_QUALITY = 4
DEFAULT_PITCH = 0
DT_LEFT = 0x0000
DT_TOP = 0x0000
DT_NOPREFIX = 0x0800
DT_CALCRECT = 0x0400

# GetSystemMetrics
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# ---------- structs ----------
WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wintypes.UINT),
        ("style",         wintypes.UINT),
        ("lpfnWndProc",   WNDPROC),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     HINSTANCE),
        ("hIcon",         HICON),
        ("hCursor",       HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName",  wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm",       HICON),
    ]

class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",        wintypes.DWORD),
        ("biWidth",       wintypes.LONG),
        ("biHeight",      wintypes.LONG),  # negative -> top-down
        ("biPlanes",      wintypes.WORD),
        ("biBitCount",    wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage",   wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed",     wintypes.DWORD),
        ("biClrImportant",wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]

# ---------- prototypes (64-bit safe) ----------
DefWindowProcW = user32.DefWindowProcW
DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
DefWindowProcW.restype  = wintypes.LPARAM

RegisterClassExW = user32.RegisterClassExW
RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
RegisterClassExW.restype  = wintypes.ATOM

CreateWindowExW = user32.CreateWindowExW
CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, HMENU, HINSTANCE, LPVOID
]
CreateWindowExW.restype = wintypes.HWND

BeginPaint = user32.BeginPaint
BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
BeginPaint.restype  = HDC

EndPaint = user32.EndPaint
EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
EndPaint.restype  = wintypes.BOOL

ShowWindow = user32.ShowWindow
ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
ShowWindow.restype  = wintypes.BOOL

UpdateWindow = user32.UpdateWindow
UpdateWindow.argtypes = [wintypes.HWND]
UpdateWindow.restype  = wintypes.BOOL

SetWindowPos = user32.SetWindowPos
SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.UINT]
SetWindowPos.restype  = wintypes.BOOL

GetMessageW = user32.GetMessageW
GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
GetMessageW.restype  = ctypes.c_int

TranslateMessage = user32.TranslateMessage
TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
TranslateMessage.restype  = wintypes.BOOL

DispatchMessageW = user32.DispatchMessageW
DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
DispatchMessageW.restype  = wintypes.LPARAM

PostMessageW = user32.PostMessageW
PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
PostMessageW.restype  = wintypes.BOOL

SetLayeredWindowAttributes = user32.SetLayeredWindowAttributes
SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
SetLayeredWindowAttributes.restype  = wintypes.BOOL

CreateDIBSection = gdi32.CreateDIBSection
CreateDIBSection.argtypes = [HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                             ctypes.POINTER(LPVOID), HANDLE, wintypes.DWORD]
CreateDIBSection.restype  = HBITMAP

CreateCompatibleDC = gdi32.CreateCompatibleDC
CreateCompatibleDC.argtypes = [HDC]
CreateCompatibleDC.restype  = HDC

SelectObject = gdi32.SelectObject
SelectObject.argtypes = [HDC, HANDLE]
SelectObject.restype  = HANDLE

DeleteDC = gdi32.DeleteDC
DeleteDC.argtypes = [HDC]
DeleteDC.restype  = wintypes.BOOL

DeleteObject = gdi32.DeleteObject
DeleteObject.argtypes = [HANDLE]
DeleteObject.restype  = wintypes.BOOL

BitBlt = gdi32.BitBlt
BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
BitBlt.restype  = wintypes.BOOL

SetBkMode = gdi32.SetBkMode
SetBkMode.argtypes = [HDC, ctypes.c_int]
SetBkMode.restype  = ctypes.c_int

SetTextColor = gdi32.SetTextColor
SetTextColor.argtypes = [HDC, wintypes.COLORREF]
SetTextColor.restype  = wintypes.COLORREF

CreateFontW = gdi32.CreateFontW
CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                        wintypes.DWORD, wintypes.LPCWSTR]
CreateFontW.restype  = HANDLE

InvalidateRect = user32.InvalidateRect
InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
InvalidateRect.restype  = wintypes.BOOL

DrawTextW = user32.DrawTextW
DrawTextW.argtypes = [HDC, wintypes.LPCWSTR, ctypes.c_int,
                      ctypes.POINTER(wintypes.RECT), wintypes.UINT]
DrawTextW.restype  = ctypes.c_int

GetSystemMetrics = user32.GetSystemMetrics
GetSystemMetrics.argtypes = [ctypes.c_int]
GetSystemMetrics.restype  = ctypes.c_int

SetTimer = user32.SetTimer
UINT_PTR = getattr(wintypes, "UINT_PTR", wintypes.UINT)
SetTimer.argtypes = [wintypes.HWND, UINT_PTR, wintypes.UINT, LPVOID]
SetTimer.restype  = UINT_PTR
KillTimer = user32.KillTimer
KillTimer.argtypes = [wintypes.HWND, UINT_PTR]
KillTimer.restype  = wintypes.BOOL

# ---------- globals (window thread) ----------
_g_thread = None
_g_ready_evt = threading.Event()
_g_stop_evt  = threading.Event()
_g_hwnd = wintypes.HWND(0)
_g_wndproc = None

# virtual screen geometry
_g_scr_left = 0
_g_scr_top  = 0
_g_scr_w    = 1
_g_scr_h    = 1

# surface for entire virtual screen
_g_hbm   = HBITMAP(0)
_g_memDC = HDC(0)
_g_pBits = LPVOID(0)

# items state (read by window thread)
_state_lock = threading.Lock()
_items: list[dict] = []   # each: {x,y,text,color,font,font_px}
_max_items = 20
_keep_ms   = 300

# ---------- helpers ----------
def _hex_to_BGR0(hex_color: str) -> int:
    s = hex_color.lstrip("#")
    if len(s) != 6:
        s = "FFFFFF"
    r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
    return (b << 16) | (g << 8) | r

def _free_surface():
    global _g_hbm, _g_memDC
    if _g_memDC:
        DeleteDC(_g_memDC); _g_memDC = HDC(0)
    if _g_hbm:
        DeleteObject(_g_hbm); _g_hbm = HBITMAP(0)

def _create_screen_surface():
    """Create 32bpp top-down DIB for whole virtual screen."""
    global _g_hbm, _g_memDC, _g_pBits
    _free_surface()
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = _g_scr_w
    bmi.bmiHeader.biHeight = -_g_scr_h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    hbm = CreateDIBSection(HDC(0), ctypes.byref(bmi), DIB_RGB_COLORS,
                           ctypes.byref(_g_pBits), HANDLE(0), 0)
    if not hbm:
        raise OSError("CreateDIBSection failed")
    dc = CreateCompatibleDC(HDC(0))
    SelectObject(dc, hbm)
    _g_hbm, _g_memDC = hbm, dc
    _clear_surface()

def _clear_surface():
    """Fill surface with colorkey."""
    if not _g_pBits:
        return
    arr_t = ctypes.c_uint32 * (_g_scr_w * _g_scr_h)
    pixels = arr_t.from_address(ctypes.cast(_g_pBits, ctypes.c_void_p).value)
    # bulk fill
    for i in range(_g_scr_w * _g_scr_h):
        pixels[i] = MAGENTA_BGR0

def _draw_items():
    """Redraw all items to the surface."""
    _clear_surface()
    if not _items:
        return
    for it in _items:
        text = it["text"]
        font = it["font"]
        fpx  = it["font_px"]
        color= it["color"]
        x    = it["x"] - _g_scr_left
        y    = it["y"] - _g_scr_top
        if x >= _g_scr_w or y >= _g_scr_h:
            continue
        # HFONT
        hfont = CreateFontW(-int(fpx), 0, 0, 0, FW_NORMAL, 0, 0, 0,
                            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                            ANTIALIASED_QUALITY, DEFAULT_PITCH, font)
        old = SelectObject(_g_memDC, hfont)
        SetBkMode(_g_memDC, TRANSPARENT)
        SetTextColor(_g_memDC, color)
        # Measure
        rc_calc = wintypes.RECT(0, 0, 10000, 10000)
        DrawTextW(_g_memDC, text, len(text), ctypes.byref(rc_calc),
                  DT_LEFT | DT_TOP | DT_NOPREFIX | DT_CALCRECT)
        w = rc_calc.right - rc_calc.left
        h = rc_calc.bottom - rc_calc.top
        # Target rect (clamped)
        left   = max(0, x + PADDING)
        top    = max(0, y + PADDING)
        right  = min(_g_scr_w,  left + w)
        bottom = min(_g_scr_h,  top  + h)
        if right > left and bottom > top:
            rc = wintypes.RECT(left, top, right, bottom)
            DrawTextW(_g_memDC, text, len(text), ctypes.byref(rc),
                      DT_LEFT | DT_TOP | DT_NOPREFIX)
        SelectObject(_g_memDC, old)
        DeleteObject(hfont)

# ---------- window proc ----------
def _make_wndproc():
    def wndproc(hWnd, msg, wParam, lParam):
        try:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            elif msg == WM_ERASEBKGND:
                return 1
            elif msg == WM_PAINT:
                if _g_memDC and _g_hbm:
                    ps = PAINTSTRUCT()
                    hdc = BeginPaint(hWnd, ctypes.byref(ps))
                    BitBlt(hdc, 0, 0, _g_scr_w, _g_scr_h, _g_memDC, 0, 0, SRCCOPY)
                    EndPaint(hWnd, ctypes.byref(ps))
                    return 0
            elif msg == WM_APP_REBUILD:
                with _state_lock:
                    _draw_items()
                # keep topmost & show (no activate)
                SetWindowPos(hWnd, HWND_TOPMOST, _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
                InvalidateRect(hWnd, None, False)
                UpdateWindow(hWnd)

                UpdateWindow(hWnd)
                return 0
            elif msg == WM_APP_HIDE:
                ShowWindow(hWnd, SW_HIDE)
                return 0
            elif msg == WM_APP_SETKEEP:
                KillTimer(hWnd, KEEP_TIMER_ID)
                if int(wParam) > 0:
                    SetTimer(hWnd, UINT_PTR(KEEP_TIMER_ID), int(wParam), LPVOID(0))
                return 0
            elif msg == WM_APP_CLEAR:
                with _state_lock:
                    _items.clear()
                    _draw_items()
                SetWindowPos(hWnd, HWND_TOPMOST, _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
                InvalidateRect(hWnd, None, False)
                UpdateWindow(hWnd)

                UpdateWindow(hWnd)
                return 0
            elif msg == WM_TIMER and int(wParam) == KEEP_TIMER_ID:
                SetWindowPos(hWnd, HWND_TOPMOST, _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h,
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
                return 0
            elif msg == WM_DESTROY:
                KillTimer(hWnd, KEEP_TIMER_ID)
                _free_surface()
                return 0
            return DefWindowProcW(hWnd, msg, wParam, lParam)
        except Exception:
            return DefWindowProcW(hWnd, msg, wParam, lParam)
    return WNDPROC(wndproc)

# ---------- window thread ----------
def _overlay_thread():
    global _g_hwnd, _g_wndproc, _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h
    # virtual screen geometry
    _g_scr_left = GetSystemMetrics(SM_XVIRTUALSCREEN)
    _g_scr_top  = GetSystemMetrics(SM_YVIRTUALSCREEN)
    _g_scr_w    = max(1, GetSystemMetrics(SM_CXVIRTUALSCREEN))
    _g_scr_h    = max(1, GetSystemMetrics(SM_CYVIRTUALSCREEN))

    _g_wndproc = _make_wndproc()
    hInstance = kernel32.GetModuleHandleW(None)
    className = "OverlayTextLibV3"

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _g_wndproc
    wc.hInstance = hInstance
    wc.hIcon = HICON(0)
    wc.hCursor = HCURSOR(0)
    wc.hbrBackground = HBRUSH(0)
    wc.lpszClassName = className
    if not RegisterClassExW(ctypes.byref(wc)):
        _g_ready_evt.set(); return

    exstyle = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    style   = WS_POPUP
    hWnd = CreateWindowExW(exstyle, className, "overlay_text_lib_v3", style,
                           _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h,
                           wintypes.HWND(0), HMENU(0), hInstance, LPVOID(0))
    if not hWnd:
        _g_ready_evt.set(); return

    SetLayeredWindowAttributes(hWnd, MAGENTA_BGR0, 0, LWA_COLORKEY)
    _g_hwnd = hWnd
    _create_screen_surface()

    # show & keep-on-top timer
    SetWindowPos(hWnd, HWND_TOPMOST, _g_scr_left, _g_scr_top, _g_scr_w, _g_scr_h,
                 SWP_NOACTIVATE | SWP_SHOWWINDOW)
    if _keep_ms and _keep_ms > 0:
        SetTimer(hWnd, UINT_PTR(KEEP_TIMER_ID), int(_keep_ms), LPVOID(0))

    _g_ready_evt.set()

    # message loop
    msg = wintypes.MSG()
    while not _g_stop_evt.is_set():
        r = GetMessageW(ctypes.byref(msg), wintypes.HWND(0), 0, 0)
        if r == 0:
            break
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))

# ---------- public API ----------
def init_overlay(keep_on_top_ms: int = 300, max_items: int = 20, timeout_sec: float = 3.0) -> bool:
    """Start overlay thread. keep_on_top_ms=0 disables periodic raise; max_items=capacity of queue."""
    global _g_thread, _keep_ms, _max_items
    _keep_ms   = int(keep_on_top_ms)
    _max_items = max(1, int(max_items))
    if _g_thread and _g_thread.is_alive() and _g_hwnd:
        set_keep_on_top_interval(_keep_ms)
        set_max_items(_max_items)
        return True
    _g_stop_evt.clear(); _g_ready_evt.clear()
    _g_thread = threading.Thread(target=_overlay_thread, name="overlay_text_v3", daemon=True)
    _g_thread.start()
    ok = _g_ready_evt.wait(timeout=timeout_sec)
    atexit.register(close_overlay)
    return ok and bool(_g_hwnd)

def paint_with_coords(x: int, y: int, text: str,
                      color_hex: str = DEFAULT_COLOR_HEX,
                      font_name: str = DEFAULT_FONT_NAME,
                      font_px: int = DEFAULT_FONT_PX) -> bool:
    """Append a text item at screen coords (x,y). If capacity exceeded, drop oldest."""
    if not init_overlay(_keep_ms, _max_items):
        return False
    if not text:
        text = " "
    item = {
        "x": int(x), "y": int(y),
        "text": str(text),
        "color": _hex_to_BGR0(color_hex),
        "font": str(font_name),
        "font_px": int(font_px),
    }
    with _state_lock:
        _items.append(item)
        if len(_items) > _max_items:
            # drop oldest
            del _items[0: len(_items) - _max_items]
    PostMessageW(_g_hwnd, WM_APP_REBUILD, 0, 0)
    return True

def paint_wtih_coords(x, y, text, color_hex=DEFAULT_COLOR_HEX, font_name=DEFAULT_FONT_NAME, font_px=DEFAULT_FONT_PX):
    return paint_with_coords(x, y, text, color_hex, font_name, font_px)

def set_keep_on_top_interval(ms: int) -> bool:
    """Change keep-on-top interval (ms). 0 disables."""
    global _keep_ms
    _keep_ms = int(ms)
    if not _g_hwnd:
        return False
    PostMessageW(_g_hwnd, WM_APP_SETKEEP, _keep_ms, 0)
    return True

def set_max_items(n: int) -> None:
    """Set new capacity; trims current list if necessary."""
    global _max_items
    n = max(1, int(n))
    _max_items = n
    with _state_lock:
        if len(_items) > _max_items:
            del _items[0: len(_items) - _max_items]
    if _g_hwnd:
        PostMessageW(_g_hwnd, WM_APP_REBUILD, 0, 0)

def clear_overlay() -> bool:
    """Remove all items and clear overlay."""
    if not _g_hwnd:
        return False
    with _state_lock:
        _items.clear()
    PostMessageW(_g_hwnd, WM_APP_CLEAR, 0, 0)
    return True

def hide_overlay() -> bool:
    """Hide window; next paint_* will show again."""
    if not _g_hwnd:
        return False
    PostMessageW(_g_hwnd, WM_APP_HIDE, 0, 0)
    return True

def close_overlay() -> None:
    """Stop thread and free resources."""
    global _g_thread, _g_hwnd
    if _g_hwnd:
        PostMessageW(_g_hwnd, WM_DESTROY, 0, 0)
        _g_hwnd = wintypes.HWND(0)
    if _g_thread:
        _g_stop_evt.set()
        _g_thread = None
