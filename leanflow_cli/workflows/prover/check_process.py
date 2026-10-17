"""Run warm Lean feedback in OS-isolated workers with explicit writable roots."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import (
    ProcessTree,
    group_has_owned_member,
    process_exited,
    require_output_eof,
    signal_group,
    verify_owned_stopped,
)
from core.deadlines import deadline_after, operation_cancelled, remaining_seconds, wait_slice
from leanflow_cli.workflows.prover.check_capture_failure import (
    CaptureInterruption,
    capture_failure,
    interrupted_capture_note,
)
from leanflow_cli.workflows.prover.check_command_cleanup import CommandOwnership, close_commands
from leanflow_cli.workflows.prover.check_sandbox import (
    IsolationUnavailable,
    _environment,
    _sandbox_command,
    _validated_paths,
)
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation

_MAX_MESSAGE = 4 * 1024 * 1024
_MAX_ERROR = 16 * 1024
_WORKERS: dict[tuple[Path, Path], _CheckWorker] = {}
_WORKSPACE_LOCKS: dict[tuple[Path, Path], threading.Lock] = {}
_LOCK = threading.RLock()


def owned_checker_identities(workspace_root: Path) -> dict[int, str]:
    """Snapshot cached identities of this run's existing warm workers without reaping them."""
    root = workspace_root.resolve()
    identities: dict[int, str] = {}
    with _LOCK:
        for (_, workspace), worker in _WORKERS.items():
            if not workspace.is_relative_to(root) or worker.closed or worker.process is None:
                continue
            tree = worker.tree
            if tree is None or tree.process is not worker.process or tree.root_identity is None:
                raise NativeCleanupUnverified("Warm checker identity is unknown; retain its inputs")
            if worker.process.returncode is None:
                identities[worker.process.pid] = tree.root_identity
            identities.update(tree.descendants)
    return identities


