from __future__ import annotations

import subprocess
import time
import traceback
from dataclasses import dataclass
from typing import List, Optional

try:
    from scripts.client.api_client import PlannerApiClient
    from scripts.client.capture import DotaCapture
    from scripts.client.executor import CommandExecutor
    from scripts.client.dota_window import find_dota_hwnd
except Exception:
    from api_client import PlannerApiClient
    from capture import DotaCapture
    from executor import CommandExecutor
    from dota_window import find_dota_hwnd


BASE_URL = "http://192.168.217.1:8000"
VM_ID = "vm_1"
SIDE = "radiant"
CAPACITY = 5
DEBUG = True


@dataclass
class VmAccount:
    login: str
    password: str
    has_mafile: bool = False
    mafile_path: Optional[str] = None
    hwnd: Optional[int] = None
    started: bool = False
    launcher_proc: Optional[subprocess.Popen] = None


class VmClientController:
    def __init__(self):
        self.api = PlannerApiClient(base_url=BASE_URL, vm_id=VM_ID, timeout=15.0, debug=DEBUG)
        self.capture = DotaCapture()
        self.executor = CommandExecutor()

        self.accounts: List[VmAccount] = []
        self.hwnds_registered = False
        self.last_frame_submit_ts_by_hwnd: dict[int, float] = {}
        self.frame_submit_min_dt = 0.03

    def bootstrap(self) -> None:
        print("[VM] register_vm...", flush=True)
        print(f"[VM] register_vm <- {self.api.register_vm(capacity=CAPACITY, side=SIDE)}", flush=True)

        while True:
            data = self.api.get_assigned_accounts()
            accounts = data.get("accounts") or []
            if accounts:
                self.accounts = [
                    VmAccount(
                        login=str(x["login"]),
                        password=str(x["password"]),
                        has_mafile=bool(x.get("has_mafile", False)),
                        mafile_path=x.get("mafile_path"),
                    )
                    for x in accounts
                ]
                break
            print("[VM] waiting assigned accounts...", flush=True)
            time.sleep(1.0)

        print(f"[VM] assigned accounts count={len(self.accounts)}", flush=True)

    def _launch_account(self, acc: VmAccount) -> None:
        # keeping VM-side executor close to legacy: external launcher/process may be plugged here.
        # placeholder non-blocking process to mark startup stage.
        acc.launcher_proc = subprocess.Popen(["cmd", "/c", "echo", f"launch {acc.login}"], shell=False)

    def start_accounts_if_needed(self) -> None:
        for acc in self.accounts:
            if acc.started:
                continue
            try:
                self._launch_account(acc)
                acc.started = True
                print(f"[VM] account launch started: {acc.login}", flush=True)
            except Exception:
                print(f"[VM] account launch failed: {acc.login}", flush=True)
                traceback.print_exc()

    def discover_hwnds(self) -> List[int]:
        hwnds: List[int] = []
        one = find_dota_hwnd()
        if one is not None:
            hwnds.append(int(one))
        return hwnds

    def try_register_hwnds(self) -> None:
        hwnds = self.discover_hwnds()
        if hwnds:
            for i, hwnd in enumerate(hwnds):
                if i < len(self.accounts):
                    self.accounts[i].hwnd = int(hwnd)

        if self.hwnds_registered or not hwnds:
            return

        roles = ["unknown"] * len(hwnds)
        print(f"[VM] register_hwnds <- {self.api.register_hwnds(hwnds=hwnds, roles=roles, side=SIDE)}", flush=True)
        self.hwnds_registered = True

    def should_submit_frame(self, hwnd: int) -> bool:
        now = time.time()
        last = self.last_frame_submit_ts_by_hwnd.get(int(hwnd), 0.0)
        if (now - last) < self.frame_submit_min_dt:
            return False
        self.last_frame_submit_ts_by_hwnd[int(hwnd)] = now
        return True

    def process_hwnd(self, hwnd: int) -> None:
        if not self.should_submit_frame(hwnd):
            return

        frame_rgb = self.capture.grab_window_rgb(hwnd)
        if frame_rgb is None:
            return

        self.api.submit_frame_raw(hwnd=hwnd, frame_rgb=frame_rgb)

        while True:
            cmd = self.api.get_command(hwnd)
            if cmd is None:
                break

            try:
                result = self.executor.execute(hwnd, cmd)
                self.api.ack_command(hwnd=hwnd, command_id=int(cmd["id"]), status="done", result=result)
            except Exception:
                traceback.print_exc()
                self.api.ack_command(hwnd=hwnd, command_id=int(cmd["id"]), status="failed", result=traceback.format_exc())
                break

    def tick_one(self) -> None:
        self.start_accounts_if_needed()
        self.try_register_hwnds()

        for acc in self.accounts:
            if acc.hwnd is not None:
                self.process_hwnd(acc.hwnd)


def main():
    vm = VmClientController()
    vm.bootstrap()

    while True:
        try:
            vm.tick_one()
        except Exception:
            traceback.print_exc()
            time.sleep(0.2)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
