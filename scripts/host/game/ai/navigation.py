"""Pure map and screen geometry used by several behaviors."""
from __future__ import annotations

from math import hypot
from typing import Any, Iterable

from .models import Point, ScreenUnit, Tower, WorldState


def distance(a: Point, b: Point) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def extract_point(value: Any) -> Point | None:
    while isinstance(value, list) and value:
        value = value[0]
    if not isinstance(value, dict):
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None


def polyline(landmarks: dict[str, Any], key: str) -> tuple[Point, ...]:
    value = landmarks.get(key)
    if isinstance(value, list) and value and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, list):
        return ()
    return tuple(point for item in value if (point := extract_point(item)) is not None)


def closest_on_segment(a: Point, b: Point, p: Point) -> tuple[Point, float, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return a, 0.0, distance(a, p) ** 2
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    q = a[0] + t * dx, a[1] + t * dy
    return q, t, distance(q, p) ** 2


def project(poly: tuple[Point, ...], p: Point) -> tuple[Point, float, float] | None:
    """Return closest point, normalized progress along the whole polyline, distance²."""
    if not poly:
        return None
    if len(poly) == 1:
        return poly[0], 0.0, distance(poly[0], p) ** 2
    lengths = [distance(poly[i], poly[i + 1]) for i in range(len(poly) - 1)]
    total = sum(lengths)
    best: tuple[Point, float, float] | None = None
    passed = 0.0
    for i, segment_length in enumerate(lengths):
        q, local, d2 = closest_on_segment(poly[i], poly[i + 1], p)
        progress = 0.0 if total <= 1e-9 else (passed + local * segment_length) / total
        candidate = q, progress, d2
        if best is None or d2 < best[2]:
            best = candidate
        passed += segment_length
    return best


def point_at(poly: tuple[Point, ...], progress: float) -> Point | None:
    if not poly:
        return None
    if len(poly) == 1:
        return poly[0]
    progress = max(0.0, min(1.0, progress))
    lengths = [distance(poly[i], poly[i + 1]) for i in range(len(poly) - 1)]
    target, passed = sum(lengths) * progress, 0.0
    for i, length in enumerate(lengths):
        if target <= passed + length or i == len(lengths) - 1:
            local = 0.0 if length <= 1e-9 else (target - passed) / length
            a, b = poly[i], poly[i + 1]
            return a[0] + (b[0] - a[0]) * local, a[1] + (b[1] - a[1]) * local
        passed += length
    return poly[-1]


def nearest_lane_key(world: WorldState) -> str | None:
    if world.self_uv is None:
        return None
    candidates = []
    for key in ("lane_top", "lane_mid", "lane_bot"):
        result = project(polyline(world.landmarks, key), world.self_uv)
        if result is not None:
            candidates.append((result[2], key))
    return min(candidates)[1] if candidates else None


def assigned_lane_key(world: WorldState) -> str | None:
    """Return the Dota lane assigned to the configured role on this side.

    The map labels are absolute (``lane_top`` / ``lane_bot``), whereas safe
    lane and offlane swap between Radiant and Dire.  Do not infer this from
    the hero's current position: during the horn that makes five bots pick a
    lane non-deterministically.
    """
    side = str(world.side).lower().strip()
    role = " ".join(str(world.role).lower().replace("_", " ").split())
    if role in {"mid", "middle", "pos 2", "position 2", "2"}:
        return "lane_mid"
    safe_roles = {"carry", "hard support", "pos 1", "position 1", "pos 5", "position 5", "1", "5"}
    off_roles = {"offlane", "support", "soft support", "pos 3", "position 3", "pos 4", "position 4", "3", "4"}
    if role in safe_roles:
        return "lane_bot" if side == "radiant" else "lane_top"
    if role in off_roles:
        return "lane_top" if side == "radiant" else "lane_bot"
    return None


def nearest_t1(world: WorldState, lane_key: str | None = None,
               attach: float = 8.0) -> Tower | None:
    """Return our T1, optionally the T1 belonging to ``lane_key``.

    Selecting by hero distance is incorrect before laning (and after a
    retreat), because it can choose the T1 of another lane.  When lane
    geometry is available, project only T1s on the assigned lane and choose
    the one nearest our fountain along that lane.
    """
    if world.self_uv is None:
        return None
    towers = tuple(t for t in world.ally_towers if t.alive and t.tier in (None, 1))
    towers = towers or tuple(t for t in world.ally_towers if t.alive)
    lane = polyline(world.landmarks, lane_key) if lane_key else ()
    if len(lane) >= 2:
        base = fountain(world)
        reversed_lane = distance(lane[-1], base) < distance(lane[0], base)
        candidates: list[tuple[float, Tower]] = []
        for tower in towers:
            projected = project(lane, tower.point)
            if projected is not None and projected[2] ** .5 <= attach:
                progress = 1.0 - projected[1] if reversed_lane else projected[1]
                candidates.append((progress, tower))
        if candidates:
            return min(candidates, key=lambda item: item[0])[1]
    return min(towers, key=lambda t: distance(world.self_uv, t.point)) if towers else None


def fountain(world: WorldState) -> Point:
    for key in (f"fountain_{world.side}", f"ancient_{world.side}"):
        point = extract_point(world.landmarks.get(key))
        if point is not None:
            return point
    return (5.0, 95.0) if world.side == "radiant" else (95.0, 5.0)


def lane_target(world: WorldState, key: str, depth: float = 0.35,
                max_depth: float = 0.45, margin: float = 0.03,
                attach: float = 5.0) -> Point | None:
    lane = polyline(world.landmarks, key)
    if len(lane) < 2:
        return None
    start = fountain(world)
    end = fountain_for_enemy(world)
    reversed_lane = distance(lane[-1], start) < distance(lane[0], start)

    def progresses(towers: Iterable[Tower]) -> list[float]:
        result = []
        for tower in towers:
            if not tower.alive:
                continue
            projected = project(lane, tower.point)
            if projected is not None and projected[2] ** 0.5 <= attach:
                raw = projected[1]
                result.append(1.0 - raw if reversed_lane else raw)
        return result

    allies, enemies = progresses(world.ally_towers), progresses(world.enemy_towers)
    if not allies or not enemies:
        return None
    ally_front, enemy_front = max(allies), min(enemies)
    if enemy_front <= ally_front:
        return None
    span = enemy_front - ally_front
    target = min(ally_front + span * depth, ally_front + span * max_depth)
    lower, upper = ally_front + margin, enemy_front - margin
    if upper <= lower:
        return None
    target = max(lower, min(upper, target))
    return point_at(lane, 1.0 - target if reversed_lane else target)


def fountain_for_enemy(world: WorldState) -> Point:
    enemy = "dire" if world.side == "radiant" else "radiant"
    for key in (f"fountain_{enemy}", f"ancient_{enemy}"):
        point = extract_point(world.landmarks.get(key))
        if point is not None:
            return point
    return (95.0, 5.0) if enemy == "dire" else (5.0, 95.0)


def away_from(hero: Point, threat: Point, step: float) -> Point:
    dx, dy = hero[0] - threat[0], hero[1] - threat[1]
    norm = hypot(dx, dy)
    return hero if norm <= 1e-9 else (hero[0] + dx / norm * step, hero[1] + dy / norm * step)


def average_anchor(units: tuple[ScreenUnit, ...], hero: Point, offset: float = 0.0) -> Point | None:
    if not units:
        return None
    anchor = sum(u.x for u in units) / len(units), sum(u.y for u in units) / len(units)
    if offset == 0:
        return anchor
    return away_from(anchor, hero, -offset)


def retreat_path(start: Point | None, threat: Point | None, target: Point,
                 points: int = 6, side_offset: float = 10.0) -> tuple[Point, ...]:
    if start is None:
        return ()
    dx, dy = target[0] - start[0], target[1] - start[1]
    norm = hypot(dx, dy)
    if norm <= 1e-9:
        return ()
    side = 1.0
    if threat is not None:
        cross = dx * (threat[1] - start[1]) - dy * (threat[0] - start[0])
        side = -1.0 if cross >= 0 else 1.0
    nx, ny = -dy / norm * side, dx / norm * side
    result = []
    for i in range(1, max(2, points) + 1):
        t = i / max(2, points)
        arc = side_offset * 4.0 * t * (1.0 - t)
        result.append((start[0] + dx * t + nx * arc, start[1] + dy * t + ny * arc))
    return tuple(result)
