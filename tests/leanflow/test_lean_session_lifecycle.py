"""Cover conservative warm-session admission before independent Lean checks."""

from __future__ import annotations

import pytest

from core.project_resource_admission import ProjectLeanAdmission, ProjectLeanAdmissionRetained
from leanflow_cli.lean import lean_incremental, lean_session_lifecycle


@pytest.mark.parametrize(
    ("setting", "low_memory", "dispatch", "required"),
    [
        (None, False, False, False),
        ("auto", False, False, False),
        ("retain", False, False, False),
        ("reclaim", False, False, True),
        ("invalid", False, False, True),
        ("retain", True, False, True),
        ("retain", False, True, True),
        ("auto", True, True, True),
    ],
)
def test_policy_respects_resource_modes(monkeypatch, setting, low_memory, dispatch, required):
    """Never allow an override to disable low-memory or worker reclamation."""
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "1" if low_memory else "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1" if dispatch else "0")
    if setting is None:
        monkeypatch.delenv("LEANFLOW_LEAN_WARM_RETENTION", raising=False)
    else:
        monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", setting)

    assert lean_session_lifecycle.incremental_session_reclaim_required() is required


def _admission() -> ProjectLeanAdmission:
    """Return admission metadata without acquiring a real project lock."""
    return ProjectLeanAdmission("/project", "/project/lean.lock", 0.0, False, False, True)


def test_foreground_retention_does_not_attempt_close(monkeypatch):
    """Preserve a useful prefix even when the independent checker is admitted."""
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "0")
    monkeypatch.delenv("LEANFLOW_LEAN_WARM_RETENTION", raising=False)
    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: pytest.fail("normal foreground check discarded warm sessions"),
    )
    admission = _admission()

    preparation = lean_session_lifecycle.prepare_incremental_sessions_for_cold_check(
        admission, close_failure_reason="close failed"
    )

    assert preparation.ready is True
    assert preparation.reclaimed is False
    assert admission.to_dict()["retained_until_process_exit"] is False


@pytest.mark.parametrize("close_ok", [True, False])
def test_required_reclaim_retains_admission_after_close_failure(monkeypatch, close_ok):
    """Refuse another Lean process when the mandatory memory release fails."""
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "reclaim")
    calls = []
    monkeypatch.setattr(
        lean_incremental, "close_incremental_sessions", lambda: calls.append("close") or close_ok
    )
    admission = _admission()

    preparation = lean_session_lifecycle.prepare_incremental_sessions_for_cold_check(
        admission, close_failure_reason="resident checker did not close"
    )

    assert calls == ["close"]
    assert preparation.ready is close_ok
    assert preparation.reclaimed is close_ok
    assert admission.to_dict()["retained_until_process_exit"] is not close_ok
    assert admission.to_dict()["retention_reason"] == (
        "" if close_ok else "resident checker did not close"
    )


@pytest.mark.parametrize("scanner_fails_before", [True, False])
@pytest.mark.parametrize("close_ok", [True, False])
def test_build_generation_failure_invalidates_conservatively(
    monkeypatch, tmp_path, scanner_fails_before, close_ok
):
    """An unreadable dependency tree cannot justify keeping resident build state."""
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "auto")
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "0")
    monkeypatch.setattr(lean_incremental, "incremental_sessions_active", lambda root=None: True)
    monkeypatch.setitem(
        lean_session_lifecycle._PROJECT_GENERATIONS,
        tmp_path.resolve(),
        (lean_incremental._PROBE, "before"),
    )
    calls = []
    scan_count = 0

    def generation(root):
        nonlocal scan_count
        scan_count += 1
        if scanner_fails_before or scan_count == 2:
            raise OSError("dependency unavailable")
        return "before"

    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation", generation
    )
    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_project",
        lambda root: calls.append(("close-project", root)) or close_ok,
    )
    admission = _admission()

    if close_ok:
        with lean_session_lifecycle.retained_incremental_build_guard(admission, tmp_path):
            calls.append(("build", tmp_path))
    else:
        with pytest.raises(ProjectLeanAdmissionRetained):
            with lean_session_lifecycle.retained_incremental_build_guard(admission, tmp_path):
                calls.append(("build", tmp_path))

    assert ("close-project", tmp_path) in calls
    assert (("build", tmp_path) in calls) is (close_ok or not scanner_fails_before)
    assert scan_count == (1 if scanner_fails_before else 2)
    assert admission.to_dict()["retained_until_process_exit"] is not close_ok


def test_no_resident_session_skips_build_generation_scan(monkeypatch, tmp_path):
    """Avoid scanning compiled libraries when no warm state can become stale."""
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "auto")
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "0")
    monkeypatch.setattr(lean_incremental, "incremental_sessions_active", lambda root=None: False)
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation",
        lambda root: pytest.fail("no resident session needs build-generation scans"),
    )

    with lean_session_lifecycle.retained_incremental_build_guard(_admission(), tmp_path):
        pass
