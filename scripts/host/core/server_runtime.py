# scripts/host/core/server_runtime.py
from __future__ import annotations

import os
import sys
import threading


_DJANGO_THREAD: threading.Thread | None = None


def _django_runner(logger=None):
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_project.settings")

        if logger:
            logger.info(f"[DJANGO] python={sys.executable}")
            logger.info("[DJANGO] importing django.core.management...")

        from django.core.management import execute_from_command_line

        argv = [
            sys.argv[0],
            "runserver",
            "0.0.0.0:8000",
            "--noreload",
        ]

        if logger:
            logger.info(f"[DJANGO] starting runserver with argv={argv}")

        execute_from_command_line(argv)

    except Exception as e:
        if logger:
            logger.error(f"[DJANGO] server thread failed: {e}", exc_info=True)
        else:
            print(f"[DJANGO] server thread failed: {e}", flush=True)
            raise


def start_django_server_in_thread(logger=None) -> threading.Thread:
    global _DJANGO_THREAD

    if _DJANGO_THREAD is not None and _DJANGO_THREAD.is_alive():
        if logger:
            logger.info("[DJANGO] server thread already running")
        return _DJANGO_THREAD

    t = threading.Thread(
        target=_django_runner,
        kwargs={"logger": logger},
        daemon=True,
        name="django-server",
    )
    t.start()
    _DJANGO_THREAD = t

    if logger:
        logger.info("[DJANGO] server thread started")

    return t