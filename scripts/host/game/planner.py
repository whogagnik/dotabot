# planner.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
from pathlib import Path
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
from time import perf_counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import cv2
from PIL import Image

from scripts.host.vision.screen_hp_scanner import scan_hp_bars_on_screen, HpBarBox
from scripts.host.core.config import *
from scripts.host.vision.hud.hud_scanner import SelfHud
from scripts.host.core.utils import debug_log_result
from scripts.host.ml.infer import (
    find_peaks_per_channel,
    infer_one_minimap,
    load_minimap_model,
)
from scripts.host.vision.tower_detector import TowerVisibilityTracker, load_landmarks  # type: ignore
from scripts.host.game.client_game_brain import Brain
from scripts.host.core.django_service import DjangoPlannerBridge

MAX_UNITS = {
    "self": 1,
    "ally": 10,
    "enemy": 10,
}

MERGE_RADIUS_PCT = 3.0


def _to_rgb_array(img: Any) -> np.ndarray:
    if isinstance(img, np.ndarray):
        arr = img
        if arr.ndim == 2:
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB)
        return arr
    return np.array(img.convert("RGB"))


def _pct_to_px(u: float, size: int) -> int:
    u = max(0.0, min(100.0, u))
    return int((u / 100.0) * (size - 1))


def _merge_close_points_uv(
    pts: list[tuple[float, float, float]],
    radius_pct: float,
) -> list[tuple[float, float, float]]:
    if not pts:
        return []

    pts_sorted = sorted(pts, key=lambda t: t[2], reverse=True)
    rad = max(1e-6, radius_pct / 100.0)

    kept: list[tuple[float, float, float]] = []
    for u, v, s in pts_sorted:
        too_close = False
        for U, V, _S in kept:
            du = u - U
            dv = v - V
            if (du * du + dv * dv) ** 0.5 <= rad:
                too_close = True
                break
        if not too_close:
            kept.append((u, v, s))
    return kept


def _filter_units_from_peaks(
    peaks: dict[int, list[tuple[float, float, float]]],
    classes: list[str],
    *,
    max_units: dict[str, int] = MAX_UNITS,
    merge_radius_pct: float = MERGE_RADIUS_PCT,
) -> dict[str, list[dict]]:
    out = {c: [] for c in classes}
    for ci, name in enumerate(classes):
        pts = peaks.get(ci, [])
        pts = _merge_close_points_uv(pts, merge_radius_pct)
        n = max_units.get(name, len(pts))
        pts = sorted(pts, key=lambda t: t[2], reverse=True)[:n]
        for u, v, s in pts:
            out[name].append(
                {
                    "x": u * 100.0,
                    "y": v * 100.0,
                    "score": float(s),
                }
            )
    return out


@dataclass
class Snapshot:
    ts: float
    hwnd: int
    combined: Dict[str, Any]


