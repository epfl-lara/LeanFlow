"""Characterize project-scoped admission for memory-heavy Lean work."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from core import project_resource_admission as admission
from core.project_resource_admission import (
    MAX_FOREGROUND_HANDOFF_LEASE_S,
    ProjectLeanAdmission,
    ProjectLeanAdmissionRetained,
    project_lean_admission_observer,
    project_lean_heavy_admission,
    project_lean_service_reclaim_enabled,
    reserve_project_foreground_priority_lease,
)


def _ordered_admission_worker(
    root: str,
    role: str,
    attempted: Any,
    entered: Any,
    release: Any,
) -> None:
    """Enter one cross-process admission and report its observed order."""
    if role == "background":
        os.environ["LEANFLOW_DISPATCH_WORKER"] = "1"
    else:
        os.environ.pop("LEANFLOW_DISPATCH_WORKER", None)
    attempted.set()
    try:
        with project_lean_heavy_admission(root):
            entered.put(role)
            release.wait(timeout=10)
    except Exception as exc:
        entered.put(f"error:{role}:{type(exc).__name__}:{exc}")


def _event_admission_worker(
    root: str,
    attempted: Any,
    entered: Any,
    release: Any,
) -> None:
    """Wait as a dispatch worker and expose exact admission timing."""
    os.environ["LEANFLOW_DISPATCH_WORKER"] = "1"
    attempted.set()
    with project_lean_heavy_admission(root):
        entered.set()
        release.wait(timeout=10)


def _stop_process(process: multiprocessing.Process | None) -> None:
    """Join one test worker and terminate it if an assertion interrupted release."""
    if process is None:
        return
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)


def test_resident_service_reclaim_is_limited_to_dispatch_workers(monkeypatch):
    """Keep the foreground incremental service warm across proving calls."""
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)

    assert project_lean_service_reclaim_enabled() is False

    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")

    assert project_lean_service_reclaim_enabled() is True


def test_project_gate_blocks_a_second_process_until_the_holder_releases(tmp_path: Path) -> None:
    """Keep a second process out of a project's Lean-heavy slot."""
    ready = tmp_path / "child-entered"
    child_code = "\n".join(
        [
            "from pathlib import Path",
            "import sys",
            "from core.project_resource_admission import project_lean_heavy_admission",
            "root = Path(sys.argv[1])",
            "ready = Path(sys.argv[2])",
            "with project_lean_heavy_admission(root):",
            "    ready.write_text('entered', encoding='utf-8')",
        ]
    )
    with project_lean_heavy_admission(tmp_path) as first:
        assert first.enforced
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(tmp_path), str(ready)],
            cwd=Path(__file__).parents[2],
        )
        try:
            time.sleep(0.15)
            assert not ready.exists()
        finally:
            # The context exit below releases the cross-process lock before
            # waiting for the child, so this test cannot strand a child.
            pass
    try:
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0
    assert ready.read_text(encoding="utf-8") == "entered"


@pytest.mark.skipif(admission.fcntl is None, reason="foreground priority requires flock")
def test_waiting_foreground_overtakes_an_already_waiting_dispatch_worker(
    tmp_path: Path,
) -> None:
    """Stop a queued background reacquisition from starving foreground Lean."""
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    background_attempted = context.Event()
    foreground_attempted = context.Event()
    background_release = context.Event()
    foreground_release = context.Event()
    background: multiprocessing.Process | None = None
    foreground: multiprocessing.Process | None = None
    try:
        with project_lean_heavy_admission(tmp_path):
            background = context.Process(
                target=_ordered_admission_worker,
                args=(
                    str(tmp_path),
                    "background",
                    background_attempted,
                    entered,
                    background_release,
                ),
            )
            background.start()
            assert background_attempted.wait(timeout=5)

            foreground = context.Process(
                target=_ordered_admission_worker,
                args=(
                    str(tmp_path),
                    "foreground",
                    foreground_attempted,
                    entered,
                    foreground_release,
                ),
            )
            foreground.start()
            assert foreground_attempted.wait(timeout=5)
            deadline = time.monotonic() + 5
            while not admission._foreground_waiter_exists(tmp_path.resolve()):
                assert time.monotonic() < deadline
                time.sleep(0.01)

        assert entered.get(timeout=5) == "foreground"
        foreground_release.set()
        assert entered.get(timeout=5) == "background"
        background_release.set()
    finally:
        foreground_release.set()
        background_release.set()
        _stop_process(foreground)
        _stop_process(background)

    assert foreground is not None and foreground.exitcode == 0
    assert background is not None and background.exitcode == 0


