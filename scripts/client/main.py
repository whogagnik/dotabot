# scripts/client/main.py
from __future__ import annotations

import os
import json
import sys
import time
import traceback
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import requests
import win32api
import win32gui

from api_client import PlannerApiClient
from capture import DotaCapture
from executor import CommandExecutor


HOST_URL = "http://192.168.217.1:8000"
DEBUG = True
# A rotating pair contains each of five windows twice per ten captures;
# 25 captures/sec therefore preserves 5 planner frames/sec for every window.
# Dota rendering remains at 60 FPS and is independent of this transport rate.
PLANNER_TOTAL_FPS = 25.0
PLANNER_F1_INTERVAL_SEC = 1.0
PLANNER_F1_HOLD_MS = 100
# Planner runs at 5 FPS; the game runs at 60 FPS. The short activation
# pause is independent of planner cadence.
PLANNER_FOCUS_SETTLE_MS = 30
VK_F1 = 0x70
# The planner needs frames for the current client and the one immediately
# after it.  Uploading five desktop frames in parallel causes needless CPU
# contention and makes control lag behind the visible state.
PLANNER_FRAME_WINDOW_COUNT = 2
PLANNER_MAX_INFLIGHT_UPLOADS = 2


class VmClient:
    """
    Thin agent:
    - регистрируется на host
    - получает vm_id
    - шлёт логи
    - получает команды
    - исполняет команды
    - подтверждает результат
    - шлёт raw frame только когда host прислал команду capture_frame
    """

    def __init__(self):
        self.api = PlannerApiClient(
            base_url=HOST_URL,
            timeout=15.0,
            debug=DEBUG,
        )
        self.capture = DotaCapture()
        self.executor = CommandExecutor(capture=self.capture, api=self.api)
        self._running = True
        self._bootstrap_backoff_sec = 1.0
        self._loop_error_backoff_sec = 0.2
        self._last_local_error_message = ""
        self._last_local_error_ts = 0.0
        self._planner_capture_index = 0
        self._planner_frame_window_index = 0
        self._planner_command_index = 0
        self._planner_f1_index = 0
        self._planner_last_f1_action_ts = 0.0
        self._planner_last_command_hwnd: int | None = None
        self._planner_last_command_ts = 0.0
        self._planner_timing_window_started = time.monotonic()
        self._planner_timing_by_hwnd: dict[int, list[dict[str, float]]] = {}
        self._planner_generation_seen = 0
        self._planner_transport_thread: threading.Thread | None = None
        self._planner_transport_started = False
        self._planner_command_thread: threading.Thread | None = None
        self._planner_command_started = False
        self._planner_f1_thread: threading.Thread | None = None
        self._planner_f1_started = False
        self._planner_input_lock = threading.Lock()
        self._planner_last_focused_hwnd: int | None = None
        self._planner_last_f1_ts_by_hwnd: dict[int, float] = {}
        self._planner_upload_pool = ThreadPoolExecutor(
            max_workers=PLANNER_MAX_INFLIGHT_UPLOADS,
            thread_name_prefix="planner-frame-upload",
        )
        self._planner_uploads: dict[Future, tuple[int, float]] = {}
        base_dir = (
            os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))
        )
        self._planner_click_log_path = os.path.join(base_dir, "logs", "planner_clicks.jsonl")
        self._planner_click_log_lock = threading.Lock()

    @staticmethod
    def _planner_window_order(hwnds: list[int]) -> list[int]:
        """Preserve launcher order; HWND values themselves are unordered."""
        return list(dict.fromkeys(map(int, hwnds)))

    def _planner_frame_hwnds(self, hwnds: list[int]) -> list[int]:
        """Return a strictly rotating pair: current window and its successor."""
        ordered = self._planner_window_order(hwnds)
        if not ordered:
            return []
        # Frame selection must never be based on the most recent command:
        # doing so feeds the same window again after every click and creates
        # an endless command stream for that one client.
        start = getattr(self, "_planner_frame_window_index", 0) % len(ordered)
        return [
            ordered[(start + offset) % len(ordered)]
            for offset in range(min(PLANNER_FRAME_WINDOW_COUNT, len(ordered)))
        ]

    def _submit_planner_frame(
        self,
        hwnd: int,
        frame,
        generation: int,
    ) -> float:
        started = time.monotonic()
        self.api.submit_frame_raw(
            hwnd=hwnd,
            frame_rgb=frame,
            planner_generation=generation,
        )
        return (time.monotonic() - started) * 1000.0

    # ---------------------------------------------------------
    # logging
    # ---------------------------------------------------------

    def _save_planner_click_log(
        self,
        *,
        status: str,
        command_id: int,
        hwnd: int,
        command_type: str,
        payload: dict,
        result: object = None,
        error: str | None = None,
        input_context: dict | None = None,
    ) -> None:
        """Persist planner and regular host mouse actions for VM-side debugging."""
        record = {
            "ts": time.time(),
            "status": status,
            "command_id": int(command_id),
            "hwnd": int(hwnd),
            "type": str(command_type),
            "payload": payload,
            "result": result,
            "error": error,
            "input_context": input_context or {},
        }
        try:
            with self._planner_click_log_lock:
                os.makedirs(os.path.dirname(self._planner_click_log_path), exist_ok=True)
                if os.path.exists(self._planner_click_log_path) and os.path.getsize(
                    self._planner_click_log_path
                ) >= 10 * 1024 * 1024:
                    backup = self._planner_click_log_path + ".1"
                    if os.path.exists(backup):
                        os.remove(backup)
                    os.replace(self._planner_click_log_path, backup)
                with open(self._planner_click_log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as log_error:
            self.local_log(
                "warning",
                "planner_click_log_failed",
                "Could not save planner click log",
                payload={"error": str(log_error)},
                min_interval_sec=5.0,
            )

    def log(self, level: str, event: str, message: str, payload: dict | None = None) -> None:
        try:
            if self.api.vm_id:
                self.api.send_log(
                    level=level,
                    source="client",
                    event=event,
                    message=message,
                    payload=payload or {},
                )
            else:
                print(f"[CLIENT][{level.upper()}][{event}] {message} | payload={payload}", flush=True)
        except Exception as e:
            if self._should_reset_registration(e):
                self.api.reset_registration()
            self.local_log(
                "warning",
                "log_failed",
                f"Could not send {event} log",
                payload={"error": str(e)},
                min_interval_sec=5.0,
            )

    def local_log(
        self,
        level: str,
        event: str,
        message: str,
        payload: dict | None = None,
        *,
        min_interval_sec: float = 2.0,
    ) -> None:
        now = time.time()
        key = f"{level}:{event}:{message}"
        if key == self._last_local_error_message and now - self._last_local_error_ts < min_interval_sec:
            return

        self._last_local_error_message = key
        self._last_local_error_ts = now
        print(f"[CLIENT][{level.upper()}][{event}] {message} | payload={payload}", flush=True)

    def _should_reset_registration(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {404, 503}:
            return True

        return isinstance(
            exc,
            requests.exceptions.ConnectionError,
        )

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def bootstrap(self) -> None:
        resp = self.api.register_vm()
        self._start_planner_frame_transport()
        self._bootstrap_backoff_sec = 1.0
        self._loop_error_backoff_sec = 0.2
        self.log(
            "info",
            "vm_registered",
            "VM registered on host",
            payload=resp,
        )

    def bootstrap_once(self) -> bool:
        try:
            self.bootstrap()
            return True
        except Exception as e:
            self.api.reset_registration()
            self.local_log(
                "warning",
                "bootstrap_failed",
                f"Host is not ready, retry in {self._bootstrap_backoff_sec:.1f}s",
                payload={"error": str(e)},
                min_interval_sec=5.0,
            )
            time.sleep(self._bootstrap_backoff_sec)
            self._bootstrap_backoff_sec = min(15.0, self._bootstrap_backoff_sec * 1.7)
            return False

    # ---------------------------------------------------------
    # command execution
    # ---------------------------------------------------------

    def _execute_command(self, command: dict) -> dict:
        # Regular host commands share the same desktop with planner and F1.
        with self._planner_input_lock:
            command_type = str(command.get("type", ""))
            payload = dict(command.get("payload") or {})
            is_click = command_type in {"mouse_click", "attack_click", "dismiss_steam_popups"}
            is_click = is_click or (command_type == "key_press" and payload.get("vk_code") == 27)
            result = None
            error = None
            try:
                result = self.executor.execute(command)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                if is_click:
                    self._save_planner_click_log(
                        status="failed" if error is not None else "executed",
                        command_id=int(command.get("id", 0)),
                        hwnd=int(payload.get("hwnd") or 0),
                        command_type=command_type,
                        payload=payload,
                        result=result,
                        error=error,
                        input_context={"source": "host_command"},
                    )

    def _start_planner_frame_transport(self) -> None:
        if self._planner_transport_started:
            return
        self._planner_transport_started = True
        self._planner_transport_thread = threading.Thread(
            target=self._planner_frame_transport_loop,
            name="planner-frame-transport",
            daemon=True,
        )
        self._planner_transport_thread.start()
        if not self._planner_command_started:
            self._planner_command_started = True
            self._planner_command_thread = threading.Thread(
                target=self._planner_command_loop,
                name="planner-command-worker",
                daemon=True,
            )
            self._planner_command_thread.start()

    def _planner_command_loop(self) -> None:
        """Keep the planner round-robin independent of regular host commands."""
        while self._running:
            if self.api.planner_active:
                try:
                    self._tick_planner_transport()
                except Exception as error:
                    self.local_log(
                        "warning",
                        "planner_command_poll_failed",
                        "Planner command poll failed; continuing the window cycle",
                        payload={"error": str(error)},
                        min_interval_sec=2.0,
                    )
            time.sleep(0.05)

    def _planner_frame_transport_loop(self) -> None:
        """Upload newest DXcam frames without delaying planner commands."""
        next_capture_ts = 0.0
        window_started = time.monotonic()
        frames_sent = 0
        frames_captured = 0
        capture_ms_total = 0.0
        upload_ms_total = 0.0

        while self._running:
            # Uploads may take ~250 ms. Reap completed ones without blocking
            # DXcam; the bounded pool prevents old frames from accumulating.
            for future, (uploaded_hwnd, _submitted_ts) in list(self._planner_uploads.items()):
                if not future.done():
                    continue
                self._planner_uploads.pop(future, None)
                try:
                    upload_ms_total += float(future.result())
                    frames_sent += 1
                except Exception as error:
                    self.local_log(
                        "warning",
                        "planner_frame_upload_failed",
                        "Planner frame upload failed",
                        payload={"hwnd": uploaded_hwnd, "error": str(error)},
                        min_interval_sec=2.0,
                    )

            now = time.monotonic()
            hwnds = self.executor.known_dota_hwnds()
            # During login/matchmaking capture_frame owns DXcam.  Starting
            # the live planner uploader earlier races it and produces ACKs
            # with no image for MM.
            if not self.api.vm_id or not self.api.planner_active or not hwnds:
                self._planner_last_focused_hwnd = None
                self._planner_last_f1_ts_by_hwnd.clear()
                self._planner_f1_index = 0
                self._planner_frame_window_index = 0
                self._planner_capture_index = 0
                time.sleep(0.05)
                continue

            generation = int(self.api.planner_generation)
            if generation != self._planner_generation_seen:
                # The first DXcam image after a planner transition may still
                # describe the previous desktop surface.  Discard it.
                self._planner_generation_seen = generation
                self._planner_last_focused_hwnd = None
                self._planner_last_f1_ts_by_hwnd.clear()
                self._planner_f1_index = 0
                self._planner_frame_window_index = 0
                self._planner_capture_index = 0
                next_capture_ts = now + 1.0 / PLANNER_TOTAL_FPS
                time.sleep(0.05)
                continue

            if now < next_capture_ts:
                time.sleep(min(0.01, next_capture_ts - now))
                continue

            # Backpressure must not consume a window's turn. Previously we
            # advanced even with a full pool, repeatedly dropping some HWNDs.
            if len(self._planner_uploads) >= PLANNER_MAX_INFLIGHT_UPLOADS:
                time.sleep(0.01)
                continue

            # Keep capture near the client currently receiving commands:
            # current window plus one window ahead, in account order.
            frame_hwnds = self._planner_frame_hwnds(hwnds)
            hwnd = frame_hwnds[self._planner_capture_index % len(frame_hwnds)]
            self._planner_capture_index += 1
            if self._planner_capture_index % len(frame_hwnds) == 0:
                self._planner_frame_window_index = (
                    self._planner_frame_window_index + 1
                ) % len(self._planner_window_order(hwnds))
            capture_interval = 1.0 / PLANNER_TOTAL_FPS
            next_capture_ts += capture_interval
            if next_capture_ts < now:
                next_capture_ts = now + capture_interval

            try:
                capture_started = time.monotonic()
                frame = self.capture.grab_window_rgb(hwnd)
                capture_ms_total += (time.monotonic() - capture_started) * 1000.0
                if frame is not None:
                    frames_captured += 1
                    if len(self._planner_uploads) < PLANNER_MAX_INFLIGHT_UPLOADS:
                        future = self._planner_upload_pool.submit(
                            self._submit_planner_frame,
                            int(hwnd),
                            frame,
                            generation,
                        )
                        self._planner_uploads[future] = (int(hwnd), now)
            except Exception as error:
                self.local_log(
                    "warning",
                    "planner_frame_upload_failed",
                    "Planner frame upload failed",
                    payload={"hwnd": hwnd, "error": str(error)},
                    min_interval_sec=2.0,
                )

            elapsed = now - window_started
            if elapsed >= 1.0:
                self.local_log(
                    "debug",
                    "planner_transport_rate",
                    "Planner frame transport",
                    payload={
                        "fps": round(frames_sent / elapsed, 1),
                        "frames": frames_sent,
                        "captured": frames_captured,
                        "inflight": len(self._planner_uploads),
                        "windows": len(frame_hwnds),
                        "capture_ms_avg": round(capture_ms_total / max(1, frames_captured), 1),
                        "upload_ms_avg": round(upload_ms_total / max(1, frames_sent), 1),
                    },
                    min_interval_sec=0.0,
                )
                window_started = now
                frames_sent = 0
                frames_captured = 0
                capture_ms_total = 0.0
                upload_ms_total = 0.0

    def _tick_client_f1(self, hwnds: list[int]) -> dict[str, float]:
        """Refresh one camera at a time, following the same window order."""
        now = time.monotonic()
        ordered = self._planner_window_order(hwnds)
        if not ordered:
            return {"focus_ms": 0.0, "f1_ms": 0.0}
        hwnd = ordered[0]
        focus_ms = 0.0
        f1_ms = 0.0
        self._planner_input_lock.acquire()
        try:
            started = time.monotonic()
            self.executor.focus_window(
                {"hwnd": hwnd, "settle_ms": PLANNER_FOCUS_SETTLE_MS,
                 "center_cursor": True, "fast_if_visible": True}
            )
            focus_ms = (time.monotonic() - started) * 1000.0
            started = time.monotonic()
            for _ in range(2):
                self.executor.key_press(
                    {
                        "hwnd": hwnd,
                        "vk_code": VK_F1,
                        "hold_ms": PLANNER_F1_HOLD_MS,
                        "allow_short_hold": True,
                        "force_fg": False,
                    }
                )
            f1_ms = (time.monotonic() - started) * 1000.0
            self._planner_last_focused_hwnd = hwnd
            self._planner_last_f1_ts_by_hwnd[hwnd] = now
            self._planner_last_f1_action_ts = now
            self._planner_f1_index = (self._planner_f1_index + 1) % len(ordered)
        except Exception as error:
            self.local_log(
                "warning",
                "planner_f1_failed",
                "Client-side planner F1 failed",
                payload={"hwnd": hwnd, "error": str(error)},
                min_interval_sec=2.0,
            )
        finally:
            self._planner_input_lock.release()
        return {"focus_ms": focus_ms, "f1_ms": f1_ms}

    def _planner_f1_loop(self) -> None:
        """Run periodic F1 independently from command polling."""
        while self._running:
            if self.api.planner_active:
                hwnds = self.executor.known_dota_hwnds()
                if hwnds:
                    self._tick_client_f1(hwnds)
            time.sleep(0.01)

    def _tick_planner_transport(self) -> None:
        """Send live Dota frames and execute planner input commands on the VM."""
        if not self.api.planner_active:
            self._planner_last_focused_hwnd = None
            self._planner_last_f1_ts_by_hwnd.clear()
            self._planner_command_index = 0
            self._planner_f1_index = 0
            return

        hwnds = self._planner_window_order(self.executor.known_dota_hwnds())
        if not hwnds:
            return

        # Poll exactly one queue on each tick.  The index advances even when
        # a queue is empty, so actions can only be processed in launcher
        # order: first window -> second -> third -> ... -> first.
        command_hwnd = hwnds[self._planner_command_index % len(hwnds)]
        self._planner_command_index = (self._planner_command_index + 1) % len(hwnds)
        turn_started = time.monotonic()
        # Visit every window, including empty queues. F1 cannot be starved by
        # a continuous stream of mouse commands on the other windows.
        f1_timing = self._tick_client_f1([command_hwnd])
        poll_started = time.monotonic()
        for command_hwnd in (command_hwnd,):
            # One window may receive all of its keyboard updates in this
            # turn, but it may issue only one mouse click before control
            # moves to the next account window.
            commands = self.api.get_planner_commands(command_hwnd, limit=4)
            poll_ms = (time.monotonic() - poll_started) * 1000.0
            if not commands:
                continue

            mouse_clicks_processed = 0
            for command in commands:
                command_id = int(command["id"])
                command_type = str(command.get("type") or "")
                is_mouse_click = command_type in {"mouse_click", "attack_click"}
                if is_mouse_click and mouse_clicks_processed >= 1:
                    break
                execution_failed = False
                input_locked = False
                input_context: dict = {}
                try:
                    self._planner_input_lock.acquire()
                    input_locked = True
                    command_payload = dict(command.get("payload") or {})
                    payload_hwnd = command_payload.get("hwnd", command_hwnd)
                    if int(payload_hwnd) != int(command_hwnd):
                        raise RuntimeError(
                            f"planner queue hwnd={command_hwnd} differs from payload hwnd={payload_hwnd}"
                        )
                    # Ask Windows which HWND is actually foreground. Centre
                    # only on a real switch; repeated centring between
                    # move -> key -> click would erase the planned position.
                    try:
                        active_hwnd = int(win32gui.GetForegroundWindow())
                    except Exception:
                        active_hwnd = 0
                    switched_window = active_hwnd != int(command_hwnd)
                    input_context["foreground_before"] = active_hwnd
                    input_context["switched_window"] = switched_window
                    if switched_window:
                        focus_result = self.executor.focus_window(
                            {
                                "hwnd": int(command_hwnd),
                                "settle_ms": PLANNER_FOCUS_SETTLE_MS,
                                "center_cursor": True,
                                "fast_if_visible": True,
                            }
                        )
                        input_context["focus_result"] = focus_result
                        self._planner_last_focused_hwnd = int(command_hwnd)
                    try:
                        input_context["foreground_after_focus"] = int(
                            win32gui.GetForegroundWindow()
                        )
                        cursor_x, cursor_y = win32api.GetCursorPos()
                        input_context["cursor_after_focus"] = [
                            int(cursor_x),
                            int(cursor_y),
                        ]
                    except Exception as input_state_error:
                        input_context["input_state_error"] = str(input_state_error)

                    actual_hwnd = int(win32gui.GetForegroundWindow())
                    if actual_hwnd != int(command_hwnd):
                        raise RuntimeError(
                            f"planner foreground mismatch: expected hwnd={command_hwnd}, actual hwnd={actual_hwnd}"
                        )
                    command_payload["hwnd"] = int(command_hwnd)
                    # Focus was just established above. Avoid a duplicate
                    # activation inside the low-level executor method.
                    if command["type"] in {
                        "mouse_click",
                        "key_press",
                        "key_event",
                        "hotkey",
                        "attack_click",
                    }:
                        command_payload["force_fg"] = False
                    result = self.executor.execute(
                        {"type": command["type"], "payload": command_payload}
                    )
                    if is_mouse_click:
                        mouse_clicks_processed += 1
                    self._planner_last_command_hwnd = int(command_hwnd)
                    self._planner_last_command_ts = time.monotonic()
                    if command_type in {"mouse_click", "attack_click"}:
                        self._save_planner_click_log(
                            status="executed",
                            command_id=command_id,
                            hwnd=command_hwnd,
                            command_type=command_type,
                            payload=command_payload,
                            result=result,
                            input_context=input_context,
                        )
                    self.log(
                        "debug",
                        "planner_command_executed",
                        f"Planner command executed: {command.get('type')}",
                        payload={"command_id": command_id, "hwnd": command_hwnd, "result": result},
                    )
                except Exception as error:
                    execution_failed = True
                    if command_type in {"mouse_click", "attack_click"}:
                        self._save_planner_click_log(
                            status="failed",
                            command_id=command_id,
                            hwnd=command_hwnd,
                            command_type=command_type,
                            payload=dict(command.get("payload") or {}),
                            error=str(error),
                            input_context=input_context,
                        )
                    self.log(
                        "warning",
                        "planner_command_failed",
                        f"Planner command failed: {command.get('type')}",
                        payload={"command_id": command_id, "hwnd": command_hwnd, "error": str(error)},
                    )
                finally:
                    if input_locked:
                        self._planner_input_lock.release()
                    acknowledged = self.api.ack_planner_command(command_hwnd, command_id)
                    if not acknowledged:
                        self.log(
                            "warning",
                            "planner_command_ack_rejected",
                            "Planner command acknowledgement was rejected",
                            payload={"command_id": command_id, "hwnd": command_hwnd},
                        )
                        # The queue head changed or was reset. Do not execute
                        # another command from this stale response.
                        break
                if execution_failed:
                    # A failed focus/click invalidates the current queue head.
                    break

        sample = {
            "focus_ms": float(f1_timing.get("focus_ms", 0.0)) if isinstance(f1_timing, dict) else 0.0,
            "f1_ms": float(f1_timing.get("f1_ms", 0.0)) if isinstance(f1_timing, dict) else 0.0,
            "poll_ms": poll_ms,
            "command_ms": max(0.0, (time.monotonic() - poll_started) * 1000.0 - poll_ms),
            "total_ms": (time.monotonic() - turn_started) * 1000.0,
        }
        self._planner_timing_by_hwnd.setdefault(command_hwnd, []).append(sample)
        if time.monotonic() - self._planner_timing_window_started >= 5.0:
            summary = {
                str(hwnd): {
                    "turns": len(samples),
                    **{key: {"avg": round(sum(s[key] for s in samples) / len(samples), 1),
                             "max": round(max(s[key] for s in samples), 1)}
                       for key in sample}
                }
                for hwnd, samples in self._planner_timing_by_hwnd.items() if samples
            }
            self.local_log("info", "planner_turn_timing", "Planner window timings (ms)",
                           payload=summary, min_interval_sec=0.0)
            self._planner_timing_by_hwnd.clear()
            self._planner_timing_window_started = time.monotonic()

    # ---------------------------------------------------------
    # loop
    # ---------------------------------------------------------

    def tick_one(self) -> None:
        cmd = self.api.get_command()
        if cmd is None:
            time.sleep(0.05)
            return

        cmd_id = int(cmd["id"])
        cmd_type = str(cmd["type"])

        self.log(
            "debug",
            "command_received",
            f"Received command: {cmd_type}",
            payload={"command_id": cmd_id},
        )

        try:
            result = self._execute_command(cmd)
        except Exception as e:
            err = {
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

            try:
                self.api.ack_command(
                    command_id=cmd_id,
                    status="failed",
                    result=err,
                )
            except Exception as ack_error:
                self.local_log(
                    "warning",
                    "ack_failed",
                    f"Could not report failed command: {cmd_type}",
                    payload={"command_id": cmd_id, "error": str(ack_error)},
                    min_interval_sec=3.0,
                )

            self.log(
                "error",
                "command_failed",
                f"Command failed: {cmd_type}",
                payload={"command_id": cmd_id, **err},
            )
            time.sleep(0.1)
            return

        try:
            self.api.ack_command(
                command_id=cmd_id,
                status="done",
                result=result,
            )
        except Exception as e:
            self.local_log(
                "warning",
                "ack_failed",
                f"Command executed but ack failed: {cmd_type}",
                payload={"command_id": cmd_id, "error": str(e)},
                min_interval_sec=3.0,
            )
            raise

        try:
            self.log(
                "debug",
                "command_done",
                f"Command executed: {cmd_type}",
                payload={"command_id": cmd_id, "result": result},
            )
        except Exception:
            pass

    def run(self) -> None:
        while self._running:
            if not self.api.vm_id:
                if not self.bootstrap_once():
                    continue
                self.log("info", "client_started", "Client loop started")

            try:
                self.tick_one()
                self._loop_error_backoff_sec = 0.2
            except Exception as e:
                err = traceback.format_exc()

                if self._should_reset_registration(e):
                    self.api.reset_registration()

                self.local_log(
                    "error",
                    "client_tick_failed",
                    f"Unhandled exception in client loop, retry in {self._loop_error_backoff_sec:.1f}s",
                    payload={"traceback": err},
                    min_interval_sec=3.0,
                )
                time.sleep(self._loop_error_backoff_sec)
                self._loop_error_backoff_sec = min(10.0, self._loop_error_backoff_sec * 1.7)


def main():
    client = VmClient()
    client.run()


if __name__ == "__main__":
    main()
