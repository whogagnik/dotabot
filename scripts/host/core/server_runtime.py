from __future__ import annotations

import os
import sys
import threading


def _django_runner():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_project.settings")
    from django.core.management import execute_from_command_line

    argv = [
        sys.argv[0],
        "runserver",
        "0.0.0.0:8000",
        "--noreload",
    ]
    execute_from_command_line(argv)


def start_django_server_in_thread(logger=None) -> threading.Thread:
    t = threading.Thread(target=_django_runner, daemon=True, name="django-server")
    t.start()
    if logger:
        logger.info("Django thread started")
    return t