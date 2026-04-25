# scripts/client/main.py
from __future__ import annotations

import time
import traceback

from api_client import PlannerApiClient
from capture import DotaCapture
from executor import CommandExecutor


HOST_URL = "http://192.168.217.1:8000"
DEBUG = True


class VmClient:
    """
    Thin agent:
    - регистрируется на host
    - получает vm_id
    - шлёт логи
    - получает команды
    - исполняет команды
    - подтверждает результат
    - шлёт raw frame только когда host прислал команду capture_frame
    """

    def __init__(self):
        self.api = PlannerApiClient(
            base_url=HOST_URL,
            timeout=15.0,
            debug=DEBUG,
        )
        self.capture = DotaCapture()
        self.executor = CommandExecutor()
        self._running = True

    # ---------------------------------------------------------
    # logging
    # ---------------------------------------------------------

    def log(self, level: str, event: str, message: str, payload: dict | None = None) -> None:
        try:
            if self.api.vm_id:
                self.api.send_log(
                    level=level,
                    source="client",
                    event=event,
                    message=message,
                    payload=payload or {},
                )
            else:
                print(f"[CLIENT][{level.upper()}][{event}] {message} | payload={payload}", flush=True)
        except Exception:
            print(f"[CLIENT][LOG-FAIL][{level.upper()}][{event}] {message}", flush=True)

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def bootstrap(self) -> None:
        resp = self.api.register_vm()
        self.log(
            "info",
            "vm_registered",
            "VM registered on host",
            payload=resp,
        )

    # ---------------------------------------------------------
    # special commands
    # ---------------------------------------------------------

    def _handle_capture_frame(self, command: dict) -> dict:
        payload = command.get("payload") or {}
        hwnd = payload.get("hwnd")
        if hwnd is None:
            raise ValueError("capture_frame requires hwnd")
        hwnd = int(hwnd)

        frame_rgb = self.capture.grab_window_rgb(hwnd)
        if frame_rgb is None:
            raise RuntimeError(f"capture returned None for hwnd={hwnd}")

        submit_resp = self.api.submit_frame_raw(
            hwnd=hwnd,
            frame_rgb=frame_rgb,
        )

        return {
            "capture_sent": True,
            "hwnd": hwnd,
            "submit_response": submit_resp,
        }

    def _execute_command(self, command: dict) -> dict:
        cmd_type = str(command["type"])

        if cmd_type == "capture_frame":
            return self._handle_capture_frame(command)

        return self.executor.execute(command)

    # ---------------------------------------------------------
    # loop
    # ---------------------------------------------------------

    def tick_one(self) -> None:
        cmd = self.api.get_command()
        if cmd is None:
            time.sleep(0.05)
            return

        cmd_id = int(cmd["id"])
        cmd_type = str(cmd["type"])

        self.log(
            "debug",
            "command_received",
            f"Received command: {cmd_type}",
            payload={"command_id": cmd_id},
        )

        try:
            result = self._execute_command(cmd)
            self.api.ack_command(
                command_id=cmd_id,
                status="done",
                result=result,
            )
            self.log(
                "debug",
                "command_done",
                f"Command executed: {cmd_type}",
                payload={"command_id": cmd_id, "result": result},
            )
        except Exception as e:
            err = {
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

            try:
                self.api.ack_command(
                    command_id=cmd_id,
                    status="failed",
                    result=err,
                )
            except Exception:
                pass

            self.log(
                "error",
                "command_failed",
                f"Command failed: {cmd_type}",
                payload={"command_id": cmd_id, **err},
            )
            time.sleep(0.1)

    def run(self) -> None:
        self.bootstrap()
        self.log("info", "client_started", "Client loop started")

        while self._running:
            try:
                self.tick_one()
            except Exception:
                self.log(
                    "error",
                    "client_tick_failed",
                    "Unhandled exception in client loop",
                    payload={"traceback": traceback.format_exc()},
                )
                time.sleep(0.2)


def main():
    client = VmClient()
    client.run()


if __name__ == "__main__":
    main()