"""Preserve unrelated warm projects while invalidating changed dependency state."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.project_resource_admission import ProjectLeanAdmission
from leanflow_cli.lean import lean_incremental, lean_session_lifecycle


def test_scoped_close_retains_other_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[Path] = []

    class ScopedProbe:
        def close_project(self, root: Path) -> None:
            closed.append(root)

        def close(self) -> None:
            pytest.fail("Unrelated projects must remain warm")

    probe = ScopedProbe()
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    assert lean_incremental.incremental_sessions_active()
    assert lean_incremental.close_incremental_project(tmp_path)
    assert closed == [tmp_path.resolve()]
    assert lean_incremental._PROBE is probe


def test_unsupported_backend_keeps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class LegacyProbe:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(lean_incremental, "_PROBE", LegacyProbe())
    assert not lean_incremental.close_incremental_project(tmp_path)
    assert closed == []
    assert lean_incremental.incremental_sessions_active()


def test_failed_scoped_cleanup_retains_owned_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedProbe:
        def close_project(self, root: Path) -> None:
            raise RuntimeError("Process cleanup failed")

    probe = FailedProbe()
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    assert not lean_incremental.close_incremental_project(tmp_path)
    assert lean_incremental._PROBE is probe


def test_no_backend_requires_no_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lean_incremental, "_PROBE", None)
    assert not lean_incremental.incremental_sessions_active()
    assert lean_incremental.close_incremental_project(tmp_path)


def test_scoped_cleanup_error_payload_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RejectedProbe:
        def close_project(self, root: Path) -> dict[str, object]:
            return {"success": False, "error": "Unknown project"}

    probe = RejectedProbe()
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    assert not lean_incremental.close_incremental_project(tmp_path)
    assert lean_incremental._PROBE is probe


@pytest.mark.parametrize("pool_name", ["_sessions", "_scratch_sessions", "_code_sessions"])
def test_quarantined_global_cleanup_retains_probe_and_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_name: str
) -> None:
    """Do not release ownership when close returns with an unverified worker."""

    class QuarantinedProbe:
        _sessions = {}
        _scratch_sessions = {}
        _code_sessions = {}

        def close(self) -> None:
            return

    probe = QuarantinedProbe()
    setattr(probe, pool_name, {"retained": SimpleNamespace(retirement_error="still alive")})
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "reclaim")
    monkeypatch.setitem(
        lean_session_lifecycle._PROJECT_GENERATIONS, tmp_path.resolve(), (probe, "original")
    )
    admission = ProjectLeanAdmission(str(tmp_path), "", 0.0, False, False, True)

    preparation = lean_session_lifecycle.prepare_incremental_sessions_for_cold_check(
        admission, close_failure_reason="resident close was not verified"
    )

    assert not preparation.ready
    assert not preparation.reclaimed
    assert admission.to_dict()["retained_until_process_exit"] is True
    assert lean_incremental._PROBE is probe
    assert lean_session_lifecycle._PROJECT_GENERATIONS[tmp_path.resolve()] == (probe, "original")
