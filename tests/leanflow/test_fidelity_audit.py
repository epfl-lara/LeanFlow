"""Phase 4 (4/6) tests: the statement-fidelity audit (roadmap §4.11)."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state


class _Result:
    def __init__(self, response: str):
        self.response = response


@pytest.fixture()
def audit_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_FIDELITY_AUDIT", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "prove the abs inequality")


def _wire(monkeypatch, decision: str, response: str = "verdict text"):
    calls: list[dict[str, Any]] = []

    def fake_review(**kwargs):
        calls.append(kwargs)
        return _Result(response)

    monkeypatch.setattr(runner, "run_model_verification_review", fake_review)
    monkeypatch.setattr(runner, "_verification_review_decision", lambda result: decision)
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    return calls, events


def _autonomy_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem demo : True := by\n  sorry",
        }
    }


def test_flag_off_never_calls_the_reviewer(monkeypatch):
    monkeypatch.delenv("LEANFLOW_FIDELITY_AUDIT", raising=False)
    calls, events = _wire(monkeypatch, "PASS")

    assert runner._maybe_statement_fidelity_audit(_autonomy_state(), {}) == ""
    assert calls == []
    assert events == []


def test_pass_verdict_marks_node_audited(audit_enabled, monkeypatch):
    calls, events = _wire(monkeypatch, "PASS")
    # A stated node for the assignment already exists in the graph.
    node_id = plan_state.node_id_for("demo", "Demo/Main.lean")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id, name="demo", file="Demo/Main.lean", status="stated"
                ),
            )
        )
    )
    autonomy_state = _autonomy_state()

    verdict = runner._maybe_statement_fidelity_audit(autonomy_state, {})

    assert verdict == "pass"
    assert calls[0]["task"] == "statement_fidelity"
    assert "prove the abs inequality" in calls[0]["prompt"]
    assert "theorem demo : True" in calls[0]["prompt"]
    assert "sorry" not in calls[0]["prompt"]
    assert "(a / n : ℚ)" in calls[0]["prompt"]
    assert "mathematical difficulty is not a fidelity defect" in calls[0]["prompt"]
    node = plan_state.load_blueprint().node_by_id(node_id)
    assert node.status == "audited"
    assert "fidelity: audited" in node.notes
    assert any(args[0] == "statement-fidelity-audit" for args, _k in events)


def test_block_verdict_records_suspect_without_touching_status(audit_enabled, monkeypatch):
    _wire(monkeypatch, "BLOCK", response="BLOCK\nquantifier order is inverted")
    node_id = plan_state.node_id_for("demo", "Demo/Main.lean")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id, name="demo", file="Demo/Main.lean", status="proving"
                ),
            )
        )
    )
    autonomy_state = _autonomy_state()

    verdict = runner._maybe_statement_fidelity_audit(autonomy_state, {})

    assert verdict == "suspect"
    node = plan_state.load_blueprint().node_by_id(node_id)
    assert node.status == "proving"  # advisory: no status change on suspect
    assert "fidelity: suspect" in node.notes


def test_audit_runs_once_per_statement_and_reaudits_on_restate(audit_enabled, monkeypatch):
    calls, _events = _wire(monkeypatch, "PASS")
    autonomy_state = _autonomy_state()

    first = runner._maybe_statement_fidelity_audit(autonomy_state, {})
    second = runner._maybe_statement_fidelity_audit(autonomy_state, {})
    assert (first, second) == ("pass", "pass")
    assert len(calls) == 1  # cached per (theorem, statement)

    # Editing only the proof body does not change statement fidelity.
    autonomy_state["current_queue_assignment"][
        "slice"
    ] = "theorem demo : True := by\n  exact True.intro"
    assert runner._maybe_statement_fidelity_audit(autonomy_state, {}) == "pass"
    assert len(calls) == 1

    # A re-state changes the statement hash: the audit must re-run.
    autonomy_state["current_queue_assignment"]["slice"] = "theorem demo : 1 = 1 := by\n  sorry"
    third = runner._maybe_statement_fidelity_audit(autonomy_state, {})
    assert third == "pass"
    assert len(calls) == 2


def test_prompt_version_reaudits_legacy_cached_verdict(audit_enabled, monkeypatch):
    calls, _events = _wire(monkeypatch, "PASS")
    autonomy_state = _autonomy_state()
    statement = runner._statement_signature_text(
        autonomy_state["current_queue_assignment"]["slice"]
    )
    statement_hash = runner.hashlib.sha1(statement.encode("utf-8")).hexdigest()[:12]
    node_id = plan_state.node_id_for("demo", "Demo/Main.lean")
    autonomy_state["fidelity_audits_seen"] = {f"{node_id}::{statement_hash}": "suspect"}

    assert runner._maybe_statement_fidelity_audit(autonomy_state, {}) == "pass"
    assert len(calls) == 1


def test_bare_prove_file_goal_treats_existing_lean_statement_as_authority(
    audit_enabled, monkeypatch
):
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "/prove Demo/Main.lean")
    calls, events = _wire(monkeypatch, "BLOCK", response="BLOCK\ninvented objection")
    autonomy_state = _autonomy_state()

    verdict = runner._maybe_statement_fidelity_audit(autonomy_state, {})

    assert verdict == "pass"
    assert calls == []
    assert any(
        args[0] == "statement-fidelity-audit"
        and kwargs.get("verdict") == "pass"
        and "authoritative statement" in kwargs.get("detail", "")
        for args, kwargs in events
    )


def test_operational_proving_goal_treats_existing_statement_as_authority(
    audit_enabled, monkeypatch
):
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
        (
            "Complete the assigned theorem and continue until the problem is fully "
            "verified. Use LeanProbe for every concrete candidate, preserve verified "
            "facts in the living plan and graph, and research materially distinct routes."
        ),
    )
    calls, events = _wire(monkeypatch, "BLOCK", response="BLOCK\nworkflow text differs")

    verdict = runner._maybe_statement_fidelity_audit(_autonomy_state(), {})

    assert verdict == "pass"
    assert calls == []
    assert any(
        args[0] == "statement-fidelity-audit"
        and kwargs.get("verdict") == "pass"
        and "authoritative statement" in kwargs.get("detail", "")
        for args, kwargs in events
    )


def test_campaign_policy_with_every_assigned_declaration_skips_fidelity_reviewer(
    audit_enabled, monkeypatch
):
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
        (
            "Complete every assigned declaration in IMO2026/P2.lean with a "
            "kernel-verified, sorry-free proof. Use LeanProbe as the primary "
            "inner loop and preserve all logs and workflow artifacts."
        ),
    )
    calls, events = _wire(monkeypatch, "BLOCK", response="BLOCK\nworkflow text differs")

    verdict = runner._maybe_statement_fidelity_audit(_autonomy_state(), {})

    assert verdict == "pass"
    assert calls == []
    assert any(
        args[0] == "statement-fidelity-audit"
        and kwargs.get("verdict") == "pass"
        and "authoritative statement" in kwargs.get("detail", "")
        for args, kwargs in events
    )


def test_resume_campaign_policy_skips_fidelity_reviewer(audit_enabled, monkeypatch):
    """Contextual restart prose is execution policy, not an informal theorem."""
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
        (
            "Resume IMO 2026 Problem 2 from the preserved LeanFlow proof, plan, graph, "
            "and queue checkpoint. Complete the restored result without sorry or admit. "
            "Use gpt-5.6-luna at xhigh reasoning for every model role. The source is "
            "sorry-free but timed out under cold verification: use LeanProbe incremental "
            "checks, preserve logs and dead ends, and only stop for a genuine workflow blocker."
        ),
    )
    calls, events = _wire(monkeypatch, "BLOCK", response="BLOCK\nworkflow text differs")

    verdict = runner._maybe_statement_fidelity_audit(_autonomy_state(), {})

    assert verdict == "pass"
    assert calls == []
    assert any(
        args[0] == "statement-fidelity-audit"
        and kwargs.get("verdict") == "pass"
        and "authoritative statement" in kwargs.get("detail", "")
        for args, kwargs in events
    )


def test_campaign_policy_reopens_stale_fidelity_park():
    file = "Demo/Main.lean"
    node_id = plan_state.node_id_for("demo", file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(
                id=node_id,
                name="demo",
                file=file,
                status="parked",
                notes="human note; fidelity: suspect",
            ),
        )
    )
    truth = {(file, "demo"): plan_state.DeclTruth(present=True, has_sorry=True)}

    updated, reopened = runner._reopen_policy_only_fidelity_parks(
        blueprint,
        truth,
        "Complete every assigned declaration in Demo/Main.lean and preserve logs.",
    )

    node = updated.node_by_id(node_id)
    assert reopened == (("demo", file),)
    assert node is not None and node.status == "audited"
    assert "fidelity: audited" in node.notes
    assert "fidelity: suspect" not in node.notes
    assert "human note" in node.notes


def test_external_claim_keeps_fidelity_park_closed():
    file = "Demo/Main.lean"
    node_id = plan_state.node_id_for("demo", file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(
                id=node_id,
                name="demo",
                file=file,
                status="parked",
                notes="fidelity: suspect",
            ),
        )
    )
    truth = {(file, "demo"): plan_state.DeclTruth(present=True, has_sorry=True)}

    updated, reopened = runner._reopen_policy_only_fidelity_parks(
        blueprint,
        truth,
        "Prove the assigned theorem. Claim: every positive integer is even.",
    )

    assert reopened == ()
    assert updated.node_by_id(node_id).status == "parked"


def test_operational_prefix_with_explicit_claim_still_runs_fidelity_audit(
    audit_enabled, monkeypatch
):
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
        "Prove the assigned theorem. Claim: every positive integer is even.",
    )
    calls, _events = _wire(monkeypatch, "PASS")

    assert runner._maybe_statement_fidelity_audit(_autonomy_state(), {}) == "pass"
    assert len(calls) == 1


def test_real_parser_chain_handles_the_review_dataclass(audit_enabled, monkeypatch):
    """No monkeypatched parsers: the raw review result must flow through the
    real payload converter + decision parser (a dataclass passed straight to
    the mapping-based parser would silently disable the audit)."""

    class _RealShape:
        status = "ok"
        mode = "model"
        response = "PASS\nThe statement matches the informal claim."

    monkeypatch.setattr(runner, "run_model_verification_review", lambda **kwargs: _RealShape())
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    verdict = runner._maybe_statement_fidelity_audit(_autonomy_state(), {})

    assert verdict == "pass"
    assert any(args[0] == "statement-fidelity-audit" for args, _k in events)


def test_notes_deduplicate_fidelity_marker_across_restates(audit_enabled, monkeypatch):
    _wire(monkeypatch, "PASS")
    node_id = plan_state.node_id_for("demo", "Demo/Main.lean")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file="Demo/Main.lean",
                    status="stated",
                    notes="fidelity: suspect; human note",
                ),
            )
        )
    )

    runner._maybe_statement_fidelity_audit(_autonomy_state(), {})

    node = plan_state.load_blueprint().node_by_id(node_id)
    assert node.notes.count("fidelity:") == 1
    assert "fidelity: audited" in node.notes
    assert "human note" in node.notes
    # Audited nodes stay on the frontier.
    assert node.status == "audited"
    assert any(n.name == "demo" for n in plan_state.load_blueprint().frontier())


def test_unavailable_reviewer_skips_without_caching(audit_enabled, monkeypatch):
    calls, events = _wire(monkeypatch, "UNAVAILABLE")
    autonomy_state = _autonomy_state()

    assert runner._maybe_statement_fidelity_audit(autonomy_state, {}) == ""
    # Not cached: a later call retries once the provider is back.
    monkeypatch.setattr(runner, "_verification_review_decision", lambda result: "PASS")
    assert runner._maybe_statement_fidelity_audit(autonomy_state, {}) == "pass"
    assert len(calls) == 2
    assert not any(
        args[0] == "statement-fidelity-audit" and kwargs.get("verdict") == "suspect"
        for args, kwargs in events
    )
