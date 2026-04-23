from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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


_HOST_CONTROLLER: Optional["HostController"] = None


def set_host_controller(controller: "HostController") -> None:
    global _HOST_CONTROLLER
    _HOST_CONTROLLER = controller


def get_host_controller() -> "HostController":
    if _HOST_CONTROLLER is None:
        raise RuntimeError("HostController is not initialized")
    return _HOST_CONTROLLER


class HostController:
    def __init__(self, logger, status_cb):
        self.logger = logger
        self.status_cb = status_cb

        self.accounts: List[Account] = []
        self.vms: Dict[str, VmState] = {}

        self.running = False
        self._lock = threading.RLock()

        self._django_started = False
        self._server_thread = None

        self.batch_size = 5
        self.session_started_at: Optional[float] = None
        self._mafile_index: Dict[str, tuple[str, dict]] = {}

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
        planner_runtime.tick_all()
        self.cleanup_stale_vms()

    def ensure_django_started(self) -> None:
        if self._django_started:
            return

        from scripts.host.core.server_runtime import start_django_server_in_thread

        self._server_thread = start_django_server_in_thread(self.logger)
        self._django_started = True
        self.logger.info("Django server started")

    def load_accounts_from_txt(self, path: str, append: bool = False) -> int:
        with self._lock:
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

                    acc = Account(login, password, self.logger, None, self.status_cb)
                    self.accounts.append(acc)
                    count += 1

        self.logger.info(f"Загружено аккаунтов из TXT: {count}")
        return count

    def build_mafile_index(self, folder: str) -> int:
        index: Dict[str, tuple[str, dict]] = {}
        root = Path(folder)
        if not root.exists():
            self._mafile_index = {}
            return 0

        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in {".mafile", ".json"}:
                continue

            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue

            keys = {p.stem.lower()}
            steamid = str(payload.get("Session", {}).get("SteamID", "")).strip()
            if steamid:
                keys.add(steamid.lower())

            for k in keys:
                index[k] = (str(p), payload)

        self._mafile_index = index
        self.logger.info(f"Mafile index built: {len(index)} keys")
        return len(index)

    def match_mafiles_to_accounts(self) -> int:
        matched = 0
        with self._lock:
            for acc in self.accounts:
                key = acc.username.lower()
                found = self._mafile_index.get(key)
                if found is None:
                    digits = "".join(ch for ch in key if ch.isdigit())
                    if digits:
                        found = self._mafile_index.get(digits)

                if found is None:
                    acc.mafile_path = None
                    acc.mafile_data = None
                    continue

                path, payload = found
                acc.attach_mafile(path, payload)
                matched += 1

        self.logger.info(f"Matched mafiles: {matched}/{len(self.accounts)}")
        return matched

    def register_vm(self, vm_id: str, capacity: int = 5, side: str = "radiant") -> VmState:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm is None:
                vm = VmState(vm_id=vm_id, capacity=int(capacity), side=side)
                self.vms[vm_id] = vm
                planner_runtime.register_vm(vm_id=vm_id)
                self.logger.info(f"VM registered: {vm_id}")
            else:
                vm.capacity = int(capacity)
                vm.side = side

            vm.is_online = True
            vm.last_ping_ts = time.time()
            return vm

    def get_unassigned_accounts(self) -> List[Account]:
        assigned = set()
        for vm in self.vms.values():
            for acc in vm.assigned_accounts:
                assigned.add(acc.username)
        return [a for a in self.accounts if a.username not in assigned]

    def assign_batch_to_vm(self, vm_id: str) -> List[Account]:
        vm = self.vms[vm_id]
        if vm.batch_sent:
            return list(vm.assigned_accounts)

        free_accounts = self.get_unassigned_accounts()
        batch = free_accounts[: min(self.batch_size, vm.capacity)]
        vm.assigned_accounts = list(batch)
        vm.batch_sent = bool(batch)
        vm.status = "accounts_assigned" if batch else "idle"

        if batch:
            self.logger.info(f"Assigned to {vm.vm_id}: {[a.username for a in batch]}")
        return batch

    def assign_batches_to_idle_vms(self) -> None:
        for vm_id, vm in self.vms.items():
            if vm.batch_sent:
                continue
            self.assign_batch_to_vm(vm_id)

    def get_vm_accounts_payload(self, vm_id: str) -> List[dict]:
        vm = self.vms[vm_id]
        return [
            {
                "login": a.username,
                "password": a.password,
                "has_mafile": bool(a.mafile_data),
                "mafile_path": a.mafile_path,
            }
            for a in vm.assigned_accounts
        ]

    def register_hwnds(
        self,
        vm_id: str,
        hwnds: List[int],
        roles: Optional[List[str]] = None,
        side: Optional[str] = None,
    ) -> None:
        vm = self.vms[vm_id]
        vm.hwnds = [int(h) for h in hwnds]
        vm.roles = list(roles or (["unknown"] * len(vm.hwnds)))
        if side:
            vm.side = side
        vm.status = "hwnds_registered"

    def activate_planners_for_ready_vms(self) -> None:
        for vm in self.vms.values():
            if vm.planner_active or not vm.hwnds:
                continue

            planner_runtime.attach_hwnds(
                vm_id=vm.vm_id,
                hwnds=vm.hwnds,
                roles=vm.roles or (["unknown"] * len(vm.hwnds)),
                side=vm.side,
                logger=self.logger,
            )
            vm.planner_active = True
            vm.status = "planner_active"
            self.logger.info(f"Planner activated for {vm.vm_id}")

    def cleanup_stale_vms(self) -> None:
        now = time.time()
        timeout = 60.0
        for vm in self.vms.values():
            vm.is_online = (now - vm.last_ping_ts) <= timeout if vm.last_ping_ts else vm.is_online

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
                    "online": vm.is_online,
                }
            )
        return rows

    def save_state(self) -> dict:
        return {
            "accounts": [
                {
                    "login": a.username,
                    "password": a.password,
                    "mafile_path": a.mafile_path,
                }
                for a in self.accounts
            ],
            "vms": {
                vm_id: {
                    "capacity": vm.capacity,
                    "side": vm.side,
                    "status": vm.status,
                    "assigned_logins": [a.username for a in vm.assigned_accounts],
                    "hwnds": list(vm.hwnds),
                    "roles": list(vm.roles),
                    "planner_active": vm.planner_active,
                    "batch_sent": vm.batch_sent,
                }
                for vm_id, vm in self.vms.items()
            },
        }

    def load_state(self, state: dict) -> None:
        with self._lock:
            by_login = {a.username: a for a in self.accounts}
            self.vms.clear()

            for vm_id, payload in (state.get("vms") or {}).items():
                vm = VmState(
                    vm_id=vm_id,
                    capacity=int(payload.get("capacity", 5)),
                    side=str(payload.get("side", "radiant")),
                    status=str(payload.get("status", "registered")),
                )
                vm.hwnds = [int(x) for x in payload.get("hwnds", [])]
                vm.roles = [str(x) for x in payload.get("roles", [])]
                vm.planner_active = bool(payload.get("planner_active", False))
                vm.batch_sent = bool(payload.get("batch_sent", False))
                vm.assigned_accounts = [
                    by_login[x]
                    for x in payload.get("assigned_logins", [])
                    if x in by_login
                ]
                self.vms[vm_id] = vm
                planner_runtime.register_vm(vm_id)


__all__ = ["HostController", "VmState", "set_host_controller", "get_host_controller"]
