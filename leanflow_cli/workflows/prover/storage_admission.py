"""Throttle disk checks while retaining a mandatory check before native dispatch."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from leanflow_cli.runtime.storage import ensure_storage_capacity


class StorageAdmission:
    """Clean rebuildable managed storage before admitting more work under pressure."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def check(self, *, force: bool = False) -> None:
        """Check at admission and every 15 seconds during an active workflow."""
        with self._lock:
            current = time.monotonic()
            if force or current - self._checked_at >= 15:
                ensure_storage_capacity(self.root)
                self._checked_at = current
