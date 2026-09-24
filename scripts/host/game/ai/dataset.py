"""Optional append-only CatBoost decision dataset recorder."""
from __future__ import annotations

import csv
from pathlib import Path

from .models import BrainState, WorldState


class DatasetRecorder:
    FIELDS = ("time", "state", "phase", "reason", "hp", "hero_visible",
              "self_x", "self_y", "enemy_heroes", "enemy_creeps", "ally_creeps", "intent")

    def __init__(self, enabled: bool = False, path: str = "runs/catboost_dataset.csv", flush_every: int = 100):
        self.enabled, self.path, self.flush_every = enabled, Path(path), flush_every
        self.rows: list[dict] = []

    def add(self, world: WorldState, state: BrainState, phase: str, reason: str, intent) -> None:
        if not self.enabled:
            return
        x, y = world.self_uv or (None, None)
        self.rows.append({"time": world.t_game, "state": state.name, "phase": phase,
                          "reason": reason, "hp": world.hp_ratio,
                          "hero_visible": world.observed_hero is not None,
                          "self_x": x, "self_y": y,
                          "enemy_heroes": len(world.enemy_heroes),
                          "enemy_creeps": len(world.enemy_creeps),
                          "ally_creeps": len(world.ally_creeps),
                          "intent": getattr(intent, "kind", None)})
        if len(self.rows) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists() and self.path.stat().st_size > 0
        with self.path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, self.FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerows(self.rows)
        self.rows.clear()
