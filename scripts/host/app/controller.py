from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from scripts.host.core.account import Account
from scripts.host.game.planner_runtime import planner_runtime


@dataclass
class VmState:
    vm_id: str
    capacity: int = 5
    side: str = "radiant"
    status: str = "registered"
    is_online: bool = True
    last_ping_ts: float = 0.0

    assigned_accounts: List[Account] = field(default_factory=list)
    hwnds: List[int] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)

    planner_active: bool = False
    batch_sent: bool = False


class HostController:
    def __init__(self, logger, status_cb):
        self.logger = logger
        self.status_cb = status_cb

        self.accounts: List[Account] = []
        self.vms: Dict[str, VmState] = {}

        self.running = False
        self._lock = threading.RLock()

        self._django_started = False
        self._server_thread: Optional[threading.Thread] = None

        self.batch_size = 5
        self.session_started_at: Optional[float] = None

    # -------------------------------------------------------------
    # lifecycle
    # -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self.running = True
            if self.session_started_at is None:
                self.session_started_at = time.time()

    def stop(self) -> None:
        with self._lock:
            self.running = False

    def tick_one(self) -> None:
        if not self.running:
            return

        self.ensure_django_started()
        self.assign_batches_to_idle_vms()
        self.activate_planners_for_ready_vms()
        self.tick_planners()
        self.cleanup_stale_vms()

    # -------------------------------------------------------------
    # django
    # -------------------------------------------------------------

    def ensure_django_started(self) -> None:
        if self._django_started:
            return

        from scripts.host.core.server_runtime import start_django_server_in_thread

        self._server_thread = start_django_server_in_thread(self.logger)
        self._django_started = True
        self.logger.info("Django server started")

    # -------------------------------------------------------------
    # accounts
    # -------------------------------------------------------------
    def load_accounts_from_txt(self, path: str, append: bool = False) -> None:
        if not append:
            self.accounts.clear()

        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue

                login, password = line.split(":", 1)

                if any(a.username == login for a in self.accounts):
                    continue

                acc = Account(
                    login,
                    password,
                    self.logger,
                    None,
                    self.status_cb,

                )
                self.accounts.append(acc)
                count += 1

        self.logger.info(f"Загружено аккаунтов из TXT: {count}")
    def load_accounts(self, accounts: List[Account]) -> None:
        with self._lock:
            self.accounts = list(accounts)

    def get_unassigned_accounts(self) -> List[Account]:
        assigned = set()
        for vm in self.vms.values():
            for acc in vm.assigned_accounts:
                assigned.add(acc.username)
        return [a for a in self.accounts if a.username not in assigned]

    def build_next_batch(self) -> List[Account]:
        free_accounts = self.get_unassigned_accounts()
        return free_accounts[: self.batch_size]

    # -------------------------------------------------------------
    # vm registry
    # -------------------------------------------------------------

    def register_vm(self, vm_id: str, capacity: int = 5, side: str = "radiant") -> VmState:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm is None:
                vm = VmState(vm_id=vm_id, capacity=capacity, side=side)
                self.vms[vm_id] = vm
                planner_runtime.register_vm(vm_id=vm_id)
                self.logger.info(f"VM registered: {vm_id}")
            else:
                vm.capacity = capacity
                vm.side = side
                vm.is_online = True
                vm.last_ping_ts = time.time()

            return vm

    def assign_batches_to_idle_vms(self) -> None:
        for vm in self.vms.values():
            if vm.batch_sent:
                continue

            batch = self.build_next_batch()
            if not batch:
                continue

            vm.assigned_accounts = list(batch)
            vm.batch_sent = True
            vm.status = "accounts_assigned"

            self.logger.info(
                f"Assigned to {vm.vm_id}: {[a.username for a in vm.assigned_accounts]}"
            )

    def get_vm_accounts_payload(self, vm_id: str) -> List[dict]:
        vm = self.vms[vm_id]
        return [
            {
                "login": a.username,
                "password": a.password,
            }
            for a in vm.assigned_accounts
        ]

    def register_hwnds(self, vm_id: str, hwnds: List[int], roles: Optional[List[str]] = None, side: Optional[str] = None) -> None:
        vm = self.vms[vm_id]
        vm.hwnds = list(hwnds)
        vm.roles = list(roles or (["unknown"] * len(hwnds)))
        if side:
            vm.side = side
        vm.status = "hwnds_registered"

    def activate_planners_for_ready_vms(self) -> None:
        for vm in self.vms.values():
            if vm.planner_active:
                continue
            if not vm.hwnds:
                continue

            planner_runtime.attach_hwnds(
                vm_id=vm.vm_id,
                hwnds=vm.hwnds,
                roles=vm.roles or ["unknown"] * len(vm.hwnds),
                side=vm.side,
                logger=self.logger,
            )
            vm.planner_active = True
            vm.status = "planner_active"
            self.logger.info(f"Planner activated for {vm.vm_id}")

    # -------------------------------------------------------------
    # planners
    # -------------------------------------------------------------

    def tick_planners(self) -> None:
        planner_runtime.tick_all()

    # -------------------------------------------------------------
    # maintenance
    # -------------------------------------------------------------

    def cleanup_stale_vms(self) -> None:
        # сюда потом можно добавить timeout по last_ping_ts
        pass

    # -------------------------------------------------------------
    # ui
    # -------------------------------------------------------------

    def get_vm_rows(self) -> List[dict]:
        rows = []
        for vm in self.vms.values():
            rows.append(
                {
                    "vm_id": vm.vm_id,
                    "status": vm.status,
                    "capacity": vm.capacity,
                    "accounts": len(vm.assigned_accounts),
                    "hwnds": len(vm.hwnds),
                    "planner": vm.planner_active,
                }
            )
        return rows