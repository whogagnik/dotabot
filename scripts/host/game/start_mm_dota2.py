# scripts/host/game/start_mm_dota2.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

import cv2
import numpy as np
from PIL import Image

from scripts.host.game.planner_runtime import planner_runtime


class MmStage:
    IDLE = "idle"
    WAIT_DOTA_READY = "wait_dota_ready"
    BUILD_PARTY = "build_party"
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
    self_found: bool = False
    last_log_ts: float = 0.0


@dataclass
class VmMmState:
    vm_id: str
    stage: str = MmStage.IDLE
    hwnds: List[int] = field(default_factory=list)
    windows: Dict[int, HwndState] = field(default_factory=dict)

    party_built: bool = False
    start_game_done: bool = False
    heroes_picked: bool = False

    last_stage_ts: float = 0.0
    last_action_ts: float = 0.0
    inflight: bool = False

    party_search_done: bool = False
    party_search_clicked: bool = False
    party_add_done: bool = False
    party_accept_done: bool = False


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

        start_game_step: int = 0
        self._vm: Dict[str, VmMmState] = {}
        self._templates = self._load_templates()

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
            "add": "lobby_add",

            "accept_invite_ru": "lobby_accept_invite_ru",
            "accept_invite_eng": "lobby_accept_invite_eng",
            "accept_reward_ru": "lobby_accept_reward_ru",
            "play_eng": "lobby_play_eng",
            "play_ru": "lobby_play_ru",
            'public_ru': 'lobby_public_ru',
            'selected_all_pick': 'lobby_selected_all_pick',
            'unselected_all_pick': 'lobby_unselected_all_pick',
            'hit_search_game' : 'lobby_hit_search_game',
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
            "lock_in": "game_lock_in",
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
            state.stage = MmStage.DETECT_SIDE
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

        if not state.party_add_done:
            field_hit = self._find_any(frame, ["id_field_ru", "id_field_eng"])
            if field_hit and friend_ids and len(friend_ids) > 1 and friend_ids[1]:
                self._enqueue_focus(state.vm_id, leader)

                if not state.party_search_clicked:
                    self._enqueue_click(state.vm_id, leader, field_hit["x"], field_hit["y"])
                    time.sleep(1)
                    self._enqueue_write(state.vm_id, leader, str(friend_ids[1]), clear_before=True)

                    search_hit = self._find_any(frame, ["search_ru", "search_eng"])
                    if search_hit:
                        self._enqueue_click(state.vm_id, leader, search_hit["x"], search_hit["y"])
                        state.party_search_clicked = True
                        if self._enqueue_capture(state.vm_id, leader, purpose="build_party_after_search"):
                            state.inflight = True
                        return False

                add_hit = self._match(frame, "add")
                if add_hit:
                    self._enqueue_click(state.vm_id, leader, add_hit["x"], add_hit["y"])
                    state.inflight = True
                    state.party_add_done = True
                    return False

            # Если friend_id нет — пока считаем party_add_done, чтобы не зависать.
            if not friend_ids or len(friend_ids) <= 1 or not friend_ids[1]:
                self.log.warning(f"[MM] {state.vm_id}: no friend_id for party invite, skip invite")
                state.party_add_done = True
                return False

            if self._enqueue_capture(state.vm_id, leader, purpose="build_party_find_field"):
                state.inflight = True
            return False

        if not state.party_accept_done:
            accepted_any = False

            for hwnd in state.hwnds[1:]:
                frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
                if frame is None:
                    if self._enqueue_capture(state.vm_id, hwnd, purpose="party_accept_invite"):
                        state.inflight = True
                    return False

                hit = self._find_any(frame, ["accept_invite_ru", "accept_invite_eng"])
                if hit:
                    self._enqueue_focus(state.vm_id, hwnd)
                    self._enqueue_click(state.vm_id, hwnd, hit["x"], hit["y"])
                    state.windows[hwnd].invite_accepted = True
                    accepted_any = True
                    state.inflight = True
                    return False

            if not accepted_any:
                state.party_accept_done = True

        state.party_built = True
        state.stage = MmStage.DETECT_SIDE
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: party stage done")
        return False

    def _tick_detect_side(self, state: VmMmState) -> bool:
        unresolved = []

        for hwnd in state.hwnds:
            w = state.windows[hwnd]
            if w.side:
                continue

            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                if self._enqueue_capture(state.vm_id, hwnd, purpose="detect_side"):
                    state.inflight = True
                return False

            hit_r = self._match(frame, "detect_radiant")
            if hit_r:
                w.side = "radiant"
                self.log.info(f"[MM] {state.vm_id}: hwnd={hex(hwnd)} side=radiant")
                continue

            hit_d = self._match(frame, "detect_dire")
            if hit_d:
                w.side = "dire"
                self.log.info(f"[MM] {state.vm_id}: hwnd={hex(hwnd)} side=dire")
                continue

            unresolved.append(hwnd)

        if unresolved:
            if time.time() - state.last_stage_ts < 5.0:
                hwnd = unresolved[0]
                if self._enqueue_capture(state.vm_id, hwnd, purpose="detect_side_refresh"):
                    state.inflight = True
                return False

            for hwnd in unresolved:
                state.windows[hwnd].side = "unknown"
                self.log.info(f"[MM] {state.vm_id}: hwnd={hex(hwnd)} side=unknown")

        state.stage = MmStage.START_GAME
        state.last_stage_ts = time.time()
        self.log.info(f"[MM] {state.vm_id}: detect_side stage done")
        return False

    def _tick_start_game_stub(self, state: VmMmState) -> bool:
        if state.start_game_done:
            state.stage = MmStage.PICK_HEROES
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
                self._enqueue_click(state.vm_id, leader, hit["x"], hit["y"])
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

        for hwnd in state.hwnds:
            frame = self._get_latest_frame_rgb(state.vm_id, hwnd)
            if frame is None:
                if self._enqueue_capture(state.vm_id, hwnd, purpose="pick_heroes_stub"):
                    state.inflight = True
                return False

        state.heroes_picked = True
        return False

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
            return self._tick_wait_dota_ready(state)

        if state.stage == MmStage.BUILD_PARTY:
            return self._tick_build_party(state, friend_ids=friend_ids)

        if state.stage == MmStage.DETECT_SIDE:
            return self._tick_detect_side(state)

        if state.stage == MmStage.START_GAME:
            return self._tick_start_game_stub(state)

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
        return {hwnd: w.side for hwnd, w in state.windows.items() if w.side}

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