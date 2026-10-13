"""Retire stale warm imports at independent check boundaries without unrelated scans."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.lean import (
    lean_ephemeral,
    lean_incremental,
    lean_services,
    lean_session_lifecycle,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a small isolated dependency tree with no installed-library scans."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "Test"\n')
    (root / "Main.lean").write_text("theorem demo : True := by trivial\n")
    (root / "Library.lean").write_text("theorem imported : True := by trivial\n")
    (root / "Library.olean").write_bytes(b"first")
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "auto")
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "0")
    lean_session_lifecycle.forget_incremental_project_generations()
    yield root
    lean_session_lifecycle.forget_incremental_project_generations()


class _ProjectProbe:
    """Model actual file, scratch and proof-state pool ownership."""

    def __init__(self, root: Path, events: list[str]) -> None:
        self._sessions = {(str(root), str(root / "Main.lean")): SimpleNamespace(project_root=root)}
        self._scratch_sessions = {}
        self._code_sessions = {}
        self.events = events

    def close_project(self, root: Path) -> dict[str, bool]:
        self.events.append("close-project")
        self._sessions = {
            key: session for key, session in self._sessions.items() if session.project_root != root
        }
        return {"success": True}

    def close(self) -> None:
        self.events.append("close-all")


@pytest.mark.parametrize("mode", ["project", "file_exact"])
@pytest.mark.parametrize("changed", ["Library.olean", "Library.ir"])
def test_external_dependency_change_before_noop_check_closes_stale_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, mode: str, changed: str
) -> None:
    """Compare current imports to warm provenance rather than only before/after a no-op."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    (project / changed).write_bytes(b"changed import")
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services, "_run_command", lambda *args, **kwargs: (events.append("cold") or 0, "ok")
    )

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode=mode)

    assert result.ok
    assert events == ["close-project", "cold"]
    assert not lean_incremental.incremental_sessions_active(project)


def test_unchanged_exact_check_preserves_confirmed_project(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a warm prefix when both its recorded and current dependencies match."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(lean_services, "_run_command", lambda *a, **kw: (0, "ok"))

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode="file_exact")

    assert result.ok
    assert events == []
    assert lean_incremental.incremental_sessions_active(project)


def test_target_source_edit_retains_compiled_imports_across_exact_check(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal edits on disk reuse imported environments while the caller checks fresh source."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    target = project / "Main.lean"
    target.write_text("theorem demo : True := by exact True.intro\n")
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(lean_services, "_run_command", lambda *a, **kw: (0, "fresh source checked"))

    result = lean_services.lean_verify(str(target), cwd=project, mode="file_exact")

    assert result.ok
    assert events == []
    assert lean_incremental.incremental_sessions_active(project)


def test_unrelated_source_edit_during_exact_check_does_not_reject_result(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source-only edit does not mutate the compiled imports of an independent checker."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))

    def check(*args, **kwargs):
        (project / "Library.lean").write_text("theorem imported : True := by exact True.intro\n")
        return 0, "fresh source checked"

    monkeypatch.setattr(lean_services, "_run_command", check)

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode="file_exact")

    assert result.ok
    assert events == []


def test_other_project_sessions_do_not_trigger_dependency_scan(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live probe with only unrelated workers is not resident state for this check."""
    events: list[str] = []
    probe = _ProjectProbe(tmp_path / "other", events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(lean_services, "_run_command", lambda *a, **kw: (0, "ok"))
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation",
        lambda root: pytest.fail("unrelated project residency triggered a dependency scan"),
    )

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode="file_exact")

    assert result.ok
    assert events == []


@pytest.mark.parametrize("surface", ["verify", "diagnostics", "axioms", "ephemeral"])
def test_mutation_during_cold_check_rejects_result_and_retires_sessions(
    project: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    """A read-only independent checker cannot confirm a moving dependency environment."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    target = project / "Main.lean"
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))

    def check(*args, **kwargs):
        (project / "Library.olean").write_bytes(b"changed during cold check")
        return 0, "ok"

    monkeypatch.setattr(lean_services, "_run_command", check)
    if surface == "verify":
        result = lean_services.lean_verify(str(target), cwd=project, mode="file_exact")
        assert not result.ok
        assert "dependencies changed" in result.output
    elif surface == "diagnostics":
        assert "dependencies changed" in lean_services._diagnostics_text(target, project, {})
    elif surface == "axioms":
        code, output = lean_services._run_axiom_harness(project, "#check True\n")
        assert code == 1
        assert "dependencies changed" in output
    else:

        class Process:
            returncode = 0
            pid = 1234

            def __init__(self, command, **kwargs):
                self.output = kwargs["stdout"]

            def wait(self, timeout):
                check()
                self.output.write(b"ok\n")

        monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)
        result = lean_ephemeral.lean_ephemeral_source_check("#check True\n", cwd=project)
        assert not result["ok"]
        assert result["failure_kind"] == "project_environment_changed"
        assert result["retryable"]
    assert events == ["close-project"]


def test_repeated_warm_checks_record_generation_only_once(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Avoid rescanning large libraries on every proof-loop attempt."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    probe.check_code = lambda *args, **kwargs: {"success": True, "ok": True}
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(lean_incremental, "_probe", lambda: probe)
    scans: list[Path] = []
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation",
        lambda root: scans.append(root) or "confirmed",
    )

    assert lean_incremental.lean_scratch_check("#check True", cwd=str(project))["ok"]
    assert lean_incremental.lean_scratch_check("#check False", cwd=str(project))["ok"]
    assert scans == [project]


