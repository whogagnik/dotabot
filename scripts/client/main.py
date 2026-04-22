from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import List, Optional

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
    hwnd: Optional[int] = None
    started: bool = False
    registered_hwnd: bool = False


class VmClientController:
    def __init__(self):
        self.api = PlannerApiClient(
            base_url=BASE_URL,
            vm_id=VM_ID,
            timeout=15.0,
            debug=DEBUG,
        )
        self.capture = DotaCapture()
        self.executor = CommandExecutor()

        self.accounts: List[VmAccount] = []
        self.hwnds_registered = False

        self.last_frame_submit_ts_by_hwnd: dict[int, float] = {}
        self.frame_submit_min_dt = 0.03  # ~33 fps max per hwnd

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        print("[VM] register_vm...", flush=True)
        reg = self.api.register_vm(capacity=CAPACITY, side=SIDE)
        print(f"[VM] register_vm <- {reg}", flush=True)

        while True:
            data = self.api.get_assigned_accounts()
            accounts = data.get("accounts") or []
            if accounts:
                self.accounts = [
                    VmAccount(login=str(x["login"]), password=str(x["password"]))
                    for x in accounts
                ]
                break

            print("[VM] waiting assigned accounts...", flush=True)
            time.sleep(1.0)

        print(f"[VM] assigned accounts count={len(self.accounts)}", flush=True)

    # ------------------------------------------------------------------
    # legacy integration points
    # ------------------------------------------------------------------

    def start_accounts_if_needed(self) -> None:
        """
        Здесь подключи свой существующий код запуска аккаунтов на VM.
        Сейчас это заглушка.

        Идея:
        - для каждого аккаунта стартуешь Steam/Dota
        - как только старт процесса начат, ставишь acc.started = True
        """
        for acc in self.accounts:
            if acc.started:
                continue

            try:
                self.start_one_account(acc)
                acc.started = True
                print(f"[VM] account started: {acc.login}", flush=True)
            except Exception:
                print(f"[VM] failed to start account: {acc.login}", flush=True)
                traceback.print_exc()

    def start_one_account(self, acc: VmAccount) -> None:
        """
        ЗАМЕНИ на интеграцию с твоим start_mm_dota2.py / legacy launcher.

        Примерно тут должен быть код:
        - логин по login/password
        - запуск Dota
        - ожидание начала UI
        """
        # TODO: подключить legacy запуск
        time.sleep(0.2)

    def discover_hwnds(self) -> List[int]:
        """
        ЗАМЕНИ на поиск всех 5 hwnd через твой существующий код.

        Сейчас fallback: пробуем найти одно окно Dota.
        """
        hwnds: List[int] = []

        one = find_dota_hwnd()
        if one is not None:
            hwnds.append(int(one))

        # TODO: заменить на реальный поиск 5 hwnd
        return hwnds

    # ------------------------------------------------------------------
    # planner registration
    # ------------------------------------------------------------------

    def try_register_hwnds(self) -> None:
        if self.hwnds_registered:
            return

        hwnds = self.discover_hwnds()
        if not hwnds:
            print("[VM] no hwnds yet", flush=True)
            return

        roles = ["unknown"] * len(hwnds)
        resp = self.api.register_hwnds(hwnds=hwnds, roles=roles, side=SIDE)
        print(f"[VM] register_hwnds <- {resp}", flush=True)

        # связываем hwnd с аккаунтами по порядку, насколько удалось
        for i, hwnd in enumerate(hwnds):
            if i < len(self.accounts):
                self.accounts[i].hwnd = int(hwnd)
                self.accounts[i].registered_hwnd = True

        self.hwnds_registered = True

    # ------------------------------------------------------------------
    # frame pipeline
    # ------------------------------------------------------------------

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
            print(f"[VM] capture None hwnd={hex(hwnd)}", flush=True)
            return

        submit_resp = self.api.submit_frame_raw(hwnd=hwnd, frame_rgb=frame_rgb)
        print(f"[VM] submit_frame_raw hwnd={hex(hwnd)} <- {submit_resp}", flush=True)

        empty_polls = 0
        max_empty_polls = 3

        while True:
            cmd = self.api.get_command(hwnd)
            if cmd is None:
                empty_polls += 1
                if empty_polls >= max_empty_polls:
                    break
                time.sleep(0.01)
                continue

            empty_polls = 0
            print(f"[VM] command hwnd={hex(hwnd)} -> {cmd}", flush=True)

            try:
                result = self.executor.execute(hwnd, cmd)
                self.api.ack_command(
                    hwnd=hwnd,
                    command_id=int(cmd["id"]),
                    status="done",
                    result=result,
                )
            except Exception:
                traceback.print_exc()
                try:
                    self.api.ack_command(
                        hwnd=hwnd,
                        command_id=int(cmd["id"]),
                        status="failed",
                        result=traceback.format_exc(),
                    )
                except Exception:
                    traceback.print_exc()
                break

    # ------------------------------------------------------------------
    # main tick
    # ------------------------------------------------------------------

    def tick_one(self) -> None:
        self.start_accounts_if_needed()
        self.try_register_hwnds()

        for acc in self.accounts:
            if acc.hwnd is None:
                continue
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