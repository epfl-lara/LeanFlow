from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.native import source_only_startup


def _configure_file_scope(monkeypatch: pytest.MonkeyPatch, root: Path, active: Path) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(root))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", str(active))
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "0")
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "0")
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "0")


def _forbid_lean_startup_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("source-only startup called a Lean backend")

    for name in (
        "lean_inspect",
        "probe_capabilities",
        "lean_goals",
        "_query_live_diagnostics",
        "_query_live_goals",
        "_query_live_goals_from_capabilities",
        "_retry_unverified_helper_gates",
        "_promote_live_state_to_verified_compat",
    ):
        monkeypatch.setattr(runner, name, forbidden)


def test_source_only_startup_selects_sorry_without_any_lean_call(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem first : True := by\n" "  sorry\n\n" "theorem second : True := by\n" "  sorry\n",
        encoding="utf-8",
    )
    _configure_file_scope(monkeypatch, tmp_path, active)
    _forbid_lean_startup_calls(monkeypatch)

    state = runner._build_source_only_startup_snapshot([], {}, {})

    assert state["proof_state_authority"] == "source_only_unverified"
    assert state["used_source_only_snapshot"] is True
    assert state["proof_solved"] is False
    assert "verification_ok" not in state
    assert state["last_verification"] == {}
    assert state["sorry_count"] == 2
    assert state["target_symbol"] == "first"
    assert state["current_queue_item"]["reasons"] == ["contains sorry"]
    assert "not queried" in state["diagnostics"]
    assert "not queried" in state["goals"]
    assert state["source_revision_sha256"]


def test_source_only_and_full_builder_select_the_same_source_queue_item(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem first : True := by\n" "  sorry\n\n" "theorem second : True := by\n" "  sorry\n",
        encoding="utf-8",
    )
    _configure_file_scope(monkeypatch, tmp_path, active)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    runner.plan_state.save_blueprint(
        runner.plan_state.Blueprint(
            nodes=(
                runner.plan_state.GraphNode(
                    id=runner.plan_state.node_id_for("first", str(active)),
                    kind="theorem",
                    name="first",
                    file=str(active),
                    statement=": True",
                    status="blocked",
                ),
                runner.plan_state.GraphNode(
                    id=runner.plan_state.node_id_for("second", str(active)),
                    kind="theorem",
                    name="second",
                    file=str(active),
                    statement=": True",
                    status="stated",
                ),
            )
        )
    )

    source_only = runner._build_source_only_startup_snapshot([], {}, {})

    class _Inspection:
        diagnostics = "no errors found"
        goals = "no goals queried for initial symbol"
        sorry_count = 2
        project_sorry_count = 2
        capability_report = {"degraded_reasons": []}
        queue_items: list[dict] = []

    monkeypatch.setattr(runner, "lean_inspect", lambda *_args, **_kwargs: _Inspection())
    monkeypatch.setattr(
        runner,
        "probe_capabilities",
        lambda *_args, **_kwargs: pytest.fail("inspection already supplied capabilities"),
    )
    monkeypatch.setattr(
        runner,
        "_query_live_goals_from_capabilities",
        lambda *_args, **_kwargs: "target goals",
    )
    monkeypatch.setattr(runner, "_retry_unverified_helper_gates", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runner, "_count_project_sorries", lambda _root: (2, ["Main.lean (2)"]))
    monkeypatch.setattr(
        runner,
        "_promote_live_state_to_verified_compat",
        lambda state, _autonomy=None: dict(state),
    )

    full = runner._build_live_proof_state([], {}, {})

    assert source_only["target_symbol"] == full["target_symbol"] == "second"
    assert source_only["current_queue_item"] == full["current_queue_item"]
    assert source_only["declaration_queue"] == full["declaration_queue"]


@pytest.mark.parametrize(
    "case",
    [
        "clean",
        "comment_only",
        "unreadable",
        "project_scope",
        "document",
        "frontier_excluded",
        "source_race",
    ],
)
def test_source_only_startup_falls_back_on_ambiguous_or_ineligible_state(
    monkeypatch, tmp_path, case
):
    active = tmp_path / "Main.lean"
    active.write_text(
        (
            "theorem demo : True := by\n  -- sorry is only a comment\n  trivial\n"
            if case == "comment_only"
            else (
                "theorem demo : True := by\n  trivial\n"
                if case == "clean"
                else "theorem demo : True := by\n  sorry\n"
            )
        ),
        encoding="utf-8",
    )
    _configure_file_scope(monkeypatch, tmp_path, active)
    if case == "unreadable":
        active.unlink()
    elif case == "project_scope":
        monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
        monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", f"prove {active}")
    elif case == "document":
        monkeypatch.setattr(runner, "_document_formalization_requested", lambda: True)
    elif case == "frontier_excluded":
        monkeypatch.setattr(
            runner,
            "_graph_frontier_precedence",
            lambda *_args, **_kwargs: lambda _label: 3,
        )
    elif case == "source_race":
        monkeypatch.setattr(
            runner.source_only_startup,
            "source_revision_is_current",
            lambda _revision: False,
        )

    assert runner._build_source_only_startup_snapshot([], {}, {}) == {}


