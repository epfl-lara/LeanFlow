"""False campaign-decomposition cleanup provenance and restart tests."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.lean import negation_probe
from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import (
    decomposition_provenance,
    false_decomposition_cleanup,
    negation_promotion,
    plan_state,
    workflow_activity_retention,
)
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode
from leanflow_cli.workflows.queue_manager import QueueItem, TheoremKey, TheoremQueueManager
from leanflow_cli.workflows.workflow_json_io import update_json_file

PARENT = "parent_goal"
HELPER = "bad_helper"
NEGATION_HELPER = "neg_bad_helper"

BEFORE_SOURCE = """namespace Demo

theorem parent_goal : True := by
  sorry

end Demo
"""

INSERTED_HELPER = "theorem bad_helper : False := by sorry"
VALID_SIBLING = "theorem valid_sibling : True := by trivial"

CURRENT_SOURCE = """namespace Demo

theorem neg_bad_helper : ¬ False := by
  simp

/-- Campaign-created false helper. -/
@[category research open]
theorem bad_helper : False := by
  sorry

/-- A valid sibling from the same decomposition remains owned source. -/
theorem valid_sibling : True := by
  trivial

/-- Current parent documentation must survive proof restoration. -/
@[category research open]
theorem parent_goal : True := by
  exact False.elim bad_helper

end Demo
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def cleanup_project(monkeypatch, tmp_path):
    state_root = tmp_path / ".leanflow" / "workflow-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_root))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo.lean")
    source = tmp_path / "Demo.lean"
    source.write_text(CURRENT_SOURCE, encoding="utf-8")
    return tmp_path, state_root, source


def _promotion(source: Path) -> dict[str, object]:
    helper = decomposition_provenance.declaration_slice(source.read_text(encoding="utf-8"), HELPER)
    assert helper is not None
    return {
        "promotion_id": "promotion-bad-helper",
        "promotion_kind": "source_negation",
        "theorem": HELPER,
        "file": str(source),
        "canonical_file": str(source),
        "node_id": plan_state.node_id_for(HELPER, str(source)),
        "is_main_goal": False,
        "source_revision_sha256": _sha256(source.read_text(encoding="utf-8")),
        "declaration_signature_sha256": helper.signature_sha256,
        "proof_declaration": NEGATION_HELPER,
        "proof_tactic": f"exact {NEGATION_HELPER}",
        "negation_prop": "False",
        "axioms": [],
    }


def _relative_graph_promotion(source: Path) -> dict[str, object]:
    """Return new-format evidence bound to a relative dependency-graph label."""
    promotion = _promotion(source)
    graph_file = source.name
    node_id = plan_state.node_id_for(HELPER, graph_file)
    payload = {
        "theorem": HELPER,
        "operation_path": str(source),
        "node_id": node_id,
        "graph_node_name": HELPER,
        "graph_node_file": graph_file,
        "is_main_goal": False,
    }
    promotion.update(payload)
    promotion["file"] = str(source)
    promotion["canonical_file"] = str(source)
    promotion["key"] = f"{source}::{HELPER}"
    promotion["graph_identity_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return promotion


def _seed_graph(
    source: Path,
    *,
    generated_by: str = "decomposer",
    helper_status: str = "false",
) -> None:
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent_id = plan_state.node_id_for(PARENT, str(source))
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=helper_id,
                    name=HELPER,
                    file=str(source),
                    status=helper_status,
                    generated_by=generated_by,
                ),
                GraphNode(
                    id=parent_id,
                    name=PARENT,
                    file=str(source),
                    statement="Assigned declaration slice (1-7):\nSTALE",
                    status="proved",
                    owner="dead-owner",
                    generated_by="queue-sync",
                ),
            ),
            # The regression is specifically a false helper with zero edges.
            edges=(),
        )
    )


def _seed_relative_graph(source: Path) -> None:
    """Seed the same decomposition using its project-relative graph identity."""
    graph_file = source.name
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=plan_state.node_id_for(HELPER, graph_file),
                    name=HELPER,
                    file=graph_file,
                    status="false",
                    generated_by="decomposer",
                ),
                GraphNode(
                    id=plan_state.node_id_for(PARENT, graph_file),
                    name=PARENT,
                    file=graph_file,
                    status="proved",
                    owner="dead-owner",
                    generated_by="queue-sync",
                ),
            ),
        )
    )


def _seed_promotion(promotion: dict[str, object]) -> None:
    source = Path(str(promotion.get("operation_path") or promotion["file"]))
    graph_file = str(promotion.get("graph_node_file") or source)
    theorem = str(promotion["theorem"])
    node_id = plan_state.node_id_for(theorem, graph_file)
    promotion.update(
        {
            "key": f"{source}::{theorem}",
            "file": str(source),
            "canonical_file": str(source),
            "operation_path": str(source),
            "negation_name": str(promotion.get("negation_name") or NEGATION_HELPER),
            "promoted_at": "2026-07-16T00:00:00+00:00",
            "node_id": node_id,
            "graph_node_name": theorem,
            "graph_node_file": graph_file,
            "is_main_goal": False,
            "classification_basis": "decomposition_helper",
            "scope_root_campaign_id": "",
            "scope_root_identity_sha256": "",
            "scope_root_theorem": "",
            "scope_root_file": "",
            "scope_root_node_id": "",
            "graph_before_statuses": {},
            "graph_after_statuses": {},
            "graph_changed_node_identities": {},
            "graph_before_revision": plan_state.load_blueprint().revision,
            "graph_expected_revision": plan_state.load_blueprint().revision,
        }
    )
    graph_payload = {
        "theorem": theorem,
        "operation_path": str(source),
        "node_id": node_id,
        "graph_node_name": theorem,
        "graph_node_file": graph_file,
        "is_main_goal": False,
    }
    promotion["graph_identity_sha256"] = negation_promotion._graph_identity_sha256(graph_payload)
    classification_payload = {
        **graph_payload,
        "classification_basis": "decomposition_helper",
        "scope_root_campaign_id": "",
        "scope_root_identity_sha256": "",
        "scope_root_theorem": "",
        "scope_root_file": "",
        "scope_root_node_id": "",
    }
    promotion["classification_identity_sha256"] = negation_promotion._graph_identity_sha256(
        classification_payload
    )
    promotion.update(negation_promotion._seal_rollback_plan(promotion))
    promotion.update(negation_promotion._canonicalize_promotion_record(promotion, source.parent))
    transaction = {
        "transaction_id": promotion["promotion_id"],
        "state": "committed",
        "prepared_at": "2026-07-16T00:00:00+00:00",
        "committed_at": "2026-07-16T00:01:00+00:00",
        "promotion": dict(promotion),
    }

    def mutate(summary):
        summary["negation_promotions"] = [promotion]
        summary["negation_promotion_transactions"] = [transaction]

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _seed_current_provenance(source: Path) -> None:
    inserted_source = BEFORE_SOURCE.replace(
        "theorem parent_goal",
        f"{INSERTED_HELPER}\n\n{VALID_SIBLING}\n\ntheorem parent_goal",
    )
    with decomposition_provenance.source_operation(source) as operation:
        record = decomposition_provenance.begin_decomposition(
            active_file=str(source),
            target_symbol=PARENT,
            skeletons=[INSERTED_HELPER, VALID_SIBLING],
            before_text=BEFORE_SOURCE,
            after_text=inserted_source,
            cwd=str(source.parent),
            operation=operation,
        )
        decomposition_provenance.finish_decomposition(
            str(record["transaction_id"]), state="committed"
        )


def _valid(_promotion):
    return SimpleNamespace(ok=True, reason="fresh negation is valid")


@pytest.mark.parametrize(
    "migration_case",
    (
        "current-cleanup-spelling",
        "live-persisted-spelling-empty-provenance",
        "wrong-nonempty-provenance",
        "multiple-parser-quarantines",
        "duplicate-active-promotion-authority",
        "unrelated-quarantined-transaction",
        "stale-dependent-let-parent",
        "stale-dependent-let-parent-v1-replay",
    ),
)
def test_dependent_let_helper_uses_negation_promotion_signature_identity(
    monkeypatch,
    tmp_path,
    migration_case,
):
    """Cleanup admits the exact dependent-let statement promoted as false."""
    state_root = tmp_path / ".leanflow" / "workflow-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_root))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    helper_name = "erdos_242_mod_five_two_witness_candidate"
    parent_name = "erdos_242_parent"
    helper_signature = """private lemma erdos_242_mod_five_two_witness_candidate
    (t : ℕ) :
    let n := 840 * t + 361
    let x := 210 * t + 91
    let Q := 4 * x - n
    let B := n * x
    let p₁ := 1
    let p₂ := B ^ 2
    1 ≤ x ∧ 0 < Q ∧ p₁ * p₂ = B ^ 2 ∧
      Q ∣ (B + p₁) ∧ Q ∣ (B + p₂) ∧
      x < (B + p₁) / Q ∧ (B + p₁) / Q < (B + p₂) / Q"""
    parent_before = """theorem erdos_242_parent (t : ℕ) :
  let n := 840 * t + 361
  n = n := by
  sorry"""
    before_source = "\n\n".join(
        (
            "namespace Erdos242",
            parent_before,
            "end Erdos242\n",
        )
    )
    source_text = "\n\n".join(
        (
            "namespace Erdos242",
            f"{helper_signature} := by\n  sorry",
            parent_before,
            "end Erdos242\n",
        )
    )
    source = tmp_path / "Erdos242.lean"
    source.write_text(source_text, encoding="utf-8")

    helper = decomposition_provenance.declaration_slice(source_text, helper_name)
    goal = negation_probe.build_negation_goal(str(source), helper_name, cwd=str(tmp_path))

    assert helper is not None
    assert not isinstance(goal, dict)
    assert helper.signature == helper_signature.split(":=", 1)[0].rstrip()
    assert goal.original == helper_signature
    expected_signature_sha256 = "7540160bba6eeb9dd9ad51272a84b72943623801d3859d9b12d0014a7009c790"
    assert helper.signature_sha256 != expected_signature_sha256
    assert (
        decomposition_provenance.full_declaration_signature_sha256(helper.text)
        == expected_signature_sha256
    )
    assert (
        decomposition_provenance.full_declaration_signature_sha256(
            f"{helper_signature} := dependentTermProof"
        )
        == expected_signature_sha256
    )
    assert hashlib.sha256(goal.original.encode("utf-8")).hexdigest() == (expected_signature_sha256)

    parent = decomposition_provenance.declaration_slice(source_text, parent_name)
    assert parent is not None
    with decomposition_provenance.source_operation(source) as operation:
        provenance = decomposition_provenance.begin_decomposition(
            active_file=str(source),
            target_symbol=parent_name,
            skeletons=[f"{helper_signature} := by\n  sorry"],
            before_text=before_source,
            after_text=source_text,
            cwd=str(tmp_path),
            operation=operation,
        )
        assert decomposition_provenance.finish_decomposition(
            str(provenance["transaction_id"]), state="committed"
        )
    resolved, provenance_reason = decomposition_provenance.resolve_helper_provenance(
        helper_name=helper_name,
        file_label=str(source),
        promotion_signature_sha256=expected_signature_sha256,
        current_source=source_text,
        cwd=str(tmp_path),
    )

    assert provenance_reason == ""
    assert resolved is not None
    assert resolved["transaction_id"] == provenance["transaction_id"]
    transaction, reason = false_decomposition_cleanup._build_source_transaction(
        {
            "promotion_id": "promotion-dependent-let-helper",
            "theorem": helper_name,
            "node_id": plan_state.node_id_for(helper_name, str(source)),
            "declaration_signature_sha256": expected_signature_sha256,
        },
        resolved,
        current_source=source_text,
        file_identity=str(source),
    )

    assert reason == ""
    assert transaction is not None
    assert transaction["helper_signature_sha256"] == expected_signature_sha256

    stale_source = source_text.replace("840 * t + 361", "840 * t + 362", 1)
    stale_transaction, stale_reason = false_decomposition_cleanup._build_source_transaction(
        {
            "promotion_id": "promotion-dependent-let-helper",
            "theorem": helper_name,
            "node_id": plan_state.node_id_for(helper_name, str(source)),
            "declaration_signature_sha256": expected_signature_sha256,
        },
        resolved,
        current_source=stale_source,
        file_identity=str(source),
    )
    assert stale_transaction is None
    assert stale_reason == "current false helper signature hash differs from promotion evidence"

    if migration_case in {
        "stale-dependent-let-parent",
        "stale-dependent-let-parent-v1-replay",
    }:
        changed_parent = parent_before.replace("840 * t + 361", "840 * t + 362").replace(
            "  sorry",
            f"  have _ := {helper_name} t\n  sorry",
        )
        changed_parent_slice = decomposition_provenance.declaration_slice(
            changed_parent, parent_name
        )
        original_parent_slice = decomposition_provenance.declaration_slice(
            parent_before, parent_name
        )
        assert changed_parent_slice is not None and original_parent_slice is not None
        assert changed_parent_slice.signature_sha256 == original_parent_slice.signature_sha256
        assert decomposition_provenance.full_declaration_signature_sha256(
            changed_parent_slice.text
        ) != decomposition_provenance.full_declaration_signature_sha256(original_parent_slice.text)
        source_text = source_text.replace(parent_before, changed_parent)
        source.write_text(source_text, encoding="utf-8")

    helper_id = plan_state.node_id_for(helper_name, str(source))
    parent_id = plan_state.node_id_for(parent_name, str(source))
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=helper_id,
                    name=helper_name,
                    file=str(source),
                    status="false",
                    generated_by="decomposer",
                ),
                GraphNode(
                    id=parent_id,
                    name=parent_name,
                    file=str(source),
                    status="proved",
                    generated_by="queue-sync",
                ),
            )
        )
    )
    promotion = {
        "promotion_id": "promotion-dependent-let-helper",
        "promotion_kind": "source_negation",
        "theorem": helper_name,
        "file": str(source),
        "canonical_file": str(source),
        "node_id": helper_id,
        "is_main_goal": False,
        "source_revision_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "declaration_signature_sha256": expected_signature_sha256,
        "proof_declaration": "erdos_242_mod_five_two_witness_candidate_impossible",
        "proof_tactic": ("exact erdos_242_mod_five_two_witness_candidate_impossible"),
        "negation_prop": goal.prop,
        "axioms": [],
    }
    _seed_promotion(promotion)
    if migration_case == "duplicate-active-promotion-authority":

        def duplicate_active_promotion(summary):
            summary["negation_promotions"] = [
                *summary["negation_promotions"],
                dict(summary["negation_promotions"][0]),
            ]

        update_json_file(
            plan_state.plan_state_paths().summary_json,
            duplicate_active_promotion,
        )
    legacy_replay_transaction_id = ""
    if migration_case == "stale-dependent-let-parent-v1-replay":
        changed_parent_slice = decomposition_provenance.declaration_slice(source_text, parent_name)
        assert changed_parent_slice is not None
        legacy_replay = {
            **transaction,
            "promotion": dict(promotion),
            "promotion_id": promotion["promotion_id"],
            "source_before_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "parent_current_declaration_sha256": (changed_parent_slice.declaration_sha256),
            "helper_node_id": helper_id,
            "parent_node_id": parent_id,
            "graph_file": str(source),
            "ownership_basis": "decomposer-graph",
        }
        legacy_replay = false_decomposition_cleanup._seal_transaction(legacy_replay)
        legacy_replay = false_decomposition_cleanup._begin_transaction(legacy_replay)
        legacy_replay_transaction_id = str(legacy_replay["transaction_id"])
    parser_quarantines: tuple[tuple[str, str], ...] = ()
    if migration_case == "current-cleanup-spelling":
        parser_quarantines = (
            (
                "current false helper signature hash differs from promotion evidence",
                str(provenance["transaction_id"]),
            ),
        )
    elif migration_case in {
        "live-persisted-spelling-empty-provenance",
        "unrelated-quarantined-transaction",
    }:
        parser_quarantines = (
            (
                "current false helper signature hash differs from promoted evidence",
                "",
            ),
        )
    elif migration_case == "wrong-nonempty-provenance":
        parser_quarantines = (
            (
                "current false helper signature hash differs from promoted evidence",
                "f" * 64,
            ),
        )
    elif migration_case == "duplicate-active-promotion-authority":
        parser_quarantines = (
            (
                "current false helper signature hash differs from promoted evidence",
                "",
            ),
        )
    elif migration_case == "multiple-parser-quarantines":
        parser_quarantines = (
            (
                "current false helper signature hash differs from promoted evidence",
                "",
            ),
            (
                "current false helper signature hash differs from promotion evidence",
                str(provenance["transaction_id"]),
            ),
        )

    unrelated_transaction_id = ""
    if migration_case == "unrelated-quarantined-transaction":
        unrelated, unrelated_reason = false_decomposition_cleanup._build_source_transaction(
            promotion,
            resolved,
            current_source=source_text,
            file_identity=str(source),
        )
        assert unrelated is not None and unrelated_reason == ""
        unrelated.update(
            {
                "helper_node_id": helper_id,
                "parent_node_id": parent_id,
                "graph_file": str(source),
                "ownership_basis": "decomposer-graph",
            }
        )
        unrelated = false_decomposition_cleanup._seal_transaction(unrelated)
        unrelated = false_decomposition_cleanup._begin_transaction(unrelated)
        unrelated_transaction_id = str(unrelated["transaction_id"])
        false_decomposition_cleanup._mark_transaction_quarantined(
            unrelated,
            "unrelated operator-owned cleanup ambiguity",
        )

    for quarantine_reason, quarantine_provenance_id in parser_quarantines:
        false_decomposition_cleanup._quarantine_candidate(
            promotion,
            reason=quarantine_reason,
            provenance_id=quarantine_provenance_id,
        )
    persisted_parser_quarantines = [
        item
        for item in plan_state.load_summary().get("false_decomposition_cleanup_quarantine", [])
        if item.get("reason") in false_decomposition_cleanup._SIGNATURE_MISMATCH_REASONS
    ]
    assert [
        (item["reason"], item["provenance_id"]) for item in persisted_parser_quarantines
    ] == list(parser_quarantines)

    before_cleanup = source.read_bytes()
    cleanup = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(tmp_path), validate_promotion=_valid
    )

    if migration_case in {
        "current-cleanup-spelling",
        "live-persisted-spelling-empty-provenance",
    }:
        assert cleanup.cleaned == 1
        assert cleanup.quarantined == 0
        assert (
            decomposition_provenance.declaration_slice(
                source.read_text(encoding="utf-8"), helper_name
            )
            is None
        )
        quarantines = plan_state.load_summary()["false_decomposition_cleanup_quarantine"]
        assert quarantines[-1]["state"] == "resolved"
        assert "dependent-let signature parser identity" in quarantines[-1]["resolution_reason"]
    else:
        assert cleanup.cleaned == 0
        assert source.read_bytes() == before_cleanup
        assert (
            decomposition_provenance.declaration_slice(
                source.read_text(encoding="utf-8"), helper_name
            )
            is not None
        )
        summary = plan_state.load_summary()
        if migration_case in {
            "wrong-nonempty-provenance",
            "multiple-parser-quarantines",
            "duplicate-active-promotion-authority",
        }:
            assert all(
                item["state"] == "quarantined"
                for item in summary["false_decomposition_cleanup_quarantine"]
                if item.get("reason") in false_decomposition_cleanup._SIGNATURE_MISMATCH_REASONS
            )
        elif migration_case == "unrelated-quarantined-transaction":
            matching_transactions = [
                item
                for item in summary["false_decomposition_cleanup_transactions"]
                if item.get("transaction_id") == unrelated_transaction_id
            ]
            assert len(matching_transactions) == 1
            assert matching_transactions[0]["state"] == "quarantined"
            migrated = [
                item
                for item in summary["false_decomposition_cleanup_quarantine"]
                if item.get("reason")
                == "current false helper signature hash differs from promoted evidence"
            ]
            assert len(migrated) == 1 and migrated[0]["state"] == "resolved"
        elif migration_case == "stale-dependent-let-parent":
            quarantines = summary["false_decomposition_cleanup_quarantine"]
            assert quarantines[-1]["state"] == "quarantined"
            assert "current parent statement differs" in quarantines[-1]["reason"]
        else:
            replayed = [
                item
                for item in summary["false_decomposition_cleanup_transactions"]
                if item.get("transaction_id") == legacy_replay_transaction_id
            ]
            assert len(replayed) == 1 and replayed[0]["state"] == "quarantined"
            assert "parent full statement differs" in replayed[0]["reason"]


