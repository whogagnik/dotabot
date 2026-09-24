"""Simple moving and pre-game waiting policies."""
from dataclasses import dataclass, replace

from .actions import Intent
from .models import BehaviorDecision, BrainState, Point, WorldState
from .navigation import assigned_lane_key, distance, nearest_t1


@dataclass(frozen=True)
class MoveMemory:
    target: Point | None = None
    radius: float = 5.0


@dataclass(frozen=True)
class WaitMemory:
    target: Point | None = None


def step_moving(world: WorldState, memory: MoveMemory, now: float) -> BehaviorDecision:
    if memory.target is None:
        return BehaviorDecision(memory, "move_target_missing", next_state=BrainState.IDLE, phase="DONE")
    if world.self_uv is None:
        return BehaviorDecision(memory, "position_unknown", phase="MOVING")
    if distance(world.self_uv, memory.target) <= memory.radius:
        return BehaviorDecision(replace(memory, target=None), "move_arrived", next_state=BrainState.IDLE, phase="DONE")
    return BehaviorDecision(memory, "move_to_target", Intent("move_minimap", *memory.target, cooldown=.5, repeat_after=.8, tolerance=1), phase="MOVING")


def step_wait(world: WorldState, memory: WaitMemory, now: float) -> BehaviorDecision:
    if world.t_game > 110:
        return BehaviorDecision(WaitMemory(), "match_started", next_state=BrainState.IDLE, phase="DONE")
    target = memory.target
    if target is None:
        tower = nearest_t1(world, assigned_lane_key(world))
        target = tower.point if tower else None
        memory = replace(memory, target=target)
    if target is None or world.self_uv is None:
        return BehaviorDecision(memory, "wait_for_position", phase="WAIT")
    if distance(world.self_uv, target) <= 5:
        return BehaviorDecision(memory, "waiting_at_t1", phase="WAIT")
    point = target[0] + 1, target[1] + 1
    return BehaviorDecision(memory, "move_to_wait_position", Intent("move_minimap", *point, cooldown=1, repeat_after=1, tolerance=1), phase="MOVE")
