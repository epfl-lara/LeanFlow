"""Run complete native sessions with bounded supervision and verified owned cleanup."""

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
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from .commands import build_command, child_environment
from .context_policy import validate_restricted_native_context
from .discovery import discover_mcp_servers
from .events import EventState, usage_delta
from .isolation import IsolationPolicy, IsolationUnavailable, sandbox_command
from .model_defaults import reasoning_default
from .models import (
    BackendError,
    HarnessRequest,
    HarnessResult,
    NativeCleanupUnverified,
    NativeLaunchFailure,
)
from .processes import (
    ProcessTree,
    cleanup_group,
    process_exited,
    verify_owned_stopped,
)
from .redaction import environment_secrets, redact_native_value
from .resources import (
    SAMPLE_INTERVAL_S,
    NativeMemoryLimit,
    NativeResourceSampler,
    NativeResourceUnavailable,
    native_memory_limit_bytes,
)
from .restricted import restricted_isolation
from .settings import SettingsRecorder, native_input, private_control_response, startup_settings
from .stream_redaction import NativeStreamRedactor


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


def _prepare_launch(
    request: HarnessRequest,
) -> tuple[list[str], dict[str, str], tuple[str, ...], str, IsolationPolicy] | HarnessResult:
    """Resolve pure launch inputs and close discovery helpers before returning or raising."""
    if request.deadline is not None and (
        isinstance(request.deadline, bool) or not math.isfinite(request.deadline)
    ):
        raise BackendError("The campaign deadline must be a finite monotonic timestamp or None.")
    command = build_command(request)
    environment = child_environment(request)
    native_memory_limit_bytes(environment)
    validate_restricted_native_context(request, environment)
    secrets = environment_secrets(environment)
    executable = shutil.which(command[0], path=environment.get("PATH"))
    if executable is None:
        return HarnessResult(
            "startup_error",
            session_id=request.session_id,
            error=f"Native backend executable is unavailable: {command[0]}",
            cleanup_verified=True,
        )
    # Linux mounts the granted target file; a PATH symlink may live outside it.
    executable = str(Path(executable).resolve())
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
        return HarnessResult(stop, session_id=request.session_id, cleanup_verified=True)
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
        return HarnessResult(stop, session_id=request.session_id, cleanup_verified=True)
    return command, environment, secrets, executable, policy


