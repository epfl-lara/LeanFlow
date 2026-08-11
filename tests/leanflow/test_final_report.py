"""Phase 3 §6 tests: the never-silent end-of-scope final report."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import final_report as fr
from leanflow_cli.workflows import negation_promotion, plan_state


@pytest.fixture()
def state_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    return tmp_path / ".leanflow" / "workflow-state"


def _autonomy_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {"target_symbol": "hard", "active_file": "Demo.lean"},
        "theorem_outcomes": {
            "Demo.lean::demo": {"target_symbol": "demo", "status": "solved"},
            "Demo.lean::hard": {"target_symbol": "hard", "status": "blocked"},
        },
        "failed_attempts": [
            {
                "attempt": 1,
                "target_symbol": "hard",
                "active_file": "Demo.lean",
                "reason": "type mismatch",
            }
        ],
    }


def test_generate_final_report_sections_and_mirror(state_root):
    state_root.mkdir(parents=True)
    (state_root / "summary.json").write_text(
        json.dumps(
            {
                "dispatch_ledger": [
                    {"spec": {"job_id": "run.planner.np-001"}, "state": "running", "notes": ""}
                ],
                "negation_probes": [
                    {
                        "theorem": "hard",
                        "plausible": {"counterexample_text": "n := 7"},
                        "negation": {"verdict": "inconclusive"},
                    }
                ],
                "decision_packets": [{"packet_id": "bp-1", "decision": None}],
            }
        ),
        encoding="utf-8",
    )

    path = fr.generate_final_report(
        stop_reason="budget-breakpoint",
        autonomy_state=_autonomy_state(),
        live_state={},
        run_id="prove-t1",
    )

    text = path.read_text(encoding="utf-8")
    for heading in (
        "## Theorem ledger",
        "## What was tried",
        "## What was learned",
        "## Open jobs",
        "## Recommended next actions",
    ):
        assert heading in text
    assert "`demo` | solved" in text
    assert "`hard` | blocked" in text
    # Open jobs are LOUD (the never-silently-lost audit).
    assert "**OPEN** run.planner.np-001 [running]" in text
    assert "counterexample for `hard`: n := 7" in text
    assert "undecided decision packet" in text

    summary = json.loads((state_root / "summary.json").read_text(encoding="utf-8"))
    mirror = summary["final_report"]
    assert mirror["stop_reason"] == "budget-breakpoint"
    assert mirror["outcome_kind"] == "report"
    assert mirror["status"] == "documented"
    assert mirror["theorem_counts"] == {"proved": 1, "blocked": 1, "unresolved": 0}
    assert mirror["open_jobs"][0]["job_id"] == "run.planner.np-001"


def _revalidated_disproof_state(state_root):
    """Build a sealed root plus the exact payload produced by native revalidation."""
    from leanflow_cli.workflows.queue_models import TheoremKey

    source = state_root.parent.parent / "Demo.lean"
    key = TheoremKey.make("hard", "Demo.lean").storage_key()
    node_id = plan_state.node_id_for("hard", "Demo.lean")
    root = negation_promotion._seal_campaign_root_entry(
        {
            "campaign_id": "campaign-test",
            "theorem": "hard",
            "operation_path": str(source),
            "node_id": node_id,
            "graph_node_name": "hard",
            "graph_node_file": "Demo.lean",
            "declaration_signature_sha256": "1" * 64,
            "initial_source_revision_sha256": "2" * 64,
        }
    )
    summary = {
        "campaign": {
            "campaign_id": "campaign-test",
            "provider_turn_nonce": 1,
            negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD: False,
            negation_promotion._CAMPAIGN_ROOTS_FIELD: {
                "version": 1,
                "campaign_id": "campaign-test",
                "roots": [root],
                "registry_sha256": negation_promotion._campaign_root_registry_sha256([root]),
            },
        }
    }
    graph_payload = {
        "theorem": "hard",
        "operation_path": str(source),
        "node_id": node_id,
        "graph_node_name": "hard",
        "graph_node_file": "Demo.lean",
        "is_main_goal": True,
    }
    evidence = {
        "key": key,
        **graph_payload,
        "file": str(source),
        "canonical_file": str(source),
        "source_revision_sha256": "2" * 64,
        "declaration_signature_sha256": "1" * 64,
        "negation_name": "not_hard",
        "negation_prop": "¬ True",
        "proof_tactic": "decide",
        "promotion_kind": "scratch_negation",
        "axioms": [],
        "promoted_at": "2026-07-17T00:00:00+00:00",
        "classification_basis": "requested_scope_manifest",
        "scope_root_campaign_id": "campaign-test",
        "scope_root_identity_sha256": root["root_identity_sha256"],
        "scope_root_theorem": "hard",
        "scope_root_file": "Demo.lean",
        "scope_root_node_id": node_id,
        "graph_before_statuses": {node_id: "proving"},
        "graph_after_statuses": {node_id: "false"},
        "graph_changed_node_identities": {node_id: {"name": "hard", "file": "Demo.lean"}},
        "graph_before_revision": 1,
        "graph_expected_revision": 2,
    }
    evidence["graph_identity_sha256"] = negation_promotion._graph_identity_sha256(graph_payload)
    evidence["classification_identity_sha256"] = negation_promotion._graph_identity_sha256(
        {
            **graph_payload,
            "classification_basis": "requested_scope_manifest",
            "scope_root_campaign_id": "campaign-test",
            "scope_root_identity_sha256": root["root_identity_sha256"],
            "scope_root_theorem": "hard",
            "scope_root_file": "Demo.lean",
            "scope_root_node_id": node_id,
        }
    )
    evidence = negation_promotion._canonicalize_promotion_record(
        negation_promotion._seal_rollback_plan(evidence),
        state_root.parent.parent,
    )
    summary["negation_promotions"] = [evidence]
    summary["negation_promotion_transactions"] = [
        {
            "transaction_id": evidence["promotion_id"],
            "state": "committed",
            "prepared_at": "2026-07-17T00:00:00+00:00",
            "committed_at": "2026-07-17T00:01:00+00:00",
            "promotion": evidence,
        }
    ]
    autonomy = {
        **_autonomy_state(),
        "terminal_outcome": "disproved",
        "negation_promotion": {
            "ok": True,
            "is_main_goal": True,
            "evidence": evidence,
        },
    }
    return autonomy, summary


def test_classify_disproved_rejects_raw_or_stale_promotion_rows(state_root):
    from leanflow_cli.workflows.queue_models import TheoremKey

    key = TheoremKey.make("hard", "Demo.lean").storage_key()
    summary = {
        "negation_probes": [
            {
                "key": key,
                "theorem": "hard",
                "negation": {"verdict": "negation_proved", "axioms_ok": True},
            }
        ]
    }
    assert fr.classify_scope_outcome(_autonomy_state(), {}, summary).kind == "report"

    promoted = {"negation_promotions": [{"key": key, "node_id": "n-hard", "is_main_goal": True}]}
    assert fr.classify_scope_outcome(_autonomy_state(), {}, promoted).kind == "report"
    sublemma = {"negation_promotions": [{"key": key, "node_id": "n-hard", "is_main_goal": False}]}
    assert fr.classify_scope_outcome(_autonomy_state(), {}, sublemma).kind == "report"
    # Same theorem NAME in a different file never classifies this scope
    # disproved (exact key match required).
    other_key = TheoremKey.make("hard", "Other.lean").storage_key()
    other = {
        "negation_promotions": [{"key": other_key, "node_id": "n-other", "is_main_goal": True}]
    }
    assert fr.classify_scope_outcome(_autonomy_state(), {}, other).kind == "report"


def test_classify_disproved_accepts_this_runs_revalidated_root_payload(state_root):
    autonomy, summary = _revalidated_disproof_state(state_root)

    outcome = fr.classify_scope_outcome(autonomy, {}, summary)

    assert outcome.kind == "disproved"
    assert "hard" in outcome.detail


def test_classify_disproved_rejects_reconciliation_ambiguity(state_root):
    autonomy, summary = _revalidated_disproof_state(state_root)
    autonomy["negation_promotion_pending"] = 1

    assert fr.classify_scope_outcome(autonomy, {}, summary).kind == "report"


@pytest.mark.parametrize("transaction_change", ["removed", "duplicated", "pending", "tampered"])
def test_classify_disproved_requires_one_exact_committed_transaction(
    state_root, transaction_change
):
    """Runtime evidence cannot bypass the durable promotion commit boundary."""
    autonomy, summary = _revalidated_disproof_state(state_root)
    transaction = json.loads(json.dumps(summary["negation_promotion_transactions"][0]))
    if transaction_change == "removed":
        summary["negation_promotion_transactions"] = []
    elif transaction_change == "duplicated":
        summary["negation_promotion_transactions"] = [transaction, dict(transaction)]
    elif transaction_change == "pending":
        transaction["state"] = "pending"
        transaction.pop("committed_at")
        summary["negation_promotion_transactions"] = [transaction]
    else:
        transaction["promotion"]["proof_tactic"] = "exact forged"
        summary["negation_promotion_transactions"] = [transaction]

    assert fr.classify_scope_outcome(autonomy, {}, summary).kind == "report"


@pytest.mark.parametrize("ledger_change", ["removed", "duplicated", "tampered", "replaced"])
def test_classify_disproved_requires_one_exact_current_promotion_row(state_root, ledger_change):
    """Cached runtime evidence cannot outlive or diverge from its durable row."""
    autonomy, summary = _revalidated_disproof_state(state_root)
    original = json.loads(json.dumps(summary["negation_promotions"][0]))
    if ledger_change == "removed":
        summary["negation_promotions"] = []
    elif ledger_change == "duplicated":
        summary["negation_promotions"] = [original, dict(original)]
    elif ledger_change == "tampered":
        original["proof_tactic"] = "exact forged"
        summary["negation_promotions"] = [original]
    else:
        original["promotion_id"] = "replacement-promotion"
        summary["negation_promotions"] = [original]

    assert fr.classify_scope_outcome(autonomy, {}, summary).kind == "report"


def test_pause_is_not_a_scope_end(state_root, monkeypatch):
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: pytest.fail("paused must not generate a report"),
    )
    runner._maybe_generate_final_report("paused", {}, {})


def test_prose_cannot_break_report_structure(state_root):
    state = _autonomy_state()
    state["failed_attempts"][0]["reason"] = "evil | pipes\nand ## headings"

    path = fr.generate_final_report(
        stop_reason="stalled", autonomy_state=state, live_state={}, run_id="t"
    )

    text = path.read_text(encoding="utf-8")
    assert "evil / pipes and ## headings" in text
    assert "evil | pipes" not in text


def test_runner_hook_gating_and_idempotency(state_root, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: calls.append(kwargs["stop_reason"]) or (state_root / "r.md"),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state: dict[str, Any] = {}

    # Verified and handoff exits are exempt.
    runner._maybe_generate_final_report("verified", state, {})
    runner._maybe_generate_final_report("formalization-prover-handoff-ready", state, {})
    assert calls == []

    runner._maybe_generate_final_report("stalled", state, {})
    assert calls == ["stalled"]
    assert state["final_report_written"] is True
    # Idempotent: ceiling + stall interplay writes once.
    runner._maybe_generate_final_report("blocked", state, {})
    assert calls == ["stalled"]

    # Opt-out flag.
    fresh: dict[str, Any] = {}
    monkeypatch.setenv("LEANFLOW_FINAL_REPORT", "0")
    runner._maybe_generate_final_report("stalled", fresh, {})
    assert calls == ["stalled"]


def test_generation_failure_never_crashes_the_stop(state_root, monkeypatch):
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state: dict[str, Any] = {}

    runner._maybe_generate_final_report("stalled", state, {})

    assert "final_report_written" not in state  # retryable on the next exit


# ---------------------------------------------------------------------------
# Phase 4 §4.12: graph inventory + route history + ranked open subgoals
# ---------------------------------------------------------------------------


@pytest.fixture()
def plan_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def test_report_renders_graph_route_and_subgoal_sections(state_root, plan_dir):
    from leanflow_cli.workflows import plan_state

    file = "Demo.lean"

    def node(name: str, status: str, notes: str = "") -> plan_state.GraphNode:
        return plan_state.GraphNode(
            id=plan_state.node_id_for(name, file), name=name, file=file, status=status, notes=notes
        )

    ready, done = node("ready_one", "stated"), node("done_one", "proved")
    waiting, dep = node("waiting_one", "audited"), node("dep_one", "blocked")
    parked = node("parked_one", "parked", notes="awaiting human")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(ready, done, waiting, dep, parked, node("refuted_one", "false")),
            edges=(plan_state.GraphEdge(source=waiting.id, target=dep.id, kind="depends_on"),),
        )
    )
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "trigger": "stall",
            "route": "decompose",
            "reason": "search exhausted | on `hard`",
            "source": "floor",
            "name": "hard",
        }
    )
    plan_state.append_journal_event({"event": "node-status", "id": ready.id})  # must not render

    text = fr.generate_final_report(
        stop_reason="stalled", autonomy_state=_autonomy_state(), live_state={}, run_id="prove-t2"
    ).read_text(encoding="utf-8")

    assert "## Graph inventory" in text
    assert "- proved: `done_one` (Demo.lean)" in text
    assert "- false: `refuted_one` (Demo.lean)" in text
    assert "- parked: `parked_one` (Demo.lean)" in text

    assert "## Route history" in text
    assert "[stall] decompose (floor): search exhausted / on `hard`" in text
    assert "node-status" not in text

    assert "## Open subgoals (ranked)" in text
    ready_pos = text.index("READY `ready_one`")
    waiting_pos = text.index("waiting `waiting_one` [audited]")
    parked_pos = text.index("parked `parked_one` (Demo.lean) — awaiting human")
    assert ready_pos < waiting_pos < parked_pos
    assert "blocked `dep_one`" in text
    assert "`done_one`" not in text[text.index("## Open subgoals") :]


def test_report_sections_absent_when_plan_state_off(state_root, monkeypatch):
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)

    text = fr.generate_final_report(
        stop_reason="stalled", autonomy_state=_autonomy_state(), live_state={}, run_id="prove-t3"
    ).read_text(encoding="utf-8")

    for heading in ("## Graph inventory", "## Route history", "## Open subgoals (ranked)"):
        assert heading not in text
