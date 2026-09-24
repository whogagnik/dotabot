"""Hand-written multiframe expectations for every behavior module."""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import EngineMemory
from .jungle import FarmPhase, JungleMemory
from .laning import LaningMemory
from .models import (BrainState, CampKind, CampNode, Point, ScreenUnit, Tower,
                     WorldState)
from .movement import MoveMemory


@dataclass(frozen=True)
class BehaviorFrame:
    time: float
    world: WorldState
    reason: str
    action: str | None
    phase: str
    caption: str


@dataclass(frozen=True)
class BehaviorScenario:
    name: str
    description: str
    state: BrainState
    frames: tuple[BehaviorFrame, ...]
    initial: EngineMemory = field(default_factory=EngineMemory)
    phase_flow: tuple[str, ...] = ()


def unit(x, y, hp=None):
    return ScreenUnit(float(x), float(y), hp)


def world(*, hero=(400, 260), uv=(30, 70), hp=.8, enemy_heroes=(),
          ally_heroes=(), enemy_creeps=(), ally_creeps=(), towers=(), t_game=200,
          landmarks=None):
    return WorldState(
        observed_hero=unit(*hero, hp) if hero else None,
        self_uv=uv,
        hp_ratio=hp,
        alive=None,
        t_game=t_game,
        hero_level=5,
        side="radiant",
        role="mid",
        enemy_heroes=tuple(unit(*u) for u in enemy_heroes),
        ally_heroes=tuple(unit(*u) for u in ally_heroes),
        enemy_creeps=tuple(unit(*u) for u in enemy_creeps),
        ally_creeps=tuple(unit(*u) for u in ally_creeps),
        ally_towers=tuple(Tower(*tower) for tower in towers),
        landmarks=landmarks or {"fountain_radiant": [{"x": 5, "y": 95}]},
    )


JUNGLE_START = EngineMemory(jungle=JungleMemory(
    camps=(CampNode(0, CampKind.SMALL, 30, 70),), initialized=True,
    reset_minute=3,
))


BEHAVIOR_SCENARIOS = (
    BehaviorScenario(
        "Лайнинг: безопасность → ластхит",
        "Без союзной волны бот отходит к T1, затем подходит к крипу и атакует один раз.",
        BrainState.LANING,
        (
            BehaviorFrame(0, world(towers=((25, 75, 1, True),)), "fallback_to_t1", "move_minimap", "SAFE_TOWER", "Союзных крипов нет: выбрать безопасную T1."),
            BehaviorFrame(.9, world(enemy_creeps=((700, 260, .4),), ally_creeps=((500, 260, .8),)), "approach_lasthit", "move_screen", "LAST_HIT", "Крип подходит под порог здоровья, но ещё далеко."),
            BehaviorFrame(1.4, world(enemy_creeps=((530, 260, .2),), ally_creeps=((500, 260, .8),)), "last_hit", "attack_screen", "LAST_HIT", "Цель в радиусе: отправить одну атаку."),
            BehaviorFrame(1.7, world(enemy_creeps=((530, 260, .1),), ally_creeps=((500, 260, .8),)), "attack_in_progress", None, "LAST_HIT", "Не перебивать только что отправленную атаку."),
        ),
        initial=EngineMemory(laning=LaningMemory(left_spawn=True)),
        phase_flow=("SAFE_TOWER", "LAST_HIT", "DENY", "POSITION"),
    ),
    BehaviorScenario(
        "Лес: прийти → драться → потерять наблюдение",
        "Исчезновение нейтралов завершает визит, но не подтверждает зачистку лагеря.",
        BrainState.FARMING_JUNGLE,
        (
            BehaviorFrame(0, world(), "select_camp", "move_minimap", "MOVE_TO_TARGET", "Выбрать ближайший доступный лагерь."),
            BehaviorFrame(1, world(enemy_creeps=((520, 260, .8),)), "fight_camp", "attack_screen", "FIGHT", "У лагеря видны нейтралы: атаковать."),
            BehaviorFrame(1.3, world(), "wait_missing_camp", None, "FIGHT", "Нейтралы исчезли: пока только ждать."),
            BehaviorFrame(2.6, world(), "camp_disappeared", None, "PICK_TARGET", "После паузы отметить VISITED, не CLEARED."),
            BehaviorFrame(3, world(), "no_ready_camps", None, "PICK_TARGET", "В этой минуте доступных лагерей больше нет."),
        ),
        initial=JUNGLE_START,
        phase_flow=("PICK_TARGET", "MOVE_TO_TARGET", "FIGHT"),
    ),
    BehaviorScenario(
        "Бой: погоня → атака → риск",
        "Дистанция и риск приводят к разным, видимым решениям.",
        BrainState.FIGHTING,
        (
            BehaviorFrame(0, world(enemy_heroes=((760, 260, .8),)), "chase_enemy", "move_screen", "CHASE", "Враг в радиусе погони: приблизиться."),
            BehaviorFrame(.5, world(enemy_heroes=((590, 260, .8),)), "attack_enemy", "right_attack", "ATTACK", "Враг вошёл в радиус атаки."),
            BehaviorFrame(1, world(hp=.2, enemy_heroes=((520, 260, .8),)), "combat_risk", "move_screen", "KITE", "Мало здоровья и враг близко: увеличить дистанцию."),
            BehaviorFrame(2, world(enemy_heroes=()), "no_enemy_visible", None, "SEARCH", "Враг пропал с экрана: не объявлять его мёртвым."),
        ),
        phase_flow=("SEARCH", "CHASE", "ATTACK", "KITE", "HOLD"),
    ),
    BehaviorScenario(
        "Отход: союзник → путь к фонтану",
        "Сначала спрятаться за союзником, затем идти к фонтану; видимый враг не уводит героя за край экрана.",
        BrainState.RETREAT,
        (
            BehaviorFrame(0, world(ally_heroes=((500, 260, .8),), enemy_heroes=((620, 260, .8),)), "retreat_behind_ally", "move_screen", "GO_BEHIND_ALLY", "Одноразово занять точку за союзником."),
            BehaviorFrame(1.1, world(), "follow_retreat_path", "move_minimap", "FOLLOW_PATH", "Построить дугу к фонтану и выбрать первую точку."),
            BehaviorFrame(1.4, world(enemy_heroes=((520, 260, .8),)), "follow_retreat_path", None, "FOLLOW_PATH", "Видимый враг сохраняет маршрут к фонтану вместо клика за край экрана."),
            BehaviorFrame(2, world(hero=None, uv=None, hp=None), "hero_not_observed", None, "FOLLOW_PATH", "Герой пропал: сохранить маршрут и не делать вывод о смерти."),
        ),
        phase_flow=("GO_BEHIND_ALLY", "BUILD_PATH", "FOLLOW_PATH", "DIRECT_TO_FOUNTAIN"),
    ),
    BehaviorScenario(
        "Движение к заданной точке",
        "Обычная задача движения повторяется покадрово и завершается по координатам.",
        BrainState.MOVING,
        (
            BehaviorFrame(0, world(uv=(20, 80)), "move_to_target", "move_minimap", "MOVING", "Отправить движение к заданной точке."),
            BehaviorFrame(.2, world(uv=(40, 60)), "move_to_target", None, "MOVING", "Кулдаун подавляет слишком ранний повтор."),
            BehaviorFrame(1, world(uv=(78, 22)), "move_arrived", None, "DONE", "Точка достигнута: завершить задачу."),
        ),
        initial=EngineMemory(moving=MoveMemory((80, 20), 5)),
        phase_flow=("MOVING", "DONE"),
    ),
)
