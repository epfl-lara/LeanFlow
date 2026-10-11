"""Run a complete native agent session with only campaign cancellation or deadline."""

from __future__ import annotations

import json
import math
import os
import selectors
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from .commands import build_command, child_environment
from .context_policy import validate_restricted_native_context
from .discovery import discover_mcp_servers
from .events import EventState, usage_delta
from .isolation import IsolationUnavailable, sandbox_command
from .models import BackendError, HarnessRequest, HarnessResult
from .processes import ProcessTree, cleanup_group
from .redaction import environment_secrets, redact_native_value
from .restricted import restricted_isolation


def _stop_reason(request: HarnessRequest) -> str:
    """Return an external cancellation reason without inventing an idle timeout."""
    if request.cancelled is not None and request.cancelled():
        return "cancelled"
    if request.deadline is not None and time.monotonic() >= request.deadline:
        return "deadline_exceeded"
    return ""


def _record(
    stream: TextIO | None,
    payload: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None,
    secrets: tuple[str, ...] = (),
) -> None:
    """Persist observations before notifying the controller on the reader thread."""
    payload = redact_native_value(payload, secrets)
    if stream is not None:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()
    if on_event is not None:
        on_event(payload)


def run_harness(
    request: HarnessRequest, on_event: Callable[[dict[str, Any]], None] | None = None
) -> HarnessResult:
    """Launch one native invocation and wait until terminal output is quiescent.

    A zero-output session is allowed to run indefinitely. Only the caller's absolute
    monotonic campaign deadline or cancellation callback stops active execution.
    The cleanup grace bounds process termination, never useful model work.
    """
    if request.deadline is not None and (
        isinstance(request.deadline, bool) or not math.isfinite(request.deadline)
    ):
        raise BackendError("The campaign deadline must be a finite monotonic timestamp or None.")
    command = build_command(request)
    environment = child_environment(request)
    validate_restricted_native_context(request, environment)
    secrets = environment_secrets(environment)
    executable = shutil.which(command[0], path=environment.get("PATH"))
    if executable is None:
        return HarnessResult(
            "startup_error", error=f"Native backend executable is unavailable: {command[0]}"
        )
    command[0] = executable
    if request.isolation is None:
        raise BackendError("A native harness requires an explicit isolated workspace policy.")
    if request.cwd.resolve() != request.isolation.snapshot_root.resolve():
        raise BackendError("The invocation cwd must match its isolated snapshot root.")
    try:
        policy = replace(
            request.isolation,
            readable_roots=(*request.isolation.readable_roots, Path(executable).resolve()),
        )
        if request.restricted_tools:
            home_key = (
                "CLAUDE_CONFIG_DIR"
                if request.backend == "claude"
                else f"{request.backend.upper()}_HOME"
            )
            native_home = Path(
                environment.get(
                    home_key,
                    str(Path(environment.get("HOME", str(Path.home()))) / f".{request.backend}"),
                )
            ).resolve()
            policy = restricted_isolation(policy, native_home)
        # macOS forbids nesting seatbelt. The validated outer policy, inherited by
        # every tool descendant, is the filesystem authority for the full session.
        command = build_command(request, externally_isolated=True)
        command[0] = executable
        command = sandbox_command(
            command, policy, writable=request.tools and not request.restricted_tools
        )
    except (IsolationUnavailable, OSError, ValueError) as exc:
        raise BackendError(str(exc)) from exc
    if request.isolation.temp_root is not None:
        environment.update(
            {key: str(request.isolation.temp_root) for key in ("TMPDIR", "TMP", "TEMP")}
        )
        if request.backend == "claude":
            environment["CLAUDE_CODE_TMPDIR"] = str(request.isolation.temp_root)
    stop = _stop_reason(request)
    if stop:
        return HarnessResult(stop, session_id=request.session_id)
    if request.backend != "claude":
        discovery_command = sandbox_command(
            [
                executable,
                "mcp",
                "list",
                "--json",
                "-c",
                "features.plugins=false",
                "-c",
                "features.hooks=false",
            ],
            policy,
            writable=False,
        )
        disabled = discover_mcp_servers(request, discovery_command, environment)
        command = build_command(request, native_mcp_servers=disabled, externally_isolated=True)
        command[0] = executable
        command = sandbox_command(
            command, policy, writable=request.tools and not request.restricted_tools
        )
    stop = _stop_reason(request)
    if stop:
        return HarnessResult(stop, session_id=request.session_id)
    state = EventState(request.backend, session_id=request.session_id)
    malformed = False
    process: subprocess.Popen[bytes] | None = None
    if request.log_path is not None:
        request.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_context = (
        request.log_path.open("a", encoding="utf-8") if request.log_path else nullcontext(None)
    )
    with tempfile.TemporaryFile() as prompt, log_context as log:
        prompt.write(request.prompt.encode())
        prompt.seek(0)
        if request.on_admitting is not None:
            request.on_admitting()
        try:
            process = subprocess.Popen(
                command,
                cwd=request.cwd,
                env=environment,
                stdin=prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return HarnessResult("startup_error", session_id=request.session_id, error=str(exc))
        buffers: dict[str, bytes] = {"stdout": b"", "stderr": b""}

        def consume(channel: str, line: bytes) -> None:
            """Decode one channel record; stderr never enters the JSON parser."""
            nonlocal malformed
            decoded = line.decode("utf-8", errors="replace")
            if channel == "stderr":
                if decoded:
                    _record(log, {"type": "harness.stderr", "text": decoded}, on_event, secrets)
                return
            if not decoded.strip():
                return
            try:
                event = json.loads(decoded)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("Expected a native event object with a string type")
            except (ValueError, TypeError) as exc:
                malformed = True
                state.diagnostics.append(f"Malformed native JSON output: {exc}")
                _record(log, {"type": "harness.invalid_output", "text": decoded}, on_event, secrets)
                return
            state.consume(event)
            _record(log, event, on_event, secrets)

        cleaned = False
        drain_deadline: float | None = None
        tree = ProcessTree(process.pid)
        last_scan = 0.0
        try:
            if request.on_started is not None:
                request.on_started()
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                for channel, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, channel)
                while selector.get_map() or process.poll() is None:
                    if not cleaned:
                        now = time.monotonic()
                        if now - last_scan >= 0.25:
                            tree.refresh()
                            last_scan = now
                        stop = _stop_reason(request)
                        if stop or process.poll() is not None:
                            cleanup_group(process, tree)
                            cleaned = True
                            drain_deadline = time.monotonic() + 2
                    for key, _ in selector.select(timeout=0.1):
                        data = os.read(key.fd, 65536)
                        channel = key.data
                        if not data:
                            selector.unregister(key.fileobj)
                            if buffers[channel]:
                                consume(channel, buffers[channel])
                                buffers[channel] = b""
                            continue
                        buffers[channel] += data
                        while b"\n" in buffers[channel]:
                            line, buffers[channel] = buffers[channel].split(b"\n", 1)
                            consume(channel, line)
                    if (
                        drain_deadline is not None
                        and time.monotonic() >= drain_deadline
                        and selector.get_map()
                    ):
                        state.error = (
                            "Native descendants did not close output channels after cleanup."
                        )
                        break
        finally:
            if not cleaned:
                cleanup_group(process, tree)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    code = process.returncode
    if stop:
        status = stop
    elif code != 0:
        status = "failed"
        state.error = state.error or f"Native backend exited with status {code}."
    elif malformed:
        status = "malformed_output"
        state.error = state.error or "The native backend emitted malformed structured output."
    elif state.error or not state.completed:
        status = "failed"
        state.error = (
            state.error or "The native backend exited without a successful terminal event."
        )
    else:
        status = "completed"
    usage = usage_delta(
        state.usage_total,
        request.previous_usage,
        resumed=bool(request.session_id) and not request.restricted_tools,
        cumulative=True,
    )
    return HarnessResult(
        status,
        state.final_response,
        state.session_id,
        usage,
        state.usage_total,
        code,
        state.error,
        tuple(state.diagnostics),
    )