def _assert_cleaned(source: Path) -> None:
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    assert decomposition_provenance.declaration_slice(text, NEGATION_HELPER) is not None
    assert decomposition_provenance.declaration_slice(text, "valid_sibling") is not None
    assert "/-- Current parent documentation must survive proof restoration. -/" in text
    assert "@[category research open]\ntheorem parent_goal" in text
    restored = decomposition_provenance.declaration_slice(text, PARENT)
    expected = decomposition_provenance.declaration_slice(BEFORE_SOURCE, PARENT)
    assert restored is not None and expected is not None
    assert restored.declaration_sha256 == expected.declaration_sha256
    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(plan_state.node_id_for(HELPER, str(source))) is None
    parent = blueprint.node_by_id(plan_state.node_id_for(PARENT, str(source)))
    assert parent is not None and parent.status == "stated"
    expected = decomposition_provenance.declaration_slice(BEFORE_SOURCE, PARENT)
    assert expected is not None
    assert parent.statement == false_decomposition_cleanup._graph_statement(expected.text, PARENT)
    assert "Assigned declaration slice" not in parent.statement
    assert parent.owner == ""
    assert parent.statement == ": True"
    summary = plan_state.load_summary()
    assert summary.get("negation_promotions", []) == []
    assert summary["false_decomposition_cleanups"][-1]["promotion"]["theorem"] == HELPER


def _prepare_cleanup_transaction(monkeypatch, source: Path) -> tuple[dict, dict]:
    """Persist one valid pending cleanup and stop before any source mutation."""
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "pending-persisted":
            raise RuntimeError("prepared cleanup transaction")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="prepared cleanup transaction"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(source.parent), validate_promotion=_valid
        )
    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _s: None)
    transaction = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    return promotion, dict(transaction)


def test_zero_edge_false_helper_is_retracted_and_parent_reopened(cleanup_project):
    """Exact provenance handles the zero-edge graph state that stranded work."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.quarantined == 0
    _assert_cleaned(source)


def test_insertion_only_false_helper_cleans_when_parent_is_exactly_unchanged(cleanup_project):
    """An unchanged pre-edit parent authorizes a no-op restoration without a helper use."""
    _root, _state_root, source = cleanup_project
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "theorem parent_goal : True := by\n  exact False.elim bad_helper",
            "theorem parent_goal : True := by\n  sorry",
        ),
        encoding="utf-8",
    )
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.quarantined == 0
    _assert_cleaned(source)


def test_retryable_kernel_validation_remains_pending_then_cleans_automatically(cleanup_project):
    """Missing project imports must not create a permanent cleanup quarantine."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    before_source = source.read_bytes()
    before_summary = deepcopy(plan_state.load_summary())
    before_blueprint = plan_state.load_blueprint()
    attempts = 0

    def transient_then_valid(_candidate):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return negation_promotion.PromotionResult(
                False,
                "fresh source rerun did not elaborate the exact negation",
                failure_kind="project_environment_unavailable",
                retryable=True,
            )
        return negation_promotion.PromotionResult(True, "fresh negation is valid")

    pending = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion],
        cwd=str(source.parent),
        validate_promotion=transient_then_valid,
    )

    assert pending.pending == 1
    assert pending.quarantined == 0
    assert "awaits retry" in pending.reasons[0]
    assert source.read_bytes() == before_source
    assert plan_state.load_summary() == before_summary
    assert plan_state.load_blueprint() == before_blueprint

    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion],
        cwd=str(source.parent),
        validate_promotion=transient_then_valid,
    )

    assert attempts == 2
    assert cleaned.cleaned == 1
    assert cleaned.pending == 0
    assert cleaned.quarantined == 0
    _assert_cleaned(source)


def test_comments_strings_and_prefix_identifiers_are_not_helper_references(cleanup_project):
    """Lexical lookalikes outside the parent cannot manufacture dependencies."""
    _root, _state_root, source = cleanup_project
    harmless = """theorem harmless_names : True := by
  let note := "bad_helper"
  -- bad_helper is mentioned only in prose.
  have bad_helper_suffix : True := by trivial
  exact bad_helper_suffix

"""
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "/-- Current parent documentation must survive proof restoration. -/",
            harmless + "/-- Current parent documentation must survive proof restoration. -/",
        ),
        encoding="utf-8",
    )
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert "theorem harmless_names" in source.read_text(encoding="utf-8")
    _assert_cleaned(source)


@pytest.mark.parametrize(
    "replacement",
    [
        '  let note := "bad_helper"\n  trivial',
        "  -- bad_helper\n  trivial",
        "  have bad_helper_suffix : True := by trivial\n  exact bad_helper_suffix",
    ],
)
def test_parent_lookalike_reference_cannot_authorize_restoration(cleanup_project, replacement):
    """Only an exact proof identifier authorizes retracting the helper."""
    _root, _state_root, source = cleanup_project
    source.write_text(
        source.read_text(encoding="utf-8").replace("  exact False.elim bad_helper", replacement),
        encoding="utf-8",
    )
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    assert (
        "proof no longer references"
        in plan_state.load_summary()["false_decomposition_cleanup_quarantine"][-1]["reason"]
    )


def test_same_file_external_dependent_blocks_cleanup(cleanup_project):
    """A second declaration using the helper prevents destructive retraction."""
    _root, _state_root, source = cleanup_project
    dependent = "theorem external_user : False := by\n  exact bad_helper\n\n"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "/-- Current parent documentation must survive proof restoration. -/",
            dependent + "/-- Current parent documentation must survive proof restoration. -/",
        ),
        encoding="utf-8",
    )
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    assert (
        "same-file"
        in plan_state.load_summary()["false_decomposition_cleanup_quarantine"][-1]["reason"]
    )


def test_zero_edge_decomposer_helper_promotes_as_sublemma_and_cleans_immediately(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_project,
) -> None:
    """Missing split edges can never turn a campaign helper into a main disproof."""
    _root, _state_root, source = cleanup_project
    _seed_current_provenance(source)
    _seed_graph(source, helper_status="proving")

    def scratch(code: str, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' does not depend on any axioms",
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)

    promoted = negation_promotion.promote_source_negation(
        theorem_id=HELPER,
        file_label=str(source),
        proof_declaration=NEGATION_HELPER,
        cwd=str(source.parent),
    )

    assert promoted.ok is True
    assert promoted.is_main_goal is False
    _assert_cleaned(source)
    startup = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(source.parent),
        target_symbol=PARENT,
        active_file=str(source),
    )
    assert startup.terminal_disproof is False


def test_multiline_crlf_helper_promotion_cleans_without_becoming_terminal(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_project,
) -> None:
    """CRLF ownership and LF probe identity agree without rewriting source bytes."""
    _root, _state_root, source = cleanup_project
    before_source = (
        "namespace Demo\r\n\r\n"
        "/-- Original parent declaration. -/\r\n"
        "theorem parent_goal : True := by\r\n"
        "  sorry\r\n\r\n"
        "end Demo\r\n"
    )
    helper_stub = "theorem bad_helper\r\n" "    (n : Nat) :\r\n" "    n < 0 := by\r\n" "  sorry"
    inserted_source = before_source.replace(
        "theorem parent_goal",
        f"{helper_stub}\r\n\r\ntheorem parent_goal",
    )
    current_source = (
        "namespace Demo\r\n\r\n"
        "theorem neg_bad_helper : ¬ (∀ n : Nat, n < 0) := by\r\n"
        "  omega\r\n\r\n"
        f"{helper_stub}\r\n\r\n"
        "/-- Parent résumé and λ marker must survive byte-for-byte. -/\r\n"
        "theorem parent_goal : True := by\r\n"
        "  have impossible := bad_helper 0\r\n"
        "  omega\r\n\r\n"
        "end Demo\r\n"
    )
    source.write_bytes(current_source.encode("utf-8"))
    with decomposition_provenance.source_operation(source) as operation:
        provenance = decomposition_provenance.begin_decomposition(
            active_file=str(source),
            target_symbol=PARENT,
            skeletons=[helper_stub],
            before_text=before_source,
            after_text=inserted_source,
            before_bytes=before_source.encode("utf-8"),
            after_bytes=inserted_source.encode("utf-8"),
            cwd=str(source.parent),
            operation=operation,
        )
        assert decomposition_provenance.finish_decomposition(
            str(provenance["transaction_id"]), state="committed"
        )
    _seed_graph(source, helper_status="proving")

    helper = decomposition_provenance.declaration_slice(current_source, HELPER)
    goal = negation_probe.build_negation_goal(str(source), HELPER, cwd=str(source.parent))
    assert helper is not None
    assert not isinstance(goal, dict)
    assert "\r\n" in helper.signature
    assert "\r" not in goal.original
    assert helper.signature_sha256 == hashlib.sha256(goal.original.encode("utf-8")).hexdigest()

    def scratch(code: str, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' does not depend on any axioms",
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)
    promoted = negation_promotion.promote_source_negation(
        theorem_id=HELPER,
        file_label=str(source),
        proof_declaration=NEGATION_HELPER,
        cwd=str(source.parent),
    )

    assert promoted.ok is True
    assert promoted.is_main_goal is False
    cleaned_bytes = source.read_bytes()
    line_ending_residue = cleaned_bytes.replace(b"\r\n", b"")
    assert b"\r" not in line_ending_residue
    assert b"\n" not in line_ending_residue
    marker = "Parent résumé and λ marker must survive byte-for-byte.".encode()
    assert marker in cleaned_bytes
    cleaned_text = cleaned_bytes.decode("utf-8")
    assert decomposition_provenance.declaration_slice(cleaned_text, HELPER) is None
    assert decomposition_provenance.declaration_slice(cleaned_text, NEGATION_HELPER) is not None
    restored = decomposition_provenance.declaration_slice(cleaned_text, PARENT)
    original = decomposition_provenance.declaration_slice(before_source, PARENT)
    assert restored is not None and original is not None
    assert restored.declaration_sha256 == original.declaration_sha256

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(plan_state.node_id_for(HELPER, str(source))) is None
    parent = blueprint.node_by_id(plan_state.node_id_for(PARENT, str(source)))
    assert parent is not None and parent.status == "stated"
    summary = plan_state.load_summary()
    assert summary.get("negation_promotions", []) == []
    cleanup = summary["false_decomposition_cleanups"][-1]
    assert cleanup["state"] == "committed"
    assert cleanup["helper"] == HELPER
    assert cleanup["parent"] == PARENT

    startup = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(source.parent),
        target_symbol=PARENT,
        active_file=str(source),
    )
    assert startup.terminal_disproof is False
    assert plan_state.load_summary().get("final_report", {}).get("status") != "disproved"


