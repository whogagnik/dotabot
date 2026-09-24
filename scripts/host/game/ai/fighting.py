"""Small combat policy: target, assess risk, then emit at most one intent."""
from dataclasses import dataclass

from .actions import Intent
from .models import BehaviorDecision, WorldState
from .navigation import away_from


@dataclass(frozen=True)
class FightingMemory:
    pass


def step_fighting(world: WorldState, memory: FightingMemory, now: float) -> BehaviorDecision:
    if world.observed_hero is None:
        return BehaviorDecision(memory, "hero_not_observed", phase="OBSERVE")
    enemy = world.nearest_screen(world.enemy_heroes)
    if enemy is None:
        return BehaviorDecision(memory, "no_enemy_visible", phase="SEARCH")
    distance = world.screen_distance(enemy) or 0.0
    my_hp = 1.0 if world.hp_ratio is None else world.hp_ratio
    enemy_hp = enemy.hp
    risky = world.low_hp or (len(world.enemy_heroes) >= 2 and my_hp < .65 and not world.under_ally_tower)
    risky = risky or (world.near_enemy_tower and (enemy_hp is None or my_hp < .75 or enemy_hp > .35))
    risky = risky or (enemy_hp is not None and my_hp + .15 < enemy_hp and not world.under_ally_tower)
    if risky:
        intent = None
        if distance < (360 if world.under_ally_tower else 320):
            intent = Intent("move_screen", *away_from(world.observed_hero.point, enemy.point, 90), emergency=True)
        return BehaviorDecision(memory, "combat_risk", intent, phase="KITE")
    if enemy_hp is not None and enemy_hp <= .25:
        return BehaviorDecision(memory, "finish_enemy", Intent("right_attack", enemy.x, enemy.y + 10, cooldown=.25, repeat_after=.25), phase="ATTACK")
    attack_range = 270 if world.under_ally_tower else 240
    chase_range = 470 if world.under_ally_tower else 420
    if distance <= attack_range:
        return BehaviorDecision(memory, "attack_enemy", Intent("right_attack", enemy.x, enemy.y + 10, cooldown=.25, repeat_after=.25), phase="ATTACK")
    if distance <= chase_range:
        return BehaviorDecision(memory, "chase_enemy", Intent("move_screen", enemy.x, enemy.y + 10, cooldown=.3), phase="CHASE")
    return BehaviorDecision(memory, "enemy_too_far", phase="HOLD")
