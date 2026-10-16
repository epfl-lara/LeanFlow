"""Exercise actual default guidance for the prove and formalize workflows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leanflow_cli import workflow
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.runtime.skill_core import load_skill


def test_actual_default_formalization_prompt_contains_document_gates(tmp_path, monkeypatch):
    kind = "formalize"
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "isolated-home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "Main.lean").write_text("import Init\n")
    (project / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda cwd: SimpleNamespace(root=project, label="Example"),
    )
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda requested=None: {"provider": "codex"}
    )
    target = "paper.tex"
    plan = workflow.resolve_workflow_request(f"/{kind} {target}", active_cwd=project, preview=True)
    skill = load_skill(plan.active_skill, cwd=project)
    assert skill is not None and skill["source"] == "builtin"
    snapshot = tmp_path / "snapshot"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    context, prompt = prepare_context(
        root=project, snapshot=snapshot, scratch=scratch, environment=plan.child_env, allowed=()
    )
    assert skill["content"] in prompt
    assert context["workflow"] == kind and context["requested_target"] == target
    assert plan.active_skill == "lean-formalization"
    assert "complete frozen source inventory" in prompt
    assert "checked preparation" in prompt and "representative-slice receipt" in prompt
    assert "independent per-item and whole-document review" in prompt
    assert "complete assigned region dispositions" in prompt
    assert not (project / ".leanflow/formalizations").exists()


@pytest.mark.parametrize("alias", ["prove", "autoprove"])
@pytest.mark.parametrize("parallelism", [1, 3])
def test_prover_aliases_select_current_session_without_old_worker_toolset(
    tmp_path, monkeypatch, alias, parallelism
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda cwd: SimpleNamespace(root=tmp_path, label="Example"),
    )
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda requested=None: {"provider": "codex"}
    )
    suffix = f" --agents {parallelism}" if parallelism > 1 else ""
    plan = workflow.resolve_workflow_request(
        f"/{alias} Main.lean{suffix}", active_cwd=tmp_path, preview=True
    )
    assert plan.workflow.workflow_kind == "prove"
    assert plan.toolset_name == "leanflow-prover-session"
    assert "leanflow_cli.workflows.prover.runtime" in plan.argv
    assert plan.active_skill == "lean-bounded-prover"


@pytest.mark.parametrize("label", ["Display A", "Different Display B"])
def test_formalization_layout_uses_project_configuration_without_label_keyword(
    tmp_path, monkeypatch, label
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda cwd: SimpleNamespace(root=tmp_path, label=label),
    )
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda requested=None: {"provider": "codex"}
    )
    monkeypatch.setattr(
        "leanflow_cli.formalization.preparation_launch.prepare_formalization_launch",
        lambda *args, **kwargs: {
            "root": tmp_path,
            "budget_path": tmp_path / "budget.json",
            "run_id": "fixture-run",
        },
    )

    def intake(*, project_root, cwd, workflow_args, require_preparation):
        assert project_root == cwd == tmp_path and workflow_args == "paper.tex"
        assert require_preparation
        return SimpleNamespace(
            source_relative="paper.tex",
            target_lean_relative="src/Actual/Paper.lean",
            blueprint_skill_path=tmp_path / "guidance.md",
            to_env=lambda: {},
        )

    monkeypatch.setattr(workflow, "prepare_formalization_document_context", intake)
    plan = workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=tmp_path)
    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == "src/Actual/Paper.lean"
