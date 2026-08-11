from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leanflow_cli.config import save_config
from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import workflow_state
from leanflow_cli.workflows.workflow_state import (
    WorkflowLiveStatusOwnerConflictError,
    _agent_event_preview,
    append_workflow_activity,
    append_workflow_run_log,
    enqueue_workflow_agent_message,
    load_workflow_live_status,
    mark_workflow_live_status_startup,
    read_workflow_activity,
    read_workflow_agent_inbox,
    read_workflow_run_log,
    release_workflow_run_log_owner,
    reset_workflow_run_log,
    resolve_workflow_agent_id,
    save_workflow_live_status,
    summarize_workflow_agents,
    terminate_all_workflow_agents,
    terminate_project_workflow_agents,
    terminate_workflow_agent,
    terminate_workflow_agent_descendants,
    workflow_agent_activity_path,
    workflow_agent_detail,
    workflow_agent_transcript,
    workflow_latest_run_activity_path,
    workflow_run_activity_path,
    workflow_run_metadata_path,
    workflow_runs_root,
)


def _owned_process(process_id: int) -> dict[str, object]:
    """Return a complete fake process identity for signal-path tests."""
    return {
        "process_id": process_id,
        "process_group_id": process_id,
        "process_session_id": process_id,
        "process_token_sha256": "a" * 64,
    }


