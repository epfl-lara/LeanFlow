"""Admit source-only formalization through checked preparation while keeping previews read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli import workflow
from leanflow_cli.formalization import preparation_launch as launch
from leanflow_cli.runtime.project_operations import project_setup
from tests.leanflow.preparation_helpers import inspector_readiness_record


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
    monkeypatch.setattr(launch, "inspect_project", lambda root: {"ready": True, "configured": True})
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


@pytest.mark.parametrize(
    "unsupported",
    [r"\unknownmath{A}", r"\ifdim 1pt<2pt Hidden mathematics.\fi"],
)
def test_source_blocker_precedes_discovery_budget_and_project_effects(
    source_folder, monkeypatch, unsupported
):
    root = source_folder
    source = root / "paper.tex"
    source.write_text(source.read_text() + unsupported)
    original = source.read_bytes()
    monkeypatch.setattr(
        launch, "discover_preparation_plan", lambda *args, **kwargs: pytest.fail("Paid discovery")
    )
    monkeypatch.setattr(
        launch, "prepare_document_project", lambda *args, **kwargs: pytest.fail("Project setup")
    )
    monkeypatch.setattr(launch, "load_config", lambda: pytest.fail("Campaign admitted"))
    with pytest.raises(
        ValueError, match="Source extraction needs repair before project preparation"
    ):
        launch.prepare_formalization_launch(root, "paper.tex", {"provider": "claude"})
    assert source.read_bytes() == original
    assert not (root / ".leanflow/formalization-campaigns").exists()
    assert not (root / "lean-toolchain").exists()


def test_preparation_owns_setup_before_activity_and_shares_discovery_budget(
    source_folder, monkeypatch, capsys
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
        kwargs["progress"](
            {
                "type": "progress",
                "phase": "dependencies",
                "message": "Downloading Mathlib",
                "log_path": "/private/setup.log",
            }
        )
        kwargs["progress"](
            {"type": "output", "phase": "dependencies", "message": "H tracked/source.lean"}
        )
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
    output = capsys.readouterr()
    assert output.out == ""
    assert "dependency_plan" in output.err
    assert "Downloading Mathlib" in output.err and "Log: /private/setup.log" in output.err
    assert "Project setup passed" in output.err
    assert "H tracked/source.lean" not in output.err


def test_automatic_preparation_progress_redacts_and_bounds_messages(capsys):
    launch._report_preparation_progress(
        {
            "phase": "dependencies",
            "message": "Fetching https://user:secret@example.test/repo " + "x" * 10000,
            "log_path": "/private/setup.log",
        }
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "user:secret" not in output.err and "[REDACTED]@example.test" in output.err
    assert len(output.err) < 2200


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


def test_stop_after_real_reuse_commit_prevents_native_workflow_continuation(
    source_folder, monkeypatch
):
    from leanflow_cli.formalization import preparation_reuse as reuse
    from leanflow_cli.formalization.evidence import identity
    from leanflow_cli.formalization.preparation import load_preparation_receipt
    from leanflow_cli.formalization.preparation_journal import (
        load_journal,
        load_registry,
        preparation_directory,
    )
    from leanflow_cli.formalization.preparation_plan import default_preparation_plan
    from leanflow_cli.formalization.preparation_transaction import (
        commit_preparation,
        protected_inputs,
    )
    from leanflow_cli.formalization.records import create_document

    root = source_folder
    record = create_document(root, root / "paper.tex")
    plan = default_preparation_plan(root, record, preset="lean")
    previous = {
        "schema_version": 1,
        "status": "prepared",
        "source_bundle": record["source_bundle"],
        "imports": plan["required_imports"],
        "layout": plan["layout"],
        "resolved_packages": [
            {"name": item["name"], "url": item["url"], "rev": item["revision"]}
            for item in plan["packages"]
        ],
        "environment_id": "checked-fixture-environment",
        "project_sources": {},
        "inspector_readiness": inspector_readiness_record(),
    }
    previous["preparation_id"] = "preparation-" + identity(previous)
    original = commit_preparation(
        root,
        root,
        managed={},
        protected=protected_inputs(root),
        receipt=previous,
        validate=lambda: None,
        cancelled=lambda: False,
    )
    original_path = (
        preparation_directory(root) / "transactions" / (original["transaction_id"] + ".json")
    )
    original_bytes = original_path.read_bytes()
    results = []
    monkeypatch.setattr(reuse, "environment_identity", lambda root: previous["environment_id"])

    def prepare(project_root, selected, **kwargs):
        def committed(name):
            if name == "journal:COMMITTED":
                kwargs["cancel"].set()

        result = reuse.reuse_preparation(
            project_root,
            selected,
            kwargs["plan"],
            previous,
            budget_path=kwargs["budget_path"],
            cancelled=kwargs["cancel"].is_set,
            checkpoint=committed,
        )
        results.append(result)
        return result

    monkeypatch.setattr(launch, "prepare_document_project", prepare)
    monkeypatch.setattr(
        workflow,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "codex", "model": "fixture"},
    )
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda *args: pytest.fail("Stopped preparation continued into native project discovery"),
    )
    monkeypatch.setattr(
        workflow,
        "prepare_formalization_document_context",
        lambda **kwargs: pytest.fail("Stopped preparation reached native intake"),
    )
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DISCOVERY", "false")
    with pytest.raises(ValueError, match="dependencies committed; preparation reuse stopped"):
        workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root)
    result = results[0]
    assert result["accepted"] is False and result["committed"] is True
    assert result["status"] == "cancelled"
    assert load_registry(root)["selected"] == result["transaction_id"]
    assert load_preparation_receipt(root) == result["receipt"]
    assert load_journal(root, result["transaction_id"])["writes"] == {}
    assert original_path.read_bytes() == original_bytes
    assert load_journal(root, original["transaction_id"])["plan"]["receipt"] == previous


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
