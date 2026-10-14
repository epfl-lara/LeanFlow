"""Run owned setup processes with streaming logs and user cancellation."""

from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from core.error_redaction import sanitize_auxiliary_error

if TYPE_CHECKING:
    from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy

Progress = Callable[[dict[str, Any]], None]


class SetupFailure(RuntimeError):
    """Carry an actionable setup failure across phase boundaries."""

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


class SetupCancelled(Exception):
    """Report explicit cancellation without treating it as an installation error."""


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


def _stop(process: subprocess.Popen[str]) -> None:
    """Reap the owned process group even when its leader has already exited."""
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            process.wait()
        return

    def signal_group(value: int) -> bool:
        try:
            os.killpg(process.pid, value)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS can report EPERM for an empty group while its leader exits.
            # A live owned process must still surface a real signalling failure.
            if sys.platform != "darwin" or process.poll() is None:
                raise
            return False

    if signal_group(signal.SIGTERM):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not signal_group(0):
                break
            time.sleep(0.02)
        signal_group(signal.SIGKILL)
    process.wait()


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
    execution_policy: SetupExecutionPolicy | None = None,
) -> tuple[int, str]:
    """Stream both channels, keeping successful protocol stdout separate on request."""
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
            execution_policy=execution_policy,
        )
    check_cancel(cancel)
    log.write("\n$ " + safe_text(" ".join(command)) + "\n")
    log.flush()
    resolved = find_executable(command[0]) or command[0]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "ELAN_NO_PROMPT": "1"}
    if command[0] == "lake":
        environment["PATH"] = str(Path(resolved).parent) + os.pathsep + environment.get("PATH", "")
    process = subprocess.Popen(
        [resolved, *command[1:]],
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    )
    lines: queue.Queue[tuple[bool, str | None]] = queue.Queue()

    def read_output(stream: TextIO, is_stdout: bool) -> None:
        try:
            for line in stream:
                lines.put((is_stdout, line))
        finally:
            lines.put((is_stdout, None))

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(
            target=read_output, args=(stream, is_stdout), daemon=True, name="leanflow-setup-output"
        )
        for stream, is_stdout in ((process.stdout, True), (process.stderr, False))
    ]
    for reader in readers:
        reader.start()
    tail = ""
    stdout_tail = ""
    try:
        if input_text is not None:
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()
        ended = 0
        while ended < len(readers) or process.poll() is None:
            check_cancel(cancel)
            try:
                is_stdout, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                ended += 1
                continue
            sanitized = safe_text(line)
            log.write(sanitized + "\n")
            log.flush()
            tail = (tail + line)[-64000:]
            if is_stdout:
                stdout_tail = (stdout_tail + line)[-64000:]
            if progress:
                progress({"type": "output", "phase": phase, "message": sanitized})
        exit_code = process.wait()
        return exit_code, stdout_tail if stdout_only and exit_code == 0 else tail
    finally:
        _stop(process)
        for reader in readers:
            reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
