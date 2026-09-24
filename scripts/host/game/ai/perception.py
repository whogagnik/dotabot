"""Convert Planner's loose dictionaries and detector objects into one world model."""
from __future__ import annotations

from typing import Any, Iterable

from .models import MapUnit, ScreenUnit, Tower, WorldState


def build_world(combined: dict[str, Any], *, side: str, role: str) -> WorldState:
    heroes = combined.get("heroes") or {}
    creeps = combined.get("creeps") or {}
    minimap = combined.get("map") or {}
    towers = combined.get("towers") or {}
    screen_towers = combined.get("screen_towers") or {}
    self_units = _screen_units(heroes.get("self") or ())
    hud_hp = _ratio(combined.get("hp_ratio"))
    screen_hp = self_units[0].hp if self_units else None
    hp_values = [value for value in (hud_hp, screen_hp) if value is not None]
    return WorldState(
        observed_hero=self_units[0] if self_units else None,
        self_uv=_first_point(minimap.get("self") or ()),
        hp_ratio=min(hp_values) if hp_values else None,
        alive=_optional_bool(combined.get("alive")),
        t_game=_number(combined.get("t_game"), 0.0),
        hero_level=_positive_int(combined.get("hero_level")),
        side=str(side or "radiant").lower().strip(),
        role=str(role or "unknown").lower().strip(),
        enemy_heroes=_screen_units(heroes.get("enemy") or ()),
        ally_heroes=_screen_units(heroes.get("ally") or ()),
        enemy_creeps=_screen_units(creeps.get("enemy") or ()),
        ally_creeps=_screen_units(creeps.get("ally") or ()),
        enemy_uvs=tuple(unit.point for unit in _map_units(minimap.get("enemy") or ())),
        ally_uvs=tuple(unit.point for unit in _map_units(minimap.get("ally") or ())),
        enemy_towers=_towers(towers.get("enemy") or ()),
        ally_towers=_towers(towers.get("ally") or ()),
        screen_enemy_tower=bool(combined.get("screen_enemy_tower")),
        screen_enemy_towers=_screen_units(screen_towers.get("enemy") or ()),
        landmarks=_landmarks(combined.get("landmarks")),
    )


def _screen_units(items: Iterable[Any]) -> tuple[ScreenUnit, ...]:
    result = []
    for index, item in enumerate(items):
        try:
            x = (_value(item, "x0") + _value(item, "x1")) * 0.5
            y = (_value(item, "y0") + _value(item, "y1")) * 0.5
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        key = None
        for name in ("id", "uid", "track_id", "ent_id"):
            value = _raw(item, name)
            if value is not None:
                key = f"{name}:{value}"
                break
        if key is None:
            key = f"pos:{round(x)}:{round(y)}:{index}"
        result.append(ScreenUnit(x, y, _ratio(_raw(item, "hp_ratio")), key))
    return tuple(result)


def _map_units(items: Iterable[Any]) -> tuple[MapUnit, ...]:
    result = []
    for item in items:
        try:
            result.append(MapUnit(_value(item, "x"), _value(item, "y")))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return tuple(result)


def _first_point(items: Iterable[Any]) -> tuple[float, float] | None:
    units = _map_units(items)
    return units[0].point if units else None


def _towers(items: Iterable[Any]) -> tuple[Tower, ...]:
    result = []
    for item in items:
        try:
            result.append(Tower(
                _value(item, "x"), _value(item, "y"),
                _optional_int(_raw(item, "tier")),
                bool(True if _raw(item, "alive") is None else _raw(item, "alive")),
            ))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return tuple(result)


def _landmarks(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _raw(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _value(item: Any, name: str) -> float:
    value = _raw(item, name)
    if value is None:
        raise KeyError(name)
    return float(value)


def _ratio(value: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _positive_int(value: Any) -> int | None:
    result = _optional_int(value)
    return result if result is not None and result > 0 else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if value is not None else None
