# scripts/client/main.py
from __future__ import annotations

import os
import time
import traceback

import requests

from api_client import PlannerApiClient
from capture import DotaCapture
from executor import CommandExecutor


HOST_URL = "http://192.168.217.1:8000"
DEBUG = os.environ.get("DOTABOT_CLIENT_DEBUG", "").lower() in {"1", "true", "yes", "on"}


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
        self.executor = CommandExecutor(capture=self.capture, api=self.api)
        self._running = True
        self._bootstrap_backoff_sec = 1.0
        self._loop_error_backoff_sec = 0.2
        self._last_local_error_message = ""
        self._last_local_error_ts = 0.0

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
        except Exception as e:
            if self._should_reset_registration(e):
                self.api.reset_registration()
            self.local_log(
                "warning",
                "log_failed",
                f"Could not send {event} log",
                payload={"error": str(e)},
                min_interval_sec=5.0,
            )

    def local_log(
        self,
        level: str,
        event: str,
        message: str,
        payload: dict | None = None,
        *,
        min_interval_sec: float = 2.0,
    ) -> None:
        now = time.time()
        key = f"{level}:{event}:{message}"
        if key == self._last_local_error_message and now - self._last_local_error_ts < min_interval_sec:
            return

        self._last_local_error_message = key
        self._last_local_error_ts = now
        print(f"[CLIENT][{level.upper()}][{event}] {message} | payload={payload}", flush=True)

    def _should_reset_registration(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {404, 503}:
            return True

        return isinstance(
            exc,
            (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ),
        )

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def bootstrap(self) -> None:
        resp = self.api.register_vm()
        self._bootstrap_backoff_sec = 1.0
        self._loop_error_backoff_sec = 0.2
        self.log(
            "info",
            "vm_registered",
            "VM registered on host",
            payload=resp,
        )

    def bootstrap_once(self) -> bool:
        try:
            self.bootstrap()
            return True
        except Exception as e:
            self.api.reset_registration()
            self.local_log(
                "warning",
                "bootstrap_failed",
                f"Host is not ready, retry in {self._bootstrap_backoff_sec:.1f}s",
                payload={"error": str(e)},
                min_interval_sec=5.0,
            )
            time.sleep(self._bootstrap_backoff_sec)
            self._bootstrap_backoff_sec = min(15.0, self._bootstrap_backoff_sec * 1.7)
            return False

    # ---------------------------------------------------------
    # command execution
    # ---------------------------------------------------------

    def _execute_command(self, command: dict) -> dict:
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
            except Exception as ack_error:
                self.local_log(
                    "warning",
                    "ack_failed",
                    f"Could not report failed command: {cmd_type}",
                    payload={"command_id": cmd_id, "error": str(ack_error)},
                    min_interval_sec=3.0,
                )

            self.log(
                "error",
                "command_failed",
                f"Command failed: {cmd_type}",
                payload={"command_id": cmd_id, **err},
            )
            time.sleep(0.1)
            return

        try:
            self.api.ack_command(
                command_id=cmd_id,
                status="done",
                result=result,
            )
        except Exception as e:
            self.local_log(
                "warning",
                "ack_failed",
                f"Command executed but ack failed: {cmd_type}",
                payload={"command_id": cmd_id, "error": str(e)},
                min_interval_sec=3.0,
            )
            raise

        try:
            self.log(
                "debug",
                "command_done",
                f"Command executed: {cmd_type}",
                payload={"command_id": cmd_id, "result": result},
            )
        except Exception:
            pass

    def run(self) -> None:
        while self._running:
            if not self.api.vm_id:
                if not self.bootstrap_once():
                    continue
                self.log("info", "client_started", "Client loop started")

            try:
                self.tick_one()
                self._loop_error_backoff_sec = 0.2
            except Exception as e:
                err = traceback.format_exc()

                if self._should_reset_registration(e):
                    self.api.reset_registration()

                self.local_log(
                    "error",
                    "client_tick_failed",
                    f"Unhandled exception in client loop, retry in {self._loop_error_backoff_sec:.1f}s",
                    payload={"traceback": err},
                    min_interval_sec=3.0,
                )
                time.sleep(self._loop_error_backoff_sec)
                self._loop_error_backoff_sec = min(10.0, self._loop_error_backoff_sec * 1.7)


def main():
    client = VmClient()
    client.run()


if __name__ == "__main__":
    main()
