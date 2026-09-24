"""Deterministic laning policy."""
from dataclasses import dataclass, replace

from .actions import Intent
from .fighting import FightingMemory, step_fighting
from .models import BehaviorDecision, Point, WorldState
from .navigation import (assigned_lane_key, average_anchor, away_from, distance,
                         fountain, lane_target, nearest_lane_key, nearest_t1,
                         point_at, polyline)


@dataclass(frozen=True)
class LaningMemory:
    lane_key: str | None = None
    anchor: Point | None = None
    attack_until: float = 0.0
    wave_front: Point | None = None
    left_spawn: bool = False
    lane_arrival_frames: int = 0


def step_laning(world: WorldState, memory: LaningMemory, now: float) -> BehaviorDecision:
    if world.observed_hero is None:
        return BehaviorDecision(memory, "hero_not_observed", phase="OBSERVE")
    key = (memory.lane_key or assigned_lane_key(world) or nearest_lane_key(world)
           or nearest_lane_key(replace(world, self_uv=fountain(world))))
    anchor = memory.anchor
    if anchor is None and key is not None:
        anchor = lane_target(world, key)
    at_spawn = (world.self_uv is not None
                and distance(world.self_uv, fountain(world)) <= 10.0)
    if at_spawn:
        memory = replace(memory, left_spawn=False, lane_arrival_frames=0)
    memory = replace(memory, lane_key=key, anchor=anchor)

    # A single minimap detection outside the fountain is not proof that the
    # hero has left it. Keep screen creep detection disabled until the icon is
    # seen at the lane destination in consecutive frames.
    if not memory.left_spawn:
        target = _departure_target(world, key)
        arrived = (not at_spawn and world.self_uv is not None and target is not None
                   and distance(world.self_uv, target) <= 9.0)
        frames = memory.lane_arrival_frames + 1 if arrived else 0
        memory = replace(memory, lane_arrival_frames=frames)
        if frames >= 2 or (target is None and world.self_uv is not None
                           and distance(world.self_uv, fountain(world)) >= 25.0):
            memory = replace(memory, left_spawn=True)
        else:
            intent = (Intent("move_minimap", *target, cooldown=.8,
                             repeat_after=1.5, tolerance=1) if target else None)
            return BehaviorDecision(memory, "leave_spawn", intent, phase="GO_TO_LANE")

    hero = world.observed_hero.point
    # Screen detection is stronger than a minimap landmark: never walk toward
    # a creep or tower while an enemy tower is visible in the camera.
    if world.screen_enemy_tower:
        tower = world.nearest_screen(world.screen_enemy_towers)
        if tower is not None:
            safe_point = away_from(hero, tower.point, 220.0)
            return BehaviorDecision(
                memory, "tower_visible_keep_distance",
                Intent("move_screen", *safe_point, cooldown=.25, repeat_after=.45,
                       tolerance=20, emergency=True),
                phase="TOWER_SAFETY",
            )
        return BehaviorDecision(memory, "tower_visible_hold", phase="TOWER_SAFETY")
    # Fight visible heroes on the lane before choosing a creep to last hit.
    # Keep the current attack uninterrupted unless combat requires an escape.
    enemy = world.nearest_screen(world.enemy_heroes)
    if enemy is not None and world.screen_distance(enemy) <= (470 if world.under_ally_tower else 420):
        fight = step_fighting(world, FightingMemory(), now)
        if fight.intent is not None and (now >= memory.attack_until or fight.intent.emergency):
            return BehaviorDecision(memory, fight.reason, fight.intent, phase=fight.phase)
    if now < memory.attack_until:
        return BehaviorDecision(memory, "attack_in_progress", phase="LAST_HIT")

    if not world.ally_creeps:
        if world.enemy_creeps and world.under_ally_tower:
            target = world.nearest_screen(world.enemy_creeps)
            return BehaviorDecision(memory, "tower_farm", _attack(target), phase="SAFE_TOWER")
        tower = nearest_t1(world, key)
        intent = Intent("move_minimap", *tower.point, cooldown=.8, repeat_after=.8, tolerance=1) if tower else None
        return BehaviorDecision(memory, "fallback_to_t1", intent, phase="SAFE_TOWER")

    candidates = [u for u in world.enemy_creeps if u.hp is not None and u.hp <= .50 and world.screen_distance(u) <= 650]
    if candidates:
        target = min(candidates, key=lambda u: (u.hp, world.screen_distance(u)))
        if world.screen_distance(target) > 150:
            return BehaviorDecision(memory, "approach_lasthit", Intent("move_screen", target.x, target.y + 10), phase="LAST_HIT")
        return BehaviorDecision(memory, "last_hit", _attack(target, .25), phase="LAST_HIT")

    denies = [u for u in world.ally_creeps if u.hp is not None and u.hp <= .30 and world.screen_distance(u) <= 550]
    if denies:
        target = min(denies, key=lambda u: (u.hp, world.screen_distance(u)))
        return BehaviorDecision(memory, "deny", _attack(target, .35), phase="DENY")

    # Stand in the wave or just in front of it.  The previous "behind wave"
    # point placed our own creeps between the camera and the enemy wave.
    position = _wave_front(world, memory) or average_anchor(world.ally_creeps, hero)
    if position is not None and world.enemy_creeps:
        memory = replace(memory, wave_front=position)
    if world.enemy_hero_near:
        return BehaviorDecision(memory, "position_behind_wave", Intent("move_screen", *(position or hero)), phase="POSITION")
    return BehaviorDecision(memory, "hold_lane_position", Intent("move_screen", *(position or hero)), phase="POSITION")


