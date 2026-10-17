"""Stream sandboxed preparation commands under serial, bounded ownership and cancellation."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, TextIO

from agent.runtime.harness.isolation import IsolationUnavailable, sandbox_command
from agent.runtime.harness.models import BackendError
from agent.runtime.harness.processes import ProcessTree, cleanup_group
from leanflow_cli.workflows.project_setup.cleanup import verify_owned_stopped
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy, owned_rss_bytes
from leanflow_cli.workflows.project_setup.process import (
    Progress,
    SetupFailure,
    safe_text,
    validate_stdout_limit,
)

CHUNK_BYTES = 4096
TAIL_BYTES = 64000
LINE_BYTES = 16000


def run_restricted_command(
    command: list[str],
    *,
    cwd: Path,
    log: TextIO,
    cancel: threading.Event | None,
    progress: Progress | None,
    phase: str,
    execution_policy: SetupExecutionPolicy,
    input_text: str | None = None,
    stdout_only: bool = False,
    stdout_limit_bytes: int | None = None,
) -> tuple[int, str]:
    """Execute only bound tools, sampling owned-tree resources and preserving protocol stdout."""
    validate_stdout_limit(stdout_only, stdout_limit_bytes)
    policy = execution_policy
    cwd = cwd.resolve()
    if not cwd.is_relative_to(policy.isolation.snapshot_root.resolve()):
        raise SetupFailure(
            "setup_workspace_escape",
            "Preparation command escaped its private workspace.",
            "Use a controller-owned private preparation stage.",
        )
    if not command or command[0] not in policy.executables:
        raise SetupFailure(
            "setup_tool_unbound",
            "Preparation command is not a bound executable.",
            "Bind the required tool explicitly before starting preparation.",
        )
    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    if input_bytes is not None and len(input_bytes) > TAIL_BYTES:
        raise SetupFailure(
            "setup_input_limit",
            "Preparation protocol input exceeds its byte budget.",
            "Use a bounded import-only readiness query.",
        )
    if not policy.owner.acquire(blocking=False):
        raise SetupFailure(
            "setup_concurrency",
            "Another command owns this preparation resource budget.",
            "Run preparation commands serially.",
        )
    process: subprocess.Popen[bytes] | None = None
    tree: ProcessTree | None = None
    workers: list[threading.Thread] = []
    stopped = threading.Event()
    lines: queue.Queue[tuple[bool, bytes | Exception | None]] = queue.Queue(maxsize=64)
    try:
        policy.check(cancel)
        try:
            wrapped = sandbox_command(
                [policy.executables[command[0]], *command[1:]], policy.isolation
            )
        except IsolationUnavailable as error:
            raise SetupFailure(
                "setup_isolation_unavailable",
                str(error),
                "Use supported macOS isolation or Linux Bubblewrap; unrestricted preparation is unavailable.",
            ) from error
        policy.check(cancel)
        log.write("\n$ " + safe_text(" ".join(command)) + "\n")
        log.flush()
        policy.observe_disk()
        process = subprocess.Popen(
            wrapped,
            cwd=cwd,
            env=dict(policy.environment),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        policy.usage.commands += 1
        tree = ProcessTree(process.pid, process=process)

        def enqueue(channel: bool, content: bytes | Exception | None) -> None:
            while not stopped.is_set():
                try:
                    lines.put((channel, content), timeout=0.05)
                    return
                except queue.Full:
                    pass

        def read_output(stream: BinaryIO, channel: bool) -> None:
            try:
                while not stopped.is_set():
                    block = stream.read1(CHUNK_BYTES)  # type: ignore[attr-defined]
                    if not block:
                        break
                    enqueue(channel, block)
            except Exception as error:
                enqueue(channel, error)
            finally:
                enqueue(channel, None)

        assert process.stdout is not None and process.stderr is not None
        for stream, channel in ((process.stdout, True), (process.stderr, False)):
            worker = threading.Thread(
                target=read_output,
                args=(stream, channel),
                daemon=True,
                name="leanflow-private-setup-output",
            )
            workers.append(worker)
            worker.start()
        if input_bytes is not None:

            def write_input() -> None:
                assert process is not None and process.stdin is not None
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
                except OSError:
                    pass

            writer = threading.Thread(
                target=write_input, daemon=True, name="leanflow-private-setup-input"
            )
            workers.append(writer)
            writer.start()
        tail = b""
        stdout_tail = b""
        protocol_stdout = bytearray()
        pending = {False: b"", True: b""}
        preceding_cr = {False: False, True: False}

        def emit(content: bytes) -> None:
            if len(content) > LINE_BYTES:
                raise SetupFailure(
                    "setup_line_limit",
                    "Preparation emitted an oversized output line.",
                    "Correct the noisy dependency; the unbounded line was not retained or forwarded.",
                )
            sanitized = safe_text(content.decode("utf-8", errors="replace"))
            log.write(sanitized + "\n")
            log.flush()
            if progress:
                progress({"type": "output", "phase": phase, "message": sanitized})

        ended = 0
        sample_at = 0.0
        while ended < 2 or process.poll() is None:
            if cancel is not None and cancel.is_set():
                policy.check(cancel)
            now = time.monotonic()
            if now >= policy.deadline:
                policy.check(cancel)
            if now >= sample_at:
                policy.check(cancel)
                current_rss = owned_rss_bytes(process, tree)
                policy.observe_rss(current_rss)
                policy.observe_disk()
                sample_at = now + 0.25
            try:
                channel, content = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if content is None:
                if pending[channel]:
                    emit(pending[channel])
                    pending[channel] = b""
                ended += 1
                continue
            if isinstance(content, Exception):
                if stdout_limit_bytes is not None:
                    raise SetupFailure(
                        "setup_protocol_read_failed",
                        "Complete preparation protocol output could not be read.",
                        "Retry the readiness query; incomplete output cannot establish readiness.",
                    ) from content
                continue
            policy.usage.output_bytes += len(content)
            if policy.usage.output_bytes > policy.max_output_bytes:
                raise SetupFailure(
                    "setup_output_limit",
                    "Preparation exceeded its cumulative output byte budget.",
                    "Inspect the retained bounded log and correct the noisy build or dependency.",
                )
            tail = (tail + content)[-TAIL_BYTES:]
            if channel:
                stdout_tail = (stdout_tail + content)[-TAIL_BYTES:]
                if stdout_limit_bytes is not None:
                    if len(protocol_stdout) + len(content) > stdout_limit_bytes:
                        raise SetupFailure(
                            "setup_protocol_output_limit",
                            "Complete preparation protocol output exceeds its byte bound.",
                            "Reduce or page the protocol query; truncated output cannot establish readiness.",
                        )
                    protocol_stdout.extend(content)
            # Treat terminal progress updates as bounded logical records while
            # preserving the exact raw protocol bytes in the tails above.
            if preceding_cr[channel] and content.startswith(b"\n"):
                content = content[1:]
            preceding_cr[channel] = False
            pending[channel] += content
            while True:
                cr = pending[channel].find(b"\r")
                lf = pending[channel].find(b"\n")
                boundaries = [offset for offset in (cr, lf) if offset >= 0]
                if not boundaries:
                    break
                boundary = min(boundaries)
                separator = pending[channel][boundary : boundary + 1]
                line = pending[channel][:boundary]
                remainder = pending[channel][boundary + 1 :]
                if separator == b"\r":
                    if remainder.startswith(b"\n"):
                        remainder = remainder[1:]
                    elif not remainder:
                        preceding_cr[channel] = True
                pending[channel] = remainder
                emit(line)
            if len(pending[channel]) > LINE_BYTES:
                emit(pending[channel])
        code = process.wait()
        stdout = bytes(protocol_stdout) if stdout_limit_bytes is not None else stdout_tail
        return code, (stdout if stdout_only and code == 0 else tail).decode(
            "utf-8", errors="replace"
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupFailure(
            "setup_resource_inspection_failed",
            "Preparation resource supervision could not be established.",
            "Use a supported host with process inspection and sandbox capabilities.",
        ) from error
    finally:
        stopped.set()
        try:
            if process is not None:
                try:
                    cleanup_group(process, tree)
                    if tree is None or not verify_owned_stopped(tree, group=process.pid):
                        raise BackendError(
                            "An owned preparation process remains live after shutdown"
                        )
                except (BackendError, OSError, ValueError, subprocess.SubprocessError) as error:
                    policy.usage.cleanup_verified = False
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation stopped, but descendant cleanup could not be verified.",
                        "Retain the private stage and inspect its owned processes before retrying.",
                    ) from error
                for worker in workers:
                    worker.join(timeout=2)
                if any(worker.is_alive() for worker in workers):
                    policy.usage.cleanup_verified = False
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation still has an open owned protocol channel.",
                        "Retain the private stage and inspect its owned processes before retrying.",
                    )
                for channel_stream in (process.stdin, process.stdout, process.stderr):
                    if channel_stream is not None:
                        channel_stream.close()
        finally:
            policy.owner.release()
