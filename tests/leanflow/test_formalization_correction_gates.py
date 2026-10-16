"""Exercise correction records through the existing clause, review and slice gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import corrections, preparation, review, slice_gate
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.records import (
    current_selection_token,
    document_directory,
    save_revision,
)
from leanflow_cli.formalization.review_cache import rejection_key
from leanflow_cli.formalization.review_dossier import evidence_documents, mandatory_dossier
from tests.leanflow.test_formalization_clause_obligations import _answer
from tests.leanflow.test_formalization_corrections import _review
from tests.leanflow.test_formalization_corrections import inputs as _inputs


@pytest.fixture
def checked_correction(tmp_path, monkeypatch):
    """Supply synthetic replay rows to test integration, without claiming a real Lean check."""
    record, proposal, action = _inputs.__wrapped__(tmp_path, monkeypatch)
    source_base = copy.deepcopy(record)
    path = "Main/Formalization/Paper/Statements.lean"
    record["layout"] = {"root_file": "Main.lean", "aggregator": "Main/Formalization/Paper.lean"}
    record["lean_files"] = {
        path: put_blob(Path(record["_directory"]) / "blobs", b"-- synthetic gate fixture\n")
    }
    record["units"][0].update(
        representation_note="Use the explicitly recorded equal-weight normalization repair.",
        depends_on_units=[],
    )
    record["units"][0]["declarations"][0]["file"] = path
    for row in record["checks"]["declarations"]:
        row["file"] = path
    corrections.apply_corrections(record, [proposal])
    corrections.bind_correction_evidence(record, record["checks"])
    return record, source_base, action


def test_correction_and_clause_need_pass_in_the_same_exact_review(checked_correction):
    record, _, _ = checked_correction
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    packet = json.loads(_answer(dossier, record["revision_id"], "exact-dossier"))
    _, correction_packet = _review(record)
    with pytest.raises(ValueError, match="correction"):
        review.validate_review(
            json.dumps(packet),
            dossier,
            record["revision_id"],
            "exact-dossier",
            canonical_regions=record["inventory"]["regions"],
        )
    packet["correction_verdicts"] = correction_packet["correction_verdicts"]
    accepted = review.validate_review(
        json.dumps(packet),
        dossier,
        record["revision_id"],
        "exact-dossier",
        canonical_regions=record["inventory"]["regions"],
    )
    assert accepted["accepted"]
    packet["correction_verdicts"][0].update(
        decision="BLOCK", issues=["The source does not select this repair over the alternative."]
    )
    blocked = review.validate_review(
        json.dumps(packet),
        dossier,
        record["revision_id"],
        "exact-dossier",
        canonical_regions=record["inventory"]["regions"],
    )
    assert not blocked["accepted"]
    assert blocked["verdicts"][0]["clause_verdicts"][0]["decision"] == "BLOCK"
    documents = evidence_documents(record)
    assert "correction:" + record["corrections"][0]["id"] in documents
    assert "correction-evidence:" + record["corrections"][0]["id"] in documents
    assert "authority:" + record["correction_authorities"][0]["id"] in documents


def test_slice_reuse_preserves_exact_correction_without_blocking_base_admission(
    checked_correction, tmp_path, monkeypatch
):
    record, source_base, _ = checked_correction
    for candidate in (record, source_base):
        candidate.update(
            workflow_protocol=2,
            preparation_id="prepared",
            preparation_plan_id="planned",
            slice_source_ids=[record["corrections"][0]["source_item_id"]],
        )
    record["proposal_phase"] = "representative_slice"
    record = save_revision(document_directory(tmp_path, record["document_id"]), record)
    monkeypatch.setattr(preparation, "assert_preparation_current", lambda *args, **kwargs: {})
    _, review_packet = _review(record)
    slice_gate.save_reviewed_slice(
        tmp_path,
        {
            "accepted": True,
            "record": record,
            "selection_token": current_selection_token(tmp_path, record["document_id"]),
        },
        {"accepted": True, "reviews": [review_packet]},
    )
    assert slice_gate.slice_status(tmp_path, source_base)["phase"] == "complete"
    complete = copy.deepcopy(record)
    complete["proposal_phase"] = "complete"
    assert slice_gate.slice_status(tmp_path, complete)["phase"] == "complete"
    outside = copy.deepcopy(complete["corrections"][0])
    outside.update(id="outside-slice", source_item_id=record["inventory"]["items"][1]["id"])
    complete["corrections"].append(outside)
    assert slice_gate.slice_status(tmp_path, complete)["phase"] == "complete"
    complete["corrections"][0]["adopted"]["reading"] = "A changed normalization interpretation."
    assert slice_gate.slice_status(tmp_path, complete)["phase"] == "representative_slice"
    complete["corrections"] = []
    assert slice_gate.slice_status(tmp_path, complete)["phase"] == "representative_slice"
    source_base["correction_authorities"][0]["enabled"] = False
    assert slice_gate.slice_status(tmp_path, source_base)["phase"] == "representative_slice"


def test_transient_recheck_metadata_cannot_buy_another_correction_review(checked_correction):
    record, _, _ = checked_correction
    dossier = mandatory_dossier(record, record["units"], coherence=True)
    evidence = copy.deepcopy(record["checks"]["correction_evidence"])
    key = rejection_key(record, dossier, {})
    record["checks"].update(command=["/temporary/recheck/lean"], duration_s=91.2, process_id=1234)
    record["checks"]["kernel_replay"]["artifact_directory"] = "/another/frozen/project"
    for row in record["checks"]["declarations"]:
        row.update(artifact_path="/another/frozen/project/module.olean", line_start=71)
    assert corrections.bind_correction_evidence(record, record["checks"]) == evidence
    assert rejection_key(record, dossier, {}) == key
    pending = record["checks"]["declarations"][0]
    pending["body_hash"] = "a-sorry-label-at-a-different-source-location"
    corrections.bind_correction_evidence(record, record["checks"])
    assert rejection_key(record, dossier, {}) == key
    record["checks"]["declarations"][1]["body_hash"] = "different-closed-local-goal"
    corrections.bind_correction_evidence(record, record["checks"])
    assert rejection_key(record, dossier, {}) != key
