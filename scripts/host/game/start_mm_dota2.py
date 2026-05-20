# scripts/host/game/start_mm_dota2.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import json
import os
import random
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any, Tuple

import cv2
import numpy as np
from PIL import Image

from scripts.host.core.config import (
    DEFAULT_ID_HERO_TO_NAME,
    MM_PARTY_INVITE_TIMEOUT_SEC,
    OUT_FILE_HERO_BUILDS,
)
from scripts.host.game.planner_runtime import planner_runtime


PICK_ROLE_ORDER = ["Carry", "Offlane", "Hard Support", "Support", "Mid"]
PICK_SUPPORT_ROLES = ["Hard Support", "Support"]
PICK_CORE_ROLES = ["Carry", "Offlane"]
PICK_MID_ROLES = ["Mid"]
PICK_PHASE_ORDER = ["supports", "cores", "mid"]
PICK_PHASE_ROLES = {
    "supports": PICK_SUPPORT_ROLES,
    "cores": PICK_CORE_ROLES,
    "mid": PICK_MID_ROLES,
}


class MmStage:
    IDLE = "idle"
    WAIT_DOTA_READY = "wait_dota_ready"
    BUILD_PARTY = "build_party"
    WAIT_ACCEPT_GAME = "wait_accept_game"
    DETECT_SIDE = "detect_side"
    START_GAME = "start_game"
    PICK_HEROES = "pick_heroes"
    DONE = "done"


class HostCommandType:
    FOCUS_WINDOW = "focus_window"
    MOUSE_CLICK = "mouse_click"
    WRITE_TEXT = "write_text"
    KEY_PRESS = "key_press"
    HOTKEY = "hotkey"
    CAPTURE_FRAME = "capture_frame"
    SLEEP = "sleep"


@dataclass
class HwndState:
    hwnd: int
    dota_ready: bool = False
    side: Optional[str] = None
    invite_accepted: bool = False
    game_accepted: bool = False
    invite_sent_ts: float = 0.0
    latest_frame_ts: float = 0.0
    self_found: bool = False
    last_log_ts: float = 0.0


@dataclass
class VmMmState:
    vm_id: str
    stage: str = MmStage.IDLE
    hwnds: List[int] = field(default_factory=list)
    windows: Dict[int, HwndState] = field(default_factory=dict)
    side: Optional[str] = None

    party_built: bool = False
    start_game_done: bool = False
    heroes_picked: bool = False

    last_stage_ts: float = 0.0
    last_action_ts: float = 0.0
    inflight: bool = False
    start_game_step: int = 0

    party_search_done: bool = False
    party_search_clicked: bool = False
    party_add_done: bool = False
    party_accept_done: bool = False
    party_invite_index: int = 1
    party_return_to_dota_pending: bool = False
    party_invited_indices: List[int] = field(default_factory=list)
    party_retry_invite_active: bool = False

    pick_role_by_hwnd: Dict[int, str] = field(default_factory=dict)
    pick_hwnd_by_role: Dict[str, int] = field(default_factory=dict)
    pick_phase: str = "init"
    pick_phase_started_ts: float = 0.0
    pick_wait_started_ts: float = 0.0
    pick_wait_seen_disabled_hwnds: List[int] = field(default_factory=list)
    pick_candidate_pools_by_hwnd: Dict[int, List[str]] = field(default_factory=dict)
    pick_candidate_index_by_hwnd: Dict[int, int] = field(default_factory=dict)
    pick_probe_index: int = 0
    pick_selected_hero_by_hwnd: Dict[int, str] = field(default_factory=dict)
    pick_confirmed_ts_by_hwnd: Dict[int, float] = field(default_factory=dict)
    pick_confirmed_hwnds: List[int] = field(default_factory=list)
    pick_finalized_hwnds: List[int] = field(default_factory=list)
    pick_banned_heroes: List[str] = field(default_factory=list)