def test_source_only_authority_can_never_verify_or_exit_zero(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_document_formalization_needs_planner_draft",
        lambda *_args, **_kwargs: False,
    )
    polluted = {
        "proof_state_authority": "source_only_unverified",
        "active_file": "/tmp/Main.lean",
        "declaration_scope": "file",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "sorry_count": 0,
        "verification_ok": True,
        "last_verification": {"ok": True},
    }

    assert runner._live_state_is_verified(polluted) is False
    assert runner._verified_workflow_should_exit_without_prompt(polluted) is False
    assert runner._workflow_completion_exit_code(polluted, {}) == runner.EXIT_PAUSED


def test_source_revision_guard_rejects_even_same_path_after_rewrite(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    revision = source_only_startup.capture_source_revision(str(active))
    assert revision is not None
    assert source_only_startup.source_revision_is_current(revision)

    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert not source_only_startup.source_revision_is_current(revision)


def test_main_reports_source_only_startup_and_skips_project_manager_and_full_builder(
    monkeypatch, tmp_path
):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    _configure_file_scope(monkeypatch, tmp_path, active)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(runner, "install_native_termination_handlers", lambda: {})
    monkeypatch.setattr(runner, "restore_native_termination_handlers", lambda _handlers: None)
    monkeypatch.setattr(runner, "defer_repeated_sigint", lambda: None)
    monkeypatch.setattr(runner, "restore_sigint", lambda _handler: None)
    monkeypatch.setattr(runner, "_install_workflow_run_log_capture", lambda: None)
    monkeypatch.setattr(runner, "_persist_startup_live_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_reconcile_stale_workflow_file_locks", lambda: 0)
    monkeypatch.setattr(runner, "_cleanup_scratch_artifacts_on_startup", lambda _state: None)
    monkeypatch.setattr(runner.environment_memory, "hydrate", lambda _state: None)
    monkeypatch.setattr(runner, "_restore_queue_manager_state", lambda _state: False)
    monkeypatch.setattr(runner, "_reconcile_source_transaction_state", lambda _state: {})
    monkeypatch.setattr(runner, "_initialize_campaign_root_authority", lambda _state: True)
    monkeypatch.setattr(runner, "_migrate_negation_promotions_on_startup", lambda: {})
    monkeypatch.setattr(runner.research_findings, "hydrate_delivery_markers", lambda _state: None)
    monkeypatch.setattr(
        runner,
        "_reconcile_negation_promotions_on_startup",
        lambda _state: runner.negation_promotion.PromotionReconciliation(),
    )
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_plan_state_resume_block", lambda _state: "")
    monkeypatch.setattr(
        runner,
        "_ensure_project_prove_manager_started",
        lambda *_args, **_kwargs: pytest.fail("file-scoped source path started project manager"),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state_compat",
        lambda *_args, **_kwargs: pytest.fail("source path fell through to full Lean builder"),
    )
    monkeypatch.setattr(runner, "_persist_live_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_print_header", lambda: None)
    monkeypatch.setattr(runner, "_compact_closed_activity_on_startup", lambda: None)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: events.append((event, details)),
    )
    monkeypatch.setattr(
        runner,
        "_build_agent",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(runner, "_record_campaign_exit", lambda code, *_args, **_kwargs: code)
    monkeypatch.setattr(runner, "shutdown_native_runtime_services", lambda _agent: None)

    assert runner.main() == runner.EXIT_INTERRUPTED
    finished = next(
        details for event, details in events if event == "startup-proof-state-refresh-finished"
    )
    assert finished["used_verified_preflight"] is False
    assert finished["used_source_only_snapshot"] is True
    assert "source_only_snapshot" in finished["phase_seconds"]


def test_pre_provider_recheck_keeps_current_source_only_snapshot(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    _configure_file_scope(monkeypatch, tmp_path, active)
    state = runner._build_source_only_startup_snapshot([], {}, {})
    events: list[str] = []
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state_compat",
        lambda *_args, **_kwargs: pytest.fail(
            "current source revision triggered full Lean refresh"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, *_args, **_kwargs: events.append(event),
    )

    refreshed, queue_changed = runner._recheck_source_only_snapshot_before_provider(
        [], {}, {}, state
    )

    assert refreshed == state
    assert queue_changed is False
    assert events == ["startup-source-only-revision-current"]


def test_pre_provider_recheck_uses_full_authority_after_source_change(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    _configure_file_scope(monkeypatch, tmp_path, active)
    state = runner._build_source_only_startup_snapshot([], {}, {})
    active.write_text(
        "theorem helper : True := by\n  trivial\n\n" "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    events: list[str] = []
    full_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "lean_inspection",
    }
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state_compat",
        lambda *_args, **_kwargs: dict(full_state),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, *_args, **_kwargs: events.append(event),
    )

    refreshed, queue_changed = runner._recheck_source_only_snapshot_before_provider(
        [], {}, {}, state
    )

    assert refreshed == full_state
    assert queue_changed is True
    assert events == ["startup-source-only-revision-stale"]
