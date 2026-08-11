"""Tests for durable parent priority over worker-checked helper candidates."""

from __future__ import annotations

import hashlib

import pytest

from leanflow_cli.workflows import (
    plan_state,
    research_findings,
    research_route_context,
)
from leanflow_cli.workflows import (
    research_helper_candidate_priority as priority,
)
from leanflow_cli.workflows.workflow_json_io import WorkflowStateCorruptionError


def _finding(active_file: str, *, job_id: str = "campaign.orchestrator.em-1"):
    declaration = "private lemma checked_helper (n : Nat) : n + 0 = n := by\n  simp"
    return {
        "job_id": job_id,
        "target_symbol": "demo",
        "active_file": active_file,
        "semantic_novelty": {
            "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
            "classification": "novel",
            "progress_anchor_eligible": True,
            "progress_anchor_reason": "new_mathematical_semantics",
            "has_checked_helper": True,
        },
        "deliverable": {
            "checked_helper_route_disposition": "advance_current_route",
            "checked_helper_dependency_advanced": "demo's reflexive core",
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "active_file": active_file,
                    "anchor_target_symbol": "demo",
                    "declaration": declaration,
                    "declaration_sha256": hashlib.sha256(declaration.encode()).hexdigest(),
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["checked_helper"],
                    },
                }
            ],
        },
    }


def _checked_helper(active_file: str, *, name: str, declaration: str) -> dict:
    """Return one parent-recheckable checked-helper record."""
    return {
        "active_file": active_file,
        "anchor_target_symbol": "demo",
        "declaration": declaration,
        "declaration_sha256": hashlib.sha256(declaration.encode()).hexdigest(),
        "parent_recheck_required": True,
        "worker_check": {
            "tool": "lean_incremental_check",
            "action": "check_helper",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": [name],
        },
    }


def _foreground_helper_check(active_file: str, *, declaration: str, name: str = "checked_helper"):
    """Return exact tool arguments and a successful foreground helper result."""
    return (
        {
            "action": "check_helper",
            "file_path": active_file,
            "theorem_id": "demo",
            "replacement": declaration,
        },
        {
            "success": True,
            "action": "check_helper",
            "ok": True,
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": [name],
        },
    )


