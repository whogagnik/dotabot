"""Stable data contracts shared by perception, behaviors and the Brain runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from math import hypot
from typing import Any

Point = tuple[float, float]


class BrainState(Enum):
    IDLE = auto()
    LANING = auto()
    FARMING_JUNGLE = auto()
    FARMING_LANE = auto()
    MOVING = auto()
    FIGHTING = auto()
    RETREAT = auto()
    DEAD = auto()
    WAIT_START = auto()


class CampKind(Enum):
    SMALL = auto()
    MEDIUM = auto()
    LARGE = auto()


class CampState(Enum):
    READY = auto()
    CLEARED = auto()  # Reserved for a future positive confirmation signal.
    SKIPPED = auto()
    VISITED = auto()


class FarmPlan(Enum):
    LANE_THEN_JUNGLE = auto()
    JUNGLE_ONLY = auto()


@dataclass(frozen=True)
class ScreenUnit:
    x: float
    y: float
    hp: float | None = None
    key: str | None = None

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class MapUnit:
    x: float
    y: float

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class Tower:
    x: float
    y: float
    tier: int | None = None
    alive: bool = True

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class CampNode:
    camp_id: int
    kind: CampKind
    x: float
    y: float
    state: CampState = CampState.READY

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class WorldState:
    observed_hero: ScreenUnit | None
    self_uv: Point | None
    hp_ratio: float | None
    alive: bool | None
    t_game: float
    hero_level: int | None
    side: str
    role: str
    enemy_heroes: tuple[ScreenUnit, ...] = ()
    ally_heroes: tuple[ScreenUnit, ...] = ()
    enemy_creeps: tuple[ScreenUnit, ...] = ()
    ally_creeps: tuple[ScreenUnit, ...] = ()
    enemy_uvs: tuple[Point, ...] = ()
    ally_uvs: tuple[Point, ...] = ()
    enemy_towers: tuple[Tower, ...] = ()
    ally_towers: tuple[Tower, ...] = ()
    screen_enemy_tower: bool = False
    screen_enemy_towers: tuple[ScreenUnit, ...] = ()
    landmarks: dict[str, Any] = field(default_factory=dict)

    @property
    def low_hp(self) -> bool:
        return self.hp_ratio is not None and self.hp_ratio < 0.30

    def nearest_screen(self, units: tuple[ScreenUnit, ...]) -> ScreenUnit | None:
        if self.observed_hero is None or not units:
            return None
        hx, hy = self.observed_hero.point
        return min(units, key=lambda unit: hypot(unit.x - hx, unit.y - hy))

    def screen_distance(self, unit: ScreenUnit | None) -> float | None:
        if self.observed_hero is None or unit is None:
            return None
        return hypot(unit.x - self.observed_hero.x, unit.y - self.observed_hero.y)

    def nearest_uv(self, points: tuple[Point, ...]) -> Point | None:
        if self.self_uv is None or not points:
            return None
        sx, sy = self.self_uv
        return min(points, key=lambda p: hypot(p[0] - sx, p[1] - sy))

    def uv_distance(self, point: Point | None) -> float | None:
        if self.self_uv is None or point is None:
            return None
        return hypot(point[0] - self.self_uv[0], point[1] - self.self_uv[1])

    def nearest_tower(self, towers: tuple[Tower, ...]) -> Tower | None:
        alive = tuple(tower for tower in towers if tower.alive)
        if self.self_uv is None or not alive:
            return None
        sx, sy = self.self_uv
        return min(alive, key=lambda tower: hypot(tower.x - sx, tower.y - sy))

    @property
    def enemy_hero_near(self) -> bool:
        distance = self.uv_distance(self.nearest_uv(self.enemy_uvs))
        return bool(self.enemy_heroes) or (distance is not None and distance <= 10.0)

    @property
    def under_ally_tower(self) -> bool:
        tower = self.nearest_tower(self.ally_towers)
        distance = self.uv_distance(tower.point if tower else None)
        return distance is not None and distance <= 7.0

    @property
    def near_enemy_tower(self) -> bool:
        if self.screen_enemy_tower:
            return True
        tower = self.nearest_tower(self.enemy_towers)
        distance = self.uv_distance(tower.point if tower else None)
        return distance is not None and distance <= 7.0


@dataclass(frozen=True)
class BehaviorDecision:
    memory: object
    reason: str
    intent: object | None = None
    next_state: BrainState | None = None
    phase: str = ""


@dataclass(frozen=True)
class BrainTrace:
    state_before: BrainState
    state_after: BrainState
    phase: str
    reason: str
    intent: object | None
    sent: bool
    gate: str
