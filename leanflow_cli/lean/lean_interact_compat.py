"""Patch known LeanInteract response-reader performance defects."""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from typing import IO, Any

logger = logging.getLogger(__name__)

_PATCH_MARKER = "_leanflow_linear_repl_reader"


def _read_repl_response(stdout: IO[str]) -> str:
    """Read one delimited REPL response with linear string construction."""
    chunks: list[str] = []
    while True:
        line = stdout.readline()
        if not line:
            break
        chunks.append(line)
        # Lean's protocol terminates one JSON response with a blank line.
        # Checking the newest line avoids repeatedly joining the growing body.
        if line in {"\n", "\r\n"} or line.endswith("\n\n"):
            break
    return "".join(chunks)


def _linear_execute_cmd_in_repl(
    self: Any,
    json_query: str,
    verbose: bool,
    timeout: float | None,
) -> str:
    """Send one query and collect its response without quadratic concatenation."""
    process = self._proc
    assert process is not None and process.stdin is not None and process.stdout is not None
    with self._lock:
        if verbose:
            logger.info("Sending query: %s", json_query)
        process.stdin.write(json_query + "\n\n")
        process.stdin.flush()

        responses: list[str] = []

        def reader() -> None:
            responses.append(_read_repl_response(process.stdout))

        thread = threading.Thread(target=reader, daemon=True, name="leanflow-repl-reader")
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.kill()
            # Closing the server pipe normally releases readline immediately.
            # Keep shutdown bounded if a platform backend delays that close.
            thread.join(2.0)
            raise TimeoutError(
                f"The Lean server did not respond in time ({timeout=}) and is now killed."
            )
        output = responses[0] if responses else ""
        if output.strip():
            return output
        raise BrokenPipeError("The Lean server returned no output.")


setattr(_linear_execute_cmd_in_repl, _PATCH_MARKER, True)


def install_linear_repl_reader() -> bool:
    """Replace the known quadratic LeanInteract reader and report installation."""
    try:
        from lean_interact import LeanServer
    except Exception:
        return False
    current: Callable[..., str] | None = getattr(LeanServer, "_execute_cmd_in_repl", None)
    if current is None:
        return False
    if bool(getattr(current, _PATCH_MARKER, False)):
        return True
    try:
        vulnerable = "output += line" in inspect.getsource(current)
    except (OSError, TypeError):
        vulnerable = False
    if not vulnerable:
        # A future LeanInteract release may ship its own linear reader. Do not
        # replace unfamiliar internals merely because the private name remains.
        return False
    setattr(LeanServer, "_execute_cmd_in_repl", _linear_execute_cmd_in_repl)
    return True