class StartMmDota2:
    """
    Host-side orchestration only.

    Host responsibilities:
    - request capture_frame from client
    - receive capture_frame result through on_command_result()
    - store frames locally before planner is active
    - match PNG on host
    - decide next low-level client command
    - advance state by tick_one()

    Client responsibilities:
    - execute low-level commands only
    """

    def __init__(
        self,
        logger: logging.Logger,
        queue_command: Callable[[str, str, Dict[str, Any]], Any],
        *,
        images_root: str = "images",
        confidence: float = 0.87,
    ):
        self.log = logger
        self.queue_command = queue_command
        self.images_root = images_root
        self.confidence = float(confidence)


        self._vm: Dict[str, VmMmState] = {}
        self._templates = self._load_templates()
        self._hero_template_key_by_name: Dict[str, str] = {}
        self._hero_display_name_by_norm: Dict[str, str] = {}
        self._pick_role_pools: Optional[Dict[str, List[str]]] = None

        # ВАЖНО:
        # До planner_runtime.attach_hwnds() bridge может ещё не отдавать кадры.
        # Поэтому StartMmDota2 хранит кадры сам, получая их из результата capture_frame.
        self._frame_cache: Dict[tuple[str, int], np.ndarray] = {}
        self._last_capture_request_ts: Dict[tuple[str, int], float] = {}

    # ---------------------------------------------------------
    # assets
    # ---------------------------------------------------------

    def _load_templates(self) -> Dict[str, Optional[np.ndarray]]:
        """
        Загружает все картинки из images/* рекурсивно.

        Пример ключей:
          images/lobby/dota.png                 -> lobby_dota
          images/lobby/close-welcome-ru.png     -> lobby_close_welcome_ru
          images/game/detect-radiant.png        -> game_detect_radiant
          images/steam/continue_anyway_ru.png   -> steam_continue_anyway_ru

        Также добавляет алиасы для старого кода:
          dota -> lobby_dota
          close_welcome_ru -> lobby_close_welcome_ru
        """
        out: Dict[str, Optional[np.ndarray]] = {}

        valid_ext = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

        root_abs = os.path.abspath(self.images_root)

        if not os.path.isdir(root_abs):
            self.log.warning(f"[MM] images_root not found: {root_abs}")
            return out

        for root, _, files in os.walk(root_abs):
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in valid_ext:
                    continue

                path = os.path.join(root, filename)

                rel = os.path.relpath(path, root_abs)
                rel_no_ext = os.path.splitext(rel)[0]

                # lobby/close-welcome-ru -> lobby_close_welcome_ru
                key = (
                    rel_no_ext
                    .replace("\\", "_")
                    .replace("/", "_")
                    .replace("-", "_")
                    .replace(" ", "_")
                    .lower()
                )

                img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)

                if img is None or img.size == 0:
                    self.log.warning(f"[MM] failed to load template: {path}")
                    out[key] = None
                    continue

                out[key] = img

        # Алиасы под текущий код.
        aliases = {
            # lobby
            "dota": "lobby_dota",
            "self": "lobby_self",

            "add_party": "lobby_add_party",
            "id_field_ru": "lobby_id_field_ru",
            "id_field_eng": "lobby_id_field_eng",
            "search_ru": "lobby_search_ru",
            "search_eng": "lobby_search_eng",
            'search_game_ru' : 'lobby_search_game_ru',
            "add": "lobby_add",

            "accept_invite_ru": "lobby_accept_invite_ru",
            "accept_invite_eng": "lobby_accept_invite_eng",
            "accept_game": "lobby_accept_game",
            "accept_eng": "lobby_accept_eng",
            "accept_reward_ru": "lobby_accept_reward_ru",
            'invite_pop' : 'lobby_invite_pop',
            "play_eng": "lobby_play_eng",
            "play_ru": "lobby_play_ru",
            'public_ru': 'lobby_public_ru',
            'selected_all_pick': 'lobby_selected_all_pick',
            'unselected_all_pick': 'lobby_unselected_all_pick',
            "continue": "lobby_continue",

            "queue": "lobby_queue",
            "ok": "lobby_ok",
            "rank": "lobby_rank",
            "friend_id": "lobby_friend_id",

            "close_welcome_ru": "lobby_close_welcome_ru",
            "close_welcome_ru2": "lobby_close_welcome_ru2",

            # game
            "detect_radiant": "game_detect_radiant",
            "detect_dire": "game_detect_dire",
            "lock_in_ru": "game_lock_in_ru",
            "lock_in_eng": "game_lock_in_eng",
            "lock_in": "game_lock_in",
            "lock_in_disabled_ru": "game_lock_in_disabled_ru",
            "inventory": "game_inventory",
            "shop_search": "game_shop_search",

            # steam
            "avast_warrning": "steam_avast_warrning",
            "cancel_ru": "steam_cancel_ru",
            "continue_anyway_ru": "steam_continue_anyway_ru",
        }

        for alias, real_key in aliases.items():
            if alias not in out and real_key in out:
                out[alias] = out[real_key]

        loaded = sorted([k for k, v in out.items() if v is not None])
        missing_aliases = sorted([
            alias for alias, real_key in aliases.items()
            if alias not in out or out.get(alias) is None
        ])

        self.log.info(f"[MM] loaded templates count={len(loaded)}")
        self.log.info(f"[MM] loaded templates keys={loaded}")

        if missing_aliases:
            self.log.warning(f"[MM] missing alias templates={missing_aliases}")

        return out

    # ---------------------------------------------------------
    # controller callback / frame cache
    # ---------------------------------------------------------

    def on_command_result(
        self,
        *,
        vm_id: str,
        cmd_type: str,
        payload: Dict[str, Any],
        result: Dict[str, Any],
        status: str,
    ) -> None:
        """
        Этот метод ОБЯЗАТЕЛЬНО должен вызываться из controller.ack_command().

        Иначе capture_frame будет выполнен client'ом, но StartMmDota2 не узнает,
        что кадр пришёл, и будет бесконечно писать no frame.
        """
        self.mark_command_done(vm_id)

        if cmd_type != HostCommandType.CAPTURE_FRAME:
            return

        hwnd = payload.get("hwnd")
        if hwnd is None:
            return

        hwnd_i = int(hwnd)

        if status != "done":
            self._log_throttled(
                vm_id,
                hwnd_i,
                f"[MM] capture_frame failed vm={vm_id} hwnd={hwnd_i}: {result}",
                interval=2.0,
            )
            return

        frame = self._decode_capture_frame_result(result)
        if frame is None:
            self._log_throttled(
                vm_id,
                hwnd_i,
                f"[MM] capture_frame returned no image vm={vm_id} hwnd={hwnd_i} "
                f"keys={list(result.keys())}",
                interval=2.0,
            )
            return

        self._frame_cache[(vm_id, hwnd_i)] = frame

        state = self._vm.get(vm_id)
        if state is not None and hwnd_i in state.windows:
            state.windows[hwnd_i].latest_frame_ts = time.time()

    def _decode_capture_frame_result(self, result: Dict[str, Any]) -> Optional[np.ndarray]:
        image_b64 = (
            result.get("image_b64")
            or result.get("frame_b64")
            or result.get("png_b64")
            or result.get("jpg_b64")
            or result.get("jpeg_b64")
        )

        if not image_b64:
            return None

        try:
            raw = base64.b64decode(image_b64)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            arr = np.array(img, dtype=np.uint8)

            if arr.ndim != 3 or arr.shape[2] != 3:
                return None

            return arr
        except Exception as e:
            self.log.warning(f"[MM] failed to decode capture_frame: {e}")
            return None

    # ---------------------------------------------------------
    # bridge / vision
    # ---------------------------------------------------------

    def _get_entry(self, vm_id: str):
        return planner_runtime.get_entry(vm_id)

    def _get_latest_frame_rgb(self, vm_id: str, hwnd: int) -> Optional[np.ndarray]:
        hwnd_i = int(hwnd)

        cached = self._frame_cache.get((vm_id, hwnd_i))
        if cached is not None:
            return cached

        # Fallback на planner bridge, если он уже есть.
        entry = self._get_entry(vm_id)
        if entry is None:
            return None

        try:
            frame = entry.bridge.get_latest_frame(hwnd_i)
        except Exception:
            return None

        if frame is None:
            return None

        arr = getattr(frame, "image_rgb", None)
        if arr is None:
            arr = getattr(frame, "frame_rgb", None)
        if arr is None:
            arr = getattr(frame, "image", None)
        if arr is None:
            return None

        if not isinstance(arr, np.ndarray):
            return None
        if arr.ndim != 3 or arr.shape[2] != 3:
            return None
        if arr.dtype != np.uint8:
            try:
                arr = arr.astype(np.uint8)
            except Exception:
                return None

        self._frame_cache[(vm_id, hwnd_i)] = arr

        state = self._vm.get(vm_id)
        if state is not None and hwnd_i in state.windows:
            state.windows[hwnd_i].latest_frame_ts = time.time()

        return arr

    def _match(
        self,
        frame_rgb: np.ndarray,
        key: str,
        confidence: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        tpl = self._templates.get(key)

        if tpl is None:
            return None

        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None

        th, tw = tpl.shape[:2]
        fh, fw = gray.shape[:2]
        if fh < th or fw < tw:
            return None

        try:
            res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
        except Exception:
            return None

        score = float(max_val)
        threshold = self.confidence if confidence is None else float(confidence)

        if score < threshold:
            return None

        return {
            "x": int(max_loc[0] + tw / 2),
            "y": int(max_loc[1] + th / 2),
            "score": score,
            "w": int(tw),
            "h": int(th),
            "key": key,
        }

    def _find_any(
        self,
        frame_rgb: np.ndarray,
        keys: List[str],
        confidence: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        best = None
        for key in keys:
            hit = self._match(frame_rgb, key, confidence=confidence)
            if hit is None:
                continue
            if best is None or hit["score"] > best["score"]:
                best = hit
        return best

    def _find_accept_game(self, frame_rgb: np.ndarray) -> Optional[Dict[str, Any]]:
        return self._find_any(
            frame_rgb,
            [
                "accept_game",
                "lobby_accept_game",
                "game_accept_game",
                "accept_eng",
                "lobby_accept_eng",
            ],
            confidence=0.80,
        )

    # ---------------------------------------------------------
    # vm state
    # ---------------------------------------------------------

    def _ensure_vm(self, vm_id: str, hwnds: List[int]) -> VmMmState:
        state = self._vm.get(vm_id)
        if state is None:
            state = VmMmState(vm_id=vm_id)
            self._vm[vm_id] = state

        unique_hwnds: List[int] = []
        seen = set()
        for x in hwnds:
            hwnd_i = int(x)
            if hwnd_i in seen:
                continue
            seen.add(hwnd_i)
            unique_hwnds.append(hwnd_i)

        state.hwnds = unique_hwnds

        for hwnd in state.hwnds:
            if hwnd not in state.windows:
                state.windows[hwnd] = HwndState(hwnd=hwnd)

        stale = [hwnd for hwnd in state.windows if hwnd not in state.hwnds]
        for hwnd in stale:
            state.windows.pop(hwnd, None)
            self._frame_cache.pop((vm_id, hwnd), None)

        if state.stage == MmStage.IDLE:
            state.stage = MmStage.WAIT_DOTA_READY
            state.last_stage_ts = time.time()

        return state

    def mark_command_done(self, vm_id: str) -> None:
        state = self._vm.get(vm_id)
        if state:
            state.inflight = False
            state.last_action_ts = time.time()

    # ---------------------------------------------------------
    # low-level host->client helpers
    # ---------------------------------------------------------

    def _clear_frame(self, vm_id: str, hwnd: int) -> None:
        self._frame_cache.pop((vm_id, int(hwnd)), None)

    def _enqueue_focus(self, vm_id: str, hwnd: int) -> None:
        self.queue_command(
            vm_id,
            HostCommandType.FOCUS_WINDOW,
            {"hwnd": int(hwnd)},
        )

    def _enqueue_click(self, vm_id: str, hwnd: int, x: int, y: int) -> None:
        self._clear_frame(vm_id, hwnd)

        self.queue_command(
            vm_id,
            HostCommandType.MOUSE_CLICK,
            {
                "hwnd": int(hwnd),
                "x": int(x),
                "y": int(y),
                "coord_space": "client",
                "button": "left",
                "clicks": 1,
                "force_fg": True,
            },
        )

    def _enqueue_capture(self, vm_id: str, hwnd: int, purpose: str = "") -> bool:
        hwnd_i = int(hwnd)
        key = (vm_id, hwnd_i)
        now = time.time()

        last_ts = self._last_capture_request_ts.get(key, 0.0)
        if now - last_ts < 0.35:
            return False

        self._last_capture_request_ts[key] = now

        payload = {"hwnd": hwnd_i}
        if purpose:
            payload["purpose"] = purpose

        self.queue_command(
            vm_id,
            HostCommandType.CAPTURE_FRAME,
            payload,
        )

        self._log_throttled(
            vm_id,
            hwnd_i,
            f"[MM] request frame vm={vm_id} hwnd={hwnd_i} purpose={purpose}",
            interval=2.0,
        )

        return True

    def _enqueue_write(self, vm_id: str, hwnd: int, text: str, clear_before: bool = True) -> None:
        self._clear_frame(vm_id, hwnd)

        self.queue_command(
            vm_id,
            HostCommandType.WRITE_TEXT,
            {
                "hwnd": int(hwnd),
                "text": text,
                "clear_before": bool(clear_before),
            },
        )

    def _enqueue_key(self, vm_id: str, hwnd: int, vk_code: int, hold_ms: int = 25) -> None:
        self._clear_frame(vm_id, hwnd)

        self.queue_command(
            vm_id,
            HostCommandType.KEY_PRESS,
            {
                "hwnd": int(hwnd),
                "vk_code": int(vk_code),
                "hold_ms": int(hold_ms),
                "force_fg": True,
            },
        )

    def _enqueue_hotkey(self, vm_id: str, hwnd: int, keys: List[str]) -> None:
        self._clear_frame(vm_id, hwnd)

        self.queue_command(
            vm_id,
            HostCommandType.HOTKEY,
            {
                "hwnd": int(hwnd),
                "keys": [str(x) for x in keys],
                "force_fg": True,
            },
        )

    def _enqueue_sleep(self, vm_id: str, ms: int) -> None:
        self.queue_command(
            vm_id,
            HostCommandType.SLEEP,
            {"duration_ms": int(ms)},
        )

    # ---------------------------------------------------------
    # hero pick helpers
    # ---------------------------------------------------------

    @staticmethod
    def _normalize_pick_role(role: Optional[str]) -> Optional[str]:
        if not role:
            return None

        aliases = {
            "carry": "Carry",
            "offlane": "Offlane",
            "hard support": "Hard Support",
            "support": "Support",
            "mid": "Mid",
        }

        key = " ".join(str(role).strip().lower().replace("_", " ").split())
        return aliases.get(key)

    @staticmethod
    def _normalize_hero_name(hero_name: Optional[str]) -> str:
        return " ".join(str(hero_name or "").strip().lower().split())

    @staticmethod
    def _append_unique(values: List[Any], value: Any) -> None:
        if value not in values:
            values.append(value)

    @staticmethod
    def _remove_value(values: List[Any], value: Any) -> None:
        values[:] = [x for x in values if x != value]

    def _load_pick_role_pools(self) -> Dict[str, List[str]]:
        if self._pick_role_pools is not None:
            return self._pick_role_pools

        pools: Dict[str, List[str]] = {role: [] for role in PICK_ROLE_ORDER}
        self._hero_template_key_by_name = {}
        self._hero_display_name_by_norm = {}

        try:
            with open(DEFAULT_ID_HERO_TO_NAME, "r", encoding="utf-8") as f:
                id_to_name = json.load(f)
        except Exception as e:
            self.log.warning(
                f"[MM] failed to load id_hero_to_name from "
                f"{DEFAULT_ID_HERO_TO_NAME}: {e}"
            )
            self._pick_role_pools = pools
            return pools

        if not isinstance(id_to_name, dict):
            self.log.warning(
                f"[MM] {DEFAULT_ID_HERO_TO_NAME} must contain object id -> hero name"
            )
            self._pick_role_pools = pools
            return pools

        for hero_id, hero_name in id_to_name.items():
            hero_name_s = str(hero_name or "").strip()
            if not hero_name_s:
                continue

            template_key = f"heroes_{hero_id}"
            if self._templates.get(template_key) is None:
                continue

            norm_name = self._normalize_hero_name(hero_name_s)
            self._hero_template_key_by_name[norm_name] = template_key
            self._hero_display_name_by_norm[norm_name] = hero_name_s

        try:
            with open(OUT_FILE_HERO_BUILDS, "r", encoding="utf-8") as f:
                hero_builds = json.load(f)
        except Exception as e:
            self.log.warning(
                f"[MM] failed to load hero builds from {OUT_FILE_HERO_BUILDS}: {e}"
            )
            self._pick_role_pools = pools
            return pools

        if not isinstance(hero_builds, list):
            self.log.warning(f"[MM] {OUT_FILE_HERO_BUILDS} must contain JSON list")
            self._pick_role_pools = pools
            return pools

        skipped_without_template = 0

        for record in hero_builds:
            if not isinstance(record, dict):
                continue

            role = self._normalize_pick_role(record.get("role"))
            if role not in pools:
                continue

            hero_raw = (
                record.get("hero")
                or record.get("hero_name")
                or record.get("hero_slug")
            )
            norm_name = self._normalize_hero_name(hero_raw)
            hero_name = self._hero_display_name_by_norm.get(norm_name)

            if not hero_name:
                skipped_without_template += 1
                continue

            if hero_name not in pools[role]:
                pools[role].append(hero_name)

        for role, heroes in pools.items():
            self.log.info(f"[MM] pick pool role={role} heroes={len(heroes)}")

        if skipped_without_template:
            self.log.warning(
                f"[MM] skipped hero build records without hero png template: "
                f"{skipped_without_template}"
            )

        self._pick_role_pools = pools
        return pools

    def _reset_pick_state(self, state: VmMmState) -> None:
        state.pick_phase = "supports"
        state.pick_phase_started_ts = time.time()
        state.pick_wait_started_ts = 0.0
        state.pick_wait_seen_disabled_hwnds = []
        state.pick_candidate_pools_by_hwnd = {}
        state.pick_candidate_index_by_hwnd = {}
        state.pick_probe_index = 0
        state.pick_selected_hero_by_hwnd = {}
        state.pick_confirmed_ts_by_hwnd = {}
        state.pick_confirmed_hwnds = []
        state.pick_finalized_hwnds = []
        state.pick_banned_heroes = []

    def _ensure_pick_setup(self, state: VmMmState) -> bool:
        expected_role_by_hwnd: Dict[int, str] = {}

        for index, hwnd in enumerate(state.hwnds[:len(PICK_ROLE_ORDER)]):
            expected_role_by_hwnd[int(hwnd)] = PICK_ROLE_ORDER[index]

        if not expected_role_by_hwnd:
            return False

        expected_hwnd_by_role = {
            role: hwnd for hwnd, role in expected_role_by_hwnd.items()
        }

        if (
            state.pick_role_by_hwnd == expected_role_by_hwnd
            and state.pick_hwnd_by_role == expected_hwnd_by_role
            and state.pick_phase != "init"
        ):
            return True

        state.pick_role_by_hwnd = expected_role_by_hwnd
        state.pick_hwnd_by_role = expected_hwnd_by_role
        self._reset_pick_state(state)

        if len(state.hwnds) < len(PICK_ROLE_ORDER):
            self.log.warning(
                f"[MM] {state.vm_id}: only {len(state.hwnds)} hwnds for "
                f"{len(PICK_ROLE_ORDER)} pick roles"
            )

        role_text = ", ".join(
            f"{hex(hwnd)}={role}" for hwnd, role in state.pick_role_by_hwnd.items()
        )
        self.log.info(f"[MM] {state.vm_id}: pick roles assigned {role_text}")
        return True

    def _pick_hwnds_for_roles(self, state: VmMmState, roles: List[str]) -> List[int]:
        out: List[int] = []
        for role in roles:
            hwnd = state.pick_hwnd_by_role.get(role)
            if hwnd is not None:
                out.append(int(hwnd))
        return out

    def _pick_all_hwnds(self, state: VmMmState) -> List[int]:
        return self._pick_hwnds_for_roles(state, PICK_ROLE_ORDER)

    def _current_pick_phase_roles(self, state: VmMmState) -> List[str]:
        if state.pick_phase not in PICK_PHASE_ROLES:
            state.pick_phase = "supports"
            state.pick_phase_started_ts = time.time()
            state.pick_probe_index = 0

        return list(PICK_PHASE_ROLES[state.pick_phase])

    def _advance_pick_phase(self, state: VmMmState) -> bool:
        try:
            phase_index = PICK_PHASE_ORDER.index(state.pick_phase)
        except ValueError:
            phase_index = 0

        next_index = phase_index + 1
        if next_index >= len(PICK_PHASE_ORDER):
            state.heroes_picked = True
            self.log.info(f"[MM] {state.vm_id}: all heroes picked")
            return True

        state.pick_phase = PICK_PHASE_ORDER[next_index]
        state.pick_phase_started_ts = time.time()
        state.pick_probe_index = 0

        for hwnd in self._pick_hwnds_for_roles(
            state,
            PICK_PHASE_ROLES[state.pick_phase],
        ):
            self._clear_frame(state.vm_id, hwnd)

        self.log.info(
            f"[MM] {state.vm_id}: enter pick phase={state.pick_phase} "
            f"roles={PICK_PHASE_ROLES[state.pick_phase]}"
        )
        return False

    def _used_pick_heroes(
        self,
        state: VmMmState,
        *,
        exclude_hwnd: Optional[int] = None,
    ) -> set:
        used = set()
        exclude_hwnd_i = int(exclude_hwnd) if exclude_hwnd is not None else None

        for hwnd, hero_name in state.pick_selected_hero_by_hwnd.items():
            if exclude_hwnd_i is not None and int(hwnd) == exclude_hwnd_i:
                continue
            used.add(hero_name)

        return used

    def _next_pick_candidate(
        self,
        state: VmMmState,
        hwnd: int,
        role: str,
    ) -> Tuple[Optional[str], bool]:
        pools = self._load_pick_role_pools()

        if hwnd not in state.pick_candidate_pools_by_hwnd:
            candidates = list(pools.get(role, []))
            random.shuffle(candidates)
            state.pick_candidate_pools_by_hwnd[hwnd] = candidates
            state.pick_candidate_index_by_hwnd[hwnd] = 0

        candidates = state.pick_candidate_pools_by_hwnd.get(hwnd, [])
        banned = set(state.pick_banned_heroes)

        index = state.pick_candidate_index_by_hwnd.get(hwnd, 0)
        if index >= len(candidates):
            return None, True

        state.pick_candidate_index_by_hwnd[hwnd] = index + 1

        hero_name = candidates[index]
        if hero_name in banned:
            return None, False
        if hero_name in self._used_pick_heroes(state, exclude_hwnd=hwnd):
            return None, False

        return hero_name, False

    def _hero_template_key(self, hero_name: str) -> Optional[str]:
        return self._hero_template_key_by_name.get(
            self._normalize_hero_name(hero_name)
        )

    def _find_lock_in(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        return self._find_any(
            frame,
            ["lock_in_ru", "lock_in_eng", "lock_in"],
            confidence=0.80,
        )

    def _find_disabled_lock_in(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        return self._match(frame, "lock_in_disabled_ru", confidence=0.80)

    def _request_pick_refresh(
        self,
        state: VmMmState,
        hwnd: int,
        purpose: str,
    ) -> None:
        self._clear_frame(state.vm_id, hwnd)
        if self._enqueue_capture(state.vm_id, hwnd, purpose=purpose):
            state.inflight = True

    def _reset_pick_hwnd(
        self,
        state: VmMmState,
        hwnd: int,
        *,
        ban_selected: bool,
        reason: str,
    ) -> None:
        hero_name = state.pick_selected_hero_by_hwnd.pop(hwnd, None)
        state.pick_confirmed_ts_by_hwnd.pop(hwnd, None)
        self._remove_value(state.pick_confirmed_hwnds, hwnd)
        self._remove_value(state.pick_finalized_hwnds, hwnd)

        if ban_selected and hero_name:
            self._append_unique(state.pick_banned_heroes, hero_name)

        self._clear_frame(state.vm_id, hwnd)
        self.log.info(
            f"[MM] {state.vm_id}: reset pick hwnd={hex(hwnd)} "
            f"hero={hero_name} reason={reason}"
        )

    def _mark_pick_confirmed(
        self,
        state: VmMmState,
        hwnd: int,
        hero_name: str,
    ) -> None:
        self._append_unique(state.pick_confirmed_hwnds, hwnd)
        state.pick_selected_hero_by_hwnd[hwnd] = hero_name
        state.pick_confirmed_ts_by_hwnd[hwnd] = time.time()
        self._clear_frame(state.vm_id, hwnd)
        self.log.info(
            f"[MM] {state.vm_id}: pick confirmed hwnd={hex(hwnd)} "
            f"role={state.pick_role_by_hwnd.get(hwnd)} hero={hero_name}"
        )

    def _finalize_pick_hwnds(self, state: VmMmState, hwnds: List[int]) -> None:
        for hwnd in hwnds:
            if hwnd in state.pick_selected_hero_by_hwnd:
                self._append_unique(state.pick_finalized_hwnds, hwnd)

    def _tick_pick_single_hwnd(self, state: VmMmState, hwnd: int) -> bool:
        hwnd = int(hwnd)
        role = state.pick_role_by_hwnd.get(hwnd)

        if not role:
            return True

        if hwnd in state.pick_finalized_hwnds:
            return True

        frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
        if frame is None:
            if self._enqueue_capture(state.vm_id, hwnd, purpose="pick_heroes"):
                state.inflight = True
            return False

        selected_hero = state.pick_selected_hero_by_hwnd.get(hwnd)
        disabled_hit = self._find_disabled_lock_in(frame)

        if hwnd in state.pick_confirmed_hwnds:
            latest_ts = (
                state.windows.get(hwnd).latest_frame_ts
                if hwnd in state.windows else 0.0
            )
            confirmed_ts = state.pick_confirmed_ts_by_hwnd.get(hwnd, 0.0)
            if latest_ts < confirmed_ts or time.time() - latest_ts > 1.0:
                self._request_pick_refresh(state, hwnd, "pick_watch_after_inventory")
                return False

            if disabled_hit:
                self._reset_pick_hwnd(
                    state,
                    hwnd,
                    ban_selected=True,
                    reason="pick_rolled_back_after_inventory",
                )
                return False
            return True

        if selected_hero:
            inventory_hit = self._match(frame, "inventory", confidence=0.80)
            if inventory_hit:
                self._mark_pick_confirmed(state, hwnd, selected_hero)
                return True

            lock_hit = self._find_lock_in(frame)
            if lock_hit:
                self._enqueue_focus(state.vm_id, hwnd)
                self._enqueue_click(state.vm_id, hwnd, lock_hit["x"], lock_hit["y"])
                self._clear_frame(state.vm_id, hwnd)
                state.inflight = True
                self.log.info(
                    f"[MM] {state.vm_id}: clicked lock-in hwnd={hex(hwnd)} "
                    f"role={role} hero={selected_hero} by {lock_hit['key']}"
                )
                return False

            if disabled_hit:
                self._reset_pick_hwnd(
                    state,
                    hwnd,
                    ban_selected=True,
                    reason="pick_selection_did_not_enable_lock",
                )
                return False

            self._request_pick_refresh(state, hwnd, "pick_wait_lock_or_inventory")
            return False

        hero_name, exhausted = self._next_pick_candidate(state, hwnd, role)
        if not hero_name:
            if exhausted:
                self._log_throttled(
                    state.vm_id,
                    hwnd,
                    f"[MM] {state.vm_id}: no pick candidates left "
                    f"hwnd={hex(hwnd)} role={role}",
                    interval=5.0,
                )
                self._request_pick_refresh(state, hwnd, "pick_no_candidates")
                return False

            self._request_pick_refresh(state, hwnd, "pick_skip_candidate")
            return False

        template_key = self._hero_template_key(hero_name)
        if not template_key:
            self._request_pick_refresh(state, hwnd, "pick_skip_missing_template")
            return False

        hero_hit = self._match(frame, template_key, confidence=0.80)
        if not hero_hit:
            self._request_pick_refresh(state, hwnd, "pick_candidate_not_visible")
            return False

        state.pick_selected_hero_by_hwnd[hwnd] = hero_name
        self._enqueue_focus(state.vm_id, hwnd)
        self._enqueue_click(state.vm_id, hwnd, hero_hit["x"], hero_hit["y"])
        self._clear_frame(state.vm_id, hwnd)
        state.inflight = True
        self.log.info(
            f"[MM] {state.vm_id}: selected hero hwnd={hex(hwnd)} "
            f"role={role} hero={hero_name} by {template_key}"
        )
        return False

    def _begin_pick_wait(
        self,
        state: VmMmState,
        phase: str,
        active_roles: List[str],
        next_roles: List[str],
    ) -> None:
        state.pick_phase = phase
        state.pick_phase_started_ts = time.time()
        state.pick_wait_started_ts = state.pick_phase_started_ts
        state.pick_wait_seen_disabled_hwnds = []

        for hwnd in (
            self._pick_hwnds_for_roles(state, active_roles)
            + self._pick_hwnds_for_roles(state, next_roles)
        ):
            self._clear_frame(state.vm_id, hwnd)

        self.log.info(
            f"[MM] {state.vm_id}: wait pick phase={phase} "
            f"next_roles={next_roles}"
        )

    def _enter_pick_phase(
        self,
        state: VmMmState,
        phase: str,
        active_roles: List[str],
    ) -> None:
        state.pick_phase = phase
        state.pick_phase_started_ts = time.time()
        state.pick_wait_started_ts = 0.0
        state.pick_wait_seen_disabled_hwnds = []

        for hwnd in self._pick_hwnds_for_roles(state, active_roles):
            self._clear_frame(state.vm_id, hwnd)

        self.log.info(
            f"[MM] {state.vm_id}: enter pick phase={phase} roles={active_roles}"
        )

    def _tick_pick_phase(self, state: VmMmState, roles: List[str]) -> bool:
        active_hwnds = self._pick_hwnds_for_roles(state, roles)
        if not active_hwnds:
            return True

        for hwnd in active_hwnds:
            if not self._tick_pick_single_hwnd(state, hwnd):
                return False
            if state.inflight:
                return False

        return True

    def _tick_pick_current_turn(self, state: VmMmState) -> bool:
        phase_roles = self._current_pick_phase_roles(state)
        phase_hwnds = self._pick_hwnds_for_roles(state, phase_roles)
        if not phase_hwnds:
            return False

        pending_hwnds = [
            hwnd for hwnd in phase_hwnds
            if hwnd not in state.pick_finalized_hwnds
        ]

        if not pending_hwnds:
            self._advance_pick_phase(state)
            return False

        waiting_selected_hwnds: List[int] = []

        for hwnd in pending_hwnds:
            if hwnd in state.pick_confirmed_hwnds:
                if self._tick_pick_single_hwnd(state, hwnd):
                    self._append_unique(state.pick_finalized_hwnds, hwnd)
                return False

            if hwnd not in state.pick_selected_hero_by_hwnd:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                self._request_pick_refresh(state, hwnd, "pick_refresh_selected")
                return False

            if (
                self._match(frame, "inventory", confidence=0.80)
                or self._find_lock_in(frame)
                or self._find_disabled_lock_in(frame)
            ):
                if self._tick_pick_single_hwnd(state, hwnd):
                    if hwnd in state.pick_confirmed_hwnds:
                        self._append_unique(state.pick_finalized_hwnds, hwnd)
                return False

            waiting_selected_hwnds.append(hwnd)

        ready_hwnds: List[int] = []
        missing_hwnds: List[int] = []

        for hwnd in pending_hwnds:
            if (
                hwnd in state.pick_selected_hero_by_hwnd
                or hwnd in state.pick_confirmed_hwnds
            ):
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                missing_hwnds.append(hwnd)
                continue

            if self._find_disabled_lock_in(frame):
                ready_hwnds.append(hwnd)

        if ready_hwnds:
            hwnd = ready_hwnds[0]
            self._log_throttled(
                state.vm_id,
                hwnd,
                f"[MM] {state.vm_id}: current pick "
                f"phase={state.pick_phase} hwnd={hex(hwnd)} "
                f"role={state.pick_role_by_hwnd.get(hwnd)}",
                interval=2.0,
            )
            self._tick_pick_single_hwnd(state, hwnd)
            return False

        probe_hwnds = missing_hwnds or waiting_selected_hwnds or pending_hwnds
        target = probe_hwnds[state.pick_probe_index % len(probe_hwnds)]
        state.pick_probe_index += 1

        if missing_hwnds:
            purpose = f"pick_probe_{state.pick_phase}"
        elif waiting_selected_hwnds:
            purpose = f"pick_wait_{state.pick_phase}_lock_or_inventory"
        else:
            purpose = f"pick_wait_{state.pick_phase}"

        self._request_pick_refresh(state, target, purpose)
        return False

    def _tick_wait_for_next_pick_phase(
        self,
        state: VmMmState,
        *,
        rollback_roles: List[str],
        retry_phase: str,
        next_roles: List[str],
        next_phase: str,
    ) -> bool:
        rollback_hwnds = self._pick_hwnds_for_roles(state, rollback_roles)
        next_hwnds = self._pick_hwnds_for_roles(state, next_roles)
        now = time.time()

        for hwnd in rollback_hwnds:
            if hwnd in state.pick_finalized_hwnds:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            latest_ts = (
                state.windows.get(hwnd).latest_frame_ts
                if hwnd in state.windows else 0.0
            )
            if frame is None or latest_ts < state.pick_wait_started_ts:
                self._request_pick_refresh(state, hwnd, f"{retry_phase}_rollback_watch")
                return False

            if self._find_disabled_lock_in(frame):
                self._reset_pick_hwnd(
                    state,
                    hwnd,
                    ban_selected=True,
                    reason=f"{retry_phase}_rolled_back_while_waiting",
                )
                self._enter_pick_phase(state, retry_phase, rollback_roles)
                return False

        if not next_hwnds:
            self._finalize_pick_hwnds(state, rollback_hwnds)
            self._enter_pick_phase(state, next_phase, next_roles)
            return True

        for hwnd in next_hwnds:
            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            latest_ts = (
                state.windows.get(hwnd).latest_frame_ts
                if hwnd in state.windows else 0.0
            )
            if frame is None or latest_ts < state.pick_wait_started_ts:
                self._request_pick_refresh(state, hwnd, f"{next_phase}_wait_enabled")
                return False

            if self._find_disabled_lock_in(frame):
                self._append_unique(state.pick_wait_seen_disabled_hwnds, hwnd)
                self._request_pick_refresh(state, hwnd, f"{next_phase}_disabled")
                return False

        seen_all_disabled = all(
            hwnd in state.pick_wait_seen_disabled_hwnds for hwnd in next_hwnds
        )
        if not seen_all_disabled and now - state.pick_wait_started_ts < 3.0:
            self._request_pick_refresh(
                state,
                next_hwnds[0],
                f"{next_phase}_wait_disabled_seen",
            )
            return False

        self._finalize_pick_hwnds(state, rollback_hwnds)
        self._enter_pick_phase(state, next_phase, next_roles)
        return True

    def _tick_wait_final_pick_confirmation(
        self,
        state: VmMmState,
        *,
        rollback_roles: List[str],
        retry_phase: str,
        wait_seconds: float = 3.0,
    ) -> bool:
        rollback_hwnds = self._pick_hwnds_for_roles(state, rollback_roles)
        now = time.time()

        for hwnd in rollback_hwnds:
            if hwnd in state.pick_finalized_hwnds:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            latest_ts = (
                state.windows.get(hwnd).latest_frame_ts
                if hwnd in state.windows else 0.0
            )
            if frame is None or latest_ts < state.pick_wait_started_ts:
                self._request_pick_refresh(state, hwnd, f"{retry_phase}_final_watch")
                return False

            if self._find_disabled_lock_in(frame):
                self._reset_pick_hwnd(
                    state,
                    hwnd,
                    ban_selected=True,
                    reason=f"{retry_phase}_rolled_back_before_done",
                )
                self._enter_pick_phase(state, retry_phase, rollback_roles)
                return False

        if rollback_hwnds and now - state.pick_wait_started_ts < wait_seconds:
            self._request_pick_refresh(
                state,
                rollback_hwnds[0],
                f"{retry_phase}_final_wait",
            )
            return False

        self._finalize_pick_hwnds(state, rollback_hwnds)
        state.heroes_picked = True
        self.log.info(f"[MM] {state.vm_id}: all heroes picked")
        return True

    # ---------------------------------------------------------
    # stages
    # ---------------------------------------------------------

    def _tick_close_first_run_popup(self, state: VmMmState, hwnd: int) -> bool:
        frame = self._get_latest_frame_rgb(state.vm_id, hwnd)


        if frame is None:
            if self._enqueue_capture(state.vm_id, hwnd, purpose="close_first_run_popup"):
                state.inflight = True
                return True
            return False

        hit = self._find_any(
            frame,
            [
                "close_welcome_ru",
                "close_welcome_ru2",
                "accept_reward_ru"
            ],
            confidence=0.87,
        )

        if hit is None:
            return False

        self._enqueue_focus(state.vm_id, hwnd)
        self._enqueue_click(state.vm_id, hwnd, hit["x"], hit["y"])
        self._enqueue_sleep(state.vm_id, 300)

        state.inflight = True
        self.log.info(
            f"[MM] {state.vm_id}: closed first-run popup on hwnd={hex(hwnd)} by {hit['key']}"
        )
        return True

    def _tick_wait_dota_ready(self, state: VmMmState) -> bool:
        all_ready = True

        for hwnd in state.hwnds:
            w = state.windows[hwnd]

            if w.dota_ready:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                if self._enqueue_capture(state.vm_id, hwnd, purpose="wait_dota_ready"):
                    state.inflight = True
                return False

            # 1) Сначала закрываем welcome/first-run окна.
            # Даже если dota.png уже виден на фоне, окно ещё НЕ готово.
            popup_closed = self._tick_close_first_run_popup(state, hwnd)
            if popup_closed:
                return False

            # 2) Только после отсутствия popup ищем dota.png.
            hit_dota = self._match(frame, "dota", confidence=0.75)
            if hit_dota is not None:
                w.dota_ready = True

                hit_self = self._match(frame, "self", confidence=0.75)
                if hit_self is not None:
                    w.self_found = True

                self.log.info(
                    f"[MM] {state.vm_id}: dota window ready hwnd={hex(hwnd)} "
                    f"self_found={w.self_found}"
                )
                continue

            # 3) Не нашли ни popup, ни dota — берём новый кадр.
            if self._enqueue_capture(state.vm_id, hwnd, purpose="wait_dota_ready_refresh"):
                state.inflight = True

            all_ready = False
            return False

        if not all_ready:
            return False

        state.stage = MmStage.BUILD_PARTY
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: all dota windows are ready")
        return False

    def _tick_build_party(
        self,
        state: VmMmState,
        *,
        friend_ids: Optional[List[Optional[str]]],
    ) -> bool:
        if state.party_built:
            state.stage = MmStage.START_GAME
            state.last_stage_ts = time.time()
            return False

        if not state.hwnds:
            return False

        leader = state.hwnds[0]
        frame = self._get_latest_frame_rgb(state.vm_id, leader)

        if frame is None:
            if self._enqueue_capture(state.vm_id, leader, purpose="build_party_leader"):
                state.inflight = True
            return False

        if not state.party_add_done:
            between_invites_sleep_ms = 2500

            if state.party_return_to_dota_pending:
                dota_hit = self._match(frame, "dota", confidence=0.75)
                if dota_hit:
                    self._enqueue_focus(state.vm_id, leader)
                    self._enqueue_click(state.vm_id, leader, dota_hit["x"], dota_hit["y"])
                    self._enqueue_sleep(state.vm_id, between_invites_sleep_ms)

                    state.party_return_to_dota_pending = False
                    state.party_search_clicked = False

                    if state.party_retry_invite_active:
                        state.party_retry_invite_active = False
                        state.party_add_done = True
                        state.party_invite_index = len(state.hwnds)
                    else:
                        state.party_invite_index += 1

                        if state.party_invite_index >= len(state.hwnds):
                            state.party_add_done = True
                        else:
                            state.party_search_done = False

                    state.inflight = True

                    if self._enqueue_capture(state.vm_id, leader, purpose="build_party_after_dota"):
                        state.inflight = True

                    self.log.info(
                        f"[MM] {state.vm_id}: returned to dota after party invite "
                        f"next_index={state.party_invite_index}"
                    )
                    return False

                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_find_dota"):
                    state.inflight = True
                return False

            if state.party_invite_index >= len(state.hwnds):
                state.party_add_done = True
                return False

            current_friend_id = None
            if friend_ids and state.party_invite_index < len(friend_ids):
                current_friend_id = friend_ids[state.party_invite_index]

            if not current_friend_id:
                self.log.warning(
                    f"[MM] {state.vm_id}: no friend_id for party invite "
                    f"index={state.party_invite_index}, skip invite"
                )
                state.party_invite_index += 1
                state.party_search_clicked = False
                state.party_search_done = False

                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_skip_missing_friend_id"):
                    state.inflight = True
                return False

            current_friend_id = str(current_friend_id)

            if not state.party_search_done:
                hit_add_party = self._match(frame, "add_party")
                if hit_add_party:
                    self._enqueue_focus(state.vm_id, leader)
                    self._enqueue_click(state.vm_id, leader, hit_add_party["x"], hit_add_party["y"])
                    state.inflight = True
                    state.party_search_done = True
                    return False

                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_find_add_party"):
                    state.inflight = True
                return False

            if not state.party_search_clicked:
                self._enqueue_focus(state.vm_id, leader)
                state.inflight = True

                field_hit = self._find_any(frame, ["id_field_ru", "id_field_eng"])
                if field_hit:
                    self._enqueue_click(state.vm_id, leader, field_hit["x"], field_hit["y"])
                    self._enqueue_sleep(state.vm_id, 1000)
                    self._enqueue_write(state.vm_id, leader, current_friend_id, clear_before=True)

                    search_hit = self._find_any(frame, ["search_ru", "search_eng"])
                    if search_hit:
                        self._enqueue_click(state.vm_id, leader, search_hit["x"], search_hit["y"])
                        self._enqueue_sleep(state.vm_id, between_invites_sleep_ms)
                        state.party_search_clicked = True
                        if self._enqueue_capture(state.vm_id, leader, purpose="build_party_after_search"):
                            state.inflight = True
                        return False

                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_find_field"):
                    state.inflight = True
                return False

            add_hit = self._match(frame, "add")
            if add_hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, add_hit["x"], add_hit["y"])
                self._enqueue_sleep(state.vm_id, between_invites_sleep_ms)

                if 0 <= state.party_invite_index < len(state.hwnds):
                    invite_hwnd = state.hwnds[state.party_invite_index]
                    state.windows[invite_hwnd].invite_sent_ts = time.time()
                    state.windows[invite_hwnd].invite_accepted = False

                if state.party_invite_index not in state.party_invited_indices:
                    state.party_invited_indices.append(state.party_invite_index)

                state.party_return_to_dota_pending = True
                state.inflight = True
                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_after_add"):
                    state.inflight = True
                self.log.info(
                    f"[MM] {state.vm_id}: sent party invite "
                    f"index={state.party_invite_index} friend_id={current_friend_id}"
                )
                return False

            invite_pop_hit = self._match(frame, "invite_pop")
            if invite_pop_hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(
                    state.vm_id,
                    leader,
                    invite_pop_hit["x"],
                    invite_pop_hit["y"],
                )
                self._enqueue_sleep(state.vm_id, 1000)
                state.inflight = True

                if self._enqueue_capture(state.vm_id, leader, purpose="build_party_after_invite_pop"):
                    state.inflight = True

                self.log.info(
                    f"[MM] {state.vm_id}: add button not found, clicked invite_pop "
                    f"index={state.party_invite_index} friend_id={current_friend_id}"
                )
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="build_party_find_add"):
                state.inflight = True
            return False

        if not state.party_accept_done:
            member_indices = [
                i for i in state.party_invited_indices if 0 <= i < len(state.hwnds)
            ]
            if not member_indices:
                state.party_accept_done = True
            else:
                all_accepted = True

                for member_index in member_indices:
                    hwnd = state.hwnds[member_index]
                    w = state.windows[hwnd]

                    if w.invite_accepted:
                        continue

                    all_accepted = False
                    frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
                    if frame is None or (
                        w.invite_sent_ts > 0 and w.latest_frame_ts < w.invite_sent_ts
                    ):
                        if self._enqueue_capture(state.vm_id, hwnd, purpose="party_accept_invite"):
                            state.inflight = True
                        continue

                    hit = self._find_any(frame, ["accept_invite_ru", "accept_invite_eng"])
                    if hit:
                        self._enqueue_focus(state.vm_id, hwnd)
                        self._enqueue_click(state.vm_id, hwnd, hit["x"], hit["y"])
                        state.windows[hwnd].invite_accepted = True
                        state.inflight = True
                        return False

                    now = time.time()
                    if (
                        w.invite_sent_ts > 0
                        and now - w.invite_sent_ts >= MM_PARTY_INVITE_TIMEOUT_SEC
                    ):
                        self.log.warning(
                            f"[MM] {state.vm_id}: party invite timeout "
                            f"index={member_index} hwnd={hex(hwnd)} "
                            f"timeout={MM_PARTY_INVITE_TIMEOUT_SEC:.1f}s, retry invite"
                        )
                        state.party_invite_index = member_index
                        state.party_search_done = False
                        state.party_search_clicked = False
                        state.party_add_done = False
                        state.party_return_to_dota_pending = False
                        state.party_retry_invite_active = True
                        self._clear_frame(state.vm_id, leader)
                        return False

                    if self._enqueue_capture(state.vm_id, hwnd, purpose="party_accept_invite"):
                        state.inflight = True

                if all_accepted:
                    state.party_accept_done = True

        if not state.party_accept_done:
            return False

        state.party_built = True
        state.stage = MmStage.START_GAME
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: party stage done")
        return False

    def _tick_wait_accept_game(self, state: VmMmState) -> bool:
        if not state.hwnds:
            return False

        leader = state.hwnds[0]
        leader_state = state.windows[leader]

        if not leader_state.game_accepted:
            frame = self._get_latest_frame_rgb(state.vm_id, leader)
            if frame is None:
                if self._enqueue_capture(state.vm_id, leader, purpose="wait_accept_game_leader"):
                    state.inflight = True
                return False

            hit = self._find_accept_game(frame)
            if hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"])
                leader_state.game_accepted = True
                state.inflight = True
                self.log.info(
                    f"[MM] {state.vm_id}: accepted game on leader hwnd={hex(leader)} "
                    f"by {hit['key']}"
                )
                return False

            self._log_throttled(
                state.vm_id,
                leader,
                f"[MM] {state.vm_id}: accept_game not found yet on leader hwnd={hex(leader)}",
                interval=2.0,
            )
            if self._enqueue_capture(state.vm_id, leader, purpose="wait_accept_game_leader_refresh"):
                state.inflight = True
            return False

        for hwnd in state.hwnds[1:]:
            w = state.windows[hwnd]
            if w.game_accepted:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                if self._enqueue_capture(state.vm_id, hwnd, purpose="wait_accept_game_member"):
                    state.inflight = True
                return False

            hit = self._find_accept_game(frame)
            if hit:
                self._enqueue_focus(state.vm_id, hwnd)
                self._enqueue_click(state.vm_id, hwnd, hit["x"], hit["y"])
                w.game_accepted = True
                state.inflight = True
                self.log.info(
                    f"[MM] {state.vm_id}: accepted game on hwnd={hex(hwnd)} by {hit['key']}"
                )
                return False

            self._log_throttled(
                state.vm_id,
                hwnd,
                f"[MM] {state.vm_id}: accept_game not found yet hwnd={hex(hwnd)}",
                interval=2.0,
            )
            if self._enqueue_capture(state.vm_id, hwnd, purpose="wait_accept_game_member_refresh"):
                state.inflight = True
            return False

        state.stage = MmStage.DETECT_SIDE
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: wait_accept_game stage done")
        return False

    def _tick_detect_side(self, state: VmMmState) -> bool:
        if state.side in ("radiant", "dire"):
            state.stage = MmStage.PICK_HEROES
            state.last_stage_ts = time.time()
            self.log.info(f"[MM] {state.vm_id}: detect_side stage done side={state.side}")
            return False

        if not state.hwnds:
            return False

        hwnd = state.hwnds[0]
        w = state.windows[hwnd]

        frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
        if frame is None:
            if self._enqueue_capture(state.vm_id, hwnd, purpose="detect_side"):
                state.inflight = True
            return False

        hit_r = self._match(frame, "detect_radiant")
        if hit_r:
            state.side = "radiant"

        if state.side is None:
            hit_d = self._match(frame, "detect_dire")
            if hit_d:
                state.side = "dire"

        if state.side is None:
            self._log_throttled(
                state.vm_id,
                hwnd,
                f"[MM] {state.vm_id}: side not detected yet hwnd={hex(hwnd)}",
                interval=2.0,
            )
            if self._enqueue_capture(state.vm_id, hwnd, purpose="detect_side_refresh"):
                state.inflight = True
            return False

        for hwnd_i in state.hwnds:
            if hwnd_i in state.windows:
                state.windows[hwnd_i].side = state.side

        w.side = state.side
        self.log.info(f"[MM] {state.vm_id}: hwnd={hex(hwnd)} side={state.side}")

        state.stage = MmStage.PICK_HEROES
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: detect_side stage done side={state.side}")
        return False

    def _tick_start_game_stub(self, state: VmMmState) -> bool:
        if state.start_game_done:
            state.stage = MmStage.WAIT_ACCEPT_GAME
            state.last_stage_ts = time.time()
            return False

        leader = state.hwnds[0]
        frame = self._get_latest_frame_rgb(state.vm_id, leader)

        if frame is None:
            if self._enqueue_capture(state.vm_id, leader, purpose="start_game_stub"):
                state.inflight = True
            return False

        # step 0: Play
        if state.start_game_step == 0:
            hit = self._find_any(frame, ["play_ru", "play_eng"], confidence=0.80)
            if hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"])
                state.inflight = True
                state.start_game_step = 1
                self._clear_frame(state.vm_id, leader)
                self.log.info(f"[MM] {state.vm_id}: clicked play by {hit['key']}")
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="start_game_find_play"):
                state.inflight = True
            return False

        # step 1: Public / normal game section
        if state.start_game_step == 1:
            hit = self._find_any(frame, ["public_ru", "public_eng"], confidence=0.80)
            if hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"])
                state.inflight = True
                state.start_game_step = 2
                self._clear_frame(state.vm_id, leader)
                self.log.info(f"[MM] {state.vm_id}: clicked public by {hit['key']}")
                return False

            # если public уже выбран/не нужен, через пару секунд идём дальше
            if time.time() - state.last_stage_ts > 2.0:
                state.start_game_step = 2
                self._clear_frame(state.vm_id, leader)
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="start_game_find_public"):
                state.inflight = True
            return False

        # step 2: All Pick
        if state.start_game_step == 2:
            hit = self._match(
                frame,"unselected_all_pick",
            )
            if hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"] )
                state.inflight = True
                state.start_game_step = 3
                self._clear_frame(state.vm_id, leader)
                self.log.info(f"[MM] {state.vm_id}: clicked all pick by {hit['key']}")
                return False

            # если режим уже выбран, идём дальше
            if time.time() - state.last_stage_ts > 4.0:
                state.start_game_step = 3
                self._clear_frame(state.vm_id, leader)
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="start_game_find_all_pick"):
                state.inflight = True
            return False

        # step 3: Search game / Find match
        if state.start_game_step == 3:
            hit = self._find_any(
                frame,
                [
                    "search_game_ru",
                    "search_game_eng",
                ],
                confidence=0.80,
            )
            if hit:
                self._enqueue_focus(state.vm_id, leader)
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"])
                state.inflight = True
                state.start_game_done = True
                self._clear_frame(state.vm_id, leader)
                self.log.info(f"[MM] {state.vm_id}: clicked search game by {hit['key']}")
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="start_game_find_search"):
                state.inflight = True
            return False

        state.start_game_done = True
        self.log.info(f"[MM] {state.vm_id}: start_game_stub done")
        return False

    def _tick_pick_heroes_stub(self, state: VmMmState) -> bool:
        if state.heroes_picked:
            state.stage = MmStage.DONE
            state.last_stage_ts = time.time()
            self.log.info(f"[MM] {state.vm_id}: pick_heroes_stub done")
            return True

        if not self._ensure_pick_setup(state):
            return False

        self._load_pick_role_pools()
        return self._tick_pick_current_turn(state)

    # ---------------------------------------------------------
    # public tick
    # ---------------------------------------------------------

    def tick_one(
        self,
        vm_id: str,
        hwnds: List[int],
        *,
        friend_ids: Optional[List[Optional[str]]] = None,
    ) -> bool:
        """
        Returns True when host-side pre-planner MM stage is fully done.
        """

        if not hwnds:
            return False

        state = self._ensure_vm(vm_id, hwnds)

        if state.inflight:
            return False

        if state.stage == MmStage.WAIT_DOTA_READY:
            state.stage = MmStage.WAIT_ACCEPT_GAME
            return False
            return self._tick_wait_dota_ready(state)

        if state.stage == MmStage.BUILD_PARTY:
            return self._tick_build_party(state, friend_ids=friend_ids)

        if state.stage == MmStage.START_GAME:
            return self._tick_start_game_stub(state)

        if state.stage == MmStage.WAIT_ACCEPT_GAME:
            return self._tick_wait_accept_game(state)

        if state.stage == MmStage.DETECT_SIDE:
            return self._tick_detect_side(state)

        if state.stage == MmStage.PICK_HEROES:
            return self._tick_pick_heroes_stub(state)

        if state.stage == MmStage.DONE:
            return True

        return False

    # ---------------------------------------------------------
    # helpers for controller/ui
    # ---------------------------------------------------------

    def get_stage(self, vm_id: str) -> str:
        state = self._vm.get(vm_id)
        if state is None:
            return MmStage.IDLE
        return state.stage

    def get_sides(self, vm_id: str) -> Dict[int, str]:
        state = self._vm.get(vm_id)
        if state is None:
            return {}
        if state.side in ("radiant", "dire"):
            return {hwnd: state.side for hwnd in state.hwnds}
        return {hwnd: w.side for hwnd, w in state.windows.items() if w.side}

    def get_side(self, vm_id: str) -> Optional[str]:
        state = self._vm.get(vm_id)
        if state is None:
            return None
        if state.side in ("radiant", "dire"):
            return state.side
        for w in state.windows.values():
            if w.side in ("radiant", "dire"):
                return w.side
        return None

    # ---------------------------------------------------------
    # logging
    # ---------------------------------------------------------

    def _log_throttled(
        self,
        vm_id: str,
        hwnd: int,
        message: str,
        interval: float = 2.0,
    ) -> None:
        state = self._vm.get(vm_id)
        if state is None:
            self.log.info(message)
            return

        w = state.windows.get(int(hwnd))
        if w is None:
            self.log.info(message)
            return

        now = time.time()
        if now - w.last_log_ts >= interval:
            w.last_log_ts = now
            self.log.info(message)
