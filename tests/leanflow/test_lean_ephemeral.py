"""Exact-project ephemeral Lean source-check tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from leanflow_cli.lean import lean_ephemeral, lean_incremental


def test_exact_project_check_uses_out_of_tree_copy_and_removes_it(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    authoritative = project / "Main.lean"
    authoritative.write_text("theorem stable : True := by trivial\n", encoding="utf-8")
    before = authoritative.read_bytes()
    seen: dict[str, object] = {}

    class Process:
        returncode = 0
        pid = 1234

        def __init__(self, command, **kwargs):
            seen["command"] = command
            seen["cwd"] = kwargs["cwd"]
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            seen["timeout"] = timeout
            harness = Path(seen["command"][-1])
            assert harness.is_file()
            assert not harness.resolve().is_relative_to(project.resolve())
            assert harness.read_text(encoding="utf-8") == "theorem probe : True := by trivial\n"
            self.output.write(b"'probe' does not depend on any axioms\n")
            self.output.flush()
            return 0

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem probe : True := by trivial\n",
        cwd=project,
        timeout_s=37,
    )

    harness = Path(seen["command"][-1])
    assert seen["command"][:3] == ["lake", "env", "lean"]
    assert seen["cwd"] == str(project.resolve())
    assert seen["timeout"] == 37
    assert result["success"] is True
    assert result["retryable"] is False
    assert "does not depend on any axioms" in result["output"]
    assert not harness.exists()
    assert authoritative.read_bytes() == before


def test_missing_project_import_environment_is_retryable(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    class Process:
        returncode = 1
        pid = 1234

        def __init__(self, _command, **kwargs):
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            assert timeout == 120
            self.output.write(b"error: unknown module prefix 'FormalConjectures'\n")
            self.output.flush()
            return 1

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)

    result = lean_ephemeral.lean_ephemeral_source_check("import FormalConjectures\n", cwd=project)

    assert result["success"] is False
    assert result["retryable"] is True
    assert result["failure_kind"] == "project_environment_unavailable"


def test_actual_lean_elaboration_error_is_not_retryable(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    class Process:
        returncode = 1
        pid = 1234

        def __init__(self, _command, **kwargs):
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            assert timeout == 120
            self.output.write(b"Probe.lean:1:25: error: type mismatch\n")
            self.output.flush()
            return 1

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem broken : False := by trivial\n", cwd=project
    )

    assert result["success"] is False
    assert result["retryable"] is False
    assert result["failure_kind"] == "lean_elaboration"


def test_exact_check_surfaces_error_after_earlier_warnings(monkeypatch, tmp_path):
    """Put the first Lean error in the concise field even after noisy warnings."""
    project = tmp_path / "project"
    project.mkdir()

    class Process:
        returncode = 1
        pid = 1234

        def __init__(self, _command, **kwargs):
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            assert timeout == 120
            self.output.write(
                b"Probe.lean:1:1: warning: unused variable\n"
                b"The binding can be removed.\n"
                b"Probe.lean:20:7: error: application type mismatch\n"
            )
            self.output.flush()
            return 1

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem broken : False := by trivial\n", cwd=project
    )

    assert result["error"] == "Lean error at line 20: application type mismatch"
    assert "warning" in result["output"]


def test_timeout_is_retryable_and_reaps_process_group(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    signals: list[int] = []

    class Process:
        returncode = None
        pid = 4321

        def __init__(self, _command, **kwargs):
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            if timeout == 2:
                self.returncode = -15
                return -15
            raise subprocess.TimeoutExpired("lake", timeout)

        def poll(self):
            return self.returncode

        def communicate(self, timeout):
            assert timeout == 1
            return b"", None

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)
    monkeypatch.setattr(
        lean_ephemeral.os,
        "killpg",
        lambda pid, requested_signal: signals.append(requested_signal),
    )

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem slow : True := by trivial\n", cwd=project, timeout_s=10
    )

    assert result["timed_out"] is True
    assert result["retryable"] is True
    assert signals == [lean_ephemeral.signal.SIGTERM]


def test_parent_interrupt_reaps_process_group_and_source_harness(monkeypatch, tmp_path):
    """Runner interruption must not orphan an in-progress exact Lean check."""
    project = tmp_path / "project"
    project.mkdir()
    signals: list[int] = []
    harnesses: list[Path] = []

    class Process:
        returncode = None
        pid = 4321

        def __init__(self, command, **_kwargs):
            harnesses.append(Path(command[-1]))

        def wait(self, timeout):
            if timeout == 2:
                self.returncode = -15
                return -15
            raise KeyboardInterrupt()

        def poll(self):
            return self.returncode

        def communicate(self, timeout):
            assert timeout == 1
            return b"", None

    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)
    monkeypatch.setattr(
        lean_ephemeral.os,
        "killpg",
        lambda pid, requested_signal: signals.append(requested_signal),
    )

    with pytest.raises(KeyboardInterrupt):
        lean_ephemeral.lean_ephemeral_source_check(
            "theorem interrupted : True := by trivial\n",
            cwd=project,
            timeout_s=10,
        )

    assert signals == [lean_ephemeral.signal.SIGTERM]
    assert len(harnesses) == 1
    assert not harnesses[0].exists()


def test_exact_check_reclaims_owned_probe_under_project_admission(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    class Probe:
        closed = False

        def close(self):
            self.closed = True

    probe = Probe()

    class Process:
        returncode = 0
        pid = 1234

        def __init__(self, _command, **kwargs):
            self.output = kwargs["stdout"]

        def wait(self, timeout):
            self.output.write(b"ok\n")
            self.output.flush()
            return 0

    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(lean_ephemeral.subprocess, "Popen", Process)

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem probe : True := by trivial\n",
        cwd=project,
    )

    assert result["ok"] is True
    assert probe.closed is True
    assert result["resource_admission"]["incremental_session_reclaimed"] is True
    assert result["resource_admission"]["enforced"] is True


def test_exact_check_does_not_spawn_after_probe_reclaim_failure(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    class Probe:
        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", Probe())
    monkeypatch.setattr(
        lean_ephemeral.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Lake spawned after failed probe reclaim")
        ),
    )

    result = lean_ephemeral.lean_ephemeral_source_check(
        "theorem probe : True := by trivial\n",
        cwd=project,
    )

    assert result["ok"] is False
    assert result["retryable"] is True
    assert result["failure_kind"] == "resource_admission_retained"
    assert "close failed" in result["error"]
    assert result["resource_admission"]["incremental_session_reclaimed"] is False
    assert result["resource_admission"]["retained_until_process_exit"] is True
