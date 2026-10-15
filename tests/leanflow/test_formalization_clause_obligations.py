"""Keep known runtime, domain and qualifier losses from passing the real source-review gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import review, review_cache
from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import create_document, source_bytes
from leanflow_cli.formalization.review_dossier import evidence_documents, mandatory_dossier
from tests.leanflow import test_formalization_revision_review as review_helpers
from tests.leanflow.test_formalization_lean_policy import _row

_BASE_ANSWER = review_helpers.answer


def _record(tmp_path: Path, text: str, disposition: str = "represented") -> dict:
    """Build a frozen source and protected clause registration, without asserting Lean compilation."""
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}\label{thm:known}" + text + r"\end{theorem}" + "\n")
    record = create_document(tmp_path, source)
    item = record["inventory"]["items"][0]
    apply_action(
        record,
        {
            "type": "record_obligation",
            "item_id": item["id"],
            "source_refs": [item["statement_spans"][0]],
            "description": text,
            "reason": "Explicitly identified requirement from source review.",
        },
    )
    obligation = next(iter(obligation_registry(record)))
    file = "Main/Formalization/Doc/Statements.lean"
    raw = b"def core (n : Nat) : Nat := n\n"
    unit = {
        "id": "unit-known",
        "required_source_ids": [item["id"]],
        "declarations": [{"file": file, "name": "core"}],
        "representation_note": "Only the statistical core is implemented; the loss is disclosed.",
        "depends_on_units": [],
        "clause_dispositions": [
            {
                "obligation_id": obligation,
                "disposition": disposition,
                "declaration_names": ["core"],
                "rationale": "The statistical core omits this exact requirement.",
            }
        ],
    }
    record.update(
        units=[unit],
        layout={"library": "Main"},
        environment_id="env-fixture",
        lean_files={file: put_blob(Path(record["_directory"]) / "blobs", raw)},
        checks={
            "accepted": True,
            "declarations": [
                {"name": "core", "kind": "definition", "type": "Nat → Nat", "data_sorry": False}
            ],
        },
    )
    return record


def _answer(dossier, revision, key, *, decision="PASS") -> str:
    """Return structurally exact independent verdicts, including a deliberately overoptimistic PASS."""
    value = json.loads(_BASE_ANSWER(dossier, revision, key, decision=decision))
    units = {unit["id"]: unit for unit in dossier["units"]}
    for verdict in value["verdicts"]:
        verdict["clause_verdicts"] = [
            {
                "obligation_id": row["obligation_id"],
                "decision": decision,
                "rationale": "Compared this exact source clause with its mapped declarations.",
                "source_evidence_ids": ["obligation:" + row["obligation_id"]],
                "declaration_names": row["declaration_names"],
            }
            for row in units[verdict["unit_id"]].get("clause_dispositions", [])
        ]
    return json.dumps(value)


def _proposal(record: dict) -> dict:
    return {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "units": copy.deepcopy(record["units"]),
        "coverage": [
            {
                "region_id": row["id"],
                "item_ids": row.get("item_ids", []),
                "disposition": "item" if row.get("item_ids") else "supporting_context",
                "reason": "Exact source region accounted for.",
            }
            for row in record["inventory"]["regions"]
        ],
    }


@pytest.mark.parametrize(
    "text,disposition",
    [
        ("The generator runs in expected polynomial time.", "omitted"),
        ("For arbitrary random variables A, B and C the bound holds.", "specialized"),
        ("The output has the stated entropy (including the variable-length case).", "unresolved"),
    ],
)
def test_known_clause_loss_blocks_actual_review_despite_disclosure_and_native_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, disposition: str
) -> None:
    record = _record(tmp_path, text, disposition)
    # Preserve the public accepted-path shape: proposal -> complete dossier ->
    # fresh reviewer response -> verdict validation/cache. No preview label is tested.
    record = apply_proposal(record, _proposal(record))
    documents = evidence_documents(record)
    key = next(iter(obligation_registry(record)))
    assert text in documents["obligation:" + key]["source_text"][0]
    monkeypatch.setattr(review_helpers, "answer", _answer)
    calls, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)
    result = review.review_revision(record, **kwargs)
    assert not result["accepted"] and result["status"] == "completed"
    assert len(calls) == 1 and calls[0]["dossier"]["known_obligations"][0]["id"] == key
    verdict = result["reviews"][0]["verdicts"][0]
    assert verdict["decision"] == "BLOCK" and verdict["clause_verdicts"][0]["decision"] == "BLOCK"
    assert key in verdict["issues"][0]
    # A repeated misleading PASS cannot purchase another attempt at the same evidence.
    again = review.review_revision(record, **kwargs)
    assert not again["accepted"] and len(calls) == 1


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "invented", "no_names", "foreign_name"]
)
def test_proposal_requires_exact_known_clause_membership(tmp_path: Path, mutation: str) -> None:
    record = _record(tmp_path, "All quantified domains must be retained.")
    proposal = _proposal(record)
    unit = proposal["units"][0]
    if mutation == "missing":
        unit.pop("clause_dispositions")
    elif mutation == "duplicate":
        unit["clause_dispositions"] *= 2
    elif mutation == "invented":
        unit["clause_dispositions"][0]["obligation_id"] = "unknown"
    elif mutation == "no_names":
        unit["clause_dispositions"][0]["declaration_names"] = []
    else:
        unit["clause_dispositions"][0]["declaration_names"] = ["foreign.declaration"]
    with pytest.raises(ValueError):
        apply_proposal(record, proposal)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "invented", "source_evidence", "declaration_evidence"]
)
def test_review_requires_independent_clause_evidence(tmp_path: Path, mutation: str) -> None:
    record = _record(tmp_path, "The complete claim has a faithful representation.")
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    value = json.loads(_answer(dossier, "revision", "dossier"))
    verdict = value["verdicts"][0]
    if mutation == "missing":
        verdict.pop("clause_verdicts")
    elif mutation == "duplicate":
        verdict["clause_verdicts"] *= 2
    elif mutation == "invented":
        verdict["clause_verdicts"][0]["obligation_id"] = "unknown"
    elif mutation == "source_evidence":
        verdict["clause_verdicts"][0]["source_evidence_ids"] = ["source:paper.tex"]
    else:
        verdict["clause_verdicts"][0]["declaration_names"] = ["foreign.declaration"]
    with pytest.raises(ValueError):
        review.validate_review(
            json.dumps(value),
            dossier,
            "revision",
            "dossier",
            canonical_regions=record["inventory"]["regions"],
        )


def test_exact_clause_checklist_can_pass_without_claiming_automatic_discovery(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, "The complete claim has a faithful representation.")
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    accepted = review.validate_review(
        _answer(dossier, "revision", "dossier"),
        dossier,
        "revision",
        "dossier",
        canonical_regions=record["inventory"]["regions"],
    )
    assert accepted["accepted"]
    assert "not claim automatic discovery" in dossier["clause_policy"]


@pytest.mark.parametrize(
    "mutation", [None, "data_hole", "type_hole", "non_prop", "not_pending", "definition"]
)
def test_pending_clause_means_an_actual_prop_proof_slot(
    tmp_path: Path, mutation: str | None
) -> None:
    record = _record(tmp_path, "The expected running time is polynomial.", "pending_proof")
    row = _row("core")
    if mutation == "data_hole":
        row["data_sorry"] = True
    elif mutation == "type_hole":
        row["direct_type_sorry"] = True
    elif mutation == "non_prop":
        row["is_prop"] = False
    elif mutation == "not_pending":
        row["pending"] = False
    elif mutation == "definition":
        row["kind"] = "definition"
    record["checks"]["declarations"] = [row]
    if mutation is not None:
        with pytest.raises(ValueError, match="actual named Prop proof slot"):
            mandatory_dossier(record, record["units"], coherence=True)
    else:
        dossier = mandatory_dossier(record, record["units"], coherence=True)
        assert review.validate_review(
            _answer(dossier, "revision", "dossier"),
            dossier,
            "revision",
            "dossier",
            canonical_regions=record["inventory"]["regions"],
        )["accepted"]


def test_obligation_repair_is_frozen_and_non_authorizing(tmp_path: Path) -> None:
    record = _record(tmp_path, "Retain the exact trailing qualifier.", "unresolved")
    item = record["inventory"]["items"][0]
    row = item["known_obligations"][0]
    assert row["actor"] == "user" and row["source_bundle"] == record["source_bundle"]
    assert item["scope"] == "included" and "proof_authorization" not in item
    assert not record["inventory"]["audit_complete"] and record["status"] != "accepted"
    frozen = source_bytes(record, "paper.tex")
    (tmp_path / "paper.tex").write_text("Changed live paper; not review evidence.")
    docs = evidence_documents(record)
    assert "exact trailing qualifier" in docs["obligation:" + row["id"]]["source_text"][0]
    assert source_bytes(record, "paper.tex") == frozen
    corrupted = copy.deepcopy(record)
    corrupted["inventory"]["items"][0]["known_obligations"][0]["source_refs"][0][
        "file_hash"
    ] = "sha256:other"
    with pytest.raises(ValueError, match="frozen source bytes"):
        obligation_registry(corrupted)


def test_review_contract_upgrade_invalidates_old_selected_policy(tmp_path, monkeypatch) -> None:
    record = _record(tmp_path, "Exact clause.")
    configuration = review.review_configuration("claude", "fixture", "max", {})
    assert configuration["protocol"] == "source-review-v10"
    record["review_policy"] = {**configuration, "protocol": "source-review-v9"}
    _, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Reviewer contract changed"):
        review.review_revision(record, **kwargs)


def test_reused_frozen_catalog_preserves_negative_cache_identity(tmp_path) -> None:
    record = _record(tmp_path, "Exact clause.")
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    config = review.review_configuration("claude", "fixture", "max", {})
    assert review_cache.rejection_key(record, dossier, config) == review_cache.rejection_key(
        record, dossier, config, documents=evidence_documents(record)
    )


def test_mapping_repair_preserves_explicit_clause_checklist_without_approval(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, "Preserve the polynomial runtime requirement.", "unresolved")
    unit = record["units"][0]
    action = {
        "type": "set_mapping",
        "unit_id": unit["id"],
        "source_ids": unit["required_source_ids"],
        "lean_refs": unit["declarations"],
        "rationale": "Explicit mapping repair preserves the unresolved requirement.",
        "clause_dispositions": copy.deepcopy(unit["clause_dispositions"]),
    }
    apply_action(record, action)
    assert record["units"][0]["clause_dispositions"] == action["clause_dispositions"]
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    assert not review.validate_review(
        _answer(dossier, "revision", "dossier"),
        dossier,
        "revision",
        "dossier",
        canonical_regions=record["inventory"]["regions"],
    )["accepted"]


def test_frozen_reextraction_preserves_known_clauses_and_revokes_mapping(tmp_path: Path) -> None:
    record = _record(tmp_path, "The algorithm runs in polynomial time.", "omitted")
    before = copy.deepcopy(obligation_registry(record))
    apply_action(record, {"type": "retry_extraction", "reason": "Try current lexical extraction."})
    assert obligation_registry(record) == before
    assert not record["units"] and not record["inventory"]["audit_complete"]


def test_reextraction_cannot_drop_a_registered_manually_identified_clause(tmp_path: Path) -> None:
    record = _record(tmp_path, "Preserve the exact expected runtime requirement.")
    refs = record["inventory"]["items"][0]["statement_spans"]
    apply_action(
        record,
        {
            "type": "add_item",
            "title": "Manual runtime requirement",
            "source_refs": refs,
            "reason": "Record an explicitly identified source item.",
        },
    )
    manual = record["inventory"]["items"][-1]
    apply_action(
        record,
        {
            "type": "record_obligation",
            "item_id": manual["id"],
            "source_refs": refs,
            "description": "The expected runtime remains part of the claim.",
            "reason": "Source review identified this requirement.",
        },
    )
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="cannot discard known source clauses"):
        apply_action(record, {"type": "retry_extraction", "reason": "Try current extraction."})
    assert record == before
