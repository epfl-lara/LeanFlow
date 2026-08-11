"""Provide retry-safe directory creation for shared runtime roots."""

from __future__ import annotations

import time
from pathlib import Path

_MKDIR_RETRY_DELAYS_S = (0.01, 0.02, 0.05, 0.1, 0.2)


def ensure_directory(path: Path, *, mode: int = 0o777) -> Path:
    """Create a directory tree despite transient bind-mount metadata races.

    Docker Desktop bind mounts can return ``EEXIST`` from a recursive mkdir
    while an immediately following directory check still reports false. Retry
    the complete request so a concurrently created parent does not abort
    creation of the requested child. Real file collisions remain errors.
    """
    last_error: FileExistsError | None = None
    for delay_s in (*_MKDIR_RETRY_DELAYS_S, None):
        try:
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            return path
        except FileExistsError as exc:
            if path.is_dir():
                return path
            last_error = exc
            if delay_s is not None:
                time.sleep(delay_s)
    assert last_error is not None
    raise last_error
