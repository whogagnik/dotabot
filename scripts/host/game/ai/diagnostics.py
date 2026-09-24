"""Logging helpers without importing Windows input or capture libraries."""

from functools import wraps
import logging


def debug_log_result(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        logger = getattr(args[0], "log", None) if args else None
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug("[BRAIN] %s -> %.160s", fn.__name__, repr(result))
        return result

    return wrapper
