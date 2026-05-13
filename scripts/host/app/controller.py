# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any

import cv2
import numpy as np
from PIL import Image

from scripts.host.core.account import Account
from scripts.host.core.config import *
from scripts.host.game.planner_runtime import planner_runtime

try:
    from scripts.host.game.start_mm_dota2 import StartMmDota2
except Exception:
    StartMmDota2 = None  # type: ignore[assignment]


class VmStatus:
    REGISTERED = "registered"
    IDLE = "idle"
    ACCOUNTS_ASSIGNED = "accounts_assigned"
    BOOTSTRAP = "bootstrap"
    LOGIN = "login"
    WAIT_DOTA = "wait_dota"
    GAME_READY = "game_ready"
    MM_PREPARE = "mm_prepare"
    PLANNER_ACTIVE = "planner_active"
    OFFLINE = "offline"
    ERROR = "error"


class HostCommandType:
    LAUNCH_PROCESS = "launch_process"
    KILL_PROCESS_TREE = "kill_process_tree"
    FIND_LOGIN_WINDOW = "find_login_window"
    FIND_DOTA_WINDOW = "find_dota_window"
    FOCUS_WINDOW = "focus_window"
    MOVE_WINDOW = "move_window"
    MOUSE_CLICK = "mouse_click"
    MOUSE_MOVE = "mouse_move"
    KEY_PRESS = "key_press"
    KEY_EVENT = "key_event"
    WRITE_TEXT = "write_text"
    HOTKEY = "hotkey"
    SLEEP = "sleep"
    CAPTURE_FRAME = "capture_frame"
    CAPTURE_DESKTOP = "capture_desktop"
    LOG = "log"
    DISMISS_STEAM_POPUPS = "dismiss_steam_popups"


@dataclass
class VmAccountState:
    username: str
    password: str
    mafile_path: Optional[str] = None
    has_mafile: bool = False

    launched: bool = False
    launch_sent: bool = False
    launch_pid: Optional[int] = None
    last_launch_ts: float = 0.0

    login_window_found: bool = False
    login_hwnd: Optional[int] = None
    login_find_in_progress: bool = False
    last_login_find_ts: float = 0.0

    auth_branch: Optional[str] = None
    auth_started: bool = False
    auth_done: bool = False
    auth_capture_requested: bool = False
    last_auth_capture_ts: float = 0.0
    auth_capture_fail_count: int = 0
    auth_flow_in_progress: bool = False
    auth_flow_started_ts: float = 0.0
    auth_flow_fail_count: int = 0
    auth_job_id: int = 0

    popup_capture_requested: bool = False
    popup_dismiss_in_progress: bool = False
    popup_fail_count: int = 0
    last_popup_scan_ts: float = 0.0
    last_popup_match_name: Optional[str] = None

    dota_window_found: bool = False
    dota_hwnd: Optional[int] = None
    dota_pid: Optional[int] = None
    dota_find_in_progress: bool = False
    last_dota_find_ts: float = 0.0
    dota_wait_started_ts: float = 0.0
    dota_wait_fail_count: int = 0

    last_error: Optional[str] = None

    def to_payload(self) -> dict:
        return {
            "login": self.username,
            "password": self.password,
            "has_mafile": self.has_mafile,
            "mafile_path": self.mafile_path,
        }


@dataclass
class VmCommand:
    id: int
    type: str
    payload: Dict[str, Any]
    created_ts: float
    sent_ts: float = 0.0
    status: str = "queued"
    result: Optional[Dict[str, Any]] = None


@dataclass
class VmState:
    vm_id: str
    capacity: int = 5
    status: str = VmStatus.REGISTERED
    is_online: bool = True
    side: str = "radiant"

    assigned_accounts: List[VmAccountState] = field(default_factory=list)

    login_hwnds: List[int] = field(default_factory=list)
    dota_hwnds: List[int] = field(default_factory=list)
    dota_window_sizes: Dict[int, tuple[int, int]] = field(default_factory=dict)

    desktop_width: int = 1920
    desktop_height: int = 1080

    roles: List[str] = field(default_factory=list)

    planner_active: bool = False
    last_ping_ts: float = 0.0
    last_log_ts: float = 0.0

    windows_arranged: bool = False
    windows_arrange_sent: bool = False
    windows_arrange_attempts: int = 0
    windows_arrange_pending_ids: List[int] = field(default_factory=list)

    command_queue: List[VmCommand] = field(default_factory=list)
    current_command_id: Optional[int] = None