@pytest.mark.skipif(admission.fcntl is None, reason="foreground priority requires flock")
def test_foreground_release_reserves_a_bounded_parent_handoff_before_background(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Do not let a queued dispatch worker reacquire between parent Lean stages."""
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_FOREGROUND_GRACE_S", "0.4")
    context = multiprocessing.get_context("spawn")
    attempted = context.Event()
    entered = context.Event()
    release = context.Event()
    background = context.Process(
        target=_event_admission_worker,
        args=(str(tmp_path), attempted, entered, release),
    )
    try:
        with project_lean_heavy_admission(tmp_path):
            background.start()
            assert attempted.wait(timeout=5)
            assert entered.wait(timeout=0.1) is False

        # The foreground operation has released the main Lean slot, but the
        # bounded handoff window must keep an already-queued worker out while
        # the parent records the result and enters its finalization stage.
        assert entered.wait(timeout=0.15) is False
        assert entered.wait(timeout=2)
        release.set()
    finally:
        release.set()
        _stop_process(background)

    assert background.exitcode == 0
    waiter_root = admission._priority_waiter_root(tmp_path.resolve())
    assert not list(waiter_root.glob("waiter-*.lock"))


@pytest.mark.skipif(admission.fcntl is None, reason="foreground priority requires flock")
def test_explicit_foreground_handoff_lease_keeps_background_out_without_holding_main_gate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Advertise a candidate-commit handoff while leaving the main slot unlocked."""
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_FOREGROUND_GRACE_S", "0")
    context = multiprocessing.get_context("spawn")
    attempted = context.Event()
    entered = context.Event()
    release = context.Event()
    background = context.Process(
        target=_event_admission_worker,
        args=(str(tmp_path), attempted, entered, release),
    )
    try:
        with project_lean_heavy_admission(tmp_path) as foreground:
            foreground.reserve_foreground_handoff(0.4, reason="exact candidate ready")
            background.start()
            assert attempted.wait(timeout=5)
            assert entered.wait(timeout=0.1) is False

        # The candidate lease is only an unlocked priority advertisement:
        # foreground verification can acquire the free main gate immediately,
        # while the already-waiting dispatch worker must remain outside.
        started = time.monotonic()
        with admission.project_lean_verification_transaction(tmp_path):
            assert time.monotonic() - started < 0.2
            assert entered.is_set() is False

        assert entered.wait(timeout=0.1) is False
        assert entered.wait(timeout=2)
        release.set()
    finally:
        release.set()
        _stop_process(background)

    assert background.exitcode == 0
    waiter_root = admission._priority_waiter_root(tmp_path.resolve())
    assert not list(waiter_root.glob("waiter-*.lock"))


def test_explicit_foreground_handoff_lease_is_finite_and_hard_capped() -> None:
    """Reject non-finite requests and cap overlarge requests in the core authority."""
    malformed = ProjectLeanAdmission(
        project_root="/tmp/demo",
        lock_path="/tmp/demo.lock",
        waited_s=0.0,
        contended=False,
        nested=False,
        enforced=True,
    )
    assert malformed.reserve_foreground_handoff(float("nan"), reason="candidate") == 0.0

    admitted = ProjectLeanAdmission(
        project_root="/tmp/demo",
        lock_path="/tmp/demo.lock",
        waited_s=0.0,
        contended=False,
        nested=False,
        enforced=True,
    )

    reserved = admitted.reserve_foreground_handoff(10_000, reason="candidate")

    assert reserved == MAX_FOREGROUND_HANDOFF_LEASE_S
    assert admitted.to_dict()["foreground_handoff_grace_s"] == (MAX_FOREGROUND_HANDOFF_LEASE_S)


@pytest.mark.skipif(admission.fcntl is None, reason="foreground priority requires flock")
def test_pre_admission_lease_blocks_background_until_foreground_consumes_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Protect provider-to-first-tool latency without holding the main Lean slot."""
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_FOREGROUND_GRACE_S", "0")
    lease = reserve_project_foreground_priority_lease(
        tmp_path,
        2.0,
        reason="scope-entry first foreground inspection",
    )
    assert lease is not None
    context = multiprocessing.get_context("spawn")
    attempted = context.Event()
    entered = context.Event()
    release = context.Event()
    background = context.Process(
        target=_event_admission_worker,
        args=(str(tmp_path), attempted, entered, release),
    )
    try:
        background.start()
        assert attempted.wait(timeout=5)
        assert entered.wait(timeout=0.15) is False

        # The upcoming foreground may acquire immediately despite its own
        # unlocked priority marker. Consume the one-shot reservation only
        # after the foreground owns both its waiter and the main slot.
        started = time.monotonic()
        with project_lean_heavy_admission(tmp_path):
            assert time.monotonic() - started < 0.2
            assert lease.release() is True
            assert entered.is_set() is False

        assert entered.wait(timeout=2)
        release.set()
    finally:
        lease.release()
        release.set()
        _stop_process(background)

    assert background.exitcode == 0


@pytest.mark.skipif(admission.fcntl is None, reason="foreground priority requires flock")
def test_dead_foreground_waiter_does_not_strand_background_admission(
    tmp_path: Path,
) -> None:
    """Reclaim only an OS-unlocked waiter marker after its process exits."""
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    foreground_attempted = context.Event()
    foreground_release = context.Event()
    background_attempted = context.Event()
    background_release = context.Event()
    foreground: multiprocessing.Process | None = None
    background: multiprocessing.Process | None = None
    try:
        with project_lean_heavy_admission(tmp_path):
            foreground = context.Process(
                target=_ordered_admission_worker,
                args=(
                    str(tmp_path),
                    "foreground",
                    foreground_attempted,
                    entered,
                    foreground_release,
                ),
            )
            foreground.start()
            assert foreground_attempted.wait(timeout=5)
            deadline = time.monotonic() + 5
            while not admission._foreground_waiter_exists(tmp_path.resolve()):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            foreground.terminate()
            foreground.join(timeout=5)
            assert not foreground.is_alive()

            background = context.Process(
                target=_ordered_admission_worker,
                args=(
                    str(tmp_path),
                    "background",
                    background_attempted,
                    entered,
                    background_release,
                ),
            )
            background.start()
            assert background_attempted.wait(timeout=5)

        assert entered.get(timeout=5) == "background"
        background_release.set()
    finally:
        foreground_release.set()
        background_release.set()
        _stop_process(foreground)
        _stop_process(background)

    assert background is not None and background.exitcode == 0
    waiter_root = admission._priority_waiter_root(tmp_path.resolve())
    assert not list(waiter_root.glob("waiter-*.lock"))


@pytest.mark.skipif(admission.fcntl is None, reason="foreground cleanup requires flock")
def test_process_finalization_reclaims_only_its_unlocked_foreground_markers(
    tmp_path: Path,
) -> None:
    """Remove an exiting runner's live grace marker without touching another PID."""
    root = tmp_path.resolve()
    waiter = admission._register_foreground_waiter(root)
    assert waiter is not None
    assert admission._arm_foreground_handoff(waiter, requested_grace_s=60.0)
    admission._clear_foreground_waiter(root, waiter, preserve_grace=True)
    other = admission._priority_waiter_root(root) / f"waiter-{os.getpid() + 1}-other.lock"
    other.write_text("grace-until=9999999999\n", encoding="utf-8")

    residual = admission.reclaim_process_foreground_waiters(root)

    assert residual == ()
    assert waiter.path.exists() is False
    assert other.is_file()


@pytest.mark.skipif(admission.fcntl is None, reason="foreground cleanup requires flock")
def test_process_finalization_never_unlinks_an_actively_locked_waiter(tmp_path: Path) -> None:
    """Return a residual path while same-process Lean admission still owns it."""
    root = tmp_path.resolve()
    waiter = admission._register_foreground_waiter(root)
    assert waiter is not None
    try:
        residual = admission.reclaim_process_foreground_waiters(root)

        assert residual == (str(waiter.path),)
        assert waiter.path.is_file()
    finally:
        admission._clear_foreground_waiter(root, waiter)


def test_project_gate_is_reentrant_in_one_thread(tmp_path: Path) -> None:
    """Permit a tool-level gate to wrap a backend-level gate without deadlock."""
    with project_lean_heavy_admission(tmp_path) as outer:
        with project_lean_heavy_admission(tmp_path) as inner:
            assert outer.enforced
            assert inner.nested
            assert inner.waited_s == outer.waited_s


def test_admission_observer_reports_only_the_actual_gate_lifecycle(tmp_path: Path) -> None:
    """Correlate one real acquisition without duplicating its nested scope."""
    events: list[tuple[str, dict[str, object]]] = []

    with project_lean_admission_observer(
        lambda phase, details: events.append((phase, dict(details)))
    ):
        with project_lean_heavy_admission(tmp_path):
            with project_lean_heavy_admission(tmp_path) as nested:
                assert nested.nested is True

    assert [phase for phase, _details in events] == ["waiting", "admitted", "released"]
    request_ids = {str(details["admission_request_id"]) for _phase, details in events}
    assert len(request_ids) == 1
    assert all(details["admission_role"] == "foreground" for _phase, details in events)
    assert events[1][1]["nested"] is False
    assert events[2][1]["retained_until_process_exit"] is False


def test_admission_observer_failure_does_not_block_lean_authority(tmp_path: Path) -> None:
    """Keep activity persistence failures outside the admission authority."""

    def fail_observer(phase, details):
        raise RuntimeError(f"observer failed during {phase}: {details}")

    with project_lean_admission_observer(fail_observer):
        with project_lean_heavy_admission(tmp_path) as admitted:
            assert admitted.enforced is True


@pytest.mark.skipif(admission.fcntl is None, reason="transaction exclusion requires flock")
def test_verification_transaction_excludes_background_between_nested_gates(
    tmp_path: Path,
) -> None:
    """Keep priority from exact elaboration through the following axiom gate."""
    context = multiprocessing.get_context("spawn")
    attempted = context.Event()
    entered = context.Event()
    release = context.Event()
    background: multiprocessing.Process | None = None
    try:
        with admission.project_lean_verification_transaction(tmp_path) as transaction:
            with project_lean_heavy_admission(tmp_path) as exact:
                assert exact.nested is True
                assert exact.lock_path == transaction.lock_path

            background = context.Process(
                target=_event_admission_worker,
                args=(str(tmp_path), attempted, entered, release),
            )
            background.start()
            assert attempted.wait(timeout=5)
            assert entered.wait(timeout=0.2) is False

            with project_lean_heavy_admission(tmp_path) as axiom:
                assert axiom.nested is True
                assert axiom.lock_path == transaction.lock_path
            assert entered.is_set() is False

        assert entered.wait(timeout=5)
        release.set()
    finally:
        release.set()
        _stop_process(background)

    assert background is not None and background.exitcode == 0


def test_nested_file_and_project_directory_share_one_canonical_gate(tmp_path: Path) -> None:
    """Canonicalize all file/cwd spellings to the actual Lean project root."""
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")

    with project_lean_heavy_admission(project) as outer:
        with project_lean_heavy_admission(target) as inner:
            assert inner.nested is True
            assert inner.project_root == str(project.resolve())
            assert inner.lock_path == outer.lock_path


def test_retained_gate_refuses_same_process_retry_without_deadlock(tmp_path: Path) -> None:
    """Fail closed instead of waiting forever on this process's sticky gate."""
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    with project_lean_heavy_admission(project) as first:
        first.retain_until_process_exit("probe close failed")

    with pytest.raises(ProjectLeanAdmissionRetained, match="probe close failed"):
        with project_lean_heavy_admission(project):
            pytest.fail("a sticky retained admission was reacquired")


def test_flock_failure_closes_the_partial_descriptor(monkeypatch, tmp_path: Path) -> None:
    """Do not leak a lock-file descriptor when OS admission fails."""
    if admission.fcntl is None:
        pytest.skip("flock is unavailable")

    class _Handle:
        closed = False

        def fileno(self):
            return 123

        def close(self):
            self.closed = True

    handle = _Handle()
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: handle)
    monkeypatch.setattr(admission.fcntl, "flock", lambda *args: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError), project_lean_heavy_admission(tmp_path):
        pass

    assert handle.closed is True
