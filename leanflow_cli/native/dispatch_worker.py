"""Run one dispatch backend in an isolated subprocess and write its result."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from core.process_identity import current_process_identity, process_identity_details
from core.provider_capacity import background_actor_lease
from core.utils import atomic_json_write
from leanflow_cli.workflows.dispatch_models import JobSpec

try:  # POSIX launch fencing; the parent uses the same sidecar.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

PARENT_POLL_INTERVAL_S = 1.0
PARENT_CLEANUP_GRACE_S = 10.0
DESCENDANT_TERM_GRACE_S = 1.0
_WORKER_ENV_KEYS = (
    "LEANFLOW_DISPATCH_WORKER",
    "LEANFLOW_RESEARCH_MODE",
    "LEANFLOW_RESEARCH_WORKERS",
    "LEANFLOW_NATIVE_WORKFLOW_KIND",
    "LEANFLOW_NATIVE_ACTIVE_FILE",
    "LEANFLOW_DISPATCH_SCRATCH_ONLY",
    "LEANFLOW_DISPATCH_ARCHETYPE",
)


class _LaunchNonceMismatch(ValueError):
    """Reject a stale worker after a parent rotated its launch transaction."""


class _LaunchParentMissing(RuntimeError):
    """Reject a worker whose expected launch parent disappeared."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--launch-nonce", default="")
    parser.add_argument("--identity-file", default="")
    parser.add_argument("--evidence-file", default="")
    parser.add_argument("--launch-lock-file", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    return parser.parse_args()


def _publish_launch_identity(identity_file: str, launch_nonce: str) -> None:
    """Publish the nonce-bound worker identity before entering the backend."""
    if not identity_file or not launch_nonce:
        return
    identity = current_process_identity()
    if not identity.verifiable or identity.process_group_id <= 0 or identity.session_id <= 0:
        raise RuntimeError("dispatch worker lacks an exact launch process identity")
    atomic_json_write(
        Path(identity_file).expanduser().resolve(),
        {
            "version": 1,
            "launch_nonce": launch_nonce,
            **process_identity_details(identity),
            "parent_process_id": os.getppid(),
        },
        sort_keys=True,
    )


def _load_nonce_bound_spec(spec_path: Path, launch_nonce: str) -> dict[str, Any]:
    """Load one worker spec only while its durable launch nonce is current."""
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("dispatch worker spec must be a JSON object")
    if launch_nonce:
        persisted_nonce = str(raw.get("launch_nonce", "") or "").strip()
        spec_payload = raw.get("spec")
        if persisted_nonce != launch_nonce or not isinstance(spec_payload, dict):
            raise _LaunchNonceMismatch("dispatch launch nonce/spec envelope mismatch")
        return spec_payload
    legacy_payload = raw.get("spec")
    return dict(legacy_payload) if isinstance(legacy_payload, dict) else raw


@contextlib.contextmanager
def _launch_spec_fence(launch_lock_file: str) -> Iterator[None]:
    """Serialize the final spec recheck with parent rotation and commit."""
    normalized = str(launch_lock_file or "").strip()
    if not normalized or fcntl is None:
        yield
        return
    path = Path(normalized).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parent_process_alive(parent_pid: int) -> bool:
    """Return whether the worker still belongs to its expected direct parent."""
    if parent_pid <= 1:
        return True
    if os.getppid() != parent_pid:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _force_exit_orphaned_worker() -> None:
    """Terminate detached descendants and force-exit after graceful cleanup stalls."""
    from leanflow_cli.native.runtime_cleanup import exit_native_process
    from leanflow_cli.workflows.dispatch_service import _descendant_process_ids

    for child_pid in _descendant_process_ids(os.getpid()):
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(child_pid, signal.SIGTERM)
    time.sleep(DESCENDANT_TERM_GRACE_S)
    # Refresh the tree because a service can fork while handling SIGTERM.
    for child_pid in _descendant_process_ids(os.getpid()):
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)
    exit_native_process(1)