def _kill(process: subprocess.Popen[bytes], tree: ProcessTree | None = None) -> None:
    """Stop owned identities without adopting descendants from an already reaped PID."""
    failures: list[Exception] = []
    try:
        # An unreaped child reserves its PID. After wait/poll, only a matching
        # tracked member can establish ownership of the former group.
        if process.returncode is None or group_has_owned_member(process, tree):
            signal_group(process, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        failures.append(error)
    if tree is not None:
        actions = [lambda: tree.signal(signal.SIGKILL)]
        if process.returncode is None:
            actions.insert(0, tree.refresh)
        for action in actions:
            try:
                action()
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                failures.append(error)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise NativeCleanupUnverified(
                "Lean checker leader remains live; retain its inputs"
            ) from error
    if failures:
        raise NativeCleanupUnverified(
            "Lean checker descendant cleanup is unverified; retain its inputs: " + str(failures[0])
        ) from failures[0]
    try:
        verified = verify_owned_stopped(tree, group=process.pid)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise NativeCleanupUnverified(
            "Lean checker closure evidence is unavailable; retain its inputs"
        ) from error
    if not verified:
        raise NativeCleanupUnverified(
            "Lean checker group or tracked descendants remain live; retain its inputs"
        )


def _failure(code: str, error: str, *, timed_out: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "ok": False,
        "error_code": code,
        "error": error[:_MAX_ERROR],
        "timed_out": timed_out,
    }


def _require_output_eof(process: subprocess.Popen[bytes]) -> None:
    """Require physical pipe closure rather than hiding an unobserved writer locally."""
    require_output_eof(process, description="Lean checker")


class _CheckWorker:
    """Own one persistent JSON-lines worker and its bounded response stream."""

    def __init__(self, project: Path, workspace: Path) -> None:
        self.project = project
        self.workspace = workspace
        self.environment_generation = ""
        self.private = Path(tempfile.mkdtemp(prefix="leanflow-check-")).resolve()
        self.process: subprocess.Popen[bytes] | None = None
        self.tree: ProcessTree | None = None
        self.buffer = bytearray()
        self.errors = bytearray()
        self.closed = False
        self._close_lock = threading.Lock()
        self._closing = False
        self._processes_verified = False
        self._output_eof_verified = False
        self.has_checked = False
        try:
            script = Path(__file__).with_name("check_worker.py").resolve()
            bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')"
            argv = [
                sys.executable,
                "-I",
                "-B",
                "-u",
                "-c",
                bootstrap,
                str(script.parents[3]),
                str(script),
                str(project),
                str(workspace),
                str(self.private),
            ]
            command = _sandbox_command(
                argv, project=project, workspace=workspace, private=self.private
            )
            self.process = subprocess.Popen(
                command,
                cwd=script.parents[3],
                env=_environment(self.private),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.tree = ProcessTree(self.process.pid, process=self.process)
            assert self.process.stdout is not None and self.process.stderr is not None
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stderr.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Keep ownership retryable until every checker process has verifiably stopped."""
        with self._close_lock:
            if self.closed:
                return
            self._closing = True
            if self.process is not None:
                if not self._processes_verified:
                    _kill(self.process, self.tree)
                    self._processes_verified = True
                if not self._output_eof_verified:
                    _require_output_eof(self.process)
                    self._output_eof_verified = True
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            shutil.rmtree(self.private, ignore_errors=True)
            self.closed = True
            self._closing = False

    def _require_open(self, *, refresh: bool = False) -> None:
        """Serialize root sampling with close and refuse retired or uncertain workers."""
        with self._close_lock:
            assert self.process is not None
            if self.closed or self._closing or self.process.returncode is not None:
                self._closing = not self.closed
                raise NativeCleanupUnverified(
                    "Lean checker is closing or already reaped; complete its cleanup before reuse"
                )
            if refresh and self.tree is not None:
                self.tree.refresh()

    def request(
        self,
        request: dict[str, Any],
        timeout_s: float | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Exchange one bounded request; kill the entire worker on deadline/protocol errors."""
        assert self.process is not None and self.process.stdin is not None
        process = self.process
        assert process.stdin is not None
        self._require_open()
        payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > _MAX_MESSAGE:
            return _failure("check_request_too_large", "Lean checker request exceeds 4 MiB.")
        deadline = deadline_after(timeout_s)
        try:
            # The worker's input is bounded; selectors handle backpressure as well as results.
            os.set_blocking(process.stdin.fileno(), False)
            offset = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE, "input")
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ, "output")
                selector.register(process.stderr, selectors.EVENT_READ, "error")
                while True:
                    self._require_open(refresh=True)
                    if operation_cancelled() or (cancelled is not None and cancelled()):
                        self.close()
                        return _failure(
                            "check_cancelled", "The controller cancelled this Lean check."
                        )
                    if b"\n" in self.buffer:
                        line, _, remainder = self.buffer.partition(b"\n")
                        self.buffer = bytearray(remainder)
                        result = json.loads(line)
                        if (
                            not isinstance(result, dict)
                            or result.get("id") != request["id"]
                            or not isinstance(result.get("result"), dict)
                        ):
                            raise ValueError("Lean checker returned a mismatched response.")
                        return dict(result["result"])
                    remaining = remaining_seconds(deadline)
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Protected Lean check exceeded its wall-clock deadline.")
                    for key, _ in selector.select(wait_slice(deadline)):
                        if key.data == "input":
                            try:
                                offset += os.write(key.fd, payload[offset:])
                            except BrokenPipeError:
                                # A failed startup can close stdin before its
                                # diagnostic streams are drained.
                                selector.unregister(process.stdin)
                                continue
                            if offset == len(payload):
                                selector.unregister(process.stdin)
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                if key.data == "output":
                                    # Startup diagnostics may arrive in the same select cycle
                                    # after stdout EOF. Keep the actual sandbox/setup reason.
                                    while True:
                                        try:
                                            tail = os.read(process.stderr.fileno(), 65536)
                                        except BlockingIOError:
                                            break
                                        if not tail:
                                            break
                                        self.errors.extend(tail)
                                        self.errors = self.errors[-_MAX_ERROR:]
                                    raise RuntimeError(
                                        "Protected Lean worker exited: "
                                        + self.errors.decode("utf-8", errors="replace")
                                    )
                            elif key.data == "error":
                                self.errors.extend(chunk)
                                self.errors = self.errors[-_MAX_ERROR:]
                            else:
                                self.buffer.extend(chunk)
                                if len(self.buffer) > _MAX_MESSAGE:
                                    raise ValueError("Lean checker response exceeds 4 MiB.")
        except NativeCleanupUnverified:
            raise
        except TimeoutError as exc:
            self.close()
            return _failure("check_timeout", str(exc), timed_out=True)
        except (OSError, ValueError, RuntimeError) as exc:
            self.close()
            return _failure("check_setup_failed", str(exc))


