"""Hand-authored multi-frame expectations shared by tests and visual playback.

These are scripted observations, not a physics simulator or vision accuracy test.
Changing an implementation must not silently regenerate these expectations.
"""

from dataclasses import dataclass, field

from .lane import LaneMemory, LaneObservation, LaneFarmPhase, Unit


@dataclass(frozen=True)
class Frame:
    time: float
    observation: LaneObservation
    reason: str
    action: str | None
    phase: str
    caption: str


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    frames: tuple[Frame, ...]
    initial: LaneMemory = field(default_factory=LaneMemory)


def scene(
    *,
    uv=(30.0, 70.0),
    hero=(400.0, 260.0),
    enemies=(),
    allies=(),
    hp=0.8,
    danger=False,
    tower=False,
    target=(60.0, 40.0)
):
    return LaneObservation(
        hero,
        uv,
        target,
        tuple(Unit(*u) for u in enemies),
        tuple(Unit(*u) for u in allies),
        danger,
        tower,
        hp,
    )


MOVE = "MOVE_TO_POINT"
WAVE = "WAIT_OR_CLEAR_WAVE"
ACTIVE = LaneMemory(LaneFarmPhase.WAIT_OR_CLEAR_WAVE, (60, 40), 0.0)


SCENARIOS = (
    Scenario(
        "Подойти → атаковать → потерять волну",
        "Полный путь задачи, повтор движения и ожидание после атаки.",
        (
            Frame(
                0,
                scene(),
                "select_target",
                "move_minimap",
                MOVE,
                "Выбрать участок и отправить движение по миникарте.",
            ),
            Frame(
                0.2,
                scene(),
                "move_to_lane",
                None,
                MOVE,
                "Не повторять команду через 0,2 секунды.",
            ),
            Frame(
                1.1,
                scene(uv=(43, 57)),
                "move_to_lane",
                "move_minimap",
                MOVE,
                "Повтор уже разрешён: старая цель не блокируется навсегда.",
            ),
            Frame(
                2,
                scene(uv=(60, 40), enemies=((760, 260, 0.4),)),
                "approach_creep",
                "move_screen",
                WAVE,
                "Дошли до участка. Крип далеко — подойти.",
            ),
            Frame(
                2.5,
                scene(uv=(60, 40), enemies=((630, 260, 0.3),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Крип в пределах порога дистанции — атаковать.",
            ),
            Frame(
                2.8,
                scene(uv=(60, 40), enemies=((635, 260, 0.2),)),
                "attack_in_progress",
                None,
                WAVE,
                "Не перебивать недавно отправленную атаку.",
            ),
            Frame(
                3.4,
                scene(uv=(60, 40)),
                "wait_missing_wave",
                None,
                WAVE,
                "Крип исчез. Это не доказательство убийства.",
            ),
            Frame(
                5.4,
                scene(uv=(60, 40)),
                "wave_not_visible",
                None,
                "DONE",
                "Волна долго не видна. Завершить задачу без заявления о зачистке.",
            ),
            Frame(
                6,
                scene(uv=(60, 40)),
                "finished",
                None,
                "DONE",
                "Завершённая задача не запускается сама снова.",
            ),
        ),
    ),
    Scenario(
        "Герой исчезает и возвращается",
        "Память сохраняется. Время без наблюдения не превращается в смерть или таймаут.",
        (
            Frame(
                0, scene(), "select_target", "move_minimap", MOVE, "Начать движение."
            ),
            Frame(
                0.5,
                scene(hero=None, uv=None, hp=None),
                "hero_not_observed",
                None,
                MOVE,
                "Пропал герой — не объявлять DEAD, не очищать цель.",
            ),
            Frame(
                20,
                scene(hero=None, uv=None, hp=None),
                "hero_not_observed",
                None,
                MOVE,
                "Длительная пропажа также не доказывает смерть.",
            ),
            Frame(
                21,
                scene(uv=(45, 55)),
                "move_to_lane",
                "move_minimap",
                MOVE,
                "Герой вернулся — продолжить прежнюю задачу.",
            ),
            Frame(
                22, scene(uv=(60, 40)), "wait_wave", None, WAVE, "Дошли — ждать волну."
            ),
        ),
    ),
    Scenario(
        "Крипы пропадают на один кадр",
        "Краткий провал наблюдения не завершает задачу.",
        (
            Frame(
                0,
                scene(uv=(60, 40), enemies=((620, 260, 0.4),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Отправить атаку.",
            ),
            Frame(
                0.2,
                scene(uv=(60, 40)),
                "attack_in_progress",
                None,
                WAVE,
                "Цель исчезла, но ещё идёт ожидание атаки.",
            ),
            Frame(
                0.9,
                scene(uv=(60, 40)),
                "wait_missing_wave",
                None,
                WAVE,
                "Короткая пропажа — подождать.",
            ),
            Frame(
                1.2,
                scene(uv=(60, 40), enemies=((625, 260, 0.3),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Крип снова виден — продолжить работу с волной.",
            ),
            Frame(
                1.5,
                scene(uv=(60, 40), enemies=((620, 260, 0.2),)),
                "attack_in_progress",
                None,
                WAVE,
                "Повторно не перебивать атаку.",
            ),
        ),
        ACTIVE,
    ),
    Scenario(
        "Опасность во время движения",
        "Проверка опасности работает до прибытия к волне.",
        (
            Frame(
                0,
                scene(),
                "select_target",
                "move_minimap",
                MOVE,
                "Начать движение на линию.",
            ),
            Frame(
                0.3,
                scene(uv=(40, 60), hp=0.2),
                "low_hp",
                None,
                "ABORT",
                "Видим мало HP — прервать фарм; Brain передаст управление отступлению.",
            ),
            Frame(
                1,
                scene(uv=(40, 60), hp=0.2),
                "finished",
                None,
                "ABORT",
                "Отменённый фарм больше не генерирует кликов.",
            ),
        ),
    ),
    Scenario(
        "Вражеская башня прерывает фарм",
        "Даже ожидание после атаки не блокирует отмену опасной задачи.",
        (
            Frame(
                0,
                scene(uv=(60, 40), enemies=((620, 260, 0.4),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Атаковать крипа.",
            ),
            Frame(
                0.1,
                scene(uv=(60, 40), enemies=((620, 260, 0.4),), tower=True),
                "enemy_tower",
                None,
                "ABORT",
                "Башня важнее продолжения атаки — передать управление отступлению.",
            ),
            Frame(
                0.4,
                scene(uv=(60, 40)),
                "finished",
                None,
                "ABORT",
                "Фарм остаётся отменённым.",
            ),
        ),
        ACTIVE,
    ),
    Scenario(
        "Волна не появилась",
        "У поиска волны отдельный таймаут 30 секунд, он не обрезается боевым лимитом 22 секунды.",
        (
            Frame(
                0,
                scene(uv=(60, 40)),
                "wait_wave",
                None,
                WAVE,
                "Ждать появления крипов.",
            ),
            Frame(
                22.1,
                scene(uv=(60, 40)),
                "wait_wave",
                None,
                WAVE,
                "На 22-й секунде поиск ещё продолжается.",
            ),
            Frame(
                29.9,
                scene(uv=(60, 40)),
                "wait_wave",
                None,
                WAVE,
                "До 30 секунд не завершать поиск.",
            ),
            Frame(
                30,
                scene(uv=(60, 40)),
                "find_wave_timeout",
                None,
                "DONE",
                "Истекло время поиска — завершить задачу.",
            ),
        ),
        ACTIVE,
    ),
    Scenario(
        "Движение застряло",
        "Повторять команду можно; бесконечно держать невыполнимую задачу нельзя.",
        (
            Frame(
                0, scene(), "select_target", "move_minimap", MOVE, "Начать движение."
            ),
            Frame(
                1,
                scene(),
                "move_to_lane",
                "move_minimap",
                MOVE,
                "Повторить ту же цель после интервала.",
            ),
            Frame(
                14,
                scene(),
                "move_to_lane",
                "move_minimap",
                MOVE,
                "Герой виден, но всё ещё не дошёл.",
            ),
            Frame(
                15,
                scene(),
                "move_timeout",
                None,
                "ABORT",
                "Прервать движение по таймауту.",
            ),
        ),
    ),
    Scenario(
        "Враг рядом: не посылать движение и атаку вместе",
        "Один шаг выдаёт одно согласованное действие.",
        (
            Frame(
                0,
                scene(
                    uv=(60, 40),
                    enemies=((650, 260, 0.9), (680, 280, 0.9)),
                    allies=((520, 260, 0.8),),
                    danger=True,
                ),
                "hold_behind_allies",
                "move_screen",
                WAVE,
                "Держаться за союзником. Не отправлять следом атаку.",
            ),
            Frame(
                0.2,
                scene(
                    uv=(60, 40),
                    enemies=((650, 260, 0.9),),
                    allies=((520, 260, 0.8),),
                    danger=True,
                ),
                "hold_behind_allies",
                None,
                WAVE,
                "Повторный отход пока подавлен кулдауном.",
            ),
            Frame(
                1,
                scene(
                    uv=(60, 40), enemies=((650, 260, 0.9),), allies=((520, 260, 0.8),)
                ),
                "push_wave",
                "attack_screen",
                WAVE,
                "Враг больше не наблюдается рядом — выбрать атаку вместо движения.",
            ),
        ),
        ACTIVE,
    ),
    Scenario(
        "Бой длится слишком долго",
        "Таймаут проверяется до новой атаки, в том числе при постоянно видимых крипах.",
        (
            Frame(
                0,
                scene(uv=(60, 40), enemies=((620, 260, 0.5),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Начать работу с волной.",
            ),
            Frame(
                10,
                scene(uv=(60, 40), enemies=((620, 260, 0.4),)),
                "attack_creep",
                "attack_screen",
                WAVE,
                "Волна ещё в пределах лимита.",
            ),
            Frame(
                22,
                scene(uv=(60, 40), enemies=((620, 260, 0.3),)),
                "wave_timeout",
                None,
                "DONE",
                "Не отправлять атаку после истечения лимита.",
            ),
        ),
        ACTIVE,
    ),
)
