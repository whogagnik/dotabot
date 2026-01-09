from __future__ import annotations

import time
from enum import Enum, auto
from dataclasses import dataclass
from typing import Dict, Any, Optional, TYPE_CHECKING, Tuple, List

import numpy as np
from scripts.core.utils import *  # noqa



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
@dataclass
class Senses:
    alive: bool
    t_game: float
    hp_ratio: Optional[float]
    low_hp: bool

    enemy_hero_near: bool
    enemy_hero_dist_screen: Optional[float]
    enemy_hero_dist_mm: Optional[float]

    # --- перенесли из tick_laning сюда ---
    enemy_hero_cnt_screen: int
    avg_enemy_hero_hp_ratio_screen: Optional[float]

    enemy_creep_near: bool
    enemy_creep_dist_screen: Optional[float]

    ally_creep_near: bool
    ally_creep_dist_screen: Optional[float]

    under_ally_tower: bool
    ally_tower_dist_mm: Optional[float]

    near_enemy_tower: bool
    enemy_tower_dist_mm: Optional[float]


class BrainState(Enum):
    IDLE = auto()
    LANING = auto()
    FARMING = auto()
    MOVING = auto()
    FIGHTING = auto()
    DEAD = auto()
    WAIT_START = auto()


class Brain:
    """
    Один Brain на один hwnd.
    Он не знает про окна в целом, только про свой hwnd + данные из Planner.
    """

    def __init__(self, hwnd: int, planner: "Planner", logger=None):
        self.hwnd = hwnd
        self.pl = planner
        self.log = logger
        self.state = BrainState.IDLE

        # время старта (для t_game)
        self.t0 = time.time()

        # можно хранить ещё какие-то внутренние штуки:
        self.last_action_ts = 0.0

        # цель для MOVING
        self._moving_point: Optional[tuple[float, float]] = None  # (x,y) в координатах миникарты 0..100
        self._moving_radius: float = 5.0  # радиус "достаточно близко" (в тех же единицах)
        self.lane_offset_px: float = 80.0  # насколько пикселей отходить от линии по нормали

        self._laning_fig = None
        self._laning_ax = None
        self._laning_last_ts = 0.0  # чтобы не рисовать каждый кадр, а раз в N секунд

        self.min_enemy_dist_px: float = 150.0
        self.max_enemy_dist_px: float = 400.0

        self._lasthit_target_key: Optional[tuple[str, object]] = None
        self._lasthit_target_expire: float = 0.0  # safety timeout

        # --- lasthit / approach tuning ---
        self.lasthit_prepare_hp: float = 0.50  # если крип <= 0.5 -> можно готовиться
        self.lasthit_attack_hp: float = 0.25  # реально добивать при <= 0.25
        self.lasthit_attack_range_px: float = 150.0  # “мы в радиусе удара”
        self.lasthit_max_seek_px: float = 650.0  # дальше не рассматриваем
        self.lasthit_cmd_cooldown: float = 0.18  # чтобы не спамить клики

        self._manual_switch_last_ts: float = 0.0
        self._digit_prev_down: Dict[int, bool] = {d: False for d in range(1, 10)}
        self._manual_switch_cooldown: float = 0.20  # антидребезг

    # --------- публичный метод ---------
    def tick_one(self, snap: "Snapshot"):
        """
        Один тик мозга.
        Дополнительно: цифры 1..N (N = кол-во tick_{state}) переключают текущий state,
        и, соответственно, то какой tick_{state} будет вызываться.
        """
        c = snap.combined

        # 1) senses
        senses = self._gather_senses(c)

        # 2) ручное переключение state цифрами
        states = list(BrainState)  # порядок как в Enum: IDLE, LANING, FARMING, MOVING, FIGHTING, DEAD, WAIT_START
        d = self._poll_digit_press(max_digit=len(states))
        if d is not None:
            new_state = states[d - 1]
            self._set_state(new_state)
            if self.log:
                self.log.info(f"[BRAIN {hex(self.hwnd)}] manual switch: {d} -> {new_state.name}")

        # 3) выполняем тик по текущему state
        self._tick_state(c, senses)

    def _tick_state(self, c: Dict[str, Any], s: Senses):
        """
        Диспетчер: в зависимости от self.state зовёт нужный _tick_*.
        """
        st = self.state

        if st is BrainState.DEAD:
            self._tick_dead(c, s)

        elif st is BrainState.WAIT_START:
            self._tick_wait_start(c, s)

        elif st is BrainState.MOVING:
            self._tick_moving(c, s)

        elif st is BrainState.LANING:
            self._tick_laning(c, s)

        elif st is BrainState.FARMING:
            self._tick_farming(c, s)

        elif st is BrainState.FIGHTING:
            self._tick_fighting(c, s)

        elif st is BrainState.IDLE:
            self._tick_idle(c, s)

        else:
            if self.log:
                self.log.warning(f"[BRAIN {hex(self.hwnd)}] unknown state: {st}")

    def _update_state(self, s: Senses):
        # смерть
        if not s.alive:
            self._set_state(BrainState.DEAD)
            return

        # до 1:50 ждём у T1
        if s.t_game < 110:
            if self.state != BrainState.MOVING:
                self._set_state(BrainState.WAIT_START)
            return

        if s.enemy_creep_near and s.ally_creep_near:
            self._set_state(BrainState.LANING)

        # если рядом враг-герой — дерёмся
        if s.enemy_hero_near and s.hp_ratio and s.hp_ratio > 0.5 and self.state != BrainState.FIGHTING:
            self._set_state(BrainState.FIGHTING)
            return

        # если врагов нет, но есть крипы в лесу — FARMING
        if s.enemy_creep_near and not s.enemy_hero_near:
            self._set_state(BrainState.FARMING)
            return

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

        # простые эвристики рядом/под башней
        enemy_hero_near, hero_dist_scr, hero_dist_mm = self._sense_enemy_hero_near(c)
        enemy_creep_near, enemy_creep_dist_scr = self._sense_enemy_creep_near(c)
        ally_creep_near, ally_creep_dist_scr = self._sense_ally_creep_near(c)
        under_ally_tower, ally_tower_dist_mm = self._sense_under_ally_tower(c)
        near_enemy_tower, enemy_tower_dist_mm = self._sense_near_enemy_tower(c)

        # --- ПЕРЕНЕСЕНО ИЗ tick_laning ---
        enemy_cnt_screen = self._count_enemy_heroes_near_screen(c)
        avg_enemy_hp_screen = self._avg_enemy_hero_hp_ratio_screen(c)

        return Senses(
            alive=alive,
            t_game=t_game,
            hp_ratio=hp_ratio,
            low_hp=low_hp,

            enemy_hero_near=enemy_hero_near,
            enemy_hero_dist_screen=hero_dist_scr,
            enemy_hero_dist_mm=hero_dist_mm,

            enemy_hero_cnt_screen=enemy_cnt_screen,
            avg_enemy_hero_hp_ratio_screen=avg_enemy_hp_screen,

            enemy_creep_near=enemy_creep_near,
            enemy_creep_dist_screen=enemy_creep_dist_scr,

            ally_creep_near=ally_creep_near,
            ally_creep_dist_screen=ally_creep_dist_scr,

            under_ally_tower=under_ally_tower,
            ally_tower_dist_mm=ally_tower_dist_mm,

            near_enemy_tower=near_enemy_tower,
            enemy_tower_dist_mm=enemy_tower_dist_mm,
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

        Возвращает:
          mid  — точка на разделяющей прямой (середина между центрами масс)
          d    — направление прямой (перпендикуляр к нормали)
          n    — нормаль из A в E (юнит-вектор)
          c_a  — центр масс A
          c_e  — центр масс E
        """
        c_a = points_a.mean(axis=0)
        c_e = points_e.mean(axis=0)

        n = c_e - c_a
        if np.allclose(n, 0):
            n = np.array([0.0, 1.0], dtype=np.float32)
        n = n / np.linalg.norm(n)

        mid = 0.5 * (c_a + c_e)
        d = np.array([-n[1], n[0]], dtype=np.float32)

        return mid.astype(np.float32), d, n, c_a.astype(np.float32), c_e.astype(np.float32)

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

    # --------- служебные ---------
    # --- НОВОЕ: low-level чтение цифровых клавиш (Windows) ---
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

    @debug_log_result
    def _compute_laning_point(self, c: Dict[str, Any], min_dist_to_enemy: float) -> Optional[tuple[int, int]]:
        """
        Возвращает экранную точку (КЛИЕНТСКИЕ координаты),
        куда хотим кликать при лайнинге.
        """
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_list = heroes.get("self", [])
        ally_heroes = heroes.get("ally", [])
        enemy_heroes = heroes.get("enemy", [])
        ally_creeps = creeps.get("ally", [])
        enemy_creeps = creeps.get("enemy", [])

        if not self_list or len(ally_creeps) < 2 or len(enemy_creeps) < 2:
            return None

        ally_creep_pts = np.array([self._hpbar_center(cb) for cb in ally_creeps], dtype=np.float32)
        enemy_creep_pts = np.array([self._hpbar_center(cb) for cb in enemy_creeps], dtype=np.float32)

        if ally_creep_pts.shape[0] < 2 or enemy_creep_pts.shape[0] < 2:
            return None

        mid_all, d_all, n_all, cA_all, cE_all = self._compromise_line(ally_creep_pts, enemy_creep_pts)

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

        proj_allies = ally_creep_pts @ n_all
        proj_enemies = enemy_creep_pts @ n_all

        front_allies = float(np.max(proj_allies))
        s_behind_allies = front_allies - float(self.lane_offset_px)

        s_far_from_enemy = None
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

    def _lm_root(self, c: Dict[str, Any]) -> Dict[str, Any]:
        """
        Возвращает словарь landmarks['data'] если есть, иначе весь landmarks.
        """
        lm = c.get("landmarks") or {}
        if isinstance(lm, dict) and "data" in lm and isinstance(lm["data"], dict):
            return lm["data"]
        return lm if isinstance(lm, dict) else {}

    def _get_side(self) -> str:
        """
        Side хранится в Planner (как в твоём planner.py).
        """
        side = getattr(self.pl, "side", "radiant")
        return str(side).lower().strip()

    @debug_log_result
    def goto_nearest_camp(
            self,
            c: Dict[str, Any],
            *,
            camp_kind: str = "малый",
            from_pos: Optional[Tuple[float, float]] = None,
            attack: bool = True,
    ) -> None:
        """
        Идём на ближайший кемп заданного вида.
        Использует данные из текущего combined (c), НЕ из Planner напрямую.
        """

        # 1) позиция героя
        cur = self._get_self_uv(c)
        if cur is None:
            cur = from_pos if from_pos is not None else (50.0, 50.0)

        # 2) нормализация camp_kind -> key
        ck = (camp_kind or "").strip().lower()
        kind_map = {
            "малый": "small", "small": "small",
            "средний": "medium", "medium": "medium",
            "большой": "large", "large": "large",
        }
        ck2 = kind_map.get(ck, ck)  # если уже small/medium/large
        key = f"camp_{ck2}"  # camp_small / camp_medium / camp_large

        root = self._lm_root(c)
        arr = root.get(key)
        if not arr:
            if self.log:
                self.log.debug(f"[CAMP] no camps for key={key}")
            return

        # формат: [ [ {x,y}, ... ] ] или просто [ {x,y}, ...]
        points_raw = arr[0] if isinstance(arr, list) and len(arr) > 0 and isinstance(arr[0], list) else arr

        candidates: List[Tuple[float, float]] = []
        if isinstance(points_raw, list):
            for pt in points_raw:
                try:
                    candidates.append((float(pt["x"]), float(pt["y"])))
                except Exception:
                    continue

        if not candidates:
            if self.log:
                self.log.debug(f"[CAMP] empty camps for key={key}")
            return

        # 3) ближайший по евклиду
        best = min(candidates, key=lambda p: _euclid2(cur, p))
        bx, by = best

        # 4) клик по миникарте
        self.pl.click_minimap_pct(self.hwnd, bx + 1, by + 1, attack=attack)

        if self.log:
            self.log.debug(
                f"[CAMP] hwnd={hex(self.hwnd)} kind={ck2} -> ({bx:.1f},{by:.1f}) "
                f"from=({cur[0]:.1f},{cur[1]:.1f}) attack={attack}"
            )

    @debug_log_result
    def goto_nearest_lane(
            self,
            c: Dict[str, Any],
            *,
            from_pos: Optional[Tuple[float, float]] = None,
            attack: bool = False,
    ) -> None:
        """
        Идём на ближайшую линию (top/mid/bot), НЕ указывая имя.
        """

        cur = self._get_self_uv(c)
        if cur is None:
            cur = from_pos if from_pos is not None else (50.0, 50.0)

        root = self._lm_root(c)
        lane_keys = ["lane_top", "lane_mid", "lane_bot"]

        candidates: List[Tuple[str, List[Dict[str, float]]]] = []
        for k in lane_keys:
            arr = root.get(k, [])
            # формат: lane_*: [[{x,y}, ...]]
            if isinstance(arr, list) and arr and isinstance(arr[0], list) and arr[0]:
                candidates.append((k, arr[0]))

        if not candidates:
            if self.log:
                self.log.debug("[LANE] no lane polylines in landmarks")
            return

        best_lane = None
        best_q = None
        best_d2 = 1e18

        for lname, poly in candidates:
            qx, qy, d2 = _closest_point_on_polyline(poly, cur)
            if d2 < best_d2:
                best_d2 = d2
                best_q = (qx, qy)
                best_lane = lname

        if best_q is None:
            return

        self.pl.click_minimap_pct(self.hwnd, best_q[0], best_q[1], attack=attack)

        if self.log:
            self.log.debug(f"[LANE] hwnd={hex(self.hwnd)} -> {best_lane} @ ({best_q[0]:.1f},{best_q[1]:.1f})")

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
        root = self._lm_root(c)
        side = self._get_side()

        for key in (f"fountain_{side}", f"ancient_{side}"):
            val = root.get(key)
            pt = self._extract_point_xy(val)
            if pt is not None:
                return pt

        return 50.0, 50.0

    @debug_log_result
    def goto_fountain(self, c: Dict[str, Any]) -> None:
        x, y = self._pick_fountain_point(c)
        self.pl.click_minimap_pct(self.hwnd, x, y, attack=False)

    @debug_log_result
    def goto_nearest_tower(
            self,
            c: Dict[str, Any],
            *,
            ally_or_enemy: str = "enemy",
            only_alive: bool = True,
            attack: bool = False,
    ) -> None:
        """
        Находим ближайшую (живую) башню ally_or_enemy к текущей позиции self и кликаем по ней.
        """
        ally_or_enemy = (ally_or_enemy or "enemy").strip().lower()
        if ally_or_enemy not in ("ally", "enemy"):
            ally_or_enemy = "enemy"

        tws = c.get("towers", {}).get(ally_or_enemy, [])
        if not tws:
            return

        cur = self._get_self_uv(c) or (50.0, 50.0)

        best = None
        best_d2 = 1e18
        for t in tws:
            if only_alive and not bool(t.get("alive", True)):
                continue
            try:
                px, py = float(t["x"]), float(t["y"])
            except Exception:
                continue
            d2 = _euclid2(cur, (px, py))
            if d2 < best_d2:
                best_d2 = d2
                best = (px, py)

        if best is None:
            return

        self.pl.click_minimap_pct(self.hwnd, best[0] + 1, best[1] + 1, attack=attack)

    @debug_log_result
    def _select_lasthit_creep(
        self,
        c: Dict[str, Any],
        *,
        hp_threshold: float = 0.25,
        max_dist_px: float = 550.0,
    ):
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_heroes = heroes.get("self", [])
        enemy_creeps = creeps.get("enemy", [])

        if not self_heroes or not enemy_creeps:
            return None

        hx, hy = self._hpbar_center(self_heroes[0])

        best_cb = None
        best_cx = best_cy = None
        best_hp = None

        max_d2 = max_dist_px * max_dist_px

        for cb in enemy_creeps:
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
        radius_uv: float = 5.0,
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

    def _set_state(self, new_state: BrainState):
        if new_state is self.state:
            return
        if self.log:
            self.log.debug(f"[BRAIN {hex(self.hwnd)}] {self.state.name} -> {new_state.name}")
        self.state = new_state
        self.last_action_ts = 0.0

    # --------- обработчики состояний (пока примитивные заглушки) ---------
    def _tick_dead(self, c: Dict[str, Any], s: Senses):
        pass

    def _tick_idle(self, c: Dict[str, Any], s: Senses):
        pass

    def _tick_laning(self, c: Dict[str, Any], s: Senses):
        """
        Лайнинг:
          1) если есть активная цель для ластхита и она ещё жива — ждём (не даём новых команд).
          2) иначе ищем крипа с низким HP:
              - если крип < 0.5: решаем, можно ли сблизиться
                  - если можно: подходим (walk) к крипу если далеко
                  - если уже близко: атакуем
              - если нельзя сближаться: ничего не делаем по этому крипу
          3) если подходящих крипов нет — занимаем позицию по линии.
        """
        now = time.time()

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

        # --- 2) ищем крипа на ластхит ---
        lasthit_candidate = self._select_lasthit_creep(c, hp_threshold=0.5)
        if lasthit_candidate is not None:
            cb, cx, cy = lasthit_candidate
            creep_hp = float(getattr(cb, "hp_ratio", 1.0))

            # расстояние до крипа
            heroes = c.get("heroes", {})
            self_heroes = heroes.get("self", [])
            if self_heroes:
                hx, hy = self._hpbar_center(self_heroes[0])
                dist = float(((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5)
            else:
                dist = 9999.0

            # --- ТЕПЕРЬ БЕРЁМ ИЗ Senses ---
            enemy_cnt = s.enemy_hero_cnt_screen
            avg_enemy_hp = s.avg_enemy_hero_hp_ratio_screen

            allow = self._should_approach_for_lasthit(
                s,
                creep_hp=creep_hp,
                dist_to_creep_px=dist,
                enemy_hero_cnt_near=enemy_cnt,
                avg_enemy_hp=avg_enemy_hp,
            )

            if not allow:
                if self.log:
                    self.log.debug(
                        f"[BRAIN] lasthit: SKIP (risk) creep_hp={creep_hp:.2f}, "
                        f"dist={dist:.0f}px, enemy_cnt={enemy_cnt}, avg_enemy_hp={avg_enemy_hp}"
                    )
                return

            # если можно — решаем: подойти или атаковать
            attack_dist_px = 260.0

            if dist > attack_dist_px:
                if self.log:
                    self.log.debug(
                        f"[BRAIN] lasthit: APPROACH creep at ({cx:.0f},{cy:.0f}) "
                        f"dist={dist:.0f}px creep_hp={creep_hp:.2f}"
                    )
                self.pl.click_on_screen_walk(self.hwnd, int(cx), int(cy) + 10, attack=False)
                self.last_action_ts = now
                return

            if self.log:
                self.log.debug(
                    f"[BRAIN] lasthit: ATTACK creep at ({cx:.0f},{cy:.0f}), "
                    f"hp_ratio={creep_hp:.2f}"
                )

            self.pl.click_on_screen(self.hwnd, int(cx), int(cy) + 10, attack=True)

            key = self._make_lasthit_key(cb, cx, cy)
            self._lasthit_target_key = key
            self._lasthit_target_expire = now + 1.5
            self.last_action_ts = now
            return

        # --- 3) обычный лайнинг по линии ---
        dist_to_enemy = self._compute_dist_to_enemy_hero(s)
        target = self._compute_laning_point(c, min_dist_to_enemy=dist_to_enemy)
        if target is None:
            return

        tx, ty = target

        if self.log:
            self.log.debug(f"[BRAIN] laning: move to ({tx},{ty})")

        self.pl.click_on_screen_walk(self.hwnd, tx, ty, attack=False)
        self.last_action_ts = now

    def _tick_farming(self, c: Dict[str, Any], s: Senses):
        pass

    def _tick_fighting(self, c: Dict[str, Any], s: Senses):
        pass

    def _tick_wait_start(self, c: Dict[str, Any], s: Senses):
        if s.t_game > 110:
            self._set_state(BrainState.IDLE)
            return

        if c.get("t_game") > 110:
            self._set_state(BrainState.IDLE)

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

        R2 = self._moving_radius * self._moving_radius
        close_any = False

        for t in t1_list:
            try:
                tx = float(t["x"])
                ty = float(t["y"])
            except Exception:
                continue
            if self._dist2_uv(me_x, me_y, tx, ty) <= R2:
                close_any = True
                break

        if close_any:
            if self.log:
                self.log.debug(f"[BRAIN {hex(self.hwnd)}] WAIT_START: already near T1")
            return

        import random

        t_target = random.choice(t1_list)
        try:
            tx = float(t_target["x"])
            ty = float(t_target["y"])
        except Exception:
            return

        self._moving_point = (tx, ty)
        self._set_state(BrainState.MOVING)

        if self.log:
            self.log.debug(
                f"[BRAIN {hex(self.hwnd)}] WAIT_START: too far from T1, "
                f"moving to ({tx:.1f},{ty:.1f})"
            )

        self.pl.click_minimap_pct(self.hwnd, tx + 1, ty + 1, attack=False)
        self.last_action_ts = time.time()

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