def check_scratch(
    *,
    project_root: Path,
    workspace: Path,
    file: Path,
    declaration: str = "",
    replacement: str = "",
    timeout_s: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    include_axiom_profile: bool = False,
    allow_placeholders_for_elaboration: bool = False,
) -> dict[str, Any]:
    """Check one target/file in a warm process that cannot modify canonical sources."""
    started = time.monotonic()
    deadline = deadline_after(timeout_s)
    try:
        project, work = _validated_paths(project_root, workspace)
        target = file.resolve(strict=True)
        if not target.is_relative_to(project) and not target.is_relative_to(work):
            raise ValueError("Lean check file must belong to the project or checker workspace.")
        key = (project, work)
        with _LOCK:
            # The slot lock outlives workers so queued callers cannot race a
            # stale worker replacement and execute two checks in one workspace.
            slot = _WORKSPACE_LOCKS.setdefault(key, threading.Lock())
        while not slot.acquire(timeout=wait_slice(deadline)):
            if operation_cancelled() or (cancelled is not None and cancelled()):
                return _failure(
                    "check_cancelled", "The controller cancelled the queued Lean check."
                )
            if deadline is not None and time.monotonic() >= deadline:
                return _failure(
                    "check_busy",
                    "A prior check still owns this workspace's Lean worker.",
                    timed_out=True,
                )
        try:
            generation = lean_environment_generation(project, excluded_sources=(target,))
            source_hash = hashlib.sha256(target.read_bytes()).digest()
            remaining = remaining_seconds(deadline)
            stopped = operation_cancelled() or (cancelled is not None and cancelled())
            if stopped or (remaining is not None and remaining <= 0):
                with _LOCK:
                    if key in _WORKERS:
                        _WORKERS[key].close()
                return _failure(
                    "check_cancelled" if stopped else "check_timeout",
                    "The controller stopped this check before Lean execution.",
                    timed_out=not stopped,
                )
            with _LOCK:
                worker = _WORKERS.get(key)
                if worker is not None and (
                    worker.environment_generation != generation
                    or getattr(worker, "_closing", False)
                ):
                    worker.close()
                if worker is None or worker.closed:
                    worker = _CheckWorker(project, work)
                    worker.environment_generation = generation
                    _WORKERS[key] = worker
            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                worker.close()
                return _failure(
                    "check_timeout", "Lean startup consumed the check deadline.", timed_out=True
                )
            cold_start = not getattr(worker, "has_checked", False)
            result = worker.request(
                {
                    "id": uuid.uuid4().hex,
                    "file": str(target),
                    "declaration": declaration,
                    "replacement": replacement,
                    "timeout_s": remaining,
                    "include_axiom_profile": include_axiom_profile,
                    "allow_placeholders_for_elaboration": allow_placeholders_for_elaboration,
                },
                remaining,
                **({"cancelled": cancelled} if cancelled is not None else {}),
            )
            if (
                generation != lean_environment_generation(project, excluded_sources=(target,))
                or source_hash != hashlib.sha256(target.read_bytes()).digest()
            ):
                worker.close()
                result = _failure(
                    "check_environment_changed",
                    "Lean source or dependencies changed during this check; run it again.",
                )
            worker.has_checked = True
            return {
                **result,
                "timeout_s": timeout_s,
                "elapsed_s": round(time.monotonic() - started, 3),
                "cold_start": cold_start,
                "timing_scope": "worker queue, startup, imports and elaboration",
            }
        finally:
            slot.release()
    except IsolationUnavailable as exc:
        return _failure("isolation_unavailable", str(exc))
    except (OSError, ValueError) as exc:
        return _failure("check_setup_failed", str(exc))


