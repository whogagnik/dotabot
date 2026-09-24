"""Dispatch all behavior policies through one uniform interface."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .actions import Intent
from .death import DeathMemory, commit_death_action, step_dead
from .fighting import FightingMemory, step_fighting
from .jungle import JungleMemory, step_jungle
from .lane import (LaneConfig, LaneFarmPhase, LaneMemory, LaneObservation, Unit,
                   commit_lane_action, step_lane)
from .laning import LaningMemory, commit_laning, step_laning
from .models import BehaviorDecision, BrainState, WorldState
from .movement import MoveMemory, WaitMemory, step_moving, step_wait
from .navigation import assigned_lane_key, lane_target, nearest_lane_key
from .retreat import RetreatMemory, step_retreat


@dataclass(frozen=True)
class EngineMemory:
    death: DeathMemory = DeathMemory()
    laning: LaningMemory = LaningMemory()
    jungle: JungleMemory = JungleMemory()
    lane: LaneMemory = LaneMemory()
    fighting: FightingMemory = FightingMemory()
    retreat: RetreatMemory = RetreatMemory()
    moving: MoveMemory = MoveMemory()
    waiting: WaitMemory = WaitMemory()


@dataclass(frozen=True)
class EngineStep:
    memory: EngineMemory
    decision: BehaviorDecision
    detail: dict | None = None


def enter_state(memory: EngineMemory, state: BrainState, now: float) -> EngineMemory:
    if state is BrainState.DEAD:
        return replace(memory, death=DeathMemory())
    if state is BrainState.RETREAT:
        return replace(memory, retreat=RetreatMemory(phase_started=now))
    if state is BrainState.FARMING_LANE:
        return replace(memory, lane=LaneMemory())
    if state is BrainState.LANING:
        return replace(memory, laning=LaningMemory())
    if state is BrainState.FIGHTING:
        return replace(memory, fighting=FightingMemory())
    if state is BrainState.WAIT_START:
        return replace(memory, waiting=WaitMemory())
    return memory


def set_move_target(memory: EngineMemory, target, radius: float = 5.0) -> EngineMemory:
    return replace(memory, moving=MoveMemory(target=target, radius=radius))


def step_engine(state: BrainState, world: WorldState, memory: EngineMemory, now: float) -> EngineStep:
    if state is BrainState.DEAD:
        decision = step_dead(world, memory.death)
        return EngineStep(replace(memory, death=decision.memory), decision)
    if state is BrainState.LANING:
        decision = step_laning(world, memory.laning, now)
        return EngineStep(replace(memory, laning=decision.memory), decision)
    if state is BrainState.FARMING_JUNGLE:
        decision = step_jungle(world, memory.jungle, now)
        return EngineStep(replace(memory, jungle=decision.memory), decision)
    if state is BrainState.FARMING_LANE:
        return _lane_step(world, memory, now)
    if state is BrainState.FIGHTING:
        decision = step_fighting(world, memory.fighting, now)
        return EngineStep(replace(memory, fighting=decision.memory), decision)
    if state is BrainState.RETREAT:
        decision = step_retreat(world, memory.retreat, now)
        return EngineStep(replace(memory, retreat=decision.memory), decision)
    if state is BrainState.MOVING:
        decision = step_moving(world, memory.moving, now)
        return EngineStep(replace(memory, moving=decision.memory), decision)
    if state is BrainState.WAIT_START:
        decision = step_wait(world, memory.waiting, now)
        return EngineStep(replace(memory, waiting=decision.memory), decision)
    reason = "dead_wait" if state is BrainState.DEAD else "idle"
    return EngineStep(memory, BehaviorDecision(memory, reason, phase=state.name))


def commit_action(step: EngineStep, state: BrainState, sent: bool, now: float) -> EngineStep:
    intent = step.decision.intent if isinstance(step.decision.intent, Intent) else None
    if state is BrainState.FARMING_LANE:
        lane = commit_lane_action(step.memory.lane, intent, sent, now)
        return replace(step, memory=replace(step.memory, lane=lane))
    if state is BrainState.LANING:
        laning = commit_laning(step.memory.laning, intent, sent, now)
        return replace(step, memory=replace(step.memory, laning=laning))
    if state is BrainState.DEAD:
        death = commit_death_action(step.memory.death, intent, sent)
        return replace(step, memory=replace(step.memory, death=death))
    return step


def _lane_step(world: WorldState, memory: EngineMemory, now: float) -> EngineStep:
    before = memory.lane
    key = assigned_lane_key(world) or nearest_lane_key(world)
    target = before.target
    if before.phase is LaneFarmPhase.SELECT_SEGMENT and key is not None:
        target = lane_target(world, key)
    obs = LaneObservation(
        hero_screen=world.observed_hero.point if world.observed_hero else None,
        hero_uv=world.self_uv,
        target_uv=target,
        enemies=tuple(Unit(u.x, u.y, u.hp) for u in world.enemy_creeps),
        allies=tuple(Unit(u.x, u.y, u.hp) for u in world.ally_creeps),
        enemy_hero_near=world.enemy_hero_near,
        near_enemy_tower=world.near_enemy_tower,
        hp_ratio=world.hp_ratio,
    )
    cfg = LaneConfig()
    raw = step_lane(obs, before, now, cfg)
    next_state = None
    if raw.memory.phase is LaneFarmPhase.ABORT:
        next_state = BrainState.RETREAT if raw.reason in ("low_hp", "enemy_tower") else BrainState.IDLE
    elif raw.memory.phase is LaneFarmPhase.DONE:
        next_state = BrainState.IDLE
    decision = BehaviorDecision(raw.memory, raw.reason, raw.intent, next_state, raw.memory.phase.name)
    detail = {"observation": asdict(obs), "config": asdict(cfg),
              "before": _lane_dict(before), "after": _lane_dict(raw.memory)}
    return EngineStep(replace(memory, lane=raw.memory), decision, detail)


def _lane_dict(memory: LaneMemory) -> dict:
    return {**asdict(memory), "phase": memory.phase.name}
