"""Keep correction navigation and saved evidence distinct from current whole-project acceptance."""

from __future__ import annotations

import copy

import pytest

from leanflow_cli.formalization import corrections
from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.service_corrections import (
    authority_options,
    item_corrections,
    mark_current_corrections,
)
from leanflow_cli.formalization.service_snapshot import _ref, snapshot
from tests.leanflow.test_formalization_corrections import _bind, _review
from tests.leanflow.test_formalization_corrections import inputs as _inputs

inputs = _inputs


@pytest.mark.parametrize("bundle", [None, "", "unbound", 7, "a" * 63])
def test_legacy_unbound_inventory_offers_no_authority_actions(bundle):
    record = {"source_bundle": bundle}
    assert authority_options(record, {"items": [{"id": "old-item"}]}) == {"old-item": []}


def test_paper_policy_scope_is_never_truncated_to_the_first_sixty_four_items(tmp_path):
    paper = tmp_path / "large.tex"
    paper.write_text(
        "\n".join(r"\begin{theorem}Claim " + str(i) + r".\end{theorem}" for i in range(65))
    )
    record = create_document(tmp_path, paper)
    for item in record["inventory"]["items"]:
        item["scope"] = "included"
    options = authority_options(record, record["inventory"])
    assert len(options) == 65
    assert all(
        all(row["action"]["kind"] != "bounded_policy" for row in rows) for rows in options.values()
    )
    assert all(
        len(row["action"]["source_item_ids"]) == 1 for rows in options.values() for row in rows
    )


def _projection(inputs):
    record = _bind(inputs)
    for item in record["inventory"]["items"]:
        apply_action(
            record,
            {
                "type": "set_scope",
                "item_id": item["id"],
                "scope": "included",
                "reason": "Keep the complete source for this projection fixture",
            },
        )
    unit = record["units"][0]
    unit["representation_note"] = "Keep the adopted reading separate from its original source."
    unit["declarations"][0]["file"] = "Paper.lean"
    record["units"].append(
        {
            "id": "dependent",
            "required_source_ids": [record["inventory"]["items"][1]["id"]],
            "declarations": [{"name": "Paper.dependent", "file": "Paper.lean"}],
            "depends_on_units": [unit["id"]],
            "representation_note": "Uses the recorded construction.",
        }
    )
    for row in record["checks"]["declarations"]:
        row.update(file="Paper.lean", line_start=7, pretty_type="a readable compiled proposition")
    # Rebind because the exact checked context now includes the display metadata.
    corrections.bind_correction_evidence(record, record["checks"])
    _, review = _review(record)
    receipt = {
        "status": "published",
        "revision_id": record["revision_id"],
        "reviews": [review],
        "checks": record["checks"],
    }
    return record, receipt


def _rows(record, receipt):
    return item_corrections(
        record,
        record["inventory"]["items"][0]["id"],
        references=[ref for unit in record["units"] for ref in unit["declarations"]],
        checks=record["checks"],
        receipt=receipt,
        artifacts={"Paper.lean": "lean-artifact"},
        source_ref=lambda span: _ref(record, span, literal_excerpt=True),
    )


def test_exact_original_adopted_compiled_evidence_and_dependents_are_navigable(inputs):
    record, receipt = _projection(inputs)
    original = copy.deepcopy(record["source_files"]), copy.deepcopy(record["inventory"])
    row = _rows(record, receipt)[0]
    assert row["original"]["literal_reading"] == inputs[1]["original"]["literal_reading"]
    assert row["original"]["source_refs"][0]["artifact_id"]
    assert row["original"]["source_refs"][0]["line_start"] == 1
    assert "one third" in row["original"]["source_refs"][0]["text"]
    assert row["adopted"]["reading"] == inputs[1]["adopted"]["reading"]
    lean = row["adopted"]["lean_refs"][0]
    assert lean["artifact_id"] == "lean-artifact" and lean["line_start"] == 7
    assert lean["type"] == "a readable compiled proposition"
    assert all(lean[key] for key in ("type_hash", "body_hash", "definition_context_hash"))
    assert row["evidence"][0]["proof"]["artifact_id"] == "lean-artifact"
    assert row["evidence_status"] == "saved_checked" and row["acceptance_status"] == "saved"
    assert row["reviews"][0]["binding_matches"] is True
    assert {unit["unit_id"] for unit in row["dependents"]} == {"unit", "dependent"}
    assert (record["source_files"], record["inventory"]) == original


