"""Tests for runtime-home-aware optional error logging."""

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.accounting.error_log import ensure_error_log_handler


def _close_handlers(target_logger: logging.Logger) -> None:
    """Close every handler installed on one isolated test logger."""
    for handler in list(target_logger.handlers):
        target_logger.removeHandler(handler)
        handler.close()


def _handler_destinations(target_logger: logging.Logger) -> list[Path]:
    """Return resolved file destinations for one isolated test logger."""
    return [
        Path(handler.baseFilename).resolve()
        for handler in target_logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]


def test_runtime_home_change_replaces_only_leanflow_owned_handler(monkeypatch, tmp_path):
    """A home change retires the owned sink but preserves unrelated handlers."""
    target_logger = logging.Logger("leanflow-test-home-change")
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    unrelated_path = first_home / "logs" / "application.log"
    unrelated_path.parent.mkdir(parents=True, exist_ok=True)
    unrelated = RotatingFileHandler(unrelated_path, maxBytes=1024, backupCount=1)
    target_logger.addHandler(unrelated)

    try:
        monkeypatch.setenv("LEANFLOW_HOME", str(first_home))
        first = ensure_error_log_handler(target_logger)
        assert first is not None

        monkeypatch.setenv("LEANFLOW_HOME", str(second_home))
        second = ensure_error_log_handler(target_logger)

        assert second is not None
        assert second is not first
        assert first not in target_logger.handlers
        assert unrelated in target_logger.handlers
        assert sorted(_handler_destinations(target_logger)) == sorted(
            [unrelated_path.resolve(), (second_home / "logs" / "errors.log").resolve()]
        )
    finally:
        _close_handlers(target_logger)


def test_concurrent_same_home_setup_reuses_one_handler(monkeypatch, tmp_path):
    """Concurrent agent setup cannot create duplicate same-path handlers."""
    target_logger = logging.Logger("leanflow-test-concurrent-setup")
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "runtime-home"))
    barrier = threading.Barrier(12)
    returned: list[logging.Handler | None] = []
    returned_lock = threading.Lock()

    def install() -> None:
        barrier.wait()
        handler = ensure_error_log_handler(target_logger)
        with returned_lock:
            returned.append(handler)

    threads = [threading.Thread(target=install) for _ in range(12)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert len(returned) == 12
        assert len({id(handler) for handler in returned}) == 1
        assert len(_handler_destinations(target_logger)) == 1
    finally:
        _close_handlers(target_logger)


def test_unwritable_new_home_is_optional_and_retires_owned_old_home(monkeypatch, tmp_path):
    """Filesystem failure returns no handler and cannot retain the old-home sink."""
    target_logger = logging.Logger("leanflow-test-unwritable-home")
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "writable"))

    try:
        old_handler = ensure_error_log_handler(target_logger)
        assert old_handler is not None

        blocked_home = tmp_path / "blocked"
        blocked_home.write_text("occupied", encoding="utf-8")
        monkeypatch.setenv("LEANFLOW_HOME", str(blocked_home))

        assert ensure_error_log_handler(target_logger) is None
        assert old_handler not in target_logger.handlers
        assert _handler_destinations(target_logger) == []
    finally:
        _close_handlers(target_logger)