class Planner:
    """
    Один planner = одна VM.
    Внутри planner несколько brain-ов по hwnd.

    Planner не делает локальный screenshot и не шлет локальные click/key.
    Он:
    - берет последний кадр окна через django_bridge
    - анализирует кадр
    - генерирует команды через django_bridge
    """

    def __init__(
        self,
        hwnds: List[int],
        roles: List[str],
        django_bridge: DjangoPlannerBridge,
        side: str = "radiant",
        *,
        full_frame_min_dt: float = 0.01,
        win_crop_min_dt: float = 0.01,
        show_preview: bool = True,
        logger=None,
        collect_catboost_dataset: bool = False,
    ):
        self.hwnds = list(hwnds)
        self.roles = list(roles)
        self.side = side.lower().strip()
        self.django_bridge = django_bridge
        self.log = logger
        self.show_preview = bool(show_preview)

        self.self_hp = SelfHud()
        self.game_start_ts: float = time.time()

        self.full_frame_min_dt = max(0.0, float(full_frame_min_dt))
        self.win_crop_min_dt = max(0.0, float(win_crop_min_dt))

        self.last_by_hwnd: Dict[int, Snapshot] = {}

        self.history_len_creeps = 5
        self.max_keep_creep_frames = 2
        self._creep_history_by_hwnd: Dict[int, deque] = {}

        self._frame_ts_by_hwnd: Dict[int, float] = {}
        self._frame_size_by_hwnd: Dict[int, Tuple[int, int]] = {}
        self._last_roi_by_hwnd: Dict[int, Tuple[int, int, int, int]] = {}
        self._fps_prev_ts: float = time.time()
        self._fps_smooth: float = 0.0
        self._current_frame_id_by_hwnd: Dict[int, int] = {}

        with open(DEFAULT_LANDMARKS_DIR, "r", encoding="utf-8") as f:
            _landmarks_raw = json.load(f)

        if isinstance(_landmarks_raw, dict):
            self.landmarks = {k: v for k, v in _landmarks_raw.items() if k != "towers"}
        else:
            self.landmarks = _landmarks_raw

        radiant_pts, dire_pts, ancient_r, ancient_d, lanes = load_landmarks(
            DEFAULT_LANDMARKS_DIR
        )
        self.tower_tracker = TowerVisibilityTracker(
            radiant_pts,
            dire_pts,
            radiant_ancient=ancient_r,
            dire_ancient=ancient_d,
            lanes=lanes,
            timeout_sec=TIMEOUT_SEC_TOWER_DETECTOR,
            color_radius=COLOR_RADIUS_TOWER_DETECTOR,
        )

        self.net, self.classes, self.size = load_minimap_model(
            DEFAULT_ML_MINIMAP_DIR,
            device="cuda",
        )
        self.cls2idx = {c: i for i, c in enumerate(self.classes)}

        self.brains: Dict[int, Brain] = {
            hwnd: Brain(hwnd, planner=self, logger=logger, role=role,collect_catboost_dataset=collect_catboost_dataset)
            for hwnd, role in zip(self.hwnds, self.roles)
        }

        self.forbidden_ui_rects = [
            (240, 0, 610, 20),
            (0, 0, 90, 20),
            (0, 430, 130, 480),
            (220, 400, 620, 480),
            (710, 370, 850, 480),
        ]

        self.block_input: bool = True
        self._last_center_ts_by_hwnd: Dict[int, float] = {}
        self._center_cooldown_sec: float = 0.15

    # ------------------------------------------------------------------
    # Input frame access
    # ------------------------------------------------------------------

    def _get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        size = self._frame_size_by_hwnd.get(hwnd)

        if size is None:
            pil = self._grab_window_pil(hwnd)
            if pil is None:
                raise RuntimeError(f"No frame size for hwnd={hwnd}")
            size = pil.size
            self._frame_size_by_hwnd[hwnd] = size

        w, h = size
        return 0, 0, int(w), int(h)

    def _grab_window_pil(self, hwnd: int) -> Optional[Image.Image]:
        frame_id = self.django_bridge.get_last_frame_id(hwnd)
        if frame_id is None:
            return None

        self._current_frame_id_by_hwnd[int(hwnd)] = int(frame_id)

        pil = self.django_bridge.get_frame_pil(hwnd, frame_id)
        if pil is None:
            return None

        self._frame_size_by_hwnd[int(hwnd)] = pil.size

        ts = self.django_bridge.get_frame_ts(hwnd, frame_id)
        if ts is not None:
            self._frame_ts_by_hwnd[int(hwnd)] = float(ts)

        return pil

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    @debug_log_result
    def tick_one(self) -> Dict[int, Snapshot]:
        out: Dict[int, Snapshot] = {}

        # --- FPS ---
        now = time.time()
        dt = now - self._fps_prev_ts
        self._fps_prev_ts = now

        inst_fps = 1.0 / dt if dt > 1e-6 else 0.0
        self._fps_smooth = (
            inst_fps
            if self._fps_smooth == 0.0
            else self._fps_smooth * 0.9 + inst_fps * 0.1
        )

        for hwnd in list(self.hwnds):
            hay = self._grab_window_pil(hwnd)

            if hay is None:
                continue

            if self.block_input:
                self.center_screen_on_self(hwnd, force_fg=True)

            snap = self.collect_for_hwnd(hwnd, hay_pil=hay)
            if snap is None:
                continue

            if self.show_preview:
                v = visualize_full_frame(self, hwnd, snap, fps=self._fps_smooth)
                if v is not None:
                    cv2.imshow("planner", v)
                    cv2.waitKey(1)

            self.last_by_hwnd[hwnd] = snap
            out[hwnd] = snap

            brain = self.brains.get(hwnd)
            if brain is None:
                brain = Brain(hwnd, planner=self, logger=self.log, role="unknown")
                self.brains[hwnd] = brain

            if self.block_input:
                brain.tick_one(
                    Snapshot(ts=snap.ts, hwnd=snap.hwnd, combined=snap.combined)
                )


        return out

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _box_center(b) -> tuple[float, float]:
        return ((b.x0 + b.x1) * 0.5, (b.y0 + b.y1) * 0.5)

    @staticmethod
    def _center_dist2(b1, b2) -> float:
        x1, y1 = Planner._box_center(b1)
        x2, y2 = Planner._box_center(b2)
        dx = x1 - x2
        dy = y1 - y2
        return dx * dx + dy * dy

    @staticmethod
    def mask_rects_white(
        img: np.ndarray,
        rects: list[tuple[int, int, int, int]],
    ) -> np.ndarray:
        masked = img.copy()
        h, w = masked.shape[:2]

        for x0, y0, x1, y1 in rects:
            x0 = max(0, min(w, int(x0)))
            x1 = max(0, min(w, int(x1)))
            y0 = max(0, min(h, int(y0)))
            y1 = max(0, min(h, int(y1)))

            if x1 <= x0 or y1 <= y0:
                continue

            if masked.ndim == 2:
                masked[y0:y1, x0:x1] = 255
            else:
                masked[y0:y1, x0:x1, :3] = 255

        return masked

    @debug_log_result
    def _point_in_forbidden(self, x: int, y: int) -> bool:
        for x0, y0, x1, y1 in self.forbidden_ui_rects:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return False

    @debug_log_result
    def _adjust_click_for_forbidden(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        _, _, w, h = self._get_client_rect(hwnd)

        x = max(0, min(w - 1, int(x)))
        y = max(0, min(h - 1, int(y)))

        if not self._point_in_forbidden(x, y):
            return x, y

        cx = w / 2.0
        cy = h / 2.0
        dx = x - cx
        dy = y - cy

        if abs(dx) < 1e-3 and abs(dy) < 1e-3:
            dx, dy = 0.0, -1.0

        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-3:
            return x, y

        step_len = 5.0
        step_x = -dx / length * step_len
        step_y = -dy / length * step_len

        cur_x = float(x)
        cur_y = float(y)
        max_iters = int(max(w, h) / step_len) + 5

        for _ in range(max_iters):
            cur_x += step_x
            cur_y += step_y

            cur_x = max(0.0, min(w - 1, cur_x))
            cur_y = max(0.0, min(h - 1, cur_y))

            ix = int(round(cur_x))
            iy = int(round(cur_y))

            if not self._point_in_forbidden(ix, iy):
                return ix, iy

        return int(cx), int(cy)

    # ------------------------------------------------------------------
    # Command emitters
    # ------------------------------------------------------------------

    def _emit_command(self, hwnd: int, command_type: str, payload: Dict[str, Any]) -> None:
        frame_id = self._current_frame_id_by_hwnd.get(int(hwnd))
        self.django_bridge.push_command(
            hwnd=hwnd,
            command_type=command_type,
            payload=payload,
            frame_id=frame_id,
        )

    def _send_mouse_click_client(self, hwnd, x, y, button="right"):

        self.django_bridge.push_command(
            hwnd,
            "mouse_click",
            {
                "hwnd": int(hwnd),
                "x": int(x),
                "y": int(y),
                "button": str(button),
                "coord_space": "client",
                "clicks": 1,
            },
        )

    def _send_key_to_hwnd(self, hwnd, vk_code, down):
        self.django_bridge.push_command(
            hwnd,
            "key_event",
            {
                "hwnd": int(hwnd),
                "vk_code": int(vk_code),
                "down": bool(down),
            },
        )

    def _press_vk_global(self, vk: int, *, hold_ms: int = 25) -> None:
        for hwnd in self.hwnds:
            self._emit_command(
                hwnd,
                "key_press",
                {
                    "hwnd": int(hwnd),
                    "vk_code": int(vk),
                    "hold_ms": int(hold_ms),
                },
            )

    def _press_vk_for_hwnd(
        self,
        hwnd: int,
        vk: int,
        *,
        hold_ms: int = 25,
        force_fg: bool = True,
    ) -> None:
        self._emit_command(
            hwnd,
            "key_press",
            {
                "hwnd": int(hwnd),
                "vk_code": int(vk),
                "hold_ms": int(hold_ms),
                "force_fg": bool(force_fg),
            },
        )

    def center_screen_on_self(
        self,
        hwnd: int,
        *,
        force_fg: bool = True,
        cooldown_sec: Optional[float] = 1,
    ) -> None:
        now = time.time()
        cd = self._center_cooldown_sec if cooldown_sec is None else float(cooldown_sec)
        last = self._last_center_ts_by_hwnd.get(hwnd, 0.0)

        if cd > 0 and (now - last) < cd:
            return

        self._press_vk_for_hwnd(
            hwnd, KEY_FOR_CENTER_SCREEN, hold_ms=0, force_fg=force_fg
        )
        self._press_vk_for_hwnd(
            hwnd, KEY_FOR_CENTER_SCREEN, hold_ms=0, force_fg=force_fg
        )
        self._last_center_ts_by_hwnd[hwnd] = now

    @debug_log_result
    def click_on_screen_walk(self, hwnd: int, x: int, y: int, *, attack: bool = False):
        _, _, win_w, win_h = self._get_client_rect(hwnd)

        x = max(0, min(win_w - 1, int(x)))
        y = max(0, min(win_h - 1, int(y)))
        x_adj, y_adj = self._adjust_click_for_forbidden(hwnd, x, y)

        if attack:
            self._emit_command(
                hwnd,
                "attack_click",
                {
                    "hwnd": int(hwnd),
                    "x": int(x_adj),
                    "y": int(y_adj),
                    "coord_space": "client",
                },
            )
        else:
            self._send_mouse_click_client(hwnd, x_adj, y_adj, button="right")

    @debug_log_result
    def click_on_screen(
        self,
        hwnd: int,
        x: int,
        y: int,
        *,
        mouse_button: str = "right",
        attack: bool = False,
    ) -> None:
        _, _, win_w, win_h = self._get_client_rect(hwnd)

        x = max(0, min(win_w - 1, int(x)))
        y = max(0, min(win_h - 1, int(y)))
        x_adj, y_adj = self._adjust_click_for_forbidden(hwnd, x, y)

        if attack:
            self._emit_command(
                hwnd,
                "attack_click",
                {
                    "hwnd": int(hwnd),
                    "x": int(x_adj),
                    "y": int(y_adj),
                    "coord_space": "client",
                },
            )
        else:
            self._send_mouse_click_client(hwnd, x_adj, y_adj, button=mouse_button)

    @debug_log_result
    def click_minimap_pct(self, hwnd: int, u: float, v: float, *, attack: bool = False):
        if hwnd not in self._last_roi_by_hwnd:
            raise RuntimeError("ROI unknown; call collect_for_hwnd() first.")

        rx, ry, rw, rh = self._last_roi_by_hwnd[hwnd]
        px = rx + _pct_to_px(u, rw)
        py = ry + _pct_to_px(v, rh)

        if attack:
            self._emit_command(
                hwnd,
                "attack_click",
                {
                    "hwnd": int(hwnd),
                    "x": int(px),
                    "y": int(py),
                    "coord_space": "client",
                },
            )
        else:
            self._emit_command(
                hwnd,
                "mouse_click",
                {
                    "hwnd": int(hwnd),
                    "x": int(px),
                    "y": int(py),
                    "button": "right",
                    "coord_space": "client",
                    "clicks": 3,
                },
            )

    # ------------------------------------------------------------------
    # Vision pipeline
    # ------------------------------------------------------------------

    def _stabilize_creeps_for_hwnd(
        self,
        hwnd: int,
        new_creeps: Dict[str, List[HpBarBox]],
    ) -> Dict[str, List[HpBarBox]]:
        hist = self._creep_history_by_hwnd.setdefault(
            hwnd,
            deque(maxlen=self.history_len_creeps),
        )
        hist.appendleft(
            {
                "ally": list(new_creeps.get("ally", [])),
                "enemy": list(new_creeps.get("enemy", [])),
            }
        )

        if len(hist) == 1:
            return new_creeps

        base_dist_thr_px = 45.0
        age_step_increase = 10.0
        max_keep = self.max_keep_creep_frames
        roi_margin_px = 80.0

        stabilized: Dict[str, List[HpBarBox]] = {"ally": [], "enemy": []}

        def _center(box: HpBarBox):
            return ((box.x0 + box.x1) * 0.5, (box.y0 + box.y1) * 0.5)

        current_frame = hist[0]

        for side in ("ally", "enemy"):
            current = list(current_frame[side])
            kept = list(current)

            if not current:
                stabilized[side] = []
                continue

            cx_list = []
            cy_list = []
            for b in current:
                cx, cy = _center(b)
                cx_list.append(cx)
                cy_list.append(cy)

            min_cx = min(cx_list) - roi_margin_px
            max_cx = max(cx_list) + roi_margin_px
            min_cy = min(cy_list) - roi_margin_px
            max_cy = max(cy_list) + roi_margin_px

            for age, past in enumerate(list(hist)[1:], start=1):
                if age > max_keep:
                    break

                dist_thr_px = base_dist_thr_px + age_step_increase * age
                dist_thr2 = dist_thr_px * dist_thr_px

                for old_box in past[side]:
                    ocx, ocy = _center(old_box)

                    if not (min_cx <= ocx <= max_cx and min_cy <= ocy <= max_cy):
                        continue

                    has_match = False
                    for b in kept:
                        if self._center_dist2(old_box, b) <= dist_thr2:
                            has_match = True
                            break

                    if not has_match:
                        kept.append(old_box)

            stabilized[side] = kept

        return stabilized

    def _crop_minimap_from_window(
        self,
        hwnd: int,
        hay_pil: Image.Image,
    ) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
        _, _, w, h = self._get_client_rect(hwnd)

        rx_l = w - MM_W - MM_DX
        ry_l = h - MM_H - MM_DY

        if rx_l < 0 or ry_l < 0 or (rx_l + MM_W) > w or (ry_l + MM_H) > h:
            arr = np.array(hay_pil)
            sub = arr[
                max(0, ry_l) : max(0, ry_l) + MM_H,
                max(0, rx_l) : max(0, rx_l) + MM_W,
                :,
            ]
            if sub.shape[0] != MM_H or sub.shape[1] != MM_W:
                raise RuntimeError(
                    f"minimap crop OOB: local=({rx_l},{ry_l},{MM_W},{MM_H}) win=({w},{h})"
                )
            return sub.copy(), (rx_l, ry_l, MM_W, MM_H)

        sub = hay_pil.crop((rx_l, ry_l, rx_l + MM_W, ry_l + MM_H))
        mm_rgb = np.array(sub.convert("RGB"))
        return mm_rgb, (rx_l, ry_l, MM_W, MM_H)

    @staticmethod
    def _pack_nn(
        peaks: Dict[int, List[Tuple[float, float, float]]],
        classes: List[str],
    ) -> Dict[str, List[Dict[str, float]]]:
        out: Dict[str, List[Dict[str, float]]] = {c: [] for c in classes}
        for ci, pts in peaks.items():
            cname = classes[ci]
            for u, v, s in pts:
                out[cname].append(
                    {
                        "x": float(u * 100.0),
                        "y": float(v * 100.0),
                        "score": float(s),
                    }
                )
        return out

    @debug_log_result
    def collect_for_hwnd(
        self, hwnd: int, hay_pil: Optional[Image.Image] = None
    ) -> Optional[Snapshot]:
        hay = hay_pil or self._grab_window_pil(hwnd)
        if hay is None:
            return None

        try:
            mm_rgb, roi = self._crop_minimap_from_window(hwnd, hay)
        except Exception as e:
            if self.log:
                self.log.error(f"[MM] crop failed hwnd={hex(hwnd)}: {e}", exc_info=True)
            return None

        t_total0 = perf_counter()

        device = next(self.net.parameters()).device.type
        prob = infer_one_minimap(self.net, mm_rgb, size=self.size, device=device)
        peaks = find_peaks_per_channel(prob, thr=DEFAULT_THR, nms_kernel=DEFAULT_NMS)
        units = _filter_units_from_peaks(peaks, self.classes)

        frame = np.array(hay)
        hp_cur, hp_max = self.self_hp.get_hp(frame)

        now_s = self._frame_ts_by_hwnd.get(hwnd, time.time())
        t_game = now_s - self.game_start_ts

        towers = self.tower_tracker.tick_one(mm_rgb, now=now_s, side=self.side)

        frame_masked = self.mask_rects_white(frame, self.forbidden_ui_rects)
        screen_info = scan_hp_bars_on_screen(frame_masked)

        raw_heroes = screen_info["heroes"]
        raw_creeps = screen_info["creeps"]
        stable_creeps = self._stabilize_creeps_for_hwnd(hwnd, raw_creeps)

        if hp_cur is None or hp_max is None:
            alive = False
            hp_ratio = None
        else:
            alive = hp_cur > 0
            hp_ratio = float(hp_cur) / float(hp_max) if hp_max > 0 else None

        combined = {
            "map": units,
            "towers": towers,
            "landmarks": self.landmarks,
            "hp_pair": (hp_cur, hp_max),
            "hp_ratio": hp_ratio,
            "gold": 123,
            "alive": alive,
            "t_game": t_game,
            "heroes": raw_heroes,
            "creeps": stable_creeps,
        }

        self._last_roi_by_hwnd[hwnd] = roi

        if self.log:
            t_total = (perf_counter() - t_total0) * 1000.0
            self.log.debug(f"[TIMERS] hwnd={hex(hwnd)} TOTAL={t_total:.2f}ms")

        return Snapshot(ts=now_s, hwnd=hwnd, combined=combined)


def visualize_full_frame(
    pl: "Planner",
    hwnd: int,
    snap: Snapshot,
    fps: float = 0.0,
) -> Optional[np.ndarray]:
    hay = pl._grab_window_pil(hwnd)
    if hay is None:
        if pl.log:
            pl.log.debug(f"[VIS] _grab_window_pil() returned None for hwnd={hex(hwnd)}")
        return None

    frame_rgb = np.array(hay)
    img = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    H, W = img.shape[:2]

    combined = snap.combined
    heroes = combined.get("heroes", {})
    creeps = combined.get("creeps", {})
    units = combined.get("map", {})
    towers = combined.get("towers", {})
    hp_pair = combined.get("hp_pair")

    col_heroes = {
        "enemy": (0, 0, 255),
        "ally": (0, 255, 0),
        "self": (255, 0, 0),
    }
    col_creeps = {
        "enemy": (0, 255, 255),
        "ally": (255, 255, 0),
    }

    for kind, color in col_heroes.items():
        for b in heroes.get(kind, []):
            cv2.rectangle(
                img, (b.x0, b.y0), (b.x1, b.y1), color, 1, lineType=cv2.LINE_AA
            )

    for kind, color in col_creeps.items():
        for b in creeps.get(kind, []):
            cv2.rectangle(
                img, (b.x0, b.y0), (b.x1, b.y1), color, 1, lineType=cv2.LINE_AA
            )

    _, _, win_w, win_h = pl._get_client_rect(hwnd)
    mm_x0 = win_w - MM_W - MM_DX
    mm_y0 = win_h - MM_H - MM_DY
    mm_x1 = mm_x0 + MM_W - 1
    mm_y1 = mm_y0 + MM_H - 1

    if not (mm_x0 < 0 or mm_y0 < 0 or mm_x1 >= W or mm_y1 >= H):
        unit_colors = {
            "self": (40, 215, 255),
            "ally": (80, 220, 80),
            "enemy": (40, 40, 240),
        }

        def _mm_to_px(x_pct: float, y_pct: float) -> tuple[int, int]:
            px = int(round(mm_x0 + (x_pct / 100.0) * (MM_W - 1)))
            py = int(round(mm_y0 + (y_pct / 100.0) * (MM_H - 1)))
            return px, py

        for kind, color in unit_colors.items():
            for det in units.get(kind, []):
                ux = float(det["x"])
                uy = float(det["y"])
                px, py = _mm_to_px(ux, uy)
                cv2.rectangle(
                    img,
                    (px - 3, py - 3),
                    (px + 3, py + 3),
                    color,
                    1,
                    lineType=cv2.LINE_AA,
                )
                cv2.circle(img, (px, py), 1, color, -1, lineType=cv2.LINE_AA)

        tower_colors = {"ally": (0, 180, 0), "enemy": (0, 0, 180)}
        for side in ("ally", "enemy"):
            for t in towers.get(side, []):
                try:
                    tx = float(t["x"])
                    ty = float(t["y"])
                except Exception:
                    continue

                alive = bool(t.get("alive", True))
                px, py = _mm_to_px(tx, ty)
                col = tower_colors.get(side, (200, 200, 200))
                thickness = 2 if alive else 1

                cv2.circle(img, (px, py), 5, col, thickness, lineType=cv2.LINE_AA)
                if not alive:
                    cv2.line(
                        img, (px - 4, py - 4), (px + 4, py + 4), col, 1, cv2.LINE_AA
                    )
                    cv2.line(
                        img, (px - 4, py + 4), (px + 4, py - 4), col, 1, cv2.LINE_AA
                    )

    heroes_counts = {k: len(heroes.get(k, [])) for k in ("enemy", "ally", "self")}
    creeps_counts = {k: len(creeps.get(k, [])) for k in ("enemy", "ally")}

    y = 20
    cv2.putText(
        img,
        f"Heroes: E={heroes_counts['enemy']} A={heroes_counts['ally']} S={heroes_counts['self']}",
        (10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    y += 20

    cv2.putText(
        img,
        f"Creeps: E={creeps_counts['enemy']} A={creeps_counts['ally']}",
        (10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    y += 20

    if hp_pair is not None:
        cv2.putText(
            img,
            f"HUD HP: {hp_pair}",
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )

    fps_text = f"{fps:5.1f} FPS"
    cv2.putText(
        img,
        fps_text,
        (W - 150, 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )

    img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
    return img


class _LocalDxcamDotaCapture:
    def __init__(self, *, output_idx: int = 0):
        import dxcam

        self._cam = dxcam.create(output_idx=int(output_idx), output_color="RGB")
        if self._cam is None:
            raise RuntimeError(f"dxcam.create(output_idx={output_idx}) returned None")

    @staticmethod
    def _desktop_bounds() -> tuple[int, int, int, int]:
        import win32api

        left = win32api.GetSystemMetrics(76)
        top = win32api.GetSystemMetrics(77)
        width = win32api.GetSystemMetrics(78)
        height = win32api.GetSystemMetrics(79)
        return int(left), int(top), int(left + width), int(top + height)

    @staticmethod
    def _clamp_region_to_desktop(
        region: tuple[int, int, int, int],
    ) -> Optional[tuple[int, int, int, int]]:
        left, top, right, bottom = region
        desk_left, desk_top, desk_right, desk_bottom = (
            _LocalDxcamDotaCapture._desktop_bounds()
        )

        left = max(left, desk_left)
        top = max(top, desk_top)
        right = min(right, desk_right)
        bottom = min(bottom, desk_bottom)

        if right <= left or bottom <= top:
            return None

        if desk_left != 0 or desk_top != 0:
            left -= desk_left
            right -= desk_left
            top -= desk_top
            bottom -= desk_top

        if right <= left or bottom <= top:
            return None

        return int(left), int(top), int(right), int(bottom)

    @staticmethod
    def _window_ok(hwnd: int) -> bool:
        import win32gui

        try:
            return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
        except Exception:
            return False

    @staticmethod
    def get_screen_client_rect(hwnd: int) -> tuple[int, int, int, int]:
        import win32gui

        try:
            left, top, right, bottom = win32gui.GetClientRect(int(hwnd))
            sx, sy = win32gui.ClientToScreen(int(hwnd), (0, 0))
            return sx, sy, max(1, right - left), max(1, bottom - top)
        except Exception:
            left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
            return left, top, max(1, right - left), max(1, bottom - top)

    def grab_window_rgb(self, hwnd: int) -> Optional[np.ndarray]:
        hwnd = int(hwnd)
        if not self._window_ok(hwnd):
            return None

        try:
            x, y, w, h = self.get_screen_client_rect(hwnd)
        except Exception:
            return None

        if w <= 0 or h <= 0:
            return None

        region = self._clamp_region_to_desktop((x, y, x + w, y + h))
        if region is None:
            return None

        try:
            frame = self._cam.grab(region=region)
        except Exception:
            return None

        if frame is None or not isinstance(frame, np.ndarray):
            return None
        if frame.ndim != 3 or frame.shape[2] != 3:
            return None
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)

        return frame.copy()

    def close(self) -> None:
        stop = getattr(self._cam, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass


class LocalPlanner(Planner):
    """
    Direct, single-machine planner for running this file as __main__.

    It replaces the Django bridge with dxcam frames and direct win32 input.
    The regular server/runtime path keeps using Planner unchanged.
    """

    def __init__(
        self,
        hwnds: List[int],
        roles: List[str],
        *,
        side: str = "radiant",
        output_idx: int = 0,
        logger=None,
        show_preview: bool = True,
    ):
        self._local_capture = _LocalDxcamDotaCapture(output_idx=output_idx)
        self._local_frame_seq: int = 0
        self._local_init_win32()

        try:
            super().__init__(
                hwnds=hwnds,
                roles=roles,
                side=side,
                django_bridge=DjangoPlannerBridge(vm_id="local"),
                logger=logger,
                show_preview=show_preview,
                collect_catboost_dataset=True
            )
        except Exception:
            self._local_capture.close()
            raise

    def _local_init_win32(self) -> None:
        import ctypes
        import win32api
        import win32con
        import win32gui
        import win32process

        self._ctypes = ctypes
        self._win32api = win32api
        self._win32con = win32con
        self._win32gui = win32gui
        self._win32process = win32process
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def close(self) -> None:
        self._local_capture.close()

    def _force_foreground(self, hwnd: int) -> None:
        hwnd = int(hwnd)
        try:
            if self._win32gui.IsIconic(hwnd):
                self._win32gui.ShowWindow(hwnd, self._win32con.SW_RESTORE)

            self._win32gui.ShowWindow(hwnd, self._win32con.SW_SHOWNORMAL)

            fore = self._win32gui.GetForegroundWindow()
            ftid = (
                self._win32process.GetWindowThreadProcessId(fore)[0] if fore else 0
            )
            ctid = self._win32api.GetCurrentThreadId()

            self._user32.AttachThreadInput(ftid, ctid, True)
            try:
                self._win32gui.BringWindowToTop(hwnd)
                self._win32gui.SetForegroundWindow(hwnd)
                self._win32gui.SetActiveWindow(hwnd)
            finally:
                self._user32.AttachThreadInput(ftid, ctid, False)

            time.sleep(0.06)
        except Exception:
            try:
                self._win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.06)
            except Exception:
                pass

    def _vk_scan_code(self, vk_code: int) -> int:
        try:
            return int(self._user32.MapVirtualKeyW(int(vk_code), 0))
        except Exception:
            return 0

    def _key_down_local(self, vk_code: int) -> None:
        vk_code = int(vk_code)
        self._win32api.keybd_event(vk_code, self._vk_scan_code(vk_code), 0, 0)

    def _key_up_local(self, vk_code: int) -> None:
        vk_code = int(vk_code)
        self._win32api.keybd_event(
            vk_code,
            self._vk_scan_code(vk_code),
            self._win32con.KEYEVENTF_KEYUP,
            0,
        )

    def _tap_vk_local(
        self,
        vk_code: int,
        *,
        hold_ms: int = 25,
        hwnd: Optional[int] = None,
        force_fg: bool = True,
    ) -> None:
        if hwnd is not None and force_fg:
            self._force_foreground(int(hwnd))

        self._key_down_local(vk_code)
        time.sleep(max(0, int(hold_ms)) / 1000.0)
        self._key_up_local(vk_code)

    def _mouse_click_local(
        self,
        hwnd: int,
        x: int,
        y: int,
        *,
        button: str = "right",
        clicks: int = 1,
        coord_space: str = "client",
        force_fg: bool = True,
    ) -> None:
        hwnd = int(hwnd)
        button = str(button).lower()
        coord_space = str(coord_space)

        if force_fg:
            self._force_foreground(hwnd)

        if coord_space == "screen":
            sx, sy = int(x), int(y)
        else:
            win_x, win_y, win_w, win_h = (
                self._local_capture.get_screen_client_rect(hwnd)
            )
            cx = max(0, min(win_w - 1, int(x)))
            cy = max(0, min(win_h - 1, int(y)))
            sx, sy = int(win_x + cx), int(win_y + cy)

        self._win32api.SetCursorPos((sx, sy))
        time.sleep(0.01)

        if button == "left":
            down_flag = self._win32con.MOUSEEVENTF_LEFTDOWN
            up_flag = self._win32con.MOUSEEVENTF_LEFTUP
        elif button == "middle":
            down_flag = self._win32con.MOUSEEVENTF_MIDDLEDOWN
            up_flag = self._win32con.MOUSEEVENTF_MIDDLEUP
        else:
            down_flag = self._win32con.MOUSEEVENTF_RIGHTDOWN
            up_flag = self._win32con.MOUSEEVENTF_RIGHTUP

        for _ in range(max(1, int(clicks))):
            self._win32api.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.02)
            self._win32api.mouse_event(up_flag, 0, 0, 0, 0)
            time.sleep(0.03)

    def _attack_click_local(
        self,
        hwnd: int,
        x: int,
        y: int,
        *,
        coord_space: str = "client",
        force_fg: bool = True,
    ) -> None:
        if force_fg:
            self._force_foreground(int(hwnd))
        self._tap_vk_local(ord("A"), hold_ms=25, hwnd=hwnd, force_fg=False)
        time.sleep(0.03)
        self._mouse_click_local(
            hwnd,
            x,
            y,
            button="left",
            clicks=1,
            coord_space=coord_space,
            force_fg=False,
        )

    def _get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        size = self._frame_size_by_hwnd.get(int(hwnd))
        if size is None:
            _, _, w, h = self._local_capture.get_screen_client_rect(int(hwnd))
            size = (int(w), int(h))
            self._frame_size_by_hwnd[int(hwnd)] = size

        w, h = size
        return 0, 0, int(w), int(h)

    def _grab_window_pil(self, hwnd: int) -> Optional[Image.Image]:
        frame_rgb = self._local_capture.grab_window_rgb(int(hwnd))
        if frame_rgb is None:
            if self.log:
                self.log.debug(
                    f"[LocalPlanner] dxcam returned no frame for hwnd={hex(int(hwnd))}"
                )
            return None

        self._local_frame_seq += 1
        self._current_frame_id_by_hwnd[int(hwnd)] = self._local_frame_seq
        self._frame_ts_by_hwnd[int(hwnd)] = time.time()

        pil = Image.fromarray(frame_rgb, mode="RGB")
        self._frame_size_by_hwnd[int(hwnd)] = pil.size
        return pil

    def _emit_command(self, hwnd: int, command_type: str, payload: Dict[str, Any]) -> None:
        if command_type == "mouse_click":
            self._mouse_click_local(
                int(payload.get("hwnd", hwnd)),
                int(payload["x"]),
                int(payload["y"]),
                button=str(payload.get("button", "right")),
                clicks=int(payload.get("clicks", 1)),
                coord_space=str(payload.get("coord_space", "client")),
                force_fg=bool(payload.get("force_fg", True)),
            )
            return

        if command_type == "attack_click":
            self._attack_click_local(
                int(payload.get("hwnd", hwnd)),
                int(payload["x"]),
                int(payload["y"]),
                coord_space=str(payload.get("coord_space", "client")),
                force_fg=bool(payload.get("force_fg", True)),
            )
            return

        if command_type == "key_press":
            self._tap_vk_local(
                int(payload["vk_code"]),
                hold_ms=int(payload.get("hold_ms", 25)),
                hwnd=int(payload.get("hwnd", hwnd)),
                force_fg=bool(payload.get("force_fg", True)),
            )
            return

        if command_type == "key_event":
            self._send_key_to_hwnd(
                int(payload.get("hwnd", hwnd)),
                int(payload["vk_code"]),
                bool(payload["down"]),
            )
            return

        if self.log:
            self.log.warning(f"[LocalPlanner] unsupported command: {command_type}")

    def _send_mouse_click_client(self, hwnd, x, y, button="right"):
        self._mouse_click_local(
            int(hwnd),
            int(x),
            int(y),
            button=str(button),
            clicks=1,
            coord_space="client",
        )

    def _send_key_to_hwnd(self, hwnd, vk_code, down):
        self._force_foreground(int(hwnd))
        if bool(down):
            self._key_down_local(int(vk_code))
        else:
            self._key_up_local(int(vk_code))

    def _press_vk_global(self, vk: int, *, hold_ms: int = 25) -> None:
        for hwnd in self.hwnds:
            self._tap_vk_local(int(vk), hold_ms=hold_ms, hwnd=int(hwnd))

    def _press_vk_for_hwnd(
        self,
        hwnd: int,
        vk: int,
        *,
        hold_ms: int = 25,
        force_fg: bool = True,
    ) -> None:
        self._tap_vk_local(int(vk), hold_ms=hold_ms, hwnd=int(hwnd), force_fg=force_fg)


def _parse_hwnd(raw: str) -> int:
    return int(str(raw), 0)


def _find_current_dota_hwnd(preferred_hwnd: Optional[int] = None) -> Optional[int]:
    import win32gui

    def _title_matches(hwnd: int) -> bool:
        try:
            title = (win32gui.GetWindowText(int(hwnd)) or "").strip().lower()
        except Exception:
            return False
        return "dota 2" in title

    def _window_ok(hwnd: int) -> bool:
        try:
            return (
                bool(win32gui.IsWindow(int(hwnd)))
                and bool(win32gui.IsWindowVisible(int(hwnd)))
                and not bool(win32gui.IsIconic(int(hwnd)))
            )
        except Exception:
            return False

    if preferred_hwnd is not None:
        hwnd = int(preferred_hwnd)
        if _window_ok(hwnd):
            return hwnd
        raise RuntimeError(f"hwnd is not a visible live window: {hwnd}")

    foreground = win32gui.GetForegroundWindow()
    if foreground and _window_ok(foreground) and _title_matches(foreground):
        return int(foreground)

    found: Optional[int] = None

    def _enum_cb(hwnd, _):
        nonlocal found
        if found is not None:
            return
        if _window_ok(int(hwnd)) and _title_matches(int(hwnd)):
            found = int(hwnd)

    win32gui.EnumWindows(_enum_cb, None)
    return found


def _build_local_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Planner locally against the current Dota 2 window via dxcam."
    )
    parser.add_argument("--hwnd", type=_parse_hwnd, default=None)
    parser.add_argument("--side", choices=("radiant", "dire"), default="radiant")
    parser.add_argument("--role", default="unknown")
    parser.add_argument("--fps", type=float, default=40.0)
    parser.add_argument("--output-idx", type=int, default=0)
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _run_local_main() -> int:
    args = _build_local_arg_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level)),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("planner.local")

    hwnd = _find_current_dota_hwnd(args.hwnd)
    if hwnd is None:
        log.error("Dota 2 window was not found. Focus Dota or pass --hwnd 0x...")
        return 2

    log.info(
        "Local planner attached to hwnd=%s side=%s role=%s input=%s preview=%s",
        hex(int(hwnd)),
        args.side,
        args.role,
        "off" if args.no_input else "on",
        "off" if args.no_preview else "on",
    )

    planner = LocalPlanner(
        hwnds=[int(hwnd)],
        roles=[str(args.role)],
        side=str(args.side),
        output_idx=int(args.output_idx),
        logger=log,
        show_preview=not bool(args.no_preview),
    )
    planner.block_input = not bool(args.no_input)

    min_dt = 1.0 / max(0.1, float(args.fps))

    try:
        while True:
            t0 = time.time()
            try:
                planner.tick_one()

            except Exception:
                log.exception("Local planner tick failed")
                time.sleep(0.5)

            if not args.no_preview:
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break

            elapsed = time.time() - t0
            sleep_s = min_dt - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        log.info("Stopping local planner")
    finally:
        planner.close()
        if not args.no_preview:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(_run_local_main())
