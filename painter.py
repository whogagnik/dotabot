# overlay_text_lib.py
# Windows 10/11, Python 3.x, чистый WinAPI (ctypes)
# API:
#   init_overlay()
#   paint_with_coords(x, y, text, color_hex="#ffffff", font_name="Segoe UI", font_px=24)
#   paint_wtih_coords(... )  # алиас
#   hide_overlay()
#   close_overlay()

import ctypes, threading, sys, atexit
from ctypes import wintypes

# ========= Проверка ОС =========
if sys.platform != "win32":
    raise OSError("overlay_text_lib: поддерживается только Windows")

# ========= Настройки по умолчанию =========
DEFAULT_FONT_NAME = "Segoe UI"
DEFAULT_FONT_PX   = 24
DEFAULT_COLOR_HEX = "#ffffff"
PADDING           = 8
MAGENTA_BGR0      = 0x00FF00FF  # фон, который станет прозрачным (colorkey)

# ========= Шимы типов =========
HANDLE    = wintypes.HANDLE
HINSTANCE = getattr(wintypes, "HINSTANCE", HANDLE)
HCURSOR   = getattr(wintypes, "HCURSOR",   HANDLE)
HICON     = getattr(wintypes, "HICON",     HANDLE)
HBRUSH    = getattr(wintypes, "HBRUSH",    HANDLE)
HBITMAP   = getattr(wintypes, "HBITMAP",   HANDLE)
HDC       = getattr(wintypes, "HDC",       HANDLE)
HMENU     = getattr(wintypes, "HMENU",     HANDLE)
LPVOID    = ctypes.c_void_p
try:
    LRESULT = wintypes.LRESULT
except AttributeError:
    LRESULT = wintypes.LPARAM

# ========= WinAPI =========
user32 = ctypes.windll.user32
gdi32  = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# DPI-aware
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor v2
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# ---- Константы ----
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
WM_HOTKEY       = 0x0312
HTTRANSPARENT   = -1
WM_APP          = 0x8000
WM_APP_UPDATE   = WM_APP + 1
WM_APP_HIDE     = WM_APP + 2

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
DT_WORDBREAK = 0x0010
DT_CALCRECT = 0x0400

# ---- Структуры ----
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
        ("biHeight",      wintypes.LONG),  # отрицательная -> top-down
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

# ---- Прототипы (64-bit safe) ----
DefWindowProcW = user32.DefWindowProcW
DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
DefWindowProcW.restype  = LRESULT

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
DispatchMessageW.restype  = LRESULT

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
CreateFontW.restype  = HANDLE  # HFONT

DrawTextW = user32.DrawTextW
DrawTextW.argtypes = [HDC, wintypes.LPCWSTR, ctypes.c_int,
                      ctypes.POINTER(wintypes.RECT), wintypes.UINT]
DrawTextW.restype  = ctypes.c_int

# ========= Глобальные (управляет поток-окно) =========
_g_thread = None
_g_ready_evt = threading.Event()
_g_stop_evt  = threading.Event()
_g_hwnd = wintypes.HWND(0)

# Буфер состояния (читается в оконном потоке при WM_APP_UPDATE)
_state_lock = threading.Lock()
_state_text = ""
_state_color_bgr0 = 0x00FFFFFF
_state_font_name = DEFAULT_FONT_NAME
_state_font_px = DEFAULT_FONT_PX
_state_size = (1, 1)
_state_pos = (0, 0)

# Графика в оконном потоке
_g_hbm = HBITMAP(0)
_g_memDC = HDC(0)
_g_pBits = LPVOID(0)
_g_width = 1
_g_height = 1
_g_wndproc = None  # держим ссылку на callback

# ========= Утилиты =========
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