def test_committed_cleanup_retires_ghost_outcome_and_reopens_parent(
    cleanup_project,
) -> None:
    """Queue replay cannot resurrect a deleted false helper or block later success."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )
    assert cleaned.cleaned == 1

    manager = TheoremQueueManager()
    manager.replace_queue([QueueItem(label=HELPER), QueueItem(label=PARENT)])
    manager.assign(QueueItem(label=HELPER), active_file=str(source))
    helper_key = manager.current.key
    parent_key = TheoremKey.make(PARENT, str(source))
    manager.record_attempt(cycle=2, proof_shape="exact bad_helper", reason="false")
    manager.record_outcome(status="disproved", note="authoritative negation")
    manager.record_outcome_for(
        parent_key,
        status="invalidated-by-dependency",
        note="depended on false helper",
    )
    autonomy_state = manager.to_autonomy_state()
    plan_state.save_queue_manager_state(manager.to_checkpoint_state())

    reconciled = runner._reconcile_false_decomposition_queue_state(autonomy_state)

    assert reconciled == (helper_key,)
    assert "current_queue_assignment" not in autonomy_state
    outcomes = autonomy_state["theorem_outcomes"]
    assert helper_key.storage_key() not in outcomes
    assert outcomes[parent_key.storage_key()]["status"] == "unresolved"
    assert helper_key.storage_key() not in plan_state.load_queue_manager_state().get(
        "theorem_outcomes", {}
    )

    assert runner._maybe_sync_plan_state(autonomy_state, None) is True
    assert (
        plan_state.load_blueprint().node_by_id(plan_state.node_id_for(HELPER, str(source))) is None
    )
    assert all(node.name != HELPER for node in plan_state.load_blueprint().nodes)

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "theorem parent_goal : True := by\n  sorry",
            "theorem parent_goal : True := by\n  trivial",
        ),
        encoding="utf-8",
    )
    runner._reconcile_false_decomposition_queue_state(autonomy_state)
    assert parent_key.storage_key() not in autonomy_state.get("theorem_outcomes", {})
    assert runner._has_unresolved_theorem_outcomes(autonomy_state) is False
    runner._maybe_sync_plan_state(autonomy_state, None)
    assert all(node.name != HELPER for node in plan_state.load_blueprint().nodes)


def test_committed_cleanup_replay_preserves_later_parent_assignment(
    cleanup_project,
    monkeypatch,
) -> None:
    """A completed queue replay cannot clear a parent reassigned on a later run."""
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )
    assert cleaned.cleaned == 1

    manager = TheoremQueueManager()
    manager.replace_queue([QueueItem(label=HELPER), QueueItem(label=PARENT)])
    manager.assign(QueueItem(label=HELPER), active_file=str(source))
    autonomy_state = manager.to_autonomy_state()
    runner._flush_queue_manager(autonomy_state, manager)

    runner._reconcile_false_decomposition_queue_state(autonomy_state)
    events.clear()
    resumed = TheoremQueueManager.from_autonomy_state(autonomy_state)
    resumed.replace_queue([QueueItem(label=PARENT)])
    resumed.assign(QueueItem(label=PARENT), active_file=str(source))
    runner._flush_queue_manager(autonomy_state, resumed)
    assert runner._maybe_sync_plan_state(autonomy_state, None) is True
    parent_id = plan_state.node_id_for(PARENT, str(source))
    assert plan_state.load_blueprint().node_by_id(parent_id).status == "proving"

    runner._reconcile_false_decomposition_queue_state(autonomy_state)
    assert runner._maybe_sync_plan_state(autonomy_state, None) is True

    assignment = autonomy_state.get("current_queue_assignment")
    assert isinstance(assignment, dict)
    assert assignment["target_symbol"] == PARENT
    assert assignment["active_file"] == str(source)
    assert plan_state.load_blueprint().node_by_id(parent_id).status == "proving"
    assert not any(args[0] == "false-decomposition-queue-reconciled" for args, _kwargs in events)


@pytest.mark.parametrize(
    "crash_stage",
    ["pending-persisted", "source-persisted", "graph-persisted", "committed"],
)
def test_cleanup_transaction_replays_each_restart_window(monkeypatch, cleanup_project, crash_stage):
    """Every source/graph/summary crash boundary converges idempotently."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == crash_stage:
            raise RuntimeError(f"crash at {stage}")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash at"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(source.parent), validate_promotion=_valid
        )

    monkeypatch.setattr(
        false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _stage: None
    )
    false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    _assert_cleaned(source)
    committed = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert committed["state"] == "committed"


@pytest.mark.parametrize("drift_kind", ["readded-helper", "reassigned-helper-id"])
def test_graph_persisted_drift_cannot_finalize_cleanup(monkeypatch, cleanup_project, drift_kind):
    """A writer racing after graph persistence leaves durable work for safe replay."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    helper_id = plan_state.node_id_for(HELPER, str(source))

    def drift_after_graph(stage):
        if stage != "graph-persisted":
            return
        blueprint = plan_state.load_blueprint()
        if drift_kind == "readded-helper":
            raced = GraphNode(
                id=helper_id,
                name=HELPER,
                file=str(source),
                status="false",
                generated_by="decomposer",
            )
        else:
            raced = GraphNode(
                id=helper_id,
                name="unrelated_user_node",
                file=str(source),
                status="stated",
                generated_by="human",
            )
        plan_state.save_blueprint(
            Blueprint(
                nodes=(*blueprint.nodes, raced),
                edges=blueprint.edges,
                revision=blueprint.revision,
            )
        )

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        drift_after_graph,
    )
    raced = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert raced.cleaned == 0
    assert raced.pending == 1
    assert raced.quarantined == 0
    assert (
        decomposition_provenance.declaration_slice(source.read_text(encoding="utf-8"), HELPER)
        is None
    )
    summary = plan_state.load_summary()
    assert summary["false_decomposition_cleanup_transactions"][-1]["state"] == "pending"
    assert summary["negation_promotions"][-1]["promotion_id"] == promotion["promotion_id"]
    assert plan_state.load_blueprint().node_by_id(helper_id) is not None

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        lambda _stage: None,
    )
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=tuple(node for node in blueprint.nodes if node.id != helper_id),
            edges=blueprint.edges,
            revision=blueprint.revision,
        )
    )
    replayed = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert replayed.cleaned == 1
    assert replayed.pending == 0
    _assert_cleaned(source)


@pytest.mark.parametrize("alias_kind", ["file", "canonical_file", "key"])
def test_immutable_promotion_path_rejects_alias_retarget(cleanup_project, alias_kind):
    """A new promotion cannot redirect cleanup away from its operation_path."""
    root, _state_root, source = cleanup_project
    retarget = root / "Retarget.lean"
    retarget.write_text(CURRENT_SOURCE, encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    if alias_kind == "key":
        promotion["key"] = f"{retarget}::{HELPER}"
    else:
        promotion[alias_kind] = str(retarget)
    source_before = source.read_bytes()
    retarget_before = retarget.read_bytes()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.pending == 0
    assert result.quarantined == 1
    assert source.read_bytes() == source_before
    assert retarget.read_bytes() == retarget_before
    assert (
        plan_state.load_blueprint().node_by_id(plan_state.node_id_for(HELPER, str(source)))
        is not None
    )


def test_bound_relative_graph_file_cleans_under_absolute_source_lease(cleanup_project):
    """Source and graph identities remain distinct while referring to one file."""
    root, _state_root, source = cleanup_project
    promotion = _relative_graph_promotion(source)
    _seed_current_provenance(source)
    _seed_relative_graph(source)
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.pending == 0
    assert result.quarantined == 0
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    expected_parent = decomposition_provenance.declaration_slice(BEFORE_SOURCE, PARENT)
    restored_parent = decomposition_provenance.declaration_slice(text, PARENT)
    assert expected_parent is not None and restored_parent is not None
    assert restored_parent.declaration_sha256 == expected_parent.declaration_sha256
    graph = plan_state.load_blueprint()
    assert graph.node_by_id(plan_state.node_id_for(HELPER, source.name)) is None
    parent = graph.node_by_id(plan_state.node_id_for(PARENT, source.name))
    assert parent is not None and parent.status == "stated" and parent.owner == ""
    committed = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert committed["file"] == str(source)
    assert committed["graph_file"] == source.name


def test_graph_file_alias_ambiguity_quarantines_without_source_edit(cleanup_project):
    """Two graph labels resolving to one theorem/source cannot authorize deletion."""
    root, _state_root, source = cleanup_project
    promotion = _relative_graph_promotion(source)
    _seed_current_provenance(source)
    _seed_relative_graph(source)
    graph = plan_state.load_blueprint()
    alias = GraphNode(
        id=plan_state.node_id_for(HELPER, str(source)),
        name=HELPER,
        file=str(source),
        status="false",
        generated_by="decomposer",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*graph.nodes, alias),
            edges=graph.edges,
            revision=graph.revision,
        )
    )
    _seed_promotion(promotion)
    before = source.read_bytes()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.pending == 0
    assert result.quarantined == 1
    assert any("ambiguous file aliases" in reason for reason in result.reasons)
    assert source.read_bytes() == before
    graph = plan_state.load_blueprint()
    assert graph.node_by_id(plan_state.node_id_for(HELPER, source.name)) is not None
    assert graph.node_by_id(plan_state.node_id_for(HELPER, str(source))) is not None


def test_pending_transaction_retarget_fails_fingerprint_before_source_open(
    monkeypatch, cleanup_project
):
    """Coordinated durable-path drift cannot replay a sealed cleanup elsewhere."""
    root, _state_root, source = cleanup_project
    retarget = root / "Retarget.lean"
    retarget.write_text(CURRENT_SOURCE, encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash_after_prepare(stage):
        if stage == "pending-persisted":
            raise RuntimeError("crash after prepare")

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        crash_after_prepare,
    )
    with pytest.raises(RuntimeError, match="crash after prepare"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(root), validate_promotion=_valid
        )
    source_before = source.read_bytes()
    retarget_before = retarget.read_bytes()

    def retarget_pending(summary):
        transaction = summary["false_decomposition_cleanup_transactions"][-1]
        transaction["file"] = str(retarget)
        transaction["helper_node_id"] = plan_state.node_id_for(HELPER, str(retarget))
        transaction["parent_node_id"] = plan_state.node_id_for(PARENT, str(retarget))

    update_json_file(plan_state.plan_state_paths().summary_json, retarget_pending)
    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        lambda _stage: None,
    )
    monkeypatch.setattr(
        decomposition_provenance,
        "source_operation",
        lambda *_args, **_kwargs: pytest.fail(
            "sealed transaction drift reached the source-open boundary"
        ),
    )

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result.cleaned == 0
    assert result.pending == 1
    assert result.quarantined == 0
    assert source.read_bytes() == source_before
    assert retarget.read_bytes() == retarget_before
    summary = plan_state.load_summary()
    transaction = summary["false_decomposition_cleanup_transactions"][-1]
    assert transaction["state"] == "pending"
    assert "reason" not in transaction
    assert not summary.get("false_decomposition_cleanup_quarantine")
    assert any("ambiguous false-decomposition" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "drift_kind",
    [
        "source-after",
        "restored-parent",
        "restored-statement",
        "promotion-evidence",
        "promotion-graph-binding",
    ],
)
def test_pending_transaction_authenticates_all_replay_payloads(
    monkeypatch, cleanup_project, drift_kind
):
    """Replay rejects payload drift even when its top-level path remains unchanged."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash_after_prepare(stage):
        if stage == "pending-persisted":
            raise RuntimeError("crash after prepare")

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        crash_after_prepare,
    )
    with pytest.raises(RuntimeError, match="crash after prepare"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(root), validate_promotion=_valid
        )
    source_before = source.read_bytes()

    def drift_pending(summary):
        transaction = summary["false_decomposition_cleanup_transactions"][-1]
        if drift_kind == "source-after":
            transaction["source_after"] += "\n"
        elif drift_kind == "restored-parent":
            transaction["parent_restored_declaration"] = transaction[
                "parent_restored_declaration"
            ].replace("sorry", "trivial")
        elif drift_kind == "restored-statement":
            transaction["parent_restored_statement"] = "tampered graph proposition"
        elif drift_kind == "promotion-evidence":
            transaction["promotion"]["proof_tactic"] = "exact attacker_proof"
        else:
            transaction["promotion"]["graph_node_file"] = "Retarget.lean"

    update_json_file(plan_state.plan_state_paths().summary_json, drift_pending)
    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        lambda _stage: None,
    )

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result.cleaned == 0
    assert result.pending == 1
    assert result.quarantined == 0
    assert source.read_bytes() == source_before
    summary = plan_state.load_summary()
    transaction = summary["false_decomposition_cleanup_transactions"][-1]
    assert transaction["state"] == "pending"
    assert "reason" not in transaction
    assert not summary.get("false_decomposition_cleanup_quarantine")
    assert any("ambiguous false-decomposition" in reason for reason in result.reasons)


