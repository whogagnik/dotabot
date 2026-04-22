# scripts/client/executor.py
from __future__ import annotations

import time
import win32api
import win32con
import win32gui

from dota_window import get_client_rect


def force_foreground(hwnd: int) -> None:
    try:
        print(f"[EXEC] force_foreground hwnd={hex(hwnd)}", flush=True)

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.02)
    except Exception as e:
        print(f"[EXEC] force_foreground failed: {e}", flush=True)


class CommandExecutor:
    def click_on_screen(
        self,
        hwnd: int,
        x: int,
        y: int,
        *,
        mouse_button: str = "right",
        attack: bool = False,
        force_fg: bool = True,
    ) -> None:
        print(
            f"[EXEC] click_on_screen hwnd={hex(hwnd)} client=({x},{y}) "
            f"button={mouse_button} attack={attack} force_fg={force_fg}",
            flush=True,
        )

        if force_fg:
            force_foreground(hwnd)

        win_x, win_y, win_w, win_h = get_client_rect(hwnd)

        x = max(0, min(win_w - 1, int(x)))
        y = max(0, min(win_h - 1, int(y)))

        sx = win_x + x
        sy = win_y + y

        print(f"[EXEC] screen click -> ({sx},{sy})", flush=True)

        try:
            ox, oy = win32api.GetCursorPos()
        except Exception:
            ox = oy = None

        win32api.SetCursorPos((sx, sy))
        time.sleep(0.002)

        if attack:
            vk_a = 0x41  # 'A'
            win32api.keybd_event(vk_a, 0, 0, 0)
            time.sleep(0.002)

            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            time.sleep(0.002)
            win32api.keybd_event(vk_a, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            btn = (mouse_button or "right").lower()

            if btn == "left":
                down_flag = win32con.MOUSEEVENTF_LEFTDOWN
                up_flag = win32con.MOUSEEVENTF_LEFTUP
            elif btn == "middle":
                down_flag = win32con.MOUSEEVENTF_MIDDLEDOWN
                up_flag = win32con.MOUSEEVENTF_MIDDLEUP
            else:
                down_flag = win32con.MOUSEEVENTF_RIGHTDOWN
                up_flag = win32con.MOUSEEVENTF_RIGHTUP

            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)

        if ox is not None and oy is not None:
            win32api.SetCursorPos((ox, oy))

    def key_event(
        self,
        hwnd: int,
        vk_code: int,
        down: bool,
        *,
        force_fg: bool = True,
    ) -> None:
        print(
            f"[EXEC] key_event hwnd={hex(hwnd)} vk={vk_code} down={down} force_fg={force_fg}",
            flush=True,
        )

        if force_fg:
            force_foreground(hwnd)

        if down:
            win32api.keybd_event(int(vk_code), 0, 0, 0)
        else:
            win32api.keybd_event(int(vk_code), 0, win32con.KEYEVENTF_KEYUP, 0)

    def key_press(
        self,
        hwnd: int,
        vk_code: int,
        hold_ms: int = 25,
        *,
        force_fg: bool = True,
    ) -> None:
        print(
            f"[EXEC] key_press hwnd={hex(hwnd)} vk={vk_code} hold_ms={hold_ms} force_fg={force_fg}",
            flush=True,
        )

        if force_fg:
            force_foreground(hwnd)

        win32api.keybd_event(int(vk_code), 0, 0, 0)
        time.sleep(max(0, int(hold_ms)) / 1000.0)
        win32api.keybd_event(int(vk_code), 0, win32con.KEYEVENTF_KEYUP, 0)

    def execute(self, hwnd: int, command: dict) -> str:
        print(f"[EXEC] execute command={command}", flush=True)

        cmd_type = command["type"]
        payload = command.get("payload", {}) or {}

        if cmd_type == "mouse_click":
            self.click_on_screen(
                hwnd=hwnd,
                x=int(payload["x"]),
                y=int(payload["y"]),
                mouse_button=str(payload.get("button", "right")),
                attack=False,
                force_fg=bool(payload.get("force_fg", True)),
            )
            return "ok"

        if cmd_type == "attack_click":
            self.click_on_screen(
                hwnd=hwnd,
                x=int(payload["x"]),
                y=int(payload["y"]),
                attack=True,
                force_fg=bool(payload.get("force_fg", True)),
            )
            return "ok"

        if cmd_type == "key_event":
            self.key_event(
                hwnd=hwnd,
                vk_code=int(payload["vk_code"]),
                down=bool(payload["down"]),
                force_fg=bool(payload.get("force_fg", True)),
            )
            return "ok"

        if cmd_type == "key_press":
            self.key_press(
                hwnd=hwnd,
                vk_code=int(payload["vk_code"]),
                hold_ms=int(payload.get("hold_ms", 25)),
                force_fg=bool(payload.get("force_fg", True)),
            )
            return "ok"

        raise ValueError(f"Unknown command type: {cmd_type}")