def run_harness(
    request: HarnessRequest, on_event: Callable[[dict[str, Any]], None] | None = None
) -> HarnessResult:
    """Launch one native invocation and wait until terminal output is quiescent.

    A zero-output session has no idle deadline. The caller's campaign deadline or
    cancellation stops normal execution; persistent process-inspection failure
    aborts supervision. Cleanup bounds termination and never weakens final evidence.
    """
    try:
        prepared = _prepare_launch(request)
    except (NativeCleanupUnverified, NativeLaunchFailure):
        raise
    except (BackendError, IsolationUnavailable, OSError, ValueError) as error:
        valid_deadline = request.deadline is None or (
            not isinstance(request.deadline, bool) and math.isfinite(request.deadline)
        )
        status = (_stop_reason(request) if valid_deadline else "") or "startup_error"
        raise NativeLaunchFailure(
            str(error), status=status, session_id=request.session_id
        ) from error
    if isinstance(prepared, HarnessResult):
        return prepared
    command, environment, secrets, executable, policy = prepared
    memory_limit = native_memory_limit_bytes(environment)
    state = EventState(request.backend, session_id=request.session_id)
    settings = SettingsRecorder(request, secrets)
    public_stream = NativeStreamRedactor(secrets)
    malformed = False
    process: subprocess.Popen[bytes] | None = None
    sampler: NativeResourceSampler | None = None
    try:
        with ExitStack() as resources:
            try:
                if request.log_path is not None:
                    request.log_path.parent.mkdir(parents=True, exist_ok=True)
                log_context = (
                    request.log_path.open("a", encoding="utf-8")
                    if request.log_path
                    else nullcontext(None)
                )
                log = resources.enter_context(log_context)
                prompt = resources.enter_context(tempfile.TemporaryFile())
                prompt.write(native_input(request))
                prompt.seek(0)
                if request.on_admitting is not None:
                    request.on_admitting()
            except NativeCleanupUnverified:
                raise
            except (BackendError, OSError, ValueError) as error:
                raise NativeLaunchFailure(str(error), session_id=request.session_id) from error
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
                return HarnessResult(
                    "startup_error",
                    session_id=request.session_id,
                    error=str(exc),
                    cleanup_verified=True,
                )
            buffers: dict[str, bytes] = {"stdout": b"", "stderr": b""}

            def consume(channel: str, line: bytes) -> None:
                """Decode one channel record; stderr never enters the JSON parser."""
                nonlocal malformed
                decoded = line.decode("utf-8", errors="replace")
                if channel == "stderr":
                    metadata = startup_settings(request.backend, decoded)
                    if metadata is not None:
                        if not metadata.get("reasoning_effort"):
                            effort = reasoning_default(
                                request, executable, environment, policy, metadata["model"]
                            )
                            if effort:
                                metadata.update(
                                    reasoning_effort=effort, reasoning_source="native_model_default"
                                )
                        if change := settings.update(metadata):
                            _record(log, change, on_event, secrets)
                        return
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
                    # A truncated delta cannot be scoped or joined safely; retaining
                    # its raw body would bypass the fragment credential guard.
                    _record(
                        log,
                        {
                            "type": "harness.invalid_output",
                            "text": f"Malformed native JSON output: {exc}. Raw record omitted.",
                        },
                        on_event,
                        secrets,
                    )
                    return
                state.consume(event)
                if change := settings.consume(event):
                    _record(log, change, on_event, secrets)
                if private_control_response(event):
                    return
                for public_event in public_stream.consume(event):
                    _record(log, public_event, on_event, secrets)

            cleaned = False
            outputs_closed = False
            drain_deadline: float | None = None
            tree = ProcessTree(process.pid, process=process)
            sampler = NativeResourceSampler(process, tree, memory_limit)
            resource_error = ""
            last_scan = 0.0
            try:
                if request.on_started is not None:
                    request.on_started()
                _record(
                    log,
                    {
                        "type": "harness.resource_limits",
                        "max_owned_rss_bytes": memory_limit,
                        "sampling_interval_s": SAMPLE_INTERVAL_S,
                        "max_consecutive_sample_failures": sampler.max_failures,
                    },
                    on_event,
                    secrets,
                )
                if initial := settings.update({}):
                    _record(log, initial, on_event, secrets)
                with selectors.DefaultSelector() as selector:
                    assert process.stdout is not None and process.stderr is not None
                    for channel, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, channel)
                    while selector.get_map() or not process_exited(process):
                        if not cleaned:
                            now = time.monotonic()
                            if now - last_scan >= SAMPLE_INTERVAL_S:
                                try:
                                    sampled = sampler.sample()
                                    if not sampled and not state.diagnostics:
                                        state.diagnostics.append(
                                            "Native memory or ownership sample unavailable; retrying."
                                        )
                                except (NativeMemoryLimit, NativeResourceUnavailable) as error:
                                    resource_error = str(error)
                                    _record(
                                        log,
                                        {
                                            "type": "harness.resource_error",
                                            "error": resource_error,
                                            "peak_owned_rss_bytes": sampler.peak_owned_rss_bytes,
                                            "max_owned_rss_bytes": memory_limit,
                                        },
                                        on_event,
                                        secrets,
                                    )
                                last_scan = now
                            stop = _stop_reason(request)
                            if stop or resource_error or process_exited(process):
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
                    outputs_closed = not bool(selector.get_map())
            finally:
                try:
                    if not cleaned:
                        cleanup_group(process, tree)
                    for public_event in public_stream.flush():
                        _record(log, public_event, on_event, secrets)
                finally:
                    for output_pipe in (process.stdout, process.stderr):
                        if output_pipe is not None:
                            try:
                                output_pipe.close()
                            except OSError:
                                pass  # Preserve any owned-cleanup failure and its observed usage.
        code = process.returncode
        try:
            cleanup_verified = verify_owned_stopped(tree, group=process.pid)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise NativeCleanupUnverified(
                "Native cleanup evidence is unavailable; retain invocation inputs: " + str(error)
            ) from error
        if not cleanup_verified:
            raise NativeCleanupUnverified(
                "Native process group or tracked descendants remain live; retain invocation inputs"
            )
        cleanup_verified = cleanup_verified and outputs_closed
        if stop:
            status = stop
        elif resource_error:
            status = "failed"
            state.error = resource_error
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
            tuple(settings.history),
            cleanup_verified,
            sampler.peak_owned_rss_bytes,
            memory_limit,
        )
    except NativeCleanupUnverified as error:
        if process is not None:
            error.result = HarnessResult(
                "cleanup_unverified",
                state.final_response,
                state.session_id,
                usage_delta(
                    state.usage_total,
                    request.previous_usage,
                    resumed=bool(request.session_id) and not request.restricted_tools,
                    cumulative=True,
                ),
                state.usage_total,
                process.returncode,
                str(error),
                tuple(state.diagnostics),
                tuple(settings.history),
                False,
                sampler.peak_owned_rss_bytes if sampler is not None else 0,
                memory_limit,
            )
        raise