@pytest.mark.parametrize("edge_kind", ["depends_on", "evidence"])
def test_external_or_evidence_graph_edge_is_preserved_and_blocks_cleanup(
    cleanup_project, edge_kind
):
    """Cleanup never erases another dependent or a forensic evidence edge."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    blueprint = plan_state.load_blueprint()
    external = GraphNode(
        id=plan_state.node_id_for("external_user", str(source)),
        name="external_user",
        file=str(source),
        status="stated",
    )
    helper_id = plan_state.node_id_for(HELPER, str(source))
    edge = GraphEdge(source=external.id, target=helper_id, kind=edge_kind)
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, external),
            edges=(*blueprint.edges, edge),
            revision=blueprint.revision,
        )
    )
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    preserved = plan_state.load_blueprint()
    assert edge in preserved.edges
    assert preserved.node_by_id(helper_id) is not None


@pytest.mark.parametrize("edge_direction", ["parent-to-helper", "helper-to-parent"])
def test_partial_parent_structural_pair_blocks_cleanup(cleanup_project, edge_direction):
    """One half of decomposer parent ownership is ambiguous, unlike zero or a full pair."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent_id = plan_state.node_id_for(PARENT, str(source))
    edge = (
        GraphEdge(source=parent_id, target=helper_id, kind="depends_on")
        if edge_direction == "parent-to-helper"
        else GraphEdge(source=helper_id, target=parent_id, kind="split_of")
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=blueprint.nodes,
            edges=(*blueprint.edges, edge),
            revision=blueprint.revision,
        )
    )
    _seed_promotion(promotion)
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert any("complete owned edge pair" in reason for reason in result.reasons)
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before


@pytest.mark.parametrize("identity_kind", ["evidence", "nested"])
def test_forged_incident_graph_node_id_blocks_cleanup(cleanup_project, identity_kind):
    """Name/file lookalikes cannot impersonate stable proof or nested-helper identities."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    if identity_kind == "evidence":
        forged = GraphNode(
            id="forged-evidence-id",
            name=NEGATION_HELPER,
            file=str(source),
            status="proved",
            generated_by="prover-edit",
        )
        edges = (GraphEdge(source=forged.id, target=helper_id, kind="evidence"),)
    else:
        forged = GraphNode(
            id="forged-nested-id",
            name="positive_repair",
            file=str(source),
            status="proved",
            generated_by="decomposer",
        )
        edges = (
            GraphEdge(source=helper_id, target=forged.id, kind="depends_on"),
            GraphEdge(source=forged.id, target=helper_id, kind="split_of"),
        )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, forged),
            edges=(*blueprint.edges, *edges),
            revision=blueprint.revision,
        )
    )
    _seed_promotion(promotion)
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before


def _seed_authenticated_negation_evidence_and_nested_decomposition(
    source: Path,
) -> tuple[GraphEdge, GraphNode]:
    """Add the exact live-run negation evidence and one owned nested helper."""
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent_id = plan_state.node_id_for(PARENT, str(source))
    source_text = source.read_text(encoding="utf-8")
    evidence_declaration = decomposition_provenance.declaration_slice(source_text, NEGATION_HELPER)
    assert evidence_declaration is not None
    evidence_node = GraphNode(
        id=plan_state.node_id_for(NEGATION_HELPER, str(source)),
        name=NEGATION_HELPER,
        file=str(source),
        statement=evidence_declaration.text,
        source_sha256=_sha256(source_text),
        status="proved",
        generated_by="prover-edit",
    )
    nested = GraphNode(
        id=plan_state.node_id_for("positive_repair", str(source)),
        name="positive_repair",
        file=str(source),
        status="proved",
        generated_by="decomposer",
    )
    evidence = GraphEdge(source=evidence_node.id, target=helper_id, kind="evidence")
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, evidence_node, nested),
            edges=(
                *blueprint.edges,
                GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                GraphEdge(source=helper_id, target=nested.id, kind="depends_on"),
                GraphEdge(source=nested.id, target=helper_id, kind="split_of"),
                evidence,
            ),
            revision=blueprint.revision,
        )
    )
    return evidence, nested


def _seed_live_multi_evidence_and_dependent_tombstone(
    source: Path,
    promotion: dict[str, object],
    *,
    evidence_status: str = "proved",
    evidence_generated_by: str = "prover-edit",
    evidence_source_sha256: str = "",
    dependent_status: str = "conjectured",
) -> tuple[tuple[GraphEdge, ...], GraphEdge]:
    """Add the exact live shape: promoted proof, extra evidence, and dependent."""
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent_id = plan_state.node_id_for(PARENT, str(source))
    source_sha256 = str(evidence_source_sha256 or promotion.get("source_revision_sha256") or "")
    source_text = source.read_text(encoding="utf-8")
    promoted_declaration = decomposition_provenance.declaration_slice(source_text, NEGATION_HELPER)
    assert promoted_declaration is not None
    promoted_proof = GraphNode(
        id=plan_state.node_id_for(NEGATION_HELPER, str(source)),
        name=NEGATION_HELPER,
        file=str(source),
        statement=promoted_declaration.text,
        source_sha256=source_sha256,
        status="proved",
        generated_by="prover-edit",
    )
    extra_names = (
        "extra_verified_evidence",
        "extra_universal_obstruction",
        "extra_invariant",
        "extra_second_obstruction",
    )
    extra_evidence = tuple(
        GraphNode(
            id=plan_state.node_id_for(name, str(source)),
            name=name,
            file=str(source),
            statement=f"theorem {name} : True := by trivial",
            source_sha256=source_sha256,
            status=evidence_status,
            generated_by=evidence_generated_by,
        )
        for name in extra_names
    )
    dependent = GraphNode(
        id=plan_state.node_id_for("dependent_candidate", str(source)),
        name="dependent_candidate",
        file=str(source),
        statement="theorem dependent_candidate : True := by sorry",
        source_sha256=str(promotion.get("source_revision_sha256") or ""),
        status=dependent_status,
        generated_by="decomposer",
    )
    promoted_edge = GraphEdge(source=promoted_proof.id, target=helper_id, kind="evidence")
    extra_edges = tuple(
        GraphEdge(source=node.id, target=helper_id, kind="evidence") for node in extra_evidence
    )
    dependent_edge = GraphEdge(source=dependent.id, target=helper_id, kind="depends_on")
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, promoted_proof, *extra_evidence, dependent),
            edges=(
                GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                promoted_edge,
                *extra_edges,
                dependent_edge,
            ),
            revision=blueprint.revision,
        )
    )
    return (promoted_edge, *extra_edges), dependent_edge


def _live_multi_evidence_source() -> str:
    """Return a source revision matching all five live evidence graph nodes."""
    return CURRENT_SOURCE.replace(
        "theorem neg_bad_helper",
        "theorem extra_verified_evidence : True := by trivial\n\n"
        "theorem extra_universal_obstruction : True := by trivial\n\n"
        "theorem extra_invariant : True := by trivial\n\n"
        "theorem extra_second_obstruction : True := by trivial\n\n"
        "theorem dependent_candidate : True := by sorry\n\n"
        "theorem neg_bad_helper",
    )


def test_authenticated_negation_evidence_survives_structural_cleanup(cleanup_project):
    """Keep the exact proof-of-negation edge while reopening invalid decomposition."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    evidence, nested = _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.quarantined == 0
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    blueprint = plan_state.load_blueprint()
    helper = blueprint.node_by_id(plan_state.node_id_for(HELPER, str(source)))
    assert helper is not None and helper.status == "false"
    assert blueprint.node_by_id(nested.id) == nested
    assert evidence in blueprint.edges
    assert [
        edge
        for edge in blueprint.edges
        if helper.id in {edge.source, edge.target} and edge.kind != "evidence"
    ] == []
    parent = blueprint.node_by_id(plan_state.node_id_for(PARENT, str(source)))
    assert parent is not None and parent.status == "stated"
    journal = [
        json.loads(line)
        for line in plan_state.plan_state_paths()
        .journal_jsonl.read_text(encoding="utf-8")
        .splitlines()
    ]
    preservation = [
        event
        for event in journal
        if event.get("event") == "false-helper-negation-evidence-preserved"
    ]
    assert len(preservation) == 1
    assert preservation[0]["proof_declaration"] == NEGATION_HELPER
    assert preservation[0]["preserved_evidence_edges"] == [evidence.to_mapping()]
    assert len(preservation[0]["removed_structural_edges"]) == 4


