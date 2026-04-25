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

    popup_capture_requested: bool = False
    popup_dismiss_in_progress: bool = False
    popup_fail_count: int = 0
    last_popup_scan_ts: float = 0.0
    last_popup_match_name: Optional[str] = None

    dota_window_found: bool = False
    dota_hwnd: Optional[int] = None
    dota_find_in_progress: bool = False
    last_dota_find_ts: float = 0.0

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

        self.batch_size = 5
        self.steam_path = r"C:\Program Files (x86)\Steam\steam.exe"
        self.app_id = APP_ID_DOTA
        self.launch_opts = list(DOTA_LAUNCH_OPTS)

        # fallback values only
        self.screen_w = 1920
        self.screen_h = 1080
        self.dota_window_w = 820
        self.dota_window_h = 640

        self._steam_templates = self._load_steam_templates()
        self._desktop_frames: Dict[str, np.ndarray] = {}

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
        if not self.running:
            return

        try:
            self.ensure_runtime_ready()
            self.assign_batches_to_idle_vms()
            self.drive_vm_bootstrap()
            self.activate_planners_for_ready_vms()
            planner_runtime.tick_all()
            self.mark_stale_vms()
        except Exception as e:
            self.logger.error(f"controller.tick_one failed: {e}", exc_info=True)

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

    def _get_latest_frame_rgb(self, vm_id: str, hwnd: int) -> Optional[np.ndarray]:
        entry = planner_runtime.get_entry(vm_id)
        if entry is None:
            return None

        try:
            frame = entry.bridge.get_latest_frame(hwnd)
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

        return arr

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
        except Exception as e:
            self.logger.warning(f"{vm_id}: failed to decode desktop frame: {e}")

    def _find_desktop_steam_popup_match(
        self,
        vm_id: str,
        threshold: float = 0.88,
    ) -> Optional[dict[str, Any]]:
        frame_rgb = self._desktop_frames.get(vm_id)
        if frame_rgb is None or not self._steam_templates:
            return None

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

        return best

    # -----------------------------------------------------
    # persistence
    # -----------------------------------------------------

    def save_state(self) -> None:
        data = {
            "steam_path": self.steam_path,
            "accounts": [acc.to_state_dict() for acc in self.accounts],
            # Runtime VM/window state intentionally not saved.
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
        return self.vms.get(vm_id)

    def touch_vm(self, vm_id: str) -> None:
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
        vm = self.vms.get(vm_id)
        if vm is None:
            return None

        if vm.current_command_id is not None:
            for c in vm.command_queue:
                if c.id == vm.current_command_id and c.status in ("queued", "sent"):
                    c.status = "sent"
                    return {"id": c.id, "type": c.type, "payload": c.payload}

        for c in vm.command_queue:
            if c.status == "queued":
                c.status = "sent"
                vm.current_command_id = c.id
                return {"id": c.id, "type": c.type, "payload": c.payload}

        return None

    def ack_command(self, vm_id: str, command_id: int, status: str, result: Optional[dict]) -> bool:
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

                if self.mm_starter is not None:
                    try:
                        self.mm_starter.on_command_result(
                            vm_id=vm_id,
                            cmd_type=c.type,
                            payload=c.payload,
                            result=c.result or {},
                            status=c.status,
                        )
                    except AttributeError:
                        try:
                            self.mm_starter.mark_command_done(vm_id)
                        except Exception:
                            pass
                    except Exception as e:
                        self.logger.warning(f"{vm_id}: mm_starter.on_command_result failed: {e}")

                return True

        return False

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
                    self._desktop_frames.pop(vm.vm_id, None)
                    soft_failure = True

                elif cmd.type == HostCommandType.FIND_DOTA_WINDOW:
                    acc.dota_find_in_progress = False
                    soft_failure = True

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
                    if found and acc.login_hwnd is not None and acc.login_hwnd not in vm.login_hwnds:
                        vm.login_hwnds.append(acc.login_hwnd)

            elif cmd.type == HostCommandType.KEY_PRESS:
                if acc is not None:
                    vk_code = int(cmd.payload.get("vk_code", 0))
                    if vk_code == 0x0D:
                        acc.auth_started = True
                        acc.auth_done = True

            elif cmd.type == HostCommandType.CAPTURE_FRAME:
                if acc is not None:
                    if acc.auth_capture_requested:
                        acc.auth_capture_requested = False
                        acc.auth_capture_fail_count = 0
                        acc.auth_started = True
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
                    self._desktop_frames.pop(vm.vm_id, None)

            elif cmd.type == HostCommandType.FIND_DOTA_WINDOW:
                if acc is not None:
                    acc.dota_find_in_progress = False

                    found = bool((cmd.result or {}).get("found"))
                    hwnd = (cmd.result or {}).get("hwnd")
                    pid = (cmd.result or {}).get("pid")
                    source = (cmd.result or {}).get("source")

                    if found and hwnd is not None:
                        hwnd_i = int(hwnd)

                        owner = None
                        for other in vm.assigned_accounts:
                            if other.username != acc.username and other.dota_hwnd == hwnd_i:
                                owner = other.username
                                break

                        if owner is not None:
                            self.logger.warning(
                                f"{vm.vm_id}: rejected duplicate dota hwnd={hwnd_i} "
                                f"for {acc.username}, already owned by {owner}"
                            )
                            acc.dota_window_found = False
                            acc.dota_hwnd = None
                        else:
                            acc.dota_window_found = True
                            acc.dota_hwnd = hwnd_i

                            window_info = (cmd.result or {}).get("window_info") or {}
                            window_rect = window_info.get("window_rect") or {}
                            desktop = window_info.get("desktop") or {}

                            win_w = int(window_rect.get("width") or self.dota_window_w)
                            win_h = int(window_rect.get("height") or self.dota_window_h)

                            vm.dota_window_sizes[hwnd_i] = (win_w, win_h)

                            vm.desktop_width = int(desktop.get("width") or vm.desktop_width or self.screen_w)
                            vm.desktop_height = int(desktop.get("height") or vm.desktop_height or self.screen_h)

                            if acc.dota_hwnd not in vm.dota_hwnds:
                                vm.dota_hwnds.append(acc.dota_hwnd)

                            self.logger.info(
                                f"{vm.vm_id}: dota found -> {acc.username} "
                                f"hwnd={acc.dota_hwnd} pid={pid} source={source} "
                                f"outer={win_w}x{win_h} desktop={vm.desktop_width}x{vm.desktop_height}"
                            )
                    else:
                        acc.dota_window_found = False
                        acc.dota_hwnd = None

            elif cmd.type == HostCommandType.MOVE_WINDOW:
                pass

        vm.command_queue = [x for x in vm.command_queue if x.status not in ("done", "failed")]

    # -----------------------------------------------------
    # bootstrap scenario
    # -----------------------------------------------------

    def drive_vm_bootstrap(self) -> None:
        for vm in self.vms.values():
            if not vm.is_online or not vm.assigned_accounts or vm.planner_active:
                continue
            if vm.current_command_id is not None:
                continue

            acc = self._first_active_account(vm)
            if acc is None:
                vm.status = VmStatus.GAME_READY
                continue

            if self._has_inflight_for_account(vm, acc.username):
                continue

            # 1) strict sequential launch
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

            # 2) find login window
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
                        "timeout_ms": 30000,
                    },
                )
                acc.login_find_in_progress = True
                acc.last_login_find_ts = now
                vm.status = VmStatus.LOGIN
                self.logger.info(f"{vm.vm_id}: find_login_window -> {acc.username}")
                continue

            # 3) auth
            if not acc.auth_done:
                if acc.has_mafile:
                    now = time.time()
                    if acc.auth_capture_fail_count >= 3:
                        acc.login_window_found = False
                        acc.login_hwnd = None
                        acc.login_find_in_progress = False
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
                    HostCommandType.WRITE_TEXT,
                    {
                        "account_login": acc.username,
                        "hwnd": int(acc.login_hwnd),
                        "field": "login",
                        "text": acc.username,
                        "clear_before": True,
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

            # 4) wait Dota + continuously dismiss desktop-level popups
            if not acc.dota_window_found or acc.dota_hwnd is None:
                now = time.time()

                if acc.popup_dismiss_in_progress:
                    continue
                if acc.popup_capture_requested:
                    continue
                if acc.dota_find_in_progress:
                    continue

                match = self._find_desktop_steam_popup_match(vm.vm_id)

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

                if vm.vm_id not in self._desktop_frames or now - acc.last_popup_scan_ts >= 1.2:
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
                    continue

                if now - acc.last_dota_find_ts < 2.0:
                    continue

                self._push_command(
                    vm,
                    HostCommandType.FIND_DOTA_WINDOW,
                    {
                        "account_login": acc.username,
                        "timeout_ms": 2500,
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

        screen_w = int(vm.desktop_width or self.screen_w)
        screen_h = int(vm.desktop_height or self.screen_h)

        positions = self._build_window_grid_positions_dynamic(
            hwnds=vm.dota_hwnds,
            screen_w=screen_w,
            screen_h=screen_h,
            window_sizes=vm.dota_window_sizes,
        )

        for hwnd, (x, y) in positions.items():
            self._push_command(
                vm,
                HostCommandType.MOVE_WINDOW,
                {
                    "hwnd": int(hwnd),
                    "x": int(x),
                    "y": int(y),
                },
            )

        vm.windows_arrange_sent = True
        vm.windows_arranged = True

        self.logger.info(
            f"{vm.vm_id}: queued arrange for {len(vm.dota_hwnds)} dota windows "
            f"desktop={screen_w}x{screen_h} sizes={vm.dota_window_sizes}"
        )

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
            account_hwnds = [acc.dota_hwnd for acc in vm.assigned_accounts if acc.dota_hwnd is not None]
            unique_hwnds = set(int(x) for x in account_hwnds)

            if len(account_hwnds) != expected:
                continue

            if len(unique_hwnds) != expected:
                self.logger.warning(
                    f"{vm.vm_id}: duplicate dota hwnds detected, block planner: {account_hwnds}"
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
                if not mm_ready:
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
        vm = self.vms.get(vm_id)
        if vm:
            vm.last_log_ts = time.time()

        line = f"[VMLOG][{vm_id}][{source}][{event}] {message}"
        if payload:
            line += f" | payload={payload}"

        lvl = (level or "info").lower()
        if lvl == "debug":
            self.logger.debug(line)
        elif lvl == "warning":
            self.logger.warning(line)
        elif lvl == "error":
            self.logger.error(line)
        else:
            self.logger.info(line)

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