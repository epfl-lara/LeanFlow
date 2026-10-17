"""Bound public setup subprocesses with nonblocking I/O and durable closure evidence."""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TextIO

from agent.runtime.harness.models import BackendError
from agent.runtime.harness.processes import (
    ProcessTree,
    _cleanup_states,
    cleanup_group,
    process_exited,
    require_output_eof,
)
from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.workflows.project_setup import invocation_evidence
from leanflow_cli.workflows.project_setup.process import (
    Progress,
    SetupFailure,
    check_cancel,
    find_executable,
    safe_text,
)

DEFAULT_TIMEOUT_S = 1800.0
MAX_OUTPUT_BYTES = 4 * 1024**2
EOF_TIMEOUT_S = 2.0


@dataclass
class _RetainedInvocation:
    """Keep physical channels available for a same-owner bounded cleanup retry."""

    process: subprocess.Popen[bytes]
    tree: ProcessTree | None
    path: Path
    record: dict[str, Any]


_UNVERIFIED: dict[Path, _RetainedInvocation] = {}
_CLEANUP_LOCK = threading.RLock()
_BOUND_ROOT: ContextVar[Path | None] = ContextVar("baseline_setup_root", default=None)


@contextmanager
def bind_invocation_root(root: Path) -> Iterator[None]:
    """Bind template and stage subprocesses to the same canonical setup owner."""
    token = _BOUND_ROOT.set(root.resolve())
    try:
        yield
    finally:
        _BOUND_ROOT.reset(token)


def retry_pending_invocations(cwd: Path) -> None:
    """Retry live-owner evidence only; a dead owner's unknown ledger remains blocked."""
    directory = invocation_directory(cwd)
    with _CLEANUP_LOCK:
        for path, retained in list(_UNVERIFIED.items()):
            if path.parent != directory:
                continue
            try:
                cleanup_group(retained.process, retained.tree)
                require_output_eof(
                    retained.process, timeout_s=EOF_TIMEOUT_S, description="Public setup retry"
                )
            except (BackendError, OSError, ValueError, subprocess.SubprocessError) as error:
                raise SetupFailure(
                    "setup_cleanup_unverified",
                    "Setup invocation still has uncertain closure.",
                    f"Retain inputs and inspect {path}.",
                ) from error
            retained.record.update(
                state="closed", cleanup_verified=True, physical_eof_verified=True
            )
            durable_json(path, retained.record)
            for stream in (
                retained.process.stdin,
                retained.process.stdout,
                retained.process.stderr,
            ):
                if stream is not None:
                    stream.close()
            del _UNVERIFIED[path]


def invocation_directory(cwd: Path) -> Path:
    """Locate persistent invocation evidence without creating it during inspection."""
    return invocation_evidence.directory(cwd)


