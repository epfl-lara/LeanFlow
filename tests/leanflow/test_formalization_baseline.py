"""Require reusable project tools before source intake or paid formalization planning."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import preparation
from leanflow_cli.formalization import preparation_launch as launch
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_campaign import (
    campaign_budget,
    campaign_lock,
    select_campaign,
)
from leanflow_cli.formalization.preparation_transaction import (
    commit_preparation,
    protected_inputs,
)
from leanflow_cli.formalization.records import create_document, current_selection_token
from leanflow_cli.workflows.project_setup import inspect_project, prepare_project, service
from tests.leanflow.preparation_helpers import inspector_readiness_record


def _forbid_new_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject any source selection, configuration, setup or native admission before the checkpoint."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("An unchecked baseline reached source mutation or campaign admission")

    for name in (
        "_select_formalization_document",
        "create_document",
        "load_config",
        "discover_preparation_plan",
        "prepare_document_project",
    ):
        monkeypatch.setattr(launch, name, forbidden)


def _files(root: Path) -> dict[str, bytes]:
    """Snapshot exact project bytes without following directory aliases."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("draft", [False, True])
def test_missing_baseline_stops_before_source_mutation_or_paid_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: bool, draft: bool
) -> None:
    root = tmp_path / "Paper folder"
    root.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    if source:
        (root / "paper.tex").write_text(r"\ifdim 1pt<2pt Unresolved source.\fi")
    before = _files(root)
    _forbid_new_campaign(monkeypatch)
    with pytest.raises(
        ValueError, match="Set up project tools before formalization planning"
    ) as error:
        launch.prepare_formalization_launch(
            root, "paper.tex", {"provider": "codex"}, source_draft_only=draft
        )
    assert shlex.join(["leanflow", "project", "setup", str(root), "--preset", "mathlib"]) in str(
        error.value
    )
    assert "VS Code Formalization tab" in str(error.value)
    assert _files(root) == before
    assert not (root / ".leanflow").exists()