def test_persist_live_status_writes_shell_visible_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "google/gemma-3-27b-it")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "2")
    monkeypatch.setenv("LEANFLOW_PLANNER_ENABLED", "1")

    runner._persist_live_status(
        [{"role": "assistant", "content": "Working"}],
        compaction_state={"reason": "auto", "snapshot_text": "snapshot"},
        checkpoint_state={
            "count": 2,
            "current": {
                "label": "proof milestone",
                "linked_filesystem_checkpoint": "abc123def456",
            },
        },
        live_state={
            "active_file": "Main.lean",
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "x : Nat\n⊢ x = x",
            "build_status": "lake build running",
            "message": "1 goal remaining",
            "sorry_count": 1,
            "blocker_summary": "remaining sorry",
            "proof_solved": True,
            "warning_cleanup_status": "verified",
            "warning_cleanup_attempted": True,
            "warning_cleanup_verified": True,
            "warning_cleanup_warning_count": 0,
            "warning_cleanup_diagnostics": "warning cleanup verified; no warnings remain",
            "warning_cleanup": {
                "status": "verified",
                "proof_solved": True,
                "attempted": True,
                "verified": True,
                "skipped": False,
                "blocked": False,
                "warning_count": 0,
                "warning_summary": "",
                "diagnostics": "warning cleanup verified; no warnings remain",
            },
            "document_formalization_handoff": {
                "ok": False,
                "issues": ["statement/source verification pending"],
            },
            "document_formalization_proof_sorry_count": 2,
            "document_formalization_construction_sorry_count": 1,
        },
        phase="busy",
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "busy"
    assert payload["runtime_heartbeat_at"] == payload["updated_at"]
    assert payload["parallel_agents"] == 1
    assert payload["agent_capacity"] == {
        "foreground": 1,
        "planner": 2,
        "background": 2,
        "shared_background": 2,
        "total": 3,
    }
    assert payload["workflow_kind"] == "prove"
    assert payload["active_skill"] == "lean-proof-loop"
    assert payload["latest_checkpoint_label"] == "proof milestone"
    assert payload["snapshot_present"] is True
    assert payload["goals"] == "x : Nat\n⊢ x = x"
    assert payload["proof_solved"] is True
    assert payload["warning_cleanup_status"] == "verified"
    assert payload["warning_cleanup_verified"] is True
    assert payload["warning_cleanup"]["status"] == "verified"
    assert payload["document_formalization_handoff"]["ok"] is False
    assert payload["document_formalization_handoff"]["issues"] == [
        "statement/source verification pending"
    ]
    assert payload["document_formalization_proof_sorry_count"] == 2
    assert payload["document_formalization_construction_sorry_count"] == 1


def test_startup_status_preserves_mathematical_snapshot_and_replaces_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "current-run-token")
    save_workflow_live_status(
        {
            "phase": "exited",
            "process_id": 0,
            "process_group_id": 111,
            "process_session_id": 222,
            "process_token_sha256": "old-token-fingerprint",
            "active_file": "/tmp/project/Main.lean",
            "target_symbol": "remaining_goal",
            "diagnostics": "warning: declaration uses sorry",
            "sorry_count": 1,
            "proof_solved": False,
            "stale_snapshot": True,
            "stale_process_id": 999,
            "interrupt_source": "signal",
            "exit_code": 2,
            "reason": "explicit interactive exit",
        }
    )

    mark_workflow_live_status_startup(
        phase="starting",
        metadata={
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": "/tmp/project",
        },
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "starting"
    assert payload["process_id"] == os.getpid()
    assert payload["process_token_sha256"] != "old-token-fingerprint"
    assert payload["runtime_heartbeat_at"] == payload["updated_at"]
    assert payload["startup_reconciliation_pending"] is True
    assert payload["active_file"] == "/tmp/project/Main.lean"
    assert payload["target_symbol"] == "remaining_goal"
    assert payload["diagnostics"] == "warning: declaration uses sorry"
    assert payload["sorry_count"] == 1
    assert payload["proof_solved"] is False
    assert payload["interrupt_source"] == ""
    assert "exit_code" not in payload
    assert "reason" not in payload
    assert "stale_snapshot" not in payload
    assert "stale_process_id" not in payload


def test_startup_status_without_previous_snapshot_does_not_invent_proof_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))

    mark_workflow_live_status_startup(
        phase="reconciling",
        metadata={"workflow_kind": "prove", "project_root": "/tmp/project"},
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "reconciling"
    assert payload["process_id"] == os.getpid()
    assert payload["startup_reconciliation_pending"] is True
    assert "sorry_count" not in payload
    assert "proof_solved" not in payload


def test_startup_status_rejects_distinct_verified_live_owner(monkeypatch, tmp_path):
    """A second startup must not replace an exactly verified live runner."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "new-owner-token")
    existing = {
        "version": 1,
        "phase": "reconciling",
        "process_id": 24680,
        "process_group_id": 24680,
        "process_session_id": 24680,
        "process_token_sha256": "a" * 64,
        "run_id": "prove-existing",
        "updated_at": "2026-07-19T06:11:42+00:00",
        "runtime_heartbeat_at": "2026-07-19T06:11:42+00:00",
        "target_symbol": "erdos_242.variants.schinzel_generalization",
    }
    save_workflow_live_status(existing)
    monkeypatch.setattr(
        workflow_state,
        "process_identity_matches",
        lambda identity: identity.pid == 24680,
    )

    with pytest.raises(WorkflowLiveStatusOwnerConflictError, match="verified live owner"):
        mark_workflow_live_status_startup(
            phase="starting",
            metadata={"run_id": "prove-contender"},
        )

    assert workflow_state.read_json_file(workflow_state.workflow_live_status_path()) == existing


def test_foreign_terminal_save_cannot_replace_verified_live_owner(monkeypatch, tmp_path):
    """A losing startup finalizer cannot erase the winning owner's live pointer."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "losing-owner-token")
    existing = {
        "version": 1,
        "phase": "reconciling",
        "process_id": 24680,
        "process_group_id": 24680,
        "process_session_id": 24680,
        "process_token_sha256": "a" * 64,
        "run_id": "prove-existing",
    }
    save_workflow_live_status(existing)
    monkeypatch.setattr(
        workflow_state,
        "process_identity_matches",
        lambda identity: identity.pid == 24680,
    )

    with pytest.raises(WorkflowLiveStatusOwnerConflictError, match="verified live owner"):
        save_workflow_live_status(
            {
                "version": 1,
                "phase": "failed",
                "process_id": 0,
                "run_id": "prove-contender",
            }
        )

    assert workflow_state.read_json_file(workflow_state.workflow_live_status_path()) == existing


def test_current_owner_can_save_terminal_live_status(monkeypatch, tmp_path):
    """The owning runner may clear its process identity during finalization."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "current-owner-token")
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "process_id": os.getpid(),
            "run_id": "prove-current",
        }
    )
    monkeypatch.setattr(
        workflow_state,
        "process_identity_matches",
        lambda identity: identity.pid == os.getpid(),
    )

    save_workflow_live_status(
        {
            "version": 1,
            "phase": "exited",
            "process_id": 0,
            "run_id": "prove-current",
        }
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "exited"
    assert payload["process_id"] == 0


def test_startup_status_takes_over_stale_startup_owner_with_audit_metadata(monkeypatch, tmp_path):
    """A stale startup owner is replaceable without erasing its last startup phase."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "new-owner-token")
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "reconciling",
            "process_id": 24680,
            "process_group_id": 24680,
            "process_session_id": 24680,
            "process_token_sha256": "a" * 64,
            "run_id": "prove-aborted",
            "updated_at": "2026-07-19T06:11:42+00:00",
            "runtime_heartbeat_at": "2026-07-19T06:11:42+00:00",
            "target_symbol": "erdos_242.variants.schinzel_generalization",
        }
    )
    monkeypatch.setattr(
        workflow_state,
        "process_identity_matches",
        lambda identity: identity.pid == os.getpid(),
    )

    mark_workflow_live_status_startup(
        phase="starting",
        metadata={"run_id": "prove-resumed"},
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "starting"
    assert payload["process_id"] == os.getpid()
    assert payload["run_id"] == "prove-resumed"
    assert payload["target_symbol"] == "erdos_242.variants.schinzel_generalization"
    assert payload["startup_previous_owner"] == {
        "phase": "reconciling",
        "process_id": 24680,
        "process_group_id": 24680,
        "process_session_id": 24680,
        "process_token_sha256": "a" * 64,
        "run_id": "prove-aborted",
        "updated_at": "2026-07-19T06:11:42+00:00",
        "runtime_heartbeat_at": "2026-07-19T06:11:42+00:00",
    }


def test_startup_status_takes_over_reused_pid_identity(monkeypatch, tmp_path):
    """A matching PID without the matching launch token is not a live owner."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "current-owner-token")
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "starting",
            "process_id": os.getpid(),
            "process_group_id": os.getpgid(os.getpid()),
            "process_session_id": os.getsid(os.getpid()),
            "process_token_sha256": "a" * 64,
            "run_id": "prove-reused-pid",
        }
    )

    mark_workflow_live_status_startup(
        phase="reconciling",
        metadata={"run_id": "prove-current"},
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "reconciling"
    assert payload["run_id"] == "prove-current"
    assert payload["startup_previous_owner"]["process_id"] == os.getpid()
    assert payload["startup_previous_owner"]["run_id"] == "prove-reused-pid"


def test_stale_status_normalization_cannot_clobber_concurrent_startup_claim(monkeypatch, tmp_path):
    """A status reader must recheck stale normalization under the owner lock."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "new-owner-token")
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "reconciling",
            "process_id": 24680,
            "process_group_id": 24680,
            "process_session_id": 24680,
            "process_token_sha256": "a" * 64,
            "run_id": "prove-stale",
        }
    )
    monkeypatch.setattr(
        workflow_state,
        "process_identity_matches",
        lambda identity: identity.pid == os.getpid(),
    )
    normalize = workflow_state._normalize_workflow_live_status_payload
    first_normalization = True

    def claim_during_normalization(payload):
        nonlocal first_normalization
        result = normalize(payload)
        if first_normalization:
            first_normalization = False
            mark_workflow_live_status_startup(
                phase="starting",
                metadata={"run_id": "prove-winner"},
            )
        return result

    monkeypatch.setattr(
        workflow_state,
        "_normalize_workflow_live_status_payload",
        claim_during_normalization,
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "starting"
    assert payload["process_id"] == os.getpid()
    assert payload["run_id"] == "prove-winner"


def test_dead_startup_normalization_preserves_previous_owner_metadata(monkeypatch, tmp_path):
    """Reading an aborted startup retains its last phase before marking it dead."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "reconciling",
            "process_id": 24680,
            "process_group_id": 24680,
            "process_session_id": 24680,
            "process_token_sha256": "a" * 64,
            "run_id": "prove-aborted",
            "updated_at": "2026-07-19T06:11:42+00:00",
            "runtime_heartbeat_at": "2026-07-19T06:11:42+00:00",
        }
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda _identity: False)

    payload = load_workflow_live_status()

    assert payload["phase"] == "dead"
    assert payload["startup_previous_owner"]["phase"] == "reconciling"
    assert payload["startup_previous_owner"]["process_id"] == 24680
    assert payload["startup_previous_owner"]["run_id"] == "prove-aborted"


def test_rebuilt_live_status_clears_startup_reconciliation_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "_held_lock_count", lambda _owner_id: 0)
    mark_workflow_live_status_startup(
        phase="reconciling",
        metadata={"workflow_kind": "prove", "project_root": "/tmp/project"},
    )

    runner._persist_live_status(
        [],
        checkpoint_state={},
        live_state={
            "active_file": "/tmp/project/Main.lean",
            "target_symbol": "remaining_goal",
            "diagnostics": "warning: declaration uses sorry",
            "sorry_count": 1,
        },
        phase="ready",
    )

    payload = load_workflow_live_status()
    assert payload["phase"] == "ready"
    assert "startup_reconciliation_pending" not in payload


@pytest.mark.parametrize("phase", ["starting", "reconciling"])
def test_startup_live_phases_count_as_active_agent_status(phase):
    assert workflow_state._agent_status_from_live_phase(phase) == "active"


def test_owner_activity_refreshes_live_runtime_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    save_workflow_live_status(
        {
            "process_id": os.getpid(),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runtime_heartbeat_at": "2026-01-01T00:00:00+00:00",
        }
    )

    append_workflow_activity("plan-graph-reconcile", "helper became proved")

    payload = load_workflow_live_status()
    assert payload["runtime_heartbeat_at"] > "2026-01-01T00:00:00+00:00"
    assert payload["updated_at"] == payload["runtime_heartbeat_at"]
    assert payload["last_activity_type"] == "plan-graph-reconcile"
    assert payload["last_activity_message"] == "helper became proved"


def test_foreign_process_activity_cannot_refresh_owner_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    save_workflow_live_status(
        {
            "process_id": os.getpid() + 10_000_000,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runtime_heartbeat_at": "2026-01-01T00:00:00+00:00",
        }
    )

    append_workflow_activity("research-portfolio", "background worker changed")

    payload = load_workflow_live_status()
    assert payload["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["runtime_heartbeat_at"] == "2026-01-01T00:00:00+00:00"


def test_persist_live_status_releases_locks_before_exit_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_RUNNER_OWNER", "agent-a")

    released: list[str] = []
    monkeypatch.setattr(
        runner,
        "release_all_file_locks",
        lambda *, owner_id: released.append(owner_id) or {"released": 1},
    )
    monkeypatch.setattr(runner, "_held_lock_count", lambda owner_id: 0 if released else 1)

    runner._persist_live_status(
        [{"role": "assistant", "content": "Done"}],
        live_state={
            "active_file": "Main.lean",
            "active_file_label": "Main.lean",
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "lake env lean Main.lean exits 0",
            "verification_ok": True,
            "last_verification": {"ok": True, "scope": "file", "tool": "lean_verify"},
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "sorry_count": 0,
        },
        phase="exited",
    )

    payload = load_workflow_live_status()

    assert released == ["agent-a"]
    assert payload["phase"] == "exited"
    assert payload["held_locks"] == 0


@pytest.mark.parametrize(
    ("exit_code", "reason", "proof_solved", "sorry_count"),
    [
        (0, "verified completion", True, 0),
        (runner.EXIT_PAUSED, "explicit interactive exit", False, 1),
        (runner.EXIT_PAUSED, "infrastructure pause", False, 1),
        (runner.EXIT_DISPROVED, "authoritative disproof", False, 1),
        (runner.EXIT_INTERRUPTED, "signal interrupt", False, 1),
    ],
    ids=[
        "verified",
        "explicit-exit-unresolved",
        "infrastructure-pause",
        "disproved",
        "signal",
    ],
)
def test_finalizer_persists_truthful_terminal_outcome_in_live_status(
    monkeypatch,
    tmp_path,
    exit_code,
    reason,
    proof_solved,
    sorry_count,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".leanflow").mkdir()
    (project / ".leanflow" / "project.yaml").write_text("name: demo\n", encoding="utf-8")
    source = project / "Main.lean"
    source.write_text(
        (
            "theorem demo : False := by\n  sorry\n"
            if exit_code == runner.EXIT_DISPROVED
            else "theorem demo : True := by\n  trivial\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv(
        "LEANFLOW_PLAN_STATE_DIR",
        str(project / ".leanflow" / "workflow-state"),
    )
    monkeypatch.setattr(runner, "_stop_native_owned_work", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_release_native_runner_locks", lambda _agent: None)
    monkeypatch.setattr(
        runner,
        "_write_signal_interruption_checkpoint",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_record_campaign_exit", lambda code, *args, **kwargs: code)
    monkeypatch.setattr(runner, "_held_lock_count", lambda _owner_id: 0)
    monkeypatch.setattr(runner, "_maybe_record_learnings", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_generate_final_report", lambda *args, **kwargs: None)

    live_state = {
        "active_file": str(source),
        "target_symbol": "demo",
        "declaration_scope": "file",
        "proof_solved": proof_solved,
        "sorry_count": sorry_count,
    }
    autonomy_state = {}
    if exit_code == 0:
        verified_state = {
            **live_state,
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "lake env lean Main.lean exits 0",
            "verification_ok": True,
            "last_verification": {
                "ok": True,
                "scope": "file",
                "tool": "lean_verify",
            },
        }
        monkeypatch.setattr(runner, "_negation_reconciliation_barrier", lambda _state: False)
        monkeypatch.setattr(
            runner,
            "_revalidate_verified_scope_after_quiescence",
            lambda *args, **kwargs: dict(verified_state),
        )
        monkeypatch.setattr(
            runner,
            "_live_state_is_verified",
            lambda state: bool(dict(state or {}).get("verification_ok")),
        )
    elif exit_code == runner.EXIT_DISPROVED:
        monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
        monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "status-finalizer-disproof")
        campaign = runner.campaign_epoch.ensure_campaign(autonomy_state)
        setup = runner.campaign_roots.initialize_campaign_roots(
            campaign_id=campaign["campaign_id"],
            project_root=project,
            source_files=(source,),
        )
        assert setup.ok, setup.reason
        promotion = {
            "ok": True,
            "is_main_goal": True,
            "evidence": {"operation_path": str(source)},
        }
        autonomy_state.update(
            {
                "terminal_outcome": "disproved",
                "negation_promotion": promotion,
            }
        )
        monkeypatch.setattr(
            runner,
            "_revalidate_disproof_after_quiescence",
            lambda _state: runner.negation_promotion.PromotionReconciliation(
                terminal_disproof=True
            ),
        )
        monkeypatch.setattr(
            runner.negation_promotion,
            "authoritative_runtime_main_promotion",
            lambda *args, **kwargs: dict(promotion),
        )
        monkeypatch.setattr(
            runner.negation_promotion,
            "revalidate_promotion",
            lambda *args, **kwargs: runner.negation_promotion.PromotionResult(
                True,
                "current",
                is_main_goal=True,
            ),
        )

    result = runner._finalize_native_run(
        runner.NativeRunFinalizer(),
        exit_code,
        agent=None,
        history=[],
        compaction_state={},
        checkpoint_state={},
        autonomy_state=autonomy_state,
        live_state=live_state,
        reason=reason,
    )

    payload = load_workflow_live_status()
    assert result == exit_code
    assert payload["phase"] == "exited"
    assert payload["process_id"] == 0
    assert payload["exit_code"] == exit_code
    assert payload["reason"] == reason
    assert payload["proof_solved"] is proof_solved
    expected_sorry_count = 0 if exit_code == runner.EXIT_INTERRUPTED else sorry_count
    assert payload["sorry_count"] == expected_sorry_count
    assert payload["interrupt_source"] == ("signal" if exit_code == runner.EXIT_INTERRUPTED else "")


def test_workflow_state_prefers_project_local_state(monkeypatch, tmp_path):
    project = tmp_path / "project"
    (project / ".leanflow").mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    reset_workflow_run_log()
    append_workflow_run_log("alpha\n")

    assert read_workflow_run_log(1) == "alpha"
    assert (project / ".leanflow" / "workflow-state" / "latest-run.log").is_file()


def test_record_activity_captures_workflow_and_skill_context(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "review")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/review Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-diagnostics")

    runner._record_activity("resume", "Loaded workflow checkpoint", checkpoint_label="milestone")

    events = read_workflow_activity(limit=4)

    assert len(events) == 1
    assert events[0]["type"] == "resume"
    assert events[0]["details"]["workflow_kind"] == "review"
    assert events[0]["details"]["active_skill"] == "lean-diagnostics"
    assert events[0]["details"]["checkpoint_label"] == "milestone"


def test_workflow_run_log_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    reset_workflow_run_log()
    append_workflow_run_log("line 1\n")
    append_workflow_run_log("line 2\n")
    append_workflow_run_log("line 3\n")

    assert read_workflow_run_log(tail_lines=2) == "line 2\nline 3"


def test_workflow_run_log_does_not_share_cross_process_activity_append_lock(monkeypatch, tmp_path):
    """The top-level console tee must not queue behind background JSONL writers."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-owner-run")
    reset_workflow_run_log()
    monkeypatch.setattr(
        workflow_state,
        "_locked_append",
        lambda *_args, **_kwargs: pytest.fail("run log used the shared activity flock"),
    )

    append_workflow_run_log("foreground verification\n")

    assert read_workflow_run_log() == "foreground verification"
    assert (workflow_runs_root() / "prove-owner-run.log").read_text(
        encoding="utf-8"
    ) == "foreground verification\n"


