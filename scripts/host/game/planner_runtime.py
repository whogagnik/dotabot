from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Dict, Optional

from scripts.host.game.planner import Planner
from scripts.host.core.django_service import DjangoPlannerBridge


@dataclass
class PlannerRuntimeEntry:
    vm_id: str
    bridge: DjangoPlannerBridge
    planner: Optional[Planner] = None
    hwnds: list[int] | None = None
    roles: list[str] | None = None
    side: str = "radiant"


class PlannerRuntimeRegistry:
    def __init__(self):
        self._entries: Dict[str, PlannerRuntimeEntry] = {}
        self._lock = RLock()

    def register_vm(self, vm_id: str) -> PlannerRuntimeEntry:
        with self._lock:
            entry = self._entries.get(vm_id)
            if entry is not None:
                return entry

            entry = PlannerRuntimeEntry(
                vm_id=vm_id,
                bridge=DjangoPlannerBridge(vm_id=vm_id),
            )
            self._entries[vm_id] = entry
            return entry

    def attach_hwnds(
        self,
        vm_id: str,
        hwnds: list[int],
        roles: list[str],
        side: str,
        logger=None,
    ) -> PlannerRuntimeEntry:
        with self._lock:
            entry = self._entries.get(vm_id)
            if entry is None:
                entry = self.register_vm(vm_id)

            entry.hwnds = list(hwnds)
            entry.roles = list(roles)
            entry.side = side

            entry.planner = Planner(
                hwnds=hwnds,
                roles=roles,
                side=side,
                django_bridge=entry.bridge,
                logger=logger,
            )
            return entry

    def get_entry(self, vm_id: str) -> Optional[PlannerRuntimeEntry]:
        with self._lock:
            return self._entries.get(vm_id)

    def unregister_planner(self, vm_id: str) -> None:
        with self._lock:
            self._entries.pop(vm_id, None)

    def tick_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())

        for entry in entries:
            if entry.planner is not None:
                entry.planner.tick_one()


planner_runtime = PlannerRuntimeRegistry()