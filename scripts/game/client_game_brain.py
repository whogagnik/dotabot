from __future__ import annotations
import time
from typing import Dict, Any, Optional, TYPE_CHECKING, Tuple, List, Callable
import csv
from pathlib import Path
import numpy as np
from scripts.core.utils import *  # noqa
from scripts.core.config import CATBOOST_DATASET_CSV_PATH


if TYPE_CHECKING:
    # эти импорты используются только для type hints, на рантайме не выполняются
    from planner import Planner, Snapshot

def _euclid2(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    dx = a[0] - b[0]; dy = a[1] - b[1]
    return dx*dx + dy*dy

def _nearest_point(poly: List[Dict[str, float]], pt: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    """Просто ближайшая вершина из полилинии (достаточно для клика)."""
    if not poly:
        return None
    x, y = pt
    best = None
    best_d2 = 1e18
    for pnt in poly:
        px, py = float(pnt["x"]), float(pnt["y"])
        d2 = _euclid2((x, y), (px, py))
        if d2 < best_d2:
            best_d2 = d2
            best = (px, py)
    return best
# --- Геометрия: ближайшая точка на отрезке / полилинии (в координатах 0..100) ---

def _closest_point_on_segment(ax: float, ay: float, bx: float, by: float,
                              px: float, py: float) -> Tuple[float, float, float]:
    """
    Ближайшая точка от P(px,py) к отрезку AB(ax,ay)-(bx,by).
    Возвращает (qx, qy, dist2), где Q — проекция, dist2 — квадрат расстояния.
    """
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    if ab2 <= 1e-9:
        # вырожденный отрезок — возвращаем A
        qx, qy = ax, ay
        dx, dy = px - qx, py - qy
        return qx, qy, dx * dx + dy * dy
    t = (apx * abx + apy * aby) / ab2
    if t < 0.0:   t = 0.0
    if t > 1.0:   t = 1.0
    qx, qy = ax + t * abx, ay + t * aby
    dx, dy = px - qx, py - qy
    return qx, qy, dx * dx + dy * dy


def _closest_point_on_polyline(poly: List[Dict[str, float]],
                               p: Tuple[float, float]) -> Tuple[float, float, float]:
    """
    poly: [{x,y}, ...] — одна ломаная (линия).
    p: (px,py) — текущая позиция.
    Возвращает (qx, qy, dist2) — ближайшая точка на всей ломаной.
    """
    if not poly or len(poly) == 1:
        pt = poly[0] if poly else {"x": 50.0, "y": 50.0}
        dx, dy = p[0] - float(pt["x"]), p[1] - float(pt["y"])
        return float(pt["x"]), float(pt["y"]), dx * dx + dy * dy

    px, py = p
    best_q = (float(poly[0]["x"]), float(poly[0]["y"]))
    best_d2 = 1e18

    for i in range(len(poly) - 1):
        ax, ay = float(poly[i]["x"]), float(poly[i]["y"])
        bx, by = float(poly[i + 1]["x"]), float(poly[i + 1]["y"])
        qx, qy, d2 = _closest_point_on_segment(ax, ay, bx, by, px, py)
        if d2 < best_d2:
            best_d2 = d2
            best_q = (qx, qy)

    return best_q[0], best_q[1], best_d2
from enum import Enum, auto
from dataclasses import dataclass

class CampKind(Enum):
    SMALL = auto()
    MEDIUM = auto()
    LARGE = auto()

class CampState(Enum):
    READY = auto()
    CLEARED = auto()
    SKIPPED = auto()

class FarmPlan(Enum):
    # Optional route preset for old farming pipeline.
    LANE_THEN_JUNGLE = auto()
    JUNGLE_ONLY = auto()

class FarmPhase(Enum):
    PICK_TARGET = auto()
    MOVE_TO_TARGET = auto()
    FIGHT = auto()
    RETREAT = auto()


class LaneFarmPhase(Enum):
    SELECT_SEGMENT = auto()
    MOVE_TO_POINT = auto()
    WAIT_OR_CLEAR_WAVE = auto()
    DONE = auto()
    ABORT = auto()

class RetreatPhase(Enum):
    GO_BEHIND_ALLY = auto()
    BUILD_PATH = auto()
    FOLLOW_PATH = auto()
    DIRECT_TO_FOUNTAIN = auto()

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
@dataclass
class CampNode:
    camp_id: int
    kind: CampKind
    x: float          # 0..100
    y: float          # 0..100
    state: CampState = CampState.READY

@dataclass
class Senses:
    alive: bool
    t_game: float
    hp_ratio: Optional[float]
    low_hp: bool

    side: str
    role: str

    enemy_hero_near: bool
    enemy_hero_dist_screen: Optional[float]
    enemy_hero_dist_mm: Optional[float]
    enemy_hero_cnt_screen: int
    avg_enemy_hero_hp_ratio_screen: Optional[float]

    ally_hero_cnt_screen: int
    closest_enemy_hero_hp_ratio: Optional[float]
    closest_enemy_hero_dist_px: Optional[float]

    closest_enemy_hero_mm_dist: Optional[float]

    closest_ally_hero_dist_px: Optional[float]
    closest_ally_hero_mm_dist: Optional[float]

    enemy_creep_near: bool
    enemy_creep_dist_screen: Optional[float]
    enemy_creep_cnt_screen: int

    ally_creep_near: bool
    ally_creep_dist_screen: Optional[float]
    ally_creep_cnt_screen: int

    best_enemy_lasthit_hp_ratio: Optional[float]
    best_enemy_lasthit_dist_px: Optional[float]

    best_ally_deny_hp_ratio: Optional[float]
    best_ally_deny_dist_px: Optional[float]

    under_ally_tower: bool
    ally_tower_dist_mm: Optional[float]

    near_enemy_tower: bool
    enemy_tower_dist_mm: Optional[float]

    landmarks: Optional[Dict[str, Any]]

    lane_key_known: bool
    last_action_delta: float
    lane_wave_seen: bool
    lane_wave_seen_delta: float

    has_lasthit_target: bool
    has_lane_target: bool
    has_farm_target: bool
    has_moving_target: bool

    brain_state_name: str
    farm_phase_name: str
    lane_farm_phase_name: str

    self_x_uv: Optional[float]
    self_y_uv: Optional[float]

    def to_catboost_features(self) -> Dict[str, Any]:
        return {
            "alive": bool(self.alive),
            "t_game": float(self.t_game),
            "hp_ratio": None if self.hp_ratio is None else float(self.hp_ratio),
            "low_hp": bool(self.low_hp),

            "side": self.side,
            "role": self.role,

            "enemy_hero_near": bool(self.enemy_hero_near),
            "enemy_hero_dist_screen": self.enemy_hero_dist_screen,
            "enemy_hero_dist_mm": self.enemy_hero_dist_mm,
            "enemy_hero_cnt_screen": int(self.enemy_hero_cnt_screen),
            "avg_enemy_hero_hp_ratio_screen": self.avg_enemy_hero_hp_ratio_screen,


            "ally_hero_cnt_screen": int(self.ally_hero_cnt_screen),
            "closest_enemy_hero_hp_ratio": self.closest_enemy_hero_hp_ratio,
            "closest_enemy_hero_dist_px": self.closest_enemy_hero_dist_px,
            "closest_enemy_hero_mm_dist": self.closest_enemy_hero_mm_dist,
            "closest_ally_hero_dist_px": self.closest_ally_hero_dist_px,
            "closest_ally_hero_mm_dist": self.closest_ally_hero_mm_dist,

            "enemy_creep_near": bool(self.enemy_creep_near),
            "enemy_creep_dist_screen": self.enemy_creep_dist_screen,
            "enemy_creep_cnt_screen": int(self.enemy_creep_cnt_screen),

            "ally_creep_near": bool(self.ally_creep_near),
            "ally_creep_dist_screen": self.ally_creep_dist_screen,
            "ally_creep_cnt_screen": int(self.ally_creep_cnt_screen),

            "best_enemy_lasthit_hp_ratio": self.best_enemy_lasthit_hp_ratio,
            "best_enemy_lasthit_dist_px": self.best_enemy_lasthit_dist_px,

            "best_ally_deny_hp_ratio": self.best_ally_deny_hp_ratio,
            "best_ally_deny_dist_px": self.best_ally_deny_dist_px,

            "under_ally_tower": bool(self.under_ally_tower),
            "ally_tower_dist_mm": self.ally_tower_dist_mm,

            "near_enemy_tower": bool(self.near_enemy_tower),
            "enemy_tower_dist_mm": self.enemy_tower_dist_mm,

            "lane_key_known": bool(self.lane_key_known),
            "last_action_delta": float(self.last_action_delta),
            "lane_wave_seen": bool(self.lane_wave_seen),
            "lane_wave_seen_delta": float(self.lane_wave_seen_delta),

            "has_lasthit_target": bool(self.has_lasthit_target),
            "has_lane_target": bool(self.has_lane_target),
            "has_farm_target": bool(self.has_farm_target),
            "has_moving_target": bool(self.has_moving_target),

            "brain_state_name": self.brain_state_name,
            "farm_phase_name": self.farm_phase_name,
            "lane_farm_phase_name": self.lane_farm_phase_name,

            "self_x_uv": self.self_x_uv,
            "self_y_uv": self.self_y_uv,
            "enemy_minus_ally_heroes": int(self.enemy_hero_cnt_screen) - int(self.ally_hero_cnt_screen),
            "enemy_minus_ally_creeps": int(self.enemy_creep_cnt_screen) - int(self.ally_creep_cnt_screen),
            "has_wave_clash": bool(self.enemy_creep_near and self.ally_creep_near),
            "can_lasthit_enemy": bool(self.best_enemy_lasthit_hp_ratio is not None),
            "can_deny_ally": bool(self.best_ally_deny_hp_ratio is not None),
        }



class Brain:
    """
    Один Brain на один hwnd.
    Он не знает про окна в целом, только про свой hwnd + данные из Planner.
    """

    def __init__(self, hwnd: int, planner: "Planner", logger=None,role: str = "unknown",):
        self.hwnd = hwnd
        self.pl = planner
        self.log = logger
        self.state = BrainState.IDLE
        self.role = str(role).lower().strip() if role is not None else "unknown"
        if not self.role:
            self.role = "unknown"

        # время старта (для t_game)
        self.t0 = time.time()

        # можно хранить ещё какие-то внутренние штуки:
        self.last_action_ts = 0.0

        # цель для MOVING
        self._moving_point: Optional[tuple[float, float]] = None  # (x,y) в координатах миникарты 0..100
        self._moving_radius: float = 5.0  # радиус "достаточно близко" (в тех же единицах)
        self.lane_offset_px: float = 80.0  # насколько пикселей отходить от линии по нормали

        self._wait_start_target: Optional[Tuple[float, float]] = None
        self._wait_start_click_cooldown: float = 1.0
        self._wait_start_last_click_ts: float = 0.0

        # --------- FARMING state ---------
        self.farm_plan: FarmPlan = FarmPlan.JUNGLE_ONLY
        self.farm_lane_prob: float = 0.35   # шанс выбрать LANE_THEN_JUNGLE (если линия будет задана)

        self._farm_phase: FarmPhase = FarmPhase.PICK_TARGET
        self._farm_target_id: Optional[int] = None
        self._farm_fight_camp_id: Optional[int] = None
        self._farm_fight_started_near_target: bool = False

        self._camps: list[CampNode] = []
        self._camps_inited: bool = False
        self._camp_reset_minute: int = -1  # для ресета каждую минуту

        self._farming_minimap_click_cooldown: float = 5.0  # сек
        self._farming_last_minimap_click_ts: float = 0.0

        self.min_enemy_dist_px: float = 150.0
        self.max_enemy_dist_px: float = 400.0

        self._lasthit_target_key: Optional[tuple[str, object]] = None
        self._lasthit_target_expire: float = 0.0  # safety timeout

        # --- attack (screen click) cooldown ---
        self._attack_click_cooldown: float = 1.5  # сек, подстрой если надо
        self._last_attack_click_ts: float = 0.0

        # --- lasthit / approach tuning ---
        self.lasthit_prepare_hp: float = 0.50  # если крип <= 0.5 -> можно готовиться
        self.lasthit_attack_hp: float = 0.25  # реально добивать при <= 0.25
        self.lasthit_attack_range_px: float = 150.0  # “мы в радиусе удара”
        self.lasthit_max_seek_px: float = 650.0  # дальше не рассматриваем
        self.lasthit_cmd_cooldown: float = 1  # чтобы не спамить клики

        self._manual_switch_last_ts: float = 0.0
        self._digit_prev_down: Dict[int, bool] = {d: False for d in range(1, 10)}
        self._manual_switch_cooldown: float = 0.20  # антидребезг

        # --- anti spam walk clicks ---
        self.walk_cmd_cooldown: float = 0.50   # общий кулдаун на walk-команды
        self.walk_same_target_tol_px: float = 18.0  # "та же цель" для walk

        self._last_walk_ts: float = 0.0
        self._last_walk_target: Optional[Tuple[int, int]] = None

        # --- LANING orientation ---
        self._lane_inited: bool = False
        self._lane_key: Optional[str] = None          # "lane_top" / "lane_mid" / "lane_bot"
        self._lane_anchor_uv: Optional[Tuple[float, float]] = None  # ����� 0..100 �� ����� � �1

        # --- CatBoost action routing (stub) ---


        self._catboost_model = None
        self._brain_state_id_map: Dict[int, BrainState] = {
            0: BrainState.FARMING_JUNGLE,
            1: BrainState.FARMING_LANE,
            2: BrainState.LANING,
            3: BrainState.FIGHTING,
            4: BrainState.RETREAT,
            5: BrainState.IDLE,
        }


        self.catboost_dataset_flush_every: int = 100
        self.catboost_dataset_csv_path: str = CATBOOST_DATASET_CSV_PATH
        self.use_catboost_brain_state: bool = True
        self._catboost_dataset_rows: List[Dict[str, Any]] = []
        self.collect_catboost_dataset: bool = False

        # --- lane farming FSM memory ---
        self._lane_farm_phase: LaneFarmPhase = LaneFarmPhase.SELECT_SEGMENT
        self._lane_target_uv: Optional[Tuple[float, float]] = None
        self._lane_wave_seen: bool = False
        self._lane_wave_last_seen_ts: float = 0.0
        self._lane_phase_start_ts: float = 0.0

        # --- lane farming tunables ---
        self.lane_attach_uv: float = 5.0
        self.lane_max_depth_frac: float = 0.45
        self.lane_tower_margin: float = 0.03
        self.lane_arrive_radius_uv: float = 5.0
        self.lane_move_timeout: float = 15.0
        self.lane_find_wave_timeout: float = 30.0
        self.lane_wave_timeout: float = 22.0
        self.lane_wave_clear_grace: float = 2.5
        self._farm_no_creep_since_ts: float = 0.0
        self._farm_arrived_at_camp_ts: float = 0.0
        self._farm_cleared_confirm_delay: float = 1.2
        self._farm_stuck_near_camp_timeout: float = 2.0
        self._farm_near_camp_radius_uv: float = 6

        # --- retreat runtime ---
        self._retreat_phase: RetreatPhase = RetreatPhase.DIRECT_TO_FOUNTAIN
        self._retreat_enter_ts: float = 0.0
        self._retreat_phase_ts: float = 0.0

        self._retreat_enemy_memory_until: float = 0.0

        # tunables
        self.retreat_enemy_memory_sec: float = 2.0
        self.retreat_ally_screen_radius_px: float = 320.0
        self.retreat_ally_mm_radius_uv: float = 10.0
        self.retreat_anchor_timeout: float = 1.0
        self.retreat_anchor_backstep_px: float = 120.0
        self.retreat_screen_step_px: float = 160.0
        self.retreat_arc_side_uv: float = 8.0
        self.retreat_arc_forward_uv: float = 14.0
        self.retreat_enemy_path_corridor_uv: float = 6.0

        # --- retreat path ---
        self._retreat_path_uv: List[Tuple[float, float]] = []
        self._retreat_path_idx: int = 0
        self._retreat_path_built_ts: float = 0.0

        self.retreat_path_arrive_radius_uv: float = 3.0
        self.retreat_path_rebuild_sec: float = 0.8
        self.retreat_path_points: int = 6
        self.retreat_path_side_offset_uv: float = 10.0
        self.retreat_path_forward_gain_uv: float = 10.0
    # --------- публичный метод ---------
    def tick_one(self, snap: "Snapshot"):
        c = snap.combined

        senses = self._gather_senses(c)

        states = list(BrainState)
        d = self._poll_digit_press(max_digit=len(states))
        if d is not None:
            new_state = states[d - 1]
            self._set_state(new_state)
            if self.log:
                self.log.info(f"[BRAIN {hex(self.hwnd)}] manual switch: {d} -> {new_state.name}")
        else:

            # системные override-состояния
            forced_state = self._get_forced_state(senses)
            '''
            if forced_state is not None:
                self._set_state(forced_state)
            elif self.use_catboost_brain_state:
                if self._catboost_model is None:
                    raise RuntimeError("use_catboost_brain_state=True, but CatBoost model is not loaded")

                features = senses.to_catboost_features()
                predicted_state = self._predict_brain_state_with_catboost(features)
                self._set_state(predicted_state)
            '''
        self._collect_catboost_train_row(senses)

        self._tick_state(c, senses)

    def _tick_state(self, c: Dict[str, Any], s: Senses):
        st = self.state

        if st is BrainState.DEAD:
            self._tick_dead(c, s)

        elif st is BrainState.WAIT_START:
            self._tick_wait_start(c, s)

        elif st is BrainState.MOVING:
            self._tick_moving(c, s)

        elif st is BrainState.LANING:
            self._tick_laning(c, s)

        elif st is BrainState.FARMING_JUNGLE:
            self._tick_farming_jungle(c, s)

        elif st is BrainState.FARMING_LANE:
            self._tick_farming_lane(c, s)

        elif st is BrainState.FIGHTING:
            self._tick_fighting(c, s)

        elif st is BrainState.RETREAT:
            self._tick_retreat(c, s)

        elif st is BrainState.IDLE:
            self._tick_idle(c, s)

        else:
            if self.log:
                self.log.warning(f"[BRAIN {hex(self.hwnd)}] unknown state: {st}")

    def _gather_senses(self, c: Dict[str, Any]) -> Senses:
        alive = bool(c.get("alive", True))
        t_game = float(c.get("t_game", 0.0))

        hp_ratio_from_hud = c.get("hp_ratio")
        hp_ratio_from_screen = c.get("heroes", {}).get("self", [])

        if len(hp_ratio_from_screen) == 0:
            hp_ratio_from_screen = None

        hp_ratio: Optional[float] = None
        if hp_ratio_from_hud is not None and hp_ratio_from_screen is not None:
            hp_pair_from_screen = hp_ratio_from_screen[0].hp_ratio
            hp_ratio = max(hp_pair_from_screen, hp_ratio_from_hud)
        elif hp_ratio_from_hud is not None:
            hp_ratio = hp_ratio_from_hud
        elif hp_ratio_from_screen is not None:
            hp_ratio = hp_ratio_from_screen[0].hp_ratio

        low_hp = hp_ratio is not None and hp_ratio < 0.3

        enemy_hero_near, hero_dist_scr, hero_dist_mm = self._sense_enemy_hero_near(c)
        enemy_creep_near, enemy_creep_dist_scr = self._sense_enemy_creep_near(c)
        ally_creep_near, ally_creep_dist_scr = self._sense_ally_creep_near(c)
        under_ally_tower, ally_tower_dist_mm = self._sense_under_ally_tower(c)
        near_enemy_tower, enemy_tower_dist_mm = self._sense_near_enemy_tower(c)

        enemy_cnt_screen = self._count_enemy_heroes_near_screen(c)
        avg_enemy_hp_screen = self._avg_enemy_hero_hp_ratio_screen(c)

        ally_hero_cnt_screen = self._count_ally_heroes_near_screen(c)
        closest_enemy_hero_hp_ratio, closest_enemy_hero_dist_px = self._closest_enemy_hero_features(c)

        closest_enemy_hero_mm_dist = self._closest_enemy_hero_mm_dist(c)
        closest_ally_hero_dist_px = self._closest_ally_hero_dist_px(c)
        closest_ally_hero_mm_dist = self._closest_ally_hero_mm_dist(c)

        enemy_creep_cnt_screen = self._count_enemy_creeps_screen(c)
        ally_creep_cnt_screen = self._count_ally_creeps_screen(c)

        best_enemy_lasthit_hp_ratio, best_enemy_lasthit_dist_px = self._best_lasthit_features(
            c, enemy=True, hp_threshold=0.5
        )
        best_ally_deny_hp_ratio, best_ally_deny_dist_px = self._best_lasthit_features(
            c, enemy=False, hp_threshold=0.5
        )

        now = time.time()
        last_action_delta = (now - self.last_action_ts) if self.last_action_ts > 0 else 9999.0
        lane_wave_seen_delta = (now - self._lane_wave_last_seen_ts) if self._lane_wave_last_seen_ts > 0 else 9999.0

        self_uv = self._get_self_uv(c)
        self_x_uv = float(self_uv[0]) if self_uv is not None else None
        self_y_uv = float(self_uv[1]) if self_uv is not None else None

        return Senses(
            alive=alive,
            t_game=t_game,
            hp_ratio=hp_ratio,
            low_hp=low_hp,

            side=self._get_side(),
            role=self._get_role(),

            enemy_hero_near=enemy_hero_near,
            enemy_hero_dist_screen=hero_dist_scr,
            enemy_hero_dist_mm=hero_dist_mm,
            enemy_hero_cnt_screen=enemy_cnt_screen,
            avg_enemy_hero_hp_ratio_screen=avg_enemy_hp_screen,

            ally_hero_cnt_screen=ally_hero_cnt_screen,
            closest_enemy_hero_hp_ratio=closest_enemy_hero_hp_ratio,
            closest_enemy_hero_dist_px=closest_enemy_hero_dist_px,
            closest_enemy_hero_mm_dist=closest_enemy_hero_mm_dist,

            closest_ally_hero_dist_px=closest_ally_hero_dist_px,
            closest_ally_hero_mm_dist=closest_ally_hero_mm_dist,

            enemy_creep_near=enemy_creep_near,
            enemy_creep_dist_screen=enemy_creep_dist_scr,
            enemy_creep_cnt_screen=enemy_creep_cnt_screen,

            ally_creep_near=ally_creep_near,
            ally_creep_dist_screen=ally_creep_dist_scr,
            ally_creep_cnt_screen=ally_creep_cnt_screen,

            best_enemy_lasthit_hp_ratio=best_enemy_lasthit_hp_ratio,
            best_enemy_lasthit_dist_px=best_enemy_lasthit_dist_px,

            best_ally_deny_hp_ratio=best_ally_deny_hp_ratio,
            best_ally_deny_dist_px=best_ally_deny_dist_px,

            under_ally_tower=under_ally_tower,
            ally_tower_dist_mm=ally_tower_dist_mm,

            near_enemy_tower=near_enemy_tower,
            enemy_tower_dist_mm=enemy_tower_dist_mm,

            landmarks=((c.get("landmarks") or {}).get("data") if isinstance(c.get("landmarks"), dict) else None),

            lane_key_known=bool(self._lane_key),
            last_action_delta=float(last_action_delta),
            lane_wave_seen=bool(self._lane_wave_seen),
            lane_wave_seen_delta=float(lane_wave_seen_delta),

            has_lasthit_target=bool(self._lasthit_target_key is not None),
            has_lane_target=bool(self._lane_target_uv is not None),
            has_farm_target=bool(self._farm_target_id is not None),
            has_moving_target=bool(self._moving_point is not None),

            brain_state_name=self.state.name.lower(),
            farm_phase_name=self._farm_phase.name.lower() if self._farm_phase is not None else "none",
            lane_farm_phase_name=self._lane_farm_phase.name.lower() if self._lane_farm_phase is not None else "none",

            self_x_uv=self_x_uv,
            self_y_uv=self_y_uv,
        )

    @staticmethod
    @debug_log_result
    def _hpbar_center(box) -> tuple[float, float]:
        """
        box: объект с полями x0,y0,x1,y1 (HpBarBox из hp_scanner).
        Возвращает центр в пикселях.
        """
        return ((box.x0 + box.x1) * 0.5, (box.y0 + box.y1) * 0.5)

    @staticmethod
    @debug_log_result
    def _dist2_pts(p1: tuple[float, float], p2: tuple[float, float]) -> float:
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return dx * dx + dy * dy

    @staticmethod
    @debug_log_result
    def _dist2_uv(x1: float, y1: float, x2: float, y2: float) -> float:
        dx = x1 - x2
        dy = y1 - y2
        return dx * dx + dy * dy

    @staticmethod
    @debug_log_result
    def _compromise_line(
            points_a: np.ndarray,
            points_e: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        A — "наши" точки, E — "вражеские".
        Работает даже если один из наборов пустой.
        """

        # приведение к форме (N,2)
        if points_a is None:
            points_a = np.zeros((0, 2), dtype=np.float32)
        if points_e is None:
            points_e = np.zeros((0, 2), dtype=np.float32)

        if points_a.ndim != 2 or points_a.shape[1] != 2:
            points_a = np.zeros((0, 2), dtype=np.float32)
        if points_e.ndim != 2 or points_e.shape[1] != 2:
            points_e = np.zeros((0, 2), dtype=np.float32)

        # fallback центры
        if points_a.shape[0] == 0 and points_e.shape[0] == 0:
            c_a = np.array([0.0, 0.0], dtype=np.float32)
            c_e = np.array([0.0, 1.0], dtype=np.float32)
        elif points_a.shape[0] == 0:
            c_e = points_e.mean(axis=0).astype(np.float32)
            c_a = (c_e + np.array([0.0, -1.0], dtype=np.float32)).astype(np.float32)
        elif points_e.shape[0] == 0:
            c_a = points_a.mean(axis=0).astype(np.float32)
            c_e = (c_a + np.array([0.0, 1.0], dtype=np.float32)).astype(np.float32)
        else:
            c_a = points_a.mean(axis=0).astype(np.float32)
            c_e = points_e.mean(axis=0).astype(np.float32)

        n = c_e - c_a
        if float(np.linalg.norm(n)) < 1e-6:
            n = np.array([0.0, 1.0], dtype=np.float32)
        else:
            n = (n / np.linalg.norm(n)).astype(np.float32)

        mid = (0.5 * (c_a + c_e)).astype(np.float32)
        d = np.array([-n[1], n[0]], dtype=np.float32)

        return mid, d, n, c_a, c_e

    @debug_log_result
    def _count_enemy_creeps_screen(self, c: Dict[str, Any]) -> int:
        creeps = c.get("creeps", {})
        enemy_creeps = creeps.get("enemy", [])
        return len(enemy_creeps) if enemy_creeps else 0

    @debug_log_result
    def _count_ally_creeps_screen(self, c: Dict[str, Any]) -> int:
        creeps = c.get("creeps", {})
        ally_creeps = creeps.get("ally", [])
        return len(ally_creeps) if ally_creeps else 0

    @debug_log_result
    def _closest_enemy_hero_features(
            self,
            c: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[float]]:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])

        if not self_heroes or not enemy_heroes:
            return None, None

        hx, hy = self._hpbar_center(self_heroes[0])

        best_dist: Optional[float] = None
        best_hp: Optional[float] = None

        for eb in enemy_heroes:
            ex, ey = self._hpbar_center(eb)
            dist = float(((ex - hx) ** 2 + (ey - hy) ** 2) ** 0.5)
            hp_ratio = getattr(eb, "hp_ratio", None)

            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_hp = float(hp_ratio) if hp_ratio is not None else None

        return best_hp, best_dist

    @debug_log_result
    def _best_lasthit_features(
            self,
            c: Dict[str, Any],
            *,
            enemy: bool = True,
            hp_threshold: float = 0.5,
    ) -> Tuple[Optional[float], Optional[float]]:
        candidate = self._select_creep(c, hp_threshold=hp_threshold, enemy=enemy)
        if candidate is None:
            return None, None

        cb, cx, cy = candidate
        hp_ratio = getattr(cb, "hp_ratio", None)

        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        if not self_heroes:
            return (float(hp_ratio) if hp_ratio is not None else None), None

        hx, hy = self._hpbar_center(self_heroes[0])
        dist = float(((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5)

        return (float(hp_ratio) if hp_ratio is not None else None), dist
    @debug_log_result
    def _count_ally_heroes_near_screen(self, c: Dict[str, Any]) -> int:
        heroes = c.get("heroes", {})
        ally_heroes = heroes.get("ally", [])
        return len(ally_heroes) if ally_heroes else 0
    def _compute_arc_waypoint_to_fountain(
        self,
        c: Dict[str, Any],
        enemy_uv: Tuple[float, float],
        fountain_uv: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        cur = self._get_self_uv(c)
        if cur is None:
            return None

        cx, cy = cur
        fx, fy = fountain_uv
        ex, ey = enemy_uv

        dx = fx - cx
        dy = fy - cy
        dn = float((dx * dx + dy * dy) ** 0.5)
        if dn <= 1e-6:
            return None

        ux = dx / dn
        uy = dy / dn

        vex = ex - cx
        vey = ey - cy

        forward = vex * ux + vey * uy
        if forward <= 0.0:
            return None

        nx = -uy
        ny = ux
        side = vex * nx + vey * ny

        if abs(side) > self.retreat_enemy_path_corridor_uv:
            return None

        sign = -1.0 if side > 0.0 else 1.0
        wx = cx + ux * self.retreat_arc_forward_uv + nx * sign * self.retreat_arc_side_uv
        wy = cy + uy * self.retreat_arc_forward_uv + ny * sign * self.retreat_arc_side_uv

        wx = max(0.0, min(100.0, wx))
        wy = max(0.0, min(100.0, wy))
        return (wx, wy)
    @debug_log_result
    def _compute_dist_to_enemy_hero(self, s: Senses) -> float:
        """
        distance_to_enemy_hero = min + (max - min) * hp_ratio

        Если hp_ratio неизвестен -> используем min_enemy_dist_px.
        """
        if s.hp_ratio is None:
            return self.min_enemy_dist_px

        r = 1 - max(0.0, min(1.0, float(s.hp_ratio)))
        dist = self.min_enemy_dist_px + (self.max_enemy_dist_px - self.min_enemy_dist_px) * r
        return dist

    @debug_log_result
    def _count_enemy_heroes_near_screen(self, c: Dict[str, Any]) -> int:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])
        if not self_heroes or not enemy_heroes:
            return 0
        return len(enemy_heroes)

    @debug_log_result
    def _avg_enemy_hero_hp_ratio_screen(
        self,
        c: Dict[str, Any],
        *,
        min_count: int = 1,
    ) -> Optional[float]:
        """
        Средний hp_ratio всех вражеских героев на экране.
        Возвращает None если врагов с hp_ratio не найдено.
        """
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])
        if not self_heroes or not enemy_heroes:
            return None

        vals: List[float] = []
        for eb in enemy_heroes:
            ehp = getattr(eb, "hp_ratio", None)
            if ehp is None:
                continue
            vals.append(float(ehp))

        if len(vals) < min_count:
            return None

        avg = float(sum(vals) / len(vals))
        return max(0.0, min(1.0, avg))

    @debug_log_result
    def _should_approach_for_lasthit(
        self,
        s: Senses,
        *,
        creep_hp: float,
        dist_to_creep_px: float,
        enemy_hero_cnt_near: int,
        avg_enemy_hp: Optional[float],
    ) -> bool:
        """
        Решение: можно ли сближаться ради ластхита.
        """
        my_hp = 1.0 if s.hp_ratio is None else max(0.0, min(1.0, float(s.hp_ratio)))
        creep_hp = max(0.0, min(1.0, float(creep_hp)))

        if creep_hp >= 0.5:
            return False

        if my_hp < 0.25:
            return False

        risk = 0.0

        if enemy_hero_cnt_near <= 0:
            risk += 0.0
        elif enemy_hero_cnt_near == 1:
            risk += 0.35
        elif enemy_hero_cnt_near == 2:
            risk += 0.75
        else:
            risk += 1.20

        if avg_enemy_hp is not None:
            e_hp = max(0.0, min(1.0, float(avg_enemy_hp)))
            diff = my_hp - e_hp

            if diff < 0.0:
                risk += min(0.8, (-diff) * 1.2)
            else:
                risk -= min(0.25, diff * 0.5)

            if my_hp < 0.45 and e_hp > 0.65:
                return False

        if dist_to_creep_px > 350:
            risk += min(0.8, (dist_to_creep_px - 350.0) / 400.0)

        hp_factor = 1.0
        if my_hp < 0.5:
            hp_factor += (0.5 - my_hp) * 1.2
        else:
            hp_factor -= min(0.25, (my_hp - 0.5) * 0.5)

        risk *= hp_factor

        reward = min(0.55, (0.5 - creep_hp) * 1.1)
        risk -= reward

        threshold = 0.65

        if s.near_enemy_tower:
            return False

        if s.enemy_hero_near and my_hp < 0.4:
            return False

        return risk <= threshold

    def _flush_catboost_dataset_to_csv(self, force: bool = False) -> None:
        if not self._catboost_dataset_rows:
            return

        if (not force) and (len(self._catboost_dataset_rows) < self.catboost_dataset_flush_every):
            return

        rows = self._catboost_dataset_rows
        self._catboost_dataset_rows = []

        path = Path(self.catboost_dataset_csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # единый набор колонок
        fieldnames: List[str] = []
        for row in rows:
            for key in row.keys():
                if key not in fieldnames:
                    fieldnames.append(key)

        file_exists = path.exists() and path.stat().st_size > 0

        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )

            if not file_exists:
                writer.writeheader()

            for row in rows:
                normalized_row = {k: row.get(k) for k in fieldnames}
                writer.writerow(normalized_row)

        if self.log:
            self.log.info(
                f"[BRAIN {hex(self.hwnd)}] flushed {len(rows)} catboost rows to {path}"
            )

    def flush_catboost_dataset_now(self) -> None:
        self._flush_catboost_dataset_to_csv(force=True)
    def _vk_digit(self, d: int) -> int:
        # '1'..'9' => 0x31..0x39
        return 0x30 + d

    def _is_vk_down(self, vk: int) -> bool:
        """
        True если клавиша сейчас нажата (Windows GetAsyncKeyState).
        Если не Windows/нет user32 — вернёт False.
        """
        try:
            # GetAsyncKeyState: старший бит == down
            return bool(win32api.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _poll_digit_press(self, *, max_digit: int) -> Optional[int]:
        """
        Возвращает цифру (1..max_digit) при нажатии (rising edge), иначе None.
        Учитывает debounce/cooldown.
        """
        now = time.time()
        if now - self._manual_switch_last_ts < self._manual_switch_cooldown:
            # всё равно обновим prev_down, чтобы edge не залипал
            for d in range(1, max_digit + 1):
                self._digit_prev_down[d] = self._is_vk_down(self._vk_digit(d))
            return None

        pressed: Optional[int] = None
        for d in range(1, max_digit + 1):
            down = self._is_vk_down(self._vk_digit(d))
            was_down = self._digit_prev_down.get(d, False)

            # rising edge
            if down and not was_down and pressed is None:
                pressed = d

            self._digit_prev_down[d] = down

        if pressed is not None:
            self._manual_switch_last_ts = now
        return pressed

    @debug_log_result
    def _get_self_uv(self, c: Dict[str, Any]) -> Optional[tuple[float, float]]:
        units = c.get("map", {})
        self_units = units.get("self", [])
        if not self_units:
            return None
        try:
            me = self_units[0]
            return float(me["x"]), float(me["y"])
        except Exception:
            return None


    def _get_forced_state(self, s: Senses) -> Optional[BrainState]:

        if not s.alive:
            return BrainState.DEAD

        if s.t_game < 110:
            return BrainState.WAIT_START

        return None
    def _clear_catboost_dataset(self) -> None:
        self._catboost_dataset_rows.clear()

    def _ensure_camps_inited(self, s: Senses) -> None:
        if self._camps_inited:
            return

        root = s.landmarks or {}

        key_to_kind = {
            "camp_small": CampKind.SMALL,
            "camp_medium": CampKind.MEDIUM,
            "camp_large": CampKind.LARGE,
        }

        camps: list[CampNode] = []
        cid = 0

        for key, kind in key_to_kind.items():
            arr = root.get(key)
            if not arr:
                continue

            # ожидаем формат как у тебя в landmarks: либо [[{x,y},...]] либо [{x,y},...]
            points_raw = arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], list) else arr
            if not isinstance(points_raw, list):
                continue

            for pt in points_raw:
                try:
                    x = float(pt["x"])
                    y = float(pt["y"])
                except Exception:
                    continue
                camps.append(CampNode(camp_id=cid, kind=kind, x=x, y=y, state=CampState.READY))
                cid += 1

        self._camps = camps
        self._camps_inited = True
    def _attack_on_screen_throttled(
        self,
        x: float,
        y: float,
        *,
        y_offset: int = 10,
        cooldown: Optional[float] = None,
    ) -> bool:
        """
        Универсальный удар кликом по экрану (крип/герой/что угодно).
        Есть кулдаун, чтобы не спамить атаки.
        """
        now = time.time()
        cd = self._attack_click_cooldown if cooldown is None else float(cooldown)
        if now - self._last_attack_click_ts < cd:
            return False

        self.pl.click_on_screen(self.hwnd, int(x), int(y) + int(y_offset), attack=True)
        self._last_attack_click_ts = now
        self.last_action_ts = now
        return True

    def _minimap_click_throttled(self, x: float, y: float, *, cooldown: Optional[float] = None) -> bool:
        now = time.time()
        cd = self._farming_minimap_click_cooldown if cooldown is None else float(cooldown)
        if now - self._farming_last_minimap_click_ts < cd:
            return False
        self.pl.click_minimap_pct(self.hwnd, x, y, attack=False)
        self._farming_last_minimap_click_ts = now
        self.last_action_ts = now
        return True
    def _walk_throttled(self, x: int, y: int, *, cooldown: Optional[float] = None,
                        tol_px: Optional[float] = None, attack: bool = False) -> bool:
        """
        Делает click_on_screen_walk, но:
          - не чаще чем cooldown секунд
          - не повторяет клик в почти ту же точку (tol_px) слишком часто
        Возвращает True если команда отправлена, иначе False.
        """
        now = time.time()
        cd = self.walk_cmd_cooldown if cooldown is None else float(cooldown)
        tol = self.walk_same_target_tol_px if tol_px is None else float(tol_px)

        if now - self._last_walk_ts < cd:
            # слишком рано
            return False

        if self._last_walk_target is not None:
            lx, ly = self._last_walk_target
            dx = float(x - lx)
            dy = float(y - ly)
            if (dx * dx + dy * dy) <= (tol * tol):
                # почти та же цель -> не спамим
                return False

        self.pl.click_on_screen_walk(self.hwnd, int(x), int(y), attack=attack)
        self._last_walk_ts = now
        self._last_walk_target = (int(x), int(y))
        self.last_action_ts = now
        return True


    @debug_log_result
    def _compute_laning_point(self, c: Dict[str, Any], min_dist_to_enemy: float) -> Optional[tuple[int, int]]:
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_list = heroes.get("self", [])
        ally_heroes = heroes.get("ally", [])
        enemy_heroes = heroes.get("enemy", [])
        ally_creeps = creeps.get("ally", [])
        enemy_creeps = creeps.get("enemy", [])

        if not self_list:
            return None

        # союзные крипы обязательны (хотя бы 1), иначе пусть решает tick_laning ("просто бить крипов")
        if len(ally_creeps) < 1:
            return None

        ally_pts = np.array([self._hpbar_center(cb) for cb in ally_creeps], dtype=np.float32)
        if ally_pts.shape[0] == 0:
            return None

        # enemy точки: сначала enemy creeps, если их нет — enemy heroes, если и их нет — пусто
        if len(enemy_creeps) >= 1:
            enemy_pts = np.array([self._hpbar_center(cb) for cb in enemy_creeps], dtype=np.float32)
        elif len(enemy_heroes) >= 1:
            enemy_pts = np.array([self._hpbar_center(b) for b in enemy_heroes], dtype=np.float32)
        else:
            enemy_pts = np.zeros((0, 2), dtype=np.float32)

        # общая линия "all"
        mid_all, d_all, n_all, cA_all, cE_all = self._compromise_line(ally_pts, enemy_pts)

        # лидеры (как было)
        leaders_ally_boxes = [self_list[0]] + list(ally_heroes)
        leaders_enemy_boxes = list(enemy_heroes)

        if not leaders_enemy_boxes:
            mid_lead, d_lead, n_lead = mid_all, d_all, n_all
        else:
            ally_leader_pts = np.array([self._hpbar_center(b) for b in leaders_ally_boxes], dtype=np.float32)
            enemy_leader_pts = np.array([self._hpbar_center(b) for b in leaders_enemy_boxes], dtype=np.float32)
            mid_lead, d_lead, n_lead, _, _ = self._compromise_line(ally_leader_pts, enemy_leader_pts)

        mix_dir = d_all + d_lead
        if np.allclose(mix_dir, 0):
            mix_dir = d_all.copy()

        norm_mix = float(np.linalg.norm(mix_dir))
        if norm_mix < 1e-6:
            return None
        mix_dir = mix_dir / norm_mix

        mid_third = 0.5 * (mid_all + mid_lead)

        vec_to_A = cA_all - mid_third
        t_proj = float(np.dot(vec_to_A, mix_dir))
        base_point = mid_third + mix_dir * t_proj

        # фронт союзных крипов по нормали
        proj_allies = ally_pts @ n_all
        front_allies = float(np.max(proj_allies))
        s_behind_allies = front_allies - float(self.lane_offset_px)

        # ограничение "дальше от врага" только если есть enemy точки
        s_far_from_enemy = None
        if enemy_pts.shape[0] > 0:
            proj_enemies = enemy_pts @ n_all
            if proj_enemies.size > 0:
                front_enemy = float(np.min(proj_enemies))
                s_far_from_enemy = front_enemy - float(min_dist_to_enemy)

        s_base = float(np.dot(base_point, n_all))
        s_target = s_behind_allies
        if s_far_from_enemy is not None:
            s_target = min(s_target, s_far_from_enemy)

        delta_s = s_target - s_base
        target = base_point + n_all * delta_s

        tx = float(target[0])
        ty = float(target[1])
        return int(round(tx)), int(round(ty))


    def _closest_point_to_polyline_uv(
        self,
        poly: List[Dict[str, float]],
        p: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        if not poly:
            return None
        qx, qy, _ = _closest_point_on_polyline(poly, p)
        return float(qx), float(qy)

    def _pick_nearest_lane_key(self, s: Senses, cur_uv: Tuple[float, float]) -> Optional[str]:
        root = s.landmarks or {}
        best_key = None
        best_d2 = 1e18

        for key in ("lane_top", "lane_mid", "lane_bot"):
            arr = root.get(key)
            if not (isinstance(arr, list) and arr and isinstance(arr[0], list) and arr[0]):
                continue
            poly = arr[0]
            q = self._closest_point_to_polyline_uv(poly, cur_uv)
            if q is None:
                continue
            d2 = _euclid2(cur_uv, q)
            if d2 < best_d2:
                best_d2 = d2
                best_key = key

        return best_key

    def _build_lane_anchor_near_t1(self, c: Dict[str, Any], s: Senses, lane_key: str) -> Optional[Tuple[float, float]]:
        """
        Точка на lane_key (0..100) ближайшая к нашей ближайшей ally T1.
        """
        root = s.landmarks or {}
        arr = root.get(lane_key)
        if not (isinstance(arr, list) and arr and isinstance(arr[0], list) and arr[0]):
            return None
        poly = arr[0]

        # Берём ближайшую ally T1 из c.towers (tier==1 или tier отсутствует)
        towers = c.get("towers", {}).get("ally", []) or []
        if not towers:
            return None

        t1 = []
        for t in towers:
            tier = t.get("tier", None)
            if tier is None or tier == 1:
                t1.append(t)
        if not t1:
            t1 = towers

        # позиция героя нужна только чтобы выбрать ближайшую T1
        cur = self._get_self_uv(c) or (50.0, 50.0)

        best_t = None
        best_d2 = 1e18
        for t in t1:
            try:
                tx, ty = float(t["x"]), float(t["y"])
            except Exception:
                continue
            d2 = _euclid2(cur, (tx, ty))
            if d2 < best_d2:
                best_d2 = d2
                best_t = (tx, ty)

        if best_t is None:
            return None

        q = self._closest_point_to_polyline_uv(poly, best_t)
        return q

    @debug_log_result
    def _make_lasthit_key(self, cb, cx: float, cy: float) -> tuple[str, object]:
        for attr in ("id", "uid", "track_id", "ent_id"):
            if hasattr(cb, attr):
                return ("id", getattr(cb, attr))
        return ("pos", (int(round(cx)), int(round(cy))))

    @debug_log_result
    def _lasthit_target_still_present(self, c: Dict[str, Any]) -> bool:
        if self._lasthit_target_key is None:
            return False

        if time.time() > self._lasthit_target_expire:
            self._lasthit_target_key = None
            return False

        kind, value = self._lasthit_target_key
        enemy_creeps = c.get("creeps", {}).get("enemy", [])
        if not enemy_creeps:
            self._lasthit_target_key = None
            return False

        if kind == "id":
            for cb in enemy_creeps:
                for attr in ("id", "uid", "track_id", "ent_id"):
                    if hasattr(cb, attr) and getattr(cb, attr) == value:
                        return True
            self._lasthit_target_key = None
            return False

        if kind == "pos":
            tx, ty = value
            max_d2 = 20.0 * 20.0
            for cb in enemy_creeps:
                cx, cy = self._hpbar_center(cb)
                d2 = (cx - tx) * (cx - tx) + (cy - ty) * (cy - ty)
                if d2 <= max_d2:
                    return True
            self._lasthit_target_key = None
            return False

        self._lasthit_target_key = None
        return False

    def _get_landmarks(self, c: Dict[str, Any]) -> Dict[str, Any]:
        """
        Возвращает словарь landmarks['data'] если есть, иначе весь landmarks.
        """
        lm = c.get("landmarks")
        return lm

    def _get_side(self) -> str:
        """
        Side хранится в Planner (как в твоём planner.py).
        """
        side = getattr(self.pl, "side", "radiant")

        return str(side).lower().strip()

    def _get_role(self) -> str:
        return self.role
    @debug_log_result
    def _reset_camps_if_needed(self, t_game: float) -> None:
        minute = int(max(0.0, float(t_game)) // 60.0)
        if minute == self._camp_reset_minute:
            return
        self._camp_reset_minute = minute
        for camp in self._camps:
            camp.state = CampState.READY
    def _retreat_point_behind_ally(
        self,
        ally_xy: Tuple[float, float],
        enemy_xy: Optional[Tuple[float, float]],
        self_xy: Tuple[float, float],
        *,
        step_px: Optional[float] = None,
    ) -> Tuple[int, int]:
        ax, ay = ally_xy
        hx, hy = self_xy
        step = self.retreat_anchor_backstep_px if step_px is None else float(step_px)

        if enemy_xy is not None:
            ex, ey = enemy_xy
            vx = ax - ex
            vy = ay - ey
        else:
            vx = ax - hx
            vy = ay - hy

        norm = float((vx * vx + vy * vy) ** 0.5)
        if norm <= 1e-6:
            return int(round(ax)), int(round(ay))

        tx = ax + vx / norm * step
        ty = ay + vy / norm * step
        return int(round(tx)), int(round(ty))
    def _reset_jungle_fight_runtime(self) -> None:
        self._farm_fight_camp_id = None
        self._farm_fight_started_near_target = False
        self._farm_no_creep_since_ts = 0.0
        self._farm_arrived_at_camp_ts = 0.0

    def _get_camp_by_id(self, camp_id: Optional[int]) -> Optional[CampNode]:
        if camp_id is None:
            return None
        for camp in self._camps:
            if camp.camp_id == camp_id:
                return camp
        return None

    def _pick_next_ready_camp(self, c: Dict[str, Any], *, kind: Optional[CampKind] = None) -> Optional[CampNode]:
        if not self._camps:
            return None
        cur = self._get_self_uv(c) or (50.0, 50.0)

        best = None
        best_d2 = 1e18
        for camp in self._camps:
            if camp.state is not CampState.READY:
                continue
            if kind is not None and camp.kind is not kind:
                continue
            d2 = _euclid2(cur, (camp.x, camp.y))
            if d2 < best_d2:
                best_d2 = d2
                best = camp
        return best

    def _is_near_camp(self, cur: Optional[Tuple[float, float]], camp: CampNode, *,
                      radius_uv: Optional[float] = None) -> bool:
        if cur is None:
            return False
        r = self._farm_near_camp_radius_uv if radius_uv is None else float(radius_uv)
        return _euclid2(cur, (camp.x, camp.y)) <= (r * r)


    def _attack_enemy_creep_on_screen(self, c: Dict[str, Any]) -> bool:
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})
        self_heroes = heroes.get("self", [])
        enemy_creeps = creeps.get("enemy", [])
        if not self_heroes or not enemy_creeps:
            return False

        hx, hy = self._hpbar_center(self_heroes[0])

        best_xy = None
        best_d2 = 1e18
        for cb in enemy_creeps:
            cx, cy = self._hpbar_center(cb)
            d2 = (cx - hx) * (cx - hx) + (cy - hy) * (cy - hy)
            if d2 < best_d2:
                best_d2 = d2
                best_xy = (cx, cy)

        if best_xy is None:
            return False

        cx, cy = best_xy
        return self._attack_on_screen_throttled(cx, cy, y_offset=10)



    def _extract_point_xy(self, obj: Any) -> Optional[Tuple[float, float]]:
        """
        Достаёт (x,y) из возможных форматов:
          - {"x":..,"y":..}
          - [{"x":..,"y":..}, ...] -> берём первый
          - [[{"x":..,"y":..}, ...]] -> берём первый первого
        """
        try:
            if isinstance(obj, dict):
                return float(obj["x"]), float(obj["y"])
            if isinstance(obj, list) and obj:
                return self._extract_point_xy(obj[0])
        except Exception:
            return None
        return None

    @debug_log_result
    def _pick_fountain_point(self, c: Dict[str, Any]) -> Tuple[float, float]:
        """
        Приоритет: fountain_<side> -> ancient_<side> -> (50,50)
        """
        root = self._get_landmarks(c)['data']
        side = self._get_side()

        for key in (f"fountain_{side}", f"ancient_{side}"):
            val = root.get(key)
            pt = self._extract_point_xy(val)
            if pt is not None:
                return pt

        return 50.0, 50.0

    @debug_log_result
    def _select_creep(
            self,
            c: Dict[str, Any],
            *,
            enemy: bool = True,
            hp_threshold: float = 0.25,
            max_dist_px: float = 550.0,
    ):
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_heroes = heroes.get("self", [])
        if not self_heroes:
            return None

        creep_list = creeps.get("enemy" if enemy else "ally", [])

        if not creep_list:
            return None

        hx, hy = self._hpbar_center(self_heroes[0])

        best_cb = None
        best_cx = best_cy = None
        best_hp = None

        max_d2 = max_dist_px * max_dist_px

        for cb in creep_list:
            hp_ratio = getattr(cb, "hp_ratio", None)
            if hp_ratio is None:
                continue
            if hp_ratio > hp_threshold:
                continue

            cx, cy = self._hpbar_center(cb)
            d2 = (cx - hx) * (cx - hx) + (cy - hy) * (cy - hy)
            if d2 > max_d2:
                continue

            if best_hp is None or hp_ratio < best_hp:
                best_hp = hp_ratio
                best_cb = cb
                best_cx, best_cy = cx, cy

        if best_cb is None:
            return None

        return best_cb, best_cx, best_cy

    def _sense_under_ally_tower(
        self,
        c: Dict[str, Any],
        *,
        radius_uv: float = 4.0,
        only_alive: bool = True,
    ) -> tuple[bool, Optional[float]]:
        units = c.get("map", {})
        self_units = units.get("self", [])
        if not self_units:
            return False, None

        try:
            me = self_units[0]
            me_x = float(me["x"])
            me_y = float(me["y"])
        except Exception:
            return False, None

        towers_all = c.get("towers", {}).get("ally", [])
        if not towers_all:
            return False, None

        towers = []
        for t in towers_all:
            if only_alive and not t.get("alive", True):
                continue
            towers.append(t)

        if not towers:
            return False, None

        dist_min: Optional[float] = None
        for t in towers:
            try:
                tx = float(t["x"])
                ty = float(t["y"])
            except Exception:
                continue
            d2 = self._dist2_uv(me_x, me_y, tx, ty)
            d = d2**0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        under = dist_min <= radius_uv
        return under, dist_min

    def _sense_ally_creep_near(self, c: Dict[str, Any]) -> tuple[bool, Optional[float]]:
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_heroes = heroes.get("self", [])
        ally_creeps = creeps.get("ally", [])

        if not self_heroes or not ally_creeps:
            return False, None

        self_center = self._hpbar_center(self_heroes[0])
        dist_min: Optional[float] = None

        for cb in ally_creeps:
            c_center = self._hpbar_center(cb)
            d2 = self._dist2_pts(self_center, c_center)
            d = d2**0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        ally_creep_near = dist_min is not None
        return ally_creep_near, dist_min

    def _sense_enemy_creep_near(self, c: Dict[str, Any]) -> tuple[bool, Optional[float]]:
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_heroes = heroes.get("self", [])
        enemy_creeps = creeps.get("enemy", [])

        if not self_heroes or not enemy_creeps:
            return False, None

        self_center = self._hpbar_center(self_heroes[0])
        dist_min: Optional[float] = None

        for cb in enemy_creeps:
            c_center = self._hpbar_center(cb)
            d2 = self._dist2_pts(self_center, c_center)
            d = d2**0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        enemy_creep_near = dist_min is not None
        return enemy_creep_near, dist_min

    def _sense_enemy_hero_near(
        self,
        c: Dict[str, Any],
        *,
        minimap_radius_uv: float = 10.0,
    ) -> tuple[bool, Optional[float], Optional[float]]:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])

        dist_screen_min: Optional[float] = None

        if self_heroes and enemy_heroes:
            self_center = self._hpbar_center(self_heroes[0])
            for eb in enemy_heroes:
                e_center = self._hpbar_center(eb)
                d2 = self._dist2_pts(self_center, e_center)
                d = d2**0.5
                if dist_screen_min is None or d < dist_screen_min:
                    dist_screen_min = d

        units = c.get("map", {})
        self_units = units.get("self", [])
        enemy_units = units.get("enemy", [])

        dist_mm_min: Optional[float] = None

        if self_units and enemy_units:
            try:
                me = self_units[0]
                me_x = float(me["x"])
                me_y = float(me["y"])
            except Exception:
                me_x = me_y = None

            if me_x is not None:
                for e in enemy_units:
                    try:
                        ex = float(e["x"])
                        ey = float(e["y"])
                    except Exception:
                        continue
                    d2 = self._dist2_uv(me_x, me_y, ex, ey)
                    d = d2**0.5
                    if dist_mm_min is None or d < dist_mm_min:
                        dist_mm_min = d

        near_by_mm = (dist_mm_min is not None and dist_mm_min <= minimap_radius_uv)
        near_by_screen = (dist_screen_min is not None)
        enemy_hero_near = bool(near_by_screen or near_by_mm)
        return enemy_hero_near, dist_screen_min, dist_mm_min

    def _sense_near_enemy_tower(
        self,
        c: Dict[str, Any],
        *,
        radius_uv: float = 7.0,
        only_alive: bool = True,
    ) -> tuple[bool, Optional[float]]:
        pos = self._get_self_uv(c)
        if pos is None:
            return False, None
        me_x, me_y = pos

        towers_all = c.get("towers", {}).get("enemy", [])
        if not towers_all:
            return False, None

        towers = []
        for t in towers_all:
            if only_alive and not t.get("alive", True):
                continue
            towers.append(t)

        if not towers:
            return False, None

        dist_min: Optional[float] = None
        for t in towers:
            try:
                tx = float(t["x"])
                ty = float(t["y"])
            except Exception:
                continue
            d2 = self._dist2_uv(me_x, me_y, tx, ty)
            d = d2**0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        near = dist_min <= radius_uv
        return near, dist_min


    @debug_log_result
    def _nearest_enemy_screen_xy(self, c: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])

        if not self_heroes or not enemy_heroes:
            return None

        hx, hy = self._hpbar_center(self_heroes[0])

        best_xy: Optional[Tuple[float, float]] = None
        best_d2 = 1e18

        for eb in enemy_heroes:
            ex, ey = self._hpbar_center(eb)
            d2 = (ex - hx) * (ex - hx) + (ey - hy) * (ey - hy)
            if d2 < best_d2:
                best_d2 = d2
                best_xy = (float(ex), float(ey))

        return best_xy
    def _should_anchor_behind_ally(self, s: Senses) -> bool:
        if s.closest_ally_hero_dist_px is not None:
            return s.closest_ally_hero_dist_px <= self.retreat_ally_screen_radius_px

        if s.closest_ally_hero_mm_dist is not None:
            return s.closest_ally_hero_mm_dist <= self.retreat_ally_mm_radius_uv

        return False
    @debug_log_result
    def _closest_enemy_hero_mm_dist(self, c: Dict[str, Any]) -> Optional[float]:
        units = c.get("map", {})
        self_units = units.get("self", [])
        enemy_units = units.get("enemy", [])

        if not self_units or not enemy_units:
            return None

        try:
            me = self_units[0]
            mx = float(me["x"])
            my = float(me["y"])
        except Exception:
            return None

        best: Optional[float] = None
        for e in enemy_units:
            try:
                ex = float(e["x"])
                ey = float(e["y"])
            except Exception:
                continue
            d = float(((ex - mx) ** 2 + (ey - my) ** 2) ** 0.5)
            if best is None or d < best:
                best = d

        return best

    @debug_log_result
    def _closest_ally_hero_dist_px(self, c: Dict[str, Any]) -> Optional[float]:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        ally_heroes = heroes.get("ally", [])

        if not self_heroes or not ally_heroes:
            return None

        hx, hy = self._hpbar_center(self_heroes[0])

        best: Optional[float] = None
        for ab in ally_heroes:
            ax, ay = self._hpbar_center(ab)
            d = float(((ax - hx) ** 2 + (ay - hy) ** 2) ** 0.5)
            if best is None or d < best:
                best = d

        return best

    @debug_log_result
    def _closest_ally_hero_mm_dist(self, c: Dict[str, Any]) -> Optional[float]:
        units = c.get("map", {})
        self_units = units.get("self", [])
        ally_units = units.get("ally", [])

        if not self_units or not ally_units:
            return None

        try:
            me = self_units[0]
            mx = float(me["x"])
            my = float(me["y"])
        except Exception:
            return None

        best: Optional[float] = None
        for a in ally_units:
            try:
                ax = float(a["x"])
                ay = float(a["y"])
            except Exception:
                continue
            d = float(((ax - mx) ** 2 + (ay - my) ** 2) ** 0.5)
            if best is None or d < best:
                best = d

        return best

    def _collect_catboost_train_row(self, s: Senses) -> None:
        if not self.collect_catboost_dataset:
            return

        row = s.to_catboost_features()
        row["brain_state"] = self.state.name.lower()
        row["ts"] = float(time.time())

        self._catboost_dataset_rows.append(row)
        self._flush_catboost_dataset_to_csv(force=False)

    @debug_log_result
    def _nearest_enemy_uv(self, c: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        cur = self._get_self_uv(c)
        if cur is None:
            return None

        enemy_units = c.get("map", {}).get("enemy", [])
        if not enemy_units:
            return None

        best_xy: Optional[Tuple[float, float]] = None
        best_d2 = 1e18

        for e in enemy_units:
            try:
                ex = float(e["x"])
                ey = float(e["y"])
            except Exception:
                continue

            d2 = _euclid2(cur, (ex, ey))
            if d2 < best_d2:
                best_d2 = d2
                best_xy = (ex, ey)

        return best_xy
    def _nearest_ally_hero_screen_xy(self, c: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        ally_heroes = heroes.get("ally", [])

        if not self_heroes or not ally_heroes:
            return None

        hx, hy = self._hpbar_center(self_heroes[0])
        best_xy = None
        best_d2 = 1e18

        for ab in ally_heroes:
            ax, ay = self._hpbar_center(ab)
            d2 = (ax - hx) * (ax - hx) + (ay - hy) * (ay - hy)
            if d2 < best_d2:
                best_d2 = d2
                best_xy = (float(ax), float(ay))

        return best_xy
    def _normalize_brain_state(self, raw_pred: Any) -> Optional[BrainState]:
        if raw_pred is None:
            return None

        if isinstance(raw_pred, np.ndarray):
            if raw_pred.size == 0:
                return None
            raw_pred = raw_pred.flatten()[0]

        if isinstance(raw_pred, (list, tuple)):
            if not raw_pred:
                return None
            raw_pred = raw_pred[0]

        if isinstance(raw_pred, np.generic):
            raw_pred = raw_pred.item()

        if isinstance(raw_pred, (int, np.integer)):
            return self._brain_state_id_map.get(int(raw_pred))

        if isinstance(raw_pred, str):
            key = raw_pred.strip().lower()
            aliases = {
                "farming_jungle": BrainState.FARMING_JUNGLE,
                "farming_lane": BrainState.FARMING_LANE,
                "laning": BrainState.LANING,
                "fighting": BrainState.FIGHTING,
                "retreat": BrainState.RETREAT,
                "idle": BrainState.IDLE,
            }
            return aliases.get(key)

        return None

    def _build_retreat_path_uv(
            self,
            c: Dict[str, Any],
            *,
            enemy_uv: Optional[Tuple[float, float]],
            fountain_uv: Tuple[float, float],
    ) -> List[Tuple[float, float]]:
        cur = self._get_self_uv(c)
        if cur is None:
            return [fountain_uv]

        sx, sy = cur
        fx, fy = fountain_uv

        dx = fx - sx
        dy = fy - sy
        dist_sf = float((dx * dx + dy * dy) ** 0.5)
        if dist_sf <= 1e-6:
            return [fountain_uv]

        ux = dx / dist_sf
        uy = dy / dist_sf

        # нормаль к линии self -> fountain
        nx = -uy
        ny = ux

        # сторону выбираем ОТ врага
        side_sign = 1.0
        if enemy_uv is not None:
            ex, ey = enemy_uv
            vex = ex - sx
            vey = ey - sy
            side = vex * nx + vey * ny
            side_sign = -1.0 if side > 0.0 else 1.0

        side_off = max(8.0, float(self.retreat_path_side_offset_uv))
        fwd1 = max(8.0, float(self.retreat_path_forward_gain_uv) * 0.8)
        fwd2 = max(14.0, float(self.retreat_path_forward_gain_uv) * 1.8)
        fwd3 = max(22.0, float(self.retreat_path_forward_gain_uv) * 2.8)

        # 1) резко уйти вбок от опасной линии
        p1 = (
            sx + nx * side_sign * (side_off * 1.35),
            sy + ny * side_sign * (side_off * 1.35),
        )

        # 2) двигаться вперёд, оставаясь на безопасной стороне
        p2 = (
            sx + ux * fwd1 + nx * side_sign * (side_off * 1.45),
            sy + uy * fwd1 + ny * side_sign * (side_off * 1.45),
        )

        p3 = (
            sx + ux * fwd2 + nx * side_sign * (side_off * 1.15),
            sy + uy * fwd2 + ny * side_sign * (side_off * 1.15),
        )

        # 3) только ближе к концу начинаем возвращаться к фонтану
        p4 = (
            sx + ux * fwd3 + nx * side_sign * (side_off * 0.45),
            sy + uy * fwd3 + ny * side_sign * (side_off * 0.45),
        )

        ctrl = [p1, p2, p3, p4, (fx, fy)]

        pts: List[Tuple[float, float]] = []
        min_gap2 = 2.0 * 2.0

        for pt in ctrl:
            x = max(0.0, min(100.0, float(pt[0])))
            y = max(0.0, min(100.0, float(pt[1])))

            # не добавляем точки слишком близко к текущей позиции
            if _euclid2((sx, sy), (x, y)) < (self.retreat_path_arrive_radius_uv * self.retreat_path_arrive_radius_uv):
                continue

            if pts and _euclid2(pts[-1], (x, y)) < min_gap2:
                continue

            pts.append((x, y))

        if not pts or _euclid2(pts[-1], (fx, fy)) > 1.0:
            pts.append((float(fx), float(fy)))

        return pts

    def _follow_retreat_path(self, c: Dict[str, Any]) -> bool:
        cur = self._get_self_uv(c)
        if cur is None:
            return False

        if not self._retreat_path_uv:
            return False

        # пропускаем только реально достигнутые точки
        while self._retreat_path_idx < len(self._retreat_path_uv):
            tx, ty = self._retreat_path_uv[self._retreat_path_idx]
            if _euclid2(cur, (tx, ty)) <= (self.retreat_path_arrive_radius_uv ** 2):
                self._retreat_path_idx += 1
            else:
                break

        # весь путь завершён
        if self._retreat_path_idx >= len(self._retreat_path_uv):
            return False

        tx, ty = self._retreat_path_uv[self._retreat_path_idx]

        sent = self._minimap_click_throttled(tx, ty, cooldown=0.35)
        if sent:
            return True

        now = time.time()
        if (now - self._farming_last_minimap_click_ts) > 0.65:
            self.pl.click_minimap_pct(self.hwnd, tx, ty, attack=False)
            self._farming_last_minimap_click_ts = now
            self.last_action_ts = now
            return True

        # даже если клик сейчас не ушёл, путь всё ещё активен
        return True
    def _should_rebuild_retreat_path(self, now: float) -> bool:
        if not self._retreat_path_uv:
            return True
        if self._retreat_path_idx >= len(self._retreat_path_uv):
            return True
        if now - self._retreat_path_built_ts >= self.retreat_path_rebuild_sec:
            return True
        return False

    def _lane_landmarks_root(self, c: Dict[str, Any], s: Senses) -> Dict[str, Any]:
        if isinstance(s.landmarks, dict):
            return s.landmarks
        lm = c.get("landmarks")
        if isinstance(lm, dict):
            data = lm.get("data")
            if isinstance(data, dict):
                return data
            return lm
        return {}

    def _get_ally_creep_anchor_screen(self, c: Dict[str, Any], *, back_offset_px: float = 90.0) -> Optional[
        Tuple[int, int]]:
        creeps = c.get("creeps", {})
        ally_creeps = creeps.get("ally", [])
        enemy_creeps = creeps.get("enemy", [])

        if not ally_creeps:
            return None

        ally_pts = np.array([self._hpbar_center(cb) for cb in ally_creeps], dtype=np.float32)

        if len(enemy_creeps) >= 1:
            enemy_pts = np.array([self._hpbar_center(cb) for cb in enemy_creeps], dtype=np.float32)
        else:
            enemy_pts = np.zeros((0, 2), dtype=np.float32)

        mid, d, n, c_a, c_e = self._compromise_line(ally_pts, enemy_pts)

        # фронт союзных крипов по нормали к линии
        proj_allies = ally_pts @ n
        front_allies = float(np.max(proj_allies))

        # стоим чуть позади своей пачки
        s_target = front_allies - float(back_offset_px)
        s_base = float(np.dot(mid, n))
        delta_s = s_target - s_base
        target = mid + n * delta_s

        tx = int(round(float(target[0])))
        ty = int(round(float(target[1])))
        return tx, ty
    def _get_lane_polyline(self, c: Dict[str, Any], s: Senses, lane_key: str) -> List[Dict[str, float]]:
        root = self._lane_landmarks_root(c, s)
        arr = root.get(lane_key)
        if isinstance(arr, list) and arr and isinstance(arr[0], list):
            return arr[0]
        if isinstance(arr, list):
            return arr
        return []

    def _project_to_lane(
        self,
        poly: List[Dict[str, float]],
        p: Tuple[float, float],
    ) -> Optional[Tuple[Tuple[float, float], float, float]]:
        if not poly:
            return None

        pts: List[Tuple[float, float]] = []
        for node in poly:
            try:
                pts.append((float(node["x"]), float(node["y"])))
            except Exception:
                continue

        if not pts:
            return None
        if len(pts) == 1:
            dx = p[0] - pts[0][0]
            dy = p[1] - pts[0][1]
            return (pts[0], 0.0, dx * dx + dy * dy)

        seg_lens: List[float] = []
        total_len = 0.0
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            seg_len = float(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5)
            seg_lens.append(seg_len)
            total_len += seg_len

        if total_len <= 1e-9:
            dx = p[0] - pts[0][0]
            dy = p[1] - pts[0][1]
            return (pts[0], 0.0, dx * dx + dy * dy)

        best_q = pts[0]
        best_d2 = 1e18
        best_t = 0.0
        len_before = 0.0
        px, py = p

        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            abx, aby = bx - ax, by - ay
            ab2 = abx * abx + aby * aby
            if ab2 <= 1e-12:
                len_before += seg_lens[i]
                continue

            apx, apy = px - ax, py - ay
            t_seg = (apx * abx + apy * aby) / ab2
            t_seg = max(0.0, min(1.0, t_seg))

            qx = ax + t_seg * abx
            qy = ay + t_seg * aby
            dx = px - qx
            dy = py - qy
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_q = (qx, qy)
                best_t = (len_before + seg_lens[i] * t_seg) / total_len

            len_before += seg_lens[i]

        return best_q, float(best_t), float(best_d2)

    def _pick_nearest_ally_t1_uv(self, c: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        cur = self._get_self_uv(c)
        if cur is None:
            return None

        towers = c.get("towers", {}).get("ally", []) or []
        if not towers:
            return None

        t1_list = []
        for t in towers:
            tier = t.get("tier", None)
            if tier is None or tier == 1:
                t1_list.append(t)

        if not t1_list:
            t1_list = towers

        best = None
        best_d2 = 1e18
        for t in t1_list:
            try:
                tx = float(t["x"])
                ty = float(t["y"])
            except Exception:
                continue
            d2 = _euclid2(cur, (tx, ty))
            if d2 < best_d2:
                best_d2 = d2
                best = (tx, ty)

        return best
    def _point_at_progress(self, poly: List[Dict[str, float]], t: float) -> Optional[Tuple[float, float]]:
        if not poly:
            return None

        pts: List[Tuple[float, float]] = []
        for node in poly:
            try:
                pts.append((float(node["x"]), float(node["y"])))
            except Exception:
                continue

        if not pts:
            return None
        if len(pts) == 1:
            return pts[0]

        t = max(0.0, min(1.0, float(t)))

        seg_lens: List[float] = []
        total_len = 0.0
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            seg_len = float(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5)
            seg_lens.append(seg_len)
            total_len += seg_len

        if total_len <= 1e-9:
            return pts[0]

        target_len = t * total_len
        walked = 0.0
        for i in range(len(pts) - 1):
            seg_len = seg_lens[i]
            if seg_len <= 1e-12:
                continue
            if walked + seg_len >= target_len:
                ratio = (target_len - walked) / seg_len
                ax, ay = pts[i]
                bx, by = pts[i + 1]
                return ax + (bx - ax) * ratio, ay + (by - ay) * ratio
            walked += seg_len

        return pts[-1]

    def _oriented_progress(self, raw_t: float, lane_reversed: bool) -> float:
        raw_t = max(0.0, min(1.0, float(raw_t)))
        return (1.0 - raw_t) if lane_reversed else raw_t

    def _resolve_base_uv(self, c: Dict[str, Any], s: Senses, *, ally: bool) -> Optional[Tuple[float, float]]:
        root = self._lane_landmarks_root(c, s)
        my_side = self._get_side()
        other_side = "dire" if my_side == "radiant" else "radiant"
        side = my_side if ally else other_side

        for key in (f"fountain_{side}", f"ancient_{side}"):
            pt = self._extract_point_xy(root.get(key))
            if pt is not None:
                return pt
        return None

    def _lane_is_reversed(self, c: Dict[str, Any], s: Senses, poly: List[Dict[str, float]]) -> bool:
        ally_base = self._resolve_base_uv(c, s, ally=True)
        enemy_base = self._resolve_base_uv(c, s, ally=False)
        if ally_base is None or enemy_base is None:
            return False

        proj_ally = self._project_to_lane(poly, ally_base)
        proj_enemy = self._project_to_lane(poly, enemy_base)
        if proj_ally is None or proj_enemy is None:
            return False

        _, t_ally, _ = proj_ally
        _, t_enemy, _ = proj_enemy
        return t_ally > t_enemy

    def _front_tower_progresses(
        self,
        towers: List[Dict[str, Any]],
        poly: List[Dict[str, float]],
        *,
        lane_reversed: bool,
    ) -> List[Tuple[Dict[str, Any], float]]:
        out: List[Tuple[Dict[str, Any], float]] = []
        for t in towers:
            if not bool(t.get("alive", True)):
                continue
            try:
                tx = float(t["x"])
                ty = float(t["y"])
            except Exception:
                continue

            proj = self._project_to_lane(poly, (tx, ty))
            if proj is None:
                continue

            _, raw_t, dist2 = proj
            if (dist2 ** 0.5) > self.lane_attach_uv:
                continue

            out.append((t, self._oriented_progress(raw_t, lane_reversed)))

        return out

    def _build_lane_target_between_front_towers(
        self,
        c: Dict[str, Any],
        s: Senses,
        lane_key: str,
    ) -> Optional[Tuple[float, float]]:
        poly = self._get_lane_polyline(c, s, lane_key)
        if len(poly) < 2:
            return None

        lane_reversed = self._lane_is_reversed(c, s, poly)

        towers = c.get("towers", {})
        ally_towers = towers.get("ally", []) or []
        enemy_towers = towers.get("enemy", []) or []

        ally_progress = self._front_tower_progresses(ally_towers, poly, lane_reversed=lane_reversed)
        enemy_progress = self._front_tower_progresses(enemy_towers, poly, lane_reversed=lane_reversed)

        if not ally_progress or not enemy_progress:
            return None

        _, t_ally_front = max(ally_progress, key=lambda x: x[1])
        _, t_enemy_front = min(enemy_progress, key=lambda x: x[1])

        if t_enemy_front <= t_ally_front:
            return None

        seg = t_enemy_front - t_ally_front
        t_target = t_ally_front + seg * 0.35
        t_cap = t_ally_front + seg * self.lane_max_depth_frac
        t_target = min(t_target, t_cap)

        t_min = t_ally_front + self.lane_tower_margin
        t_max = t_enemy_front - self.lane_tower_margin
        if t_max <= t_min:
            return None

        t_target = max(t_target, t_min)
        t_target = min(t_target, t_max)

        raw_t = (1.0 - t_target) if lane_reversed else t_target
        return self._point_at_progress(poly, raw_t)

    def _reset_runtime_substates_on_death(self) -> None:
        # base stateful action memory
        self._moving_point = None

        # lasthit
        self._lasthit_target_key = None
        self._lasthit_target_expire = 0.0

        # jungle farm
        self._farm_phase = FarmPhase.PICK_TARGET
        self._farm_target_id = None

        # lane farm
        self._reset_lane_farm_memory()

        # optional lane init memory
        self._lane_inited = False
        self._lane_key = None
        self._lane_anchor_uv = None

        # cooldown-ish memory
        self._last_walk_target = None
        self.last_action_ts = 0.0

        self._retreat_phase = RetreatPhase.DIRECT_TO_FOUNTAIN
        self._retreat_enter_ts = 0.0
        self._retreat_phase_ts = 0.0
        self._retreat_enemy_memory_until = 0.0

        self._retreat_path_uv = []
        self._retreat_path_idx = 0
        self._retreat_path_built_ts = 0.0

    def _reset_lane_farm_memory(self) -> None:
        self._lane_target_uv = None
        self._lane_wave_seen = False
        self._lane_wave_last_seen_ts = 0.0
        self._lane_phase_start_ts = 0.0
        self._lane_farm_phase = LaneFarmPhase.SELECT_SEGMENT





    def _set_state(self, new_state: BrainState):
        if new_state is self.state:
            return

        if self.log:
            self.log.info(f"[BRAIN {hex(self.hwnd)}] {self.state.name} -> {new_state.name}")

        old_state = self.state
        self.state = new_state
        self.last_action_ts = 0.0
        if new_state is BrainState.RETREAT and old_state is not BrainState.RETREAT:
            now = time.time()
            self._retreat_enter_ts = now
            self._retreat_phase_ts = now
            self._retreat_enemy_memory_until = 0.0
            self._retreat_phase = RetreatPhase.GO_BEHIND_ALLY
            self._retreat_path_uv = []
            self._retreat_path_idx = 0
            self._retreat_path_built_ts = 0.0

        if new_state is BrainState.DEAD and old_state is not BrainState.DEAD:
            self._reset_runtime_substates_on_death()

        if old_state is BrainState.DEAD and new_state is not BrainState.DEAD:
            # после респавна стартуем чисто
            self._reset_lane_farm_memory()
            self._farm_phase = FarmPhase.PICK_TARGET
            self._farm_target_id = None
            self._lasthit_target_key = None
            self._lasthit_target_expire = 0.0


    def _tick_dead(self, c: Dict[str, Any], s: Senses):
        if s.alive:
            self._set_state(BrainState.IDLE)


    def _tick_idle(self, c: Dict[str, Any], s: Senses) -> None:
        pass

    def _tick_laning(self, c: Dict[str, Any], s: Senses):
        """
        Лайнинг:
          1) если есть активная цель для ластхита и она ещё жива — ждём.
          2) если нет союзных крипов:
             - под своей T1 можно безопасно бить enemy creeps
             - иначе откатываемся к своей T1
          3) если есть безопасный ластхит enemy creep — подходим/атакуем
          4) иначе пробуем deny
          5) если ни ластхит, ни deny не подходят — занимаем позицию по линии
        """
        now = time.time()

        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        if not self_heroes:
            return

        # --- 0) если союзных крипов нет ---
        ally_creeps = c.get("creeps", {}).get("ally", [])
        if len(ally_creeps) == 0:
            if s.enemy_creep_near and s.under_ally_tower:
                self._attack_enemy_creep_on_screen(c)
                return

            t1_uv = self._pick_nearest_ally_t1_uv(c)
            if t1_uv is not None:
                tx, ty = t1_uv
                self._minimap_click_throttled(tx, ty, cooldown=0.8)
            return

        # --- 1) если уже атакуем цель — ждём её смерти ---
        if self._lasthit_target_key is not None:
            if self._lasthit_target_still_present(c):
                if self.log:
                    self.log.debug("[BRAIN] lasthit: waiting current target to die")
                return
            else:
                if self.log:
                    self.log.debug("[BRAIN] lasthit: target gone, resume laning")
                self._lasthit_target_key = None

        # --- 1.5) инициализация линии / якоря у своей T1 ---
        if not self._lane_inited:
            cur_uv = self._get_self_uv(c) or (50.0, 50.0)
            lane_key = self._pick_nearest_lane_key(s, cur_uv)
            if lane_key is not None:
                anchor = self._build_lane_anchor_near_t1(c, s, lane_key)
                if anchor is not None:
                    self._lane_key = lane_key
                    self._lane_anchor_uv = anchor
                    self._lane_inited = True

        # Если крипов вообще не видно — идём к якорю линии
        if (not s.enemy_creep_near) and (not s.ally_creep_near):
            if self._lane_anchor_uv is not None:
                ax, ay = self._lane_anchor_uv
                self._minimap_click_throttled(ax, ay, cooldown=0.8)
            return

        # Понадобится для fallback reposition
        dist_to_enemy = self._compute_dist_to_enemy_hero(s)

        # --- 2) пробуем ластхит enemy creep ---
        lasthit_candidate = self._select_creep(c, hp_threshold=0.5, enemy=True)

        if lasthit_candidate is not None:
            cb, cx, cy = lasthit_candidate
            creep_hp = float(getattr(cb, "hp_ratio", 1.0))

            hx, hy = self._hpbar_center(self_heroes[0])
            dist = float(((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5)

            enemy_cnt = s.enemy_hero_cnt_screen
            avg_enemy_hp = s.avg_enemy_hero_hp_ratio_screen

            allow = self._should_approach_for_lasthit(
                s,
                creep_hp=creep_hp,
                dist_to_creep_px=dist,
                enemy_hero_cnt_near=enemy_cnt,
                avg_enemy_hp=avg_enemy_hp,
            )

            if allow:
                attack_dist_px = 260.0

                if dist > attack_dist_px:
                    if self.log:
                        self.log.debug(
                            f"[BRAIN] lasthit: APPROACH creep at ({cx:.0f},{cy:.0f}) "
                            f"dist={dist:.0f}px creep_hp={creep_hp:.2f}"
                        )

                    sent = self._walk_throttled(
                        int(cx),
                        int(cy) + 10,
                        cooldown=self.lasthit_cmd_cooldown,
                        tol_px=14.0,
                        attack=False,
                    )
                    if sent and self.log:
                        self.log.debug("[BRAIN] lasthit: walk command sent")
                    return

                if self.log:
                    self.log.debug(
                        f"[BRAIN] lasthit: ATTACK creep at ({cx:.0f},{cy:.0f}), "
                        f"hp_ratio={creep_hp:.2f}"
                    )

                if not self._attack_on_screen_throttled(cx, cy, y_offset=10):
                    return

                key = self._make_lasthit_key(cb, cx, cy)
                self._lasthit_target_key = key
                self._lasthit_target_expire = now + 1.5
                self.last_action_ts = now
                return
            else:
                if self.log:
                    self.log.debug(
                        f"[BRAIN] lasthit: SKIP (risk) creep_hp={creep_hp:.2f}, "
                        f"dist={dist:.0f}px, enemy_cnt={enemy_cnt}, avg_enemy_hp={avg_enemy_hp}"
                    )
                # НЕ return — дальше идём в reposition / deny

        # --- 2.5) DENY: добивание своих ---
        deny_candidate = self._select_creep(c, hp_threshold=0.3, enemy=False)

        if deny_candidate is not None:
            cb, cx, cy = deny_candidate
            creep_hp = float(getattr(cb, "hp_ratio", 1.0))

            hx, hy = self._hpbar_center(self_heroes[0])
            dist = float(((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5)

            enemy_cnt = s.enemy_hero_cnt_screen
            avg_enemy_hp = s.avg_enemy_hero_hp_ratio_screen

            allow = self._should_approach_for_lasthit(
                s,
                creep_hp=creep_hp,
                dist_to_creep_px=dist,
                enemy_hero_cnt_near=enemy_cnt,
                avg_enemy_hp=avg_enemy_hp,
            )

            if allow:
                attack_dist_px = 260.0

                if dist > attack_dist_px:
                    self._walk_throttled(
                        int(cx),
                        int(cy) + 10,
                        cooldown=self.lasthit_cmd_cooldown,
                        tol_px=14.0,
                        attack=False,
                    )
                    return

                if not self._attack_on_screen_throttled(cx, cy, y_offset=10):
                    return

                key = self._make_lasthit_key(cb, cx, cy)
                self._lasthit_target_key = key
                self._lasthit_target_expire = now + 1.5
                self.last_action_ts = now
                return

        # --- 3) обычный лайнинг по линии / reposition ---
        target = self._compute_laning_point(c, min_dist_to_enemy=dist_to_enemy)
        if target is None:
            # fallback: если не смогли вычислить точку, хотя бы держимся у якоря линии
            if self._lane_anchor_uv is not None:
                ax, ay = self._lane_anchor_uv
                self._minimap_click_throttled(ax, ay, cooldown=0.8)
            return

        tx, ty = target

        if self.log:
            self.log.debug(f"[BRAIN] laning: move to ({tx},{ty})")

        self._walk_throttled(tx, ty, cooldown=0.35, tol_px=22.0, attack=False)

    def _tick_farming_jungle(self, c: Dict[str, Any], s: Senses) -> None:
        self._ensure_camps_inited(s)
        self._reset_camps_if_needed(s.t_game)

        if not self._camps:
            return

        now = time.time()
        cur = self._get_self_uv(c)

        # 1) если уже в бою
        if self._farm_phase is FarmPhase.FIGHT:
            # пока крипы видны — продолжаем бой
            if s.enemy_creep_near:
                self._farm_no_creep_since_ts = 0.0
                self._attack_enemy_creep_on_screen(c)
                return

            # крипы пропали — подтверждаем окончание
            if self._farm_no_creep_since_ts <= 0.0:
                self._farm_no_creep_since_ts = now
                return

            if now - self._farm_no_creep_since_ts < self._farm_cleared_confirm_delay:
                return

            # если бой начался у target camp — считаем target camp очищенным
            if self._farm_fight_started_near_target and self._farm_target_id is not None:
                target_camp = self._get_camp_by_id(self._farm_target_id)
                if target_camp is not None:
                    target_camp.state = CampState.CLEARED
                    if self.log:
                        self.log.info(
                            f"[BRAIN {hex(self.hwnd)}] jungle: target camp {target_camp.camp_id} cleared"
                        )

                self._farm_target_id = None
                self._farm_phase = FarmPhase.PICK_TARGET
                self._reset_jungle_fight_runtime()
                return

            # если бой был не у target camp — просто возвращаемся к planned target
            if self._farm_target_id is not None:
                self._farm_phase = FarmPhase.MOVE_TO_TARGET
            else:
                self._farm_phase = FarmPhase.PICK_TARGET

            self._reset_jungle_fight_runtime()
            return

        # 2) выбираем target camp
        if self._farm_phase is FarmPhase.PICK_TARGET:
            if self._farm_target_id is None:
                nxt = self._pick_next_ready_camp(c)
                if nxt is None:
                    return
                self._farm_target_id = nxt.camp_id

            self._farm_phase = FarmPhase.MOVE_TO_TARGET
            self._reset_jungle_fight_runtime()

        # 3) идём к target camp
        if self._farm_phase is FarmPhase.MOVE_TO_TARGET:
            target_camp = self._get_camp_by_id(self._farm_target_id)
            if target_camp is None:
                self._farm_target_id = None
                self._farm_phase = FarmPhase.PICK_TARGET
                self._reset_jungle_fight_runtime()
                return

            near_target_camp = self._is_near_camp(cur, target_camp)

            # 3.1 если по пути увидели крипов — фармим, но target не забываем
            if s.enemy_creep_near and not near_target_camp:
                self._farm_phase = FarmPhase.FIGHT
                self._farm_fight_started_near_target = False
                self._farm_no_creep_since_ts = 0.0
                self._farm_arrived_at_camp_ts = 0.0

                if self.log:
                    self.log.info(
                        f"[BRAIN {hex(self.hwnd)}] jungle: route fight start (planned_target={self._farm_target_id})"
                    )

                self._attack_enemy_creep_on_screen(c)
                return

            # 3.2 если уже у target camp и там есть крипы — это target fight
            if near_target_camp and s.enemy_creep_near:
                self._farm_phase = FarmPhase.FIGHT
                self._farm_fight_started_near_target = True
                self._farm_no_creep_since_ts = 0.0
                self._farm_arrived_at_camp_ts = 0.0

                if self.log:
                    self.log.info(
                        f"[BRAIN {hex(self.hwnd)}] jungle: target fight start (target={target_camp.camp_id})"
                    )

                self._attack_enemy_creep_on_screen(c)
                return

            # 3.3 если пришли к camp, но там никого нет слишком долго — skip
            if near_target_camp:
                if self._farm_arrived_at_camp_ts <= 0.0:
                    self._farm_arrived_at_camp_ts = now

                if now - self._farm_arrived_at_camp_ts >= self._farm_stuck_near_camp_timeout:
                    if self.log:
                        self.log.info(
                            f"[BRAIN {hex(self.hwnd)}] jungle: target camp {target_camp.camp_id} stuck timeout, skip"
                        )

                    target_camp.state = CampState.SKIPPED
                    self._farm_target_id = None
                    self._farm_phase = FarmPhase.PICK_TARGET
                    self._reset_jungle_fight_runtime()
                    return
            else:
                self._farm_arrived_at_camp_ts = 0.0

            self._minimap_click_throttled(target_camp.x, target_camp.y)

    def _tick_farming_lane(self, c: Dict[str, Any], s: Senses) -> None:
        now = time.time()

        if self._lane_farm_phase is LaneFarmPhase.SELECT_SEGMENT:
            if self._lane_key is None:
                cur_uv = self._get_self_uv(c) or (50.0, 50.0)
                self._lane_key = self._pick_nearest_lane_key(s, cur_uv)

            if self._lane_key is None:
                self._lane_farm_phase = LaneFarmPhase.ABORT
            else:
                target_uv = self._build_lane_target_between_front_towers(c, s, self._lane_key)
                if target_uv is None:
                    self._lane_farm_phase = LaneFarmPhase.ABORT
                else:
                    self._lane_target_uv = target_uv
                    self._lane_wave_seen = False
                    self._lane_wave_last_seen_ts = 0.0
                    self._lane_phase_start_ts = now
                    self._lane_farm_phase = LaneFarmPhase.MOVE_TO_POINT

        if self._lane_farm_phase is LaneFarmPhase.MOVE_TO_POINT:
            if self._lane_target_uv is None:
                self._lane_farm_phase = LaneFarmPhase.ABORT
            else:
                cur_uv = self._get_self_uv(c)
                tx, ty = self._lane_target_uv

                if cur_uv is not None:
                    if _euclid2(cur_uv, (tx, ty)) <= (self.lane_arrive_radius_uv ** 2):
                        self._lane_phase_start_ts = now
                        self._lane_farm_phase = LaneFarmPhase.WAIT_OR_CLEAR_WAVE
                    elif now - self._lane_phase_start_ts >= self.lane_move_timeout:
                        self._lane_farm_phase = LaneFarmPhase.ABORT
                    else:
                        self._minimap_click_throttled(tx, ty)
                else:
                    if now - self._lane_phase_start_ts >= self.lane_move_timeout:
                        self._lane_farm_phase = LaneFarmPhase.ABORT

        if self._lane_farm_phase is LaneFarmPhase.WAIT_OR_CLEAR_WAVE:
            if s.low_hp or s.near_enemy_tower:
                self._lane_farm_phase = LaneFarmPhase.ABORT
            else:
                if s.enemy_creep_near:
                    self._lane_wave_seen = True
                    self._lane_wave_last_seen_ts = now

                    enemy_creeps = c.get("creeps", {}).get("enemy", [])
                    enemy_creep_count = len(enemy_creeps) if enemy_creeps else 0
                    now_ts = time.time()

                    # 1) если уже есть цель на ластхит — не залипаем вечно
                    if self._lasthit_target_key is not None:
                        if self._lasthit_target_still_present(c):
                            # если это уже почти точно последний крип, даём возможность переиздать атаку раньше
                            if enemy_creep_count <= 1 and now_ts >= (self._lasthit_target_expire - 0.35):
                                self._lasthit_target_key = None
                            elif now_ts < self._lasthit_target_expire:
                                return
                            else:
                                self._lasthit_target_key = None
                        else:
                            self._lasthit_target_key = None

                    # 2) сначала пробуем ластхит — особенно важно для 1 оставшегося крипа
                    lasthit_candidate = self._select_creep(c, hp_threshold=0.6, enemy=True)
                    if lasthit_candidate is not None:
                        cb, cx, cy = lasthit_candidate

                        heroes = c.get("heroes", {})
                        self_heroes = heroes.get("self", [])
                        dist = 9999.0
                        if self_heroes:
                            hx, hy = self._hpbar_center(self_heroes[0])
                            dist = float(((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5)

                        # для последнего крипа можно чуть агрессивнее заходить
                        attack_dist_px = 290.0 if enemy_creep_count <= 1 else 260.0
                        walk_tol = 12.0 if enemy_creep_count <= 1 else 14.0

                        if dist > attack_dist_px:
                            self._walk_throttled(
                                int(cx),
                                int(cy) + 10,
                                cooldown=self.lasthit_cmd_cooldown,
                                tol_px=walk_tol,
                                attack=False,
                            )
                            return

                        attacked = self._attack_on_screen_throttled(cx, cy, y_offset=10, cooldown=0.25)
                        if attacked:
                            self._lasthit_target_key = self._make_lasthit_key(cb, cx, cy)
                            self._lasthit_target_expire = now_ts + 0.8
                            self.last_action_ts = now_ts
                            return

                    # 3) только если last hit не нужен — держимся за своей пачкой
                    if s.ally_creep_near and enemy_creep_count > 1:
                        anchor = self._get_ally_creep_anchor_screen(c, back_offset_px=90.0)
                        if anchor is not None:
                            ax, ay = anchor
                            if s.enemy_hero_near:
                                self._walk_throttled(ax, ay, cooldown=0.45, tol_px=20.0, attack=False)
                            else:
                                self._walk_throttled(ax, ay, cooldown=0.35, tol_px=20.0, attack=False)

                    # 4) если рядом вражеский герой — лишний раз не пушим
                    if s.enemy_hero_near:
                        return

                    # 5) иначе просто бьём ближайшего enemy creep
                    self._attack_enemy_creep_on_screen(c)
                else:
                    # если enemy creep нет, но ally creeps видны — идём за ними
                    if s.ally_creep_near and not s.enemy_hero_near and not s.near_enemy_tower:
                        anchor = self._get_ally_creep_anchor_screen(c, back_offset_px=-10.0)
                        if anchor is not None:
                            ax, ay = anchor
                            self._walk_throttled(ax, ay, cooldown=0.35, tol_px=20.0, attack=False)

                    if self._lane_wave_seen and (now - self._lane_wave_last_seen_ts >= self.lane_wave_clear_grace):
                        self._lane_farm_phase = LaneFarmPhase.DONE

                waited = now - self._lane_phase_start_ts
                if waited >= self.lane_wave_timeout:
                    self._lane_farm_phase = LaneFarmPhase.DONE
                if (not self._lane_wave_seen) and (waited >= self.lane_find_wave_timeout):
                    self._lane_farm_phase = LaneFarmPhase.DONE

        if self._lane_farm_phase in (LaneFarmPhase.DONE, LaneFarmPhase.ABORT):
            self._reset_lane_farm_memory()

    def _tick_fighting(self, c: Dict[str, Any], s: Senses):
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])

        if not self_heroes:
            return

        # Если врагов не видно — ничего не делаем.
        # Переключение состояния пусть решает внешний роутинг.
        if not enemy_heroes:
            return

        hx, hy = self._hpbar_center(self_heroes[0])

        # Выбираем ближайшего врага
        best_enemy = None
        best_xy = None
        best_dist = None

        for eb in enemy_heroes:
            ex, ey = self._hpbar_center(eb)
            dist = float(((ex - hx) ** 2 + (ey - hy) ** 2) ** 0.5)

            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_enemy = eb
                best_xy = (ex, ey)

        if best_enemy is None or best_xy is None or best_dist is None:
            return

        ex, ey = best_xy
        my_hp = 1.0 if s.hp_ratio is None else max(0.0, min(1.0, float(s.hp_ratio)))
        enemy_hp = getattr(best_enemy, "hp_ratio", None)
        enemy_hp = None if enemy_hp is None else max(0.0, min(1.0, float(enemy_hp)))

        enemy_cnt = s.enemy_hero_cnt_screen
        under_ally_tower = s.under_ally_tower
        near_enemy_tower = s.near_enemy_tower

        # Базовые дистанции
        attack_dist_px = 240.0
        chase_dist_px = 420.0
        safe_hold_dist_px = 320.0

        # Под своей башней можно играть смелее
        if under_ally_tower:
            attack_dist_px = 270.0
            chase_dist_px = 470.0
            safe_hold_dist_px = 360.0

        # Оценка риска без смены состояния
        very_risky = False

        if s.low_hp:
            very_risky = True

        if enemy_cnt >= 2 and my_hp < 0.65 and not under_ally_tower:
            very_risky = True

        if near_enemy_tower:
            if enemy_hp is None:
                very_risky = True
            elif my_hp < 0.75 or enemy_hp > 0.35:
                very_risky = True

        if enemy_hp is not None and (my_hp + 0.15) < enemy_hp and not under_ally_tower:
            very_risky = True

        # Осторожный режим: не пушим, а просто держим дистанцию / кайтим назад
        if very_risky:
            # если враг слишком близко — немного отходим от него
            if best_dist < safe_hold_dist_px:
                dx = hx - ex
                dy = hy - ey
                norm = float((dx * dx + dy * dy) ** 0.5)

                if norm > 1e-6:
                    step = 90.0
                    tx = int(round(hx + dx / norm * step))
                    ty = int(round(hy + dy / norm * step))
                    self._walk_throttled(tx, ty, cooldown=1, tol_px=18.0, attack=False)
            return

        # Если можем добить врага с низким hp — приоритетно бьём
        if enemy_hp is not None and enemy_hp <= 0.25:
            attacked = self._attack_on_screen_throttled(ex, ey, y_offset=10, cooldown=1)
            if attacked:
                self.last_action_ts = time.time()
            return

        # Если в радиусе атаки — атакуем
        if best_dist <= attack_dist_px:
            attacked = self._attack_on_screen_throttled(ex, ey, y_offset=10, cooldown=0.25)
            if attacked:
                self.last_action_ts = time.time()
            return

        # Если не в рейндже, но ещё разумно chase-ить — подходим
        if best_dist <= chase_dist_px:
            self._walk_throttled(
                int(ex),
                int(ey) + 10,
                cooldown=0.30,
                tol_px=18.0,
                attack=False,
            )
            return

        # Слишком далеко — не спамим лишние команды
        return

    def _tick_retreat(self, c: Dict[str, Any], s: Senses) -> None:
        now = time.time()

        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        if not self_heroes:
            return

        hx, hy = self._hpbar_center(self_heroes[0])

        if s.enemy_hero_dist_screen is not None or s.enemy_hero_dist_mm is not None:
            self._retreat_enemy_memory_until = now + self.retreat_enemy_memory_sec

        enemy_on_screen = s.enemy_hero_dist_screen is not None
        enemy_on_minimap = s.enemy_hero_dist_mm is not None
        threat_recent = now <= self._retreat_enemy_memory_until

        fountain_uv = self._pick_fountain_point(c)

        # 1) одноразово спрятаться за ally
        if self._retreat_phase is RetreatPhase.GO_BEHIND_ALLY:
            done = False

            if self._should_anchor_behind_ally(s):
                ally_xy = self._nearest_ally_hero_screen_xy(c)
                if ally_xy is not None:
                    enemy_xy = self._nearest_enemy_screen_xy(c) if enemy_on_screen else None
                    tx, ty = self._retreat_point_behind_ally(
                        ally_xy=ally_xy,
                        enemy_xy=enemy_xy,
                        self_xy=(hx, hy),
                    )

                    self._walk_throttled(
                        tx,
                        ty,
                        cooldown=0.15,
                        tol_px=10.0,
                        attack=False,
                    )

                    d2 = (tx - hx) * (tx - hx) + (ty - hy) * (ty - hy)
                    if d2 <= (28.0 * 28.0) or (now - self._retreat_phase_ts) >= self.retreat_anchor_timeout:
                        done = True
                else:
                    done = True
            else:
                done = True

            if done:
                self._retreat_phase = RetreatPhase.BUILD_PATH
                self._retreat_phase_ts = now

            return

        # 2) если враг на экране — уходим screen-walk, миникарту не трогаем
        if enemy_on_screen:
            enemy_xy = self._nearest_enemy_screen_xy(c)
            if enemy_xy is not None:
                ex, ey = enemy_xy
                dx = hx - ex
                dy = hy - ey
                norm = float((dx * dx + dy * dy) ** 0.5)
                if norm > 1e-6:
                    tx = int(round(hx + dx / norm * self.retreat_screen_step_px))
                    ty = int(round(hy + dy / norm * self.retreat_screen_step_px))
                    sent = self._walk_throttled(
                        tx,
                        ty,
                        cooldown=0.15,
                        tol_px=10.0,
                        attack=False,
                    )
                    if not sent and (now - self._last_walk_ts) > 0.22:
                        self.pl.click_on_screen_walk(self.hwnd, tx, ty, attack=False)
                        self._last_walk_ts = now
                        self._last_walk_target = (tx, ty)
                        self.last_action_ts = now

            # после screen retreat хотим потом идти по path, а не напрямую
            self._retreat_phase = RetreatPhase.BUILD_PATH
            return

        # 3) build path
        if self._retreat_phase is RetreatPhase.BUILD_PATH:
            enemy_uv = self._nearest_enemy_uv(c) if (enemy_on_minimap or threat_recent) else None
            self._retreat_path_uv = self._build_retreat_path_uv(
                c,
                enemy_uv=enemy_uv,
                fountain_uv=fountain_uv,
            )
            self._retreat_path_idx = 0
            self._retreat_path_built_ts = now

            if self._retreat_path_uv:
                self._retreat_phase = RetreatPhase.FOLLOW_PATH
            else:
                self._retreat_phase = RetreatPhase.DIRECT_TO_FOUNTAIN

        # 4) follow path
        if self._retreat_phase is RetreatPhase.FOLLOW_PATH:
            # если путь пуст или устарел — перестраиваем, а не бежим сразу в фонтан
            if self._should_rebuild_retreat_path(now):
                self._retreat_phase = RetreatPhase.BUILD_PATH
                return

            active = self._follow_retreat_path(c)

            # если путь ещё активен — продолжаем по нему
            if active:
                return

            # только если путь реально закончился — можно идти напрямую
            self._retreat_phase = RetreatPhase.DIRECT_TO_FOUNTAIN
            self._retreat_phase_ts = now

        # 5) rebuild if needed
        if self._retreat_phase is RetreatPhase.BUILD_PATH:
            return self._tick_retreat(c, s)

        # 6) fallback direct fountain
        self._retreat_phase = RetreatPhase.DIRECT_TO_FOUNTAIN
        sent = self._minimap_click_throttled(
            fountain_uv[0],
            fountain_uv[1],
            cooldown=0.55,
        )
        if not sent and (now - self._farming_last_minimap_click_ts) > 0.75:
            self.pl.click_minimap_pct(self.hwnd, fountain_uv[0], fountain_uv[1], attack=False)
            self._farming_last_minimap_click_ts = now
            self.last_action_ts = now

    def _tick_wait_start(self, c: Dict[str, Any], s: Senses):
        if c.get("t_game", 0) > 110:
            self._wait_start_target = None
            self._set_state(BrainState.IDLE)
            return

        units = c.get("map", {})
        self_units = units.get("self", [])
        if not self_units:
            return

        me = self_units[0]
        try:
            me_x = float(me["x"])
            me_y = float(me["y"])
        except Exception:
            return

        towers = c.get("towers", {}).get("ally", [])
        if not towers:
            return

        t1_list = []
        for t in towers:
            tier = t.get("tier", None)
            if tier is None or tier == 1:
                t1_list.append(t)

        if not t1_list:
            t1_list = towers

        # если цель ещё не выбрана — выбираем один раз ближайшую T1
        if self._wait_start_target is None:
            best = None
            best_d2 = 1e18

            for t in t1_list:
                try:
                    tx = float(t["x"])
                    ty = float(t["y"])
                except Exception:
                    continue

                d2 = self._dist2_uv(me_x, me_y, tx, ty)
                if d2 < best_d2:
                    best_d2 = d2
                    best = (tx, ty)

            if best is None:
                return

            self._wait_start_target = best

            if self.log:
                self.log.info(
                    f"[BRAIN {hex(self.hwnd)}] WAIT_START: selected T1 target "
                    f"({best[0]:.1f}, {best[1]:.1f})"
                )

        tx, ty = self._wait_start_target

        # если уже дошли — просто стоим
        r2 = self._moving_radius * self._moving_radius
        if self._dist2_uv(me_x, me_y, tx, ty) <= r2:
            if self.log:
                self.log.debug(f"[BRAIN {hex(self.hwnd)}] WAIT_START: already near selected T1")
            return

        now = time.time()
        if now - self._wait_start_last_click_ts >= self._wait_start_click_cooldown:
            self.pl.click_minimap_pct(self.hwnd, tx + 1, ty + 1, attack=False)
            self._wait_start_last_click_ts = now
            self.last_action_ts = now

            if self.log:
                self.log.debug(
                    f"[BRAIN {hex(self.hwnd)}] WAIT_START: moving to selected T1 "
                    f"({tx:.1f}, {ty:.1f})"
                )

    def _tick_moving(self, c: Dict[str, Any], s: Senses):
        if self._moving_point is None:
            self._set_state(BrainState.IDLE)
            return

        units = c.get("map", {})
        self_units = units.get("self", [])
        if not self_units:
            return

        me = self_units[0]
        try:
            me_x = float(me["x"])
            me_y = float(me["y"])
        except Exception:
            return

        tx, ty = self._moving_point
        R = self._moving_radius
        R2 = R * R

        d2 = self._dist2_uv(me_x, me_y, tx, ty)
        if d2 <= R2:
            if self.log:
                self.log.debug(
                    f"[BRAIN {hex(self.hwnd)}] MOVING: arrived at target ({tx:.1f},{ty:.1f})"
                )

            self._moving_point = None
            self._set_state(BrainState.IDLE)
            return