class Controller:
    def __init__(
        self,
        logger: logging.Logger,
        status_cb: Callable[[str, str], None],
    ):
        self.logger = logger
        self.status_cb = status_cb

        self.accounts: List[Account] = []
        self.mafile_index: Dict[str, tuple[str, dict]] = {}
        self.vms: Dict[str, VmState] = {}

        self.running = False
        self.session_started_at: Optional[float] = None
        self.state_file = "../../../state.json"

        self._lock = threading.RLock()
        self._next_command_id = 1
        self._next_vm_num = 1
        self._django_started = False
        self._tick_error_count = 0
        self._last_tick_error_log_ts = 0.0
        self._mm_stage_log_state: Dict[str, tuple[str, float]] = {}

        self.batch_size = 5
        self.steam_path = r"C:\Program Files (x86)\Steam\steam.exe"
        self.app_id = APP_ID_DOTA
        self.launch_opts = list(DOTA_LAUNCH_OPTS)
        self.find_login_window_timeout_sec = FIND_LOGIN_WINDOW_TIMEOUT_SEC
        self.find_dota_window_timeout_sec = FIND_DOTA_WINDOW_TIMEOUT_SEC
        self.mafile_auth_timeout_sec = 150.0
        self.max_mafile_auth_failures = 3
        self.dota_wait_timeout_sec = max(180.0, FIND_DOTA_WINDOW_TIMEOUT_SEC * 3.0)
        self.max_dota_wait_failures = 3
        self.max_window_arrange_attempts = 3
        self.desktop_popup_scan_interval_sec = 10.0

        self.screen_w = 1920
        self.screen_h = 1080
        self.dota_window_w = 820
        self.dota_window_h = 640

        self._steam_templates = self._load_steam_templates()
        self._desktop_frames: Dict[str, np.ndarray] = {}
        self._desktop_frame_ts: Dict[str, float] = {}
        self._desktop_popup_match_cache: Dict[str, tuple[float, Optional[dict[str, Any]]]] = {}

        self.mm_starter = None
        if StartMmDota2 is not None:
            self.mm_starter = StartMmDota2(
                logger=self.logger,
                queue_command=lambda vm_id, cmd_type, payload: self._push_command(
                    self.vms[vm_id],
                    cmd_type,
                    payload,
                ),
                images_root="images",
                confidence=0.87,
            )

        self.load_state()


    # -----------------------------------------------------
    # lifecycle
    # -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self.running = True
            if self.session_started_at is None:
                self.session_started_at = time.time()
        self.logger.info("Host controller started")

    def stop(self) -> None:
        with self._lock:
            self.running = False
        self.logger.info("Host controller stopped")

    def tick_one(self) -> None:
        try:
            with self._lock:
                if not self.running:
                    return

                self.ensure_runtime_ready()
                self._expire_stale_commands()
                self.assign_batches_to_idle_vms()
                self.drive_vm_bootstrap()
                self.mark_stale_vms()

            self.activate_planners_for_ready_vms()
            planner_runtime.tick_all()

            self._tick_error_count = 0

        except Exception as e:
            self._tick_error_count += 1
            now = time.time()
            min_interval = min(30.0, max(2.0, 0.25 * self._tick_error_count))
            if (
                self._last_tick_error_log_ts <= 0
                or now - self._last_tick_error_log_ts >= min_interval
            ):
                self._last_tick_error_log_ts = now
                self.logger.error(
                    "controller.tick_one failed: "
                    f"{e}; failures={self._tick_error_count}",
                    exc_info=True,
                )

    def _command_timeout_sec(
        self,
        cmd_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> float:
        def payload_timeout_sec(fallback: float) -> float:
            if not payload:
                return fallback
            try:
                timeout_sec = float(payload.get("timeout_sec", 0.0) or 0.0)
                if timeout_sec > 0:
                    return timeout_sec
            except Exception:
                pass
            try:
                timeout_ms = float(payload.get("timeout_ms", 0.0) or 0.0)
                if timeout_ms > 0:
                    return timeout_ms / 1000.0
            except Exception:
                pass
            return fallback

        if cmd_type == HostCommandType.FIND_LOGIN_WINDOW:
            return payload_timeout_sec(self.find_login_window_timeout_sec) + 5.0
        if cmd_type == HostCommandType.FIND_DOTA_WINDOW:
            return payload_timeout_sec(self.find_dota_window_timeout_sec) + 3.0
        if cmd_type == HostCommandType.SLEEP:
            duration_ms = 0
            if payload:
                try:
                    duration_ms = int(payload.get("duration_ms", 0) or 0)
                except Exception:
                    duration_ms = 0
            return max(5.0, duration_ms / 1000.0 + 5.0)
        if cmd_type == HostCommandType.CAPTURE_FRAME:
            return 8.0
        if cmd_type == HostCommandType.CAPTURE_DESKTOP:
            return 8.0
        if cmd_type in (
            HostCommandType.FOCUS_WINDOW,
            HostCommandType.WRITE_TEXT,
            HostCommandType.KEY_PRESS,
            HostCommandType.DISMISS_STEAM_POPUPS,
        ):
            return 15.0
        return 20.0

    def _expire_stale_commands(self) -> None:
        now = time.time()
        for vm in self.vms.values():
            expired: list[VmCommand] = []
            for cmd in vm.command_queue:
                if cmd.status != "sent":
                    continue
                sent_ts = float(cmd.sent_ts or cmd.created_ts)
                if now - sent_ts <= self._command_timeout_sec(cmd.type, cmd.payload):
                    continue
                expired.append(cmd)

            for cmd in expired:
                self.logger.warning(
                    f"{vm.vm_id}: stale sent command expired id={cmd.id} type={cmd.type}"
                )
                cmd.status = "failed"
                cmd.result = {"error": "host timeout waiting command ack", "expired": True}
                if vm.current_command_id == cmd.id:
                    vm.current_command_id = None
                self._handle_command_result(vm, cmd)
                self._notify_mm_command_result(vm.vm_id, cmd)

    def ensure_runtime_ready(self) -> None:
        if not self._django_started:
            self._django_started = True
            self.logger.info("Host runtime marked as ready")

    # -----------------------------------------------------
    # image / desktop matching
    # -----------------------------------------------------

    def _load_steam_templates(self) -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        steam_dir = os.path.abspath(os.path.join("images", "steam"))

        if not os.path.isdir(steam_dir):
            self.logger.warning(f"steam images dir not found: {steam_dir}")
            return templates

        for name in sorted(os.listdir(steam_dir)):
            path = os.path.join(steam_dir, name)
            if not os.path.isfile(path):
                continue
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
                continue

            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if img is None or img.size == 0:
                self.logger.warning(f"failed to load steam template: {path}")
                continue

            h, w = img.shape[:2]
            templates.append(
                {
                    "name": name,
                    "path": path,
                    "image": img,
                    "w": int(w),
                    "h": int(h),
                }
            )

        self.logger.info(f"Loaded steam templates: {len(templates)}")
        return templates

    def _store_desktop_frame_from_result(self, vm_id: str, result: Optional[dict]) -> None:
        if not result:
            return

        image_b64 = result.get("image_b64")
        if not image_b64:
            return

        try:
            raw = base64.b64decode(image_b64)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            self._desktop_frames[vm_id] = np.array(img, dtype=np.uint8)
            self._desktop_frame_ts[vm_id] = time.time()
            self._desktop_popup_match_cache.pop(vm_id, None)
        except Exception as e:
            self.logger.warning(f"{vm_id}: failed to decode desktop frame: {e}")

    def _clear_desktop_frame(self, vm_id: str) -> None:
        self._desktop_frames.pop(vm_id, None)
        self._desktop_frame_ts.pop(vm_id, None)
        self._desktop_popup_match_cache.pop(vm_id, None)

    def _find_desktop_steam_popup_match(
        self,
        vm_id: str,
        threshold: float = 0.88,
    ) -> Optional[dict[str, Any]]:
        frame_rgb = self._desktop_frames.get(vm_id)
        if frame_rgb is None or not self._steam_templates:
            return None

        frame_ts = float(self._desktop_frame_ts.get(vm_id, 0.0) or 0.0)
        cached = self._desktop_popup_match_cache.get(vm_id)
        if cached is not None and float(cached[0]) == frame_ts:
            return cached[1]

        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None

        best: Optional[dict[str, Any]] = None

        for tpl in self._steam_templates:
            tpl_img = tpl["image"]
            th = tpl["h"]
            tw = tpl["w"]

            if gray.shape[0] < th or gray.shape[1] < tw:
                continue

            try:
                res = cv2.matchTemplate(gray, tpl_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
            except Exception:
                continue

            score = float(max_val)
            if score < threshold:
                continue

            cand = {
                "name": tpl["name"],
                "score": score,
                "x": int(max_loc[0] + tw / 2),
                "y": int(max_loc[1] + th / 2),
                "w": int(tw),
                "h": int(th),
            }

            if best is None or cand["score"] > best["score"]:
                best = cand

        self._desktop_popup_match_cache[vm_id] = (frame_ts, best)
        return best

    # -----------------------------------------------------
    # persistence
    # -----------------------------------------------------

    def save_state(self) -> None:
        data = {
            "steam_path": self.steam_path,
            "accounts": [acc.to_state_dict() for acc in self.accounts],
            "vms": [],
        }

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_state(self) -> None:
        if not os.path.exists(self.state_file):
            return

        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.accounts.clear()
        self.vms.clear()
        self.steam_path = data.get("steam_path", self.steam_path)

        for accd in data.get("accounts", []):
            self.accounts.append(
                Account.from_state_dict(
                    accd,
                    logger=self.logger,
                    status_cb=self.status_cb,
                )
            )

        self._next_vm_num = 1
        self.logger.info(
            f"Loaded state: accounts={len(self.accounts)}, vms=0 runtime reset"
        )

    # -----------------------------------------------------
    # accounts / mafiles
    # -----------------------------------------------------

    def load_accounts_from_txt(self, path: str, append: bool = False) -> None:
        if not append:
            self.accounts.clear()

        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue

                login, password = line.split(":", 1)
                if any(a.username == login for a in self.accounts):
                    continue

                self.accounts.append(
                    Account(
                        login,
                        password,
                        self.logger,
                        None,
                        self.status_cb,
                        thread_registry=None,
                    )
                )
                count += 1

        self.logger.info(f"Loaded accounts from txt: {count}")

    def remove_accounts(self, usernames: List[str]) -> None:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.username not in usernames]
        self.logger.info(
            f"Removed accounts: {before - len(self.accounts)}. Left: {len(self.accounts)}"
        )

    def selected_accounts(self, names: List[str]) -> List[Account]:
        by = {a.username: a for a in self.accounts}
        return [by[n] for n in names if n in by]

    def build_mafile_index(self, folder: str) -> None:
        self.mafile_index.clear()
        total = 0
        ok = 0

        for root, _, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith((".mafile", ".json")):
                    continue
                total += 1
                path = os.path.join(root, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    acc_name = (data.get("account_name") or "").lower()
                    if acc_name:
                        self.mafile_index[acc_name] = (path, data)
                        ok += 1
                except Exception as e:
                    self.logger.error(f"mafile parse error '{path}': {e}")

        self.logger.info(f"Mafiles scanned: total={total}, matched={ok}")

    def match_mafiles_to_accounts(self) -> None:
        matched = 0
        for acc in self.accounts:
            tpl = self.mafile_index.get(acc.username.lower())
            if tpl:
                path, data = tpl
                acc.attach_mafile(path, data)
                matched += 1
        self.logger.info(f"Mafiles attached: {matched}/{len(self.accounts)}")

    # -----------------------------------------------------
    # vm registry
    # -----------------------------------------------------

    def _alloc_vm_id(self) -> str:
        vm_id = f"vm_{self._next_vm_num}"
        self._next_vm_num += 1
        return vm_id

    def register_vm(self) -> VmState:
        with self._lock:
            vm_id = self._alloc_vm_id()
            vm = VmState(
                vm_id=vm_id,
                capacity=self.batch_size,
                status=VmStatus.REGISTERED,
            )
            vm.last_ping_ts = time.time()
            self.vms[vm_id] = vm
            planner_runtime.register_vm(vm_id=vm_id)
            self.logger.info(f"VM registered: {vm_id}")
            return vm

    def get_vm(self, vm_id: str) -> Optional[VmState]:
        with self._lock:
            return self.vms.get(vm_id)

    def touch_vm(self, vm_id: str) -> None:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm:
                vm.last_ping_ts = time.time()
                vm.is_online = True

    # -----------------------------------------------------
    # batching
    # -----------------------------------------------------

    def get_unassigned_accounts(self) -> List[Account]:
        assigned = set()
        for vm in self.vms.values():
            for a in vm.assigned_accounts:
                assigned.add(a.username)
        return [a for a in self.accounts if a.username not in assigned]

    def assign_batches_to_idle_vms(self) -> None:
        free_accounts = self.get_unassigned_accounts()
        if not free_accounts:
            return

        for vm in self.vms.values():
            if vm.assigned_accounts or not vm.is_online:
                continue

            batch = free_accounts[: self.batch_size]
            if not batch:
                break

            vm.assigned_accounts = [
                VmAccountState(
                    username=a.username,
                    password=a.password,
                    mafile_path=a.mafile_path,
                    has_mafile=bool(a.mafile_data),
                )
                for a in batch
            ]
            vm.status = VmStatus.ACCOUNTS_ASSIGNED
            free_accounts = free_accounts[self.batch_size :]

            self.logger.info(
                f"Assigned {len(vm.assigned_accounts)} accounts to {vm.vm_id}: "
                f"{[a.username for a in vm.assigned_accounts]}"
            )

    def get_vm_accounts_payload(self, vm_id: str) -> List[dict]:
        with self._lock:
            vm = self.vms[vm_id]
            return [a.to_payload() for a in vm.assigned_accounts]

    # -----------------------------------------------------
    # helpers
    # -----------------------------------------------------

    def _get_vm_account(self, vm: VmState, username: str) -> Optional[VmAccountState]:
        for acc in vm.assigned_accounts:
            if acc.username == username:
                return acc
        return None

    def _has_inflight_for_account(self, vm: VmState, username: str) -> bool:
        for c in vm.command_queue:
            if c.status in ("queued", "sent") and c.payload.get("account_login") == username:
                return True
        return False

    def _first_active_account(self, vm: VmState) -> Optional[VmAccountState]:
        for acc in vm.assigned_accounts:
            if not acc.dota_window_found or acc.dota_hwnd is None:
                return acc
        return None

    def _used_dota_hwnds(self, vm: VmState, except_username: Optional[str] = None) -> List[int]:
        out: List[int] = []
        for acc in vm.assigned_accounts:
            if except_username and acc.username == except_username:
                continue
            if acc.dota_hwnd is not None:
                out.append(int(acc.dota_hwnd))
        return out

    def _used_dota_pids(self, vm: VmState, except_username: Optional[str] = None) -> List[int]:
        out: List[int] = []
        for acc in vm.assigned_accounts:
            if except_username and acc.username == except_username:
                continue
            if acc.dota_pid is not None:
                out.append(int(acc.dota_pid))
        return out

    def _friend_ids_for_vm(self, vm: VmState) -> List[Optional[str]]:
        by_username = {acc.username: acc for acc in self.accounts}
        out: List[Optional[str]] = []

        for vm_acc in vm.assigned_accounts:
            acc = by_username.get(vm_acc.username)
            if acc is None:
                out.append(None)
                continue

            try:
                fid = acc.get_steamid3()
                out.append(str(fid) if fid is not None else None)
            except Exception as e:
                self.logger.warning(
                    f"friend_id resolve failed for {vm_acc.username}: {e}"
                )
                out.append(None)

        return out

    # -----------------------------------------------------
    # command queue
    # -----------------------------------------------------

    def _push_command(self, vm: VmState, cmd_type: str, payload: Dict[str, Any]) -> VmCommand:
        with self._lock:
            cmd = VmCommand(
                id=self._next_command_id,
                type=cmd_type,
                payload=payload,
                created_ts=time.time(),
            )
            self._next_command_id += 1
            vm.command_queue.append(cmd)
            return cmd

    def get_next_command(self, vm_id: str) -> Optional[dict]:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm is None:
                return None

            if vm.current_command_id is not None:
                for c in vm.command_queue:
                    if c.id != vm.current_command_id:
                        continue
                    if c.status == "queued":
                        c.status = "sent"
                        c.sent_ts = time.time()
                        return {"id": c.id, "type": c.type, "payload": c.payload}
                    if c.status == "sent":
                        return None

                vm.current_command_id = None

            for c in vm.command_queue:
                if c.status == "queued":
                    c.status = "sent"
                    c.sent_ts = time.time()
                    vm.current_command_id = c.id
                    return {"id": c.id, "type": c.type, "payload": c.payload}

        return None

    def ack_command(self, vm_id: str, command_id: int, status: str, result: Optional[dict]) -> bool:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm is None:
                return False

            for c in vm.command_queue:
                if c.id == int(command_id):
                    c.status = status
                    c.result = result or {}

                    if vm.current_command_id == c.id:
                        vm.current_command_id = None

                    self._handle_command_result(vm, c)
                    self._notify_mm_command_result(vm_id, c)

                    return True

        return False

    def _notify_mm_command_result(self, vm_id: str, cmd: VmCommand) -> None:
        if self.mm_starter is None:
            return

        try:
            self.mm_starter.on_command_result(
                vm_id=vm_id,
                cmd_type=cmd.type,
                payload=cmd.payload,
                result=cmd.result or {},
                status=cmd.status,
            )
        except AttributeError:
            try:
                self.mm_starter.mark_command_done(vm_id)
            except Exception:
                pass
        except Exception as e:
            self.logger.warning(f"{vm_id}: mm_starter.on_command_result failed: {e}")

    def _source_account_for_vm_account(self, acc: VmAccountState) -> Optional[Account]:
        for source in self.accounts:
            if source.username == acc.username:
                return source
        return None

    def _start_mafile_auth_job(
        self,
        vm: VmState,
        acc: VmAccountState,
        image_b64: str,
    ) -> None:
        if acc.auth_flow_in_progress or acc.auth_done:
            return

        source = self._source_account_for_vm_account(acc)
        mafile_path = acc.mafile_path or (source.mafile_path if source else None)
        mafile_data = source.mafile_data if source else None

        if not mafile_path and not mafile_data:
            acc.auth_flow_fail_count += 1
            acc.last_error = "mafile data is missing"
            self.logger.error(f"{vm.vm_id}: mafile auth cannot start for {acc.username}: no mafile")
            return

        acc.auth_job_id += 1
        job_id = acc.auth_job_id
        acc.auth_flow_in_progress = True
        acc.auth_flow_started_ts = time.time()
        acc.auth_started = True

        def worker() -> None:
            ok = False
            details: dict[str, Any] = {}
            error: Optional[str] = None

            try:
                from pathlib import Path

                from pyzbar.pyzbar import ZBarSymbol, decode

                from scripts.host.core import qr_loger

                raw = base64.b64decode(image_b64)
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                codes = decode(img, symbols=[ZBarSymbol.QRCODE])
                if not codes:
                    raise RuntimeError("QR code was not found in login frame")

                qr_url = codes[0].data.decode("utf-8", errors="ignore")

                ma = dict(mafile_data or {})
                path_obj = None
                if mafile_path:
                    path_obj = Path(mafile_path)
                    if not ma:
                        with open(path_obj, "r", encoding="utf-8") as f:
                            ma = json.load(f)

                access_token = qr_loger.extract_existing_token(ma)
                if not access_token:
                    raise RuntimeError("mafile has no access token for QR approval")

                ok, snapshot, updated_ma = qr_loger.do_flow(
                    qr_url=qr_url,
                    poll_payload_b64=None,
                    mafile_path=path_obj,
                    save_to=path_obj,
                    ma=ma,
                    login=acc.username,
                    password=acc.password,
                    access_token=access_token,
                    poll_seconds=60,
                    exit_on_interaction=True,
                    debug_payload=False,
                    login_hwnd=None,
                    exit_on_window_close=False,
                    exit_on_qr_disappear=False,
                    qr_disappear_consecutive=2,
                    qr_recheck_interval=1.0,
                )
                details = dict(snapshot or {})

                if path_obj is not None:
                    qr_loger.save_mafile(path_obj, updated_ma)

            except Exception as e:
                error = str(e)

            with self._lock:
                cur_vm = self.vms.get(vm.vm_id)
                cur_acc = self._get_vm_account(cur_vm, acc.username) if cur_vm else None
                if cur_acc is None or cur_acc.auth_job_id != job_id:
                    return

                cur_acc.auth_flow_in_progress = False
                if ok:
                    self._mark_auth_done(cur_acc)
                    cur_acc.auth_flow_fail_count = 0
                    cur_acc.last_error = None
                    self.logger.info(
                        f"{vm.vm_id}: mafile auth done -> {cur_acc.username} "
                        f"finish={details.get('finish_mode')}"
                    )
                else:
                    cur_acc.auth_done = False
                    cur_acc.auth_flow_fail_count += 1
                    cur_acc.last_error = error or str(details or "mafile auth failed")
                    self.logger.warning(
                        f"{vm.vm_id}: mafile auth failed for {cur_acc.username}: "
                        f"{cur_acc.last_error}"
                    )

        t = threading.Thread(
            target=worker,
            daemon=True,
            name=f"mafile-auth-{vm.vm_id}-{acc.username}",
        )
        t.start()
        self.logger.info(f"{vm.vm_id}: mafile auth started -> {acc.username}")

    def _check_mafile_auth_timeout(self, vm: VmState, acc: VmAccountState) -> bool:
        if not acc.auth_flow_in_progress:
            return False

        elapsed = time.time() - float(acc.auth_flow_started_ts or 0.0)
        if elapsed <= self.mafile_auth_timeout_sec:
            return True

        acc.auth_job_id += 1
        acc.auth_flow_in_progress = False
        acc.auth_done = False
        acc.auth_flow_fail_count += 1
        acc.last_error = "mafile auth timeout"
        self.logger.warning(
            f"{vm.vm_id}: mafile auth timed out after {elapsed:.1f}s -> {acc.username}"
        )
        return False

    def _forget_login_window(self, vm: VmState, acc: VmAccountState) -> None:
        if acc.login_hwnd is not None:
            try:
                vm.login_hwnds = [h for h in vm.login_hwnds if int(h) != int(acc.login_hwnd)]
            except Exception:
                pass
        acc.login_window_found = False
        acc.login_hwnd = None
        acc.login_find_in_progress = False

    def _mark_auth_done(self, acc: VmAccountState) -> None:
        acc.auth_started = True
        acc.auth_done = True
        if acc.dota_wait_started_ts <= 0:
            acc.dota_wait_started_ts = time.time()

    def _result_has_empty_process_tree(self, result: Optional[dict]) -> bool:
        if not result:
            return False

        if "process_tree_alive" in result:
            return not bool(result.get("process_tree_alive"))

        tree_pids = result.get("tree_pids")
        if tree_pids is None:
            return False

        try:
            return len(list(tree_pids)) == 0
        except Exception:
            return False

    def _reset_account_for_relaunch(
        self,
        vm: VmState,
        acc: VmAccountState,
        reason: str,
        *,
        count_dota_wait_failure: bool = False,
    ) -> None:
        wait_fail_count = acc.dota_wait_fail_count + (1 if count_dota_wait_failure else 0)

        self._cancel_queued_account_commands(vm, acc.username, reason)

        self._forget_login_window(vm, acc)

        if acc.dota_hwnd is not None:
            try:
                hwnd_i = int(acc.dota_hwnd)
                vm.dota_hwnds = [h for h in vm.dota_hwnds if int(h) != hwnd_i]
                vm.dota_window_sizes.pop(hwnd_i, None)
            except Exception:
                pass

        acc.launched = False
        acc.launch_sent = False
        acc.launch_pid = None
        acc.last_launch_ts = 0.0

        acc.auth_branch = None
        acc.auth_started = False
        acc.auth_done = False
        acc.auth_capture_requested = False
        acc.auth_flow_in_progress = False

        acc.popup_capture_requested = False
        acc.popup_dismiss_in_progress = False
        acc.last_popup_scan_ts = 0.0
        acc.last_popup_match_name = None

        acc.dota_window_found = False
        acc.dota_hwnd = None
        acc.dota_pid = None
        acc.dota_find_in_progress = False
        acc.last_dota_find_ts = 0.0
        acc.dota_wait_started_ts = 0.0
        acc.dota_wait_fail_count = wait_fail_count

        acc.last_error = reason
        self._clear_desktop_frame(vm.vm_id)

        if count_dota_wait_failure and wait_fail_count >= self.max_dota_wait_failures:
            vm.status = VmStatus.ERROR
            self.logger.error(
                f"{vm.vm_id}: dota did not appear for {acc.username}; "
                f"giving up after {wait_fail_count} relaunch attempts: {reason}"
            )
            return

        self.logger.warning(
            f"{vm.vm_id}: reset launch state -> {acc.username}: {reason}"
        )

    def _finish_window_arrange_if_ready(self, vm: VmState, failed: bool) -> None:
        if failed:
            vm.windows_arrange_sent = False
            vm.windows_arranged = False
            vm.windows_arrange_pending_ids.clear()
            return

        pending = {
            c.id
            for c in vm.command_queue
            if c.status in ("queued", "sent")
            and c.type == HostCommandType.MOVE_WINDOW
            and c.payload.get("purpose") == "arrange_dota_windows"
        }
        vm.windows_arrange_pending_ids = [
            cmd_id for cmd_id in vm.windows_arrange_pending_ids if cmd_id in pending
        ]

        if not vm.windows_arrange_pending_ids:
            vm.windows_arrange_sent = False
            vm.windows_arranged = True
            self.logger.info(f"{vm.vm_id}: window arrangement acknowledged")

    def _cancel_queued_account_commands(
        self,
        vm: VmState,
        username: str,
        reason: str,
    ) -> None:
        for pending in vm.command_queue:
            if pending.status != "queued":
                continue
            if pending.payload.get("account_login") != username:
                continue
            pending.status = "failed"
            pending.result = {"error": reason, "cancelled": True}

    def _handle_command_result(self, vm: VmState, cmd: VmCommand) -> None:
        account_login = str(
            (cmd.result or {}).get("account_login")
            or cmd.payload.get("account_login")
            or ""
        )
        acc = self._get_vm_account(vm, account_login) if account_login else None

        soft_failure = False

        if cmd.status == "failed":
            if acc is not None:
                acc.last_error = str((cmd.result or {}).get("error", "command failed"))

                if cmd.type == HostCommandType.LAUNCH_PROCESS:
                    acc.launch_sent = False

                elif cmd.type == HostCommandType.FIND_LOGIN_WINDOW:
                    acc.login_find_in_progress = False

                elif cmd.type == HostCommandType.CAPTURE_FRAME:
                    soft_failure = True

                    if acc.auth_capture_requested:
                        acc.auth_capture_requested = False
                        acc.auth_capture_fail_count += 1
                        if acc.auth_capture_fail_count >= 3:
                            acc.login_window_found = False
                            acc.login_hwnd = None
                            acc.login_find_in_progress = False
                            self.logger.warning(
                                f"{vm.vm_id}: auth capture failed {acc.auth_capture_fail_count} times -> "
                                f"refind login window for {acc.username}"
                            )

                    if acc.popup_capture_requested:
                        acc.popup_capture_requested = False

                    self.logger.warning(f"{vm.vm_id}: capture_frame failed softly -> continue")

                elif cmd.type == HostCommandType.CAPTURE_DESKTOP:
                    acc.popup_capture_requested = False
                    soft_failure = True

                elif cmd.type == HostCommandType.DISMISS_STEAM_POPUPS:
                    acc.popup_dismiss_in_progress = False
                    acc.popup_fail_count += 1
                    self._clear_desktop_frame(vm.vm_id)
                    soft_failure = True

                elif cmd.type == HostCommandType.FIND_DOTA_WINDOW:
                    acc.dota_find_in_progress = False
                    soft_failure = True

                elif (
                    cmd.type
                    in (
                        HostCommandType.FOCUS_WINDOW,
                        HostCommandType.WRITE_TEXT,
                        HostCommandType.KEY_PRESS,
                        HostCommandType.KEY_EVENT,
                        HostCommandType.HOTKEY,
                        HostCommandType.SLEEP,
                    )
                    and not acc.auth_done
                ):
                    self._cancel_queued_account_commands(
                        vm,
                        acc.username,
                        f"cancelled after {cmd.type} failed",
                    )
                    acc.auth_started = False
                    acc.auth_done = False
                    acc.auth_capture_requested = False
                    acc.auth_flow_in_progress = False
                    self._forget_login_window(vm, acc)
                    soft_failure = True
                    self.logger.warning(
                        f"{vm.vm_id}: auth input command failed -> refind login "
                        f"for {acc.username}: {cmd.result}"
                    )

                elif cmd.type == HostCommandType.MOVE_WINDOW:
                    soft_failure = True
                    self._finish_window_arrange_if_ready(vm, failed=True)

            elif cmd.type == HostCommandType.MOVE_WINDOW:
                soft_failure = True
                self._finish_window_arrange_if_ready(vm, failed=True)

            if not soft_failure:
                vm.status = VmStatus.ERROR
                self.logger.error(f"{vm.vm_id}: command failed {cmd.type} -> {cmd.result}")

        else:
            if cmd.type == HostCommandType.LAUNCH_PROCESS:
                if acc is not None:
                    acc.launched = True
                    acc.launch_sent = False
                    pid = (cmd.result or {}).get("pid")
                    acc.launch_pid = None if pid is None else int(pid)

            elif cmd.type == HostCommandType.FIND_LOGIN_WINDOW:
                if acc is not None:
                    acc.login_find_in_progress = False
                    found = bool((cmd.result or {}).get("found"))
                    hwnd = (cmd.result or {}).get("hwnd")
                    acc.login_window_found = found
                    acc.login_hwnd = None if hwnd is None else int(hwnd)
                    if found and acc.login_hwnd is not None:
                        acc.auth_capture_fail_count = 0
                        acc.auth_flow_fail_count = 0
                        acc.last_error = None
                        if acc.login_hwnd not in vm.login_hwnds:
                            vm.login_hwnds.append(acc.login_hwnd)
                    elif acc.launched and self._result_has_empty_process_tree(cmd.result):
                        self._reset_account_for_relaunch(
                            vm,
                            acc,
                            "steam process tree disappeared while finding login window",
                        )

            elif cmd.type == HostCommandType.KEY_PRESS:
                if acc is not None:
                    vk_code = int(cmd.payload.get("vk_code", 0))
                    if vk_code == 0x0D:
                        self._mark_auth_done(acc)

            elif cmd.type == HostCommandType.CAPTURE_FRAME:
                if acc is not None:
                    if acc.auth_capture_requested:
                        acc.auth_capture_requested = False
                        acc.auth_capture_fail_count = 0
                        acc.auth_started = True
                        if acc.auth_branch == "mafile" or acc.has_mafile:
                            image_b64 = str((cmd.result or {}).get("image_b64") or "")
                            if image_b64:
                                self._start_mafile_auth_job(vm, acc, image_b64)
                            else:
                                acc.auth_flow_fail_count += 1
                                acc.last_error = "auth capture returned no image"
                                self.logger.warning(
                                    f"{vm.vm_id}: auth capture returned no image -> {acc.username}"
                                )
                    if acc.popup_capture_requested:
                        acc.popup_capture_requested = False

            elif cmd.type == HostCommandType.CAPTURE_DESKTOP:
                self._store_desktop_frame_from_result(vm.vm_id, cmd.result)
                if acc is not None:
                    acc.popup_capture_requested = False

            elif cmd.type == HostCommandType.DISMISS_STEAM_POPUPS:
                if acc is not None:
                    acc.popup_dismiss_in_progress = False
                    dismissed = bool((cmd.result or {}).get("dismissed", False))
                    if dismissed:
                        acc.popup_fail_count = 0
                    self._clear_desktop_frame(vm.vm_id)

            elif cmd.type == HostCommandType.FIND_DOTA_WINDOW:
                if acc is not None:
                    acc.dota_find_in_progress = False

                    found = bool((cmd.result or {}).get("found"))
                    hwnd = (cmd.result or {}).get("hwnd")
                    pid = (cmd.result or {}).get("pid")
                    source = (cmd.result or {}).get("source")

                    if found and hwnd is not None:
                        hwnd_i = int(hwnd)
                        pid_i = None if pid is None else int(pid)

                        owner_hwnd = None
                        owner_pid = None

                        for other in vm.assigned_accounts:
                            if other.username == acc.username:
                                continue

                            if other.dota_hwnd is not None and int(other.dota_hwnd) == hwnd_i:
                                owner_hwnd = other.username

                            if (
                                pid_i is not None
                                and other.dota_pid is not None
                                and int(other.dota_pid) == pid_i
                            ):
                                owner_pid = other.username

                        if owner_hwnd is not None:
                            self.logger.warning(
                                f"{vm.vm_id}: rejected duplicate dota hwnd={hwnd_i} "
                                f"for {acc.username}, already owned by {owner_hwnd}"
                            )
                            acc.dota_window_found = False
                            acc.dota_hwnd = None
                            acc.dota_pid = None

                        elif owner_pid is not None:
                            self.logger.warning(
                                f"{vm.vm_id}: rejected duplicate dota pid={pid_i} "
                                f"for {acc.username}, already owned by {owner_pid}; "
                                f"source={source}"
                            )
                            acc.dota_window_found = False
                            acc.dota_hwnd = None
                            acc.dota_pid = None

                        else:
                            acc.dota_window_found = True
                            acc.dota_hwnd = hwnd_i
                            acc.dota_pid = pid_i
                            acc.dota_wait_started_ts = 0.0
                            acc.dota_wait_fail_count = 0

                            window_info = (cmd.result or {}).get("window_info") or {}
                            window_rect = window_info.get("window_rect") or {}
                            desktop = window_info.get("desktop") or {}

                            win_w = int(window_rect.get("width") or self.dota_window_w)
                            win_h = int(window_rect.get("height") or self.dota_window_h)

                            vm.dota_window_sizes[hwnd_i] = (win_w, win_h)

                            vm.desktop_width = int(desktop.get("width") or vm.desktop_width or self.screen_w)
                            vm.desktop_height = int(desktop.get("height") or vm.desktop_height or self.screen_h)

                            if hwnd_i not in vm.dota_hwnds:
                                vm.dota_hwnds.append(hwnd_i)

                            self.logger.info(
                                f"{vm.vm_id}: dota found -> {acc.username} "
                                f"hwnd={hwnd_i} pid={pid_i} source={source} "
                                f"outer={win_w}x{win_h} desktop={vm.desktop_width}x{vm.desktop_height}"
                            )
                    else:
                        acc.dota_window_found = False
                        acc.dota_hwnd = None
                        acc.dota_pid = None
                        if self._result_has_empty_process_tree(cmd.result):
                            self._reset_account_for_relaunch(
                                vm,
                                acc,
                                "steam process tree disappeared while waiting for dota window",
                                count_dota_wait_failure=True,
                            )

            elif cmd.type == HostCommandType.MOVE_WINDOW:
                self._finish_window_arrange_if_ready(vm, failed=False)

        vm.command_queue = [x for x in vm.command_queue if x.status not in ("done", "failed")]

    # -----------------------------------------------------
    # bootstrap scenario
    # -----------------------------------------------------

    def drive_vm_bootstrap(self) -> None:
        for vm in self.vms.values():
            if not vm.is_online or not vm.assigned_accounts or vm.planner_active:
                continue
            if vm.status == VmStatus.ERROR:
                continue
            if vm.current_command_id is not None:
                continue

            acc = self._first_active_account(vm)
            if acc is None:
                vm.status = VmStatus.GAME_READY
                continue

            if self._has_inflight_for_account(vm, acc.username):
                continue

            if not acc.launched:
                if acc.launch_sent:
                    continue

                self._push_command(
                    vm,
                    HostCommandType.LAUNCH_PROCESS,
                    {
                        "account_login": acc.username,
                        "exe_path": self.steam_path,
                        "args": ["-applaunch", str(self.app_id), *self.launch_opts],
                    },
                )
                acc.launch_sent = True
                acc.last_launch_ts = time.time()
                vm.status = VmStatus.BOOTSTRAP
                self.logger.info(f"{vm.vm_id}: launch_process -> {acc.username}")
                continue

            if not acc.login_window_found or acc.login_hwnd is None:
                now = time.time()
                if acc.login_find_in_progress:
                    continue
                if now - acc.last_login_find_ts < 2.0:
                    continue

                self._push_command(
                    vm,
                    HostCommandType.FIND_LOGIN_WINDOW,
                    {
                        "account_login": acc.username,
                        "timeout_ms": int(FIND_LOGIN_WINDOW_TIMEOUT_SEC * 1000),
                    },
                )
                acc.login_find_in_progress = True
                acc.last_login_find_ts = now
                vm.status = VmStatus.LOGIN
                self.logger.info(f"{vm.vm_id}: find_login_window -> {acc.username}")
                continue

            if not acc.auth_done:
                
                if acc.has_mafile:
                    now = time.time()

                    if self._check_mafile_auth_timeout(vm, acc):
                        continue

                    if acc.auth_flow_fail_count >= self.max_mafile_auth_failures:
                        self._forget_login_window(vm, acc)
                        acc.auth_capture_requested = False
                        acc.auth_flow_in_progress = False
                        self.logger.warning(
                            f"{vm.vm_id}: mafile auth failed too many times -> "
                            f"refind login window for {acc.username}"
                        )
                        continue

                    if acc.auth_capture_fail_count >= 3:
                        self._forget_login_window(vm, acc)
                        continue
                    if acc.auth_capture_requested:
                        continue
                    if now - acc.last_auth_capture_ts < 1.0:
                        continue

                    acc.auth_branch = "mafile"
                    self._push_command(
                        vm,
                        HostCommandType.FOCUS_WINDOW,
                        {
                            "account_login": acc.username,
                            "hwnd": int(acc.login_hwnd),
                        },
                    )
                    self._push_command(
                        vm,
                        HostCommandType.CAPTURE_FRAME,
                        {
                            "account_login": acc.username,
                            "hwnd": int(acc.login_hwnd),
                            "purpose": "auth_qr",
                        },
                    )
                    acc.auth_capture_requested = True
                    acc.last_auth_capture_ts = now
                    vm.status = VmStatus.LOGIN
                    self.logger.info(f"{vm.vm_id}: capture login frame -> {acc.username}")
                    continue

                acc.auth_branch = "password"
                self._push_command(
                    vm,
                    HostCommandType.FOCUS_WINDOW,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                    },
                )
                self._push_command(
                    vm,
                    HostCommandType.SLEEP,
                    {
                        "account_login": acc.username,
                        "duration_ms": 5000,
                    },
                )
                self._push_command(
                    vm,
                    HostCommandType.WRITE_TEXT,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                        "field": "login",
                        "text": acc.username,
                        "clear_before": True,
                        "input_method": "sendinput_unicode",
                    },
                )
                self._push_command(
                    vm,
                    HostCommandType.KEY_PRESS,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                        "vk_code": 0x09,
                        "hold_ms": 25,
                        "force_fg": True,
                    },
                )
                self._push_command(
                    vm,
                    HostCommandType.WRITE_TEXT,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                        "field": "password",
                        "text": acc.password,
                        "clear_before": True,
                        "input_method": "sendinput_unicode",
                    },
                )
                self._push_command(
                    vm,
                    HostCommandType.KEY_PRESS,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                        "vk_code": 0x0D,
                        "hold_ms": 25,
                        "force_fg": True,
                    },
                )
                acc.auth_started = True
                vm.status = VmStatus.LOGIN
                self.logger.info(f"{vm.vm_id}: manual auth queued -> {acc.username}")
                continue

            if not acc.dota_window_found or acc.dota_hwnd is None:
                now = time.time()

                if acc.dota_wait_started_ts <= 0:
                    acc.dota_wait_started_ts = now
                elif now - acc.dota_wait_started_ts > self.dota_wait_timeout_sec:
                    self._reset_account_for_relaunch(
                        vm,
                        acc,
                        (
                            "dota window did not appear within "
                            f"{self.dota_wait_timeout_sec:.0f}s after auth"
                        ),
                        count_dota_wait_failure=True,
                    )
                    continue

                if acc.popup_dismiss_in_progress:
                    continue
                if acc.popup_capture_requested:
                    continue
                if acc.dota_find_in_progress:
                    if now - acc.last_dota_find_ts > (self.find_dota_window_timeout_sec + 1.0):
                        acc.dota_find_in_progress = False
                        self.logger.warning(
                            f"{vm.vm_id}: find_dota_window timed out locally -> {acc.username}; reset polling lock"
                        )
                    else:
                        continue

                has_desktop_frame = vm.vm_id in self._desktop_frames
                desktop_frame_ts = float(self._desktop_frame_ts.get(vm.vm_id, 0.0) or 0.0)

                match = (
                    self._find_desktop_steam_popup_match(vm.vm_id)
                    if has_desktop_frame
                    else None
                )

                if match is not None:
                    self._push_command(
                        vm,
                        HostCommandType.DISMISS_STEAM_POPUPS,
                        {
                            "account_login": acc.username,
                            "x": int(match["x"]),
                            "y": int(match["y"]),
                            "template_name": str(match["name"]),
                            "score": float(match["score"]),
                            "coord_space": "screen",
                            "button": "left",
                            "clicks": 1,
                            "force_fg": False,
                        },
                    )
                    acc.popup_dismiss_in_progress = True
                    acc.last_popup_match_name = str(match["name"])
                    acc.last_popup_scan_ts = now
                    vm.status = VmStatus.WAIT_DOTA
                    self.logger.info(
                        f"{vm.vm_id}: dismiss desktop popup while waiting dota -> "
                        f"{acc.username} {match['name']} score={match['score']:.3f}"
                    )
                    continue

                should_refresh_desktop = (
                    not has_desktop_frame
                    or now - desktop_frame_ts >= self.desktop_popup_scan_interval_sec
                )
                if (
                    acc.last_dota_find_ts > 0
                    and should_refresh_desktop
                    and now - acc.last_popup_scan_ts >= self.desktop_popup_scan_interval_sec
                ):
                    self._push_command(
                        vm,
                        HostCommandType.CAPTURE_DESKTOP,
                        {
                            "account_login": acc.username,
                            "purpose": "desktop_popup_scan",
                        },
                    )
                    acc.popup_capture_requested = True
                    acc.last_popup_scan_ts = now
                    vm.status = VmStatus.WAIT_DOTA
                    self.logger.info(
                        f"{vm.vm_id}: desktop popup scan -> {acc.username}"
                    )
                    continue

                if now - acc.last_dota_find_ts < 2.0:
                    continue

                self._push_command(
                    vm,
                    HostCommandType.FIND_DOTA_WINDOW,
                    {
                        "account_login": acc.username,
                        "timeout_ms": int(FIND_DOTA_WINDOW_TIMEOUT_SEC * 1000),
                        "exclude_hwnds": self._used_dota_hwnds(vm, except_username=acc.username),
                        "exclude_pids": self._used_dota_pids(vm, except_username=acc.username),
                        "min_create_ts": acc.last_launch_ts,
                    },
                )
                acc.dota_find_in_progress = True
                acc.last_dota_find_ts = now
                vm.status = VmStatus.WAIT_DOTA
                self.logger.info(f"{vm.vm_id}: find_dota_window polling -> {acc.username}")
                continue

    # -----------------------------------------------------
    # window arrangement / mm / planner activation
    # -----------------------------------------------------

    def _build_window_grid_positions_dynamic(
        self,
        hwnds: List[int],
        screen_w: int,
        screen_h: int,
        window_sizes: Dict[int, tuple[int, int]],
    ) -> Dict[int, tuple[int, int]]:
        positions: Dict[int, tuple[int, int]] = {}

        x = 0
        y = 0
        row_height = 0

        for hwnd in hwnds:
            hwnd_i = int(hwnd)
            w, h = window_sizes.get(hwnd_i, (self.dota_window_w, self.dota_window_h))

            if x > 0 and (x + w) > screen_w:
                x = 0
                y += row_height
                row_height = 0

            positions[hwnd_i] = (x, y)

            x += w
            row_height = max(row_height, h)

        return positions

    def arrange_dota_windows(self, vm: VmState) -> None:
        if not vm.dota_hwnds:
            return
        if vm.windows_arranged or vm.windows_arrange_sent:
            return
        if vm.windows_arrange_attempts >= self.max_window_arrange_attempts:
            vm.windows_arranged = True
            vm.windows_arrange_sent = False
            self.logger.warning(
                f"{vm.vm_id}: window arrangement skipped after "
                f"{vm.windows_arrange_attempts} failed attempts"
            )
            return

        screen_w = int(vm.desktop_width or self.screen_w)
        screen_h = int(vm.desktop_height or self.screen_h)

        positions = self._build_window_grid_positions_dynamic(
            hwnds=vm.dota_hwnds,
            screen_w=screen_w,
            screen_h=screen_h,
            window_sizes=vm.dota_window_sizes,
        )

        for hwnd, (x, y) in positions.items():
            cmd = self._push_command(
                vm,
                HostCommandType.MOVE_WINDOW,
                {
                    "hwnd": int(hwnd),
                    "x": int(x),
                    "y": int(y),
                    "purpose": "arrange_dota_windows",
                },
            )
            vm.windows_arrange_pending_ids.append(cmd.id)
            self._push_command(
                vm,
                HostCommandType.FOCUS_WINDOW,
                {
                    "hwnd": int(hwnd),
                    "purpose": "arrange_dota_windows",
                },
            )

        vm.windows_arrange_attempts += 1
        vm.windows_arrange_sent = True
        vm.windows_arranged = False

        self.logger.info(
            f"{vm.vm_id}: queued arrange for {len(vm.dota_hwnds)} dota windows "
            f"desktop={screen_w}x{screen_h} sizes={vm.dota_window_sizes}"
        )

    def _log_mm_stage_waiting(self, vm_id: str, stage: str) -> None:
        now = time.time()
        prev_stage, prev_ts = self._mm_stage_log_state.get(vm_id, ("", 0.0))
        if prev_stage == stage and now - prev_ts < 2.0:
            return

        self._mm_stage_log_state[vm_id] = (stage, now)
        self.logger.info(f"{vm_id}: MM not ready yet, stage={stage}")

    def activate_planners_for_ready_vms(self) -> None:
        for vm in self.vms.values():
            if vm.planner_active:
                continue
            if not vm.assigned_accounts:
                continue
            if not vm.dota_hwnds:
                continue

            if any((not acc.dota_window_found or acc.dota_hwnd is None) for acc in vm.assigned_accounts):
                continue

            expected = len(vm.assigned_accounts)

            account_hwnds = [
                acc.dota_hwnd
                for acc in vm.assigned_accounts
                if acc.dota_hwnd is not None
            ]

            account_pids = [
                acc.dota_pid
                for acc in vm.assigned_accounts
                if acc.dota_pid is not None
            ]

            if len(account_hwnds) != expected:
                continue

            if len(set(int(x) for x in account_hwnds)) != expected:
                self.logger.warning(
                    f"{vm.vm_id}: duplicate dota hwnds detected, block MM/planner: {account_hwnds}"
                )
                continue

            if len(account_pids) != expected:
                self.logger.warning(
                    f"{vm.vm_id}: not all dota pids known, block MM/planner: {account_pids}"
                )
                continue

            if len(set(int(x) for x in account_pids)) != expected:
                self.logger.warning(
                    f"{vm.vm_id}: duplicate dota pids detected, block MM/planner: {account_pids}"
                )
                continue

            if len(set(vm.dota_hwnds)) != expected:
                self.logger.warning(
                    f"{vm.vm_id}: vm.dota_hwnds mismatch, block planner: {vm.dota_hwnds}"
                )
                continue

            if vm.current_command_id is not None or vm.command_queue:
                continue

            if not vm.windows_arranged:
                self.arrange_dota_windows(vm)
                continue

            if self.mm_starter is not None:
                friend_ids = self._friend_ids_for_vm(vm)

                vm.status = VmStatus.MM_PREPARE

                mm_ready = self.mm_starter.tick_one(
                    vm_id=vm.vm_id,
                    hwnds=vm.dota_hwnds,
                    friend_ids=friend_ids,
                )

                mm_stage_after = self.mm_starter.get_stage(vm.vm_id)
                try:
                    detected_side = self.mm_starter.get_side(vm.vm_id)
                except AttributeError:
                    detected_side = None

                if detected_side in ("radiant", "dire"):
                    if vm.side != detected_side:
                        self.logger.info(
                            f"{vm.vm_id}: detected side={detected_side}, update vm state"
                        )
                    vm.side = detected_side

                if not mm_ready or mm_stage_after != "done":
                    self._log_mm_stage_waiting(vm.vm_id, mm_stage_after)
                    continue

            roles = vm.roles or (["unknown"] * len(vm.dota_hwnds))
            planner_runtime.attach_hwnds(
                vm_id=vm.vm_id,
                hwnds=vm.dota_hwnds,
                roles=roles,
                side=vm.side,
                logger=self.logger,
            )
            vm.planner_active = True
            vm.status = VmStatus.PLANNER_ACTIVE
            self.logger.info(f"Planner activated for {vm.vm_id}")

    # -----------------------------------------------------
    # vm logs / misc
    # -----------------------------------------------------

    def push_vm_log(
        self,
        vm_id: str,
        level: str,
        source: str,
        event: str,
        message: str,
        payload: Optional[dict] = None,
    ) -> None:
        with self._lock:
            vm = self.vms.get(vm_id)
            if vm:
                vm.last_log_ts = time.time()

        line = f"[VMLOG][{vm_id}][{source}][{event}] {message}"
        if payload:
            line += f" | payload={self._sanitize_log_payload(payload)}"

        lvl = (level or "info").lower()
        if lvl == "debug":
            self.logger.debug(line)
        elif lvl == "warning":
            self.logger.warning(line)
        elif lvl == "error":
            self.logger.error(line)
        else:
            self.logger.info(line)

    def _sanitize_log_payload(self, value: Any, *, max_string_len: int = 1000) -> Any:
        try:
            if isinstance(value, dict):
                out: dict[str, Any] = {}
                for key, nested in value.items():
                    key_s = str(key)
                    if key_s in {"image_b64", "frame_b64"}:
                        out[key_s] = f"<base64 {len(nested or '')} chars>"
                    elif key_s in {"raw", "bytes"} and isinstance(nested, (bytes, bytearray)):
                        out[key_s] = f"<bytes {len(nested)}>"
                    else:
                        out[key_s] = self._sanitize_log_payload(
                            nested,
                            max_string_len=max_string_len,
                        )
                return out
            if isinstance(value, list):
                return [
                    self._sanitize_log_payload(item, max_string_len=max_string_len)
                    for item in value[:50]
                ]
            if isinstance(value, tuple):
                return tuple(
                    self._sanitize_log_payload(item, max_string_len=max_string_len)
                    for item in value[:50]
                )
            if isinstance(value, str) and len(value) > max_string_len:
                return f"{value[:max_string_len]}...<truncated {len(value)} chars>"
            return value
        except Exception:
            return "<unserializable>"

    def mark_stale_vms(self, timeout_sec: float = 60.0) -> None:
        now = time.time()
        for vm in self.vms.values():
            if vm.last_ping_ts and (now - vm.last_ping_ts) > timeout_sec:
                vm.is_online = False
                if vm.status != VmStatus.OFFLINE:
                    vm.status = VmStatus.OFFLINE

    def mm_hours_stub(self, accounts: List[Account]) -> Dict[str, float]:
        return {acc.username: 0.0 for acc in accounts}

    def get_vm_rows(self) -> List[dict]:
        with self._lock:
            rows = []
            for vm in self.vms.values():
                mm_stage = ""
                if self.mm_starter is not None:
                    try:
                        mm_stage = self.mm_starter.get_stage(vm.vm_id)
                    except Exception:
                        mm_stage = ""

                rows.append(
                    {
                        "vm_id": vm.vm_id,
                        "status": vm.status,
                        "capacity": vm.capacity,
                        "accounts": len(vm.assigned_accounts),
                        "login_hwnds": len(vm.login_hwnds),
                        "dota_hwnds": len(vm.dota_hwnds),
                        "planner": vm.planner_active,
                        "queue": len(vm.command_queue),
                        "mm_stage": mm_stage,
                        "arranged": vm.windows_arranged,
                        "desktop": f"{vm.desktop_width}x{vm.desktop_height}",
                        "sizes": str(vm.dota_window_sizes),
                        "dota_pids": [
                            acc.dota_pid
                            for acc in vm.assigned_accounts
                            if acc.dota_pid is not None
                        ],
                    }
                )
            return rows


_CONTROLLER_SINGLETON: Optional[Controller] = None


def set_host_controller(controller: Controller) -> None:
    global _CONTROLLER_SINGLETON
    _CONTROLLER_SINGLETON = controller


def get_host_controller() -> Controller:
    if _CONTROLLER_SINGLETON is None:
        raise RuntimeError("HostController is not initialized")
    return _CONTROLLER_SINGLETON