def test_single_legacy_promotion_edge_without_graph_source_snapshot_remains_compatible(
    cleanup_project,
):
    """Keep old one-edge tombstones replayable under fresh promotion validation."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    evidence, _nested = _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    blueprint = plan_state.load_blueprint()
    evidence_node = blueprint.node_by_id(evidence.source)
    assert evidence_node is not None
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=tuple(
                (
                    replace(evidence_node, statement="", source_sha256="")
                    if node == evidence_node
                    else node
                )
                for node in blueprint.nodes
            ),
        )
    )
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.quarantined == 0
    assert evidence in plan_state.load_blueprint().edges


def test_live_multi_evidence_cleanup_invalidates_false_dependent_obligations(
    cleanup_project,
):
    """Remove false-dependent conjectures while retaining unrelated verified source."""
    root, _state_root, source = cleanup_project
    source.write_text(_live_multi_evidence_source(), encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    evidence_edges, dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(
        source, promotion
    )
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=(false_decomposition_cleanup._MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON),
        provenance_id=str(provenance["transaction_id"]),
    )

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.pending == 0
    assert result.quarantined == 0
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    assert "theorem extra_verified_evidence : True := by trivial" in text
    assert "theorem extra_universal_obstruction : True := by trivial" in text
    assert "theorem dependent_candidate : True := by sorry" not in text
    assert decomposition_provenance.declaration_slice(text, "valid_sibling") is not None
    blueprint = plan_state.load_blueprint()
    helper = blueprint.node_by_id(plan_state.node_id_for(HELPER, str(source)))
    assert helper is not None and helper.status == "false"
    assert all(edge in blueprint.edges for edge in evidence_edges)
    assert blueprint.node_by_id(dependent_edge.source) is None
    assert dependent_edge not in blueprint.edges
    assert [
        edge
        for edge in blueprint.edges
        if helper.id in {edge.source, edge.target} and edge not in {*evidence_edges}
    ] == []
    summary = plan_state.load_summary()
    committed = summary["false_decomposition_cleanup_transactions"][-1]
    assert committed["invalidated_dependents"] == [
        {
            "declaration_sha256": decomposition_provenance.declaration_slice(
                _live_multi_evidence_source(), "dependent_candidate"
            ).declaration_sha256,
            "file": str(source),
            "name": "dependent_candidate",
            "node_id": dependent_edge.source,
            "source_sha256": str(promotion["source_revision_sha256"]),
        }
    ]
    quarantine = summary["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "resolved"
    assert "verified same-revision prover evidence" in quarantine["resolution_reason"]
    journal = [
        json.loads(line)
        for line in plan_state.plan_state_paths()
        .journal_jsonl.read_text(encoding="utf-8")
        .splitlines()
    ]
    preservation = [
        event
        for event in journal
        if event.get("event") == "false-helper-negation-evidence-preserved"
    ]
    assert preservation[-1]["preserved_evidence_edges"] == [
        edge.to_mapping() for edge in evidence_edges
    ]
    assert preservation[-1]["invalidated_dependent_nodes"] == [dependent_edge.source]


def test_queue_replay_cannot_resurrect_an_invalidated_dependent(cleanup_project):
    """Retire deleted dependent queue state before plan sync can recreate its node."""
    root, _state_root, source = cleanup_project
    source.write_text(_live_multi_evidence_source(), encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_live_multi_evidence_and_dependent_tombstone(source, promotion)
    _seed_promotion(promotion)

    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )
    assert cleaned.cleaned == 1

    manager = TheoremQueueManager()
    manager.replace_queue(
        [
            QueueItem(label=HELPER),
            QueueItem(label="dependent_candidate"),
            QueueItem(label=PARENT),
        ]
    )
    manager.assign(QueueItem(label="dependent_candidate"), active_file=str(source))
    dependent_key = manager.current.key
    manager.record_attempt(cycle=7, proof_shape="exact bad_helper", reason="stale")
    manager.record_outcome(status="solved", note="stale pre-negation verdict")
    autonomy_state = manager.to_autonomy_state()
    plan_state.save_queue_manager_state(manager.to_checkpoint_state())

    runner._reconcile_false_decomposition_queue_state(autonomy_state)

    restored = runner._queue_manager_from_state(autonomy_state)
    assert restored.current is None
    assert restored.outcome_for(dependent_key) is None
    assert all(item.label != "dependent_candidate" for item in restored.queue)
    assert runner._maybe_sync_plan_state(autonomy_state, None) is True
    assert (
        plan_state.load_blueprint().node_by_id(
            plan_state.node_id_for("dependent_candidate", str(source))
        )
        is None
    )


def test_false_dependent_cleanup_replays_after_source_first_crash(monkeypatch, cleanup_project):
    """Replay the sealed graph invalidation after source deletion has persisted."""
    root, _state_root, source = cleanup_project
    source.write_text(_live_multi_evidence_source(), encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _evidence, dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(source, promotion)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "source-persisted":
            raise RuntimeError("crash after dependent source cleanup")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="dependent source cleanup"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(root), validate_promotion=_valid
        )

    source_after = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(source_after, "dependent_candidate") is None
    assert plan_state.load_blueprint().node_by_id(dependent_edge.source) is not None

    monkeypatch.setattr(
        false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _stage: None
    )
    recovered = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert recovered.cleaned == 1
    assert recovered.pending == 0
    assert plan_state.load_blueprint().node_by_id(dependent_edge.source) is None
    committed = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert committed["state"] == "committed"
    assert committed["invalidated_dependents"][0]["name"] == "dependent_candidate"


def test_false_dependent_cleanup_retires_transitive_conjecture_chain(cleanup_project):
    """Invalidate every unresolved decomposer descendant of the false condition."""
    root, _state_root, source = cleanup_project
    source_text = _live_multi_evidence_source().replace(
        "theorem neg_bad_helper",
        "theorem dependent_consequence : True := by sorry\n\n" "theorem neg_bad_helper",
    )
    source.write_text(source_text, encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _evidence, dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(source, promotion)
    consequence_declaration = decomposition_provenance.declaration_slice(
        source_text, "dependent_consequence"
    )
    assert consequence_declaration is not None
    consequence = GraphNode(
        id=plan_state.node_id_for("dependent_consequence", str(source)),
        name="dependent_consequence",
        file=str(source),
        statement=consequence_declaration.text,
        source_sha256=str(promotion["source_revision_sha256"]),
        status="conjectured",
        generated_by="decomposer",
    )
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=(*blueprint.nodes, consequence),
            edges=(
                *blueprint.edges,
                GraphEdge(
                    source=consequence.id,
                    target=dependent_edge.source,
                    kind="depends_on",
                ),
            ),
        )
    )
    _seed_promotion(promotion)

    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert cleaned.cleaned == 1
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, "dependent_candidate") is None
    assert decomposition_provenance.declaration_slice(text, "dependent_consequence") is None
    current = plan_state.load_blueprint()
    assert current.node_by_id(dependent_edge.source) is None
    assert current.node_by_id(consequence.id) is None
    committed = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert [item["name"] for item in committed["invalidated_dependents"]] == [
        "dependent_candidate",
        "dependent_consequence",
    ]


def test_false_dependent_cleanup_retires_untouched_stated_chain(cleanup_project):
    """Retire source-backed decomposer stubs that have not entered the queue yet."""
    root, _state_root, source = cleanup_project
    source_text = _live_multi_evidence_source().replace(
        "theorem neg_bad_helper",
        "theorem dependent_consequence : True := by sorry\n\n" "theorem neg_bad_helper",
    )
    source.write_text(source_text, encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _evidence, dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(
        source,
        promotion,
        dependent_status="stated",
    )
    consequence_declaration = decomposition_provenance.declaration_slice(
        source_text, "dependent_consequence"
    )
    assert consequence_declaration is not None
    consequence = GraphNode(
        id=plan_state.node_id_for("dependent_consequence", str(source)),
        name="dependent_consequence",
        file=str(source),
        statement=consequence_declaration.text,
        source_sha256=str(promotion["source_revision_sha256"]),
        status="stated",
        generated_by="decomposer",
    )
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=(*blueprint.nodes, consequence),
            edges=(
                *blueprint.edges,
                GraphEdge(
                    source=consequence.id,
                    target=dependent_edge.source,
                    kind="depends_on",
                ),
            ),
        )
    )
    _seed_promotion(promotion)

    cleaned = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert cleaned.cleaned == 1
    current = plan_state.load_blueprint()
    assert current.node_by_id(dependent_edge.source) is None
    assert current.node_by_id(consequence.id) is None
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, "dependent_candidate") is None
    assert decomposition_provenance.declaration_slice(text, "dependent_consequence") is None


@pytest.mark.parametrize("drift", [False, True])
def test_committed_v1_live_shape_migrates_or_quarantines_drift(cleanup_project, drift: bool):
    """Upgrade the exact stale tombstone shape retained by an older campaign."""
    root, _state_root, source = cleanup_project
    ordering_name = "dependent_ordering"
    planner_name = "planner_assembly"
    proved_name = "valid_sibling"
    source.write_text(
        _live_multi_evidence_source().replace(
            "theorem neg_bad_helper",
            f"theorem {ordering_name} : True := by sorry\n\n" "theorem neg_bad_helper",
        ),
        encoding="utf-8",
    )
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    evidence_edges, dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(
        source, promotion
    )
    source_text = source.read_text(encoding="utf-8")
    ordering_declaration = decomposition_provenance.declaration_slice(source_text, ordering_name)
    proved_declaration = decomposition_provenance.declaration_slice(source_text, proved_name)
    assert ordering_declaration is not None
    assert proved_declaration is not None
    blueprint = plan_state.load_blueprint()
    parent_id = plan_state.node_id_for(PARENT, str(source))
    dependent_id = dependent_edge.source
    ordering = GraphNode(
        id=plan_state.node_id_for(ordering_name, str(source)),
        name=ordering_name,
        file=str(source),
        statement=ordering_declaration.text,
        source_sha256=_sha256(source_text),
        status="conjectured",
        generated_by="decomposer",
    )
    planner = GraphNode(
        id=plan_state.node_id_for(planner_name, str(source)),
        name=planner_name,
        file=str(source),
        statement=f"theorem {planner_name} : True := by sorry",
        source_sha256="",
        status="conjectured",
        generated_by="planner",
    )
    proved = GraphNode(
        id=plan_state.node_id_for(proved_name, str(source)),
        name=proved_name,
        file=str(source),
        statement=proved_declaration.text,
        source_sha256=_sha256(source_text),
        status="proved",
        generated_by="decomposer",
    )
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=(*blueprint.nodes, ordering, planner, proved),
            edges=(
                *blueprint.edges,
                GraphEdge(source=dependent_id, target=parent_id, kind="split_of"),
                GraphEdge(source=parent_id, target=dependent_id, kind="depends_on"),
                GraphEdge(source=ordering.id, target=dependent_id, kind="depends_on"),
                GraphEdge(source=ordering.id, target=parent_id, kind="split_of"),
                GraphEdge(source=parent_id, target=ordering.id, kind="depends_on"),
                GraphEdge(source=planner.id, target=dependent_id, kind="depends_on"),
                GraphEdge(source=planner.id, target=ordering.id, kind="depends_on"),
                GraphEdge(source=planner.id, target=proved.id, kind="depends_on"),
                GraphEdge(source=planner.id, target=parent_id, kind="split_of"),
                GraphEdge(source=parent_id, target=proved.id, kind="depends_on"),
                GraphEdge(source=proved.id, target=parent_id, kind="split_of"),
            ),
        )
    )
    _seed_promotion(promotion)

    legacy, reason = false_decomposition_cleanup._build_source_transaction(
        promotion,
        provenance,
        current_source=source.read_text(encoding="utf-8"),
        file_identity=str(source),
    )
    assert legacy is not None, reason
    legacy.update(
        {
            "version": 1,
            "helper_node_id": plan_state.node_id_for(HELPER, str(source)),
            "parent_node_id": plan_state.node_id_for(PARENT, str(source)),
            "graph_file": str(source),
            "ownership_basis": "decomposer-graph",
        }
    )
    legacy.pop("invalidated_dependents")
    legacy_source_after = str(legacy["source_after"])
    sealed = false_decomposition_cleanup._seal_transaction(legacy)
    committed_v1 = {
        **sealed,
        "state": "committed",
        "committed_at": "2026-07-18T00:00:00+00:00",
    }
    committed_v1.pop("source_after")
    source.write_text(legacy_source_after, encoding="utf-8")

    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent = blueprint.node_by_id(parent_id)
    assert parent is not None
    restored_parent = decomposition_provenance.declaration_slice(legacy_source_after, PARENT)
    assert restored_parent is not None
    parent_edges = {
        GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
        GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
    }
    current_source_sha256 = _sha256(legacy_source_after)
    source_bound_ids = {
        parent_id,
        dependent_id,
        ordering.id,
        proved.id,
        *(edge.source for edge in evidence_edges),
    }
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=tuple(
                (
                    replace(
                        parent,
                        statement=false_decomposition_cleanup._graph_statement(
                            restored_parent.text, PARENT
                        ),
                        source_sha256=_sha256(legacy_source_after),
                        status="stated",
                        owner="",
                    )
                    if node.id == parent_id
                    else (
                        replace(node, source_sha256=current_source_sha256)
                        if node.id in source_bound_ids
                        else node
                    )
                )
                for node in blueprint.nodes
            ),
            edges=tuple(edge for edge in blueprint.edges if edge not in parent_edges),
        )
    )

    def seed_legacy_cleanup(summary):
        summary["negation_promotions"] = []
        summary["false_decomposition_cleanup_transactions"] = [committed_v1]
        summary["false_decomposition_cleanups"] = [committed_v1]

    update_json_file(plan_state.plan_state_paths().summary_json, seed_legacy_cleanup)

    if drift:
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "theorem dependent_candidate : True := by sorry",
                "theorem dependent_candidate : False := by sorry",
            ),
            encoding="utf-8",
        )
    source_before_migration = source.read_bytes()
    graph_before_migration = plan_state.load_blueprint()

    migrated = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    if drift:
        assert migrated.cleaned == 0
        assert migrated.quarantined == 1
        assert source.read_bytes() == source_before_migration
        assert plan_state.load_blueprint() == graph_before_migration
        summary = plan_state.load_summary()
        assert summary["false_decomposition_cleanup_transactions"][-1] == committed_v1
        assert (
            "committed cleanup dependent migration"
            in summary["false_decomposition_cleanup_quarantine"][-1]["reason"]
        )
        return

    assert migrated.cleaned == 1
    assert migrated.pending == 0
    text = source.read_text(encoding="utf-8")
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    assert decomposition_provenance.declaration_slice(text, "dependent_candidate") is None
    assert decomposition_provenance.declaration_slice(text, ordering_name) is None
    assert decomposition_provenance.declaration_slice(text, "valid_sibling") is not None
    current = plan_state.load_blueprint()
    assert current.node_by_id(dependent_id) is None
    assert current.node_by_id(ordering.id) is None
    assert current.node_by_id(planner.id) is None
    current_parent = current.node_by_id(parent_id)
    assert current_parent is not None and current_parent.status == "stated"
    assert current.node_by_id(proved.id) is not None
    assert all(edge in current.edges for edge in evidence_edges)
    upgraded = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert upgraded["version"] == 3
    assert upgraded["state"] == "committed"
    assert upgraded["migration_from_transaction_id"] == committed_v1["transaction_id"]
    assert [record["name"] for record in upgraded["invalidated_dependents"]] == [
        "dependent_candidate",
        ordering_name,
        planner_name,
    ]
    assert [record["source_kind"] for record in upgraded["invalidated_dependents"]] == [
        "source_obligation",
        "source_obligation",
        "graph_artifact",
    ]

    source_after = source.read_bytes()
    graph_after = current
    repeated = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))
    assert repeated.cleaned == 0
    assert source.read_bytes() == source_after
    assert plan_state.load_blueprint() == graph_after


def _seed_evidence_only_committed_v1_tombstone(cleanup_project):
    """Return an old committed cleanup whose false tombstone has only stale evidence."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    source_text = source.read_text(encoding="utf-8")
    evidence_declaration = decomposition_provenance.declaration_slice(source_text, NEGATION_HELPER)
    assert evidence_declaration is not None
    evidence = GraphNode(
        id=plan_state.node_id_for(NEGATION_HELPER, str(source)),
        name=NEGATION_HELPER,
        file=str(source),
        statement=evidence_declaration.text,
        source_sha256=_sha256(source_text),
        status="proved",
        generated_by="prover-edit",
    )
    evidence_edge = GraphEdge(
        source=evidence.id,
        target=plan_state.node_id_for(HELPER, str(source)),
        kind="evidence",
    )
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=(*blueprint.nodes, evidence),
            edges=(evidence_edge,),
        )
    )
    _seed_promotion(promotion)
    first = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )
    assert first.cleaned == 1

    summary = plan_state.load_summary()
    committed_v2 = summary["false_decomposition_cleanup_transactions"][-1]
    legacy = dict(committed_v2)
    legacy["version"] = 1
    for field in (
        "immutable_fingerprint",
        "invalidated_dependents",
        "invalidated_dependents_sha256",
        "promotion_evidence_sha256",
        "transaction_id",
    ):
        legacy.pop(field, None)
    legacy = false_decomposition_cleanup._seal_transaction(legacy)

    def seed_legacy(summary):
        summary["false_decomposition_cleanup_transactions"] = [legacy]
        summary["false_decomposition_cleanups"] = [legacy]

    update_json_file(plan_state.plan_state_paths().summary_json, seed_legacy)
    current = plan_state.load_blueprint()
    plan_state.save_blueprint(
        replace(
            current,
            nodes=tuple(
                replace(node, source_sha256="0" * 64) if node.id == evidence.id else node
                for node in current.nodes
            ),
        )
    )
    return root, source, legacy


def test_committed_v1_evidence_only_tombstone_is_no_write_noop(cleanup_project):
    """Do not revalidate stale audit evidence when no dependent branch remains."""
    root, source, legacy = _seed_evidence_only_committed_v1_tombstone(cleanup_project)
    summary_path = plan_state.plan_state_paths().summary_json
    summary_before = summary_path.read_bytes()
    stat_before = summary_path.stat()
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result == false_decomposition_cleanup.CleanupReconciliation()
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    assert summary_path.read_bytes() == summary_before
    stat_after = summary_path.stat()
    assert stat_after.st_ino == stat_before.st_ino
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns
    summary = plan_state.load_summary()
    assert summary["false_decomposition_cleanup_transactions"] == [legacy]
    assert not summary.get("false_decomposition_cleanup_quarantine")


