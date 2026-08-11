"""Expose bounded exact arithmetic only to isolated empirical actors."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

from core.runtime_modes import empirical_compute_enabled
from tools.registry import registry
from tools.utilities.empirical_compute_runtime import MAX_PROGRAM_BYTES

EMPIRICAL_COMPUTE_DEFAULT_TIMEOUT_S: Final[int] = 4
EMPIRICAL_COMPUTE_MIN_TIMEOUT_S: Final[int] = 1
EMPIRICAL_COMPUTE_MAX_TIMEOUT_S: Final[int] = 8
EMPIRICAL_COMPUTE_PARENT_OUTPUT_LIMIT: Final[int] = 64 * 1024


def check_empirical_compute_requirements() -> bool:
    """Return whether this process owns an empirical scratch assignment."""
    return empirical_compute_enabled()


def _denied(message: str) -> str:
    """Return a stable structured denial without starting a child process."""
    return json.dumps(
        {
            "success": False,
            "status": "empirical_compute_denied",
            "output": "",
            "error": message,
        },
        ensure_ascii=False,
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill and reap one isolated computation and all of its descendants."""
    if process.poll() is not None:
        process.wait()
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _decode_child_result(stdout: bytes, stderr: bytes, returncode: int) -> dict[str, Any]:
    """Validate the isolated child's bounded JSON response."""
    if len(stdout) > EMPIRICAL_COMPUTE_PARENT_OUTPUT_LIMIT:
        return {
            "success": False,
            "status": "empirical_compute_output_limit",
            "output": "",
            "error": "isolated computation exceeded its parent output boundary",
        }
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        child_error = stderr.decode("utf-8", errors="replace")[:1000].strip()
        return {
            "success": False,
            "status": "empirical_compute_error",
            "output": "",
            "error": (
                f"isolated computation exited {returncode} without a valid result"
                + (f": {child_error}" if child_error else "")
            ),
        }
    if not isinstance(payload, dict):
        return {
            "success": False,
            "status": "empirical_compute_error",
            "output": "",
            "error": "isolated computation returned a non-object result",
        }
    return {
        "success": bool(payload.get("success")),
        "status": str(payload.get("status", "empirical_compute_error") or ""),
        "output": str(payload.get("output", "") or ""),
        "error": payload.get("error"),
    }


def empirical_compute_tool(program: str, *, timeout_s: int = 4) -> str:
    """Run an exact arithmetic program in a resource-capped child process.

    The public boundary fails closed unless the current process is a
    process-isolated, scratch-only empirical dispatch worker.  The child uses
    a restricted AST, empty ephemeral working directory, isolated Python mode,
    a minimal environment, and hard parent wall-clock kill.
    """
    if not empirical_compute_enabled():
        return _denied(
            "empirical_compute is available only inside an isolated empirical planner actor"
        )
    source = str(program or "")
    if not source.strip():
        return _denied("program must contain an exact arithmetic experiment")
    if len(source.encode("utf-8")) > MAX_PROGRAM_BYTES:
        return _denied(f"program exceeds the {MAX_PROGRAM_BYTES}-byte limit")
    bounded_timeout = max(
        EMPIRICAL_COMPUTE_MIN_TIMEOUT_S,
        min(EMPIRICAL_COMPUTE_MAX_TIMEOUT_S, int(timeout_s or 0)),
    )
    runtime = Path(__file__).resolve().parents[1] / "utilities" / "empirical_compute_runtime.py"
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(runtime),
        "--timeout-s",
        str(bounded_timeout),
    ]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="leanflow-empirical-") as ephemeral:
        os.chmod(ephemeral, 0o700)
        process = subprocess.Popen(
            command,
            cwd=ephemeral,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                input=source.encode("utf-8"),
                timeout=bounded_timeout + 1,
            )
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return json.dumps(
                {
                    "success": False,
                    "status": "empirical_compute_timeout",
                    "output": "",
                    "error": f"computation exceeded the {bounded_timeout}-second hard limit",
                    "timeout_s": bounded_timeout,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "process_isolated": True,
                    "process_reaped": process.poll() is not None,
                    "project_mutation_authority": False,
                },
                ensure_ascii=False,
            )
        returncode = int(process.returncode or 0)
        timeout_signals = {
            -int(getattr(signal, name)) for name in ("SIGKILL", "SIGXCPU") if hasattr(signal, name)
        }
        if returncode in timeout_signals:
            result = {
                "success": False,
                "status": "empirical_compute_timeout",
                "output": "",
                "error": f"computation exceeded the {bounded_timeout}-second CPU limit",
            }
        else:
            result = _decode_child_result(stdout, stderr, returncode)
    result.update(
        {
            "timeout_s": bounded_timeout,
            "duration_seconds": round(time.monotonic() - started, 3),
            "process_isolated": True,
            "process_reaped": process.poll() is not None,
            "project_mutation_authority": False,
        }
    )
    return json.dumps(result, ensure_ascii=False)


EMPIRICAL_COMPUTE_SCHEMA = {
    "name": "empirical_compute",
    "description": (
        "Run a bounded, process-isolated exact integer/Fraction experiment. Fraction, gcd, "
        "isqrt, lcm, and prod are preloaded; the same names may also be imported from "
        "fractions/math for compatibility. The restricted program can use arithmetic, loops, "
        "small functions, and print, but has no filesystem, process, network, environment, "
        "dynamic-import, background, or PTY capability. Use read_file/search_files or the "
        "read-only terminal separately to inspect the assigned project."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "description": "Bounded exact-arithmetic Python subset; print the evidence needed.",
            },
            "timeout_s": {
                "type": "integer",
                "description": "Hard foreground timeout, clamped to 1-8 seconds.",
                "minimum": 1,
                "maximum": EMPIRICAL_COMPUTE_MAX_TIMEOUT_S,
                "default": EMPIRICAL_COMPUTE_DEFAULT_TIMEOUT_S,
            },
        },
        "required": ["program"],
        "additionalProperties": False,
    },
}


registry.register(
    name="empirical_compute",
    toolset="empirical-compute",
    schema=EMPIRICAL_COMPUTE_SCHEMA,
    handler=lambda args, **_kw: empirical_compute_tool(
        str(args.get("program", "") or ""),
        timeout_s=int(
            args.get("timeout_s", EMPIRICAL_COMPUTE_DEFAULT_TIMEOUT_S)
            or EMPIRICAL_COMPUTE_DEFAULT_TIMEOUT_S
        ),
    ),
    check_fn=check_empirical_compute_requirements,
    emoji="🧮",
)


__all__ = [
    "EMPIRICAL_COMPUTE_DEFAULT_TIMEOUT_S",
    "EMPIRICAL_COMPUTE_MAX_TIMEOUT_S",
    "EMPIRICAL_COMPUTE_SCHEMA",
    "check_empirical_compute_requirements",
    "empirical_compute_tool",
]
