"""Protect existing declarations while accepting bounded graph metadata updates."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.workflows.prover.models import Dag, Node
from leanflow_cli.workflows.prover.planning import apply_proposal, planning_prompt


def planning_graph() -> Dag:
    """Return one original root with independently proved and pending helpers."""
    return Dag(
        [
            Node(
                "root",
                "goal",
                "-- Preserve this comment.\ntheorem goal (n : Nat) : n = n",
                "Main.lean",
                "Main",
                original=True,
                status="conditional",
                revision=2,
                signature_sha256="original-kernel-signature",
                candidate=["rfl"],
            ),
            Node(
                "old_helper",
                "oldHelper",
                "theorem oldHelper : True",
                "LeanFlowProofs/Old.lean",
                "LeanFlowProofs.Old",
                status="proved",
                proof_sha256="verified-helper-proof",
            ),
            Node(
                "pending_helper",
                "pendingHelper",
                "theorem pendingHelper : True ∧ True",
                "LeanFlowProofs/Pending.lean",
                "LeanFlowProofs.Pending",
            ),
        ],
        ["root"],
    )


def test_dependency_only_update_preserves_statements_signatures_and_verified_progress() -> None:
    dag = planning_graph()
    before = dag.to_dict()

    updated, skeletons = apply_proposal(
        dag,
        {"nodes": [{"id": "root", "dependencies": ["old_helper", "pending_helper"]}]},
        max_nodes=10,
    )

    original = dag.by_id()["root"]
    changed = updated.by_id()["root"]
    assert changed.statement == original.statement
    assert changed.signature_sha256 == original.signature_sha256
    assert changed.candidate == original.candidate
    assert changed.dependencies == ["old_helper", "pending_helper"]
    assert changed.revision == original.revision + 1
    assert changed.status == "pending"
    assert updated.by_id()["old_helper"] == dag.by_id()["old_helper"]
    assert skeletons == {}
    assert dag.to_dict() == before


def test_metadata_only_update_keeps_revision_status_and_authority() -> None:
    dag = planning_graph()
    updated, _ = apply_proposal(
        dag,
        {"nodes": [{"id": "root", "informal_justification": "Use reflexivity."}]},
        max_nodes=10,
    )

    changed = updated.by_id()["root"]
    assert changed.informal_justification == "Use reflexivity."
    assert changed.statement == dag.by_id()["root"].statement
    assert changed.signature_sha256 == "original-kernel-signature"
    assert changed.revision == 2
    assert changed.status == "conditional"


@pytest.mark.parametrize("suffix", ["", " := by sorry"])
def test_legacy_exact_statement_copies_remain_supported(suffix: str) -> None:
    dag = planning_graph()
    statement = dag.by_id()["root"].statement
    updated, skeletons = apply_proposal(
        dag,
        {"nodes": [{"id": "root", "statement": statement + suffix}]},
        max_nodes=10,
    )
    assert updated.by_id()["root"].statement == statement
    assert skeletons == {}


@pytest.mark.parametrize(
    "statement",
    [
        "-- Changed comment.\ntheorem goal (n : Nat) : n = n",
        "-- Preserve this comment.\ntheorem goal (n : Nat) : n = 0",
        "-- Preserve this comment.\ntheorem goal (n : Int) : n = n",
        "-- Preserve this comment.\ntheorem goal (n : Nat) : n = n ",
    ],
)
def test_existing_statement_protection_includes_comments_types_claims_and_whitespace(
    statement: str,
) -> None:
    dag = planning_graph()
    before = dag.to_dict()
    with pytest.raises(ValueError, match="immutable"):
        apply_proposal(
            dag,
            {"nodes": [{"id": "root", "statement": statement, "dependencies": ["old_helper"]}]},
            max_nodes=10,
        )
    assert dag.to_dict() == before


def test_omitting_statement_cannot_rewrite_proved_helper_dependencies() -> None:
    dag = planning_graph()
    with pytest.raises(ValueError, match="independently proved"):
        apply_proposal(
            dag,
            {"nodes": [{"id": "old_helper", "dependencies": ["pending_helper"]}]},
            max_nodes=10,
        )


def test_new_helper_still_requires_a_complete_declaration() -> None:
    with pytest.raises(ValueError, match="one complete declaration"):
        apply_proposal(
            planning_graph(),
            {"nodes": [{"id": "new_helper", "dependencies": []}]},
            max_nodes=10,
        )


def test_comment_only_root_mismatch_identifies_the_copy_error_and_legal_repair() -> None:
    dag = planning_graph()
    with pytest.raises(ValueError) as caught:
        apply_proposal(
            dag,
            {
                "nodes": [
                    {
                        "id": "root",
                        "statement": "-- Changed comment.\ntheorem goal (n : Nat) : n = n",
                    }
                ]
            },
            max_nodes=10,
        )

    details = json.loads(str(caught.value).split(": ", 1)[1])
    assert details["error_code"] == "immutable_statement"
    assert details["node_id"] == "root"
    assert details["node_name"] == "goal"
    assert details["file"] == "Main.lean"
    assert details["node_kind"] == "original_root"
    assert details["first_difference"] == {
        "line": 1,
        "column": 4,
        "expected": "Preserve this comment.\ntheorem goal (n : Nat) : n = n",
        "received": "Changed comment.\ntheorem goal (n : Nat) : n = n",
    }
    assert "omit the statement field entirely" in details["repair"]
    assert "cannot be changed or replaced by a new helper ID" in details["repair"]
    assert "comments and whitespace" in details["repair"]


def test_helper_claim_mismatch_requests_a_fresh_identity_and_rewired_dependents() -> None:
    with pytest.raises(ValueError) as caught:
        apply_proposal(
            planning_graph(),
            {"nodes": [{"id": "pending_helper", "statement": "theorem pendingHelper : False"}]},
            max_nodes=10,
        )

    details = json.loads(str(caught.value).split(": ", 1)[1])
    assert details["node_id"] == "pending_helper"
    assert details["node_kind"] == "generated_helper"
    assert details["first_difference"]["expected"] == "True ∧ True"
    assert details["first_difference"]["received"] == "False"
    assert "fresh helper ID and declaration name" in details["repair"]
    assert "update its dependents" in details["repair"]


def test_planning_protocol_uses_existing_node_updates_without_statement_copies() -> None:
    prompt = planning_prompt(reason="Split this obligation.")
    assert "Omit statement, name, and file for existing nodes" in prompt
    assert "Omit unchanged nodes entirely" in prompt
    example = prompt.split("Example existing-node update: ", 1)[1].split("}. New nodes", 1)[0] + "}"
    assert set(json.loads(example)) == {"id", "dependencies", "informal_justification"}
    assert "a COMPLETE Lean declaration ending := by sorry" in prompt
    assert "original statements, including comments and whitespace" in prompt
