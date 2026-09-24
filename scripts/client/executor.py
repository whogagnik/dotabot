# scripts/client/executor.py
from __future__ import annotations

import base64
import ctypes
import io
import subprocess
import time
from ctypes import wintypes
from typing import Any, Optional

import psutil
import pyautogui as p
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image


EXECUTOR_VERSION = "executor_login_hwnd_recovery_v33"
DOTA_PRIORITY_CLASS = psutil.BELOW_NORMAL_PRIORITY_CLASS
# Each Dota client gets the same lowest Windows scheduler weight.  Unlike a
# hard rate cap, a weight never freezes a game's threads at interval boundary.
DOTA_CPU_SCHEDULER_WEIGHT = 1

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED = 0x2
JobObjectCpuRateControlInformation = 15
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("ControlFlags", wintypes.DWORD),
        ("Value", wintypes.DWORD),
    )


kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL


ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    )


INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


class HostCommandType:
    LAUNCH_PROCESS = "launch_process"
    KILL_PROCESS_TREE = "kill_process_tree"
    FIND_LOGIN_WINDOW = "find_login_window"
    FIND_DOTA_WINDOW = "find_dota_window"
    FOCUS_WINDOW = "focus_window"
    MOVE_WINDOW = "move_window"
    MOUSE_CLICK = "mouse_click"
    MOUSE_MOVE = "mouse_move"
    KEY_PRESS = "key_press"
    KEY_EVENT = "key_event"
    WRITE_TEXT = "write_text"
    HOTKEY = "hotkey"
    SLEEP = "sleep"
    CAPTURE_FRAME = "capture_frame"
    CAPTURE_DESKTOP = "capture_desktop"
    LOG = "log"
    DISMISS_STEAM_POPUPS = "dismiss_steam_popups"
    CLOSE_STEAM_WINDOWS = "close_steam_windows"


def _desktop_bounds() -> dict[str, int]:
    left = win32api.GetSystemMetrics(76)
    top = win32api.GetSystemMetrics(77)
    width = win32api.GetSystemMetrics(78)
    height = win32api.GetSystemMetrics(79)

    return {
        "left": int(left),
        "top": int(top),
        "right": int(left + width),
        "bottom": int(top + height),
        "width": int(width),
        "height": int(height),
    }


def _window_pid(hwnd: int) -> Optional[int]:
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return None


def _window_info(hwnd: int) -> dict[str, Any]:
    wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)

    try:
        title = (win32gui.GetWindowText(hwnd) or "").strip()
    except Exception:
        title = ""

    try:
        class_name = (win32gui.GetClassName(hwnd) or "").strip()
    except Exception:
        class_name = ""

    try:
        pid = _window_pid(hwnd)
    except Exception:
        pid = None

    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        csx, csy = win32gui.ClientToScreen(hwnd, (0, 0))
        client_rect = {
            "x": int(csx),
            "y": int(csy),
            "width": int(cr - cl),
            "height": int(cb - ct),
        }
    except Exception:
        client_rect = {
            "x": int(wl),
            "y": int(wt),
            "width": int(wr - wl),
            "height": int(wb - wt),
        }

    return {
        "hwnd": int(hwnd),
        "pid": None if pid is None else int(pid),
        "title": title,
        "class_name": class_name,
        "window_rect": {
            "x": int(wl),
            "y": int(wt),
            "width": int(wr - wl),
            "height": int(wb - wt),
            "left": int(wl),
            "top": int(wt),
            "right": int(wr),
            "bottom": int(wb),
        },
        "client_rect": client_rect,
        "desktop": _desktop_bounds(),
    }


def _is_real_dota_window(hwnd: int, pid: Optional[int] = None) -> bool:
    try:
        hwnd_i = int(hwnd)

        if not win32gui.IsWindow(hwnd_i):
            return False
        if not win32gui.IsWindowVisible(hwnd_i):
            return False

        owner_pid = _window_pid(hwnd_i)
        if owner_pid is None:
            return False

        if pid is not None and int(owner_pid) != int(pid):
            return False

        try:
            proc = psutil.Process(int(owner_pid))
            pname = (proc.name() or "").lower()
        except Exception:
            return False

        if pname != "dota2.exe":
            return False

        title = (win32gui.GetWindowText(hwnd_i) or "").strip().lower()
        class_name = (win32gui.GetClassName(hwnd_i) or "").strip().lower()

        if "avast" in title or "sandbox" in title:
            return False
        if "avast" in class_name or "sandbox" in class_name:
            return False

        if "dota" not in title and "дота" not in title:
            return False

        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd_i)
        ww = int(wr - wl)
        wh = int(wb - wt)

        try:
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd_i)
            cw = int(cr - cl)
            ch = int(cb - ct)
        except Exception:
            cw = ww
            ch = wh

        if ww < 500 or wh < 350:
            return False
        if cw < 500 or ch < 350:
            return False

        desktop = _desktop_bounds()
        if ww > int(desktop["width"] * 0.75) or wh > int(desktop["height"] * 0.75):
            return False

        return True
    except Exception:
        return False


def _find_main_window_for_pid(pid: int) -> Optional[int]:
    candidates: list[tuple[int, int, str, str]] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return

        if _window_pid(hwnd) != int(pid):
            return

        if not _is_real_dota_window(int(hwnd), int(pid)):
            return

        try:
            title = (win32gui.GetWindowText(hwnd) or "").strip()
        except Exception:
            title = ""

        try:
            class_name = (win32gui.GetClassName(hwnd) or "").strip()
        except Exception:
            class_name = ""

        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            area = int(r - l) * int(b - t)
        except Exception:
            area = 0

        candidates.append((area, int(hwnd), title, class_name))

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return int(candidates[0][1])


def _login_window_title_match(hwnd: int) -> bool:
    try:
        title = (win32gui.GetWindowText(hwnd) or "").lower()
    except Exception:
        return False

    for marker in ("войти в стим", "войти в steam", "вход в steam", "sign in to steam"):
        if marker in title:
            return True

    return False


def _any_login_hwnd_for_pids(pids: set[int]) -> Optional[int]:
    found = None

    def cb(hwnd, _):
        nonlocal found
        if found is not None or not win32gui.IsWindowVisible(hwnd):
            return
        if not _login_window_title_match(hwnd):
            return

        pid = _window_pid(hwnd)
        if pid and pid in pids:
            found = hwnd

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None

    return found


def _any_visible_steam_login_hwnd(exclude_hwnds: set[int] | None = None) -> Optional[int]:
    """Find the visible Steam sign-in window when Steam detached from launch PID."""
    found = None

    def cb(hwnd, _):
        nonlocal found
        if found is not None or int(hwnd) in (exclude_hwnds or set()) or not win32gui.IsWindowVisible(hwnd):
            return
        if not _login_window_title_match(hwnd):
            return
        try:
            pid = _window_pid(hwnd)
            process_name = (psutil.Process(pid).name() or "").lower() if pid else ""
        except (psutil.Error, OSError):
            return
        if process_name in {"steam.exe", "steamwebhelper.exe"}:
            found = int(hwnd)

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None
    return found


