"""Jungle farming state machine. A missing creep means VISITED, never CLEARED."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto

from .actions import Intent
from .models import BehaviorDecision, CampKind, CampNode, CampState, WorldState
from .navigation import distance


class FarmPhase(Enum):
    PICK_TARGET = auto()
    MOVE_TO_TARGET = auto()
    FIGHT = auto()


@dataclass(frozen=True)
class JungleMemory:
    camps: tuple[CampNode, ...] = ()
    initialized: bool = False
    phase: FarmPhase = FarmPhase.PICK_TARGET
    target_id: int | None = None
    fight_started_near_target: bool = False
    missing_since: float | None = None
    arrived_at: float | None = None
    reset_minute: int = -1


def step_jungle(world: WorldState, memory: JungleMemory, now: float) -> BehaviorDecision:
    if world.observed_hero is None:
        return BehaviorDecision(memory, "hero_not_observed", phase=memory.phase.name)
    memory = _initialize(memory, world)
    memory = _reset_on_minute(memory, world.t_game)
    if not memory.camps:
        return BehaviorDecision(memory, "no_camps", phase=memory.phase.name)

    target = _camp(memory, memory.target_id)
    if memory.phase is FarmPhase.PICK_TARGET or target is None:
        ready = [camp for camp in memory.camps if camp.state is CampState.READY]
        if not ready:
            return BehaviorDecision(replace(memory, target_id=None), "no_ready_camps", phase="PICK_TARGET")
        if world.self_uv is None:
            return BehaviorDecision(memory, "position_unknown", phase="PICK_TARGET")
        target = min(ready, key=lambda camp: distance(world.self_uv, camp.point))
        memory = replace(memory, phase=FarmPhase.MOVE_TO_TARGET, target_id=target.camp_id,
                         missing_since=None, arrived_at=None, fight_started_near_target=False)
        return BehaviorDecision(memory, "select_camp", _move(target), phase=memory.phase.name)

    if memory.phase is FarmPhase.MOVE_TO_TARGET:
        if world.self_uv is None:
            return BehaviorDecision(memory, "position_unknown", phase=memory.phase.name)
        near = distance(world.self_uv, target.point) <= 6.0
        if world.enemy_creeps and near:
            memory = replace(memory, phase=FarmPhase.FIGHT, fight_started_near_target=True,
                             missing_since=None, arrived_at=memory.arrived_at or now)
            return _fight(world, memory, target, now)
        if near:
            arrived = memory.arrived_at if memory.arrived_at is not None else now
            memory = replace(memory, arrived_at=arrived)
            if now - arrived >= 2.0:
                return BehaviorDecision(_finish_visit(memory, CampState.SKIPPED), "camp_empty_or_stuck", phase="PICK_TARGET")
            return BehaviorDecision(memory, "wait_for_camp", phase=memory.phase.name)
        return BehaviorDecision(memory, "move_to_camp", _move(target), phase=memory.phase.name)

    return _fight(world, memory, target, now)


def _fight(world: WorldState, memory: JungleMemory, target: CampNode, now: float) -> BehaviorDecision:
    if world.enemy_creeps:
        creep = world.nearest_screen(world.enemy_creeps)
        memory = replace(memory, missing_since=None)
        intent = None if creep is None else Intent("attack_screen", creep.x, creep.y + 10, cooldown=.6, repeat_after=.6)
        return BehaviorDecision(memory, "fight_camp", intent, phase="FIGHT")
    missing = memory.missing_since if memory.missing_since is not None else now
    memory = replace(memory, missing_since=missing)
    if memory.fight_started_near_target and now - missing >= 1.2:
        return BehaviorDecision(_finish_visit(memory, CampState.VISITED), "camp_disappeared", phase="PICK_TARGET")
    return BehaviorDecision(memory, "wait_missing_camp", phase="FIGHT")


def _initialize(memory: JungleMemory, world: WorldState) -> JungleMemory:
    if memory.initialized:
        return memory
    camps, camp_id = [], 0
    for key, kind in (("camp_small", CampKind.SMALL), ("camp_medium", CampKind.MEDIUM), ("camp_large", CampKind.LARGE)):
        value = world.landmarks.get(key) or []
        if isinstance(value, list) and value and isinstance(value[0], list):
            value = value[0]
        for point in value if isinstance(value, list) else ():
            try:
                camps.append(CampNode(camp_id, kind, float(point["x"]), float(point["y"])))
                camp_id += 1
            except (KeyError, TypeError, ValueError):
                continue
    return replace(memory, camps=tuple(camps), initialized=True)


def _reset_on_minute(memory: JungleMemory, t_game: float) -> JungleMemory:
    minute = max(0, int(t_game // 60))
    if memory.reset_minute < 0:
        return replace(memory, reset_minute=minute)
    if minute <= memory.reset_minute:
        return memory
    camps = tuple(replace(camp, state=CampState.READY) for camp in memory.camps)
    return replace(memory, camps=camps, reset_minute=minute, phase=FarmPhase.PICK_TARGET,
                   target_id=None, missing_since=None, arrived_at=None)


def _camp(memory: JungleMemory, camp_id: int | None) -> CampNode | None:
    return next((camp for camp in memory.camps if camp.camp_id == camp_id), None)


def _move(camp: CampNode) -> Intent:
    return Intent("move_minimap", camp.x, camp.y, cooldown=.8, repeat_after=1.5, tolerance=1)


def _finish_visit(memory: JungleMemory, state: CampState) -> JungleMemory:
    camps = tuple(replace(camp, state=state) if camp.camp_id == memory.target_id else camp for camp in memory.camps)
    return replace(memory, camps=camps, phase=FarmPhase.PICK_TARGET, target_id=None,
                   fight_started_near_target=False, missing_since=None, arrived_at=None)
