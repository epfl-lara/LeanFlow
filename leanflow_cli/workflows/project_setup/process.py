"""Run owned setup processes with streaming logs and user cancellation."""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from core.error_redaction import sanitize_auxiliary_error

if TYPE_CHECKING:
    from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy

Progress = Callable[[dict[str, Any]], None]
MAX_PROTOCOL_STDOUT_BYTES = 8 * 1024**2


class SetupFailure(RuntimeError):
    """Carry an actionable setup failure across phase boundaries."""

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


class SetupCancelled(Exception):
    """Report explicit cancellation without treating it as an installation error."""


def validate_stdout_limit(stdout_only: bool, limit: int | None) -> None:
    """Require a bounded complete-protocol request rather than a diagnostic tail."""
    if limit is not None and (
        not stdout_only
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 < limit <= MAX_PROTOCOL_STDOUT_BYTES
    ):
        raise ValueError("Complete protocol stdout requires a positive bound of at most 8 MiB")


def find_executable(name: str) -> str | None:
    """Find native Elan shims when an editor did not inherit the login-shell PATH."""
    found = shutil.which(name)
    if found:
        return found
    if name in {"lake", "lean", "elan"}:
        elan_home = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))).expanduser()
        candidate = elan_home / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def safe_text(value: str) -> str:
    """Redact credentials including URL user information before persistence."""
    value = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
    return sanitize_auxiliary_error(value, limit=64000)


def check_cancel(cancel: threading.Event | None) -> None:
    """Stop before starting another operation when cancellation was requested."""
    if cancel is not None and cancel.is_set():
        raise SetupCancelled()


def run_command(
    command: list[str],
    *,
    cwd: Path,
    log: TextIO,
    cancel: threading.Event | None,
    progress: Progress | None,
    phase: str,
    input_text: str | None = None,
    stdout_only: bool = False,
    stdout_limit_bytes: int | None = None,
    execution_policy: SetupExecutionPolicy | None = None,
) -> tuple[int, str]:
    """Stream diagnostics; optionally retain complete bounded stdout or reject its overflow."""
    validate_stdout_limit(stdout_only, stdout_limit_bytes)
    if execution_policy is not None:
        from leanflow_cli.workflows.project_setup.execution_process import run_restricted_command

        return run_restricted_command(
            command,
            cwd=cwd,
            log=log,
            cancel=cancel,
            progress=progress,
            phase=phase,
            input_text=input_text,
            stdout_only=stdout_only,
            stdout_limit_bytes=stdout_limit_bytes,
            execution_policy=execution_policy,
        )
    from leanflow_cli.workflows.project_setup.public_process import run_public_command

    return run_public_command(
        command,
        cwd=cwd,
        log=log,
        cancel=cancel,
        progress=progress,
        phase=phase,
        input_text=input_text,
        stdout_only=stdout_only,
        stdout_limit_bytes=stdout_limit_bytes,
    )
