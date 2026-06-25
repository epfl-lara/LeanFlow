"""Small process-runtime helpers for the agent (extracted from run_agent.py).

Leaf module, stdlib-only: a collision-avoiding short session-id generator (owns its private
issued-id set + lock, read nowhere else) and a broken-pipe-safe stdout/stderr wrapper
(`_SafeWriter` + `_install_safe_stdio`). Re-exported from run_agent so the historical
`from run_agent import _SafeWriter` / bare-name call sites keep resolving the same objects.
"""

from __future__ import annotations

import contextlib
import random
import sys
import threading
import time

_issued_session_ids: set[str] = set()
_issued_session_ids_lock = threading.Lock()


def _generate_short_session_id() -> str:
    for _ in range(100):
        candidate = f"{random.randint(0, 99999):05d}"
        with _issued_session_ids_lock:
            if candidate not in _issued_session_ids:
                _issued_session_ids.add(candidate)
                return candidate
    # Extremely unlikely fallback.
    return f"{int(time.time() * 1000) % 100000:05d}"


class _SafeWriter:
    """Transparent stdio wrapper that catches OSError from broken pipes.

    When LeanFlow runs as a systemd service, Docker container, or headless
    daemon, the stdout/stderr pipe can become unavailable (idle timeout, buffer
    exhaustion, socket reset). Any print() call then raises
    ``OSError: [Errno 5] Input/output error``, which can crash agent setup or
    run_conversation() — especially via double-fault when an except handler
    also tries to print.

    This wrapper delegates all writes to the underlying stream and silently
    catches OSError. It is transparent when the wrapped stream is healthy.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def write(self, data):
        try:
            return self._inner.write(data)
        except OSError:
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        with contextlib.suppress(OSError):
            self._inner.flush()

    def fileno(self):
        return self._inner.fileno()

    def isatty(self):
        try:
            return self._inner.isatty()
        except OSError:
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _install_safe_stdio() -> None:
    """Wrap stdout/stderr so best-effort console output cannot crash the agent."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            setattr(sys, stream_name, _SafeWriter(stream))
