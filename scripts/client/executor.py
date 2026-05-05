# scripts/client/executor.py
from __future__ import annotations

import base64
import ctypes
import io
import subprocess
import time
from typing import Any, Optional

import psutil
import pyautogui as p
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image


EXECUTOR_VERSION = "executor_real_dota_window_filter_v5"

user32 = ctypes.WinDLL("user32", use_last_error=True)


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


def _force_foreground(hwnd: int) -> None:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)

        fore = win32gui.GetForegroundWindow()
        ftid = win32process.GetWindowThreadProcessId(fore)[0] if fore else 0
        ctid = win32api.GetCurrentThreadId()

        user32.AttachThreadInput(ftid, ctid, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetActiveWindow(hwnd)
        finally:
            user32.AttachThreadInput(ftid, ctid, False)

        time.sleep(0.06)
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.06)
        except Exception:
            pass


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


def _get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        return sx, sy, max(1, r - l), max(1, b - t)
    except Exception:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return l, t, max(1, r - l), max(1, b - t)


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

        while time.time() < deadline:
            pids = set(self._proc_tree_pids(account_login))
            last_tree_pids = sorted(int(x) for x in pids)

            if not pids:
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

    def focus_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        hwnd = _require_live_window(hwnd, "focus_window")

        _force_foreground(hwnd)
        return self._result_ok(hwnd=int(hwnd))

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
        hwnd = self._find_hwnd_from_payload(payload)
        x = int(payload["x"])
        y = int(payload["y"])
        button = str(payload.get("button", "right")).lower()
        clicks = int(payload.get("clicks", 1))
        coord_space = str(payload.get("coord_space", "client"))
        force_fg = bool(payload.get("force_fg", True))

        if coord_space == "screen":
            sx = x
            sy = y
        else:
            if hwnd is None:
                raise ValueError("mouse_click requires resolved hwnd when coord_space != screen")

            if force_fg:
                _force_foreground(hwnd)

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
        time.sleep(0.01)

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
            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)
            time.sleep(0.03)

        return self._result_ok(
            hwnd=hwnd,
            x=sx,
            y=sy,
            button=button,
            clicks=clicks,
            coord_space=coord_space,
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

    def key_press(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        vk_code = int(payload["vk_code"])
        hold_ms = int(payload.get("hold_ms", 25))
        force_fg = bool(payload.get("force_fg", True))
        hwnd = _require_live_window(hwnd, "key_press")

        if force_fg:
            _force_foreground(hwnd)

        win32api.keybd_event(vk_code, 0, 0, 0)
        time.sleep(max(0, hold_ms) / 1000.0)
        win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)

        return self._result_ok(vk_code=vk_code, hold_ms=hold_ms, hwnd=hwnd)

    def key_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        vk_code = int(payload["vk_code"])
        down = bool(payload["down"])
        force_fg = bool(payload.get("force_fg", True))
        hwnd = _require_live_window(hwnd, "key_event")

        if force_fg:
            _force_foreground(hwnd)

        if down:
            win32api.keybd_event(vk_code, 0, 0, 0)
        else:
            win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)

        return self._result_ok(vk_code=vk_code, down=down, hwnd=hwnd)

    def write_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        hwnd = self._find_hwnd_from_payload(payload)
        text = str(payload.get("text", ""))
        clear_before = bool(payload.get("clear_before", False))
        field = str(payload.get("field", ""))
        hwnd = _require_live_window(hwnd, "write_text")

        _force_foreground(hwnd)
        _switch_keyboard_layout_en()
        time.sleep(0.12)

        if clear_before:
            p.hotkey("ctrl", "a")
            time.sleep(0.05)
            p.press("backspace")
            time.sleep(0.05)

        clip_err = None
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                clip_err = None
                break
            except Exception as e:
                clip_err = e
                time.sleep(0.05)

        if clip_err is not None:
            raise clip_err

        time.sleep(0.05)
        p.hotkey("ctrl", "v")
        time.sleep(0.10)

        return self._result_ok(
            text_len=len(text),
            hwnd=hwnd,
            field=field,
            method="clipboard_unicode_paste",
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

        if self.capture is None:
            raise RuntimeError("CommandExecutor.capture is not initialized")

        frame_rgb = self.capture.grab_window_rgb(hwnd)

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
        if cmd_type == HostCommandType.DISMISS_STEAM_POPUPS:
            return self.dismiss_steam_popups(payload)
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