def test_workflow_run_log_owner_takeover_preserves_run_attribution(monkeypatch, tmp_path):
    """An older runner keeps its own log but cannot append to the new latest log."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-old-run")
    reset_workflow_run_log()
    append_workflow_run_log("old owner\n")

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-new-run")
    reset_workflow_run_log()
    append_workflow_run_log("new owner\n")

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-old-run")
    append_workflow_run_log("late old output\n")

    assert read_workflow_run_log() == "new owner"
    assert (workflow_runs_root() / "prove-old-run.log").read_text(encoding="utf-8") == (
        "old owner\nlate old output\n"
    )
    assert (workflow_runs_root() / "prove-new-run.log").read_text(encoding="utf-8") == "new owner\n"


def test_workflow_run_log_exact_owner_release_cannot_be_reacquired_by_late_output(
    monkeypatch, tmp_path
):
    """Retire the exact exiting run's latest-log token while preserving its own log."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-exiting-run")
    reset_workflow_run_log()
    append_workflow_run_log("before exit\n")
    owner_path = workflow_state._workflow_run_log_owner_path()

    assert release_workflow_run_log_owner() is True
    assert owner_path.exists() is False

    append_workflow_run_log("late cleanup output\n")

    assert owner_path.exists() is False
    assert read_workflow_run_log() == "before exit"
    assert (workflow_runs_root() / "prove-exiting-run.log").read_text(encoding="utf-8") == (
        "before exit\nlate cleanup output\n"
    )


