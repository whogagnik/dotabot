import math
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.game.client_game_brain import (
    Brain,
    Senses,
    _point_at_progress,
    _project_to_lane,
)


class DummyPlanner:
    def __init__(self, side="radiant"):
        self.side = side

    def click_on_screen(self, *args, **kwargs):
        return None

    def click_minimap_pct(self, *args, **kwargs):
        return None

    def click_on_screen_walk(self, *args, **kwargs):
        return None


def _make_senses(landmarks):
    return Senses(
        alive=True,
        t_game=300.0,
        hp_ratio=1.0,
        low_hp=False,
        enemy_hero_near=False,
        enemy_hero_dist_screen=None,
        enemy_hero_dist_mm=None,
        enemy_hero_cnt_screen=0,
        avg_enemy_hero_hp_ratio_screen=None,
        enemy_creep_near=False,
        enemy_creep_dist_screen=None,
        ally_creep_near=False,
        ally_creep_dist_screen=None,
        under_ally_tower=False,
        ally_tower_dist_mm=None,
        near_enemy_tower=False,
        enemy_tower_dist_mm=None,
        landmarks=landmarks,
    )


def test_project_to_lane_progress():
    poly = [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}]
    q, t_progress, dist2 = _project_to_lane(poly, (5.0, 5.0))
    assert q == (5.0, 0.0)
    assert math.isclose(t_progress, 0.5, abs_tol=1e-6)
    assert math.isclose(dist2, 25.0, abs_tol=1e-6)


def test_point_at_progress():
    poly = [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}]
    q = _point_at_progress(poly, 0.5)
    assert q is not None
    assert math.isclose(q[0], 5.0, abs_tol=1e-6)
    assert math.isclose(q[1], 0.0, abs_tol=1e-6)


def test_compute_lane_target_uv_between_towers():
    planner = DummyPlanner(side="radiant")
    brain = Brain(hwnd=1, planner=planner)

    lane_poly = [[{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}]]
    landmarks = {
        "lane_mid": lane_poly,
        "fountain_radiant": {"x": 0.0, "y": 0.0},
        "fountain_dire": {"x": 100.0, "y": 100.0},
    }

    c = {
        "landmarks": landmarks,
        "map": {"self": [{"x": 20.0, "y": 20.0}]},
        "towers": {
            "ally": [{"x": 30.0, "y": 30.0, "alive": True}],
            "enemy": [{"x": 70.0, "y": 70.0, "alive": True}],
        },
    }

    s = _make_senses(landmarks)
    target = brain._compute_lane_target_uv(c, s)
    assert target is not None
    assert math.isclose(target[0], 44.0, abs_tol=1.5)
    assert math.isclose(target[1], 44.0, abs_tol=1.5)