def _departure_target(world: WorldState, key: str | None) -> Point | None:
    if key is None:
        return None
    base = fountain(world)
    min_departure_distance = 25.0
    target = lane_target(world, key)
    if target is not None and distance(target, base) >= min_departure_distance:
        return target
    # nearest_t1 may fall back to an inner/base tower when T1 is unavailable.
    # That click leaves the hero at spawn and enables false creep detection.
    tower = nearest_t1(replace(world, self_uv=world.self_uv or base), key)
    if (tower is not None and tower.tier == 1
            and distance(tower.point, base) >= min_departure_distance):
        return tower.point
    lane = polyline(world.landmarks, key)
    if len(lane) < 2:
        return None
    progress = .35 if distance(lane[0], base) <= distance(lane[-1], base) else .65
    return point_at(lane, progress)


def _wave_front(world: WorldState, memory: LaningMemory) -> Point | None:
    if not world.ally_creeps:
        return memory.wave_front
    ax = sum(u.x for u in world.ally_creeps) / len(world.ally_creeps)
    ay = sum(u.y for u in world.ally_creeps) / len(world.ally_creeps)
    if not world.enemy_creeps:
        # No enemy bars can mean that we backed out of vision.  Regroup in
        # the middle of our wave instead of lingering at a stale distant point.
        return ax, ay
    ex = sum(u.x for u in world.enemy_creeps) / len(world.enemy_creeps)
    ey = sum(u.y for u in world.enemy_creeps) / len(world.enemy_creeps)
    dx, dy = ex - ax, ey - ay
    length = (dx * dx + dy * dy) ** .5
    if length < 1e-6:
        return ax, ay
    # 55 px forward keeps the enemy health bars on screen without committing
    # deeply past the friendly melee creeps.
    return ax + dx / length * 55.0, ay + dy / length * 55.0


def commit_laning(memory: LaningMemory, intent: Intent | None, sent: bool, now: float) -> LaningMemory:
    if sent and intent is not None and intent.kind in ("attack_screen", "right_attack"):
        return replace(memory, attack_until=now + .8)
    return memory


def _attack(unit, cooldown: float = .8) -> Intent | None:
    return None if unit is None else Intent("attack_screen", unit.x, unit.y + 10, cooldown=cooldown, repeat_after=cooldown)
