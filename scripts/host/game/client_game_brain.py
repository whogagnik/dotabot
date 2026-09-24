"""Runtime adapter for the heuristic game decision engine."""
from __future__ import annotations

from dataclasses import asdict, replace
import time
from typing import TYPE_CHECKING, Callable

from scripts.host.game.ai.actions import ActionController, Intent
from scripts.host.game.ai.dataset import DatasetRecorder
from scripts.host.game.ai.engine import (EngineMemory, commit_action, enter_state,
                                         set_move_target, step_engine)
from scripts.host.game.ai.jungle import FarmPhase
from scripts.host.game.ai.lane import LaneFarmPhase
from scripts.host.game.ai.manual import ManualStateInput
from scripts.host.game.ai.models import (BrainState, CampKind, CampNode, CampState,
                                         FarmPlan, WorldState)
from scripts.host.game.ai.navigation import assigned_lane_key, lane_target, nearest_lane_key
from scripts.host.game.ai.perception import build_world
from scripts.host.game.ai.retreat import RetreatPhase
from scripts.host.game.ai.selector import RouterMemory, select_state

if TYPE_CHECKING:
    from scripts.host.game.planner import Planner, Snapshot


class Brain:
    """One bot brain with all mutable runtime state in ``memory``."""

    def __init__(self, hwnd: int, planner: "Planner", logger=None,
                 role: str = "unknown", hero_name: str = "unknown",
                 collect_catboost_dataset: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 poll_keys: bool = True, dry_run: bool = False):
        self.hwnd = hwnd
        self.pl = planner
        self.log = logger
        self.role = str(role or "unknown").lower().strip()
        self.hero_name = str(hero_name or "unknown").lower().strip()
        self._clock = clock
        self.poll_keys = poll_keys
        self.dry_run = dry_run

        self.state = BrainState.IDLE
        self.memory = EngineMemory()
        self.router_memory = RouterMemory()
        self.actions = ActionController()
        self.manual_input = ManualStateInput()
        dataset_path = "runs/catboost_dataset.csv"
        if collect_catboost_dataset:
            try:
                from scripts.host.core.config import CATBOOST_DATASET_CSV_PATH
                dataset_path = str(CATBOOST_DATASET_CSV_PATH)
            except (ImportError, AttributeError):
                pass
        self.dataset = DatasetRecorder(collect_catboost_dataset, dataset_path)

        self.debug_reason = "idle"
        self.debug_phase = "IDLE"
        self.last_intent: Intent | None = None
        self.last_trace: dict | None = None
        self.last_lane_trace: dict | None = None
        self.last_world: WorldState | None = None
        self.last_action_ts = 0.0
        self._lane_key: str | None = None

    def tick_one(self, snap: "Snapshot") -> None:
        now = self._clock()
        self.actions.begin_tick()
        self.last_intent = None
        self.last_trace = None
        self.last_lane_trace = None
        world = build_world(snap.combined, side=self._side(), role=self.role)
        self.last_world = world

        manual = self._manual_state(now)
        routed = select_state(self.state, world, self.router_memory, manual)
        self.router_memory = routed.memory
        state_before = self.state
        if routed.state is not self.state:
            self._transition(routed.state, now)

        if self.router_memory.manual_pause:
            self._finish_trace(now, state_before, "IDLE", "manual_pause", None, False, "no_intent")
            return

        if self.state is BrainState.FARMING_LANE and self.memory.lane.phase is LaneFarmPhase.SELECT_SEGMENT:
            key = self._lane_key or assigned_lane_key(world) or nearest_lane_key(world)
            target = self._build_lane_target_between_front_towers(world, None, key) if key else None
            if target is not None:
                self.memory = replace(self.memory, lane=replace(self.memory.lane, target=target))

        gate_before = self.actions.snapshot()
        state_for_step = self.state
        step = step_engine(state_for_step, world, self.memory, now)
        self.memory = step.memory
        decision = step.decision
        intent = decision.intent if isinstance(decision.intent, Intent) else None
        sent = self._send(intent, now) if intent is not None else False
        step = commit_action(step, state_for_step, sent, now)
        self.memory = step.memory

        trace = self._trace(now, state_before, decision.phase, decision.reason,
                            intent, sent, self.actions.last_result.reason, gate_before)
        if step.detail is not None:
            trace.update(step.detail)
            trace["after"] = {**asdict(self.memory.lane), "phase": self.memory.lane.phase.name}
            self.last_lane_trace = trace
        self.last_trace = trace

        self.debug_reason, self.debug_phase = decision.reason, decision.phase
        self.dataset.add(world, self.state, decision.phase, decision.reason, intent)
        if decision.next_state is not None and decision.next_state is not self.state:
            if decision.next_state is BrainState.RETREAT:
                self.router_memory = replace(self.router_memory, automatic_retreat=True)
            self._transition(decision.next_state, now)
            trace["state_after"] = self.state.name

    def set_state(self, state: BrainState, *, manual: bool = False) -> None:
        now = self._clock()
        if manual:
            self.router_memory = RouterMemory(manual_pause=state is BrainState.IDLE)
        self._transition(state, now)

    def set_move_target(self, x: float, y: float, radius: float = 5.0) -> None:
        self.memory = set_move_target(self.memory, (float(x), float(y)), radius)
        self._transition(BrainState.MOVING, self._clock())

    def _set_state(self, state: BrainState) -> None:
        self.set_state(state)

    def flush_catboost_dataset_now(self) -> None:
        self.dataset.flush()

    def _transition(self, state: BrainState, now: float) -> None:
        if state is self.state:
            return
        self.state = state
        self.memory = enter_state(self.memory, state, now)
        if self.log:
            self.log.info("[BRAIN %s] state -> %s", hex(self.hwnd), state.name)

    def _manual_state(self, now: float) -> BrainState | None:
        if not self.poll_keys:
            return None
        states = list(BrainState)
        digit = self.manual_input.poll(now, len(states))
        return states[digit - 1] if digit is not None else None

    def _send(self, intent: Intent, now: float) -> bool:
        self.last_intent = intent
        result = self.actions.send(intent, now, self._emit_intent)
        if result.sent:
            self.last_action_ts = now
        return result.sent

    def _emit_intent(self, intent: Intent) -> None:
        if self.dry_run:
            return
        x, y = int(round(intent.x)), int(round(intent.y))
        if intent.kind == "move_minimap":
            self.pl.click_minimap_pct(self.hwnd, intent.x, intent.y, attack=intent.attack)
        elif intent.kind == "move_screen":
            self.pl.click_on_screen_walk(self.hwnd, x, y, attack=intent.attack)
        elif intent.kind == "teleport_minimap":
            self.pl.teleport_to_minimap_pct(self.hwnd, intent.x, intent.y)
        elif intent.kind == "right_attack":
            self.pl.click_on_screen(self.hwnd, x, y, mouse_button="right", attack=False)
        elif intent.kind == "attack_screen":
            self.pl.click_on_screen(self.hwnd, x, y, attack=True)
        else:
            raise ValueError(f"Unknown intent kind: {intent.kind}")

    def _side(self) -> str:
        return str(getattr(self.pl, "side", "radiant") or "radiant").lower().strip()

    def _build_lane_target_between_front_towers(self, world, _unused=None, lane_key=None):
        if isinstance(world, WorldState) and lane_key:
            return lane_target(world, lane_key)
        return None

    def _trace(self, now, state_before, phase, reason, intent, sent, gate, gate_before):
        world = self.last_world
        visual = None
        if world is not None:
            hero = world.observed_hero
            visual = {"hero_screen": list(hero.point) if hero else None,
                      "hero_uv": list(world.self_uv) if world.self_uv else None,
                      "hp_ratio": world.hp_ratio,
                      "enemies": [asdict(u) for u in (*world.enemy_heroes, *world.enemy_creeps)],
                      "allies": [asdict(u) for u in (*world.ally_heroes, *world.ally_creeps)]}
        return {"now": now, "state_before": state_before.name,
                "state_after": self.state.name, "phase": phase, "reason": reason,
                "intent": asdict(intent) if intent else None, "sent": sent,
                "gate": gate, "gate_before": gate_before,
                "visual_observation": visual}

    def _finish_trace(self, now, state_before, phase, reason, intent, sent, gate):
        self.debug_reason, self.debug_phase = reason, phase
        self.last_trace = self._trace(now, state_before, phase, reason, intent, sent,
                                      gate, self.actions.snapshot())

    @property
    def _manual_idle(self):
        return self.router_memory.manual_pause

    @_manual_idle.setter
    def _manual_idle(self, value):
        self.router_memory = replace(self.router_memory, manual_pause=bool(value))

    @property
    def _automatic_retreat(self):
        return self.router_memory.automatic_retreat

    @_automatic_retreat.setter
    def _automatic_retreat(self, value):
        self.router_memory = replace(self.router_memory, automatic_retreat=bool(value))

    @property
    def _lane_memory(self):
        return self.memory.lane

    @_lane_memory.setter
    def _lane_memory(self, value):
        self.memory = replace(self.memory, lane=value)

    @property
    def _lane_farm_phase(self):
        return self.memory.lane.phase

    @property
    def _moving_point(self):
        return self.memory.moving.target

    @_moving_point.setter
    def _moving_point(self, value):
        self.memory = set_move_target(self.memory, value, self.memory.moving.radius)

    @property
    def _farm_phase(self):
        return self.memory.jungle.phase

    @_farm_phase.setter
    def _farm_phase(self, value):
        self.memory = replace(self.memory, jungle=replace(self.memory.jungle, phase=value))

    @property
    def _farm_target_id(self):
        return self.memory.jungle.target_id

    @_farm_target_id.setter
    def _farm_target_id(self, value):
        self.memory = replace(self.memory, jungle=replace(self.memory.jungle, target_id=value))

    @property
    def _camps(self):
        return list(self.memory.jungle.camps)

    @_camps.setter
    def _camps(self, value):
        self.memory = replace(self.memory, jungle=replace(self.memory.jungle, camps=tuple(value)))

    @property
    def _camps_inited(self):
        return self.memory.jungle.initialized

    @_camps_inited.setter
    def _camps_inited(self, value):
        self.memory = replace(self.memory, jungle=replace(self.memory.jungle, initialized=bool(value)))

    @property
    def _farm_fight_started_near_target(self):
        return self.memory.jungle.fight_started_near_target

    @_farm_fight_started_near_target.setter
    def _farm_fight_started_near_target(self, value):
        self.memory = replace(self.memory, jungle=replace(self.memory.jungle,
                                                           fight_started_near_target=bool(value)))


__all__ = ["Brain", "BrainState", "CampKind", "CampNode", "CampState",
           "FarmPhase", "FarmPlan", "LaneFarmPhase", "RetreatPhase"]