def test_committed_v1_no_work_recovery_resolves_exact_spurious_quarantine(
    cleanup_project,
):
    """Resolve only the obsolete evidence error previously emitted for a no-work tombstone."""
    root, source, legacy = _seed_evidence_only_committed_v1_tombstone(cleanup_project)
    false_decomposition_cleanup._quarantine_candidate(
        legacy["promotion"],
        reason=false_decomposition_cleanup._NO_WORK_LEGACY_MIGRATION_QUARANTINE_REASON,
        provenance_id=legacy["provenance_id"],
    )
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result == false_decomposition_cleanup.CleanupReconciliation()
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    summary = plan_state.load_summary()
    assert summary["false_decomposition_cleanup_transactions"] == [legacy]
    quarantine = summary["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "resolved"
    assert quarantine["reason"] == (
        false_decomposition_cleanup._NO_WORK_LEGACY_MIGRATION_QUARANTINE_REASON
    )
    assert "no remaining structural dependent migration work" in quarantine["resolution_reason"]


def test_committed_v1_no_work_recovery_preserves_other_quarantine(cleanup_project):
    """Do not use the no-work migration rule to forgive a different ambiguity."""
    root, source, legacy = _seed_evidence_only_committed_v1_tombstone(cleanup_project)
    other_reason = "committed cleanup dependent migration: source identity drifted"
    false_decomposition_cleanup._quarantine_candidate(
        legacy["promotion"],
        reason=other_reason,
        provenance_id=legacy["provenance_id"],
    )
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result.quarantined == 1
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    quarantine = plan_state.load_summary()["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "quarantined"
    assert quarantine["reason"] == other_reason


@pytest.mark.parametrize(
    ("drift", "expected_reason"),
    (
        ("evidence-status", "evidence edge"),
        ("evidence-origin", "evidence edge"),
        ("evidence-revision", "evidence edge"),
        ("dependent-proved", "evidence edge"),
        ("provenance", "live false-decomposition cleanup quarantine"),
    ),
)
def test_multi_evidence_quarantine_migration_fails_closed_on_drift(
    cleanup_project,
    drift,
    expected_reason,
):
    """Do not consume the row unless source, graph, and provenance still agree."""
    root, _state_root, source = cleanup_project
    source.write_text(_live_multi_evidence_source(), encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    _seed_live_multi_evidence_and_dependent_tombstone(
        source,
        promotion,
        evidence_status="proving" if drift == "evidence-status" else "proved",
        evidence_generated_by="human" if drift == "evidence-origin" else "prover-edit",
        evidence_source_sha256=("0" * 64 if drift == "evidence-revision" else ""),
        dependent_status="proved" if drift == "dependent-proved" else "conjectured",
    )
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=(false_decomposition_cleanup._MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON),
        provenance_id=(
            "wrong-provenance" if drift == "provenance" else str(provenance["transaction_id"])
        ),
    )
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert any(expected_reason in reason for reason in result.reasons)
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    quarantine = plan_state.load_summary()["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "quarantined"


@pytest.mark.parametrize(
    ("drift", "expected_reason"),
    (
        ("forged-id", "unique proved promotion proof declaration"),
        ("revision", "unique proved promotion proof declaration"),
        ("placeholder", "unique proved promotion proof declaration"),
        ("duplicate-edge", "promotion evidence edge is duplicated"),
        ("missing-edge", "unique proved promotion proof declaration"),
    ),
)
def test_multi_evidence_requires_exact_source_bound_promotion_proof(
    cleanup_project,
    drift,
    expected_reason,
):
    """Extra evidence cannot substitute for the exact promoted proof declaration."""
    root, _state_root, source = cleanup_project
    source.write_text(_live_multi_evidence_source(), encoding="utf-8")
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    evidence_edges, _dependent_edge = _seed_live_multi_evidence_and_dependent_tombstone(
        source, promotion
    )
    promoted_edge = evidence_edges[0]
    blueprint = plan_state.load_blueprint()
    promoted_node = blueprint.node_by_id(promoted_edge.source)
    assert promoted_node is not None
    nodes = list(blueprint.nodes)
    edges = list(blueprint.edges)
    if drift == "forged-id":
        forged = replace(promoted_node, id="forged-promotion-proof-id")
        nodes = [forged if node == promoted_node else node for node in nodes]
        edges = [
            replace(edge, source=forged.id) if edge == promoted_edge else edge for edge in edges
        ]
    elif drift == "revision":
        nodes = [
            replace(promoted_node, source_sha256="0" * 64) if node == promoted_node else node
            for node in nodes
        ]
    elif drift == "placeholder":
        nodes = [
            (
                replace(promoted_node, statement=promoted_node.statement + "\n-- sorry")
                if node == promoted_node
                else node
            )
            for node in nodes
        ]
    elif drift == "duplicate-edge":
        edges.append(promoted_edge)
    else:
        edges.remove(promoted_edge)
    plan_state.save_blueprint(replace(blueprint, nodes=tuple(nodes), edges=tuple(edges)))
    _seed_promotion(promotion)
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert any(expected_reason in reason for reason in result.reasons)
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before


def test_authenticated_evidence_tombstone_replay_is_idempotent(monkeypatch, cleanup_project):
    """Resume after graph persistence without losing negation audit evidence."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    evidence, _nested = _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "graph-persisted":
            raise RuntimeError("crash after evidence-preserving graph cleanup")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash after evidence-preserving"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(root), validate_promotion=_valid
        )
    source_after_graph = source.read_bytes()
    blueprint_after_graph = plan_state.load_blueprint()
    assert evidence in blueprint_after_graph.edges

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _s: None)
    recovered = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert recovered.cleaned == 1
    assert recovered.pending == 0
    assert source.read_bytes() == source_after_graph
    assert plan_state.load_blueprint() == blueprint_after_graph
    assert not plan_state.load_summary().get("negation_promotions")


def test_tombstone_origin_drift_before_finalize_stays_pending(monkeypatch, cleanup_project):
    """A graph writer cannot replace decomposer ownership after cleanup persistence."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)
    helper_id = plan_state.node_id_for(HELPER, str(source))

    def drift_after_graph(stage):
        if stage != "graph-persisted":
            return
        blueprint = plan_state.load_blueprint()
        helper = blueprint.node_by_id(helper_id)
        assert helper is not None
        plan_state.save_blueprint(
            replace(
                blueprint,
                nodes=tuple(
                    replace(helper, generated_by="human") if node.id == helper_id else node
                    for node in blueprint.nodes
                ),
            )
        )

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        drift_after_graph,
    )
    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.pending == 1
    assert any("lost authoritative graph ownership" in reason for reason in result.reasons)
    helper = plan_state.load_blueprint().node_by_id(helper_id)
    assert helper is not None and helper.generated_by == "human"
    assert (
        plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]["state"]
        == "pending"
    )


def test_legacy_evidence_quarantine_auto_reconciles_exact_safe_shape(cleanup_project):
    """Resume the historical live quarantine once its exact edge is recognized."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    evidence, _nested = _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=false_decomposition_cleanup._LEGACY_EVIDENCE_QUARANTINE_REASON,
        provenance_id=str(provenance["transaction_id"]),
    )

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 1
    assert result.pending == 0
    assert result.quarantined == 0
    blueprint = plan_state.load_blueprint()
    assert evidence in blueprint.edges
    summary = plan_state.load_summary()
    quarantine = summary["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "resolved"
    assert "audit tombstone" in quarantine["resolution_reason"]

    source_after = source.read_bytes()
    blueprint_after = blueprint
    replayed = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )
    assert replayed.cleaned == 0
    assert replayed.pending == 0
    assert replayed.quarantined == 0
    assert source.read_bytes() == source_after
    assert plan_state.load_blueprint() == blueprint_after


def test_legacy_evidence_quarantine_without_current_evidence_stays_live(cleanup_project):
    """The historical classifier exception cannot authorize a zero-evidence cleanup."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=false_decomposition_cleanup._LEGACY_EVIDENCE_QUARANTINE_REASON,
        provenance_id=str(provenance["transaction_id"]),
    )
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    quarantine = plan_state.load_summary()["false_decomposition_cleanup_quarantine"][-1]
    assert quarantine["state"] == "quarantined"
    assert not plan_state.load_summary().get("false_decomposition_cleanup_transactions")


@pytest.mark.parametrize("ambiguity_kind", ["duplicate", "forged-id"])
def test_ambiguous_legacy_quarantine_blocks_its_promotion(cleanup_project, ambiguity_kind):
    """Unauthenticated quarantine rows remain negative authority for their target."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=false_decomposition_cleanup._LEGACY_EVIDENCE_QUARANTINE_REASON,
        provenance_id=str(provenance["transaction_id"]),
    )

    def corrupt_quarantine(summary):
        quarantine = deepcopy(summary["false_decomposition_cleanup_quarantine"][-1])
        if ambiguity_kind == "duplicate":
            summary["false_decomposition_cleanup_quarantine"] = [quarantine, quarantine]
        else:
            quarantine["quarantine_id"] = "0" * 64
            summary["false_decomposition_cleanup_quarantine"] = [quarantine]

    update_json_file(plan_state.plan_state_paths().summary_json, corrupt_quarantine)
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined >= 1
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    assert not plan_state.load_summary().get("false_decomposition_cleanup_transactions")


@pytest.mark.parametrize("quarantine_drift", ["removed", "duplicated"])
def test_sealed_legacy_quarantine_drift_blocks_source_cleanup(
    monkeypatch, cleanup_project, quarantine_drift
):
    """A prepared evidence retry retains the exact quarantine authority it consumed."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)
    false_decomposition_cleanup._quarantine_candidate(
        promotion,
        reason=false_decomposition_cleanup._LEGACY_EVIDENCE_QUARANTINE_REASON,
        provenance_id=str(provenance["transaction_id"]),
    )
    source_before = source.read_bytes()
    graph_before = plan_state.load_blueprint()

    def drift_after_prepare(stage):
        if stage != "pending-persisted":
            return

        def mutate(summary):
            quarantine = summary["false_decomposition_cleanup_quarantine"][-1]
            summary["false_decomposition_cleanup_quarantine"] = (
                [] if quarantine_drift == "removed" else [quarantine, deepcopy(quarantine)]
            )

        update_json_file(plan_state.plan_state_paths().summary_json, mutate)

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "_cleanup_transaction_hook",
        drift_after_prepare,
    )
    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(root), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.pending == 1
    assert source.read_bytes() == source_before
    assert plan_state.load_blueprint() == graph_before
    transaction = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert transaction["state"] == "pending"
    assert "evidence quarantine" in transaction["last_reconciliation_reason"]


@pytest.mark.parametrize("identity_kind", ["swapped-id", "duplicate-name", "duplicate-id"])
def test_ambiguous_helper_graph_identity_quarantines_without_edits(cleanup_project, identity_kind):
    """Promotion and graph identities must agree exactly and uniquely."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    if identity_kind == "duplicate-name":
        duplicate = GraphNode(
            id="duplicate-helper-node",
            name=HELPER,
            file=str(source),
            status="false",
            generated_by="decomposer",
        )
        plan_state.save_blueprint(
            Blueprint(nodes=(*blueprint.nodes, duplicate), revision=blueprint.revision)
        )
    else:
        duplicate = GraphNode(
            id=helper_id,
            name="swapped_declaration",
            file=str(source),
            status="false",
            generated_by="decomposer",
        )
        plan_state.save_blueprint(
            Blueprint(nodes=(*blueprint.nodes, duplicate), revision=blueprint.revision)
        )
    _seed_promotion(promotion)
    if identity_kind == "swapped-id":
        promotion["node_id"] = plan_state.node_id_for(PARENT, str(source))
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    assert plan_state.load_blueprint().node_by_id(helper_id) is not None


def test_post_source_crash_preserves_user_source_and_graph_drift(monkeypatch, cleanup_project):
    """Replay recognizes cleanup plus later edits and never restores stale source."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "source-persisted":
            raise RuntimeError("crash after source")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash after source"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(source.parent), validate_promotion=_valid
        )
    drifted = source.read_text(encoding="utf-8").replace(
        "theorem parent_goal : True := by\n  sorry",
        "theorem parent_goal : True := by\n  trivial",
    )
    drifted = drifted.replace("end Demo", "theorem user_edit : True := by trivial\n\nend Demo")
    source.write_text(drifted, encoding="utf-8")
    blueprint = plan_state.load_blueprint()
    user_node = GraphNode(
        id=plan_state.node_id_for("user_edit", str(source)),
        name="user_edit",
        file=str(source),
        status="proved",
        generated_by="human",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, user_node),
            edges=blueprint.edges,
            revision=blueprint.revision,
        )
    )

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _s: None)
    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    text = source.read_text(encoding="utf-8")
    assert "theorem user_edit : True := by trivial" in text
    assert "theorem parent_goal : True := by\n  trivial" in text
    assert decomposition_provenance.declaration_slice(text, HELPER) is None
    replayed = plan_state.load_blueprint()
    assert replayed.node_by_id(user_node.id) == user_node
    assert replayed.node_by_id(plan_state.node_id_for(HELPER, str(source))) is None


@pytest.mark.parametrize(
    ("helper_status", "generated_by", "expected_reason"),
    [
        ("proved", "decomposer", "authoritative false status"),
        ("false", "human", "graph ownership changed"),
    ],
)
def test_post_source_replay_preserves_helper_after_authority_drift(
    monkeypatch,
    cleanup_project,
    helper_status,
    generated_by,
    expected_reason,
):
    """Replay never deletes a helper whose false/decomposer authority changed."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "source-persisted":
            raise RuntimeError("crash after source")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash after source"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(root), validate_promotion=_valid
        )
    blueprint = plan_state.load_blueprint()
    helper_id = plan_state.node_id_for(HELPER, str(source))
    helper = blueprint.node_by_id(helper_id)
    assert helper is not None
    drifted_helper = replace(helper, status=helper_status, generated_by=generated_by)
    plan_state.save_blueprint(
        replace(
            blueprint,
            nodes=tuple(
                drifted_helper if node.id == helper_id else node for node in blueprint.nodes
            ),
        )
    )
    graph_before_replay = plan_state.load_blueprint()
    source_before_replay = source.read_bytes()
    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _s: None)

    result = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert result.cleaned == 0
    assert result.pending == 1
    assert any(expected_reason in reason for reason in result.reasons)
    assert source.read_bytes() == source_before_replay
    assert plan_state.load_blueprint() == graph_before_replay
    assert plan_state.load_blueprint().node_by_id(helper_id) == drifted_helper


def test_post_source_external_graph_drift_stays_pending_until_repaired(
    monkeypatch, cleanup_project
):
    """Unsafe graph drift keeps exact transaction evidence and later converges."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)

    def crash(stage):
        if stage == "source-persisted":
            raise RuntimeError("crash after source")

    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash after source"):
        false_decomposition_cleanup.reconcile_false_decompositions(
            [promotion], cwd=str(source.parent), validate_promotion=_valid
        )
    blueprint = plan_state.load_blueprint()
    external = GraphNode(id="external-node", name="external", file=str(source))
    unsafe_edge = GraphEdge(
        source=external.id,
        target=plan_state.node_id_for(HELPER, str(source)),
        kind="depends_on",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, external),
            edges=(*blueprint.edges, unsafe_edge),
            revision=blueprint.revision,
        )
    )
    monkeypatch.setattr(false_decomposition_cleanup, "_cleanup_transaction_hook", lambda _s: None)

    pending = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert pending.cleaned == 0
    assert pending.pending == 1
    assert pending.quarantined == 0
    assert any("another graph node depends" in reason for reason in pending.reasons)
    summary = plan_state.load_summary()
    assert summary["false_decomposition_cleanup_transactions"][-1]["state"] == "pending"
    assert summary["negation_promotions"][-1]["theorem"] == HELPER
    assert unsafe_edge in plan_state.load_blueprint().edges

    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=tuple(node for node in blueprint.nodes if node.id != external.id),
            edges=tuple(edge for edge in blueprint.edges if edge != unsafe_edge),
            revision=blueprint.revision,
        )
    )
    finished = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )
    assert finished.cleaned == 1
    assert finished.pending == 0
    _assert_cleaned(source)


