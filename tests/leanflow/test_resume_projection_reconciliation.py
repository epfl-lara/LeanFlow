"""Provider-free resume projection reconciliation tests."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from leanflow_cli.lean import lean_incremental
from leanflow_cli.workflows import (
    campaign_epoch,
    dispatch_service,
    plan_state,
    resume_projection_reconciliation,
    workflow_state,
)
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode
from leanflow_cli.workflows.workflow_json_io import update_json_file
from leanflow_cli.workflows.workflow_state import (
    load_verified_patch_status,
    save_verified_patch_status,
)


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "resume-projection-test")
    return tmp_path


def _node(
    name: str,
    file: Path,
    *,
    status: str,
    source_sha256: str = "",
) -> GraphNode:
    return GraphNode(
        id=plan_state.node_id_for(name, str(file)),
        kind="lemma",
        name=name,
        file=str(file),
        status=status,
        source_sha256=source_sha256,
    )


def _source_sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _exact_outcome(file: Path, theorem_id: str) -> dict:
    return {
        "target_symbol": theorem_id,
        "active_file": str(file),
        "status": "solved",
        "last_verification": {
            "scope": f"target:{theorem_id}",
            "target": theorem_id,
            "tool": "lean_incremental_check",
            "ok": True,
            "errors": 0,
            "sorry": 0,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": [],
            "axiom_profile_blockers": [],
        },
    }


def _seed_patch_status(file: Path, theorem_id: str = "patched") -> None:
    save_verified_patch_status(
        {
            "checkpoint_id": "vpatch-resume",
            "status": "patch_elaborated",
            "path": str(file),
            "cwd": str(file.parent),
            "theorem_id": theorem_id,
            "patch_applied": True,
            "check_passed": True,
            "target_verified": False,
            "verified": False,
            "message": "exact target gate is still required",
        }
    )


def test_provider_free_resume_prunes_only_ineligible_conditional_helpers(
    enabled,
    monkeypatch,
):
    active = enabled / "Main.lean"
    active.write_text(
        "lemma live_bridge (h : ∀ n : ℕ, Witness n) (k : ℕ) : Witness k := by\n"
        "  exact h k\n\n"
        "theorem residual (k : ℕ) : Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    parent = _node("residual", active, status="proving")
    live = _node("live_bridge", active, status="proved")
    invalidated = _node("invalidated_bridge", active, status="false")
    blueprint = Blueprint(
        nodes=(parent, live, invalidated),
        edges=(
            GraphEdge(live.id, parent.id, "split_of"),
            GraphEdge(parent.id, live.id, "depends_on"),
        ),
    )
    plan_state.save_blueprint(blueprint)
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "residual",
                "active_file": str(active),
            }
        }
    )
    state: dict = {}
    campaign_epoch.ensure_campaign(state)

    def seed(summary):
        campaign = dict(summary["campaign"])
        campaign["conditional_helper_progress"] = {
            "version": 1,
            "deferred_node_ids": [live.id, invalidated.id, "removed-node"],
        }
        campaign["epoch_routes"] = [
            {
                "route": "direct-prove",
                "decided_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        summary["campaign"] = campaign

    update_json_file(plan_state.plan_state_paths().summary_json, seed)
    monkeypatch.setattr(
        campaign_epoch,
        "read_workflow_activity",
        lambda *args, **kwargs: pytest.fail("provider-free repair scanned activity"),
    )

    result = resume_projection_reconciliation.reconcile_provider_free_resume_projections(state)

    campaign = campaign_epoch.campaign_snapshot()
    assert result.conditional_deferred_node_ids == (live.id,)
    assert set(result.conditional_released_node_ids) == {invalidated.id, "removed-node"}
    assert campaign["conditional_helper_progress"]["deferred_node_ids"] == [live.id]
    rendered = plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    assert "current deterministic assignment: `residual`" in rendered
    assert "current assignment: `residual`" in rendered


def test_provider_free_resume_does_not_rewrite_unchanged_conditional_policy(
    enabled,
    monkeypatch,
):
    active = enabled / "Main.lean"
    active.write_text(
        "lemma live_bridge (h : ∀ n : ℕ, Witness n) (k : ℕ) : Witness k := by\n"
        "  exact h k\n\n"
        "theorem residual (k : ℕ) : Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    parent = _node("residual", active, status="proving")
    live = _node("live_bridge", active, status="proved")
    blueprint = Blueprint(
        nodes=(parent, live),
        edges=(
            GraphEdge(live.id, parent.id, "split_of"),
            GraphEdge(parent.id, live.id, "depends_on"),
        ),
    )
    plan_state.save_blueprint(blueprint)
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    campaign_epoch.reconcile_conditional_helper_progress(
        state,
        deferred_node_ids=(live.id,),
    )
    summary_path = plan_state.plan_state_paths().summary_json
    summary_before = summary_path.read_bytes()
    monkeypatch.setattr(
        campaign_epoch,
        "reconcile_conditional_helper_progress",
        lambda *args, **kwargs: pytest.fail("unchanged policy entered summary transaction"),
    )
    monkeypatch.setattr(
        dispatch_service,
        "DispatchService",
        lambda *args, **kwargs: pytest.fail("provider-free projection opened dispatch state"),
    )
    monkeypatch.setattr(
        workflow_state,
        "read_workflow_activity",
        lambda *args, **kwargs: pytest.fail("provider-free projection scanned activity"),
    )
    monkeypatch.setattr(
        lean_incremental,
        "lean_incremental_check",
        lambda *args, **kwargs: pytest.fail("provider-free projection invoked Lean"),
    )

    result = resume_projection_reconciliation.reconcile_provider_free_resume_projections(state)

    assert result.conditional_deferred_node_ids == (live.id,)
    assert result.conditional_released_node_ids == ()
    assert summary_path.read_bytes() == summary_before


def test_exact_outcome_promotes_latest_matching_verified_patch(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status(
        blueprint=Blueprint(),
        summary={},
        exact_outcome=_exact_outcome(active, "patched"),
    )

    status = load_verified_patch_status()
    assert promoted is True
    assert status["status"] == "verified"
    assert status["target_verified"] is True
    assert status["verified"] is True
    assert status["target_verification_source"] == "exact_theorem_outcome"
    assert status["target_verification_scope"] == "target:patched"


def test_resume_promotes_patch_from_matching_durable_graph_and_outcome(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    proved = _node(
        "patched",
        active,
        status="proved",
        source_sha256=_source_sha256(active),
    )
    blueprint = plan_state.save_blueprint(Blueprint(nodes=(proved,)))
    outcome = _exact_outcome(active, "patched")
    plan_state.save_queue_manager_state({"theorem_outcomes": {f"{active}::patched": outcome}})

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status(
        blueprint=blueprint,
        summary=plan_state.load_summary(),
    )

    status = load_verified_patch_status()
    assert promoted is True
    assert status["verified"] is True
    assert status["target_verification_source"] == "durable_exact_theorem_outcome"
    assert status["target_graph_node_id"] == proved.id


def test_resume_promotes_patch_from_gate_owned_graph_without_outcome(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    proved = _node(
        "patched",
        active,
        status="proved",
        source_sha256=_source_sha256(active),
    )

    plan_state.save_blueprint(Blueprint(nodes=(proved,)))

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status()

    status = load_verified_patch_status()
    assert promoted is True
    assert status["verified"] is True
    assert status["target_verification_source"] == "durable_graph_proof"
    assert status["target_graph_node_id"] == proved.id


def test_resume_rejects_stale_graph_source_revision(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    stale = _node("patched", active, status="proved", source_sha256="0" * 64)

    plan_state.save_blueprint(Blueprint(nodes=(stale,)))

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status()

    assert promoted is False
    assert load_verified_patch_status()["verified"] is False


def test_rejected_matching_outcome_blocks_stale_graph_patch_promotion(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    proved = _node(
        "patched",
        active,
        status="proved",
        source_sha256=_source_sha256(active),
    )
    rejected = _exact_outcome(active, "patched")
    rejected["status"] = "unverified"
    plan_state.save_blueprint(Blueprint(nodes=(proved,)))
    plan_state.save_queue_manager_state({"theorem_outcomes": {f"{active}::patched": rejected}})

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status()

    assert promoted is False
    assert load_verified_patch_status()["verified"] is False


def test_namespace_suffix_is_not_an_exact_patch_target(enabled):
    active = enabled / "Main.lean"
    active.write_text(
        "theorem A.B.foo : True := by\n  trivial\n\n" "theorem B.foo : True := by\n  trivial\n",
        encoding="utf-8",
    )
    _seed_patch_status(active, theorem_id="A.B.foo")

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status(
        exact_outcome=_exact_outcome(active, "B.foo"),
    )

    assert promoted is False
    assert load_verified_patch_status()["verified"] is False


def test_explicit_root_prefix_is_the_only_supported_symbol_alias(enabled):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active, theorem_id="patched")

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status(
        exact_outcome=_exact_outcome(active, "_root_.patched"),
    )

    assert promoted is True
    assert load_verified_patch_status()["verified"] is True


def test_source_change_during_graph_promotion_keeps_patch_unverified(
    enabled,
    monkeypatch,
):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    proved = _node(
        "patched",
        active,
        status="proved",
        source_sha256=_source_sha256(active),
    )
    plan_state.save_blueprint(Blueprint(nodes=(proved,)))
    original_update = update_json_file

    def change_source_before_status_commit(path, mutate):
        active.write_text("theorem patched : True := by\n  sorry\n", encoding="utf-8")
        return original_update(path, mutate)

    monkeypatch.setattr(
        resume_projection_reconciliation,
        "update_json_file",
        change_source_before_status_commit,
    )

    promoted = resume_projection_reconciliation.reconcile_verified_patch_status()

    assert promoted is False
    assert load_verified_patch_status()["verified"] is False
    assert "sorry" in active.read_text(encoding="utf-8")


def test_new_patch_status_waits_for_old_reconciliation_and_wins(enabled, monkeypatch):
    active = enabled / "Main.lean"
    active.write_text("theorem patched : True := by\n  trivial\n", encoding="utf-8")
    _seed_patch_status(active)
    original_update = update_json_file
    reconciliation_entered = threading.Event()
    allow_reconciliation = threading.Event()
    errors: list[BaseException] = []

    def pause_inside_status_transaction(path, mutate):
        def wrapped(payload):
            outcome = mutate(payload)
            reconciliation_entered.set()
            if not allow_reconciliation.wait(timeout=5):
                raise TimeoutError("test did not release patch reconciliation")
            return outcome

        return original_update(path, wrapped)

    monkeypatch.setattr(
        resume_projection_reconciliation,
        "update_json_file",
        pause_inside_status_transaction,
    )

    def reconcile_old() -> None:
        try:
            resume_projection_reconciliation.reconcile_verified_patch_status(
                exact_outcome=_exact_outcome(active, "patched"),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def save_new() -> None:
        try:
            save_verified_patch_status(
                {
                    "checkpoint_id": "newer-checkpoint",
                    "status": "patch_elaborated",
                    "path": str(active),
                    "cwd": str(active.parent),
                    "theorem_id": "newer_target",
                    "patch_applied": True,
                    "check_passed": True,
                    "target_verified": False,
                    "verified": False,
                }
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    reconcile_thread = threading.Thread(target=reconcile_old)
    reconcile_thread.start()
    assert reconciliation_entered.wait(timeout=5)
    writer_thread = threading.Thread(target=save_new)
    writer_thread.start()
    writer_thread.join(timeout=0.05)
    assert writer_thread.is_alive(), "new status writer bypassed reconciliation lock"
    allow_reconciliation.set()
    reconcile_thread.join(timeout=5)
    writer_thread.join(timeout=5)

    assert not reconcile_thread.is_alive()
    assert not writer_thread.is_alive()
    assert errors == []
    status = load_verified_patch_status()
    assert status["checkpoint_id"] == "newer-checkpoint"
    assert status["theorem_id"] == "newer_target"
    assert status["verified"] is False