def _close_owned_workers(workspace: Path | None) -> None:
    """Attempt every matching owner before surfacing any unresolved cleanup boundary."""
    failures: list[Exception] = []
    try:
        close_commands(workspace)
    except Exception as error:
        failures.append(error)
    with _LOCK:
        workers = [
            (key, worker)
            for key, worker in _WORKERS.items()
            if workspace is None or key[1] == workspace
        ]
    for key, worker in workers:
        try:
            worker.close()
        except Exception as error:
            failures.append(error)
        else:
            with _LOCK:
                if _WORKERS.get(key) is worker:
                    _WORKERS.pop(key)
    if failures:
        raise NativeCleanupUnverified(
            f"Checker cleanup remains unverified for {len(failures)} owner(s): {failures[0]}"
        ) from failures[0]


def close_check_workers(workspace: Path) -> None:
    """Release every matching checker and retain each owner whose closure is uncertain."""
    _close_owned_workers(workspace.resolve())


def preflight_check(
    *,
    project_root: Path,
    workspace: Path,
    timeout_s: float | None = None,
    source_file: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Verify sandboxed LeanProbe and assigned imports before spending a model request."""
    path: Path | None = None
    try:
        _, work = _validated_paths(project_root, workspace)
        from leanflow_cli.lean.lean_module_paths import _lean_imports_from_text

        imports = ["Lean"]
        if source_file is not None:
            imports += _lean_imports_from_text(source_file.read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lean", prefix="Preflight_", dir=work, delete=False
        ) as source:
            path = Path(source.name)
            source.write("".join(f"import {module}\n" for module in dict.fromkeys(imports)))
            source.write("theorem leanflowPreflight : True := by trivial\n")
        return check_scratch(
            project_root=project_root,
            workspace=workspace,
            file=path,
            declaration="leanflowPreflight",
            timeout_s=timeout_s,
            include_axiom_profile=True,
            cancelled=cancelled,
        )
    except (OSError, ValueError) as exc:
        return _failure("check_setup_failed", str(exc))
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def isolated_command(
    *,
    project_root: Path,
    workspace: Path,
    argv: Sequence[str],
    timeout_s: float | None = None,
    extra_writable_roots: Sequence[Path] = (),
    extra_readable_roots: Sequence[Path] = (),
    network_allowed: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a controller-owned command with only deliberately granted build outputs writable."""
    private: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    tree: ProcessTree | None = None
    ownership: CommandOwnership | None = None
    interrupted: dict[str, Any] | None = None
    try:
        if timeout_s is not None and timeout_s <= 0:
            return _failure(
                "check_timeout", "Campaign deadline expired before command launch.", timed_out=True
            )
        project, work = _validated_paths(project_root, workspace)
        if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
            raise ValueError("An isolated command requires a valid argument array.")
        private = Path(tempfile.mkdtemp(prefix="leanflow-command-")).resolve()
        command = _sandbox_command(
            argv,
            project=project,
            workspace=work,
            private=private,
            extra_writable_roots=extra_writable_roots,
            extra_readable_roots=extra_readable_roots,
            network_allowed=network_allowed,
        )
        process = subprocess.Popen(
            command,
            cwd=project,
            env=_environment(private),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        tree = ProcessTree(process.pid, process=process)
        ownership = CommandOwnership(
            work,
            private,
            process,
            tree,
            _kill,
            _require_output_eof,
            input_roots=(project, *extra_writable_roots, *extra_readable_roots),
        )
        stdout, stderr, truncated = _capture_command(
            process, timeout_s, cancelled=cancelled, tree=tree
        )
        diagnostic = stderr.decode("utf-8", errors="replace")
        if process.returncode and diagnostic.lstrip().startswith(("bwrap:", "sandbox-exec:")):
            return {
                **_failure("isolation_unavailable", diagnostic),
                "returncode": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": diagnostic,
            }
        return {
            "success": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": stdout[-_MAX_MESSAGE:].decode("utf-8", errors="replace"),
            "stderr": stderr[-_MAX_ERROR:].decode("utf-8", errors="replace"),
            "stdout_truncated": truncated["stdout"],
            "stderr_truncated": truncated["stderr"],
            "timed_out": False,
        }
    except NativeCleanupUnverified as error:
        # A final ownership.close() below may verify a transient failed stop.
        # Capture remains unsuccessful, but only that final close may claim retention.
        interrupted = {
            **_failure(
                "check_capture_failed", "Isolated command capture was interrupted; retry the check."
            ),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "capture_failure": capture_failure(error),
        }
        return interrupted
    except subprocess.TimeoutExpired:
        return {
            **_failure(
                "check_timeout",
                "Isolated command exceeded its wall-clock deadline.",
                timed_out=True,
            ),
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    except InterruptedError as exc:
        return {
            **_failure("check_cancelled", str(exc)),
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    except (IsolationUnavailable, OSError, ValueError) as exc:
        return {
            **_failure(
                (
                    "isolation_unavailable"
                    if isinstance(exc, IsolationUnavailable)
                    else "check_setup_failed"
                ),
                str(exc),
            ),
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    finally:
        if ownership is not None:
            try:
                ownership.close()
            except NativeCleanupUnverified as error:
                if interrupted is not None:
                    error.add_note(interrupted_capture_note(interrupted["capture_failure"]))
                raise
            if (
                interrupted is not None
                and ownership.closed
                and ownership.processes_verified
                and ownership.output_eof_verified
            ):
                interrupted["capture_cleanup"] = {
                    "processes_verified": True,
                    "output_eof_verified": True,
                    "closed": True,
                }
                if interrupted["capture_failure"]["leader_exited_before_cleanup"]:
                    assert process is not None
                    interrupted["capture_failure"]["leader_returncode"] = process.returncode
        elif private is not None:
            shutil.rmtree(private, ignore_errors=True)


def _stop_capture(process: subprocess.Popen[bytes], tree: ProcessTree) -> None:
    """Mark only cleanup failures originating at the command capture boundary."""
    try:
        _kill(process, tree)
    except NativeCleanupUnverified as error:
        raise CaptureInterruption("owned_stop", "Owned stop interrupted command capture") from error


def _capture_command(
    process: subprocess.Popen[bytes],
    timeout_s: float | None,
    *,
    cancelled: Callable[[], bool] | None = None,
    tree: ProcessTree | None = None,
) -> tuple[bytes, bytes, dict[str, bool]]:
    """Drain both output streams into bounded tails until exit or a hard deadline.

    The tails are bounded, so a chatty command loses the HEAD of its output.
    Report which streams that happened to: a caller parsing the result cannot
    otherwise tell a complete stream from the tail of a much longer one, and a
    process that exits 0 looks entirely successful either way.
    """
    assert process.stdout is not None and process.stderr is not None
    deadline = deadline_after(timeout_s)
    tails: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    truncated: dict[str, bool] = {"stdout": False, "stderr": False}
    drain_deadline: float | None = None
    with selectors.DefaultSelector() as selector:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or not process_exited(process):
            if tree is not None:
                if process.returncode is None:
                    tree.refresh()
                if process_exited(process) and drain_deadline is None:
                    # A background child may retain our pipes after the compiler
                    # exits. End its authority before trusting produced artifacts.
                    _stop_capture(process, tree)
                    drain_deadline = time.monotonic() + 2.0
            if operation_cancelled() or (cancelled is not None and cancelled()):
                raise InterruptedError("The controller cancelled this operation")
            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                assert timeout_s is not None
                raise subprocess.TimeoutExpired(process.args, timeout_s)
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                raise CaptureInterruption(
                    "output_eof", "Lean checker output pipes remain open after leader exit"
                )
            for key, _ in selector.select(wait_slice(deadline)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                tail = tails[key.data]
                tail.extend(chunk)
                limit = _MAX_MESSAGE if key.data == "stdout" else _MAX_ERROR
                if len(tail) > limit:
                    truncated[key.data] = True
                    del tail[:-limit]
    if tree is not None and drain_deadline is None:
        # Both channels can reach EOF in the same observation that notices exit.
        # Stop the still-reserved group before wait() releases the original PID.
        _stop_capture(process, tree)
    remaining = remaining_seconds(deadline)
    process.wait(timeout=remaining)
    return bytes(tails["stdout"]), bytes(tails["stderr"]), truncated


def _close_all() -> None:
    """Retry retained commands and reap warm subprocesses when the controller exits normally."""
    _close_owned_workers(None)


atexit.register(_close_all)
