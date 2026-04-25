# scripts/client/api_client.py
from __future__ import annotations

import time
from typing import Any, Optional

import requests


class PlannerApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        debug: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.debug = bool(debug)

        self.session = requests.Session()
        self.vm_id: Optional[str] = None
        self.capacity: int = 5

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[API] {message}", flush=True)

    def register_vm(self) -> dict[str, Any]:
        self._log("register_vm -> {}")

        resp = self.session.post(
            f"{self.base_url}/planner/register-vm",
            json={},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self.vm_id = str(data["vm_id"])
        self.capacity = int(data.get("capacity", 5))

        self._log(f"register_vm <- {data}")
        return data

    def get_assigned_accounts(self) -> dict[str, Any]:
        if not self.vm_id:
            raise RuntimeError("vm_id is not initialized")

        resp = self.session.get(
            f"{self.base_url}/planner/get-assigned-accounts",
            params={"vm_id": self.vm_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._log(f"get_assigned_accounts(vm_id={self.vm_id}) <- {data}")
        return data

    def get_command(self) -> Optional[dict[str, Any]]:
        if not self.vm_id:
            raise RuntimeError("vm_id is not initialized")

        resp = self.session.get(
            f"{self.base_url}/planner/get-command",
            params={"vm_id": self.vm_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        cmd = data.get("command")
        self._log(f"get_command(vm_id={self.vm_id}) <- {cmd}")
        return cmd

    def ack_command(
        self,
        command_id: int,
        status: str,
        result: Any,
    ) -> dict[str, Any]:
        if not self.vm_id:
            raise RuntimeError("vm_id is not initialized")

        payload = {
            "vm_id": self.vm_id,
            "command_id": int(command_id),
            "status": str(status),
            "result": result,
        }

        log_payload = payload
        try:
            if isinstance(result, dict) and "image_b64" in result:
                safe_result = dict(result)
                safe_result["image_b64"] = f"<base64 {len(result.get('image_b64') or '')} chars>"
                log_payload = dict(payload)
                log_payload["result"] = safe_result
        except Exception:
            pass

        self._log(f"ack_command -> {log_payload}")
        resp = self.session.post(
            f"{self.base_url}/planner/ack-command",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._log(f"ack_command <- {data}")
        return data

    def send_log(
        self,
        level: str,
        source: str,
        event: str,
        message: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.vm_id:
            raise RuntimeError("vm_id is not initialized")

        body = {
            "vm_id": self.vm_id,
            "level": str(level),
            "source": str(source),
            "event": str(event),
            "message": str(message),
            "payload": payload or {},
        }

        self._log(f"send_log -> {body}")
        resp = self.session.post(
            f"{self.base_url}/planner/log",
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._log(f"send_log <- {data}")
        return data

    def submit_frame_raw(
        self,
        hwnd: int,
        frame_rgb,
        ts_client: Optional[float] = None,
    ) -> dict[str, Any]:
        if not self.vm_id:
            raise RuntimeError("vm_id is not initialized")

        if ts_client is None:
            ts_client = time.time()

        height = int(frame_rgb.shape[0])
        width = int(frame_rgb.shape[1])
        channels = int(frame_rgb.shape[2])

        params = {
            "vm_id": self.vm_id,
            "hwnd": int(hwnd),
            "ts_client": float(ts_client),
            "width": width,
            "height": height,
            "channels": channels,
            "dtype": "uint8",
            "layout": "HWC",
            "color": "RGB",
        }

        self._log(
            f"submit_frame_raw -> hwnd={int(hwnd)} shape=({height}, {width}, {channels}) ts={ts_client:.3f}"
        )

        resp = self.session.post(
            f"{self.base_url}/planner/submit-frame-raw",
            params=params,
            data=frame_rgb.tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._log(f"submit_frame_raw <- {data}")
        return data