@contextlib.contextmanager
def _assignment_local_worker_environment(spec: JobSpec) -> Iterator[None]:
    """Bound one isolated worker to its assignment without nested portfolios."""
    previous = {key: os.environ.get(key) for key in _WORKER_ENV_KEYS}
    active_file = str(spec.inputs.get("active_file", "") or "").strip()
    overrides = {
        "LEANFLOW_DISPATCH_WORKER": "1",
        "LEANFLOW_RESEARCH_MODE": "0",
        "LEANFLOW_RESEARCH_WORKERS": "0",
        # Research dispatch jobs are proof-support lanes even when a unit test
        # or direct worker invocation lacks the parent runner's environment.
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "prove",
        # Defense in depth for any writer that accidentally leaks past the
        # scratch-only delegate toolset restriction.
        "LEANFLOW_DISPATCH_SCRATCH_ONLY": ("1" if spec.scope.get("scratch_only") is True else "0"),
        "LEANFLOW_DISPATCH_ARCHETYPE": spec.archetype,
    }
    if active_file:
        overrides["LEANFLOW_NATIVE_ACTIVE_FILE"] = active_file
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _worker_autonomy_state(spec: JobSpec) -> dict[str, Any]:
    """Build the minimal assignment state consumed by managed search guards."""
    inputs = dict(spec.inputs or {})
    assignment = {
        "target_symbol": str(inputs.get("target_symbol", "") or "").strip(),
        "active_file": str(inputs.get("active_file", "") or "").strip(),
    }
    assignment_slice = str(inputs.get("slice", "") or "").strip()
    if assignment_slice:
        assignment["slice"] = assignment_slice
    return {
        "current_queue_assignment": assignment,
        "dispatch_worker_job_id": spec.job_id,
        "dispatch_worker_archetype": spec.archetype,
    }


def _install_tool_availability_reporter(agent: Any, spec: JobSpec) -> None:
    """Report parent registry schemas separately from delegated model availability."""
    registry_names = sorted(
        str(name)
        for name in (getattr(agent, "valid_tool_names", set()) or set())
        if str(name).strip()
    )
    registry_summary = ", ".join(registry_names) if registry_names else "(none)"
    print(
        "🧰 Dispatch parent configured tool registry "
        f"({len(registry_names)} schemas; orchestration host only, not effective "
        f"delegated availability): {registry_summary}",
        flush=True,
    )

    def report(*, requested_toolsets: list[str], effective_tool_names: list[str]) -> None:
        """Print the exact tool schemas exposed to the delegated model."""
        normalized_toolsets = [
            str(name).strip() for name in requested_toolsets if str(name).strip()
        ]
        normalized_tools = sorted(
            {str(name).strip() for name in effective_tool_names if str(name).strip()}
        )
        toolset_summary = ", ".join(normalized_toolsets) if normalized_toolsets else "(default)"
        tool_summary = ", ".join(normalized_tools) if normalized_tools else "(none)"
        print(
            "🔒 Dispatch effective delegated tool availability "
            f"({len(normalized_tools)} schemas after runtime filtering; requested toolsets: "
            f"{toolset_summary}; job: {spec.job_id}): {tool_summary}",
            flush=True,
        )

    agent._delegated_tool_availability_reporter = report