def test_workflow_run_log_release_preserves_a_newer_owner(monkeypatch, tmp_path):
    """An older finalizer cannot remove a concurrent runner's exact token."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-old-run")
    reset_workflow_run_log()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-new-run")
    reset_workflow_run_log()
    owner_path = workflow_state._workflow_run_log_owner_path()
    newer_token = owner_path.read_text(encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-old-run")
    assert release_workflow_run_log_owner() is False

    assert owner_path.read_text(encoding="utf-8") == newer_token


def test_workflow_run_log_uses_dedicated_cross_process_flock(monkeypatch, tmp_path):
    """Console durability uses its own flock rather than the activity lock."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-owner-run")
    operations: list[int] = []
    assert workflow_state.fcntl is not None
    monkeypatch.setattr(
        workflow_state.fcntl,
        "flock",
        lambda _fd, operation: operations.append(operation),
    )

    reset_workflow_run_log()
    append_workflow_run_log("foreground verification\n")

    assert operations == [
        workflow_state.fcntl.LOCK_EX,
        workflow_state.fcntl.LOCK_UN,
        workflow_state.fcntl.LOCK_EX,
        workflow_state.fcntl.LOCK_UN,
    ]


def test_locked_append_reports_local_flock_and_write_latency(monkeypatch, tmp_path):
    path = tmp_path / "activity.jsonl"
    records = []
    ticks = iter(float(value) for value in range(0, 20, 2))
    monkeypatch.setattr(workflow_state.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        workflow_state,
        "_record_slow_append",
        lambda *args, **kwargs: records.append((args, kwargs)),
    )

    workflow_state._locked_append(path, "event\n")

    assert path.read_text(encoding="utf-8") == "event\n"
    assert len(records) == 1
    args, timing = records[0]
    assert args == (path,)
    assert timing["text_bytes"] == len(b"event\n")
    assert timing["elapsed_s"] > 0
    assert timing["local_lock_wait_s"] > 0
    assert timing["cross_process_lock_wait_s"] > 0
    assert timing["write_s"] > 0