def _timeout() -> float:
    """Require an explicit finite public command budget with a useful default."""
    try:
        value = float(os.environ.get("LEANFLOW_SETUP_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or not 0 < value <= 86400:
        raise SetupFailure(
            "setup_timeout_setting",
            "Setup timeout must be positive and at most 86400 seconds.",
            "Set LEANFLOW_SETUP_TIMEOUT_S to 1800 or an adequate bounded value.",
        )
    return value


def _read(stream: IO[bytes]) -> bytes | None:
    """Read one bounded nonblocking chunk; only an actual empty read establishes EOF."""
    try:
        return os.read(stream.fileno(), 4096)
    except BlockingIOError:
        return None


def _cached_observed_processes(tree: ProcessTree) -> dict[str, str]:
    """Serialize cached identities and always preserve the originally bound leader."""
    observed = {str(pid): " ".join(start.split()) for pid, start in tree.descendants.items()}
    if tree.root_identity:
        observed[str(tree.root)] = " ".join(tree.root_identity.split())
    return observed


def _current_observed_processes(tree: ProcessTree) -> dict[str, str]:
    """Prune proven exited identities using a fresh complete table, retaining live uncertainty."""
    table = _cleanup_states(2.0)
    # An escaped child stays owned even after reparenting or setsid. A present
    # mismatching timestamp may reflect a changed observation frame, so retain
    # it as uncertain rather than treating the old identity as proved absent.
    current = {
        pid: start
        for pid, start in tree.descendants.items()
        if pid in table and not table[pid][1].startswith("Z")
    }
    tree.descendants = current
    return _cached_observed_processes(tree)


def run_public_command(
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
) -> tuple[int, str]:
    """Run a public setup command without blocking readers or unrestricted output growth."""
    from leanflow_cli.workflows.project_setup.execution import (
        owned_rss_bytes,
        setup_memory_limit_bytes,
    )

    check_cancel(cancel)
    timeout = _timeout()
    rss_limit = setup_memory_limit_bytes()
    input_bytes = input_text.encode() if input_text is not None else b""
    if len(input_bytes) > 64000:
        raise SetupFailure(
            "setup_input_limit",
            "Setup input exceeds its 64000-byte bound.",
            "Use a bounded readiness request.",
        )
    deadline = time.monotonic() + timeout
    from leanflow_cli.workflows.project_setup.baseline_transaction import register_invocation

    root = _BOUND_ROOT.get() or cwd
    controller_table = _cleanup_states(2.0)
    controller_start = controller_table[os.getpid()][2]
    identifier = uuid.uuid4().hex
    path = invocation_evidence.register(root, identifier)
    register_invocation(root, identifier)
    record: dict[str, Any] = {
        "schema": 1,
        "id": identifier,
        "root": str(root.resolve()),
        "cwd": str(cwd.resolve()),
        "command": Path(command[0]).name,
        "cleanup_verified": False,
        "state": "admitting",
        "pid": None,
        "controller": {"pid": os.getpid(), "start": controller_start},
        "observed_processes": {},
        "physical_eof_verified": False,
    }
    durable_json(path, record)
    process: subprocess.Popen[bytes] | None = None
    tree: ProcessTree | None = None
    channels: dict[bool, IO[bytes]] = {}
    ended: set[bool] = set()
    output_bytes = 0
    peak_rss = 0
    launch_attempted = False
    no_launch_verified = False
    resolved = find_executable(command[0]) or command[0]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "ELAN_NO_PROMPT": "1"}
    if command[0] == "lake":
        environment["PATH"] = str(Path(resolved).parent) + os.pathsep + environment.get("PATH", "")
    log.write("\n$ " + safe_text(" ".join(command)) + "\n")
    log.flush()
    try:
        check_cancel(cancel)
        launch_attempted = True
        process = subprocess.Popen(
            [resolved, *command[1:]],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        record.update(state="running", pid=process.pid)
        durable_json(path, record)
        tree = ProcessTree(process.pid, process=process)
        tree.refresh()
        assert process.stdout is not None and process.stderr is not None
        channels = {True: process.stdout, False: process.stderr}
        for stream in (*channels.values(), *((process.stdin,) if process.stdin else ())):
            os.set_blocking(stream.fileno(), False)
        tail, stdout_tail = b"", b""
        protocol = bytearray()
        pending = {True: b"", False: b""}
        offset, sample_at = 0, 0.0
        exit_observed: float | None = None

        def emit(channel: bool, content: bytes, *, final: bool = False) -> None:
            pending[channel] += content.replace(b"\r", b"\n")
            while (
                b"\n" in pending[channel]
                or len(pending[channel]) >= 4096
                or (final and pending[channel])
            ):
                line, separator, rest = pending[channel].partition(b"\n")
                if not separator and len(line) >= 4096:
                    line, rest = line[:4096], line[4096:]
                pending[channel] = rest
                sanitized = safe_text(line.decode("utf-8", errors="replace"))
                log.write(sanitized + "\n")
                log.flush()
                if progress:
                    progress({"type": "output", "phase": phase, "message": sanitized})

        while True:
            exited = process_exited(process)
            if len(ended) == 2 and exited:
                break
            check_cancel(cancel)
            now = time.monotonic()
            if now >= deadline:
                raise SetupFailure(
                    "setup_deadline",
                    "Setup exceeded its bounded command deadline.",
                    "Retry with adequate LEANFLOW_SETUP_TIMEOUT_S after inspecting the retained log.",
                )
            if now >= sample_at:
                peak_rss = max(peak_rss, owned_rss_bytes(process, tree))
                if peak_rss > rss_limit:
                    raise SetupFailure(
                        "setup_memory_limit",
                        "Setup exceeded its sampled owned-process memory limit.",
                        "Reduce build parallelism or select an adequate bounded setup limit.",
                    )
                observed = _current_observed_processes(tree)
                if len(observed) > 256:
                    raise SetupFailure(
                        "setup_process_identity_limit",
                        "Setup exceeded its retained process-identity bound.",
                        "Reduce build parallelism and inspect retained invocation inputs.",
                    )
                record["observed_processes"] = observed
                durable_json(path, record)
                sample_at = now + 0.25
            if exited:
                exit_observed = exit_observed or now
                if len(ended) < 2 and now - exit_observed >= EOF_TIMEOUT_S:
                    raise SetupFailure(
                        "setup_output_eof_timeout",
                        "Setup leader exited while an output writer remains live.",
                        "Inspect the retained invocation and its child processes.",
                    )
            if process.stdin is not None and not process.stdin.closed:
                try:
                    if offset < len(input_bytes):
                        offset += os.write(
                            process.stdin.fileno(), input_bytes[offset : offset + 4096]
                        )
                    if offset == len(input_bytes):
                        process.stdin.close()
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    process.stdin.close()
            activity = False
            for channel, stream in channels.items():
                if channel in ended:
                    continue
                try:
                    content = _read(stream)
                except OSError as error:
                    raise SetupFailure(
                        "setup_protocol_read_failed",
                        "Setup output could not be read completely.",
                        "Inspect the retained invocation; partial protocol output cannot establish readiness.",
                    ) from error
                if content is None:
                    continue
                activity = True
                if not content:
                    ended.add(channel)
                    emit(channel, b"", final=True)
                    continue
                output_bytes += len(content)
                if output_bytes > MAX_OUTPUT_BYTES:
                    raise SetupFailure(
                        "setup_output_limit",
                        "Setup exceeded its bounded output budget.",
                        "Inspect the bounded log and correct the noisy dependency.",
                    )
                tail = (tail + content)[-64000:]
                if channel:
                    stdout_tail = (stdout_tail + content)[-64000:]
                    if stdout_limit_bytes is not None:
                        if len(protocol) + len(content) > stdout_limit_bytes:
                            raise SetupFailure(
                                "setup_protocol_output_limit",
                                "Complete setup protocol exceeds its byte bound.",
                                "Use a smaller protocol query; truncated results cannot establish readiness.",
                            )
                        protocol.extend(content)
                emit(channel, content)
            if not activity:
                time.sleep(0.02)
        output = bytes(protocol) if stdout_limit_bytes is not None else stdout_tail
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        if process is None:
            no_launch_verified = True
        raise SetupFailure(
            "setup_resource_inspection_failed",
            "Setup process or output supervision failed.",
            "Inspect the retained invocation and retry when process inspection is available.",
        ) from error
    finally:
        verified = False
        try:
            if process is not None:
                cleanup_group(process, tree)
                if len(channels) != 2:
                    assert process.stdout is not None and process.stderr is not None
                    channels = {True: process.stdout, False: process.stderr}
                    for stream in channels.values():
                        os.set_blocking(stream.fileno(), False)
                require_output_eof(process, timeout_s=EOF_TIMEOUT_S, description="Public setup")
            elif launch_attempted and not no_launch_verified:
                raise SetupFailure(
                    "setup_cleanup_unverified",
                    "Setup launch was interrupted without a process handle.",
                    f"Retain its invocation evidence: {path}",
                )
            record.update(state="closed", cleanup_verified=True, physical_eof_verified=True)
            verified = True
        except (
            BackendError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
            SetupFailure,
        ) as error:
            record.update(state="cleanup_unverified", cleanup_verified=False)
            if process is not None:
                with _CLEANUP_LOCK:
                    _UNVERIFIED[path] = _RetainedInvocation(process, tree, path, record)
            raise SetupFailure(
                "setup_cleanup_unverified",
                "Setup subprocess closure remains unverified.",
                f"Retain inputs and inspect invocation {path} before retrying.",
            ) from error
        finally:
            if tree is not None:
                try:
                    observed = _current_observed_processes(tree)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    # A failed final snapshot cannot discharge cached ownership
                    # or replace independent cleanup/physical-EOF verification.
                    observed = _cached_observed_processes(tree)
                    record["process_observation_error"] = str(error)[:1000]
                record["observed_processes"] = dict(list(observed.items())[:256])
                record["observed_processes_truncated"] = len(observed) > 256
            record.update(output_bytes=output_bytes, peak_owned_rss_bytes=peak_rss)
            durable_json(path, record)
            # Unknown channels remain held for a same-owner retry. Process death
            # leaves its durable marker false, never a reconstructed EOF claim.
            if process is not None and verified:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    assert process is not None and process.returncode is not None
    code = process.returncode
    return code, (output if stdout_only and code == 0 else tail).decode("utf-8", errors="replace")