def _force_foreground(
    hwnd: int, *, settle_ms: int = 60, activation_wait_ms: int = 50,
    retry_wait_ms: int = 100, fast_if_visible: bool = False,
) -> None:
    """Bring an input window to the foreground and its thread to keyboard focus."""
    last_error: Optional[Exception] = None
    for attempt in range(3):
        attached_threads: list[int] = []
        try:
            iconic = win32gui.IsIconic(hwnd)
            if iconic:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if not (fast_if_visible and attempt == 0 and not iconic):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )

            fore = win32gui.GetForegroundWindow()
            ftid = win32process.GetWindowThreadProcessId(fore)[0] if fore else 0
            target_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
            ctid = win32api.GetCurrentThreadId()
            for thread_id in dict.fromkeys((ftid, target_tid)):
                if thread_id and thread_id != ctid and user32.AttachThreadInput(ctid, thread_id, True):
                    attached_threads.append(thread_id)
            try:
                # SwitchToThisWindow bypasses cases where Steam keeps its
                # web-helper window visible but declines SetForegroundWindow.
                user32.SwitchToThisWindow(hwnd, True)
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
            finally:
                for thread_id in reversed(attached_threads):
                    user32.AttachThreadInput(ctid, thread_id, False)

            if activation_wait_ms > 0:
                time.sleep(activation_wait_ms / 1000.0)
            _require_foreground(hwnd)
            if settle_ms > 0:
                time.sleep(max(0, int(settle_ms)) / 1000.0)
            return
        except Exception as error:
            last_error = error
            if attempt < 2 and retry_wait_ms > 0:
                time.sleep(retry_wait_ms / 1000.0)

    raise RuntimeError(f"could not foreground hwnd={hwnd} after 3 attempts: {last_error}")


def _require_foreground(hwnd: int) -> None:
    actual = int(win32gui.GetForegroundWindow())
    if actual != int(hwnd):
        raise RuntimeError(f"foreground mismatch: expected hwnd={hwnd}, actual hwnd={actual}")


def _verify_click_target(hwnd: int, sx: int, sy: int) -> None:
    _require_foreground(hwnd)
    actual = tuple(win32api.GetCursorPos())
    if actual != (sx, sy):
        raise RuntimeError(f"cursor mismatch for hwnd={hwnd}: expected={(sx, sy)}, actual={actual}")
    hit = win32gui.WindowFromPoint((sx, sy))
    root = win32gui.GetAncestor(hit, win32con.GA_ROOT) if hit else 0
    if int(root) != int(hwnd):
        raise RuntimeError(f"click target mismatch: expected hwnd={hwnd}, actual hwnd={root}")


def _absolute_mouse_point(sx: int, sy: int) -> tuple[int, int]:
    # SendInput absolute coordinates cover the entire virtual desktop,
    # including monitors left/above the primary monitor.
    left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0 or not (left <= sx < left + width and top <= sy < top + height):
        raise ValueError(f"mouse point outside virtual desktop: ({sx},{sy})")
    # Aim at the centre of the destination pixel, not its left/top boundary.
    return (min(65535, int((sx - left + 0.5) * 65536 / width)),
            min(65535, int((sy - top + 0.5) * 65536 / height)))


def _require_live_window(hwnd: Optional[int], purpose: str) -> int:
    if hwnd is None:
        raise ValueError(f"{purpose} requires hwnd or account_login mapping")

    hwnd_i = int(hwnd)
    try:
        if not win32gui.IsWindow(hwnd_i):
            raise ValueError(f"{purpose} hwnd is no longer valid: {hwnd_i}")
        if not win32gui.IsWindowVisible(hwnd_i):
            raise ValueError(f"{purpose} hwnd is not visible: {hwnd_i}")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"{purpose} hwnd validation failed: {hwnd_i}: {e}") from e

    return hwnd_i


def _switch_keyboard_layout_en() -> None:
    try:
        hkl = user32.LoadKeyboardLayoutW("00000409", 1)
        hwnd = win32gui.GetForegroundWindow()
        if hwnd and hkl:
            win32api.SendMessage(
                hwnd,
                win32con.WM_INPUTLANGCHANGEREQUEST,
                0,
                hkl,
            )
        time.sleep(0.05)
    except Exception:
        pass


def _vk_scan_code(vk_code: int) -> int:
    try:
        return int(user32.MapVirtualKeyW(int(vk_code), 0))
    except Exception:
        return 0


def _key_down(vk_code: int) -> None:
    vk_code = int(vk_code)
    win32api.keybd_event(vk_code, _vk_scan_code(vk_code), 0, 0)


def _key_up(vk_code: int) -> None:
    vk_code = int(vk_code)
    win32api.keybd_event(vk_code, _vk_scan_code(vk_code), win32con.KEYEVENTF_KEYUP, 0)


def _tap_vk(vk_code: int, hold_ms: int = 25, *, escape_method: str = "scan_code") -> None:
    if int(vk_code) == win32con.VK_ESCAPE:
        if escape_method not in {"scan_code", "virtual_key"}:
            raise ValueError(f"unsupported Escape input method: {escape_method}")
        def send_escape(up: bool) -> None:
            flags = (0x0008 if escape_method == "scan_code" else 0) | (win32con.KEYEVENTF_KEYUP if up else 0)
            events = (_INPUT * 1)(_INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(ki=_KEYBDINPUT(
                    0 if escape_method == "scan_code" else win32con.VK_ESCAPE,
                    0x01, flags, 0, 0,
                )),
            ))
            ctypes.set_last_error(0)
            if user32.SendInput(1, events, ctypes.sizeof(_INPUT)) != 1:
                raise ctypes.WinError(ctypes.get_last_error())

        send_escape(False)
        try:
            time.sleep(max(0, int(hold_ms)) / 1000.0)
        finally:
            send_escape(True)
        return
    _key_down(vk_code)
    time.sleep(max(0, int(hold_ms)) / 1000.0)
    _key_up(vk_code)


def _hotkey_vk(*vk_codes: int, hold_ms: int = 35) -> None:
    pressed: list[int] = []
    try:
        for vk_code in vk_codes:
            _key_down(vk_code)
            pressed.append(int(vk_code))
            time.sleep(0.015)
        time.sleep(max(0, int(hold_ms)) / 1000.0)
    finally:
        for vk_code in reversed(pressed):
            _key_up(vk_code)
            time.sleep(0.015)


def _set_clipboard_text(text: str) -> None:
    clip_err = None
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception as e:
            clip_err = e
            time.sleep(0.05)

    if clip_err is not None:
        raise clip_err


