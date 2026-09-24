"""Retreat policy with explicit phases and observable memory."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto

from .actions import Intent
from .models import BehaviorDecision, Point, WorldState
from .navigation import away_from, distance, fountain


SHORT_ESCAPE_DISTANCE = 22.0


class RetreatPhase(Enum):
    GO_BEHIND_ALLY = auto()
    BUILD_PATH = auto()
    FOLLOW_PATH = auto()
    DIRECT_TO_FOUNTAIN = auto()


@dataclass(frozen=True)
class RetreatMemory:
    phase: RetreatPhase = RetreatPhase.GO_BEHIND_ALLY
    phase_started: float = 0.0
    enemy_memory_until: float = 0.0
    path: tuple[Point, ...] = ()
    path_index: int = 0
    path_built_at: float = 0.0
    start_uv: Point | None = None
    escape_target: Point | None = None


def step_retreat(world: WorldState, memory: RetreatMemory, now: float) -> BehaviorDecision:
    if world.observed_hero is None:
        return BehaviorDecision(memory, "hero_not_observed", phase=memory.phase.name)
    enemy_screen = world.nearest_screen(world.enemy_heroes)
    enemy_uv = world.nearest_uv(world.enemy_uvs)
    if enemy_screen is not None or enemy_uv is not None:
        memory = replace(memory, enemy_memory_until=now + 2.0)
    target = fountain(world)
    if memory.start_uv is None and world.self_uv is not None:
        memory = replace(memory, start_uv=world.self_uv)
    if world.self_uv is not None and distance(world.self_uv, target) <= 3.0:
        memory = replace(memory, phase=RetreatPhase.DIRECT_TO_FOUNTAIN, path=(), path_index=0)
        return BehaviorDecision(memory, "at_fountain", phase="SAFE")

    if memory.phase is RetreatPhase.GO_BEHIND_ALLY:
        ally = world.nearest_screen(world.ally_heroes)
        if ally is not None and world.screen_distance(ally) <= 320 and now - memory.phase_started < 1.0:
            threat = enemy_screen.point if enemy_screen else world.observed_hero.point
            anchor = away_from(ally.point, threat, 120)
            return BehaviorDecision(memory, "retreat_behind_ally", _screen(anchor), phase=memory.phase.name)
        memory = replace(memory, phase=RetreatPhase.BUILD_PATH, phase_started=now)

    if memory.phase is RetreatPhase.BUILD_PATH:
        start = memory.start_uv
        if start is None or world.self_uv is None:
            memory = replace(memory, phase=RetreatPhase.DIRECT_TO_FOUNTAIN, phase_started=now)
        elif distance(world.self_uv, start) >= SHORT_ESCAPE_DISTANCE:
            memory = replace(memory, phase=RetreatPhase.DIRECT_TO_FOUNTAIN, phase_started=now)
        else:
            threat = enemy_uv if enemy_uv is not None or now <= memory.enemy_memory_until else None
            # One short safety step only.  After it we click the fountain
            # directly instead of repeatedly building a long curved route.
            escape_target = away_from(start, threat, SHORT_ESCAPE_DISTANCE) if threat else away_from(
                start, target, -SHORT_ESCAPE_DISTANCE
            )
            memory = replace(
                memory, phase=RetreatPhase.FOLLOW_PATH, path=(escape_target,),
                path_index=0, path_built_at=now, phase_started=now,
                escape_target=escape_target,
            )

    if memory.phase is RetreatPhase.FOLLOW_PATH:
        start, escape_target = memory.start_uv, memory.escape_target
        if (start is None or escape_target is None or world.self_uv is None
                or distance(world.self_uv, start) >= SHORT_ESCAPE_DISTANCE
                or distance(world.self_uv, escape_target) <= 3.0):
            memory = replace(memory, phase=RetreatPhase.DIRECT_TO_FOUNTAIN,
                             path_index=1, phase_started=now)
        else:
            return BehaviorDecision(memory, "follow_retreat_path", _minimap(escape_target),
                                    phase=memory.phase.name)

    return BehaviorDecision(memory, "retreat_to_fountain", _minimap(target), phase=RetreatPhase.DIRECT_TO_FOUNTAIN.name)


def _screen(point: Point) -> Intent:
    return Intent("move_screen", *point, cooldown=.15, repeat_after=.3, tolerance=10, emergency=True)


def _minimap(point: Point) -> Intent:
    return Intent("move_minimap", *point, cooldown=.55, repeat_after=.8, tolerance=1, emergency=True)
