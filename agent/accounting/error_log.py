"""Configure the optional process-wide LeanFlow error log.

The active home is resolved for every agent construction because CLI and test
processes may change ``LEANFLOW_HOME`` after :mod:`run_agent` is imported.
Handler installation is process-global, so this module serializes replacement
and marks only handlers it owns for retirement on a later home change.
"""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.accounting.redact import RedactingFormatter
from core.home import leanflow_home

_ERROR_HANDLER_LOCK = threading.RLock()
_LEANFLOW_OWNED_ATTR = "_leanflow_owned_error_log_handler"
_ERROR_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_ERROR_LOG_MAX_BYTES = 2 * 1024 * 1024
_ERROR_LOG_BACKUP_COUNT = 2


def _handler_path(handler: logging.Handler) -> Path | None:
    """Return one file handler's resolved destination when available."""
    raw_path = getattr(handler, "baseFilename", None)
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        return Path(raw_path).resolve()
    except (OSError, RuntimeError):
        return None


def _retire_owned_handlers(
    target_logger: logging.Logger,
    *,
    keep: logging.Handler | None,
) -> None:
    """Remove and close stale handlers created by this module only."""
    for handler in tuple(target_logger.handlers):
        if handler is keep or not bool(getattr(handler, _LEANFLOW_OWNED_ATTR, False)):
            continue
        target_logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass


def ensure_error_log_handler(
    target_logger: logging.Logger | None = None,
) -> logging.Handler | None:
    """Install or reuse the optional handler for the current LeanFlow home.

    A home change retires only handlers previously installed here; unrelated
    application handlers remain untouched. Filesystem failures disable this
    optional sink without preventing agent construction.
    """
    active_logger = target_logger if target_logger is not None else logging.getLogger()
    try:
        error_log_path = (leanflow_home() / "logs" / "errors.log").resolve()
    except (OSError, RuntimeError):
        return None

    with _ERROR_HANDLER_LOCK:
        matching_handler = next(
            (
                handler
                for handler in active_logger.handlers
                if _handler_path(handler) == error_log_path
            ),
            None,
        )
        if matching_handler is not None:
            _retire_owned_handlers(active_logger, keep=matching_handler)
            return matching_handler

        error_file_handler: RotatingFileHandler | None = None
        try:
            error_log_path.parent.mkdir(parents=True, exist_ok=True)
            error_file_handler = RotatingFileHandler(
                error_log_path,
                maxBytes=_ERROR_LOG_MAX_BYTES,
                backupCount=_ERROR_LOG_BACKUP_COUNT,
            )
            error_file_handler.setLevel(logging.WARNING)
            error_file_handler.setFormatter(RedactingFormatter(_ERROR_LOG_FORMAT))
            setattr(error_file_handler, _LEANFLOW_OWNED_ATTR, True)
            active_logger.addHandler(error_file_handler)
        except OSError:
            if error_file_handler is not None:
                try:
                    error_file_handler.close()
                except OSError:
                    pass
            # Never keep writing an owned optional log into a previous home
            # after the runtime home authority has changed.
            _retire_owned_handlers(active_logger, keep=None)
            return None

        _retire_owned_handlers(active_logger, keep=error_file_handler)
        return error_file_handler
