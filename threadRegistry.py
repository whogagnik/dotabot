import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Any
import logging

log = logging.getLogger(__name__)

@dataclass
class _Entry:
    thread: threading.Thread
    stop_event: Optional[threading.Event]  # если есть — реестр сможет мягко останавливать поток
    manage_stop: bool = True               # можно отключить сигнал stop_event для внешних потоков


class ThreadRegistry:
    """
    Реестр потоков:
      - add(name, target, ...)  -> создать поток, передавая target'у stop_event
      - set(name, thread, ...)  -> зарегистрировать уже созданный поток
      - get(name)               -> получить поток/entry
      - remove(name, ...)       -> остановить (если можем) и удалить из реестра
      - exit(...)               -> остановить и дождаться всех

    Паттерн для worker'а:
        def worker(stop_event: threading.Event, *args, **kwargs):
            while not stop_event.is_set():
                ...
    """

    def __init__(self, default_join_timeout: float = 5.0):
        self._lock = threading.RLock()
        self._entries: Dict[str, _Entry] = {}
        self._default_join_timeout = default_join_timeout

    # ---- контекст-менеджер ----
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exit()

    # ---- публичные методы ----
    def add(
        self,
        name: str,
        target: Callable[..., Any],
        args: tuple = (),
        kwargs: Optional[dict] = None,
        *,
        daemon: bool = True,
        start: bool = True
    ) -> threading.Thread:
        """
        Создаёт поток, target будет вызван как target(stop_event, *args, **kwargs).
        """
        kwargs = dict(kwargs or {})
        stop_event = threading.Event()

        def _runner():
            try:
                target(stop_event, *args, **kwargs)
            except Exception:  # не падаем молча
                log.exception("Unhandled exception in thread %r", name)

        t = threading.Thread(name=name, target=_runner, daemon=daemon)
        with self._lock:
            if name in self._entries:
                raise ValueError(f"Thread '{name}' already exists in registry")
            self._entries[name] = _Entry(thread=t, stop_event=stop_event, manage_stop=True)
        if start:
            t.start()
        return t

    def set(
        self,
        name: str,
        thread: threading.Thread,
        *,
        stop_event: Optional[threading.Event] = None,
        manage_stop: bool = True
    ) -> None:
        """
        Регистрирует уже созданный поток.
        Если передан stop_event и manage_stop=True — реестр сможет послать стоп-сигнал.
        """
        with self._lock:
            self._entries[name] = _Entry(thread=thread, stop_event=stop_event, manage_stop=manage_stop)

    def get(self, name: str, *, with_meta: bool = False):
        """
        Возвращает поток по имени. Если with_meta=True — возвращает (_Entry).
        """
        with self._lock:
            entry = self._entries.get(name)
            if not entry:
                return None
            return entry if with_meta else entry.thread

    def remove(
        self,
        name: str,
        *,
        join: bool = True,
        timeout: Optional[float] = None,
        signal_stop: bool = True
    ) -> Optional[threading.Thread]:
        """
        Удаляет поток из реестра. По умолчанию мягко останавливает и ждёт завершения.
        ВАЖНО: «убить» поток в CPython нельзя, только сигналить и ждать.
        """
        with self._lock:
            entry = self._entries.pop(name, None)
        if not entry:
            return None

        t = entry.thread
        if signal_stop and entry.manage_stop and entry.stop_event is not None:
            entry.stop_event.set()

        if join and t.is_alive():
            t.join(timeout if timeout is not None else self._default_join_timeout)
            if t.is_alive():
                log.warning("Thread %r did not stop in time", name)
        return t

    def exit(self, *, timeout_per_thread: Optional[float] = None) -> None:
        """
        Останавливает и дожидается всех потоков, затем очищает реестр.
        """
        with self._lock:
            items = list(self._entries.items())
            self._entries.clear()

        # Сначала всем сигналим stop (там где можно)
        for name, entry in items:
            if entry.manage_stop and entry.stop_event is not None:
                entry.stop_event.set()

        # Затем ждём
        current = threading.current_thread()
        for name, entry in items:
            t = entry.thread
            if t is current:
                continue
            if t.is_alive():
                t.join(timeout_per_thread if timeout_per_thread is not None else self._default_join_timeout)
                if t.is_alive():
                    log.warning("Thread %r did not stop in time", name)