@pytest.fixture
def checked_lean_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Exercise the shared setup pipeline with deterministic external tool responses."""
    root = tmp_path / "Existing Lean"
    root.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    (root / "lakefile.toml").write_text('name = "existing"\n')
    (root / "Main.lean").write_text("def existingValue : Nat := 3\n")
    (root / "paper.tex").write_text(r"\begin{definition}Triple naturals.\end{definition}")
    monkeypatch.setattr(service, "find_executable", lambda name: "/tools/" + name)

    def run(command: list[str], *, cwd: Path, **kwargs: Any) -> tuple[int, str]:
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return 0, "123\trefs/tags/v4.20.0\n"
        if command[:2] == ["lake", "update"]:
            (cwd / "lake-manifest.json").write_text(
                json.dumps({"packages": [{"name": "repl", "rev": "a" * 40}]})
            )
        if command == ["lake", "build", "repl"]:
            executable = cwd / ".lake/packages/repl/.lake/build/bin/repl"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
        if command[:2] == ["lake", "env"]:
            assert "Mathlib" not in str(kwargs["input_text"])
            return 0, '{"env":0,"messages":[],"sorries":[]}\n\n'
        assert command != ["lake", "exe", "cache", "get"]
        return 0, ""

    monkeypatch.setattr(service, "run_command", run)
    assert prepare_project(root, preset="mathlib")["status"] == "ready"
    assert inspect_project(root)["ready"] is True
    return root


def test_shared_setup_checkpoint_preserves_existing_lean_only_project(
    checked_lean_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checked_lean_project
    originals = {
        name: (root / name).read_bytes()
        for name in ("lean-toolchain", "lakefile.toml", "Main.lean", "paper.tex")
    }
    assert "mathlib" not in (root / "lakefile.toml").read_text().lower()
    monkeypatch.setattr(launch, "load_config", lambda: {"limits": {}})

    def prepare(project: Path, record: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert project == root
        assert kwargs["plan"]["preset"] == "lean"
        assert kwargs["plan"]["packages"] == []
        assert not kwargs["plan"]["blockers"]
        return {"accepted": True, "receipt": {"preparation_id": "unit-fixture"}}

    monkeypatch.setattr(launch, "prepare_document_project", prepare)
    monkeypatch.setattr(
        launch,
        "discover_preparation_plan",
        lambda *args, **kwargs: pytest.fail("Explicitly disabled discovery started"),
    )
    result = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        {"provider": "codex"},
        environment={"LEANFLOW_FORMALIZATION_DISCOVERY": "false"},
    )
    assert result["receipt"]["preparation_id"] == "unit-fixture"
    assert {name: (root / name).read_bytes() for name in originals} == originals


@pytest.mark.parametrize("change", ["configuration", "artifact", "registration"])
def test_stale_or_unregistered_baseline_is_not_planning_authority(
    checked_lean_project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = checked_lean_project
    if change == "configuration":
        with (root / "lakefile.toml").open("a") as handle:
            handle.write("\n# editor changed the configuration\n")
    elif change == "artifact":
        (root / ".lake/packages/repl/.lake/build/bin/repl").unlink()
    else:
        (root / ".leanflow/project.yaml").unlink()
    before = _files(root)
    _forbid_new_campaign(monkeypatch)
    with pytest.raises(ValueError, match="Set up project tools"):
        launch.prepare_formalization_launch(root, "paper.tex", {"provider": "codex"})
    assert _files(root) == before


def test_current_transactional_preparation_can_resume_without_generic_setup_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    (root / "paper.tex").write_text(r"\begin{definition}A natural number.\end{definition}")
    record = create_document(root, root / "paper.tex")
    with campaign_lock(root, "existing-run"):
        binding = select_campaign(
            root,
            record,
            run_id="existing-run",
            limits={"agent_calls": 3},
            backend="codex",
            selection_token=current_selection_token(root, record["document_id"]),
            source_draft_only=False,
            effective_draft=False,
            resume=False,
        )
        budget = CampaignLimits(campaign_budget(root, binding), binding["limits"])
    budget.persist()
    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "source_bundle": record["source_bundle"],
        "environment_id": "explicit-unit-fixture-environment",
        "project_sources": {},
        "inspector_readiness": inspector_readiness_record(),
        "budget_path": budget.path.relative_to(root).as_posix(),
        "plan": {"preset": "lean"},
    }
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    commit_preparation(
        root,
        root,
        managed={},
        protected=protected_inputs(root),
        receipt=receipt,
        validate=lambda: None,
        cancelled=lambda: False,
    )
    monkeypatch.setattr(preparation, "environment_identity", lambda root: receipt["environment_id"])
    assert inspect_project(root)["ready"] is False
    monkeypatch.setattr(
        launch,
        "load_config",
        lambda: {"limits": {"agent_calls": 3, "tokens": None, "wall_time_s": None}},
    )
    for name in ("discover_preparation_plan", "prepare_document_project"):
        monkeypatch.setattr(
            launch, name, lambda *args, **kwargs: pytest.fail("Current resume rebuilt preparation")
        )
    result = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        {"provider": "codex"},
        environment={"LEANFLOW_NATIVE_RESUME_RUN_ID": "existing-run"},
    )
    assert result["receipt"] == receipt and result["budget_path"] == budget.path

    (root / "Other.lean").write_text("def concurrentAddition : Nat := 2\n")
    before = _files(root)
    _forbid_new_campaign(monkeypatch)
    with pytest.raises(ValueError, match="Set up project tools"):
        launch.prepare_formalization_launch(root, "paper.tex", {"provider": "codex"})
    assert _files(root) == before


def test_completed_draft_local_history_precedes_baseline_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = {"run_id": "spent", "source_draft_completed": True}
    monkeypatch.setattr(launch, "completed_draft_state", lambda *args, **kwargs: history)
    monkeypatch.setattr(
        launch, "inspect_project", lambda *args: pytest.fail("Local history checked project setup")
    )
    _forbid_new_campaign(monkeypatch)
    assert launch.prepare_formalization_launch(tmp_path, "paper.tex", {"provider": "codex"}) == {
        "root": tmp_path,
        "completed_source_draft": history,
    }


def test_baseline_inspection_process_error_cannot_admit_a_new_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    (tmp_path / "paper.tex").write_text("Original source bytes.\n")
    before = _files(tmp_path)
    monkeypatch.setattr(
        launch,
        "inspect_project",
        lambda root: (_ for _ in ()).throw(subprocess.TimeoutExpired(["lean"], 30)),
    )
    _forbid_new_campaign(monkeypatch)
    with pytest.raises(ValueError, match="Set up project tools.*VS Code Formalization tab"):
        launch.prepare_formalization_launch(tmp_path, "paper.tex", {"provider": "codex"})
    assert _files(tmp_path) == before