def test_workflow_run_log_tail_streams_large_history(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    path = workflow_state.workflow_run_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"line {index}\n" for index in range(20_000)),
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def guarded_read_text(candidate, *args, **kwargs):
        if candidate == path:
            raise AssertionError("run-log history must be streamed")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert read_workflow_run_log(tail_lines=3) == ("line 19997\nline 19998\nline 19999")


def test_workflow_run_log_creates_timestamped_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("LEANFLOW_WORKFLOW_RUN_ID", raising=False)

    reset_workflow_run_log()
    append_workflow_run_log("alpha\nbeta\n")

    run_logs = list(workflow_runs_root().glob("*.log"))
    assert len(run_logs) == 1
    assert run_logs[0].name.startswith("prove-")
    assert run_logs[0].read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_workflow_activity_preserves_full_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    full_text = "x" * 500

    append_workflow_activity("assistant-response", "Assistant response received", content=full_text)

    events = read_workflow_activity(limit=1)
    assert events[0]["event_id"]
    assert events[0]["run_id"]
    assert events[0]["timestamp"]
    assert events[0]["task_label"] == "prove"
    assert events[0]["details"]["content"] == full_text


def test_workflow_activity_preview_uses_reasoning_when_content_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        content="",
        reasoning_content="Plan: inspect diagnostics, patch theorem, rerun lake env lean.",
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview.startswith("Reasoning: ")
    assert "inspect diagnostics" in preview


def test_workflow_activity_preview_uses_configured_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_config(
        {
            "logging": {
                "activity_preview_chars": 80,
            }
        }
    )

    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        content="This is a deliberately long assistant response that should be truncated much earlier once the configured activity preview limit is applied.",
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert len(preview) <= 80
    assert preview.endswith("...")


def test_api_request_preview_includes_step_size(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "api-request",
        "API call #7",
        iteration=7,
        message_count=14,
        approx_tokens=12345,
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview == "API step #7 · 14 messages · ~12,345 tokens"


def test_conversation_start_preview_uses_larger_default_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    prompt = "Start " + ("x" * 360)

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        user_message=prompt,
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview.startswith("Prompt: Start ")
    assert "x" * 300 in preview


def test_workflow_activity_writes_run_and_agent_jsonl_streams(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("LEANFLOW_WORKFLOW_RUN_ID", raising=False)

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        workflow_kind="prove",
        active_skill="lean-proof-loop",
    )

    latest_run_path = workflow_latest_run_activity_path()
    assert latest_run_path is not None
    root_events = latest_run_path.read_text(encoding="utf-8").splitlines()
    root_event = json.loads(root_events[0])
    run_path = workflow_run_activity_path(root_event["run_id"])
    agent_path = workflow_agent_activity_path("12345", "prove")

    assert run_path.is_file()
    assert agent_path.is_file()
    assert not (tmp_path / "home" / "workflow-state" / "activity.jsonl").exists()

    run_event = json.loads(run_path.read_text(encoding="utf-8").splitlines()[0])
    agent_event = json.loads(agent_path.read_text(encoding="utf-8").splitlines()[0])

    assert run_event["event_id"] == root_event["event_id"]
    assert agent_event["agent_id"] == "12345"
    assert agent_event["task_label"] == "prove"


def test_workflow_activity_marks_runner_start_as_top_level(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "use abs_abs_sub first")
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project_root))
    monkeypatch.delenv("LEANFLOW_WORKFLOW_RUN_ID", raising=False)

    append_workflow_activity("runner-start", "Managed workflow runner started")

    latest_run_path = workflow_latest_run_activity_path()
    assert latest_run_path is not None
    event = json.loads(latest_run_path.read_text(encoding="utf-8").splitlines()[0])
    metadata = json.loads(workflow_run_metadata_path(event["run_id"]).read_text(encoding="utf-8"))

    assert event["run_scope"] == "top-level"
    assert event["details"]["run_scope"] == "top-level"
    assert event["details"]["project_root"] == str(project_root)
    assert event["details"]["effective_prompt"] == "use abs_abs_sub first"
    assert metadata["run_scope"] == "top-level"
    assert metadata["project_root"] == str(project_root)
    assert metadata["effective_prompt"] == "use abs_abs_sub first"