def _create_surface(w, h):
    """Создаёт top-down 32bpp DIB + memDC, заливает магентой."""
    global _g_hbm, _g_memDC, _g_pBits, _g_width, _g_height
    _free_surface()
    _g_width, _g_height = w, h

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    hbm = CreateDIBSection(HDC(0), ctypes.byref(bmi), DIB_RGB_COLORS,
                           ctypes.byref(_g_pBits), HANDLE(0), 0)
    if not hbm:
        raise OSError("CreateDIBSection failed")

    dc = CreateCompatibleDC(HDC(0))
    SelectObject(dc, hbm)

    # Заливаем фон магентой
    arr_t = ctypes.c_uint32 * (w * h)
    pixels = arr_t.from_address(ctypes.cast(_g_pBits, ctypes.c_void_p).value)
    for i in range(w * h):
        pixels[i] = MAGENTA_BGR0

    _g_hbm, _g_memDC = hbm, dc

def _measure_text(text: str, font_name: str, font_px: int) -> tuple[int,int,HANDLE]:
    """Возвращает (w,h,hfont) для текста/шрифта."""
    hfont = CreateFontW(-font_px, 0, 0, 0, FW_NORMAL, 0, 0, 0,
                        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                        ANTIALIASED_QUALITY, DEFAULT_PITCH, font_name)
    tmpDC = CreateCompatibleDC(HDC(0))
    old = SelectObject(tmpDC, hfont)
    SetBkMode(tmpDC, TRANSPARENT)

    rc = wintypes.RECT(0, 0, 10000, 10000)
    DrawTextW(tmpDC, text, len(text), ctypes.byref(rc),
              DT_LEFT | DT_TOP | DT_NOPREFIX | DT_WORDBREAK | DT_CALCRECT)
    w = rc.right - rc.left
    h = rc.bottom - rc.top

    SelectObject(tmpDC, old)
    DeleteDC(tmpDC)
    return w, h, hfont

def _draw_text(text: str, color_bgr0: int, hfont: HANDLE, w: int, h: int):
    old = SelectObject(_g_memDC, hfont)
    SetBkMode(_g_memDC, TRANSPARENT)
    SetTextColor(_g_memDC, color_bgr0)
    rc = wintypes.RECT(PADDING, PADDING, w - PADDING, h - PADDING)
    DrawTextW(_g_memDC, text, len(text), ctypes.byref(rc),
              DT_LEFT | DT_TOP | DT_NOPREFIX | DT_WORDBREAK)
    SelectObject(_g_memDC, old)
    DeleteObject(hfont)

# ========= Оконная процедура (только в оконном потоке) =========
def _make_wndproc():
    def wndproc(hWnd, msg, wParam, lParam):
        try:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT  # прокликивается насквозь
            elif msg == WM_ERASEBKGND:
                return 1
            elif msg == WM_PAINT:
                if _g_memDC and _g_hbm:
                    ps = PAINTSTRUCT()
                    hdc = BeginPaint(hWnd, ctypes.byref(ps))
                    BitBlt(hdc, 0, 0, _g_width, _g_height, _g_memDC, 0, 0, SRCCOPY)
                    EndPaint(hWnd, ctypes.byref(ps))
                    return 0
            elif msg == WM_APP_UPDATE:
                # читаем состояние и рисуем
                with _state_lock:
                    text = _state_text
                    color = _state_color_bgr0
                    font_name = _state_font_name
                    font_px = _state_font_px
                    x, y = _state_pos
                tw, th, hfont = _measure_text(text, font_name, font_px)
                w = max(1, tw + PADDING * 2)
                h = max(1, th + PADDING * 2)
                _create_surface(w, h)
                _draw_text(text, color, hfont, w, h)
                SetWindowPos(hWnd, HWND_TOPMOST, int(x), int(y), int(w), int(h),
                             SWP_NOACTIVATE | SWP_SHOWWINDOW)
                UpdateWindow(hWnd)
                return 0
            elif msg == WM_APP_HIDE:
                ShowWindow(hWnd, SW_HIDE)
                return 0
            elif msg == WM_DESTROY:
                _free_surface()
                PostQuitMessage(0)
                return 0
            return DefWindowProcW(hWnd, msg, wParam, lParam)
        except Exception:
            return DefWindowProcW(hWnd, msg, wParam, lParam)
    return WNDPROC(wndproc)

