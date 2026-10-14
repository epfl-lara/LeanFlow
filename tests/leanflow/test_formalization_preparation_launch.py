"""Admit source-only formalization through checked preparation while keeping previews read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli import workflow
from leanflow_cli.formalization import preparation_launch as launch
from leanflow_cli.runtime.project_operations import project_setup


@pytest.fixture
def source_folder(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    source = tmp_path / "paper.tex"
    source.write_text(
        r"\begin{definition}Double naturals.\end{definition}"
        "\n"
        r"\begin{theorem}Doubling equals addition.\end{theorem}"
        "\n"
    )
    monkeypatch.setattr(
        launch,
        "load_config",
        lambda: {"limits": {"agent_calls": 3, "tokens": None, "wall_time_s": None}},
    )
    monkeypatch.setattr(
        launch,
        "assert_preparation_current",
        lambda root: (_ for _ in ()).throw(ValueError("Not prepared")),
    )
    return tmp_path


def test_source_only_preview_never_prepares_or_calls_native(source_folder, monkeypatch):
    root = source_folder
    monkeypatch.setattr(
        workflow,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "codex", "model": "model"},
    )
    monkeypatch.setattr(
        launch,
        "prepare_formalization_launch",
        lambda *args, **kwargs: pytest.fail("Preview prepared project"),
    )
    plan = workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root, preview=True)
    assert plan.project.kind == "source_only" and plan.project.root == root
    assert not (root / ".leanflow").exists() and not (root / "lean-toolchain").exists()


def test_preparation_owns_setup_before_activity_and_shares_discovery_budget(
    source_folder, monkeypatch
):
    root = source_folder
    calls = []

    def discover(project_root, record, plan, **kwargs):
        assert project_root == root
        assert kwargs["user_goal"] == "Reuse natural number definitions"
        invocation = kwargs["budget"].reserve()
        kwargs["budget"].started_call(invocation)
        kwargs["budget"].finish(invocation, {"input_tokens": 10, "output_tokens": 5}, started=True)
        calls.append(("discovery", record, kwargs["budget"].path))
        return {"accepted": True, "plan": plan}

    def prepare(project_root, record, **kwargs):
        # Preparation must run before the caller takes normal workflow ownership.
        with project_setup(root):
            pass
        assert kwargs["budget_path"] == calls[0][2]
        assert not (root / "Main.lean").exists()
        calls.append(("preparation", record, kwargs["budget_path"]))
        return {
            "accepted": True,
            "receipt": {"preparation_id": "prepared", "budget_path": str(kwargs["budget_path"])},
        }

    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(launch, "prepare_document_project", prepare)
    result = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        {"provider": "claude", "model": "fixture"},
        environment={"LEANFLOW_FORMALIZATION_PRESET": "lean"},
        user_goal="Reuse natural number definitions",
    )
    assert [row[0] for row in calls] == ["discovery", "preparation"]
    assert result["budget_path"].is_file() and result["receipt"]["preparation_id"] == "prepared"
    assert result["root"] == root and not (root / "lean-toolchain").exists()


def test_unauthorized_discovery_package_cannot_reach_setup(source_folder, monkeypatch):
    root = source_folder
    monkeypatch.setattr(
        launch,
        "discover_preparation_plan",
        lambda *args, **kwargs: {
            "accepted": False,
            "status": "unauthorized_acquisition",
            "unauthorized_packages": [{"name": "unknown"}],
        },
    )
    monkeypatch.setattr(
        launch,
        "prepare_document_project",
        lambda *args, **kwargs: pytest.fail("Unapproved source acquisition"),
    )
    with pytest.raises(ValueError, match="needs correction"):
        launch.prepare_formalization_launch(root, "paper.tex", {"provider": "claude"})
    assert not (root / "lean-toolchain").exists()


@pytest.mark.parametrize(
    ("explicit", "previous", "expected"),
    [(None, None, "lean"), ("mathlib", None, "mathlib"), (None, "mathlib", "mathlib")],
)
def test_native_launch_adds_only_needed_or_previously_selected_mathlib(
    source_folder, monkeypatch, explicit, previous, expected
):
    root = source_folder
    monkeypatch.setattr(
        launch,
        "load_preparation_receipt",
        lambda root: {"plan": {"preset": previous}} if previous else None,
    )
    plans = []

    def discover(project_root, record, plan, **kwargs):
        assert plan["preset"] == expected
        assert {row["name"] for row in plan["packages"]} == (
            {"repl", "mathlib"} if expected == "mathlib" else {"repl"}
        )
        return {"accepted": True, "plan": plan}

    def prepare(project_root, record, **kwargs):
        plans.append(kwargs["plan"])
        return {"accepted": True, "receipt": {"preparation_id": "prepared"}}

    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(launch, "prepare_document_project", prepare)
    launch.prepare_formalization_launch(
        root,
        "paper.tex",
        {"provider": "claude"},
        environment={"LEANFLOW_FORMALIZATION_PRESET": explicit} if explicit else {},
    )
    assert len(plans) == 1 and plans[0]["preset"] == expected


def test_actual_launch_prepares_before_discovery_and_stamps_native_intake(
    source_folder, monkeypatch
):
    root = source_folder
    order = []

    def runtime(**kwargs):
        order.append("runtime")
        return {"provider": "codex", "model": "fixture"}

    def prepare(cwd, args, selected_runtime, *, user_goal):
        order.append("prepare")
        assert selected_runtime["provider"] == "codex"
        assert user_goal == "Use a representative definition and dependent theorem"
        return {
            "root": root,
            "budget_path": root / ".leanflow/formalization-campaigns/fixture/budget.json",
        }

    def project(cwd):
        order.append("project")
        assert order[:2] == ["runtime", "prepare"]
        return SimpleNamespace(root=root, label="Fixture", lean_root=root)

    class Context:
        source_relative = "paper.tex"
        target_lean_relative = "Main/Formalization/Paper.lean"
        blueprint_skill_path = root / "guidance"

        def to_env(self):
            return {
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": "doc",
                "LEANFLOW_FORMALIZATION_REVISION": "rev",
            }

    def intake(**kwargs):
        order.append("intake")
        assert kwargs["require_preparation"] is True
        return Context()

    monkeypatch.setattr(workflow, "resolve_runtime_provider", runtime)
    monkeypatch.setattr(launch, "prepare_formalization_launch", prepare)
    monkeypatch.setattr(workflow, "discover_leanflow_project", project)
    monkeypatch.setattr(workflow, "prepare_formalization_document_context", intake)
    monkeypatch.setattr(
        "leanflow_cli.workflows.research_mode.research_local_loogle_enabled", lambda **kwargs: True
    )
    monkeypatch.setattr(
        "leanflow_cli.cli.loogle_local.ensure_local_loogle_for_project_async",
        lambda *args: pytest.fail("Unexpected additional resident index"),
    )
    result = workflow.resolve_workflow_request(
        "/formalize paper.tex --prompt 'Use a representative definition and dependent theorem'",
        active_cwd=root,
    )
    assert order == ["runtime", "prepare", "project", "intake"]
    assert result.child_env["LEANFLOW_FORMALIZATION_BUDGET_PATH"].endswith("fixture/budget.json")
    assert result.child_env["LEANFLOW_FORMALIZATION_REVISION"] == "rev"


def test_fresh_launch_gets_new_budget_and_explicit_resume_keeps_cumulative_usage(
    source_folder, monkeypatch
):
    root = source_folder
    receipts = []

    def prepare(project_root, record, **kwargs):
        receipt = {
            "preparation_id": "prepared-" + str(len(receipts)),
            "preparation_plan_id": kwargs["plan"]["plan_id"],
            "source_bundle": record["source_bundle"],
            "plan": kwargs["plan"],
            "budget_path": str(kwargs["budget_path"]),
        }
        receipts.append(receipt)
        return {"accepted": True, "receipt": receipt}

    monkeypatch.setattr(launch, "prepare_document_project", prepare)
    monkeypatch.setattr(
        launch, "load_preparation_receipt", lambda root: receipts[-1] if receipts else None
    )
    monkeypatch.setattr(
        launch,
        "assert_preparation_current",
        lambda root: (
            receipts[-1] if receipts else (_ for _ in ()).throw(ValueError("Not prepared"))
        ),
    )
    runtime = {"provider": "claude"}
    first = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        runtime,
        environment={
            "LEANFLOW_FORMALIZATION_DISCOVERY": "false",
            "LEANFLOW_FORMALIZATION_PRESET": "lean",
        },
    )
    second = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        runtime,
        environment={
            "LEANFLOW_FORMALIZATION_DISCOVERY": "false",
        },
    )
    assert first["budget_path"] != second["budget_path"] and len(receipts) == 2
    assert receipts[-1]["plan"]["preset"] == "lean", "Retain prior explicit core-only setup intent"
    accounting = CampaignLimits(
        second["budget_path"], {"agent_calls": 3, "tokens": None, "wall_time_s": None}
    )
    call = accounting.reserve()
    accounting.started_call(call)
    accounting.finish(call, {"input_tokens": 1}, started=True)
    resumed = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        runtime,
        environment={
            "LEANFLOW_NATIVE_RESUME_RUN_ID": "original-run",
        },
    )
    assert resumed["budget_path"] == second["budget_path"] and len(receipts) == 2
    assert (
        len(
            CampaignLimits(
                resumed["budget_path"], {"agent_calls": 3, "tokens": None, "wall_time_s": None}
            ).state["invocations"]
        )
        == 1
    )
