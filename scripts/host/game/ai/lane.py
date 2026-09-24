"""Lane farming as small, deterministic steps. Absence never proves a kill."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from math import hypot

from .actions import Intent

Point = tuple[float, float]


class LaneFarmPhase(Enum):
    SELECT_SEGMENT = auto()
    MOVE_TO_POINT = auto()
    WAIT_OR_CLEAR_WAVE = auto()
    DONE = auto()
    ABORT = auto()


@dataclass(frozen=True)
class Unit:
    x: float
    y: float
    hp: float | None = None

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class LaneObservation:
    hero_screen: Point | None
    hero_uv: Point | None
    target_uv: Point | None = None
    enemies: tuple[Unit, ...] = ()
    allies: tuple[Unit, ...] = ()
    enemy_hero_near: bool = False
    near_enemy_tower: bool = False
    hp_ratio: float | None = None


@dataclass(frozen=True)
class LaneConfig:
    arrive_radius: float = 5.0
    move_timeout: float = 15.0
    find_wave_timeout: float = 30.0
    wave_timeout: float = 22.0
    missing_wave_grace: float = 2.5
    attack_hold: float = 0.8
    low_hp: float = 0.30
    prepare_hp: float = 0.60
    seek_range: float = 550.0


@dataclass(frozen=True)
class LaneMemory:
    phase: LaneFarmPhase = LaneFarmPhase.SELECT_SEGMENT
    target: Point | None = None
    phase_started: float = 0.0
    wave_started: float | None = None
    last_wave_seen: float | None = None
    attack_until: float = 0.0
    hidden_since: float | None = None


@dataclass(frozen=True)
class LaneDecision:
    memory: LaneMemory
    reason: str
    intent: Intent | None = None


REASONS = {
    "select_target": "Выбрали участок линии — идём к нему",
    "move_to_lane": "Идём к выбранному участку линии",
    "no_lane_target": "Нет подходящего участка линии",
    "position_unknown": "Позиция на миникарте потеряна — ждём",
    "hero_not_observed": "Герой не виден — сохраняем задачу и ждём наблюдения",
    "low_hp": "Видим мало здоровья — прерываем фарм для отхода",
    "enemy_tower": "Рядом вражеская башня — прерываем фарм",
    "move_timeout": "За отведённое время не дошли — отменяем задачу",
    "wait_wave": "Ждём появления волны",
    "wait_missing_wave": "Крипы пропали — ждём, это ещё не подтверждение зачистки",
    "wave_not_visible": "Волна долго не видна — завершаем задачу, убийства не подтверждены",
    "find_wave_timeout": "Не дождались волны — завершаем поиск",
    "wave_timeout": "Истекло время работы с волной — пересматриваем задачу",
    "approach_creep": "Подходим к крипу с низким здоровьем",
    "attack_creep": "Атакуем выбранного крипа",
    "attack_in_progress": "Недавно отправили атаку — не перебиваем её движением",
    "hold_behind_allies": "Держимся за союзными крипами: рядом вражеский герой",
    "enemy_near_hold": "Враг рядом — ждём безопасной возможности",
    "push_wave": "Врага рядом не видно — атакуем ближайшего крипа",
    "follow_allies": "Следуем за союзной волной",
    "finished": "Задача завершена; ждём нового назначения",
}


def step_lane(
    obs: LaneObservation, memory: LaneMemory, now: float, cfg: LaneConfig = LaneConfig()
) -> LaneDecision:
    """No I/O, clock reads, random choices or in-place mutation."""
    if memory.phase in (LaneFarmPhase.DONE, LaneFarmPhase.ABORT):
        return LaneDecision(memory, "finished")
    # Screen actions and disappearance reasoning require observing our hero.
    if obs.hero_screen is None:
        hidden = memory.hidden_since if memory.hidden_since is not None else now
        return LaneDecision(replace(memory, hidden_since=hidden), "hero_not_observed")
    if memory.hidden_since is not None:
        pause = max(0.0, now - memory.hidden_since)
        memory = replace(
            memory,
            hidden_since=None,
            phase_started=memory.phase_started + pause,
            wave_started=(
                None if memory.wave_started is None else memory.wave_started + pause
            ),
            last_wave_seen=(
                None if memory.last_wave_seen is None else memory.last_wave_seen + pause
            ),
        )
    if obs.hp_ratio is not None and obs.hp_ratio < cfg.low_hp:
        return LaneDecision(replace(memory, phase=LaneFarmPhase.ABORT), "low_hp")
    if obs.near_enemy_tower:
        return LaneDecision(replace(memory, phase=LaneFarmPhase.ABORT), "enemy_tower")
    if memory.phase is LaneFarmPhase.SELECT_SEGMENT:
        return _select(obs, memory, now)
    if memory.phase is LaneFarmPhase.MOVE_TO_POINT:
        return _move(obs, memory, now, cfg)
    return _wave(obs, memory, now, cfg)


def _select(obs: LaneObservation, m: LaneMemory, now: float) -> LaneDecision:
    if obs.target_uv is None:
        return LaneDecision(replace(m, phase=LaneFarmPhase.ABORT), "no_lane_target")
    m = replace(
        m, phase=LaneFarmPhase.MOVE_TO_POINT, target=obs.target_uv, phase_started=now
    )
    if obs.hero_uv is None:
        return LaneDecision(m, "position_unknown")
    return LaneDecision(m, "select_target", _minimap(m.target))


def _move(
    obs: LaneObservation, m: LaneMemory, now: float, cfg: LaneConfig
) -> LaneDecision:
    if m.target is None:
        return LaneDecision(replace(m, phase=LaneFarmPhase.ABORT), "no_lane_target")
    if (
        obs.hero_uv is not None
        and hypot(obs.hero_uv[0] - m.target[0], obs.hero_uv[1] - m.target[1])
        <= cfg.arrive_radius
    ):
        m = replace(m, phase=LaneFarmPhase.WAIT_OR_CLEAR_WAVE, phase_started=now)
        return _wave(obs, m, now, cfg)
    if now - m.phase_started >= cfg.move_timeout:
        return LaneDecision(replace(m, phase=LaneFarmPhase.ABORT), "move_timeout")
    if obs.hero_uv is None:
        return LaneDecision(m, "position_unknown")
    return LaneDecision(m, "move_to_lane", _minimap(m.target))


def _wave(
    obs: LaneObservation, m: LaneMemory, now: float, cfg: LaneConfig
) -> LaneDecision:
    if obs.enemies:
        m = replace(
            m,
            last_wave_seen=now,
            wave_started=now if m.wave_started is None else m.wave_started,
        )
    if m.wave_started is not None and now - m.wave_started >= cfg.wave_timeout:
        return LaneDecision(replace(m, phase=LaneFarmPhase.DONE), "wave_timeout")
    if m.wave_started is None and now - m.phase_started >= cfg.find_wave_timeout:
        return LaneDecision(replace(m, phase=LaneFarmPhase.DONE), "find_wave_timeout")
    if now < m.attack_until:
        return LaneDecision(m, "attack_in_progress")
    if obs.enemies:
        return _combat(obs, m, cfg)
    return _no_wave(obs, m, now, cfg)


def _combat(obs: LaneObservation, m: LaneMemory, cfg: LaneConfig) -> LaneDecision:
    hx, hy = obs.hero_screen
    candidates = [
        u
        for u in obs.enemies
        if u.hp is not None
        and u.hp <= cfg.prepare_hp
        and hypot(u.x - hx, u.y - hy) <= cfg.seek_range
    ]
    if candidates:
        target = min(candidates, key=lambda u: (u.hp, hypot(u.x - hx, u.y - hy)))
        reach = 290.0 if len(obs.enemies) == 1 else 260.0
        if hypot(target.x - hx, target.y - hy) > reach:
            return LaneDecision(
                m, "approach_creep", Intent("move_screen", target.x, target.y + 10)
            )
        return LaneDecision(
            m,
            "attack_creep",
            Intent(
                "attack_screen",
                target.x,
                target.y + 10,
                cooldown=0.25,
                repeat_after=0.25,
            ),
        )
    if obs.enemy_hero_near:
        anchor = _ally_anchor(obs, 90.0)
        if anchor is not None:
            return LaneDecision(m, "hold_behind_allies", Intent("move_screen", *anchor))
        return LaneDecision(m, "enemy_near_hold")
    target = min(obs.enemies, key=lambda u: hypot(u.x - hx, u.y - hy))
    return LaneDecision(
        m,
        "push_wave",
        Intent(
            "attack_screen", target.x, target.y + 10, cooldown=0.8, repeat_after=0.8
        ),
    )


def _no_wave(
    obs: LaneObservation, m: LaneMemory, now: float, cfg: LaneConfig
) -> LaneDecision:
    if m.last_wave_seen is not None:
        if now - m.last_wave_seen >= cfg.missing_wave_grace:
            return LaneDecision(
                replace(m, phase=LaneFarmPhase.DONE), "wave_not_visible"
            )
        return LaneDecision(m, "wait_missing_wave")
    anchor = _ally_anchor(obs, -10.0)
    if anchor is not None and not obs.enemy_hero_near:
        return LaneDecision(m, "follow_allies", Intent("move_screen", *anchor))
    return LaneDecision(m, "wait_wave")


def _ally_anchor(obs: LaneObservation, offset: float) -> Point | None:
    if not obs.allies or obs.hero_screen is None:
        return None
    ax = sum(u.x for u in obs.allies) / len(obs.allies)
    ay = sum(u.y for u in obs.allies) / len(obs.allies)
    hx, hy = obs.hero_screen
    norm = hypot(hx - ax, hy - ay)
    return (
        (ax, ay)
        if norm < 1e-6
        else (ax + (hx - ax) / norm * offset, ay + (hy - ay) / norm * offset)
    )


def _minimap(point: Point) -> Intent:
    return Intent("move_minimap", *point, cooldown=0.5, repeat_after=1.0, tolerance=1.0)


def commit_lane_action(
    memory: LaneMemory,
    intent: Intent | None,
    sent: bool,
    now: float,
    cfg: LaneConfig = LaneConfig(),
) -> LaneMemory:
    """Rejected/failed submissions must not create a fictitious running attack."""
    if sent and intent is not None and intent.kind in ("attack_screen", "right_attack"):
        return replace(memory, attack_until=now + cfg.attack_hold)
    return memory
