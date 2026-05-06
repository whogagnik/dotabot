# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import queue
import tkinter as tk


class GuiHandler(logging.Handler):
    def __init__(
        self,
        text_widget: tk.Text,
        flush_interval_ms: int = 100,
        max_batch: int = 200,
        max_queue_size: int = 5000,
    ):
        super().__init__()
        self.text_widget = text_widget
        self.flush_interval_ms = int(flush_interval_ms)
        self.max_batch = int(max_batch)
        self.max_queue_size = int(max_queue_size)
        self._queue: queue.Queue[str] = queue.Queue(maxsize=self.max_queue_size)
        self._flush_scheduled = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return

        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            return
        except Exception:
            return

        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                self.text_widget.after(self.flush_interval_ms, self._flush)
            except Exception:
                self._flush_scheduled = False

    def _flush(self) -> None:
        self._flush_scheduled = False

        lines: list[str] = []
        for _ in range(self.max_batch):
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break
            except Exception:
                break

        if not lines:
            return

        try:
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", "\n".join(lines) + "\n")
            self.text_widget.see("end")

            # чтобы Text не разрастался бесконечно
            total_lines = int(self.text_widget.index("end-1c").split(".")[0])
            if total_lines > 3000:
                self.text_widget.delete("1.0", "500.0")

            self.text_widget.configure(state="disabled")
        except Exception:
            pass

        # если очередь ещё не пуста — планируем следующую пачку
        if not self._queue.empty():
            self._flush_scheduled = True
            try:
                self.text_widget.after(self.flush_interval_ms, self._flush)
            except Exception:
                self._flush_scheduled = False
