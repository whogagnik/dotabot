from __future__ import annotations

import time
from typing import Optional, Any

import numpy as np
import requests


class PlannerApiClient:
    def __init__(self, base_url: str, vm_id: str, timeout: float = 10.0, debug: bool = False):
        self.base_url = base_url.rstrip("/")
        self.vm_id = str(vm_id)
        self.timeout = float(timeout)
        self.debug = bool(debug)
        self.session = requests.Session()

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"[API] {msg}", flush=True)

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    def register_vm(self, capacity: int = 5, side: str = "radiant") -> dict[str, Any]:
        payload = {
            "vm_id": self.vm_id,
            "capacity": int(capacity),
            "side": str(side),
        }
        self._log(f"register_vm -> {payload}")

        resp = self.session.post(
            f"{self.base_url}/planner/register-vm",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self._log(f"register_vm <- {data}")
        return data

    def get_assigned_accounts(self) -> dict[str, Any]:
        self._log(f"get_assigned_accounts -> vm_id={self.vm_id}")

        resp = self.session.get(
            f"{self.base_url}/planner/get-assigned-accounts",
            params={"vm_id": self.vm_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self._log(f"get_assigned_accounts <- {data}")
        return data

    def register_hwnds(
        self,
        hwnds: list[int],
        roles: Optional[list[str]] = None,
        side: str = "radiant",
    ) -> dict[str, Any]:
        hwnds = [int(x) for x in hwnds]
        roles = roles or ["unknown"] * len(hwnds)

        payload = {
            "vm_id": self.vm_id,
            "hwnds": hwnds,
            "roles": [str(x) for x in roles],
            "side": str(side),
        }
        self._log(f"register_hwnds -> {payload}")

        resp = self.session.post(
            f"{self.base_url}/planner/register-hwnds",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self._log(f"register_hwnds <- {data}")
        return data

    # ------------------------------------------------------------------
    # frames
    # ------------------------------------------------------------------

    def submit_frame_raw(
        self,
        hwnd: int,
        frame_rgb: np.ndarray,
        ts_client: Optional[float] = None,
    ) -> dict[str, Any]:
        ts_client = time.time() if ts_client is None else float(ts_client)

        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            raise ValueError(f"Expected HWC RGB frame, got shape={frame_rgb.shape}")
        if frame_rgb.dtype != np.uint8:
            frame_rgb = frame_rgb.astype(np.uint8, copy=False)

        h, w, c = frame_rgb.shape

        self._log(f"submit_frame_raw -> hwnd={hwnd} shape={frame_rgb.shape} ts={ts_client:.3f}")

        resp = self.session.post(
            f"{self.base_url}/planner/submit-frame-raw",
            params={
                "vm_id": self.vm_id,
                "hwnd": int(hwnd),
                "ts_client": ts_client,
                "width": int(w),
                "height": int(h),
                "channels": int(c),
                "dtype": "uint8",
                "layout": "HWC",
                "color": "RGB",
            },
            data=frame_rgb.tobytes(order="C"),
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self._log(f"submit_frame_raw <- {data}")
        return data

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def get_command(self, hwnd: int) -> Optional[dict[str, Any]]:
        resp = self.session.get(
            f"{self.base_url}/planner/get-command",
            params={
                "vm_id": self.vm_id,
                "hwnd": int(hwnd),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        cmd = data.get("command")

        self._log(f"get_command(hwnd={hwnd}) <- {cmd}")
        return cmd

    def ack_command(
        self,
        hwnd: int,
        command_id: int,
        status: str = "done",
        result: str = "",
    ) -> dict[str, Any]:
        payload = {
            "vm_id": self.vm_id,
            "hwnd": int(hwnd),
            "command_id": int(command_id),
            "status": str(status),
            "result": str(result),
        }
        self._log(f"ack_command -> {payload}")

        resp = self.session.post(
            f"{self.base_url}/planner/ack-command",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        self._log(f"ack_command <- {data}")
        return data