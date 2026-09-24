"""Recovery after a confirmed death."""

from dataclasses import dataclass, replace

from .actions import Intent
from .models import BehaviorDecision, BrainState, WorldState
from .navigation import assigned_lane_key, nearest_lane_key, nearest_t1


@dataclass(frozen=True)
class DeathMemory:
    teleport_sent: bool = False


def step_dead(world: WorldState, memory: DeathMemory) -> BehaviorDecision:
    """TP only after the game explicitly reports that the hero is alive again."""
    if world.alive is False or world.observed_hero is None:
        return BehaviorDecision(memory, "dead_wait", phase="DEAD")
    if memory.teleport_sent:
        return BehaviorDecision(memory, "respawn_recovered", next_state=BrainState.LANING,
                                phase="RESPAWN")
    lane_key = assigned_lane_key(world) or nearest_lane_key(world)
    tower = nearest_t1(world, lane_key)
    if tower is None:
        return BehaviorDecision(memory, "respawn_no_t1", next_state=BrainState.LANING,
                                phase="RESPAWN")
    return BehaviorDecision(
        memory,
        "teleport_to_t1",
        Intent("teleport_minimap", *tower.point, cooldown=1.0, repeat_after=2.0,
               tolerance=1.0),
        phase="RESPAWN",
    )


def commit_death_action(memory: DeathMemory, intent: Intent | None, sent: bool) -> DeathMemory:
    if sent and intent is not None and intent.kind == "teleport_minimap":
        return replace(memory, teleport_sent=True)
    return memory