def test_background_event_does_not_replace_top_level_run_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-theorem-queue-worker")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-campaign")

    append_workflow_activity(
        "runner-start",
        "Managed workflow runner started",
        process_id=111,
        agent_session_id="foreground",
    )
    append_workflow_activity(
        "dispatch-job",
        "Background worker running",
        process_id=222,
        agent_session_id="worker",
        active_skill="background-research",
    )

    metadata = json.loads(workflow_run_metadata_path("prove-campaign").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in workflow_run_activity_path("prove-campaign")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["run_scope"] == "background-session"
    assert metadata["run_scope"] == "top-level"
    assert metadata["process_id"] == 111
    assert metadata["active_skill"] == "lean-theorem-queue-worker"


def test_owner_process_events_remain_top_level_after_runner_start(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-campaign")

    append_workflow_activity(
        "runner-start",
        "Managed workflow runner started",
        process_id=os.getpid(),
    )
    append_workflow_activity("queue-item-assigned", "Queue assigned theorem demo")

    events = [
        json.loads(line)
        for line in workflow_run_activity_path("prove-campaign")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["run_scope"] for event in events] == ["top-level", "top-level"]


def test_workflow_latest_run_activity_path_prefers_top_level_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-background-test")

    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="12345",
        process_id=24680,
    )
    background_run_id = json.loads(
        workflow_latest_run_activity_path(prefer_top_level=False)
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )["run_id"]

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-top-level-test")
    append_workflow_activity("runner-start", "Managed workflow runner started")

    latest_top_level = workflow_latest_run_activity_path()
    assert latest_top_level is not None
    latest_event = json.loads(latest_top_level.read_text(encoding="utf-8").splitlines()[0])

    assert latest_event["type"] == "runner-start"
    assert latest_event["run_id"] != background_run_id
    assert latest_event["run_scope"] == "top-level"


def test_workflow_agent_summary_groups_events(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=12345,
        model="google/gemma-3-27b-it",
        provider="custom",
        delegate_depth=0,
        user_message="Prove theorem t",
    )
    append_workflow_activity(
        "api-request",
        "API call #1",
        agent_session_id="agent-main",
        iteration=1,
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="agent-main",
        content="I will inspect diagnostics first.",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        completed=True,
        api_calls=1,
    )

    summaries = summarize_workflow_agents(activity_limit=3)

    assert len(summaries) == 1
    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "completed"
    assert summaries[0]["api_calls"] == 1
    assert summaries[0]["model"] == "google/gemma-3-27b-it"
    assert summaries[0]["process_id"] == 12345
    assert summaries[0]["task_label"] == "agent"

    detail = workflow_agent_detail("agent-main", activity_limit=2)
    assert detail["agent_id"] == "agent-main"
    assert len(detail["recent_activity"]) == 2


def test_workflow_activity_status_readers_stream_jsonl(monkeypatch, tmp_path):
    """Status views must not materialize complete historical JSONL files."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="streamed-agent",
        process_id=12345,
    )
    append_workflow_activity(
        "api-request",
        "API call #1",
        agent_session_id="streamed-agent",
        iteration=1,
        message_count=2,
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="streamed-agent",
        content="bounded result",
    )

    original_read_text = Path.read_text

    def reject_jsonl_materialization(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            raise AssertionError(f"materialized activity stream: {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_jsonl_materialization)

    summaries = summarize_workflow_agents(activity_limit=2)
    recent = read_workflow_activity(limit=2, agent_id="streamed-agent")

    assert summaries[0]["agent_id"] == "streamed-agent"
    assert summaries[0]["api_calls"] == 1
    assert [event["type"] for event in recent] == ["api-request", "assistant-response"]


def test_workflow_agent_summary_reads_each_run_metadata_once(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "shared-run")
    for iteration in range(4):
        append_workflow_activity(
            "api-request",
            f"API call #{iteration + 1}",
            agent_session_id="shared-agent",
            iteration=iteration + 1,
        )

    from leanflow_cli.workflows import workflow_state

    original = workflow_state._read_workflow_run_metadata
    calls = 0

    def counted(run_id):
        nonlocal calls
        calls += 1
        return original(run_id)

    monkeypatch.setattr(workflow_state, "_read_workflow_run_metadata", counted)

    summaries = summarize_workflow_agents(activity_limit=1)

    assert summaries[0]["api_calls"] == 4
    assert calls == 1


def test_workflow_agent_summary_reuses_unchanged_history_index(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    for run_index in range(30):
        monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", f"run-{run_index:03d}")
        append_workflow_activity(
            "conversation-start",
            "Agent conversation started",
            agent_session_id=f"agent-{run_index:03d}",
            process_id=20_000 + run_index,
        )

    first = summarize_workflow_agents(activity_limit=2)
    first[0]["status"] = "caller-mutated"

    from leanflow_cli.workflows import workflow_state

    def forbid_rescan(_paths):
        raise AssertionError("unchanged activity history was rescanned")

    monkeypatch.setattr(workflow_state, "iter_jsonl_dicts", forbid_rescan)
    second = summarize_workflow_agents(activity_limit=2)

    assert len(second) == 30
    assert second[0]["status"] != "caller-mutated"


def test_workflow_agent_summary_invalidates_index_after_append(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "changing-run")
    append_workflow_activity(
        "api-request",
        "API call #1",
        agent_session_id="changing-agent",
        iteration=1,
    )
    assert summarize_workflow_agents(activity_limit=1)[0]["api_calls"] == 1

    append_workflow_activity(
        "api-request",
        "API call #2",
        agent_session_id="changing-agent",
        iteration=2,
    )

    assert summarize_workflow_agents(activity_limit=1)[0]["api_calls"] == 2


def test_workflow_agent_summary_uses_workflow_task_label(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        **_owned_process(24680),
        workflow_kind="prove",
        active_skill="lean-proof-loop",
    )

    summaries = summarize_workflow_agents(activity_limit=1)

    assert summaries[0]["task_label"] == "prove"


def test_workflow_agent_resolution_and_termination(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        **_owned_process(24680),
    )

    assert resolve_workflow_agent_id("123") == "12345"

    captured: dict[str, tuple[int, int]] = {}

    def _fake_killpg(pid: int, sig: int) -> None:
        captured["killpg"] = (pid, sig)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = terminate_workflow_agent("123")

    assert result["success"] is True
    assert result["agent_id"] == "12345"
    assert captured["killpg"][0] == 24680


def test_workflow_agent_descendant_termination(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="11111", **_owned_process(101)
    )
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="22222",
        parent_agent_session_id="11111",
        **_owned_process(202),
    )
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="33333",
        parent_agent_session_id="22222",
        **_owned_process(303),
    )

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = terminate_workflow_agent_descendants("11111")

    assert result["success"] is True
    assert result["count"] == 2
    assert set(result["terminated"]) == {"22222", "33333"}
    assert killed == [303, 202] or killed == [202, 303]


def test_workflow_agent_descendants_skip_same_process_logical_sessions(monkeypatch, tmp_path):
    """Nested model sessions in the native PID are not child processes."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    process_id = os.getpid()
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="30761", **_owned_process(process_id)
    )
    for child_id in ("80967", "89919", "90446"):
        append_workflow_activity(
            "conversation-start",
            "start",
            agent_session_id=child_id,
            parent_agent_session_id="30761",
            **_owned_process(process_id),
        )

    monkeypatch.setattr(
        workflow_state,
        "terminate_workflow_agent",
        lambda child_id: pytest.fail(f"same-process session {child_id} must not be signaled"),
    )

    result = terminate_workflow_agent_descendants("30761")

    assert result["success"] is True
    assert result["count"] == 0
    assert result["terminated"] == []
    assert result["failed"] == []
    assert set(result["skipped_same_process"]) == {"80967", "89919", "90446"}


