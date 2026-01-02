from __future__ import annotations

import time
from enum import Enum, auto
from dataclasses import dataclass
from typing import Dict, Any, Optional, TYPE_CHECKING, Tuple, List
import numpy as np
import win32gui
import matplotlib.pyplot as plt

import logging
from functools import wraps

if TYPE_CHECKING:
    # эти импорты используются только для type hints, на рантайме не выполняются
    from planner import Planner, Snapshot

def get_client_rect(hwnd: int) -> Tuple[int,int,int,int]:
    """Клиентская область в экранных координатах."""
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)  # (0,0,w,h)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        return sx, sy, max(1, r - l), max(1, b - t)
    except Exception:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return L, T, max(1, R - L), max(1, B - T)




def debug_log_result(fn):
    """
    Декоратор для методов Brain:
    логирует имя функции, аргументы и результат через self.log.debug,
    если логгер есть и включён DEBUG.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # пытаемся вытащить self.log, если это метод
        logger = None
        if args and hasattr(args[0], "log"):
            logger = getattr(args[0], "log", None)

        # если логгера нет или DEBUG не включён — просто вызываем функцию
        if not logger or not logger.isEnabledFor(logging.DEBUG):
            return fn(*args, **kwargs)

        # аккуратно формируем строку с аргументами (без гигантских дампов)
        def _short_repr(x, max_len=120):
            r = repr(x)
            if len(r) > max_len:
                return r[:max_len] + "..."
            return r

        arg_strs = [_short_repr(a) for a in args[1:]]  # args[0] == self
        kw_strs  = [f"{k}={_short_repr(v)}" for k, v in kwargs.items()]
        joined   = ", ".join(arg_strs + kw_strs)

        logger.debug(f"[BRAIN] {fn.__name__}(...): args={joined}")
        res = fn(*args, **kwargs)
        logger.debug(f"[BRAIN] {fn.__name__}(...): result -> { _short_repr(res) }")
        return res

    return wrapper


@dataclass
class Senses:
    alive: bool
    t_game: float
    hp_ratio: Optional[float]
    low_hp: bool

    enemy_hero_near: bool
    enemy_hero_dist_screen: Optional[float]
    enemy_hero_dist_mm: Optional[float]

    enemy_creep_near: bool
    enemy_creep_dist_screen: Optional[float]

    ally_creep_near: bool
    ally_creep_dist_screen: Optional[float]

    under_ally_tower: bool
    ally_tower_dist_mm: Optional[float]

    near_enemy_tower: bool
    enemy_tower_dist_mm: Optional[float]



class BrainState(Enum):
    IDLE    = auto()
    LANING  = auto()
    FARMING = auto()
    MOVING  = auto()
    FIGHTING= auto()
    DEAD    = auto()
    WAIT_START = auto()

class Brain:
    """
    Один Brain на один hwnd.
    Он не знает про окна в целом, только про свой hwnd + данные из Planner.
    """
    def __init__(self, hwnd: int, planner: "Planner", logger=None):
        self.hwnd   = hwnd
        self.pl     = planner
        self.log    = logger
        self.state  = BrainState.IDLE

        # время старта (для t_game)
        self.t0     = time.time()

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
    def _compromise_line(points_a: np.ndarray,
                         points_e: np.ndarray
                         ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Аналог твоей compromise_line:
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
        Если хочешь буквально (min + max*hp_ratio), просто замени формулу внутри.
        """
        if s.hp_ratio is None:
            return self.min_enemy_dist_px

        # clamp [0..1] на всякий случай
        r = 1 - max(0.0, min(1.0, float(s.hp_ratio)))

        # вариант с интерполяцией между min и max:
        dist =  self.min_enemy_dist_px + (self.max_enemy_dist_px - self.min_enemy_dist_px) * r

        # если хочешь ровно (min + max * hp_ratio), то:
        # dist = self.min_enemy_dist_px + self.max_enemy_dist_px * r

        return dist

    # --------- публичный метод ---------
    def update(self, snap: Snapshot):
        c = snap.combined

        # 1) senses (мелкие функции, которые читают c)
        senses = self._gather_senses(c)

        # 2) смена состояния (на основе senses)
        #self._update_state(senses)

        # 3) действие в текущем состоянии
        self._tick_laning(c,senses)
        #self.debug_plot_laning_live(c)
        #self._tick_state(c, senses)

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
        if s.enemy_hero_near and s.hp_ratio > 0.5 and self.state != BrainState.FIGHTING:
            self._set_state(BrainState.FIGHTING)
            return

        # если врагов нет, но есть крипы в лесу — FARMING
        if s.enemy_creep_near and not s.enemy_hero_near:
            self._set_state(BrainState.FARMING)
            return


        #self._set_state(BrainState.LANING)

    def _gather_senses(self, c: Dict[str, Any]) -> Senses:
        alive = bool(c.get("alive", True))
        t_game = float(c.get("t_game", 0.0))

        hp_ratio_from_hud = c.get("hp_ratio")
        hp_ratio_from_screen = c.get('heroes',{}).get('self',[])

        if len(hp_ratio_from_screen) == 0:
            hp_ratio_from_screen = None

        hp_ratio = None
        if hp_ratio_from_hud is not None and hp_ratio_from_screen is not None:
            hp_pair_from_screen = hp_ratio_from_screen[0].hp_ratio
            hp_ratio = max(hp_pair_from_screen,hp_ratio_from_hud)
        elif hp_ratio_from_hud != None:
            hp_ratio = hp_ratio_from_hud
        elif hp_ratio_from_screen != None:
            hp_ratio = hp_ratio_from_screen[0].hp_ratio

        low_hp = hp_ratio is not None and hp_ratio < 0.3

        # простые эвристики рядом/под башней
        enemy_hero_near, hero_dist_scr, hero_dist_mm = self._sense_enemy_hero_near(c)
        enemy_creep_near, enemy_creep_dist_scr = self._sense_enemy_creep_near(c)
        ally_creep_near, ally_creep_dist_scr = self._sense_ally_creep_near(c)
        under_ally_tower, ally_tower_dist_mm = self._sense_under_ally_tower(c)
        near_enemy_tower, enemy_tower_dist_mm = self._sense_near_enemy_tower(c)

        return Senses(
            alive=alive,
            t_game=t_game,
            hp_ratio=hp_ratio,
            low_hp=low_hp,

            enemy_hero_near=enemy_hero_near,
            enemy_hero_dist_screen=hero_dist_scr,
            enemy_hero_dist_mm=hero_dist_mm,

            enemy_creep_near=enemy_creep_near,
            enemy_creep_dist_screen=enemy_creep_dist_scr,

            ally_creep_near=ally_creep_near,
            ally_creep_dist_screen=ally_creep_dist_scr,

            under_ally_tower=under_ally_tower,
            ally_tower_dist_mm=ally_tower_dist_mm,

            near_enemy_tower=near_enemy_tower,
            enemy_tower_dist_mm=enemy_tower_dist_mm,
        )

    # --------- служебные ---------
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
    def _compute_laning_point(self, c: Dict[str, Any],
                              min_dist_to_enemy: float) -> Optional[tuple[int, int]]:
        """
        Возвращает экранную точку (КЛИЕНТСКИЕ координаты),
        куда хотим кликать при лайнинге.

        min_dist_to_enemy — желаемое минимальное расстояние до вражеской пачки
        вдоль нормали (в пикселях), зависит от HP.
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

        ally_creep_pts = np.array(
            [self._hpbar_center(cb) for cb in ally_creeps],
            dtype=np.float32
        )
        enemy_creep_pts = np.array(
            [self._hpbar_center(cb) for cb in enemy_creeps],
            dtype=np.float32
        )

        if ally_creep_pts.shape[0] < 2 or enemy_creep_pts.shape[0] < 2:
            return None

        # 1) линия по крипам
        mid_all, d_all, n_all, cA_all, cE_all = self._compromise_line(
            ally_creep_pts,
            enemy_creep_pts
        )

        # 2) линия по героям (лидеры)
        leaders_ally_boxes = [self_list[0]] + list(ally_heroes)
        leaders_enemy_boxes = list(enemy_heroes)

        if not leaders_enemy_boxes:
            mid_lead, d_lead, n_lead = mid_all, d_all, n_all
            cA_lead, cE_lead = cA_all, cE_all
        else:
            ally_leader_pts = np.array(
                [self._hpbar_center(b) for b in leaders_ally_boxes],
                dtype=np.float32
            )
            enemy_leader_pts = np.array(
                [self._hpbar_center(b) for b in leaders_enemy_boxes],
                dtype=np.float32
            )
            mid_lead, d_lead, n_lead, cA_lead, cE_lead = self._compromise_line(
                ally_leader_pts,
                enemy_leader_pts
            )

        # 3) третья линия
        mix_dir = d_all + d_lead
        if np.allclose(mix_dir, 0):
            mix_dir = d_all.copy()

        norm_mix = float(np.linalg.norm(mix_dir))
        if norm_mix < 1e-6:
            return None
        mix_dir = mix_dir / norm_mix

        mid_third = 0.5 * (mid_all + mid_lead)

        # 4) проекция центра нашей пачки на третью линию
        vec_to_A = cA_all - mid_third
        t_proj = float(np.dot(vec_to_A, mix_dir))
        base_point = mid_third + mix_dir * t_proj

        # 5) считаем позицию по нормали с учётом:
        #    - "за своими крипами" (back_dist)
        #    - не заходить ближе min_dist_to_enemy к врагам вдоль нормали

        proj_allies = ally_creep_pts @ n_all  # скаляры вдоль нормали
        proj_enemies = enemy_creep_pts @ n_all

        front_allies = float(np.max(proj_allies))  # фронт наших
        s_behind_allies = front_allies - float(self.lane_offset_px)

        s_far_from_enemy = None
        if proj_enemies.size > 0:
            front_enemy = float(np.min(proj_enemies))  # ближайший враг по нормали
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
        """
        Делаем "ключ" по которому будем узнавать крипа на следующих тиках.
        Сначала пытаемся найти стабильный id/uid, если нет — используем позицию центра.
        """
        for attr in ("id", "uid", "track_id", "ent_id"):
            if hasattr(cb, attr):
                return ("id", getattr(cb, attr))
        # fallback: квантованная позиция
        return ("pos", (int(round(cx)), int(round(cy))))

    @debug_log_result
    def _lasthit_target_still_present(self, c: Dict[str, Any]) -> bool:
        """
        True, если ранее выбранный крип всё ещё присутствует в creeps['enemy'].
        Если таймаут истёк — сбрасываем цель.
        """
        if self._lasthit_target_key is None:
            return False

        # safety-таймаут, чтобы не залипнуть навсегда
        if time.time() > self._lasthit_target_expire:
            self._lasthit_target_key = None
            return False

        kind, value = self._lasthit_target_key
        enemy_creeps = c.get("creeps", {}).get("enemy", [])
        if not enemy_creeps:
            # вообще нет крипов — считаем, что цель умерла
            self._lasthit_target_key = None
            return False

        if kind == "id":
            for cb in enemy_creeps:
                for attr in ("id", "uid", "track_id", "ent_id"):
                    if hasattr(cb, attr) and getattr(cb, attr) == value:
                        return True
            # id больше нет
            self._lasthit_target_key = None
            return False

        if kind == "pos":
            tx, ty = value
            max_d2 = 20.0 * 20.0  # радиус для "та же точка" ~20px
            for cb in enemy_creeps:
                cx, cy = self._hpbar_center(cb)
                d2 = (cx - tx) * (cx - tx) + (cy - ty) * (cy - ty)
                if d2 <= max_d2:
                    return True
            self._lasthit_target_key = None
            return False

        # неизвестный формат ключа — сбрасываем
        self._lasthit_target_key = None
        return False
    @debug_log_result
    def _select_lasthit_creep(
        self,
        c: Dict[str, Any],
        *,
        hp_threshold: float = 0.25,
        max_dist_px: float = 550.0,
    ):
        """
        Находит подходящего вражеского крипа для ластхита.

        Возвращает кортеж (cb, cx, cy) или None.
        cb — объект крипа из creeps['enemy'].
        cx, cy — его центр HP-бара (клиентские координаты).
        """
        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_heroes = heroes.get("self", [])
        enemy_creeps = creeps.get("enemy", [])

        if not self_heroes or not enemy_creeps:
            return None

        hero_center = self._hpbar_center(self_heroes[0])
        hx, hy = hero_center

        best_cb = None
        best_cx = best_cy = None
        best_hp = None

        max_d2 = max_dist_px * max_dist_px

        for cb in enemy_creeps:
            # предполагаем, что у бокса есть hp_ratio; если нет — пропускаем
            hp_ratio = getattr(cb, "hp_ratio", None)
            print(hp_ratio)
            if hp_ratio is None:
                continue

            # выбираем только крипов с низким HP
            if hp_ratio > hp_threshold:
                continue

            cx, cy = self._hpbar_center(cb)
            d2 = (cx - hx) * (cx - hx) + (cy - hy) * (cy - hy)
            if d2 > max_d2:
                continue

            # берём того, у кого HP минимальное
            if best_hp is None or hp_ratio < best_hp:
                best_hp = hp_ratio
                best_cb = cb
                best_cx, best_cy = cx, cy

        if best_cb is None:
            return None

        return best_cb, best_cx, best_cy

    def debug_plot_laning_live(self,
                               c: Dict[str, Any],
                               back_dist: float = 80.0,
                               min_dist_to_enemy: float = 150.0,
                               min_dt: float = 0.25):
        """
        Лайв-визуализация геометрии лайнинга через matplotlib.
        Вызывай её каждый тик, но внутри она сама ограничивает частоту перерисовки.
        """

        now = time.time()
        if now - self._laning_last_ts < min_dt:
            return
        self._laning_last_ts = now

        heroes = c.get("heroes", {})
        creeps = c.get("creeps", {})

        self_list     = heroes.get("self", [])
        ally_heroes   = heroes.get("ally", [])
        enemy_heroes  = heroes.get("enemy", [])
        ally_creeps   = creeps.get("ally", [])
        enemy_creeps  = creeps.get("enemy", [])

        if not self_list or len(ally_creeps) < 2 or len(enemy_creeps) < 2:
            return

        ally_creep_pts = np.array(
            [self._hpbar_center(cb) for cb in ally_creeps],
            dtype=np.float32
        )
        enemy_creep_pts = np.array(
            [self._hpbar_center(cb) for cb in enemy_creeps],
            dtype=np.float32
        )

        if ally_creep_pts.shape[0] < 2 or enemy_creep_pts.shape[0] < 2:
            return

        # ---------- линия по крипам ----------
        mid_all, d_all, n_all, cA_all, cE_all = self._compromise_line(
            ally_creep_pts,
            enemy_creep_pts
        )

        # ---------- линия по героям (лидеры) ----------
        leaders_ally_boxes  = [self_list[0]] + list(ally_heroes)
        leaders_enemy_boxes = list(enemy_heroes)

        if leaders_enemy_boxes:
            ally_leader_pts = np.array(
                [self._hpbar_center(b) for b in leaders_ally_boxes],
                dtype=np.float32
            )
            enemy_leader_pts = np.array(
                [self._hpbar_center(b) for b in leaders_enemy_boxes],
                dtype=np.float32
            )
            mid_lead, d_lead, n_lead, cA_lead, cE_lead = self._compromise_line(
                ally_leader_pts,
                enemy_leader_pts
            )
        else:
            mid_lead, d_lead, n_lead = mid_all, d_all, n_all
            cA_lead, cE_lead = cA_all, cE_all

        # ---------- третья линия ----------
        mix_dir = d_all + d_lead
        if np.allclose(mix_dir, 0):
            mix_dir = d_all.copy()
        mix_dir = mix_dir / np.linalg.norm(mix_dir)

        mid_third = 0.5 * (mid_all + mid_lead)

        # проекция центра масс нашей пачки на третью линию
        vec_to_A = cA_all - mid_third
        t_proj = float(np.dot(vec_to_A, mix_dir))
        base_point = mid_third + mix_dir * t_proj

        # ---------- шаг по нормали: за крипами + min_dist до врагов ----------
        proj_allies  = ally_creep_pts  @ n_all  # скаляры вдоль нормали
        proj_enemies = enemy_creep_pts @ n_all

        front_allies = float(np.max(proj_allies))      # "фронт" наших крипов вдоль нормали
        s_behind_allies = front_allies - float(back_dist)

        s_far_from_enemy = None
        if proj_enemies.size > 0:
            front_enemy = float(np.min(proj_enemies))  # ближайший враг по нормали
            s_far_from_enemy = front_enemy - float(min_dist_to_enemy)

        s_base = float(np.dot(base_point, n_all))
        s_target = s_behind_allies
        if s_far_from_enemy is not None:
            s_target = min(s_target, s_far_from_enemy)

        delta_s = s_target - s_base
        target = base_point + n_all * delta_s

        # ---------- данные для рисования линий ----------
        t_line = np.linspace(-400, 400, 2)
        line_all   = mid_all[None, :]   + t_line[:, None] * d_all[None, :]
        line_lead  = mid_lead[None, :]  + t_line[:, None] * d_lead[None, :]
        line_third = mid_third[None, :] + t_line[:, None] * mix_dir[None, :]

        # ---------- инициализация окна matplotlib один раз ----------
        if self._laning_fig is None or self._laning_ax is None:
            plt.ion()
            self._laning_fig, self._laning_ax = plt.subplots(figsize=(6, 6))

        ax = self._laning_ax
        ax.clear()

        # крипы
        ax.scatter(ally_creep_pts[:, 0],  ally_creep_pts[:, 1],
                   marker="o", color="green", label="Ally creeps")
        ax.scatter(enemy_creep_pts[:, 0], enemy_creep_pts[:, 1],
                   marker="o", color="red", label="Enemy creeps")

        # герои
        ally_hero_pts = np.array(
            [self._hpbar_center(b) for b in leaders_ally_boxes],
            dtype=np.float32
        )
        if leaders_enemy_boxes:
            enemy_hero_pts = np.array(
                [self._hpbar_center(b) for b in leaders_enemy_boxes],
                dtype=np.float32
            )
        else:
            enemy_hero_pts = None

        ax.scatter(ally_hero_pts[:, 0], ally_hero_pts[:, 1],
                   marker="s", s=60, color="cyan", label="Ally heroes")
        if enemy_hero_pts is not None and enemy_hero_pts.size > 0:
            ax.scatter(enemy_hero_pts[:, 0], enemy_hero_pts[:, 1],
                       marker="D", s=60, color="magenta", label="Enemy heroes")

        # центры масс
        ax.scatter(*cA_all,  marker="x", s=80, color="green")
        ax.scatter(*cE_all,  marker="x", s=80, color="red")
        ax.scatter(*cA_lead, marker="+", s=80, color="cyan")
        ax.scatter(*cE_lead, marker="+", s=80, color="magenta")

        # линии
        ax.plot(line_all[:, 0],   line_all[:, 1],
                linestyle="--", linewidth=1.5, label="Creeps line")
        ax.plot(line_lead[:, 0],  line_lead[:, 1],
                linestyle=":", linewidth=1.5, label="Heroes line")
        ax.plot(line_third[:, 0], line_third[:, 1],
                linestyle="-.", linewidth=2.0, label="Third line")

        # целевая точка
        ax.scatter(target[0], target[1],
                   marker="*", s=120, color="yellow", label="Target")

        # нормаль для наглядности
        n_len = 120.0
        n_from = base_point - n_all * n_len * 0.5
        n_to   = base_point + n_all * n_len * 0.5
        ax.plot([n_from[0], n_to[0]], [n_from[1], n_to[1]],
                color="orange", linewidth=1.0, label="Normal n_all")

        ax.set_aspect("equal", "box")
        ax.set_xlabel("screen X (px)")
        ax.set_ylabel("screen Y (px)")
        ax.set_title("Laning geometry (live)")
        ax.legend(loc="best")
        ax.grid(True)

        # обновляем окно (без блокировок)
        self._laning_fig.canvas.draw()
        self._laning_fig.canvas.flush_events()
        # можно ещё так:
        # plt.pause(0.001)

    def _sense_under_ally_tower(
        self,
        c: Dict[str, Any],
        *,
        radius_uv: float = 5.0,
        only_alive: bool = True,
    ) -> tuple[bool, Optional[float]]:
        """
        True, если self находится близко к любой своей башне
        в координатах миникарты (0..100).

        radius_uv — радиус в тех же единицах (0..100 по оси).
        Возвращает: (under_ally_tower, dist_mm_min)
        """

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

        # фильтр по alive, если надо
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
            d = d2 ** 0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        under = dist_min <= radius_uv
        return under, dist_min
    def _sense_ally_creep_near(
        self,
        c: Dict[str, Any],


    ) -> tuple[bool, Optional[float]]:
        """
        True, если СОЮЗНЫЙ крип близко к нашему герою на ЭКРАНЕ.

        screen_radius_px — порог по центрам HP-баров (в пикселях).
        Возвращает: (ally_creep_near, dist_screen_min)
        """
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
            d = d2 ** 0.5
            if dist_min is None or d < dist_min:
                dist_min = d

        if dist_min is None:
            return False, None

        ally_creep_near = dist_min is not None
        return ally_creep_near, dist_min


    def _sense_enemy_creep_near(
            self,
            c: Dict[str, Any]
    ) -> tuple[bool, Optional[float]]:
        """
        True, если вражеский крип близко к нашему герою на ЭКРАНЕ.

        screen_radius_px — порог по центрам HP-баров (в пикселях).
        Возвращает: (enemy_creep_near, dist_screen_min)
        """

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
            d = d2 ** 0.5
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
        """
        Возвращает:
          (enemy_hero_near, dist_screen, dist_mm)

        enemy_hero_near = True, если:
          - либо на ЭКРАНЕ есть вражеский герой достаточно близко к нашему HP-бару,
          - либо на МИНИКАРТЕ есть вражеский юнит достаточно близко к self.

        dist_screen: минимальная дистанция по центрам HP-баров (в пикселях),
                     если своего героя и хотя бы одного врага удалось найти.
        dist_mm:     минимальная дистанция в координатах 0..100 по миникарте,
                     если self и enemy найдены в combined["map"].
        """

        # ----------------- 1) по HP-барам на экране -----------------
        heroes = c.get("heroes", {})
        self_heroes = heroes.get("self", [])
        enemy_heroes = heroes.get("enemy", [])

        dist_screen_min: Optional[float] = None

        if self_heroes and enemy_heroes:
            enemy_hero_near = True

            self_center = self._hpbar_center(self_heroes[0])

            for eb in enemy_heroes:
                e_center = self._hpbar_center(eb)
                d2 = self._dist2_pts(self_center, e_center)
                d = d2 ** 0.5
                if dist_screen_min is None or d < dist_screen_min:
                    dist_screen_min = d

        # ----------------- 2) по миникарте -----------------
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
                    d = d2 ** 0.5
                    if dist_mm_min is None or d < dist_mm_min:
                        dist_mm_min = d

        # ----------------- 3) объединяем критерии -----------------

        near_by_mm = (
                dist_mm_min is not None and dist_mm_min <= minimap_radius_uv
        )

        enemy_hero_near = bool(near_by_mm)

        return enemy_hero_near, dist_screen_min, dist_mm_min
    def _sense_near_enemy_tower(
        self,
        c: Dict[str, Any],
        *,
        radius_uv: float = 7.0,
        only_alive: bool = True,
    ) -> tuple[bool, Optional[float]]:
        """
        True, если self находится близко к ЛЮБОЙ вражеской башне
        в координатах миникарты (0..100).

        radius_uv — радиус в тех же единицах (0..100 по оси).
        Возвращает: (near_enemy_tower, dist_mm_min)
        """
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
            d = d2 ** 0.5
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
        self.last_action_ts = 0.0  # можно ресетить что-то при входе
    # --------- обработчики состояний (пока примитивные заглушки) ---------
    def _tick_dead(self, c: Dict[str, Any], s: Senses):
        pass
    def _tick_idle(self, c: Dict[str, Any], s: Senses):
        pass

    def _tick_laning(self, c: Dict[str, Any], s: Senses):
        """
        Лайнинг:
          1) если есть активная цель для ластхита и она ещё жива — ждём (не даём новых команд).
          2) иначе ищем крипа с низким HP, кликаем по нему через click_on_screen.
          3) если ничего не нашли — занимаем позицию по линии через click_on_screen_walk.
        """

        now = time.time()

        # --- 1. Если уже атакуем конкретного крипа, ждём пока он "исчезнет" ---
        if self._lasthit_target_key is not None:
            if self._lasthit_target_still_present(c):
                # крип ещё жив и трекается => не сломаем атаку лишними командами
                if self.log:
                    self.log.debug("[BRAIN] lasthit: waiting current target to die")
                return
            else:
                # цель умерла / исчезла — можно продолжать обычный тик
                if self.log:
                    self.log.debug("[BRAIN] lasthit: target gone, resume laning")
                self._lasthit_target_key = None

        # --- 2. Попытка найти крипа для ластхита ---
        lasthit_candidate = self._select_lasthit_creep(c)
        if lasthit_candidate is not None:
            cb, cx, cy = lasthit_candidate

            # клик по крипу (предполагаем, что click_on_screen ждёт клиентские координаты)
            if self.log:
                self.log.debug(
                    f"[BRAIN] lasthit: click creep at ({cx:.1f},{cy:.1f}), "
                    f"hp_ratio={getattr(cb, 'hp_ratio', None)}"
                )

            # обычный ПКМ / атак-клик — зависит от того, как реализован planner.click_on_screen
            # Если там есть флаг attack=True — можно его передать.

            self.pl.click_on_screen(self.hwnd, int(cx), int(cy) + 10, attack=True)


            # сохраняем ключ цели + таймаут
            key = self._make_lasthit_key(cb, cx, cy)
            self._lasthit_target_key = key
            self._lasthit_target_expire = now + 1.5  # секунды ожидания одной анимации атаки

            self.last_action_ts = now
            return  # в этом тике больше ничего не делаем

        # --- 3. Обычный лайнинг по линии, если нечего ластхитить ---

        dist_to_enemy = self._compute_dist_to_enemy_hero(s)

        target = self._compute_laning_point(c, min_dist_to_enemy=dist_to_enemy)
        if target is None:
            return

        tx, ty = target

        # можешь добавить ограничение по частоте кликов, если нужно:
        # if now - self.last_action_ts < 0.2:
        #     return

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
            # не знаем позицию героя — ничего не делаем
            return

        me = self_units[0]
        try:
            me_x = float(me["x"])
            me_y = float(me["y"])
        except Exception:
            return

        towers = c.get("towers", {}).get("ally", [])
        if not towers:
            # нет данных по башням — нечего делать
            return

        # фильтруем T1, если есть поле tier
        t1_list = []

        for t in towers:
            tier = t.get("tier", None)
            if tier is None or tier == 1:
                # либо явно tier==1, либо неизвестный tier — считаем T1
                t1_list.append(t)

        if not t1_list:
            t1_list = towers  # на всякий случай: fallback на все ally башни

        # проверяем: близко ли мы к какой-нибудь T1

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
            # всё ок, стоим и ждём
            if self.log:
                self.log.debug(f"[BRAIN {hex(self.hwnd)}] WAIT_START: already near T1")
            return

        # далеко от всех T1 → выбираем случайную и идём к ней
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

        # сразу даём команду на движение (можно и в _tick_moving делать, но так будет отзывчивее)
        self.pl.click_minimap_pct(self.hwnd, tx + 1, ty + 1, attack=False)
        self.last_action_ts = time.time()



    def _tick_moving(self, c: Dict[str, Any], s: Senses):
        if self._moving_point is None:
            # цель потеряли — просто в IDLE
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
            # пришли в нужный радиус
            if self.log:
                self.log.debug(
                    f"[BRAIN {hex(self.hwnd)}] MOVING: arrived at target ({tx:.1f},{ty:.1f})"
                )

            self._moving_point = None
            self._set_state(BrainState.IDLE)
            return


