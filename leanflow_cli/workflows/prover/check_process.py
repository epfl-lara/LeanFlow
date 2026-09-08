"""Run warm Lean feedback in OS-isolated workers with explicit writable roots."""

from __future__ import annotations

import atexit
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

from leanflow_cli.workflows.prover.check_sandbox import (
    IsolationUnavailable,
    _environment,
    _sandbox_command,
    _validated_paths,
)

_MAX_MESSAGE = 4 * 1024 * 1024
_MAX_ERROR = 16 * 1024
_WORKERS: dict[tuple[Path, Path], _CheckWorker] = {}
_LOCK = threading.RLock()


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill the owned process group, then reap its direct process."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for the empty group during child exit before
        # waitpid reaps it. A live owned leader still needs an explicit kill.
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _failure(code: str, error: str, *, timed_out: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "ok": False,
        "error_code": code,
        "error": error[:_MAX_ERROR],
        "timed_out": timed_out,
    }


class _CheckWorker:
    """Own one persistent JSON-lines worker and its bounded response stream."""

    def __init__(self, project: Path, workspace: Path) -> None:
        self.project = project
        self.workspace = workspace
        self.lock = threading.Lock()
        self.private = Path(tempfile.mkdtemp(prefix="leanflow-check-")).resolve()
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.errors = bytearray()
        self.closed = False
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
            assert self.process.stdout is not None and self.process.stderr is not None
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stderr.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Stop all same-group descendants and remove only private checker caches."""
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            _kill(self.process)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        shutil.rmtree(self.private, ignore_errors=True)

    def request(
        self,
        request: dict[str, Any],
        timeout_s: float,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Exchange one bounded request; kill the entire worker on deadline/protocol errors."""
        assert self.process is not None and self.process.stdin is not None
        process = self.process
        assert process.stdin is not None
        payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > _MAX_MESSAGE:
            return _failure("check_request_too_large", "Lean checker request exceeds 4 MiB.")
        deadline = time.monotonic() + timeout_s
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
                    if cancelled is not None and cancelled():
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
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Protected Lean check exceeded its wall-clock deadline.")
                    for key, _ in selector.select(min(remaining, 0.2)):
                        if key.data == "input":
                            offset += os.write(key.fd, payload[offset:])
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
    timeout_s: float = 60,
    cancelled: Callable[[], bool] | None = None,
    include_axiom_profile: bool = False,
    allow_placeholders_for_elaboration: bool = False,
) -> dict[str, Any]:
    """Check one target/file in a warm process that cannot modify canonical sources."""
    started = time.monotonic()
    deadline = started + max(0, timeout_s)
    try:
        project, work = _validated_paths(project_root, workspace)
        target = file.resolve(strict=True)
        if not target.is_relative_to(project) and not target.is_relative_to(work):
            raise ValueError("Lean check file must belong to the project or checker workspace.")
        key = (project, work)
        with _LOCK:
            worker = _WORKERS.get(key)
            if worker is None or worker.closed:
                worker = _CheckWorker(project, work)
                _WORKERS[key] = worker
        while not worker.lock.acquire(timeout=min(0.2, max(0, deadline - time.monotonic()))):
            if cancelled is not None and cancelled():
                return _failure(
                    "check_cancelled", "The controller cancelled the queued Lean check."
                )
            if time.monotonic() >= deadline:
                return _failure(
                    "check_busy",
                    "A prior check still owns this workspace's Lean worker.",
                    timed_out=True,
                )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
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
                    "timeout_s": max(1, int(timeout_s)),
                    "include_axiom_profile": include_axiom_profile,
                    "allow_placeholders_for_elaboration": allow_placeholders_for_elaboration,
                },
                remaining,
                **({"cancelled": cancelled} if cancelled is not None else {}),
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
            worker.lock.release()
    except IsolationUnavailable as exc:
        return _failure("isolation_unavailable", str(exc))
    except (OSError, ValueError) as exc:
        return _failure("check_setup_failed", str(exc))


def close_check_workers(workspace: Path) -> None:
    """Release every checker belonging to this exact workspace."""
    work = workspace.resolve()
    with _LOCK:
        for key in [key for key in _WORKERS if key[1] == work]:
            _WORKERS.pop(key).close()


def preflight_check(
    *, project_root: Path, workspace: Path, timeout_s: float = 60
) -> dict[str, Any]:
    """Verify the OS sandbox and Lean REPL before spending the first model request."""
    path: Path | None = None
    try:
        _, work = _validated_paths(project_root, workspace)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lean", prefix="Preflight_", dir=work, delete=False
        ) as source:
            path = Path(source.name)
            source.write("import Lean\ntheorem leanflowPreflight : True := by trivial\n")
        return check_scratch(
            project_root=project_root,
            workspace=workspace,
            file=path,
            declaration="leanflowPreflight",
            timeout_s=timeout_s,
            include_axiom_profile=True,
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
    timeout_s: float = 60,
    extra_writable_roots: Sequence[Path] = (),
    network_allowed: bool = False,
) -> dict[str, Any]:
    """Run a controller-owned command with only deliberately granted build outputs writable."""
    private: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
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
        stdout, stderr, truncated = _capture_command(process, max(0.01, timeout_s))
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
    except subprocess.TimeoutExpired:
        if process is not None:
            _kill(process)
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
        if process is not None:
            _kill(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)


def _capture_command(
    process: subprocess.Popen[bytes], timeout_s: float
) -> tuple[bytes, bytes, dict[str, bool]]:
    """Drain both output streams into bounded tails until exit or a hard deadline.

    The tails are bounded, so a chatty command loses the HEAD of its output.
    Report which streams that happened to: a caller parsing the result cannot
    otherwise tell a complete stream from the tail of a much longer one, and a
    process that exits 0 looks entirely successful either way.
    """
    assert process.stdout is not None and process.stderr is not None
    deadline = time.monotonic() + timeout_s
    tails: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    truncated: dict[str, bool] = {"stdout": False, "stderr": False}
    with selectors.DefaultSelector() as selector:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout_s)
            for key, _ in selector.select(min(remaining, 0.2)):
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
    remaining = deadline - time.monotonic()
    process.wait(timeout=max(0.001, remaining))
    return bytes(tails["stdout"]), bytes(tails["stderr"]), truncated


def _close_all() -> None:
    """Reap warm subprocesses when the controller exits normally."""
    with _LOCK:
        for worker in _WORKERS.values():
            worker.close()
        _WORKERS.clear()


atexit.register(_close_all)