def test_workflow_agent_descendants_reach_external_grandchild_through_logical_session(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    process_id = os.getpid()
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="root", **_owned_process(process_id)
    )
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="logical",
        parent_agent_session_id="root",
        **_owned_process(process_id),
    )
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="external",
        parent_agent_session_id="logical",
        **_owned_process(424242),
    )
    terminated: list[str] = []
    monkeypatch.setattr(
        workflow_state,
        "terminate_workflow_agent",
        lambda child_id: terminated.append(child_id)
        or {"success": True, "agent_id": child_id, "process_id": 424242},
    )

    result = terminate_workflow_agent_descendants("root")

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["external"]
    assert result["skipped_same_process"] == ["logical"]
    assert terminated == ["external"]


def test_workflow_agent_descendants_signal_shared_external_process_once(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="root", **_owned_process(os.getpid())
    )
    for child_id in ("external-a", "external-b"):
        append_workflow_activity(
            "conversation-start",
            "start",
            agent_session_id=child_id,
            parent_agent_session_id="root",
            **_owned_process(424242),
        )
    terminated: list[str] = []
    monkeypatch.setattr(
        workflow_state,
        "terminate_workflow_agent",
        lambda child_id: terminated.append(child_id)
        or {"success": True, "agent_id": child_id, "process_id": 424242},
    )

    result = terminate_workflow_agent_descendants("root")

    assert result["success"] is True
    assert result["count"] == 1
    assert len(result["coalesced_same_process"]) == 1
    assert len(terminated) == 1


