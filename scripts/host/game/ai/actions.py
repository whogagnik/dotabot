"""One place for gameplay command throttling; no input APIs live here."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class Intent:
    kind: str  # move_screen, move_minimap, teleport_minimap, attack_screen, right_attack
    x: float
    y: float
    cooldown: float = 0.35
    repeat_after: float = 0.8
    tolerance: float = 18.0
    attack: bool = False
    emergency: bool = False


@dataclass(frozen=True)
class ActionResult:
    sent: bool
    reason: str


class ActionController:
    """A single gameplay intent per tick, with bounded duplicate suppression.

    An emergency is selected BEFORE ordinary behavior by Brain. We do not pretend
    to cancel commands already sent to the remote client. Times are supplied by
    the caller; an accepted click is not evidence of arriving or killing a unit.
    """

    def __init__(self):
        self._last_by_channel: dict[str, tuple[Intent, float]] = {}
        self.sent_this_tick: Intent | None = None
        self.last_result = ActionResult(False, "no_intent")

    def begin_tick(self) -> None:
        self.sent_this_tick = None
        self.last_result = ActionResult(False, "no_intent")

    def reset(self) -> None:
        self._last_by_channel.clear()

    def snapshot(self) -> dict:
        return {
            key: {"intent": asdict(intent), "time": now}
            for key, (intent, now) in self._last_by_channel.items()
        }

    def restore(self, state: dict) -> None:
        self._last_by_channel = {
            key: (Intent(**value["intent"]), value["time"])
            for key, value in state.items()
        }
        self.begin_tick()

    def send(
        self, intent: Intent, now: float, emit: Callable[[Intent], None]
    ) -> ActionResult:
        if self.sent_this_tick is not None:
            return self._reject("one_action_per_tick")
        channel = "attack" if intent.kind in ("attack_screen", "right_attack") else (
            "teleport" if intent.kind == "teleport_minimap" else "move"
        )
        previous = self._last_by_channel.get(channel)
        if previous is not None:
            old, sent_at = previous
            elapsed = now - sent_at
            # Switching from normal movement to retreat bypasses its old cooldown.
            interrupts = intent.emergency and not old.emergency
            if not interrupts and elapsed < intent.cooldown:
                return self._reject("cooldown")
            same = (old.kind, old.attack) == (intent.kind, intent.attack)
            close = (old.x - intent.x) ** 2 + (
                old.y - intent.y
            ) ** 2 <= intent.tolerance**2
            if (
                not interrupts
                and same
                and close
                and elapsed < max(intent.cooldown, intent.repeat_after)
            ):
                return self._reject("duplicate_wait")
        # Only commit cooldowns if the adapter successfully submitted the command.
        emit(intent)
        self._last_by_channel[channel] = (intent, now)
        self.sent_this_tick = intent
        self.last_result = ActionResult(True, "sent")
        return self.last_result

    def _reject(self, reason: str) -> ActionResult:
        self.last_result = ActionResult(False, reason)
        return self.last_result
