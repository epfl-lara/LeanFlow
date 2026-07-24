"""Characterize fail-closed reuse of an exact parent helper verification."""

from __future__ import annotations

import hashlib

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.native import parent_helper_verification_reuse


def _sha256(text: str) -> str:
    """Return the exact UTF-8 source identity used by the test fixture."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ready_candidate(tmp_path, *, axioms: tuple[str, ...] = ("Classical.choice",)):
    """Stage one accepted parent helper check against the original source."""
    active = tmp_path / "Main.lean"
    before = "theorem demo : True := by\n  sorry\n"
    declaration = "private lemma checked_family : True := by\n  trivial"
    active.write_text(before, encoding="utf-8")
    declaration_hash = _sha256(declaration)
    finding = {
        "job_id": "campaign.em-checked",
        "target_symbol": "demo",
        "active_file": str(active),
        "deliverable": {
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "active_file": str(active),
                    "anchor_target_symbol": "demo",
                    "declaration": declaration,
                    "declaration_sha256": declaration_hash,
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["checked_family"],
                    },
                }
            ],
        },
    }
    state = {
        "campaign_id": "campaign",
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": before.strip(),
        },
    }
    pending = runner.research_helper_candidate_priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert pending is not None
    expected = parent_helper_verification_reuse.expected_integrated_source(
        before,
        pending,
    )
    assert expected
    ready = runner.research_helper_candidate_priority.mark_parent_recheck(
        state,
        candidate_id=pending.candidate_id,
        status="accepted",
        source_revision_sha256=_sha256(before),
        expected_integrated_source_revision_sha256=_sha256(expected),
        axiom_profile_axioms=axioms,
    )
    assert ready is not None and ready.ready
    return active, before, expected, declaration, state, ready


def test_exact_parent_helper_insertion_reuses_gate_and_records_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Bank an exact insertion without compiling the identical helper twice."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    active, before, expected, _declaration, state, ready = _ready_candidate(tmp_path)
    active.write_text(expected, encoding="utf-8")

    class Agent:
        def __init__(self) -> None:
            self._managed_autonomy_state = state
            self._managed_pending_theorem_feedback = None

    agent = Agent()
    events: list[tuple[tuple, dict]] = []
    graph_syncs: list[str] = []
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda *_args, **_kwargs: pytest.fail(
            "exact cached helper triggered a second Lean compile"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_maybe_sync_plan_state",
        lambda *_args, **_kwargs: graph_syncs.append("synced") or True,
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    monkeypatch.setattr(
        runner.decomposer,
        "prover_edit_evidence_helper_names",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        runner.conditional_helper_progress,
        "deferred_helper_names",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.finite_branch_progress,
        "deferred_helper_names",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.banked_helper_inspection,
        "remember",
        lambda *_args, **_kwargs: None,
    )

    result = runner._record_helper_only_edit_progress(
        agent,
        target_symbol="demo",
        active_file=str(active),
        helper_names=(ready.helper_name,),
        verification_tool="patch",
        edit_before_source_revision_sha256=_sha256(before),
    )

    assert result.verified_any is True
    assert result.proof_progress is True
    outcome = runner._queue_manager_from_state(state).outcome_for(
        runner._queue_key(ready.helper_name, str(active))
    )
    assert outcome is not None and outcome.status == "solved"
    assert outcome.verification is not None
    assert outcome.verification.axiom_profile_checked is True
    assert outcome.verification.axiom_profile_axioms == ("Classical.choice",)
    assert graph_syncs == ["synced"]
    reused = next(
        kwargs for args, kwargs in events if args[0] == "queue-helper-parent-verification-reused"
    )
    assert reused["candidate_id"] == ready.candidate_id
    assert reused["lean_started"] is False
    assert reused["axiom_profile_axioms"] == ["Classical.choice"]


@pytest.mark.parametrize(
    ("mutation", "before_revision", "reason"),
    [
        ("exact", "stale", "pre_edit_source_changed"),
        ("reordered", "current", "integrated_source_changed"),
        ("extra", "current", "integrated_source_changed"),
    ],
)
def test_parent_helper_reuse_rejects_stale_reordered_or_extra_edits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    mutation: str,
    before_revision: str,
    reason: str,
) -> None:
    """Fall back whenever the managed edit is not the authenticated insertion image."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    active, before, expected, declaration, _state, ready = _ready_candidate(tmp_path)
    if mutation == "reordered":
        active.write_text(before + "\n" + declaration + "\n", encoding="utf-8")
    elif mutation == "extra":
        active.write_text(expected + "\n-- unrelated concurrent edit\n", encoding="utf-8")
    else:
        active.write_text(expected, encoding="utf-8")
    supplied_before_revision = _sha256(before)
    if before_revision == "stale":
        supplied_before_revision = _sha256(before + "\n-- stale snapshot")

    decision = parent_helper_verification_reuse.classify_reuse(
        ready,
        target_symbol="demo",
        active_file=str(active),
        edit_before_source_revision_sha256=supplied_before_revision,
        allowed_axioms=runner._allowed_axioms(),
    )

    assert decision.reusable is False
    assert decision.reason == reason
    assert decision.manager_check == {}