def test_quarantine_is_terminal_until_explicit_retry_authorization(cleanup_project):
    """Repairing graph state alone cannot silently replay quarantined source work."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    blueprint = plan_state.load_blueprint()
    external = GraphNode(id="external-node", name="external", file=str(source))
    unsafe_edge = GraphEdge(
        source=external.id,
        target=plan_state.node_id_for(HELPER, str(source)),
        kind="depends_on",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, external),
            edges=(*blueprint.edges, unsafe_edge),
            revision=blueprint.revision,
        )
    )

    quarantined = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )
    assert quarantined.quarantined == 1
    before = source.read_bytes()
    summary = plan_state.load_summary()
    quarantine_id = summary["false_decomposition_cleanup_quarantine"][-1]["quarantine_id"]

    current = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=tuple(node for node in current.nodes if node.id != external.id),
            edges=tuple(edge for edge in current.edges if edge != unsafe_edge),
            revision=current.revision,
        )
    )
    still_quarantined = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert still_quarantined.cleaned == 0
    assert still_quarantined.quarantined == 1
    assert source.read_bytes() == before
    assert false_decomposition_cleanup.authorize_cleanup_quarantine_retry(
        quarantine_id,
        reason="operator removed the unrelated dependency edge",
    )

    retried = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert retried.cleaned == 1
    assert retried.quarantined == 0
    _assert_cleaned(source)


def test_transaction_quarantine_and_retry_restore_are_atomic_and_replayable(
    monkeypatch, cleanup_project
):
    """One summary commit records quarantine, and authorization restores its exact plan."""
    root, _state_root, source = cleanup_project
    _promotion_record, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    original_update = false_decomposition_cleanup.update_json_file
    updates = 0

    def counted_update(*args, **kwargs):
        nonlocal updates
        updates += 1
        return original_update(*args, **kwargs)

    monkeypatch.setattr(false_decomposition_cleanup, "update_json_file", counted_update)
    false_decomposition_cleanup._mark_transaction_quarantined(
        transaction,
        "operator must reconcile a simulated source ambiguity",
    )
    assert updates == 1
    summary = plan_state.load_summary()
    quarantined = summary["false_decomposition_cleanup_transactions"][-1]
    quarantine = summary["false_decomposition_cleanup_quarantine"][-1]
    assert quarantined["state"] == "quarantined"
    assert quarantine["state"] == "quarantined"
    assert quarantine["promotion"] == quarantined["promotion"]

    assert false_decomposition_cleanup.authorize_cleanup_quarantine_retry(
        quarantine["quarantine_id"],
        reason="operator verified the exact stored source and graph plan",
    )
    summary = plan_state.load_summary()
    restored = summary["false_decomposition_cleanup_transactions"][-1]
    assert restored["state"] == "pending"
    assert restored["transaction_id"] == transaction["transaction_id"]
    assert restored["source_after"] == transaction["source_after"]
    assert "quarantined_at" not in restored
    assert "reason" not in restored
    assert restored["manual_retry_authorized_at"]
    assert restored["manual_retry_reason"]
    assert summary["false_decomposition_cleanup_quarantine"][-1]["state"] == "resolved"

    monkeypatch.setattr(false_decomposition_cleanup, "update_json_file", original_update)
    recovered = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))
    assert recovered.cleaned == 1
    assert recovered.pending == 0
    _assert_cleaned(source)


def test_begin_transaction_rejects_duplicate_promotion_and_reuses_authorized_plan(
    monkeypatch, cleanup_project
):
    """A rebuilt plan cannot shadow a live plan, but an authorized old plan can resume."""
    _root, _state_root, source = cleanup_project
    _promotion_record, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    alternate = deepcopy(transaction)
    alternate["provenance_id"] = hashlib.sha256(b"alternate provenance").hexdigest()
    alternate = false_decomposition_cleanup._seal_transaction(alternate)

    with pytest.raises(RuntimeError, match="already has another transaction"):
        false_decomposition_cleanup._begin_transaction(alternate)
    assert len(plan_state.load_summary()["false_decomposition_cleanup_transactions"]) == 1

    def authorize_legacy(summary):
        record = summary["false_decomposition_cleanup_transactions"][0]
        record.update(
            {
                "state": "manual-retry-authorized",
                "quarantined_at": "2026-07-17T00:00:00+00:00",
                "reason": "legacy quarantine",
                "manual_retry_authorized_at": "2026-07-17T00:01:00+00:00",
                "manual_retry_reason": "operator authorized exact replay",
            }
        )

    update_json_file(plan_state.plan_state_paths().summary_json, authorize_legacy)
    resumed = false_decomposition_cleanup._begin_transaction(alternate)

    assert resumed["transaction_id"] == transaction["transaction_id"]
    assert resumed["state"] == "pending"
    assert resumed["manual_retry_reason"] == "operator authorized exact replay"
    assert "quarantined_at" not in resumed
    records = plan_state.load_summary()["false_decomposition_cleanup_transactions"]
    assert len(records) == 1
    assert records[0] == resumed


def test_recovery_migrates_legacy_authorized_retry_and_replays_under_lease(
    monkeypatch, cleanup_project
):
    """Startup converts the old authorization state and completes its exact plan."""
    root, _state_root, source = cleanup_project
    _promotion_record, transaction = _prepare_cleanup_transaction(monkeypatch, source)

    def authorize_legacy(summary):
        record = summary["false_decomposition_cleanup_transactions"][0]
        record.update(
            {
                "state": "manual-retry-authorized",
                "quarantined_at": "2026-07-17T00:00:00+00:00",
                "reason": "legacy quarantine",
                "manual_retry_authorized_at": "2026-07-17T00:01:00+00:00",
                "manual_retry_reason": "operator authorized exact replay",
            }
        )

    update_json_file(plan_state.plan_state_paths().summary_json, authorize_legacy)
    recovered = false_decomposition_cleanup.recover_cleanup_transactions(cwd=str(root))

    assert recovered.cleaned == 1
    assert recovered.pending == 0
    _assert_cleaned(source)
    committed = plan_state.load_summary()["false_decomposition_cleanup_transactions"][-1]
    assert committed["transaction_id"] == transaction["transaction_id"]
    assert committed["state"] == "committed"
    assert committed["manual_retry_reason"] == "operator authorized exact replay"
    assert "quarantined_at" not in committed


def test_cleanup_finalize_preserves_hostile_unrelated_promotion_registry(
    monkeypatch, cleanup_project
):
    """Committing one target removes only that exact authenticated raw row."""
    _root, _state_root, source = cleanup_project
    promotion, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    malformed = {"future_promotion_authority": ["opaque"]}
    nonmapping = "opaque-promotion-record"
    duplicate_one = {"promotion_id": "unrelated-duplicate", "payload": 1}
    duplicate_two = {"promotion_id": "unrelated-duplicate", "payload": 2}
    hostile = [malformed, promotion, nonmapping, duplicate_one, duplicate_two]

    def seed_hostile(summary):
        summary["negation_promotions"] = deepcopy(hostile)

    update_json_file(plan_state.plan_state_paths().summary_json, seed_hostile)
    false_decomposition_cleanup._finalize_transaction(transaction)

    summary = plan_state.load_summary()
    assert summary["negation_promotions"] == [
        malformed,
        nonmapping,
        duplicate_one,
        duplicate_two,
    ]
    committed = summary["false_decomposition_cleanup_transactions"][-1]
    assert committed["state"] == "committed"


def test_cleanup_finalize_rejects_duplicate_target_without_registry_rewrite(
    monkeypatch, cleanup_project
):
    """Duplicate claims on the consumed promotion fail closed and remain byte-for-byte values."""
    _root, _state_root, source = cleanup_project
    promotion, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    duplicate = deepcopy(promotion)
    raw = [promotion, {"opaque": True}, duplicate]

    def duplicate_target(summary):
        summary["negation_promotions"] = deepcopy(raw)

    update_json_file(plan_state.plan_state_paths().summary_json, duplicate_target)
    with pytest.raises(RuntimeError, match="duplicated"):
        false_decomposition_cleanup._finalize_transaction(transaction)

    summary = plan_state.load_summary()
    assert summary["negation_promotions"] == raw
    assert summary["false_decomposition_cleanup_transactions"][-1]["state"] == "pending"


def test_legacy_promotion_bridge_commits_authority_before_source_cleanup(cleanup_project):
    """Fresh leased evidence seals a legacy row and its commit before deletion."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    authoritative = deepcopy(promotion)
    legacy_fields = (
        "axioms",
        "canonical_file",
        "declaration_signature_sha256",
        "file",
        "is_main_goal",
        "key",
        "negation_name",
        "negation_prop",
        "node_id",
        "promoted_at",
        "proof_tactic",
        "source_revision_sha256",
        "theorem",
    )
    legacy = {field: authoritative[field] for field in legacy_fields}
    for optional in ("promotion_kind", "proof_declaration"):
        if optional in authoritative:
            legacy[optional] = authoritative[optional]
    legacy["promotion_id"] = negation_promotion._legacy_promotion_id(legacy, root)

    def downgrade(summary):
        summary["negation_promotions"] = [legacy]
        summary["negation_promotion_transactions"] = []

    update_json_file(plan_state.plan_state_paths().summary_json, downgrade)
    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [legacy],
        cwd=str(root),
        validate_promotion=lambda _candidate: SimpleNamespace(
            ok=True,
            reason="fresh negation is valid",
            is_main_goal=False,
            evidence=authoritative,
        ),
    )

    assert result.cleaned == 1
    _assert_cleaned(source)
    transactions = plan_state.load_summary()["negation_promotion_transactions"]
    assert len(transactions) == 1
    assert transactions[0]["transaction_id"] == authoritative["promotion_id"]
    assert transactions[0]["state"] == "consumed-by-false-decomposition-cleanup"
    assert transactions[0]["cleanup_transaction_id"]


def test_legacy_evidence_quarantine_migrates_across_promotion_bridge(cleanup_project):
    """Freshly sealed promotion authority carries its exact old retry to resolution."""
    root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    _seed_graph(source)
    evidence, _nested = _seed_authenticated_negation_evidence_and_nested_decomposition(source)
    _seed_promotion(promotion)
    authoritative = deepcopy(promotion)
    legacy_fields = (
        "axioms",
        "canonical_file",
        "declaration_signature_sha256",
        "file",
        "is_main_goal",
        "key",
        "negation_name",
        "negation_prop",
        "node_id",
        "promoted_at",
        "proof_tactic",
        "source_revision_sha256",
        "theorem",
    )
    legacy = {field: authoritative[field] for field in legacy_fields}
    for optional in ("promotion_kind", "proof_declaration"):
        if optional in authoritative:
            legacy[optional] = authoritative[optional]
    legacy["promotion_id"] = negation_promotion._legacy_promotion_id(legacy, root)

    def downgrade(summary):
        summary["negation_promotions"] = [legacy]
        summary["negation_promotion_transactions"] = []

    update_json_file(plan_state.plan_state_paths().summary_json, downgrade)
    false_decomposition_cleanup._quarantine_candidate(
        legacy,
        reason=false_decomposition_cleanup._LEGACY_EVIDENCE_QUARANTINE_REASON,
        provenance_id=str(provenance["transaction_id"]),
    )

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [legacy],
        cwd=str(root),
        validate_promotion=lambda _candidate: SimpleNamespace(
            ok=True,
            reason="fresh negation is valid",
            is_main_goal=False,
            evidence=authoritative,
        ),
    )

    assert result.cleaned == 1
    assert result.quarantined == 0
    assert evidence in plan_state.load_blueprint().edges
    quarantine = plan_state.load_summary()["false_decomposition_cleanup_quarantine"]
    assert quarantine
    assert all(item["state"] == "resolved" for item in quarantine)
    assert any("migrated" in item["resolution_reason"] for item in quarantine)
    assert any("audit tombstone" in item["resolution_reason"] for item in quarantine)


def test_fresh_cleanup_commit_survives_terminal_history_cap(monkeypatch, cleanup_project):
    """An old pending row moved to committed is retained as the newest terminal effect."""
    _root, _state_root, source = cleanup_project
    _promotion_record, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    history = []
    for index in range(false_decomposition_cleanup._TRANSACTION_CAP):
        record = deepcopy(transaction)
        promotion_id = hashlib.sha256(f"history-promotion-{index}".encode()).hexdigest()
        record["promotion"] = dict(record["promotion"])
        record["promotion"]["promotion_id"] = promotion_id
        record["promotion_id"] = promotion_id
        record["provenance_id"] = hashlib.sha256(f"history-provenance-{index}".encode()).hexdigest()
        record = false_decomposition_cleanup._seal_transaction(record)
        record["state"] = "committed"
        record["committed_at"] = f"2026-07-17T00:{index:02d}:00+00:00"
        record.pop("source_after")
        history.append(record)

    def seed_history(summary):
        summary["false_decomposition_cleanup_transactions"] = [transaction, *history]

    update_json_file(plan_state.plan_state_paths().summary_json, seed_history)
    false_decomposition_cleanup._finalize_transaction(transaction)

    retained = plan_state.load_summary()["false_decomposition_cleanup_transactions"]
    assert len(retained) == false_decomposition_cleanup._TRANSACTION_CAP
    assert retained[-1]["transaction_id"] == transaction["transaction_id"]
    assert retained[-1]["state"] == "committed"
    assert history[0]["transaction_id"] not in {item["transaction_id"] for item in retained}


def test_cleanup_transaction_capacity_preserves_every_live_record(monkeypatch, cleanup_project):
    """A new transaction fails closed instead of evicting one pending replay."""
    _root, _state_root, source = cleanup_project
    _promotion_record, template = _prepare_cleanup_transaction(monkeypatch, source)
    pending = []
    for index in range(false_decomposition_cleanup._TRANSACTION_CAP + 1):
        record = deepcopy(template)
        promotion_id = hashlib.sha256(f"capacity-promotion-{index}".encode()).hexdigest()
        record["promotion"] = dict(record["promotion"])
        record["promotion"]["promotion_id"] = promotion_id
        record["promotion_id"] = promotion_id
        record["provenance_id"] = hashlib.sha256(
            f"capacity-provenance-{index}".encode()
        ).hexdigest()
        pending.append(false_decomposition_cleanup._seal_transaction(record))

    def seed(summary):
        summary["false_decomposition_cleanup_transactions"] = pending[:-1]

    update_json_file(plan_state.plan_state_paths().summary_json, seed)

    with pytest.raises(
        false_decomposition_cleanup.CleanupTransactionCapacityError,
        match="remain pending",
    ):
        false_decomposition_cleanup._begin_transaction(pending[-1])

    retained = plan_state.load_summary()["false_decomposition_cleanup_transactions"]
    assert [item["transaction_id"] for item in retained] == [
        item["transaction_id"] for item in pending[:-1]
    ]
    state = false_decomposition_cleanup.cleanup_reconciliation_state()
    assert state.pending == false_decomposition_cleanup._TRANSACTION_CAP
    assert len(state.reasons) == 20