def _send_unicode_text(text: str, interval_ms: int = 5) -> None:
    interval_sec = max(0, int(interval_ms)) / 1000.0
    data = text.encode("utf-16-le")

    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i : i + 2], "little")
        events = (_INPUT * 2)(
            _INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=_KEYBDINPUT(
                        wVk=0,
                        wScan=unit,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            ),
            _INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=_KEYBDINPUT(
                        wVk=0,
                        wScan=unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            ),
        )

        sent = user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        if sent != 2:
            raise ctypes.WinError(ctypes.get_last_error())

        if interval_sec:
            time.sleep(interval_sec)


def _paste_clipboard_text(text: str) -> None:
    _set_clipboard_text(text)
    time.sleep(0.05)
    _hotkey_vk(win32con.VK_CONTROL, ord("V"), hold_ms=35)
    time.sleep(0.10)


def _get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        return sx, sy, max(1, r - l), max(1, b - t)
    except Exception:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return l, t, max(1, r - l), max(1, b - t)


def _client_center_screen(hwnd: int) -> tuple[int, int]:
    x, y, width, height = _get_client_rect(int(hwnd))
    return int(x + width // 2), int(y + height // 2)


def _center_cursor_in_client(hwnd: int) -> tuple[int, int]:
    center_x, center_y = _client_center_screen(hwnd)
    win32api.SetCursorPos((center_x, center_y))
    return center_x, center_y


class CommandExecutor:
    def __init__(self, capture: Any = None, api: Any = None):
        print(f"[EXECUTOR] loaded version: {EXECUTOR_VERSION}")

        self.capture = capture
        self.api = api

        self._proc_pid_by_account: dict[str, int] = {}
        self._launch_ts_by_account: dict[str, float] = {}
        self._login_hwnd_by_account: dict[str, int] = {}
        self._dota_hwnd_by_account: dict[str, int] = {}
        self._dota_pid_by_account: dict[str, int] = {}
        # Handles must remain open: closing the last Job handle silently
        # removes its CPU policy from the process.
        self._dota_cpu_jobs: dict[int, int] = {}

    # ---------------------------------------------------------
    # helpers
    # ---------------------------------------------------------

    def _result_ok(self, **kwargs: Any) -> dict[str, Any]:
        out = {"ok": True}
        out.update(kwargs)
        return out

    def _proc_tree_pids(self, account_login: str) -> list[int]:
        root_pid = self._proc_pid_by_account.get(account_login)
        if not root_pid:
            return []

        res: list[int] = []

        try:
            root = psutil.Process(root_pid)
        except Exception:
            return res

        try:
            res.append(root.pid)
            for ch in root.children(recursive=True):
                try:
                    res.append(ch.pid)
                except Exception:
                    pass
        except Exception:
            pass

        return res

    @staticmethod
    def _running_dota_process_count() -> int:
        """Count live Dota processes before accepting another launch command."""
        count = 0
        for proc in psutil.process_iter(["name"]):
            try:
                if str(proc.info.get("name") or "").lower() == "dota2.exe":
                    count += 1
            except (psutil.Error, OSError):
                continue
        return count

    def known_dota_hwnds(self) -> list[int]:
        # HWND values are allocated by Windows and have no meaningful order.
        # Preserve the account launch order so every planner pass always goes
        # first client -> second client -> ... deterministically.
        ordered_logins = sorted(
            self._dota_hwnd_by_account,
            key=lambda login: (self._launch_ts_by_account.get(login, float("inf")), login),
        )
        seen: set[int] = set()
        hwnds: list[int] = []
        for login in ordered_logins:
            hwnd = self._dota_hwnd_by_account.get(login)
            if not hwnd:
                continue
            hwnd_i = int(hwnd)
            if hwnd_i not in seen:
                seen.add(hwnd_i)
                hwnds.append(hwnd_i)
        return hwnds

    @staticmethod
    def _deprioritize_dota_process(pid: int) -> dict[str, Any]:
        """Let the capture/client agent run ahead of background Dota clients."""
        pid = int(pid)
        try:
            proc = psutil.Process(pid)
            if (proc.name() or "").lower() != "dota2.exe":
                return {"applied": False, "pid": pid, "error": "not_dota2"}
            proc.nice(DOTA_PRIORITY_CLASS)
            return {"applied": True, "pid": pid, "priority": "below_normal"}
        except (psutil.Error, OSError) as error:
            return {"applied": False, "pid": pid, "error": str(error)}

    def _apply_dota_cpu_cap(self, pid: int) -> dict[str, Any]:
        """Give one Dota PID an equal, non-blocking scheduler share.

        A Job is kept alive for the lifetime of this executor.  The prior CPU
        limiter could lose its Job handle, which made Windows drop the policy;
        this version controls only the actual dota2.exe process and never
        suspends it, changes its affinity, or uses a hard CPU interval cap.
        """
        pid = int(pid)
        try:
            proc = psutil.Process(pid)
            if (proc.name() or "").lower() != "dota2.exe":
                return {"applied": False, "pid": pid, "error": "not_dota2"}

            if pid in self._dota_cpu_jobs:
                return {
                    "applied": True,
                    "pid": pid,
                    "mode": "per_process_equal_share",
                    "weight": DOTA_CPU_SCHEDULER_WEIGHT,
                    "reused": True,
                }

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())

            try:
                policy = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
                    ControlFlags=(
                        JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                        | JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED
                    ),
                    Value=DOTA_CPU_SCHEDULER_WEIGHT,
                )
                if not kernel32.SetInformationJobObject(
                    job,
                    JobObjectCpuRateControlInformation,
                    ctypes.byref(policy),
                    ctypes.sizeof(policy),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

                process = kernel32.OpenProcess(
                    PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                    False,
                    pid,
                )
                if not process:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if not kernel32.AssignProcessToJobObject(job, process):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel32.CloseHandle(process)
            except Exception:
                kernel32.CloseHandle(job)
                raise

            self._dota_cpu_jobs[pid] = int(job)
            return {
                "applied": True,
                "pid": pid,
                "mode": "per_process_equal_share",
                "weight": DOTA_CPU_SCHEDULER_WEIGHT,
                "reused": False,
            }
        except (psutil.Error, OSError, ctypes.ArgumentError) as error:
            return {"applied": False, "pid": pid, "error": str(error)}

    def _release_dota_cpu_cap(self, pid: int) -> None:
        job = self._dota_cpu_jobs.pop(int(pid), None)
        if job:
            kernel32.CloseHandle(wintypes.HANDLE(job))

    def _find_hwnd_from_payload(self, payload: dict[str, Any]) -> Optional[int]:
        hwnd = payload.get("hwnd")
        if hwnd is not None:
            try:
                return int(hwnd)
            except Exception:
                return None

        account_login = payload.get("account_login")
        field = payload.get("field")
        if account_login:
            account_login = str(account_login)
            if field == "dota":
                return self._dota_hwnd_by_account.get(account_login)
            return self._login_hwnd_by_account.get(account_login)

        return None

    def _click_cancel_by_coords(self, hwnd: int) -> bool:
        try:
            _force_foreground(hwnd)
            time.sleep(0.5)
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            p.moveTo(l + (r - l) / 2 + 70, b - 40)
            time.sleep(0.1)
            p.leftClick()
            return True
        except Exception:
            return False

    def _handle_blockers_prelogin_once(self, account_login: str) -> None:
        pids = set(self._proc_tree_pids(account_login))
        if not pids:
            return

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return

            pid = _window_pid(hwnd)
            if not pid or pid not in pids:
                return

            title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
            if not title or "service" in title or "служб" in title or "ошибка службы" in title:
                self._click_cancel_by_coords(hwnd)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass

    def _steamwebhelper_hwnds_in_tree(
        self,
        account_login: str,
        only_title_steam: bool = True,
    ) -> list[int]:
        pids = set(self._proc_tree_pids(account_login))
        if not pids:
            return []

        hwnds: list[int] = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return

            pid = _window_pid(hwnd)
            if not pid or pid not in pids:
                return

            try:
                pname = (psutil.Process(pid).name() or "").lower()
            except Exception:
                return

            if pname != "steamwebhelper.exe":
                return

            if only_title_steam:
                try:
                    title = (win32gui.GetWindowText(hwnd) or "").strip()
                except Exception:
                    title = ""
                if not title or "steam" not in title.lower():
                    return

            hwnds.append(hwnd)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return []

        return hwnds

    def _find_any_dota_window(
        self,
        exclude_hwnds: set[int],
        exclude_pids: set[int],
        *,
        min_create_ts: Optional[float],
    ) -> Optional[tuple[int, int]]:
        candidates: list[tuple[int, int, int, float]] = []

        def cb(hwnd, _):
            hwnd_i = int(hwnd)

            if hwnd_i in exclude_hwnds:
                return

            if not win32gui.IsWindowVisible(hwnd_i):
                return

            pid = _window_pid(hwnd_i)
            if not pid:
                return

            pid_i = int(pid)

            if pid_i in exclude_pids:
                return

            try:
                proc = psutil.Process(pid_i)
                pname = (proc.name() or "").lower()
                create_ts = float(proc.create_time())
            except Exception:
                return

            if pname != "dota2.exe":
                return

            if min_create_ts is not None and create_ts < float(min_create_ts):
                return

            if not _is_real_dota_window(hwnd_i, pid_i):
                return

            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd_i)
                area = int(r - l) * int(b - t)
            except Exception:
                area = 0

            candidates.append((area, hwnd_i, pid_i, create_ts))

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return None

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _, hwnd, pid, _ = candidates[0]
        return int(hwnd), int(pid)

    # ---------------------------------------------------------
    # process commands
    # ---------------------------------------------------------

    def launch_process(self, payload: dict[str, Any]) -> dict[str, Any]:
        exe_path = str(payload["exe_path"])
        args = [str(x) for x in payload.get("args", [])]
        account_login = str(payload.get("account_login", ""))

        try:
            max_dota_clients = int(payload.get("max_dota_clients", 0) or 0)
        except (TypeError, ValueError):
            max_dota_clients = 0
        if max_dota_clients > 0:
            current_dota_clients = self._running_dota_process_count()
            if current_dota_clients >= max_dota_clients:
                raise RuntimeError(
                    "refusing Dota launch: "
                    f"{current_dota_clients} clients already running "
                    f"(limit {max_dota_clients})"
                )

        launch_ts = time.time()
        proc = subprocess.Popen([exe_path, *args])

        if account_login:
            self._proc_pid_by_account[account_login] = int(proc.pid)
            self._launch_ts_by_account[account_login] = launch_ts

        return self._result_ok(
            pid=int(proc.pid),
            account_login=account_login,
            launch_ts=launch_ts,
        )

    def kill_process_tree(self, payload: dict[str, Any]) -> dict[str, Any]:
        pid = payload.get("pid")
        account_login = str(payload.get("account_login", ""))

        if pid is None and account_login:
            pid = self._proc_pid_by_account.get(account_login)

        if pid is None:
            raise ValueError("kill_process_tree requires pid or account_login")

        proc = psutil.Process(int(pid))
        children = proc.children(recursive=True)
        terminated_pids = {int(proc.pid), *(int(child.pid) for child in children)}

        for ch in children:
            try:
                ch.terminate()
            except Exception:
                pass

        try:
            proc.terminate()
        except Exception:
            pass

        _, alive = psutil.wait_procs([*children, proc], timeout=3)
        for p_alive in alive:
            try:
                p_alive.kill()
            except Exception:
                pass

        for terminated_pid in terminated_pids:
            self._release_dota_cpu_cap(terminated_pid)

        return self._result_ok(pid=int(pid), killed=True)

    def find_login_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_login = str(payload.get("account_login", ""))
        timeout_sec = float(payload.get("timeout_sec", 0.0))
        if timeout_sec > 0:
            timeout_ms = int(timeout_sec * 1000)
        else:
            timeout_ms = int(payload.get("timeout_ms", 30000))
        deadline = time.time() + (timeout_ms / 1000.0)
        last_tree_pids: list[int] = []
        excluded = {int(h) for login, h in self._login_hwnd_by_account.items()
                    if login != account_login and h and win32gui.IsWindow(int(h))}

        while time.time() < deadline:
            pids = set(self._proc_tree_pids(account_login))
            last_tree_pids = sorted(int(x) for x in pids)

            if not pids:
                # Steam sometimes hands the login UI to a process outside the
                # launcher's original process tree.  Its visible sign-in
                # window is still the correct target in this VM.
                detached_login = _any_visible_steam_login_hwnd(excluded)
                if detached_login:
                    _force_foreground(detached_login)
                    self._login_hwnd_by_account[account_login] = int(detached_login)
                    return self._result_ok(
                        found=True,
                        hwnd=int(detached_login),
                        account_login=account_login,
                        tree_pids=[],
                        process_tree_alive=False,
                        fallback_used=True,
                        detached_process=True,
                    )
                time.sleep(0.4)
                continue

            found = _any_login_hwnd_for_pids(pids)
            if found:
                _force_foreground(found)
                self._login_hwnd_by_account[account_login] = int(found)
                return self._result_ok(
                    found=True,
                    hwnd=int(found),
                    account_login=account_login,
                    tree_pids=last_tree_pids,
                    process_tree_alive=bool(last_tree_pids),
                )

            self._handle_blockers_prelogin_once(account_login)

            steam_hwnds = self._steamwebhelper_hwnds_in_tree(
                account_login,
                only_title_steam=True,
            )
            if not steam_hwnds:
                steam_hwnds = self._steamwebhelper_hwnds_in_tree(
                    account_login,
                    only_title_steam=False,
                )
            if steam_hwnds:
                fallback = int(steam_hwnds[0])
                _force_foreground(fallback)
                self._login_hwnd_by_account[account_login] = fallback
                return self._result_ok(
                    found=True,
                    hwnd=fallback,
                    account_login=account_login,
                    tree_pids=last_tree_pids,
                    process_tree_alive=bool(last_tree_pids),
                    steamwebhelper_hwnds=[int(x) for x in steam_hwnds],
                    fallback_used=True,
                )

            # A live launcher does not imply that the web-helper remains its
            # descendant. The old fallback ran only when the tree was empty.
            detached_login = _any_visible_steam_login_hwnd(excluded)
            if detached_login:
                _force_foreground(detached_login)
                self._login_hwnd_by_account[account_login] = int(detached_login)
                return self._result_ok(
                    found=True, hwnd=int(detached_login), account_login=account_login,
                    tree_pids=last_tree_pids, process_tree_alive=bool(last_tree_pids),
                    fallback_used=True, detached_process=True,
                )

            time.sleep(0.4)

        return self._result_ok(
            found=False,
            hwnd=None,
            account_login=account_login,
            tree_pids=last_tree_pids,
            process_tree_alive=bool(last_tree_pids),
        )

    def find_dota_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_login = str(payload.get("account_login", ""))
        timeout_sec = float(payload.get("timeout_sec", 0.0))
        if timeout_sec > 0:
            timeout_ms = int(timeout_sec * 1000)
        else:
            timeout_ms = int(payload.get("timeout_ms", 2500))
        deadline = time.time() + (timeout_ms / 1000.0)

        exclude_hwnds = {
            int(x)
            for x in payload.get("exclude_hwnds", [])
            if x is not None
        }

        exclude_pids = {
            int(x)
            for x in payload.get("exclude_pids", [])
            if x is not None
        }

        for login, hwnd in self._dota_hwnd_by_account.items():
            if login != account_login and hwnd is not None:
                exclude_hwnds.add(int(hwnd))

        for login, pid in self._dota_pid_by_account.items():
            if login != account_login and pid is not None:
                exclude_pids.add(int(pid))

        min_create_ts = payload.get("min_create_ts")
        if min_create_ts is None:
            min_create_ts = self._launch_ts_by_account.get(account_login)
        else:
            min_create_ts = float(min_create_ts)

        last_tree_pids: list[int] = []

        while time.time() < deadline:
            tree_pids = self._proc_tree_pids(account_login)
            last_tree_pids = [int(x) for x in tree_pids]

            for pid in tree_pids:
                pid_i = int(pid)

                if pid_i in exclude_pids:
                    continue

                try:
                    proc = psutil.Process(pid_i)
                    if (proc.name() or "").lower() != "dota2.exe":
                        continue

                    hwnd = _find_main_window_for_pid(pid_i)
                    if (
                        hwnd
                        and int(hwnd) not in exclude_hwnds
                        and _is_real_dota_window(int(hwnd), pid_i)
                    ):
                        hwnd_i = int(hwnd)

                        self._dota_hwnd_by_account[account_login] = hwnd_i
                        self._dota_pid_by_account[account_login] = pid_i
                        process_priority = self._deprioritize_dota_process(pid_i)
                        cpu_limit = self._apply_dota_cpu_cap(pid_i)

                        return self._result_ok(
                            found=True,
                            hwnd=hwnd_i,
                            pid=pid_i,
                            account_login=account_login,
                            source="tree",
                            tree_pids=last_tree_pids,
                            process_tree_alive=bool(last_tree_pids),
                            exclude_pids=sorted(exclude_pids),
                            exclude_hwnds=sorted(exclude_hwnds),
                            window_info=_window_info(hwnd_i),
                            process_priority=process_priority,
                            cpu_limit=cpu_limit,
                        )
                except Exception:
                    continue

            any_dota = self._find_any_dota_window(
                exclude_hwnds=exclude_hwnds,
                exclude_pids=exclude_pids,
                min_create_ts=min_create_ts,
            )
            if any_dota is not None:
                hwnd, pid = any_dota

                self._dota_hwnd_by_account[account_login] = int(hwnd)
                self._dota_pid_by_account[account_login] = int(pid)
                process_priority = self._deprioritize_dota_process(int(pid))
                cpu_limit = self._apply_dota_cpu_cap(int(pid))

                return self._result_ok(
                    found=True,
                    hwnd=int(hwnd),
                    pid=int(pid),
                    account_login=account_login,
                    source="global_fallback_real_dota_window_guarded",
                    tree_pids=last_tree_pids,
                    process_tree_alive=bool(last_tree_pids),
                    min_create_ts=min_create_ts,
                    exclude_pids=sorted(exclude_pids),
                    exclude_hwnds=sorted(exclude_hwnds),
                    window_info=_window_info(int(hwnd)),
                    process_priority=process_priority,
                    cpu_limit=cpu_limit,
                )

            time.sleep(0.25)

        return self._result_ok(
            found=False,
            hwnd=None,
            pid=None,
            account_login=account_login,
            tree_pids=last_tree_pids,
            process_tree_alive=bool(last_tree_pids),
            exclude_pids=sorted(exclude_pids),
            exclude_hwnds=sorted(exclude_hwnds),
        )

    # ---------------------------------------------------------
    # window / input commands
    # ---------------------------------------------------------

    def _current_login_hwnd(self, account_login: str) -> Optional[int]:
        """Find the current Steam sign-in window without a long polling loop."""
        pids = set(self._proc_tree_pids(account_login))
        if pids:
            hwnd = _any_login_hwnd_for_pids(pids)
            if hwnd:
                return int(hwnd)
            for only_title in (True, False):
                hwnds = self._steamwebhelper_hwnds_in_tree(
                    account_login, only_title_steam=only_title,
                )
                if hwnds:
                    return int(hwnds[0])

        excluded = {
            int(hwnd) for login, hwnd in self._login_hwnd_by_account.items()
            if login != account_login and hwnd and win32gui.IsWindow(int(hwnd))
        }
        return _any_visible_steam_login_hwnd(excluded)

    def focus_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        account_login = str(payload.get("account_login") or "")
        original_hwnd = hwnd
        try:
            hwnd = _require_live_window(hwnd, "focus_window")
        except ValueError:
            if not account_login or payload.get("field") == "dota":
                raise
            hwnd = _require_live_window(self._current_login_hwnd(account_login), "focus_window")
            self._login_hwnd_by_account[account_login] = hwnd

        # Resolve the target centre first. The pointer still moves only after
        # foregrounding, but no geometry calls remain in that critical gap.
        if bool(payload.get("center_cursor", False)):
            center_x, center_y = _client_center_screen(hwnd)
        else:
            center_x = center_y = None

        # Activate the target first, then immediately put the pointer in its
        # client-area centre. This keeps cursor placement bound to the window
        # that has just become foreground.
        # Planner focus must centre the pointer immediately after foreground,
        # rather than waiting for the generic 60 ms activation pause.
        focus_kwargs = dict(
            settle_ms=0,
            activation_wait_ms=max(0, int(payload.get("activation_wait_ms", 50))),
            retry_wait_ms=max(0, int(payload.get("retry_wait_ms", 100))),
            fast_if_visible=bool(payload.get("fast_if_visible", False)),
        )
        try:
            _force_foreground(hwnd, **focus_kwargs)
        except Exception:
            if not account_login or payload.get("field") == "dota" or win32gui.IsWindow(hwnd):
                raise
            hwnd = _require_live_window(self._current_login_hwnd(account_login), "focus_window")
            self._login_hwnd_by_account[account_login] = hwnd
            if center_x is not None:
                center_x, center_y = _client_center_screen(hwnd)
            _force_foreground(hwnd, **focus_kwargs)
        if account_login and payload.get("field") != "dota":
            self._login_hwnd_by_account[account_login] = hwnd
        if center_x is not None:
            win32api.SetCursorPos((center_x, center_y))
        settle_ms = max(0, int(payload.get("settle_ms", 0)))
        if settle_ms:
            time.sleep(settle_ms / 1000.0)
        if center_x is not None:
            return self._result_ok(hwnd=int(hwnd), x=center_x, y=center_y, settle_ms=settle_ms,
                                   replaced_hwnd=original_hwnd if original_hwnd != hwnd else None)
        return self._result_ok(hwnd=int(hwnd), settle_ms=settle_ms,
                               replaced_hwnd=original_hwnd if original_hwnd != hwnd else None)

    def move_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        x = int(payload["x"])
        y = int(payload["y"])

        if hwnd is None:
            raise ValueError("move_window requires resolved hwnd")

        win32gui.SetWindowPos(
            int(hwnd),
            None,
            int(x),
            int(y),
            0,
            0,
            win32con.SWP_NOZORDER | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
        )

        return self._result_ok(
            hwnd=int(hwnd),
            x=int(x),
            y=int(y),
            window_info=_window_info(int(hwnd)),
        )

    def mouse_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        x = int(payload["x"])
        y = int(payload["y"])
        coord_space = str(payload.get("coord_space", "client"))

        if hwnd is not None and coord_space == "client":
            win_x, win_y, win_w, win_h = _get_client_rect(hwnd)
            x = max(0, min(win_w - 1, x))
            y = max(0, min(win_h - 1, y))
            sx = win_x + x
            sy = win_y + y
        else:
            sx = int(x)
            sy = int(y)

        win32api.SetCursorPos((sx, sy))
        return self._result_ok(x=sx, y=sy)

    def mouse_click(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The Steam password-form click uses a freshly captured desktop point.
        # Steam frequently recreates its web-helper HWND before that click;
        # resolving account_login here would revive the stale handle and make
        # target validation reject the otherwise correct screen click.
        foreground_only = bool(payload.get("foreground_only", False))
        hwnd = None if foreground_only else self._find_hwnd_from_payload(payload)
        x = int(payload["x"])
        y = int(payload["y"])
        button = str(payload.get("button", "right")).lower()
        clicks = int(payload.get("clicks", 1))
        hold_ms = max(0, int(payload.get("hold_ms", 20)))
        # Optional activation delay for callers that request foreground here.
        # Planner focuses separately with a 30 ms switching delay: its 5 FPS
        # processing cadence is independent of the game's 60 FPS rendering.
        settle_ms = max(0, int(payload.get("settle_ms", 80)))
        post_move_settle_ms = max(0, int(payload.get("post_move_settle_ms", 0)))
        post_click_settle_ms = max(0, int(payload.get("post_click_settle_ms", 30)))
        coord_space = str(payload.get("coord_space", "client"))
        force_fg = bool(payload.get("force_fg", True)) and not foreground_only

        if coord_space == "screen":
            if hwnd is not None and force_fg:
                _force_foreground(hwnd)
                if settle_ms:
                    time.sleep(settle_ms / 1000.0)
            sx = x
            sy = y
        else:
            if hwnd is None:
                raise ValueError("mouse_click requires resolved hwnd when coord_space != screen")

            if force_fg:
                _force_foreground(hwnd)
                if settle_ms:
                    time.sleep(settle_ms / 1000.0)

            if coord_space == "client":
                win_x, win_y, win_w, win_h = _get_client_rect(hwnd)
                x = max(0, min(win_w - 1, x))
                y = max(0, min(win_h - 1, y))
                sx = win_x + x
                sy = win_y + y
            else:
                sx = int(x)
                sy = int(y)

        win32api.SetCursorPos((sx, sy))
        absolute_input = payload.get("target_space") == "minimap"
        input_x = input_y = move_flags = 0
        if absolute_input:
            input_x, input_y = _absolute_mouse_point(sx, sy)
            move_flags = (win32con.MOUSEEVENTF_MOVE | win32con.MOUSEEVENTF_ABSOLUTE
                          | win32con.MOUSEEVENTF_VIRTUALDESK)
            move = (_INPUT * 1)(_INPUT(
                type=INPUT_MOUSE,
                union=_INPUT_UNION(mi=_MOUSEINPUT(input_x, input_y, 0, move_flags, 0, 0)),
            ))
            ctypes.set_last_error(0)
            if user32.SendInput(1, move, ctypes.sizeof(_INPUT)) != 1:
                raise RuntimeError(
                    f"SendInput mouse_move rejected: winerror={ctypes.get_last_error()} hwnd={hwnd}"
                )
        time.sleep(
            post_move_settle_ms / 1000.0
            if post_move_settle_ms
            else 0.01
        )

        if hwnd is not None:
            _verify_click_target(hwnd, sx, sy)

        if button == "left":
            down_flag = win32con.MOUSEEVENTF_LEFTDOWN
            up_flag = win32con.MOUSEEVENTF_LEFTUP
        elif button == "middle":
            down_flag = win32con.MOUSEEVENTF_MIDDLEDOWN
            up_flag = win32con.MOUSEEVENTF_MIDDLEUP
        else:
            down_flag = win32con.MOUSEEVENTF_RIGHTDOWN
            up_flag = win32con.MOUSEEVENTF_RIGHTUP

        for _ in range(max(1, clicks)):
            if hwnd is not None:
                _verify_click_target(hwnd, sx, sy)
            # Cursor movement, Windows accepting input, and the game acting
            # on it are separate events. Report rejected button events by phase.
            down = (_INPUT * 1)(
                _INPUT(
                    type=INPUT_MOUSE,
                    union=_INPUT_UNION(
                        mi=_MOUSEINPUT(input_x, input_y, 0, down_flag | move_flags, 0, 0)
                    ),
                )
            )
            up = (_INPUT * 1)(
                _INPUT(
                    type=INPUT_MOUSE,
                    union=_INPUT_UNION(
                        mi=_MOUSEINPUT(input_x, input_y, 0, up_flag | move_flags, 0, 0)
                    ),
                )
            )
            def send_button(events, phase: str) -> None:
                ctypes.set_last_error(0)
                accepted = user32.SendInput(1, events, ctypes.sizeof(_INPUT))
                if accepted != 1:
                    error_code = ctypes.get_last_error()
                    raise RuntimeError(
                        f"SendInput {phase} rejected: accepted={accepted}/1 "
                        f"winerror={error_code} hwnd={hwnd} button={button} "
                        f"point=({sx},{sy}) executor={EXECUTOR_VERSION}"
                    )

            send_button(down, "mouse_down")
            try:
                time.sleep(hold_ms / 1000.0)
            finally:
                # Do not leave the button held if the hold is interrupted.
                send_button(up, "mouse_up")
            # Keep the cursor in place after mouse-up while Dota consumes the
            # completed click. The planner input lock remains held here, so
            # F1 or the next planner command cannot move it prematurely.
            time.sleep(post_click_settle_ms / 1000.0)

        return self._result_ok(
            hwnd=hwnd,
            x=sx,
            y=sy,
            button=button,
            clicks=clicks,
            hold_ms=hold_ms,
            post_move_settle_ms=post_move_settle_ms,
            post_click_settle_ms=post_click_settle_ms,
            coord_space=coord_space,
            input_method="SendInput",
            pointer_method="SendInput_absolute" if absolute_input else "SetCursorPos",
            executor_version=EXECUTOR_VERSION,
            button_events_accepted=2 * max(1, clicks),
        )

    def dismiss_steam_popups(self, payload: dict[str, Any]) -> dict[str, Any]:
        template_name = str(payload.get("template_name", ""))
        coord_space = str(payload.get("coord_space", "screen"))

        if coord_space == "screen":
            result = self.mouse_click(
                {
                    "x": int(payload["x"]),
                    "y": int(payload["y"]),
                    "coord_space": "screen",
                    "button": str(payload.get("button", "left")),
                    "clicks": int(payload.get("clicks", 1)),
                    "force_fg": False,
                }
            )
        else:
            hwnd = self._find_hwnd_from_payload(payload)
            if hwnd is None:
                raise ValueError("dismiss_steam_popups requires hwnd when coord_space != screen")
            result = self.mouse_click(payload)

        result["dismissed"] = True
        result["template_name"] = template_name
        return result

    def close_steam_windows(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Close visible Steam UI windows without stopping any Steam process."""
        closed: list[int] = []
        names = {"steam.exe", "steamwebhelper.exe"}

        def cb(hwnd, _):
            hwnd_i = int(hwnd)
            if not win32gui.IsWindowVisible(hwnd_i):
                return
            pid = _window_pid(hwnd_i)
            if not pid:
                return
            try:
                process_name = (psutil.Process(int(pid)).name() or "").lower()
            except (psutil.Error, OSError):
                return
            if process_name not in names:
                return
            try:
                # WM_CLOSE requests the application's normal window-close
                # path.  It deliberately does not terminate the process.
                win32gui.PostMessage(hwnd_i, win32con.WM_CLOSE, 0, 0)
                closed.append(hwnd_i)
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        return self._result_ok(closed_hwnds=closed, closed_count=len(closed))

    def key_press(self, payload: dict[str, Any]) -> dict[str, Any]:
        vk_code = int(payload["vk_code"])
        min_hold_ms = 0 if bool(payload.get("allow_short_hold", False)) else 70
        hold_ms = max(min_hold_ms, int(payload.get("hold_ms", 70)))
        foreground_only = bool(payload.get("foreground_only", False))
        force_fg = bool(payload.get("force_fg", True)) and not foreground_only
        focus_settle_ms = max(0, int(payload.get("focus_settle_ms", 80)))
        if foreground_only:
            # Password authentication intentionally preserves the focus that
            # the field click established. Steam may recreate its web-helper
            # HWND in that gap, so use the still-focused live window instead
            # of rejecting an obsolete handle from the earlier lookup.
            hwnd = _require_live_window(win32gui.GetForegroundWindow(), "key_press")
            force_fg = False
        else:
            hwnd = _require_live_window(self._find_hwnd_from_payload(payload), "key_press")

        if force_fg:
            _force_foreground(hwnd, settle_ms=0)
            if focus_settle_ms:
                time.sleep(focus_settle_ms / 1000.0)

        _require_foreground(hwnd)
        escape_method = str(payload.get("escape_method", "virtual_key"))
        _tap_vk(vk_code, hold_ms, escape_method=escape_method)

        return self._result_ok(
            vk_code=vk_code, hold_ms=hold_ms, hwnd=hwnd,
            escape_method=escape_method if vk_code == win32con.VK_ESCAPE else None,
            executor_version=EXECUTOR_VERSION,
        )

    def key_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        vk_code = int(payload["vk_code"])
        down = bool(payload["down"])
        force_fg = bool(payload.get("force_fg", True))
        hwnd = _require_live_window(hwnd, "key_event")

        if force_fg:
            _force_foreground(hwnd)
            focus_settle_ms = max(0, int(payload.get("focus_settle_ms", 80)))
            if focus_settle_ms:
                time.sleep(focus_settle_ms / 1000.0)

        if down:
            _key_down(vk_code)
        else:
            _key_up(vk_code)

        return self._result_ok(vk_code=vk_code, down=down, hwnd=hwnd)

    def write_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", ""))
        clear_before = bool(payload.get("clear_before", False))
        field = str(payload.get("field", ""))
        input_method = str(payload.get("input_method", "clipboard_unicode_paste")).lower().strip()
        char_interval_ms = int(payload.get("char_interval_ms", 5))
        foreground_only = bool(payload.get("foreground_only", False))
        force_fg = bool(payload.get("force_fg", True)) and not foreground_only
        if foreground_only:
            hwnd = _require_live_window(win32gui.GetForegroundWindow(), "write_text")
            force_fg = False
        else:
            hwnd = _require_live_window(self._find_hwnd_from_payload(payload), "write_text")

        if force_fg:
            _force_foreground(hwnd)
        else:
            _require_foreground(hwnd)
        _switch_keyboard_layout_en()
        time.sleep(0.12)

        if clear_before:
            _hotkey_vk(win32con.VK_CONTROL, ord("A"), hold_ms=35)
            time.sleep(0.05)
            _tap_vk(win32con.VK_BACK, 25)
            time.sleep(0.05)

        if input_method in ("clipboard", "clipboard_unicode_paste", "paste"):
            _paste_clipboard_text(text)
            method = "clipboard_unicode_paste"
        elif input_method in ("sendinput_unicode", "unicode", "typing", "type"):
            _send_unicode_text(text, interval_ms=char_interval_ms)
            method = "sendinput_unicode"
        else:
            raise ValueError(f"unsupported write_text input_method: {input_method}")

        return self._result_ok(
            text_len=len(text),
            hwnd=hwnd,
            field=field,
            method=method,
            force_fg=force_fg,
        )

    def hotkey(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        keys = [str(x) for x in payload.get("keys", [])]
        force_fg = bool(payload.get("force_fg", True))

        if not keys:
            raise ValueError("hotkey requires non-empty keys")
        hwnd = _require_live_window(hwnd, "hotkey")

        if force_fg:
            _force_foreground(hwnd)

        p.hotkey(*keys)
        return self._result_ok(keys=keys, hwnd=hwnd)

    def sleep_cmd(self, payload: dict[str, Any]) -> dict[str, Any]:
        duration_ms = int(payload.get("duration_ms", 0))
        time.sleep(max(0, duration_ms) / 1000.0)
        return self._result_ok(duration_ms=duration_ms)

    def capture_frame(self, payload: dict[str, Any]) -> dict[str, Any]:
        import numpy as np

        hwnd = int(payload["hwnd"])
        purpose = str(payload.get("purpose", ""))
        require_fresh = bool(payload.get("require_fresh", False))

        if self.capture is None:
            raise RuntimeError("CommandExecutor.capture is not initialized")

        # MM pages can be static; DXcam then correctly reports no *new* frame.
        # A last verified DXcam frame is still the right image for template
        # matching in this command path.
        frame_rgb = self.capture.grab_window_rgb(
            hwnd,
            allow_cached=not require_fresh,
            # After a click MM must observe the newly opened menu, not reuse
            # the preceding static DXcam frame immediately.
            wait_for_fresh_ms=500 if require_fresh else 150,
        )

        if frame_rgb is None:
            return self._result_ok(
                capture_sent=False,
                hwnd=hwnd,
                purpose=purpose,
                error=f"capture returned None for hwnd={hwnd}",
            )

        if not isinstance(frame_rgb, np.ndarray):
            return self._result_ok(
                capture_sent=False,
                hwnd=hwnd,
                purpose=purpose,
                error=f"capture returned non-numpy frame for hwnd={hwnd}",
            )

        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            return self._result_ok(
                capture_sent=False,
                hwnd=hwnd,
                purpose=purpose,
                error=f"invalid frame shape={getattr(frame_rgb, 'shape', None)}",
            )

        if frame_rgb.dtype != np.uint8:
            frame_rgb = frame_rgb.astype(np.uint8, copy=False)

        if not frame_rgb.flags["C_CONTIGUOUS"]:
            frame_rgb = np.ascontiguousarray(frame_rgb)

        height, width = frame_rgb.shape[:2]

        # MM needs frequent frames while the pick timer is running.  The raw
        # transport avoids PNG encoding and base64 JSON expansion on every
        # capture request.
        if self.api is not None and getattr(self.api, "vm_id", None):
            submit_response = self.api.submit_frame_raw(hwnd=hwnd, frame_rgb=frame_rgb)
            return self._result_ok(
                capture_sent=True,
                frame_uploaded=True,
                hwnd=hwnd,
                purpose=purpose,
                width=int(width),
                height=int(height),
                submit_response=submit_response,
                ts=time.time(),
            )

        img = Image.fromarray(frame_rgb, mode="RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=0)

        image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        submit_response = {
            "ok": True,
            "skipped": True,
            "reason": "frame_returned_in_ack",
        }

        return self._result_ok(
            capture_sent=True,
            hwnd=hwnd,
            purpose=purpose,
            width=int(width),
            height=int(height),
            format="png",
            image_b64=image_b64,
            submit_response=submit_response,
            ts=time.time(),
        )

    def capture_desktop(self, payload: dict[str, Any]) -> dict[str, Any]:
        img = p.screenshot()
        width, height = img.size

        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=0)

        return self._result_ok(
            width=int(width),
            height=int(height),
            format="png",
            image_b64=base64.b64encode(buf.getvalue()).decode("ascii"),
            desktop=_desktop_bounds(),
        )

    # ---------------------------------------------------------
    # dispatcher
    # ---------------------------------------------------------

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        cmd_type = str(command["type"])
        payload = dict(command.get("payload") or {})

        if cmd_type == "capture_frame":
            return self.capture_frame(payload)
        if cmd_type == "capture_desktop":
            return self.capture_desktop(payload)

        if cmd_type == HostCommandType.LAUNCH_PROCESS:
            return self.launch_process(payload)
        if cmd_type == HostCommandType.KILL_PROCESS_TREE:
            return self.kill_process_tree(payload)
        if cmd_type == HostCommandType.FIND_LOGIN_WINDOW:
            return self.find_login_window(payload)
        if cmd_type == HostCommandType.FIND_DOTA_WINDOW:
            return self.find_dota_window(payload)
        if cmd_type == HostCommandType.FOCUS_WINDOW:
            return self.focus_window(payload)
        if cmd_type == HostCommandType.MOVE_WINDOW:
            return self.move_window(payload)
        if cmd_type == HostCommandType.MOUSE_MOVE:
            return self.mouse_move(payload)
        if cmd_type == HostCommandType.MOUSE_CLICK:
            return self.mouse_click(payload)
        if cmd_type == "attack_click":
            hwnd = self._find_hwnd_from_payload(payload)
            hwnd = _require_live_window(hwnd, "attack_click")
            if bool(payload.get("force_fg", True)):
                _force_foreground(hwnd)
                time.sleep(max(0, int(payload.get("focus_settle_ms", 80))) / 1000.0)
            _tap_vk(ord("A"), hold_ms=max(70, int(payload.get("attack_hold_ms", 70))))
            time.sleep(0.04)
            click_payload = dict(payload)
            click_payload["hwnd"] = int(hwnd)
            click_payload["button"] = "left"
            click_payload["force_fg"] = False
            return self.mouse_click(click_payload)
        if cmd_type == HostCommandType.DISMISS_STEAM_POPUPS:
            return self.dismiss_steam_popups(payload)
        if cmd_type == HostCommandType.CLOSE_STEAM_WINDOWS:
            return self.close_steam_windows(payload)
        if cmd_type == HostCommandType.KEY_PRESS:
            return self.key_press(payload)
        if cmd_type == HostCommandType.KEY_EVENT:
            return self.key_event(payload)
        if cmd_type == HostCommandType.WRITE_TEXT:
            return self.write_text(payload)
        if cmd_type == HostCommandType.HOTKEY:
            return self.hotkey(payload)
        if cmd_type == HostCommandType.SLEEP:
            return self.sleep_cmd(payload)
        if cmd_type == HostCommandType.CAPTURE_DESKTOP:
            return self.capture_desktop(payload)
        if cmd_type == HostCommandType.CAPTURE_FRAME:
            return self.capture_frame(payload)
        if cmd_type == HostCommandType.LOG:
            return self._result_ok()

        raise ValueError(f"Unknown command type: {cmd_type}")