def test_correction_original_excerpt_comes_from_frozen_bytes_not_authored_ref_text(inputs):
    inputs[1]["original"]["source_refs"][0]["text"] = "FORGED original reading"
    record, receipt = _projection(inputs)
    displayed = _rows(record, receipt)[0]["original"]["source_refs"][0]["text"]
    assert "one third" in displayed and "FORGED" not in displayed


def test_saved_pass_requires_existing_whole_revision_current_acceptance(inputs):
    record, receipt = _projection(inputs)
    rows = [{"corrections": _rows(record, receipt)}]
    mark_current_corrections(rows, accepted_handoff=False)
    assert rows[0]["corrections"][0]["acceptance_status"] == "saved"
    mark_current_corrections(rows, accepted_handoff=True)
    assert rows[0]["corrections"][0]["acceptance_status"] == "current"
    receipt["reviews"][0]["correction_verdicts"][0]["evidence_id"] = "other-saved-evidence"
    rows = [{"corrections": _rows(record, receipt)}]
    mark_current_corrections(rows, accepted_handoff=True)
    assert rows[0]["corrections"][0]["acceptance_status"] == "saved"
    assert rows[0]["corrections"][0]["reviews"][0]["binding_matches"] is False


def test_revocation_keeps_literal_adopted_and_saved_evidence_visible(inputs):
    record, receipt = _projection(inputs)
    action = {**inputs[2], "version": 2, "enabled": False}
    corrections.record_correction_authority(record, action)
    row = _rows(record, receipt)[0]
    assert row["original"]["literal_reading"] and row["adopted"]["reading"]
    assert row["authority_current"] is False and row["evidence_status"] == "stale"
    assert row["authorities"][0]["current"] is False
    assert row["adopted"]["lean_refs"][0]["body_hash"]
    assert row["evidence"][0]["proof"]["definition_context_hash"]
    items = [{"corrections": [row]}]
    mark_current_corrections(items, accepted_handoff=True)
    assert row["acceptance_status"] != "current"


def test_paper_policy_templates_cover_all_included_items_and_increment_user_versions(inputs):
    record, _ = _projection(inputs)
    item = record["inventory"]["items"][0]
    option = next(
        value
        for value in authority_options(record, record["inventory"])[item["id"]]
        if value["action"]["kind"] == "bounded_policy"
    )
    action = option["action"]
    assert action["version"] == 1 and option["enabled"] is False
    assert set(action["source_item_ids"]) == {item["id"] for item in record["inventory"]["items"]}
    assert action["source_bundle"] == record["source_bundle"] and action["clause_ids"] == []
    corrections.record_correction_authority(record, action)
    option = authority_options(record, record["inventory"])[item["id"]][0]
    assert option["enabled"] is True and option["action"]["version"] == 2
    corrections.record_correction_authority(record, {**option["action"], "enabled": False})
    option = authority_options(record, record["inventory"])[item["id"]][0]
    assert option["enabled"] is False and option["action"]["version"] == 3


def test_historical_snapshot_never_promotes_saved_corrections_to_current(
    inputs, tmp_path, monkeypatch
):
    record, receipt = _projection(inputs)
    record["lean_files"] = {"Paper.lean": "a" * 64}
    monkeypatch.setattr(
        "leanflow_cli.formalization.service_snapshot.load_receipt", lambda _: receipt
    )
    shown = snapshot(tmp_path, record, verify_current=False)
    assert shown["source_bundle"] == record["source_bundle"]
    assert shown["currentness"] == "historical" and shown["can_prove"] is False
    assert shown["items"][0]["corrections"][0]["acceptance_status"] == "saved"
    assert shown["items"][0]["proof_status"] == "pending"
    record["corrections"] = [{"malformed": True}, None]
    with pytest.raises(ValueError, match="Invalid saved correction projection"):
        snapshot(tmp_path, record, verify_current=False)
