from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Dict, Optional, List

from scripts.host.game.planner import Planner
from scripts.host.core.django_planner_service import DjangoPlannerBridge


@dataclass
class PlannerRuntimeEntry:
    vm_id: str
    bridge: DjangoPlannerBridge
    planner: Optional[Planner] = None
    hwnds: Optional[List[int]] = None
    roles: Optional[List[str]] = None
    side: str = "radiant"
    tick_fail_count: int = 0
    last_tick_error_log_ts: float = 0.0
    next_tick_after_ts: float = 0.0


class PlannerRuntimeRegistry:
    """
    Runtime registry:

    1. register_vm(vm_id)
       Создаёт bridge и пустую runtime entry без planner.

    2. attach_hwnds(vm_id, hwnds, roles, side)
       Создаёт Planner для уже зарегистрированной VM.

    3. tick_all()
       Тикает только те VM, у которых planner уже активирован.
    """

    def __init__(self):
        self._entries: Dict[str, PlannerRuntimeEntry] = {}
        self._lock = RLock()

    # ---------------------------------------------------------
    # vm lifecycle
    # ---------------------------------------------------------

    def register_vm(self, vm_id: str) -> PlannerRuntimeEntry:
        vm_id = str(vm_id)

        with self._lock:
            entry = self._entries.get(vm_id)
            if entry is not None:
                return entry

            entry = PlannerRuntimeEntry(
                vm_id=vm_id,
                bridge=DjangoPlannerBridge(vm_id=vm_id),
                planner=None,
                hwnds=None,
                roles=None,
                side="radiant",
            )
            self._entries[vm_id] = entry
            return entry

    def attach_hwnds(
        self,
        vm_id: str,
        hwnds: List[int],
        roles: List[str],
        side: str,
        logger=None,
    ) -> PlannerRuntimeEntry:
        vm_id = str(vm_id)
        hwnds = [int(x) for x in hwnds]
        roles = [str(x) for x in roles]
        side = str(side or "radiant")

        if len(hwnds) != len(roles):
            raise ValueError("hwnds and roles must have same length")

        with self._lock:
            entry = self._entries.get(vm_id)
            if entry is None:
                entry = self.register_vm(vm_id)

            entry.hwnds = list(hwnds)
            entry.roles = list(roles)
            entry.side = side

            entry.planner = Planner(
                hwnds=entry.hwnds,
                roles=entry.roles,
                side=entry.side,
                django_bridge=entry.bridge,
                logger=logger,
            )
            return entry

    def unregister_planner(self, vm_id: str) -> None:
        vm_id = str(vm_id)
        with self._lock:
            self._entries.pop(vm_id, None)

    # ---------------------------------------------------------
    # accessors
    # ---------------------------------------------------------

    def get_entry(self, vm_id: str) -> Optional[PlannerRuntimeEntry]:
        vm_id = str(vm_id)
        with self._lock:
            return self._entries.get(vm_id)

    def get_bridge(self, vm_id: str) -> Optional[DjangoPlannerBridge]:
        entry = self.get_entry(vm_id)
        return None if entry is None else entry.bridge

    def has_vm(self, vm_id: str) -> bool:
        vm_id = str(vm_id)
        with self._lock:
            return vm_id in self._entries

    # ---------------------------------------------------------
    # ticking
    # ---------------------------------------------------------

    def tick_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())

        for entry in entries:
            planner = entry.planner
            if planner is None:
                continue
            now = time.time()
            if entry.next_tick_after_ts > now:
                continue

            try:
                planner.tick_one()
                entry.tick_fail_count = 0
                entry.next_tick_after_ts = 0.0
            except Exception:
                entry.tick_fail_count += 1
                cooldown_sec = min(5.0, 0.25 * entry.tick_fail_count)
                now = time.time()
                entry.next_tick_after_ts = now + cooldown_sec

                should_log = (
                    entry.last_tick_error_log_ts <= 0
                    or now - entry.last_tick_error_log_ts >= min(
                        30.0,
                        max(2.0, cooldown_sec),
                    )
                )
                if not should_log:
                    continue

                entry.last_tick_error_log_ts = now
                # наружу не роняем весь цикл
                if getattr(planner, "log", None):
                    try:
                        planner.log.exception(
                            "[planner_runtime] tick failed for "
                            f"vm_id={entry.vm_id}; failures={entry.tick_fail_count}; "
                            f"cooldown={cooldown_sec:.2f}s"
                        )
                    except Exception:
                        pass

    # ---------------------------------------------------------
    # debug helpers
    # ---------------------------------------------------------

    def dump_state(self) -> List[dict]:
        with self._lock:
            entries = list(self._entries.values())

        rows: List[dict] = []
        for entry in entries:
            rows.append({
                "vm_id": entry.vm_id,
                "planner_active": entry.planner is not None,
                "hwnds": list(entry.hwnds or []),
                "roles": list(entry.roles or []),
                "side": entry.side,
            })
        return rows


planner_runtime = PlannerRuntimeRegistry()
