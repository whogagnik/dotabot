"""State routing rules. Missing observations never become a death event."""
from dataclasses import dataclass, replace

from .models import BrainState, WorldState
from .navigation import distance, fountain


@dataclass(frozen=True)
class RouterMemory:
    manual_pause: bool = False
    automatic_retreat: bool = False
    retreat_hero_missing: bool = False


@dataclass(frozen=True)
class RouterDecision:
    state: BrainState
    memory: RouterMemory
    reason: str


def select_state(current: BrainState, world: WorldState, memory: RouterMemory,
                 manual: BrainState | None = None) -> RouterDecision:
    if manual is not None:
        memory = RouterMemory(manual_pause=manual is BrainState.IDLE)
        return RouterDecision(manual, memory, "manual_state")
    if memory.manual_pause:
        return RouterDecision(BrainState.IDLE, memory, "manual_pause")
    # Only an explicit detector result is a death.  A missing hero can be a
    # camera/vision issue and must preserve the current behavior.
    if world.alive is False:
        return RouterDecision(BrainState.DEAD, memory, "confirmed_dead")
    if current is BrainState.DEAD:
        return RouterDecision(BrainState.DEAD, memory, "dead_recovery")
    if world.observed_hero is None:
        if current is BrainState.RETREAT:
            return RouterDecision(
                current, replace(memory, retreat_hero_missing=True), "retreat_hero_missing"
            )
        return RouterDecision(current, memory, "hero_not_observed")
    # Death HUD can be unreadable, but a hero that vanished while retreating
    # and reappeared at the fountain is a reliable respawn transition.
    if (current is BrainState.RETREAT and memory.retreat_hero_missing
            and world.self_uv is not None
            and distance(world.self_uv, fountain(world)) <= 8.0):
        return RouterDecision(BrainState.DEAD,
                              replace(memory, retreat_hero_missing=False),
                              "retreat_respawned")
    # Lane farming records its own abort frame before handing control to retreat.
    if world.low_hp and current not in (BrainState.RETREAT, BrainState.FARMING_LANE):
        return RouterDecision(BrainState.RETREAT, replace(memory, automatic_retreat=True), "low_hp")
    if (current is BrainState.RETREAT and memory.automatic_retreat
            and world.hp_ratio is not None and world.hp_ratio >= 0.65
            and not world.enemy_hero_near):
        return RouterDecision(BrainState.LANING, replace(memory, automatic_retreat=False,
                              retreat_hero_missing=False), "retreat_complete")
    if current is BrainState.IDLE:
        return RouterDecision(BrainState.LANING, memory, "start_laning")
    return RouterDecision(current, memory, "keep_state")
