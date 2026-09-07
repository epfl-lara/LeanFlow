"""Reattach a replacement dispatcher to explicitly recorded live worker identities."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any


def process_identity(pid: int) -> str:
    """Read process birth time and command to detect PID reuse before monitoring or signalling."""
    result = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart=,command="],
        env={**os.environ, "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ""
    raise RuntimeError(f"Cannot inspect existing worker {pid}: ps returned {result.returncode}")


class AdoptedProcess:
    """Monitor a surviving worker; native terminal state supplies its unavailable exit result."""

    def __init__(self, cell: dict[str, Any]) -> None:
        self.cell = cell
        self.pid = int(cell["pid"])
        self.identity = str(cell["process_identity"])
        if not self.identity:
            raise ValueError("Cannot adopt a worker without a recorded birth identity")
        current = process_identity(self.pid)
        if current and current != self.identity:
            raise ValueError(f"Worker PID identity changed: {self.pid}")

    def poll(self) -> int | None:
        """Wait for the recorded process to exit, then score only durable native evidence."""
        current = process_identity(self.pid)
        if current == self.identity:
            return None
        if current:
            raise ValueError(f"Worker PID was reused: {self.pid}")
        self.cell["returncode_source"] = "adopted_native_state"
        return 0 if self.cell.get("verified") else 1

    def wait(self, timeout: float) -> int:
        """Bound watchdog cleanup without assuming this worker is a child process."""
        deadline = time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.5)