def test_cleanup_commit_preserves_unrelated_live_negation_authority(monkeypatch, cleanup_project):
    """Consuming one false helper never evicts other live promotion replay state."""
    _root, _state_root, source = cleanup_project
    promotion, transaction = _prepare_cleanup_transaction(monkeypatch, source)
    unrelated_promotions = [
        {
            "promotion_id": f"unrelated-promotion-{index}",
            "theorem": f"unrelated_{index}",
            "file": str(source),
        }
        for index in range(false_decomposition_cleanup._TRANSACTION_CAP + 5)
    ]
    unrelated_transactions = [
        {
            "transaction_id": f"unrelated-transaction-{index}",
            "state": "pending",
            "promotion": unrelated,
        }
        for index, unrelated in enumerate(unrelated_promotions)
    ]

    def seed(summary):
        summary["negation_promotions"] = [promotion, *unrelated_promotions]
        target_transaction = summary["negation_promotion_transactions"][0]
        summary["negation_promotion_transactions"] = [
            target_transaction,
            *unrelated_transactions,
        ]

    update_json_file(plan_state.plan_state_paths().summary_json, seed)
    false_decomposition_cleanup._finalize_transaction(transaction)
    summary = plan_state.load_summary()
    assert [item["promotion_id"] for item in summary["negation_promotions"]] == [
        item["promotion_id"] for item in unrelated_promotions
    ]
    retained_transactions = summary["negation_promotion_transactions"]
    assert [item["transaction_id"] for item in retained_transactions[:-1]] == [
        item["transaction_id"] for item in unrelated_transactions
    ]
    assert retained_transactions[-1]["state"] == "consumed-by-false-decomposition-cleanup"


def test_cleanup_quarantine_capacity_preserves_every_unresolved_record(cleanup_project):
    """A new quarantine never evicts an older unresolved safety decision."""
    _root, _state_root, source = cleanup_project
    quarantines = [
        {
            "quarantine_id": f"quarantine-{index}",
            "state": "quarantined",
            "reason": f"ambiguity-{index}",
            "promotion": {
                "promotion_id": f"promotion-{index}",
                "theorem": f"helper_{index}",
                "file": str(source),
            },
        }
        for index in range(false_decomposition_cleanup._QUARANTINE_CAP)
    ]

    def seed(summary):
        summary["false_decomposition_cleanup_quarantine"] = quarantines

    update_json_file(plan_state.plan_state_paths().summary_json, seed)
    false_decomposition_cleanup._quarantine_candidate(
        {
            "promotion_id": "promotion-overflow",
            "theorem": "helper_overflow",
            "file": str(source),
        },
        reason="overflow ambiguity",
    )

    retained = plan_state.load_summary()["false_decomposition_cleanup_quarantine"]
    assert len(retained) == false_decomposition_cleanup._QUARANTINE_CAP + 1
    assert {item["quarantine_id"] for item in retained}.issuperset(
        item["quarantine_id"] for item in quarantines
    )
    state = false_decomposition_cleanup.cleanup_reconciliation_state()
    assert state.quarantined == false_decomposition_cleanup._QUARANTINE_CAP + 1
    assert any("quarantine capacity exceeded" in reason for reason in state.reasons)


def test_retry_authorization_does_not_evict_other_overflow_quarantines(cleanup_project):
    """Resolving one oversized legacy record retains every remaining pause."""
    quarantines = []
    for index in range(false_decomposition_cleanup._QUARANTINE_CAP + 1):
        promotion_id = hashlib.sha256(f"retry-promotion-{index}".encode()).hexdigest()
        quarantines.append(
            false_decomposition_cleanup._quarantine_entry(
                {"promotion_id": promotion_id},
                provenance_id=hashlib.sha256(f"retry-provenance-{index}".encode()).hexdigest(),
                reason=f"ambiguity-{index}",
            )
        )

    def seed(summary):
        summary["false_decomposition_cleanup_quarantine"] = quarantines

    update_json_file(plan_state.plan_state_paths().summary_json, seed)
    assert false_decomposition_cleanup.authorize_cleanup_quarantine_retry(
        quarantines[0]["quarantine_id"],
        reason="operator reconciled this exact ambiguity",
    )

    retained = plan_state.load_summary()["false_decomposition_cleanup_quarantine"]
    assert len(retained) == false_decomposition_cleanup._QUARANTINE_CAP + 1
    assert {item["quarantine_id"] for item in retained} == {
        item["quarantine_id"] for item in quarantines
    }
    assert retained[0]["state"] == "resolved"
    assert all(item["state"] == "quarantined" for item in retained[1:])


def test_legacy_over_capacity_cleanup_state_pauses_without_partial_replay(cleanup_project):
    """An already-oversized live ledger is reported intact and never partially mutated."""
    pending = [
        {
            "transaction_id": f"legacy-pending-{index}",
            "state": "pending",
        }
        for index in range(false_decomposition_cleanup._TRANSACTION_CAP + 1)
    ]

    def seed(summary):
        summary["false_decomposition_cleanup_transactions"] = pending

    update_json_file(plan_state.plan_state_paths().summary_json, seed)

    result = false_decomposition_cleanup.reconcile_false_decompositions([])

    assert result.pending == false_decomposition_cleanup._TRANSACTION_CAP + 1
    assert "capacity exceeded" in result.reasons[0]
    retained = plan_state.load_summary()["false_decomposition_cleanup_transactions"]
    assert [item["transaction_id"] for item in retained] == [
        item["transaction_id"] for item in pending
    ]


@pytest.mark.parametrize("stale_kind", ["helper-signature", "parent-proof", "graph-owner"])
def test_stale_or_user_edited_source_fails_closed(cleanup_project, stale_kind):
    """Ownership ambiguity never deletes a user/upstream declaration."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_current_provenance(source)
    if stale_kind == "helper-signature":
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "theorem bad_helper : False", "theorem bad_helper : 1 = 2"
            ),
            encoding="utf-8",
        )
    elif stale_kind == "parent-proof":
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "  exact False.elim bad_helper", "  trivial"
            ),
            encoding="utf-8",
        )
    _seed_graph(source, generated_by="human" if stale_kind == "graph-owner" else "decomposer")
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    assert decomposition_provenance.declaration_slice(before, HELPER) is not None
    summary = plan_state.load_summary()
    assert summary["negation_promotions"][-1]["theorem"] == HELPER
    assert summary["false_decomposition_cleanup_quarantine"][-1]["reason"]


@pytest.mark.parametrize("ownership", ["decomposer-graph", "committed-provenance"])
def test_stale_main_goal_flag_does_not_hide_exact_helper_ownership(cleanup_project, ownership):
    """Only independently authenticated helper ownership permits source cleanup."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    promotion["is_main_goal"] = True
    _seed_current_provenance(source)
    _seed_graph(source, generated_by="decomposer" if ownership == "decomposer-graph" else "human")
    _seed_promotion(promotion)

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    if ownership == "decomposer-graph":
        assert result.cleaned == 1
        assert result.quarantined == 0
        _assert_cleaned(source)
    else:
        assert result.cleaned == 0
        assert result.quarantined == 1
        assert decomposition_provenance.declaration_slice(
            source.read_text(encoding="utf-8"), HELPER
        )


def test_fresh_main_goal_classification_outranks_later_helper_ownership(cleanup_project):
    """Mutable decomposition provenance cannot delete a revalidated campaign root."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    promotion["is_main_goal"] = True
    promotion["classification_basis"] = "requested_scope_manifest"
    _seed_current_provenance(source)
    _seed_graph(source, generated_by="decomposer")
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion],
        cwd=str(source.parent),
        validate_promotion=lambda _candidate: SimpleNamespace(
            ok=True,
            reason="fresh registered-root negation is valid",
            is_main_goal=True,
        ),
    )

    assert result.cleaned == 0
    assert result.quarantined == 0
    assert source.read_text(encoding="utf-8") == before
    summary = plan_state.load_summary()
    assert summary["negation_promotions"][-1]["promotion_id"] == promotion["promotion_id"]
    assert not summary.get("false_decomposition_cleanup_transactions")


def test_missing_fresh_campaign_marker_quarantines_root_without_source_cleanup(
    cleanup_project, monkeypatch
):
    """Damaged root provenance cannot demote requested source into a helper."""
    _root, _state_root, source = cleanup_project
    helper_id = plan_state.node_id_for(HELPER, str(source))
    parent_id = plan_state.node_id_for(PARENT, str(source))
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=helper_id,
                    name=HELPER,
                    file=str(source),
                    status="proving",
                    generated_by="queue-sync",
                ),
                GraphNode(
                    id=parent_id,
                    name=PARENT,
                    file=str(source),
                    status="proved",
                    generated_by="human",
                ),
            )
        )
    )

    def seed_campaign(summary):
        summary["campaign"] = {
            "campaign_id": "campaign-root",
            "provider_turn_nonce": 0,
            negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD: True,
        }

    update_json_file(plan_state.plan_state_paths().summary_json, seed_campaign)
    registered = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": HELPER, "active_file": str(source)}],
        campaign_id="campaign-root",
        cwd=str(source.parent),
    )
    assert registered.ok, registered.reason

    def scratch(code, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' does not depend on any axioms",
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)
    promoted = negation_promotion.promote_source_negation(
        theorem_id=HELPER,
        file_label=str(source),
        proof_declaration=NEGATION_HELPER,
        cwd=str(source.parent),
    )
    assert promoted.ok and promoted.is_main_goal
    before = source.read_text(encoding="utf-8")

    # Simulate later mutable decomposition ownership that would otherwise be
    # sufficient to delete a false helper.
    _seed_current_provenance(source)
    current_blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=helper_id,
                    name=HELPER,
                    file=str(source),
                    status="false",
                    generated_by="decomposer",
                ),
                GraphNode(
                    id=parent_id,
                    name=PARENT,
                    file=str(source),
                    status="split",
                    generated_by="queue-sync",
                ),
            ),
            edges=(GraphEdge(source=helper_id, target=parent_id, kind="split_of"),),
            revision=current_blueprint.revision,
        )
    )

    def delete_origin_marker(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop(negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD, None)
        summary["campaign"] = campaign

    update_json_file(plan_state.plan_state_paths().summary_json, delete_origin_marker)

    reconciled = negation_promotion.reconcile_promotions_on_startup(cwd=str(source.parent))

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 1
    assert reconciled.decompositions_cleaned == 0
    assert source.read_text(encoding="utf-8") == before
    assert decomposition_provenance.declaration_slice(before, HELPER) is not None
    summary = plan_state.load_summary()
    assert not summary.get("false_decomposition_cleanup_transactions")
    assert not summary.get("false_decomposition_cleanups")


def test_unvalidated_main_goal_flag_without_cleanup_ownership_is_quarantined(cleanup_project):
    """A mutable main-goal bit cannot manufacture terminal negation authority."""
    _root, _state_root, source = cleanup_project
    promotion = _promotion(source)
    promotion["is_main_goal"] = True
    _seed_graph(source, generated_by="human")
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 0
    assert result.quarantined == 1
    assert source.read_text(encoding="utf-8") == before
    summary = plan_state.load_summary()
    assert summary["negation_promotions"][-1]["promotion_id"] == promotion["promotion_id"]
    assert summary["false_decomposition_cleanup_quarantine"][-1]["reason"]


def test_legacy_activity_and_checkpoint_migrate_exact_parent(cleanup_project):
    """Old campaigns recover from activity plus a verified pre-edit snapshot."""
    _root, state_root, source = cleanup_project
    promotion = _promotion(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    activity_path = state_root / "activity" / "runs" / "legacy.jsonl"
    activity_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event_id": "legacy-decomposer-event",
        "timestamp": "2026-01-02T00:00:00+00:00",
        "type": "decomposer",
        "details": {
            "ok": True,
            "placed": [HELPER, "valid_sibling"],
            "target_symbol": PARENT,
            "active_file": str(source),
            "project_root": str(source.parent),
        },
    }
    activity_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    checkpoint_root = state_root / "verified-patch-checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "version": 1,
        "checkpoint_id": "legacy-pre-edit",
        "created_at": "2026-01-01T00:00:00+00:00",
        "file_path": str(source),
        "cwd": str(source.parent),
        "before_sha256": _sha256(BEFORE_SOURCE),
        "before_content": BEFORE_SOURCE,
    }
    (checkpoint_root / "legacy-pre-edit.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    assert result.cleaned == 1
    _assert_cleaned(source)
    provenance = plan_state.load_summary()["decomposition_provenance"][-1]
    assert provenance["provenance_kind"] == "legacy-activity-and-verified-checkpoint"
    assert provenance["legacy_checkpoint_id"] == "legacy-pre-edit"


@pytest.mark.parametrize("tamper_archive", [False, True])
def test_future_false_promotion_uses_only_checksum_verified_retained_activity(
    cleanup_project, tamper_archive
):
    """Retention keeps old ownership usable and rejects altered cold evidence."""
    _root, state_root, source = cleanup_project
    activity_path = state_root / "activity" / "runs" / "legacy-closed.jsonl"
    activity_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": "legacy-decomposer-event",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "type": "decomposer",
            "details": {
                "ok": True,
                "placed": [HELPER, "valid_sibling"],
                "target_symbol": PARENT,
                "active_file": str(source),
                "project_root": str(source.parent),
            },
        },
        {
            "event_id": "legacy-runner-exit",
            "timestamp": "2026-01-03T00:00:00+00:00",
            "type": "runner-exit",
            "details": {},
        },
    ]
    activity_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    checkpoint_root = state_root / "verified-patch-checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "version": 1,
        "checkpoint_id": "legacy-pre-edit",
        "created_at": "2026-01-01T00:00:00+00:00",
        "file_path": str(source),
        "cwd": str(source.parent),
        "before_sha256": _sha256(BEFORE_SOURCE),
        "before_content": BEFORE_SOURCE,
    }
    (checkpoint_root / "legacy-pre-edit.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    retention = workflow_activity_retention.compact_closed_activity(
        state_root,
        current_run_id="current-run",
        reduce_event=lambda *_args: None,
        compact_event=lambda _event: {},
        identity_is_live=lambda _identity: False,
    )
    assert retention.archived_runs == ("legacy-closed",)
    assert not activity_path.exists()
    archive_path = state_root / "activity/archive/runs/legacy-closed.jsonl.gz"
    if tamper_archive:
        archive_path.write_bytes(b"not the cataloged gzip evidence")

    promotion = _promotion(source)
    _seed_graph(source)
    _seed_promotion(promotion)
    before = source.read_text(encoding="utf-8")
    result = false_decomposition_cleanup.reconcile_false_decompositions(
        [promotion], cwd=str(source.parent), validate_promotion=_valid
    )

    if tamper_archive:
        assert result.cleaned == 0
        assert result.quarantined == 1
        assert source.read_text(encoding="utf-8") == before
        assert plan_state.load_summary()["negation_promotions"][-1]["theorem"] == HELPER
    else:
        assert result.cleaned == 1
        _assert_cleaned(source)