@pytest.mark.parametrize("mode", ["reclaim", "low-memory", "dispatch"])
def test_reclaim_modes_skip_initial_generation_scan(
    project: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Do not scan provenance for sessions that the next cold boundary must retire."""
    probe = _ProjectProbe(project, [])
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    if mode == "reclaim":
        monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "reclaim")
    elif mode == "low-memory":
        monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "1")
    else:
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation",
        lambda root: pytest.fail("reclaim-only mode performed an unused generation scan"),
    )

    lean_session_lifecycle.observe_incremental_project(project, probe)

    assert project not in lean_session_lifecycle._PROJECT_GENERATIONS


def test_switch_from_reclaim_to_auto_retires_unrecorded_resident_state(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing policy cannot imply provenance for a worker created without a stamp."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "reclaim")
    lean_session_lifecycle.observe_incremental_project(project, probe)
    assert project not in lean_session_lifecycle._PROJECT_GENERATIONS
    monkeypatch.setenv("LEANFLOW_LEAN_WARM_RETENTION", "auto")
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services, "_run_command", lambda *args, **kwargs: (events.append("cold") or 0, "ok")
    )

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode="file_exact")

    assert result.ok
    assert events == ["close-project", "cold"]


def test_evicted_project_refreshes_generation_before_new_warm_serve(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not associate freshly loaded imports with an evicted worker's old provenance."""
    events: list[str] = []
    probe = _ProjectProbe(project, events)
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    generation = "original"
    scans: list[Path] = []

    def current(root):
        scans.append(root)
        return generation

    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment.lean_loader_generation", current
    )
    lean_session_lifecycle.observe_incremental_project(project, probe)
    lean_session_lifecycle.observe_incremental_project(project, probe)
    assert scans == [project]
    probe._sessions.clear()
    generation = "new imports"
    lean_session_lifecycle.observe_incremental_project(project, probe)
    assert scans == [project, project]
    assert lean_session_lifecycle._PROJECT_GENERATIONS[project] == (probe, "new imports")
    probe._sessions[(str(project), str(project / "Main.lean"))] = SimpleNamespace(
        project_root=project
    )
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(lean_services, "_run_command", lambda *args, **kwargs: (0, "ok"))

    result = lean_services.lean_verify(str(project / "Main.lean"), cwd=project, mode="file_exact")

    assert result.ok
    assert events == []


@pytest.mark.parametrize("pool", ["_sessions", "_scratch_sessions", "_code_sessions"])
def test_project_residency_recognizes_legacy_and_current_pools(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    """Match all supported session families while ignoring unrelated projects."""
    other = tmp_path / "other"
    probe = SimpleNamespace(_sessions={}, _scratch_sessions={}, _code_sessions={})
    session = (
        SimpleNamespace(project_root=project)
        if pool == "_sessions"
        else SimpleNamespace(cwd=project)
    )
    setattr(probe, pool, {"legacy-key": session})
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)

    assert lean_incremental.incremental_sessions_active(project)
    assert not lean_incremental.incremental_sessions_active(other)


def test_standalone_proof_state_worker_does_not_mark_a_project_resident(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project-free code sessions cannot hold imports from the checked project."""
    probe = SimpleNamespace(
        _sessions={}, _scratch_sessions={}, _code_sessions={"standalone": SimpleNamespace(cwd=None)}
    )
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)

    assert lean_incremental.incremental_sessions_active()
    assert not lean_incremental.incremental_sessions_active(project)


@pytest.mark.parametrize("scan_s", [2.0, 11.0])
def test_initial_generation_scan_consumes_proof_execution_deadline(
    project: Path, monkeypatch: pytest.MonkeyPatch, scan_s: float
) -> None:
    """Charge first-serve provenance work before giving the backend its remaining time."""
    probe = _ProjectProbe(project, [])
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(lean_incremental, "_probe", lambda: probe)
    monkeypatch.setattr(lean_incremental, "_local_repl_dir", lambda root: root)
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "0")
    elapsed = [0.0]
    monkeypatch.setattr(lean_incremental.time, "monotonic", lambda: elapsed[0])

    def observe(*args):
        elapsed[0] += scan_s

    monkeypatch.setattr(lean_session_lifecycle, "observe_incremental_project", observe)
    calls: list[dict] = []

    def check(*args, **kwargs):
        calls.append(kwargs)
        return {"success": True, "ok": True, "messages": []}

    monkeypatch.setattr(lean_incremental, "call_lean_probe_with_deadline", check)

    result = lean_incremental.lean_incremental_check(
        action="check_target",
        file_path=str(project / "Main.lean"),
        theorem_id="demo",
        cwd=project,
        timeout_s=10,
    )

    if scan_s < 10:
        assert result["ok"]
        assert calls[0]["deadline_s"] == 10 - scan_s
        assert calls[0]["timeout_s"] == 10 - scan_s
        assert result["leanflow_timing"]["generation_s"] == scan_s
    else:
        assert not result["ok"]
        assert result["timed_out"]
        assert result["error_code"] == "lean_environment_generation_timeout"
        assert calls == []