def test_terminate_all_workflow_agents_excludes_current(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="11111", **_owned_process(101)
    )
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="22222", **_owned_process(202)
    )
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="33333", **_owned_process(303)
    )

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = terminate_all_workflow_agents(exclude_agent_id="22222", exclude_process_id=303)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_all_workflow_agents_skips_dead_and_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="11111", **_owned_process(101)
    )
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="22222", **_owned_process(202)
    )
    append_workflow_activity(
        "conversation-end",
        "done",
        agent_session_id="22222",
        **_owned_process(202),
        completed=True,
    )
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="33333", **_owned_process(303)
    )

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: pid == 101
    )
    monkeypatch.setattr(
        workflow_state, "process_identity_matches", lambda identity: identity.pid == 101
    )

    result = terminate_all_workflow_agents()

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_project_workflow_agents_filters_by_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project_a = str(tmp_path / "A")
    project_b = str(tmp_path / "B")
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="11111",
        **_owned_process(101),
        project_root=project_a,
    )
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="22222",
        **_owned_process(202),
        project_root=project_b,
    )

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = terminate_project_workflow_agents(project_a)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_project_workflow_agents_skips_missing_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project_a = str(tmp_path / "A")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-run-a")
    append_workflow_activity(
        "conversation-start", "start", agent_session_id="11111", **_owned_process(101)
    )
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-run-b")
    append_workflow_activity(
        "conversation-start",
        "start",
        agent_session_id="22222",
        **_owned_process(202),
        project_root=project_a,
    )

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("leanflow_cli.workflows.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = terminate_project_workflow_agents(project_a)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["22222"]
    assert killed == [202]


def test_workflow_agent_transcript_collects_recent_interactions(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="54321",
        user_message="Prove theorem foo",
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="54321",
        content="I will inspect diagnostics first.",
    )
    append_workflow_activity(
        "tool-call",
        "Tool call: terminal",
        agent_session_id="54321",
        tool="terminal",
    )

    transcript = workflow_agent_transcript("54321", limit=5)

    assert transcript[0]["role"] == "user"
    assert transcript[0]["content"] == "Prove theorem foo"
    assert transcript[1]["role"] == "assistant"
    assert "inspect diagnostics" in transcript[1]["content"]
    assert transcript[2]["role"] == "tool-call"


def test_workflow_agent_transcript_uses_specific_tool_previews(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="55555",
        content="",
        tool_calls=[
            {
                "name": "patch",
                "arguments": '{"path":"./ProveDemo/ProveDemo/RealTheorems-homework.lean","mode":"replace"}',
            }
        ],
    )
    append_workflow_activity(
        "tool-result",
        "Tool result: patch",
        agent_session_id="55555",
        tool="patch",
        is_error=False,
        result='{"success":true,"files_modified":["./ProveDemo/ProveDemo/RealTheorems-homework.lean"],"diff":"@@ -1 +1 @@\\n-old\\n+new\\n"}',
    )

    transcript = workflow_agent_transcript("55555", limit=4)

    assert transcript[0]["role"] == "assistant"
    assert (
        "Edit ./ProveDemo/ProveDemo/RealTheorems-homework.lean (replace)"
        in transcript[0]["content"]
    )
    assert transcript[1]["role"] == "tool-result"
    assert "updated ./ProveDemo/ProveDemo/RealTheorems-homework.lean" in transcript[1]["content"]
    assert "1 hunk(s)" in transcript[1]["content"]


def test_workflow_agent_queue_and_waiting_state(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        **_owned_process(24680),
        model="zai-org/GLM-5.1",
    )
    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="12345",
        **_owned_process(24680),
        status="verified",
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: True)

    result = enqueue_workflow_agent_message("12345", "Try a different proof strategy.")

    assert result["success"] is True
    inbox = read_workflow_agent_inbox("12345")
    assert inbox[-1]["text"] == "Try a different proof strategy."

    summaries = summarize_workflow_agents(activity_limit=4)
    assert summaries[0]["status"] == "queued"

    transcript = workflow_agent_transcript("12345", limit=6)
    assert transcript[-1]["role"] == "user"
    assert "different proof strategy" in transcript[-1]["content"]


def test_enqueue_workflow_agent_message_rejects_dead_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        process_id=24680,
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )

    result = enqueue_workflow_agent_message("12345", "Try again")

    assert result["success"] is False
    assert result["error"] == "Agent process is no longer running."


def test_workflow_agent_summary_prefers_live_busy_phase_over_conversation_end(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        completed=True,
        api_calls=2,
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )

    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "active"
    assert summaries[0]["finished_at"] == ""


def test_workflow_agent_summary_maps_live_stalled_phase_to_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "stalled",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )

    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "blocked"


def test_background_workflow_conversation_end_is_not_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-background-test")

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        completed=True,
    )

    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["status"] == "active"
    assert summaries[0]["finished_at"] == ""


def test_workflow_agent_summary_marks_dead_processes_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        status="paused",
    )

    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "dead"
    assert summaries[0]["finished_at"] != ""


def test_workflow_agent_summary_does_not_override_dead_process_with_live_phase(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )

    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["status"] == "dead"


def test_load_workflow_live_status_marks_dead_runner_snapshot_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "paused",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "process_id": 24680,
            "current_queue_item": {"label": "demo"},
        }
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "dead"
    assert payload["process_id"] == 0
    assert payload["stale_process_id"] == 24680
    assert payload["stale_snapshot"] is True
    assert load_workflow_live_status()["phase"] == "dead"


def test_load_workflow_live_status_preserves_terminal_phase_for_dead_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "exited",
            "workflow_kind": "prove",
            "process_id": 24680,
            "held_locks": 1,
        }
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "exited"
    assert payload["process_id"] == 0
    assert payload["stale_process_id"] == 24680
    assert payload["stale_snapshot"] is True
    assert payload["held_locks"] == 0
    assert payload["stale_held_locks"] == 1


def test_load_workflow_live_status_clears_legacy_stale_lock_count(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "verified",
            "workflow_kind": "prove",
            "process_id": 0,
            "stale_process_id": 24680,
            "stale_snapshot": True,
            "held_locks": 2,
        }
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "verified"
    assert payload["held_locks"] == 0
    assert payload["stale_held_locks"] == 2


def test_load_workflow_live_status_preserves_failed_phase_for_dead_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "failed",
            "workflow_kind": "prove",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: False
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "failed"
    assert payload["process_id"] == 0
    assert payload["stale_process_id"] == 24680
    assert payload["stale_snapshot"] is True


def test_workflow_agent_summary_includes_multiple_run_streams(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-run-a")
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="11111",
        process_id=101,
        workflow_kind="prove",
        workflow_command="/prove Main.lean",
        project_root=str(tmp_path / "A"),
    )
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-run-b")
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="22222",
        process_id=202,
        workflow_kind="prove",
        workflow_command="/prove Other.lean",
        project_root=str(tmp_path / "B"),
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )

    summaries = summarize_workflow_agents(activity_limit=1)

    assert {summary["agent_id"] for summary in summaries} == {"11111", "22222"}