def test_foreground_helper_check_is_durable_before_parent_recheck(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    declaration = "private lemma checked_helper (n : Nat) : n + 0 = n := by\n  simp"
    arguments, result = _foreground_helper_check(str(active), declaration=declaration)

    record = priority.remember_from_foreground_check(
        {},
        arguments,
        result,
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert record is not None
    assert record.state == priority.AWAITING_RECHECK
    assert record.job_id.startswith("foreground-check:")
    assert record.delivery_markers == ("foreground-check",)
    assert record.declaration == declaration
    assert priority.load({priority._HYDRATION_KEY: "prior-process"}) == record


def test_foreground_helper_preempts_stale_candidate_without_losing_it(monkeypatch, tmp_path):
    """Prioritize fresh foreground proof progress and durably queue older work."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    state: dict = {}
    older_declaration = "private lemma older_helper : True := by\n  trivial"
    older_arguments, older_result = _foreground_helper_check(
        str(active), declaration=older_declaration, name="older_helper"
    )
    older = priority.remember_from_foreground_check(
        state,
        older_arguments,
        older_result,
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert older is not None

    newer_declaration = "private lemma newer_helper (n : Nat) : n + 0 = n := by\n  simp"
    newer_arguments, newer_result = _foreground_helper_check(
        str(active), declaration=newer_declaration, name="newer_helper"
    )
    newer = priority.remember_from_foreground_check(
        state,
        newer_arguments,
        newer_result,
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert newer is not None
    assert priority.load(state) == newer
    assert priority.backlog(state) == (older,)
    restarted: dict = {priority._HYDRATION_KEY: "prior-process"}
    assert priority.load(restarted) == newer
    assert priority.backlog(restarted) == (older,)

    assert priority.resolve(restarted, disposition="parent_recheck_rejected") == newer
    assert priority.load(restarted) == older
    assert priority.backlog(restarted) == ()


def test_retiring_active_helper_promotes_durable_backlog(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    records = []
    for name in ("older_helper", "newer_helper"):
        declaration = f"private lemma {name} : True := by\n  trivial"
        arguments, result = _foreground_helper_check(
            str(active), declaration=declaration, name=name
        )
        record = priority.remember_from_foreground_check(
            state,
            arguments,
            result,
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        assert record is not None
        records.append(record)

    assert priority.retire(state) == records[1]
    assert priority.load(state) == records[0]
    assert priority.backlog(state) == ()


def test_foreground_scratch_helper_promotes_only_by_exact_name_change(monkeypatch, tmp_path):
    """Preserve substantive scratch work while requiring a name-only recheck."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    scratch = "private lemma test_add_zero (n : Nat) : n + 0 = n := by\n  simp"
    arguments, result = _foreground_helper_check(
        str(active), declaration=scratch, name="test_add_zero"
    )
    state = {}

    pending = priority.remember_nonproduction_from_foreground_check(
        state,
        arguments,
        result,
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert pending is not None
    assert pending.state == priority.AWAITING_PRODUCTION_RENAME
    assert priority.load(state) == pending
    restarted_state = {priority._HYDRATION_KEY: "prior-process"}
    assert priority.load(restarted_state) == pending
    production = scratch.replace("test_add_zero", "nat_add_zero")
    assert priority.is_exact_production_rename(
        pending,
        {
            "action": "check_helper",
            "file_path": str(active),
            "theorem_id": "demo",
            "replacement": production,
        },
    )
    assert not priority.is_exact_production_rename(
        pending,
        {
            "action": "check_helper",
            "file_path": str(active),
            "theorem_id": "demo",
            "replacement": production.replace("simp", "omega"),
        },
    )
    promoted_arguments, promoted_result = _foreground_helper_check(
        str(active), declaration=production, name="nat_add_zero"
    )
    promoted = priority.remember_from_foreground_check(
        restarted_state,
        promoted_arguments,
        promoted_result,
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert promoted is not None
    assert promoted.state == priority.AWAITING_RECHECK
    assert promoted.helper_name == "nat_add_zero"
    assert promoted.declaration == production
    assert priority.load(restarted_state) == promoted
    assert priority.load({priority._HYDRATION_KEY: "next-process"}) == promoted


def test_direct_forwarding_scratch_helper_never_reserves_production_rename(monkeypatch, tmp_path):
    """Treat a one-line alias of an existing theorem as inspection, not progress."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    declaration = (
        "private lemma test_call {n : Nat} (hn : n = n) : n = n := by\n" "  exact Eq.refl n"
    )
    arguments, result = _foreground_helper_check(
        str(active), declaration=declaration, name="test_call"
    )
    state: dict = {}

    assert (
        priority.successful_nonproduction_foreground_helper_name(
            arguments,
            result,
            target_symbol="demo",
            active_file=str(active),
        )
        == ""
    )
    assert (
        priority.remember_nonproduction_from_foreground_check(
            state,
            arguments,
            result,
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )
    assert priority.load(state) is None


@pytest.mark.parametrize(
    "declaration",
    [
        "private lemma winsNow_refl (p : Prop) : p ↔ p := by\n  rfl",
        "private lemma value_refl (n : Nat) : n = n := by\n  rfl",
    ],
)
def test_reflexive_foreground_helper_never_reserves_integration(monkeypatch, tmp_path, declaration):
    """Treat a checked tautology as evidence, not mandatory source growth."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    name = "winsNow_refl" if "winsNow" in declaration else "value_refl"
    arguments, result = _foreground_helper_check(str(active), declaration=declaration, name=name)

    assert (
        priority.remember_from_foreground_check(
            {},
            arguments,
            result,
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


@pytest.mark.parametrize(
    ("result_update", "declaration"),
    [
        ({"ok": False}, "private lemma checked_helper : True := by\n  trivial"),
        ({"has_sorry": True}, "private lemma checked_helper : True := by\n  trivial"),
        (
            {"replacement_declarations": ["checked_helper", "second_helper"]},
            "private lemma checked_helper : True := by\n  trivial",
        ),
        ({}, "private lemma checked_helper : True := by\n  apply?"),
        ({}, "private lemma checked_helper : True := by\n  sorry"),
    ],
)
def test_foreground_helper_check_fails_closed(monkeypatch, tmp_path, result_update, declaration):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    arguments, result = _foreground_helper_check(str(active), declaration=declaration)
    result.update(result_update)

    assert (
        priority.remember_from_foreground_check(
            {},
            arguments,
            result,
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_foreground_helper_check_requires_exact_assignment(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    other = tmp_path / "Other.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    other.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    declaration = "private lemma checked_helper : True := by\n  trivial"
    arguments, result = _foreground_helper_check(str(other), declaration=declaration)

    assert (
        priority.remember_from_foreground_check(
            {},
            arguments,
            result,
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_registers_one_exact_assignment_candidate_and_deduplicates(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}

    first = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
        delivery_markers=("marker-1",),
    )
    repeated = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
        delivery_markers=("marker-1",),
    )

    assert first is not None
    assert repeated == first
    assert first.state == "awaiting_parent_recheck"
    assert first.helper_name == "checked_helper"
    assert first.delivery_markers == ("marker-1",)
    assert priority.load(state) == first


def test_declared_evidence_only_helper_is_not_registered(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    finding["deliverable"]["checked_helper_route_disposition"] = "evidence_only"
    finding["deliverable"]["checked_helper_dependency_advanced"] = ""

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is None
    assert research_findings.foreground_use_reason(finding) == (
        "checked_helper_declared_evidence_only"
    )


@pytest.mark.parametrize(
    "name",
    [
        "pointwise_counterplays_do_not_swap_quantifiers",
        "double_angle_trigger_not_universal_at_pi_div_four",
        "exists_bounded_of_natRank_descent_probe",
    ],
)
def test_evidence_named_helper_is_not_registered_despite_worker_disposition(
    monkeypatch, tmp_path, name
):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    declaration = f"private theorem {name} : True := by\n  trivial"
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(str(active), name=name, declaration=declaration)
    ]

    assert research_findings.foreground_use_role(finding) == "actionable"
    assert (
        priority.remember_from_findings(
            {},
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_scratch_named_checked_helper_cannot_become_integration_priority(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    name = "cast_add_test"
    declaration = f"private lemma {name} : True := by\n  trivial"
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(str(active), name=name, declaration=declaration)
    ]

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is None


def test_legacy_explicitly_nonadvancing_helper_is_not_registered(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    finding["deliverable"].pop("checked_helper_route_disposition")
    finding["deliverable"][
        "interpretation"
    ] = "This checked arithmetic helper does not advance the missing construction."

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is None
    assert research_findings.foreground_use_reason(finding) == (
        "legacy_checked_helper_explicitly_nonadvancing"
    )


def test_checked_finite_singleton_is_not_registered(monkeypatch, tmp_path):
    """Keep one checked instance as evidence instead of foreground helper work."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active), job_id="campaign.orchestrator.em-singleton")
    finding["semantic_novelty"] = {
        "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
        "classification": "finite_evidence_only",
        "progress_anchor_eligible": False,
        "progress_anchor_reason": "declared_finite_evidence_only",
        "has_checked_helper": True,
    }
    finding["deliverable"].update(
        {
            "status": "finite_instance_verified",
            "bounded_experiment": {"a": 2, "n": 7, "x": 4, "y": 29, "z": 812},
        }
    )

    assert (
        priority.remember_from_findings(
            {},
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_stale_fixed_instance_without_novelty_is_not_registered(monkeypatch, tmp_path):
    """Keep em-704 finite evidence out of the parent helper-priority fence."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active), job_id="campaign.orchestrator.em-704")
    finding.pop("semantic_novelty")
    finding["deliverable"].update(
        {
            "status": "new_fixed_instance_checked_not_target_completion",
            "bounded_experiment": {"instance": {"a": 2, "n": 7, "x": 4, "y": 29, "z": 812}},
        }
    )
    state: dict = {}

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert research_findings.foreground_use_reason(finding) == "declared_finite_evidence_only"
    assert (
        priority.remember_from_findings(
            state,
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )
    assert priority.load(state) is None


def test_stale_parametric_partial_helper_remains_actionable(monkeypatch, tmp_path):
    """Do not suppress a general checked helper merely because closure is partial."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active), job_id="campaign.orchestrator.ds-parametric")
    finding.pop("semantic_novelty")
    finding["deliverable"]["status"] = "partial_general_reduction_checked"
    declaration = "private lemma checked_helper (n : Nat) (h : 0 < n) : n + 0 = n := by\n" "  simp"
    helper = finding["deliverable"]["checked_helpers"][0]
    helper["declaration"] = declaration
    helper["declaration_sha256"] = hashlib.sha256(declaration.encode()).hexdigest()
    state: dict = {}

    assert research_findings.foreground_use_role(finding) == "actionable"
    remembered = priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is not None
    assert remembered.job_id == "campaign.orchestrator.ds-parametric"
    assert remembered.declaration == declaration


def test_source_stale_parametric_helper_is_rechecked_by_parent(monkeypatch, tmp_path):
    """Retry exact helper source after an earlier dependency changes the file."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active), job_id="campaign.orchestrator.ds-stale-source")
    finding["source_revision_sha256"] = hashlib.sha256(active.read_bytes()).hexdigest()
    active.write_text(
        "private lemma new_dependency : True := by trivial\n\n"
        "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )

    assert research_findings.foreground_use_reason(finding) == "stale_active_file_revision"
    assert research_findings.foreground_use_role(finding) == "evidence_only"
    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is not None
    assert remembered.helper_name == "checked_helper"


def test_source_stale_finite_helper_remains_evidence_only(monkeypatch, tmp_path):
    """Do not let source staleness bypass finite-evidence policy."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active), job_id="campaign.orchestrator.em-stale-finite")
    finding["source_revision_sha256"] = hashlib.sha256(active.read_bytes()).hexdigest()
    finding["semantic_novelty"]["progress_anchor_eligible"] = False
    finding["semantic_novelty"]["progress_anchor_reason"] = "declared_finite_evidence_only"
    finding["deliverable"]["status"] = "finite_instance_verified"
    finding["deliverable"]["bounded_experiment"] = {"n": 3}
    active.write_text(
        "private lemma new_dependency : True := by trivial\n\n"
        "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )

    assert (
        priority.remember_from_findings(
            {},
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_refreshed_proved_eventual_graph_node_cannot_suppress_useful_helper(monkeypatch, tmp_path):
    """Retain useful source after the proved graph node's environment changes."""
    active = tmp_path / "Demo.lean"
    base = "private lemma demo_base (n x : Nat) (h : n % 2 = 1) : n = n := by\n" "  rfl"
    eventual = (
        "private lemma demo_eventual :\n"
        "    ∀ᶠ (n : Nat) in Filter.atTop, n = n := by\n"
        "  filter_upwards [] with n\n"
        "  exact demo_base n n (by omega)"
    )
    target = (
        "theorem demo (a : Nat) :\n" "    ∀ᶠ (n : Nat) in Filter.atTop, n = n := by\n" "  sorry"
    )
    source = "\n\n".join(("import Mathlib.Data.Nat.Basic", base, eventual, target)) + "\n"
    active.write_text(source, encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    file = str(active)
    # This is the unsafe authority shape: reconciliation may retain `proved`
    # and refresh its whole-file hash even though an import or earlier source
    # declaration changed without a fresh exact kernel check.
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(
                id=plan_state.node_id_for("demo_eventual", file),
                kind="lemma",
                name="demo_eventual",
                file=file,
                statement=eventual,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                status="proved",
            ),
        )
    )
    stale = "private lemma demo_odd_case (n : Nat) (h : n % 2 = 1) : n + 0 = n := by\n" "  simp"
    delta = (
        "private lemma demo_parametric_delta (a n : Nat) (h : a ≤ n) : a ≤ n := by\n" "  exact h"
    )
    finding = _finding(file, job_id="campaign.orchestrator.ds-stale-first")
    finding["deliverable"]["status"] = "checked_partial_delta_not_target_completion"
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(file, name="demo_odd_case", declaration=stale),
        _checked_helper(file, name="demo_parametric_delta", declaration=delta),
    ]

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=file,
    )

    assert blueprint.nodes[0].status == "proved"
    assert blueprint.nodes[0].source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert remembered is not None
    assert remembered.helper_name == "demo_odd_case"
    assert remembered.declaration == stale


def test_exact_source_signature_is_deduplicated_without_semantic_authority(monkeypatch, tmp_path):
    """Skip only a declaration whose full proof-insensitive signature exists."""
    active = tmp_path / "Demo.lean"
    existing = "private lemma existing_helper (n : Nat) : n = n := by\n  rfl"
    target = "theorem demo : True := by\n  sorry"
    active.write_text("\n\n".join((existing, target)) + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    file = str(active)
    duplicate = "private lemma duplicate_name (n : Nat) : n = n := by\n  exact n"
    useful = "private lemma useful_delta (n : Nat) : True := by\n  trivial"
    finding = _finding(file, job_id="campaign.orchestrator.ds-duplicate-first")
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(file, name="duplicate_name", declaration=duplicate),
        _checked_helper(file, name="useful_delta", declaration=useful),
    ]

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=file,
    )

    assert remembered is not None
    assert remembered.helper_name == "useful_delta"
    assert remembered.declaration == useful


def test_alpha_equivalent_source_signature_is_deduplicated(monkeypatch, tmp_path):
    """Ignore hypothesis-name changes when the checked proposition already exists."""
    active = tmp_path / "Demo.lean"
    existing = "private theorem existing_helper {θ : Nat} (hθpos : 0 < θ) : θ = θ := by\n" "  rfl"
    target = "theorem demo : True := by\n  sorry"
    active.write_text("\n\n".join((existing, target)) + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    file = str(active)
    duplicate = (
        "private theorem renamed_helper {x : Nat} (hx : 0 < x) : x = x := by\n" "  exact rfl"
    )
    useful = "private lemma useful_delta : True := by\n  trivial"
    finding = _finding(file, job_id="campaign.orchestrator.ds-alpha-duplicate")
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(file, name="renamed_helper", declaration=duplicate),
        _checked_helper(file, name="useful_delta", declaration=useful),
    ]

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=file,
    )

    assert remembered is not None
    assert remembered.helper_name == "useful_delta"


def test_same_name_source_declaration_is_detected_before_insertion(monkeypatch, tmp_path):
    """Detect a checked helper name already present before the target."""
    active = tmp_path / "Demo.lean"
    target = "theorem demo : True := by\n  sorry"
    active.write_text(target + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    file = str(active)
    collision = "theorem checked_helper (n : Nat) : n + 0 = n := by\n  simp"
    finding = _finding(file, job_id="campaign.orchestrator.ds-name-collision")
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(file, name="checked_helper", declaration=collision),
    ]

    remembered = priority.remember_from_findings(
        {},
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=file,
    )

    assert remembered is not None
    active.write_text(
        "theorem checked_helper (n : Nat) : n = n := by\n  rfl\n\n" + target + "\n",
        encoding="utf-8",
    )
    detected = priority.source_name_collision(remembered)
    assert detected is not None
    assert detected.existing_symbol == "checked_helper"
    assert detected.reason == "same_name_current_source"


def test_preexisting_same_name_source_declaration_is_not_staged(monkeypatch, tmp_path):
    """Reject a stale worker helper before it enters parent priority state."""
    active = tmp_path / "Demo.lean"
    active.write_text(
        "\n\n".join(
            (
                "theorem checked_helper (n : Nat) : n = n := by\n  rfl",
                "theorem demo : True := by\n  sorry",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)

    remembered = priority.remember_from_findings(
        {},
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert remembered is None


def test_preexisting_pending_eventual_candidate_is_not_retired_from_graph_status(
    monkeypatch, tmp_path
):
    """Do not retire a staged helper without immutable current kernel evidence."""
    active = tmp_path / "Demo.lean"
    base = "private lemma demo_base (n : Nat) : n = n := by\n  rfl"
    eventual = (
        "private lemma demo_eventual :\n"
        "    ∀ᶠ (n : Nat) in Filter.atTop, n = n := by\n"
        "  filter_upwards [] with n\n"
        "  exact demo_base n"
    )
    target = "theorem demo : ∀ᶠ (n : Nat) in Filter.atTop, n = n := by\n  sorry"
    active.write_text("\n\n".join((base, eventual, target)) + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    file = str(active)
    candidate = "private lemma demo_case (n : Nat) (h : 0 < n) : n + 0 = n := by\n" "  simp"
    finding = _finding(file, job_id="campaign.orchestrator.ds-resumed")
    finding["deliverable"]["checked_helpers"] = [
        _checked_helper(file, name="demo_case", declaration=candidate)
    ]
    state: dict = {}
    pending = priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=file,
    )
    refreshed_graph = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(
                id=plan_state.node_id_for("demo_eventual", file),
                kind="lemma",
                name="demo_eventual",
                file=file,
                statement=eventual,
                source_sha256=hashlib.sha256(active.read_bytes()).hexdigest(),
                status="proved",
            ),
        )
    )

    duplicate = priority.exact_source_duplicate(pending)

    assert refreshed_graph.nodes[0].status == "proved"
    assert refreshed_graph.nodes[0].source_sha256 == hashlib.sha256(active.read_bytes()).hexdigest()
    assert duplicate is None
    assert priority.load(state) == pending


def test_later_candidate_cannot_replace_unacted_candidate(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    first = priority.remember_from_findings(
        state,
        (_finding(str(active), job_id="campaign.em-strong"),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    later_finding = _finding(str(active), job_id="campaign.np-redundant")
    helper = later_finding["deliverable"]["checked_helpers"][0]
    helper["declaration"] = "private lemma redundant_helper : True := by\n  trivial"
    helper["declaration_sha256"] = hashlib.sha256(helper["declaration"].encode()).hexdigest()
    helper["worker_check"]["replacement_declarations"] = ["redundant_helper"]

    retained = priority.remember_from_findings(
        state,
        (later_finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert retained == first
    assert priority.load(state).job_id == "campaign.em-strong"


def test_parent_recheck_state_survives_summary_hydration(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    ready = priority.mark_parent_recheck(
        state,
        candidate_id=record.candidate_id,
        status="accepted",
        source_revision_sha256=priority.source_revision_sha256(str(active)),
        detail="parent exact check passed",
    )
    resumed: dict = {priority._HYDRATION_KEY: "prior-process"}

    assert ready is not None and ready.state == "ready_to_integrate"
    assert priority.load(resumed) == ready
    assert priority.matching(resumed, target_symbol="demo", active_file=str(active)) == ready


def test_inserted_match_requires_exact_current_declaration(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert priority.inserted_candidate_matches(record) is False
    active.write_text(
        record.declaration + "\n\n" + "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )

    assert priority.inserted_candidate_matches(record) is True


def test_inserted_match_includes_declaration_local_option_wrapper(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    target = "theorem demo : True := by\n  sorry\n"
    active.write_text(target, encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    helper = finding["deliverable"]["checked_helpers"][0]
    declaration = (
        "set_option maxRecDepth 100000 in\n"
        "private lemma checked_helper : True := by\n"
        "  trivial"
    )
    helper["declaration"] = declaration
    helper["declaration_sha256"] = hashlib.sha256(declaration.encode()).hexdigest()
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert record is not None

    integrated = declaration + "\n\n" + target

    assert priority.inserted_candidate_matches_source(record, integrated) is True


def test_inserted_match_rejects_helper_after_assigned_target(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    active.write_text(
        "theorem demo : True := by\n  sorry\n\n" + record.declaration + "\n",
        encoding="utf-8",
    )

    assert priority.inserted_candidate_matches(record) is False


def test_inserted_match_preserves_target_doc_and_attribute_preamble(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    target = (
        "/-- Documentation owned by demo. -/\n"
        "@[simp]\n"
        "theorem demo : True := by\n"
        "  sorry\n"
    )
    active.write_text(target, encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert record is not None

    safe = record.declaration + "\n\n" + target
    assert priority.inserted_candidate_matches_source(record, safe) is True

    stolen = (
        "/-- Documentation owned by demo. -/\n"
        + record.declaration
        + "\n\n@[simp]\ntheorem demo : True := by\n  sorry\n"
    )
    assert priority.inserted_candidate_matches_source(record, stolen) is False


def test_target_signature_tracks_let_binding_inside_statement(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem demo : (let x := True; x) := by\n  sorry\n",
        encoding="utf-8",
    )
    before = priority.target_signature_sha256(str(active), "demo")
    active.write_text(
        "theorem demo : (let x := False; x) := by\n  sorry\n",
        encoding="utf-8",
    )

    assert before
    assert priority.target_signature_sha256(str(active), "demo") != before


def test_oversized_helper_is_not_duplicated_into_priority_state(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    finding = _finding(str(active))
    helper = finding["deliverable"]["checked_helpers"][0]
    helper["declaration"] = (
        "private lemma checked_helper : True := by\n  trivial\n-- "
        + "x" * priority.MAX_DECLARATION_CHARS
    )
    helper["declaration_sha256"] = hashlib.sha256(helper["declaration"].encode()).hexdigest()

    assert (
        priority.remember_from_findings(
            {},
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_resolved_candidate_cannot_be_registered_again(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    finding = _finding(str(active))
    record = priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    priority.resolve(state, disposition="integrated")

    assert priority.load(state) is None
    assert record.candidate_id in priority.resolved_candidate_ids(state)
    assert (
        priority.remember_from_findings(
            state,
            (finding,),
            campaign_id="campaign",
            target_symbol="demo",
            active_file=str(active),
        )
        is None
    )


def test_integrated_helper_requires_target_body_consumption_before_next_priority(
    monkeypatch, tmp_path
):
    """Helper-only source edits must not allow an endless priority chain."""
    active = tmp_path / "Demo.lean"
    target = "theorem demo : True := by\n  sorry"
    active.write_text(target + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    first_finding = _finding(str(active), job_id="campaign.orchestrator.ds-first")
    first = priority.remember_from_findings(
        state,
        (first_finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert first is not None
    active.write_text(first.declaration + "\n\n" + target + "\n", encoding="utf-8")

    priority.resolve(state, disposition="integrated_managed_edit")

    assert priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )
    second_finding = _finding(str(active), job_id="campaign.orchestrator.ds-second")
    second_helper = second_finding["deliverable"]["checked_helpers"][0]
    second_declaration = "private lemma second_helper : True := by\n  trivial"
    second_helper["declaration"] = second_declaration
    second_helper["declaration_sha256"] = hashlib.sha256(second_declaration.encode()).hexdigest()
    second_helper["worker_check"]["replacement_declarations"] = ["second_helper"]
    second = priority.remember_from_findings(
        state,
        (second_finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert second is not None

    # More source growth above an unchanged target does not release priority.
    active.write_text(
        first.declaration
        + "\n\nprivate lemma unrelated : True := by\n  trivial\n\n"
        + target
        + "\n",
        encoding="utf-8",
    )
    assert priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )

    # A placeholder-preserving edit that does not use the helper is still not
    # obligation progress.
    active.write_text(
        first.declaration
        + "\n\n"
        + "theorem demo : True := by\n  have : True := trivial\n  sorry\n",
        encoding="utf-8",
    )
    assert priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )

    # Concrete use in the assigned proof releases the research gate even while
    # a residual placeholder remains. The advisor may now help with that
    # residual obligation instead of deadlocking behind the banked helper.
    active.write_text(
        first.declaration
        + "\n\n"
        + "theorem demo : True := by\n  have h := checked_helper\n  sorry\n",
        encoding="utf-8",
    )
    assert not priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )

    # Recreate the marker to retain coverage of manager-verified release.
    active.write_text(target + "\n", encoding="utf-8")
    state = {}
    first = priority.remember_from_findings(
        state,
        (first_finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert first is not None
    active.write_text(first.declaration + "\n\n" + target + "\n", encoding="utf-8")
    priority.resolve(state, disposition="integrated_managed_edit")

    # Removing the assigned placeholder is only a candidate edit. It cannot
    # release priority before the manager accepts the exact target.
    active.write_text(
        first.declaration + "\n\n" + "theorem demo : True := by\n  trivial\n",
        encoding="utf-8",
    )
    assert priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )
    assert priority.release_target_consumption_after_verified_target(
        state,
        target_symbol="demo",
        active_file=str(active),
    )
    assert not priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )


def test_target_consumption_marker_survives_summary_hydration(monkeypatch, tmp_path):
    """Restart must preserve the target-body gate after helper integration."""
    active = tmp_path / "Demo.lean"
    target = "theorem demo : True := by\n  sorry"
    active.write_text(target + "\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert record is not None
    active.write_text(record.declaration + "\n\n" + target + "\n", encoding="utf-8")
    priority.resolve(state, disposition="integrated_managed_edit")
    resumed = {priority._HYDRATION_KEY: "prior-process"}

    assert priority.target_consumption_pending(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )
    summary = priority.read_json_file(priority.plan_state.plan_state_paths().summary_json)
    assert summary[priority.CONSUMPTION_SUMMARY_KEY]["candidate_id"] == record.candidate_id

    active.write_text(
        record.declaration
        + "\n\n"
        + "theorem demo : True := by\n  have : True := trivial\n  sorry\n",
        encoding="utf-8",
    )
    assert priority.target_consumption_pending(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )
    active.write_text(
        record.declaration
        + "\n\n"
        + "theorem demo : True := by\n  have h := checked_helper\n  sorry\n",
        encoding="utf-8",
    )
    assert not priority.target_consumption_pending(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )

    # Rehydrate another integrated marker for the verified-target path.
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state-verified"))
    active.write_text(target + "\n", encoding="utf-8")
    state = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert record is not None
    active.write_text(record.declaration + "\n\n" + target + "\n", encoding="utf-8")
    priority.resolve(state, disposition="integrated_managed_edit")
    resumed = {priority._HYDRATION_KEY: "prior-process-verified"}
    active.write_text(
        record.declaration + "\n\n" + "theorem demo : True := by\n  trivial\n",
        encoding="utf-8",
    )
    assert priority.target_consumption_pending(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )
    assert priority.release_target_consumption_after_verified_target(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )
    assert not priority.target_consumption_pending(
        resumed,
        target_symbol="demo",
        active_file=str(active),
    )
    summary = priority.read_json_file(priority.plan_state.plan_state_paths().summary_json)
    assert summary[priority.CONSUMPTION_SUMMARY_KEY] == {}


def test_integrated_evidence_helper_does_not_require_target_consumption(monkeypatch, tmp_path):
    """Evidence may remain banked without forcing a dummy parent reference."""
    active = tmp_path / "Demo.lean"
    target = "theorem demo : True := by\n  sorry"
    active.write_text(target + "\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert record is not None
    active.write_text(record.declaration + "\n\n" + target + "\n", encoding="utf-8")

    priority.resolve(
        state,
        disposition="integrated_managed_edit",
        require_target_consumption=False,
    )

    assert not priority.target_consumption_pending(
        state,
        target_symbol="demo",
        active_file=str(active),
    )


def test_resolved_older_helper_cannot_hide_later_unacted_candidate(monkeypatch, tmp_path):
    """Live resume regression: banked helper A must not shadow archived em-709."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(priority.plan_state, "plan_state_enabled", lambda: False)
    state: dict = {}
    older = _finding(str(active), job_id="campaign.orchestrator.em-702")
    first = priority.remember_from_findings(
        state,
        (older,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert first is not None
    priority.resolve(state, disposition="integrated")
    later = _finding(str(active), job_id="campaign.orchestrator.em-709")
    helper = later["deliverable"]["checked_helpers"][0]
    helper["declaration"] = "private lemma denominator_scale_certificate : True := by\n  trivial"
    helper["declaration_sha256"] = hashlib.sha256(helper["declaration"].encode()).hexdigest()
    helper["worker_check"]["replacement_declarations"] = ["denominator_scale_certificate"]

    recovered = priority.remember_from_findings(
        state,
        (older, later),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )

    assert recovered is not None
    assert recovered.job_id == "campaign.orchestrator.em-709"
    assert recovered.helper_name == "denominator_scale_certificate"


def test_resolved_disk_state_overrides_stale_checkpoint_pending(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    state: dict = {}
    record = priority.remember_from_findings(
        state,
        (_finding(str(active)),),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    stale_checkpoint = {
        priority.STATE_KEY: record.to_mapping(),
        priority.RESOLVED_STATE_KEY: [],
        priority._HYDRATION_KEY: "prior-process",
    }

    priority.resolve(state, disposition="integrated")

    summary = priority.read_json_file(priority.plan_state.plan_state_paths().summary_json)
    assert summary[priority.SUMMARY_KEY] == {}
    assert summary[priority.RESOLVED_SUMMARY_KEY][-1]["candidate_id"] == record.candidate_id
    assert priority.load(stale_checkpoint) is None
    assert record.candidate_id in priority.resolved_candidate_ids(stale_checkpoint)


def test_corrupt_durable_candidate_state_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    summary_path = priority.plan_state.plan_state_paths().summary_json
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(WorkflowStateCorruptionError):
        priority.load({priority._HYDRATION_KEY: "prior-process"})
