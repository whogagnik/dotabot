from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Optional, Dict, Any, Deque

import numpy as np
from PIL import Image


@dataclass
class InMemoryFrame:
    frame_id: int
    rgb: np.ndarray
    ts_client: float


class DjangoPlannerBridge:
    """
    На каждый hwnd держим:
    - processing_frame: кадр, который сейчас обрабатывается planner / уже обработан, но его команды ещё не завершены
    - latest_frame: самый свежий присланный кадр, ожидающий обработки

    Старые latest-кадры всегда затираются.
    """

    def __init__(self, vm_id: str):
        self.vm_id = str(vm_id)
        self._lock = RLock()

        self._next_frame_id: int = 1
        self._next_command_id: int = 1

        self._processing_frame_by_hwnd: Dict[int, Optional[InMemoryFrame]] = {}
        self._latest_frame_by_hwnd: Dict[int, Optional[InMemoryFrame]] = {}

        self._commands_by_hwnd: Dict[int, Deque[dict]] = defaultdict(deque)

    # ------------------------------------------------------------------
    # frame lifecycle
    # ------------------------------------------------------------------

    def _make_frame(self, frame_rgb: np.ndarray, ts_client: float) -> InMemoryFrame:
        if frame_rgb.dtype != np.uint8:
            frame_rgb = frame_rgb.astype(np.uint8, copy=False)
        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            raise ValueError(f"Expected RGB HWC uint8 frame, got {frame_rgb.shape}")

        frame = InMemoryFrame(
            frame_id=self._next_frame_id,
            rgb=np.ascontiguousarray(frame_rgb.copy()),
            ts_client=float(ts_client),
        )
        self._next_frame_id += 1
        return frame

    def store_frame_rgb(self, *, hwnd: int, frame_rgb: np.ndarray, ts_client: float) -> int:
        """
        Новый кадр всегда становится latest_frame.
        Если там уже был старый latest_frame — он затирается.
        processing_frame не трогаем.
        """
        hwnd = int(hwnd)

        with self._lock:
            frame = self._make_frame(frame_rgb, ts_client)
            self._latest_frame_by_hwnd[hwnd] = frame
            return frame.frame_id

    def get_processing_frame_id(self, hwnd: int) -> Optional[int]:
        hwnd = int(hwnd)
        with self._lock:
            frame = self._processing_frame_by_hwnd.get(hwnd)
            return None if frame is None else frame.frame_id

    def get_latest_frame_id(self, hwnd: int) -> Optional[int]:
        hwnd = int(hwnd)
        with self._lock:
            frame = self._latest_frame_by_hwnd.get(hwnd)
            return None if frame is None else frame.frame_id

    def has_pending_commands(self, hwnd: int) -> bool:
        hwnd = int(hwnd)
        with self._lock:
            q = self._commands_by_hwnd.get(hwnd)
            return bool(q)

    def acquire_frame_for_processing(self, hwnd: int) -> Optional[int]:
        """
        Возвращает frame_id кадра, который надо использовать planner'у сейчас.

        Логика:
        - если processing_frame уже есть, возвращаем его
        - иначе берём latest_frame и переносим в processing_frame
        """
        hwnd = int(hwnd)

        with self._lock:
            processing = self._processing_frame_by_hwnd.get(hwnd)
            if processing is not None:
                return processing.frame_id

            latest = self._latest_frame_by_hwnd.get(hwnd)
            if latest is None:
                return None

            self._processing_frame_by_hwnd[hwnd] = latest
            self._latest_frame_by_hwnd[hwnd] = None
            return latest.frame_id

    def release_processing_frame_if_done(self, hwnd: int) -> bool:
        """
        Освобождаем processing_frame только если для hwnd больше нет команд.
        """
        hwnd = int(hwnd)

        with self._lock:
            q = self._commands_by_hwnd.get(hwnd)
            if q:
                return False

            if self._processing_frame_by_hwnd.get(hwnd) is not None:
                self._processing_frame_by_hwnd[hwnd] = None
                return True

            return False

    # ------------------------------------------------------------------
    # frame getters
    # ------------------------------------------------------------------

    def get_frame_rgb(self, hwnd: int, frame_id: int) -> Optional[np.ndarray]:
        hwnd = int(hwnd)
        frame_id = int(frame_id)

        with self._lock:
            processing = self._processing_frame_by_hwnd.get(hwnd)
            if processing is not None and processing.frame_id == frame_id:
                return processing.rgb.copy()

            latest = self._latest_frame_by_hwnd.get(hwnd)
            if latest is not None and latest.frame_id == frame_id:
                return latest.rgb.copy()

            return None

    def get_frame_pil(self, hwnd: int, frame_id: int) -> Optional[Image.Image]:
        rgb = self.get_frame_rgb(hwnd, frame_id)
        if rgb is None:
            return None
        return Image.fromarray(rgb, mode="RGB")

    def get_frame_ts(self, hwnd: int, frame_id: int) -> Optional[float]:
        hwnd = int(hwnd)
        frame_id = int(frame_id)

        with self._lock:
            processing = self._processing_frame_by_hwnd.get(hwnd)
            if processing is not None and processing.frame_id == frame_id:
                return processing.ts_client

            latest = self._latest_frame_by_hwnd.get(hwnd)
            if latest is not None and latest.frame_id == frame_id:
                return latest.ts_client

            return None

    # ------------------------------------------------------------------
    # planner compatibility API
    # ------------------------------------------------------------------

    def get_last_frame_id(self, hwnd: int) -> Optional[int]:
        """
        Для planner: получить frame_id кадра, который сейчас надо использовать.
        """
        return self.acquire_frame_for_processing(hwnd)

    def get_last_frame(self, hwnd: int):
        frame_id = self.acquire_frame_for_processing(hwnd)
        if frame_id is None:
            return None
        return self.get_frame_pil(hwnd, frame_id)

    def get_last_frame_ts(self, hwnd: int) -> Optional[float]:
        frame_id = self.acquire_frame_for_processing(hwnd)
        if frame_id is None:
            return None
        return self.get_frame_ts(hwnd, frame_id)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def push_command(
        self,
        hwnd: int,
        command_type: str,
        payload: Dict[str, Any],
        frame_id: Optional[int] = None,
    ) -> dict:
        hwnd = int(hwnd)

        with self._lock:
            if frame_id is None:
                processing = self._processing_frame_by_hwnd.get(hwnd)
                if processing is None:
                    raise RuntimeError(f"No processing frame for hwnd={hwnd}")
                frame_id = processing.frame_id

            cmd = {
                "id": self._next_command_id,
                "frame_id": int(frame_id),
                "type": str(command_type),
                "payload": dict(payload),
            }
            self._next_command_id += 1
            self._commands_by_hwnd[hwnd].append(cmd)
            return cmd

    def get_next_command(self, hwnd: int) -> Optional[dict]:
        hwnd = int(hwnd)
        with self._lock:
            q = self._commands_by_hwnd.get(hwnd)
            if not q:
                return None
            return dict(q[0])

    def ack_command(self, command_id: int, hwnd: int) -> bool:
        hwnd = int(hwnd)
        command_id = int(command_id)

        with self._lock:
            q = self._commands_by_hwnd.get(hwnd)
            if not q:
                return False

            if int(q[0]["id"]) != command_id:
                return False

            q.popleft()

            if not q:
                self._commands_by_hwnd.pop(hwnd, None)

        self.release_processing_frame_if_done(hwnd)
        return True

    # ------------------------------------------------------------------
    # debug / admin helpers
    # ------------------------------------------------------------------

    def clear_hwnd_state(self, hwnd: int) -> None:
        hwnd = int(hwnd)
        with self._lock:
            self._processing_frame_by_hwnd.pop(hwnd, None)
            self._latest_frame_by_hwnd.pop(hwnd, None)
            self._commands_by_hwnd.pop(hwnd, None)

    def dump_hwnd_state(self, hwnd: int) -> dict:
        hwnd = int(hwnd)
        with self._lock:
            processing = self._processing_frame_by_hwnd.get(hwnd)
            latest = self._latest_frame_by_hwnd.get(hwnd)
            q = list(self._commands_by_hwnd.get(hwnd, ()))

            return {
                "hwnd": hwnd,
                "processing_frame_id": None if processing is None else processing.frame_id,
                "latest_frame_id": None if latest is None else latest.frame_id,
                "queued_commands": len(q),
                "command_ids": [int(x["id"]) for x in q],
            }