# ========= Оконный поток =========
def _overlay_thread():
    global _g_hwnd, _g_wndproc
    # Регистрируем класс
    hInstance = kernel32.GetModuleHandleW(None)
    className = "OverlayTextLibClass"

    _g_wndproc = _make_wndproc()  # держим ссылку, чтобы GC не собрал
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _g_wndproc
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hInstance
    wc.hIcon = HICON(0)
    wc.hCursor = HCURSOR(0)
    wc.hbrBackground = HBRUSH(0)
    wc.lpszMenuName = None
    wc.lpszClassName = className
    wc.hIconSm = HICON(0)
    if not RegisterClassExW(ctypes.byref(wc)):
        _g_ready_evt.set()
        return

    exstyle = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    style   = WS_POPUP
    hWnd = CreateWindowExW(
        exstyle, className, "overlay_text_lib",
        style, 0, 0, 1, 1,
        wintypes.HWND(0), HMENU(0), hInstance, LPVOID(0)
    )
    if not hWnd:
        _g_ready_evt.set()
        return

    SetLayeredWindowAttributes(hWnd, MAGENTA_BGR0, 0, LWA_COLORKEY)
    ShowWindow(hWnd, SW_SHOWNOACTIVATE)
    UpdateWindow(hWnd)

    _g_hwnd = hWnd
    _g_ready_evt.set()

    # Цикл сообщений
    msg = wintypes.MSG()
    while not _g_stop_evt.is_set():
        r = GetMessageW(ctypes.byref(msg), wintypes.HWND(0), 0, 0)
        if r == 0:
            break
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))

# ========= Публичный API =========
def init_overlay(timeout_sec: float = 3.0) -> bool:
    """Инициализирует фоновой оконный поток (без фокуса, topmost, click-through)."""
    global _g_thread
    if _g_thread and _g_thread.is_alive() and _g_hwnd:
        return True
    _g_stop_evt.clear()
    _g_ready_evt.clear()
    _g_thread = threading.Thread(target=_overlay_thread, name="overlay_text_thread", daemon=True)
    _g_thread.start()
    ok = _g_ready_evt.wait(timeout=timeout_sec)
    atexit.register(close_overlay)
    return ok and bool(_g_hwnd)

def paint_with_coords(x: int, y: int, text: str,
                      color_hex: str = DEFAULT_COLOR_HEX,
                      font_name: str = DEFAULT_FONT_NAME,
                      font_px: int = DEFAULT_FONT_PX) -> bool:
    """
    Рисует текст поверх всех окон в экранных координатах (x,y).
    Инициализация будет выполнена автоматически при первом вызове.
    """
    if not init_overlay():
        return False
    if not text:
        text = " "
    with _state_lock:
        global _state_text, _state_color_bgr0, _state_font_name, _state_font_px, _state_pos
        _state_text = str(text)
        _state_color_bgr0 = _hex_to_BGR0(color_hex)
        _state_font_name = str(font_name)
        _state_font_px = int(font_px)
        _state_pos = (int(x), int(y))
    PostMessageW(_g_hwnd, WM_APP_UPDATE, 0, 0)
    return True

# алиас на опечатку
def paint_wtih_coords(x, y, text, color_hex=DEFAULT_COLOR_HEX, font_name=DEFAULT_FONT_NAME, font_px=DEFAULT_FONT_PX):
    return paint_with_coords(x, y, text, color_hex, font_name, font_px)

def hide_overlay() -> bool:
    """Скрывает окно оверлея (его можно снова показать вызовом paint_*)."""
    if not _g_hwnd:
        return False
    PostMessageW(_g_hwnd, WM_APP_HIDE, 0, 0)
    return True

def close_overlay() -> None:
    """Останавливает оконный поток и освобождает ресурсы."""
    global _g_thread, _g_hwnd
    if _g_hwnd:
        # отправим стандартное закрытие
        PostMessageW(_g_hwnd, WM_DESTROY, 0, 0)
        _g_hwnd = wintypes.HWND(0)
    if _g_thread:
        _g_stop_evt.set()
        _g_thread = None
