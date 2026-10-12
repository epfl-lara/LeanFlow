"""Build only filtered local prerequisites within private checker write capabilities."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.session_build import build_session_imports


def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    workspace = project / ".leanflow-job"
    workspace.mkdir(parents=True)
    (project / "Local").mkdir()
    (project / "Local/Prerequisite.lean").write_text("import Std\ndef value : Nat := 7\n")
    source = workspace / "Scratch.lean"
    source.write_text("import Local.Prerequisite\ntheorem target : True := by sorry\n")
    return project, workspace, source


def test_build_targets_only_scratch_local_imports_with_private_output_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, workspace, source = fixture(tmp_path)
    (project / "Unrelated.lean").write_text("invalid Lean unrelated to the scratch imports")
    (project / "src/Custom").mkdir(parents=True)
    (project / "src/Custom/Support.lean").write_text("import Std\ndef custom : Nat := 0\n")
    (project / ".lake/packages/Mathlib").mkdir(parents=True)
    (project / ".lake/packages/Mathlib/Basic.lean").write_text("import Std\n")
    source.write_text(
        "-- import Unrelated\n"
        "import Local.Prerequisite Mathlib.Basic\n"
        "public import Custom.Support\n"
        "import Local.Prerequisite\n"
        "theorem target : True := by sorry\n"
    )
    calls = []
    stop = lambda: False
    deadline = time.monotonic() + 45

    def build(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "returncode": 0, "stdout": "built", "stderr": ""}

    monkeypatch.setattr(check_process, "isolated_command", build)
    result = build_session_imports(
        project_root=project,
        workspace=workspace,
        source_file=source,
        deadline=deadline,
        cancelled=stop,
    )
    assert result["success"] is True
    assert result["modules"] == ["Local.Prerequisite", "Custom.Support"]
    assert len(calls) == 1
    assert calls[0]["argv"] == [
        "lake",
        "build",
        "+Local.Prerequisite:olean",
        "+Custom.Support:olean",
    ]
    assert calls[0]["extra_writable_roots"] == (project / ".lake/build",)
    assert calls[0]["workspace"] == workspace
    assert calls[0]["project_root"] == project
    assert calls[0]["cancelled"] is stop
    assert 0 < calls[0]["timeout_s"] <= 45
    assert "network_allowed" not in calls[0]


def test_dependency_only_imports_need_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, workspace, source = fixture(tmp_path)
    source.write_text("import Mathlib.Data.List.Basic\n")
    monkeypatch.setattr(check_process, "isolated_command", lambda **_: pytest.fail("build launch"))
    assert build_session_imports(
        project_root=project, workspace=workspace, source_file=source, deadline=None, cancelled=None
    ) == {"success": True, "modules": [], "skipped": True}


def test_cancellation_stops_before_prerequisite_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, workspace, source = fixture(tmp_path)
    monkeypatch.setattr(check_process, "isolated_command", lambda **_: pytest.fail("build launch"))
    result = build_session_imports(
        project_root=project,
        workspace=workspace,
        source_file=source,
        deadline=None,
        cancelled=lambda: True,
    )
    assert result["error_code"] == "check_cancelled"


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_reject_linked_build_outputs_before_write_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    project, workspace, source = fixture(tmp_path)
    protected = tmp_path / "protected"
    protected.mkdir()
    build = project / ".lake/build"
    build.parent.mkdir()
    if link == "symlink":
        build.symlink_to(protected, target_is_directory=True)
    else:
        build.mkdir()
        (protected / "artifact.olean").write_text("canonical bytes")
        os.link(protected / "artifact.olean", build / "artifact.olean")
    monkeypatch.setattr(check_process, "isolated_command", lambda **_: pytest.fail("build launch"))
    result = build_session_imports(
        project_root=project, workspace=workspace, source_file=source, deadline=None, cancelled=None
    )
    assert result["success"] is False
    assert result["error_code"] == "check_setup_failed"


def test_private_build_uses_filtered_source_and_omits_canonical_olean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, _, _ = fixture(tmp_path)
    canonical_build = canonical / ".lake/build/lib/lean/Local"
    canonical_build.mkdir(parents=True)
    (canonical_build / "Prerequisite.olean").write_text("unfiltered old proof bytes")
    snapshot = create_snapshot(canonical, tmp_path / "snapshot")
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(exist_ok=True)
    source = scratch / "Scratch.lean"
    source.write_text("import Local.Prerequisite\n")

    def build(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["project_root"] == snapshot
        assert not (snapshot / ".lake/build/lib/lean/Local/Prerequisite.olean").exists()
        assert (
            snapshot / "Local/Prerequisite.lean"
        ).read_text() == "import Std\ndef value : Nat := 7\n"
        return {"success": True, "returncode": 0}

    monkeypatch.setattr(check_process, "isolated_command", build)
    result = build_session_imports(
        project_root=snapshot,
        workspace=scratch,
        source_file=source,
        deadline=None,
        cancelled=None,
    )
    assert result["success"] is True
    assert (canonical_build / "Prerequisite.olean").read_text() == "unfiltered old proof bytes"