class ParentLivenessGuard:
    """Stop a detached dispatch worker after parent loss or budget exhaustion."""

    def __init__(self, parent_pid: int):
        self.parent_pid = max(0, int(parent_pid))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._shutdown_requested = threading.Event()
        self._shutdown_signum = 0
        self._wall_clock_deadline: float | None = None
        self._wall_clock_exhausted = False
        self._callback_lock = threading.Lock()
        self._interrupt_callback: Callable[[str], None] | None = None
        self._thread: threading.Thread | None = None
        self._parent_lost = False

    def set_interrupt_callback(self, callback: Callable[[str], None] | None) -> None:
        """Register the worker agent's cooperative interruption callback."""
        with self._callback_lock:
            self._interrupt_callback = callback

    def request_shutdown(self, signum: int = 0) -> None:
        """Wake the guard when a process-level termination signal arrives."""
        if signum:
            self._shutdown_signum = int(signum)
        self._shutdown_requested.set()
        self._wake.set()

    def set_wall_clock_budget(self, wall_clock_s: int) -> None:
        """Enforce the assignment budget independently of parent polling.

        Parent reconciliation remains the durable ledger authority, but the
        worker must release its own process and provider capacity even while
        the parent is blocked in a long Lean verification call.
        """
        self._wall_clock_deadline = time.monotonic() + max(0, int(wall_clock_s))
        self._wake.set()

    def start(self) -> None:
        """Start the daemon liveness monitor once."""
        if self._thread is not None:
            return
        if self.parent_pid > 1 and not _parent_process_alive(self.parent_pid):
            self._parent_lost = True
            self._shutdown_requested.set()
        self._thread = threading.Thread(
            target=self._run,
            name="leanflow-dispatch-parent-guard",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitor after the worker reaches a normal terminal boundary."""
        self._stop.set()
        self._shutdown_requested.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)

    def _interrupt_reason(self) -> str:
        """Return the shutdown cause without conflating signals with parent loss."""
        if self._parent_lost:
            return "dispatch worker parent exited"
        if self._wall_clock_exhausted:
            return "dispatch worker wall-clock budget exhausted"
        if self._shutdown_signum:
            try:
                signal_name = signal.Signals(self._shutdown_signum).name
            except ValueError:
                signal_name = str(self._shutdown_signum)
            return f"dispatch worker received {signal_name}"
        return "dispatch worker termination requested"

    def _interrupt_agent(self, reason: str) -> None:
        """Cooperatively interrupt the active delegate before process teardown."""
        with self._callback_lock:
            callback = self._interrupt_callback
        if callback is not None:
            with contextlib.suppress(Exception, KeyboardInterrupt):
                callback(reason)

    def _run(self) -> None:
        """Poll parent identity, initiate cleanup, and enforce a bounded exit."""
        while not self._stop.is_set():
            wait_s = PARENT_POLL_INTERVAL_S
            deadline = self._wall_clock_deadline
            if deadline is not None:
                wait_s = min(wait_s, max(0.0, deadline - time.monotonic()))
            self._wake.wait(wait_s)
            self._wake.clear()
            if self._shutdown_requested.is_set():
                break
            deadline = self._wall_clock_deadline
            if deadline is not None and time.monotonic() >= deadline:
                self._wall_clock_exhausted = True
                self._shutdown_requested.set()
                break
            if self.parent_pid > 1 and not _parent_process_alive(self.parent_pid):
                self._parent_lost = True
                self._shutdown_requested.set()
                break
        if self._stop.is_set():
            return
        self._interrupt_agent(self._interrupt_reason())
        if self._parent_lost:
            # SIGHUP cannot reach this worker because deploy_async deliberately
            # starts a new session.  Ask the main thread to run its cleanup.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(os.getpid(), signal.SIGTERM)
        if self._stop.wait(PARENT_CLEANUP_GRACE_S):
            return
        _force_exit_orphaned_worker()


def run_worker(
    spec: JobSpec,
    *,
    parent_guard: ParentLivenessGuard | None = None,
    evidence_file: str = "",
    launch_nonce: str = "",
) -> dict[str, Any]:
    """Build an isolated parent agent, execute the backend, and reap its services."""
    from leanflow_cli.native.native_runner import _build_agent, _prepare_managed_turn_state
    from leanflow_cli.native.runtime_cleanup import shutdown_native_runtime_services
    from leanflow_cli.runtime.file_locks import release_all_file_locks
    from leanflow_cli.workflows import dispatch_incremental_evidence
    from leanflow_cli.workflows.dispatch_service import DispatchService

    # Acquire before _build_agent so a queued worker process remains a small
    # orchestration shell rather than constructing a resident model/Lean
    # conversation beyond the campaign's actor capacity.
    with _assignment_local_worker_environment(spec), background_actor_lease():
        agent = _build_agent()
        _install_tool_availability_reporter(agent, spec)
        _prepare_managed_turn_state(agent, _worker_autonomy_state(spec))
        interrupt = getattr(agent, "interrupt", None)
        if parent_guard is not None and callable(interrupt):
            parent_guard.set_interrupt_callback(interrupt)
        try:
            evidence_path = (
                Path(evidence_file).expanduser().resolve()
                if str(evidence_file or "").strip() and str(launch_nonce or "").strip()
                else None
            )

            def publish_incremental_evidence(helpers: Sequence[Mapping[str, Any]]) -> None:
                """Persist the latest exact helper set outside shared plan/graph state."""
                if evidence_path is None:
                    return
                dispatch_incremental_evidence.publish_checked_helpers(
                    evidence_path,
                    launch_nonce=launch_nonce,
                    spec=spec,
                    helpers=helpers,
                )

            service = DispatchService(
                parent_agent=agent,
                incremental_evidence_sink=(
                    publish_incremental_evidence if evidence_path is not None else None
                ),
            )
            return service._run_backend(spec)
        finally:
            # Dispatch workers start their own MCP and warm Lean subprocess trees.
            # Python process exit does not reliably reap new-session grandchildren,
            # so close them before publishing the worker's terminal boundary.
            with contextlib.suppress(Exception, KeyboardInterrupt):
                shutdown_native_runtime_services(agent)
            owner_id = str(getattr(agent, "session_id", "") or "")
            if owner_id:
                with contextlib.suppress(Exception, KeyboardInterrupt):
                    release_all_file_locks(owner_id=owner_id)
            if parent_guard is not None:
                parent_guard.set_interrupt_callback(None)


def main() -> int:
    """Read a JobSpec, run it, and atomically publish a bounded result."""
    args = _parse_args()
    spec_path = Path(args.spec_file).expanduser().resolve()
    result_path = Path(args.result_file).expanduser().resolve()
    launch_nonce = str(getattr(args, "launch_nonce", "") or "").strip()
    identity_file = str(getattr(args, "identity_file", "") or "").strip()
    evidence_file = str(getattr(args, "evidence_file", "") or "").strip()
    launch_lock_file = str(getattr(args, "launch_lock_file", "") or "").strip()
    from leanflow_cli.native.runtime_cleanup import (
        install_native_termination_handlers,
        restore_native_termination_handlers,
    )

    parent_guard = ParentLivenessGuard(args.parent_pid)
    termination_handlers = install_native_termination_handlers(parent_guard.request_shutdown)
    try:
        spec_payload = _load_nonce_bound_spec(spec_path, launch_nonce)
        # Validate before publishing identity, then fence once more immediately
        # before backend entry. A worker suspended across a parent-side retry
        # cannot resume stale work or publish a stale result.
        _publish_launch_identity(identity_file, launch_nonce)
        parent_guard.start()
        if launch_nonce:
            with _launch_spec_fence(launch_lock_file):
                spec_payload = _load_nonce_bound_spec(spec_path, launch_nonce)
                if not _parent_process_alive(args.parent_pid):
                    raise _LaunchParentMissing(
                        "dispatch launch parent disappeared before backend entry"
                    )
        spec = JobSpec.from_mapping(spec_payload)
        problems = spec.validate()
        if problems:
            raise ValueError("; ".join(problems))
        parent_guard.set_wall_clock_budget(spec.budget.wall_clock_s)
        result = run_worker(
            spec,
            parent_guard=parent_guard,
            evidence_file=evidence_file,
            launch_nonce=launch_nonce,
        )
    except (_LaunchNonceMismatch, _LaunchParentMissing):
        return 1
    except BaseException as exc:
        atomic_json_write(
            result_path,
            {
                "launch_nonce": launch_nonce,
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            },
            sort_keys=True,
        )
        return 1
    else:
        atomic_json_write(
            result_path,
            {"launch_nonce": launch_nonce, "ok": True, "result": result},
            sort_keys=True,
        )
        return 0
    finally:
        parent_guard.stop()
        restore_native_termination_handlers(termination_handlers)


if __name__ == "__main__":
    from leanflow_cli.native.runtime_cleanup import exit_native_process

    exit_native_process(main())
