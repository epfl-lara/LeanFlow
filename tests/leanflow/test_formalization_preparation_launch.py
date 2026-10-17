"""Admit source-only formalization through checked preparation while keeping previews read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_limits import CampaignLimits, LimitReached
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
    monkeypatch.setattr(
        launch,
        "discover_preparation_plan",
        lambda *args, **kwargs: pytest.fail("Unexpected native discovery in a launch fixture"),
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


@pytest.mark.parametrize(
    "unsupported",
    [r"\write18{touch forbidden}", r"\input{missing-source}"],
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


@pytest.mark.parametrize(
    "lexical",
    [r"\unknownmath{A}", r"\ifdim 1pt<2pt Hidden mathematics.\fi"],
)
def test_lexical_uncertainty_reaches_discovery_without_becoming_source_approval(
    source_folder, monkeypatch, lexical
):
    from leanflow_cli.formalization.records import load_document

    root = source_folder
    source = root / "paper.tex"
    source.write_text(source.read_text() + lexical)
    original = source.read_bytes()
    observed = []

    def discover(project_root, record, plan, **kwargs):
        assert project_root == root
        assert any(row["blocking"] for row in record["inventory"]["diagnostics"])
        assert record.get("source_reconciliation") is None
        observed.append(record)
        return {"accepted": False, "status": "source_interpretation_required"}

    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(
        launch, "prepare_document_project", lambda *args, **kwargs: pytest.fail("Project setup")
    )
    with pytest.raises(ValueError, match="source_interpretation_required"):
        launch.prepare_formalization_launch(root, "paper.tex", {"provider": "codex"})
    assert len(observed) == 1 and source.read_bytes() == original
    saved = load_document(root, observed[0]["document_id"])
    assert saved.get("source_reconciliation") is None and not saved["reviews"]
    assert any(row["blocking"] for row in saved["inventory"]["diagnostics"])
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
            "run_id": "fixture-launch",
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


@pytest.mark.parametrize("receipt_path_format", ["absolute", "relative", "wrong_path"])
def test_fresh_launch_gets_new_budget_and_explicit_resume_keeps_cumulative_usage(
    source_folder, monkeypatch, receipt_path_format
):
    root = source_folder
    receipts = []

    def prepare(project_root, record, **kwargs):
        receipt = {
            "preparation_id": "prepared-" + str(len(receipts)),
            "preparation_plan_id": kwargs["plan"]["plan_id"],
            "source_bundle": record["source_bundle"],
            "plan": kwargs["plan"],
            "budget_path": (
                kwargs["budget_path"].relative_to(root).as_posix()
                if receipt_path_format == "relative"
                else str(kwargs["budget_path"])
            ),
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
            "LEANFLOW_WORKFLOW_RUN_ID": "first-run",
            "LEANFLOW_FORMALIZATION_DISCOVERY": "false",
            "LEANFLOW_FORMALIZATION_PRESET": "lean",
        },
    )
    second = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        runtime,
        environment={
            "LEANFLOW_WORKFLOW_RUN_ID": "original-run",
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
    for name in ("discover_preparation_plan", "prepare_document_project"):
        monkeypatch.setattr(
            launch, name, lambda *args, **kwargs: pytest.fail("Unexpected paid planning or setup")
        )
    monkeypatch.setattr(
        CampaignLimits,
        "reserve",
        lambda *args, **kwargs: pytest.fail("Unexpected call reservation"),
    )
    if receipt_path_format == "wrong_path":
        # A current receipt from another valid campaign must not take the reuse shortcut.
        receipts[-1]["budget_path"] = str(first["budget_path"])

        def stop_before_replanning(*args, **kwargs):
            raise ValueError("Mismatched receipt requires fresh preparation")

        monkeypatch.setattr(launch, "default_preparation_plan", stop_before_replanning)
        with pytest.raises(ValueError, match="Mismatched receipt requires fresh preparation"):
            launch.prepare_formalization_launch(
                root,
                "paper.tex",
                runtime,
                environment={"LEANFLOW_NATIVE_RESUME_RUN_ID": "original-run"},
            )
    else:
        monkeypatch.setattr(
            launch,
            "default_preparation_plan",
            lambda *args, **kwargs: pytest.fail("Current receipt was needlessly replanned"),
        )
        resumed = launch.prepare_formalization_launch(
            root,
            "paper.tex",
            runtime,
            environment={"LEANFLOW_NATIVE_RESUME_RUN_ID": "original-run"},
        )
        assert resumed["budget_path"] == second["budget_path"]
        assert resumed["receipt"]["preparation_id"] == receipts[-1]["preparation_id"]
        assert resumed["receipt"]["plan"] == receipts[-1]["plan"]
    assert len(receipts) == 2
    retained = CampaignLimits(
        second["budget_path"], {"agent_calls": 3, "tokens": None, "wall_time_s": None}
    ).state["invocations"]
    assert len(retained) == 1 and retained[call]["usage"] == {"input_tokens": 1}


def test_failed_discovery_resume_reuses_same_run_budget_without_preparation_receipt(
    source_folder, monkeypatch
):
    root = source_folder
    observations = []

    def discover(project_root, record, plan, **kwargs):
        budget = kwargs["budget"]
        observations.append((budget.path, set(budget.state["invocations"])))
        if len(observations) == 1:
            call = budget.reserve()
            budget.started_call(call)
            budget.finish(call, {"input_tokens": 11, "output_tokens": 7}, started=True)
            return {"accepted": False, "status": "fixture_discovery_failed"}
        return {"accepted": True, "plan": plan}

    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(launch, "load_preparation_receipt", lambda root: None)
    monkeypatch.setattr(
        launch,
        "prepare_document_project",
        lambda *args, **kwargs: {
            "accepted": True,
            "receipt": {"preparation_id": "prepared"},
        },
    )
    environment = {"LEANFLOW_WORKFLOW_RUN_ID": "discovery-retry"}
    with pytest.raises(ValueError, match="fixture_discovery_failed"):
        launch.prepare_formalization_launch(
            root, "paper.tex", {"provider": "codex"}, environment=environment
        )
    original = CampaignLimits(
        observations[0][0], {"agent_calls": 3, "tokens": None, "wall_time_s": None}
    )
    original_calls = set(original.state["invocations"])
    assert len(original_calls) == 1
    resumed = launch.prepare_formalization_launch(
        root,
        "paper.tex",
        {"provider": "codex"},
        environment={**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "discovery-retry"},
    )
    assert observations[1] == (observations[0][0], original_calls)
    assert resumed["budget_path"] == observations[0][0]
    assert set(CampaignLimits(resumed["budget_path"], original.limits).state["invocations"]) == (
        original_calls
    )


@pytest.mark.parametrize(
    ("limits", "usage", "message"),
    [
        ({"agent_calls": 1, "tokens": None, "wall_time_s": None}, {}, "agent-call limit"),
        (
            {"agent_calls": 3, "tokens": 18, "wall_time_s": None},
            {"input_tokens": 11, "output_tokens": 7},
            "reported-token limit",
        ),
        (
            {"agent_calls": 3, "tokens": 100, "wall_time_s": None},
            None,
            "Token usage is unavailable",
        ),
    ],
)
def test_failed_discovery_resume_preserves_exhaustion_and_unknown_usage(
    source_folder, monkeypatch, limits, usage, message
):
    root = source_folder
    paid_calls = []

    def discover(project_root, record, plan, **kwargs):
        budget = kwargs["budget"]
        invocation = budget.reserve()
        budget.started_call(invocation)
        paid_calls.append((budget.path, invocation))
        budget.finish(invocation, usage, started=True)
        return {"accepted": False, "status": "fixture_discovery_failed"}

    monkeypatch.setattr(launch, "load_config", lambda: {"limits": limits})
    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(launch, "load_preparation_receipt", lambda root: None)
    monkeypatch.setattr(
        launch, "prepare_document_project", lambda *args, **kwargs: pytest.fail("Setup admitted")
    )
    environment = {"LEANFLOW_WORKFLOW_RUN_ID": "capped-discovery"}
    with pytest.raises(ValueError, match="fixture_discovery_failed"):
        launch.prepare_formalization_launch(
            root, "paper.tex", {"provider": "codex"}, environment=environment
        )
    with pytest.raises(LimitReached, match=message):
        launch.prepare_formalization_launch(
            root,
            "paper.tex",
            {"provider": "codex"},
            environment={**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "capped-discovery"},
        )
    assert len(paid_calls) == 1
    saved = CampaignLimits(paid_calls[0][0], limits)
    assert list(saved.state["invocations"]) == [paid_calls[0][1]]
    assert saved.state["invocations"][paid_calls[0][1]]["usage"] == usage


@pytest.mark.parametrize(
    ("name", "replacement"),
    [("agent_calls", 4), ("tokens", 101), ("wall_time_s", 61)],
)
def test_failed_discovery_resume_rejects_changed_limits_before_paid_work(
    source_folder, monkeypatch, name, replacement
):
    root = source_folder
    limits = {"agent_calls": 3, "tokens": 100, "wall_time_s": 60}
    discoveries = []

    def discover(project_root, record, plan, **kwargs):
        discoveries.append(kwargs["budget"].path)
        invocation = kwargs["budget"].reserve()
        kwargs["budget"].started_call(invocation)
        kwargs["budget"].finish(invocation, {"input_tokens": 1}, started=True)
        return {"accepted": False, "status": "fixture_discovery_failed"}

    monkeypatch.setattr(launch, "load_config", lambda: {"limits": limits})
    monkeypatch.setattr(launch, "discover_preparation_plan", discover)
    monkeypatch.setattr(launch, "load_preparation_receipt", lambda root: None)
    monkeypatch.setattr(
        launch, "prepare_document_project", lambda *args, **kwargs: pytest.fail("Setup admitted")
    )
    environment = {"LEANFLOW_WORKFLOW_RUN_ID": "changed-cap"}
    with pytest.raises(ValueError, match="fixture_discovery_failed"):
        launch.prepare_formalization_launch(
            root, "paper.tex", {"provider": "codex"}, environment=environment
        )
    limits[name] = replacement
    with pytest.raises(ValueError, match="(?i)new campaign"):
        launch.prepare_formalization_launch(
            root,
            "paper.tex",
            {"provider": "codex"},
            environment={**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "changed-cap"},
        )
    assert len(discoveries) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing_state",
        "missing_marker",
        "wrong_marker",
        "wrong_checkpoint",
        "old_version",
        "other_workflow",
        "other_backend",
        "missing_baseline",
        "list_baseline",
        "nonstring_baseline",
        "invalid_hex_baseline",
        "changed_baseline",
        "invalid_portable_baseline",
        "missing_selection",
        "changed_selection",
        "author_accounting_uncertain",
        "native_cleanup_uncertain",
        "ledger_checkpoint_active",
        "ledger_accounting_error",
    ],
)
def test_public_resume_rejects_inconsistent_author_history_before_paid_stale_preparation(
    source_folder, monkeypatch, damage
):
    import json

    from core.campaign_limits import AccountingError
    from leanflow_cli.formalization import preparation_campaign as campaign
    from leanflow_cli.formalization.records import create_document, current_selection_token
    from leanflow_cli.native.harness_artifacts import source_files

    root = source_folder
    record = create_document(root, root / "paper.tex")
    run_id = "admitted-resume"
    with campaign.campaign_lock(root, run_id):
        binding = campaign.select_campaign(
            root,
            record,
            run_id=run_id,
            limits={"agent_calls": 3, "tokens": None, "wall_time_s": None},
            backend="codex",
            selection_token=current_selection_token(root, record["document_id"]),
            source_draft_only=False,
            effective_draft=False,
            resume=False,
        )
        directory = root / ".leanflow/native-workflows" / run_id
        state = directory / "state.json"
        campaign.admit_author(root, binding, resume=False, state_path=state)
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "workflow": "formalize",
                "backend": "codex",
                "run_id": run_id,
                "campaign_binding_id": binding["binding_id"],
                "baseline": {name: data.hex() for name, data in source_files(root).items()},
                "formalization_baseline": {},
                "formalization_selection_token": binding["selection_token"],
                "source_draft_intent": False,
                "source_draft_only": False,
            }
        )
    )
    budget = CampaignLimits(campaign.campaign_budget(root, binding), binding["limits"])
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, {"input_tokens": 11}, started=True)
    marker = directory / "author-admitted.json"
    if damage == "missing_state":
        state.unlink()
    elif damage == "missing_marker":
        marker.unlink()
    elif damage == "wrong_marker":
        marker.write_text(json.dumps({**json.loads(marker.read_text()), "binding_id": "0" * 64}))
    elif damage == "wrong_checkpoint":
        state.write_text(
            json.dumps({**json.loads(state.read_text()), "campaign_binding_id": "0" * 64})
        )
    elif damage.startswith("ledger_"):
        ledger = json.loads(budget.path.read_text())
        ledger[
            (
                "checkpoint_worker_active"
                if damage == "ledger_checkpoint_active"
                else "accounting_error"
            )
        ] = (True if damage == "ledger_checkpoint_active" else "Checkpoint write failed")
        budget.path.write_text(json.dumps(ledger))
    else:
        checkpoint = json.loads(state.read_text())
        field, replacement = {
            "old_version": ("version", 1),
            "other_workflow": ("workflow", "prove"),
            "other_backend": ("backend", "claude"),
            "missing_baseline": ("baseline", None),
            "list_baseline": ("baseline", []),
            "nonstring_baseline": ("baseline", {"Main.lean": 123}),
            "invalid_hex_baseline": ("baseline", {"Main.lean": "not hex"}),
            "changed_baseline": ("baseline", {"Main.lean": ""}),
            "invalid_portable_baseline": ("formalization_baseline", []),
            "missing_selection": ("formalization_selection_token", None),
            "changed_selection": ("formalization_selection_token", "other-selection"),
            "author_accounting_uncertain": ("accounting_unresolved", True),
            "native_cleanup_uncertain": ("native_cleanup_unverified", True),
        }[damage]
        if replacement is None:
            checkpoint.pop(field)
        else:
            checkpoint[field] = replacement
        state.write_text(json.dumps(checkpoint))
    before = {path: path.read_bytes() for path in (state, marker, budget.path) if path.exists()}

    def forbidden(*args, **kwargs):
        pytest.fail("Inconsistent admitted history reached paid work or dependency preparation")

    monkeypatch.setenv("LEANFLOW_NATIVE_RESUME_RUN_ID", run_id)
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda **kwargs: {"provider": "codex"}
    )
    monkeypatch.setattr(launch, "load_preparation_receipt", lambda root: {"status": "stale"})
    monkeypatch.setattr(launch, "discover_preparation_plan", forbidden)
    monkeypatch.setattr(launch, "prepare_document_project", forbidden)
    monkeypatch.setattr(CampaignLimits, "reserve", forbidden)
    monkeypatch.setattr(CampaignLimits, "persist", forbidden)
    with pytest.raises((ValueError, AccountingError)):
        workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert state.exists() == (damage != "missing_state")
    assert marker.exists() == (damage != "missing_marker")
