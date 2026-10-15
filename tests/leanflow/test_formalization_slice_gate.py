"""Require setup and independent representative-slice review before full translation authority."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import preparation, publish, slice_gate
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    save_revision,
)
from leanflow_cli.formalization.source_review_regions import coalesce_regions


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    paper = tmp_path / "paper.tex"
    paper.write_text(
        r"\begin{definition}Double natural numbers.\end{definition}"
        "\n"
        r"\begin{theorem}Doubling equals addition.\end{theorem}"
        "\n"
        r"\begin{theorem}Addition is commutative.\end{theorem}"
        "\n"
    )
    record = create_document(tmp_path, paper)
    record.update(
        workflow_protocol=2,
        preparation_id="preparation-fixture",
        preparation_plan_id="preparation-plan-fixture",
        slice_source_ids=[item["id"] for item in record["inventory"]["items"][:2]],
        environment_id="checked-env",
        layout={
            "root_file": "Main.lean",
            "module_prefix": "Main.Formalization.Paper",
            "aggregator": "Main/Formalization/Paper.lean",
        },
    )
    record = save_revision(document_directory(tmp_path, record["document_id"]), record)
    monkeypatch.setattr(
        preparation,
        "assert_preparation_current",
        lambda root, expected_id=None, expected_environment=None: {
            "preparation_id": expected_id,
            "environment_id": expected_environment,
        },
    )
    monkeypatch.setattr(
        preparation,
        "load_preparation_receipt",
        lambda root: {
            "preparation_id": record["preparation_id"],
            "preparation_plan_id": record["preparation_plan_id"],
            "source_bundle": record["source_bundle"],
            "plan": {
                "plan_id": record["preparation_plan_id"],
                "slice_source_ids": record["slice_source_ids"],
                "required_imports": ["Init"],
                "mathematical_prerequisites": [],
                "representation_notes": [],
            },
        },
    )
    return tmp_path, record


def _proposal(record: dict[str, Any], *, phase: str, reuse: bool = False) -> dict[str, Any]:
    inventory = record.get("full_inventory", record["inventory"])
    selected = (
        set(record["slice_source_ids"])
        if phase == "representative_slice"
        else {item["id"] for item in inventory["items"] if item.get("scope") != "excluded"}
    )
    regions = (
        slice_gate.slice_regions(record)
        if phase == "representative_slice"
        else {region["id"] for region in inventory["regions"]}
    )
    return {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "phase": phase,
        "units": [
            {
                "id": "unit-" + str(index),
                "required_source_ids": [item["id"]],
                "declarations": [
                    {
                        "name": "Paper.statement_" + str(index),
                        **(
                            {"imports": ["Init"]}
                            if reuse
                            else {"file": "Main/Formalization/Paper/Slice.lean"}
                        ),
                    }
                ],
                "representation_note": "Use naturals and retain exact doubling meaning.",
                "depends_on_units": [],
            }
            for index, item in enumerate(inventory["items"])
            if item["id"] in selected
        ],
        "coverage": [
            {
                "region_id": row["id"],
                "disposition": row["disposition"],
                "item_ids": row.get("item_ids", []),
                "reason": row.get("reason") or "Exact source inspected",
            }
            for row in inventory["regions"]
            if row["id"] in regions
        ],
    }


def _checked(
    root: Path, base: dict[str, Any], *, phase: str, reuse: bool = False
) -> dict[str, Any]:
    record = apply_proposal(base, _proposal(base, phase=phase, reuse=reuse))
    store = Path(base["_directory"])
    checksum = put_blob(store / "blobs", b"def double (n : Nat) : Nat := n + n\n")
    record["lean_files"] = {} if reuse else {"Main/Formalization/Paper/Slice.lean": checksum}
    record["checks"] = {
        "accepted": True,
        "declarations": [
            {
                "name": ref["name"],
                "module": "Init" if reuse else "Main.Formalization.Paper.Slice",
                "declaration_origin": "dependency" if reuse else "generated",
                "kind": "definition" if index == 0 else "theorem",
                "type_hash": "type-" + str(index),
                "body_hash": "body-" + str(index),
                "conditional": False,
                "depends_on": [],
                **({} if reuse else {"file": ref["file"]}),
            }
            for index, unit in enumerate(record["units"])
            for ref in unit["declarations"]
        ],
        "obligations": [],
    }
    record = save_revision(document_directory(root, record["document_id"]), record)
    return {
        "accepted": True,
        "record": record,
        "selection_token": current_selection_token(root, record["document_id"]),
    }


def test_full_proposal_cannot_bypass_preparation_or_source_reviewed_slice(prepared, monkeypatch):
    root, base = prepared
    assert slice_gate.slice_status(root, base)["phase"] == "representative_slice"
    with pytest.raises(ValueError, match="cannot bypass"):
        slice_gate.require_stage(root, base, "complete")
    checked = _checked(root, base, phase="representative_slice")
    with pytest.raises(ValueError, match="source review acceptance"):
        slice_gate.save_reviewed_slice(root, checked, {"accepted": False})
    assert slice_gate.slice_status(root, base)["phase"] == "representative_slice"
    with pytest.raises(ValueError, match="representative_slice"):
        publish.publish_checked(root, {}, {}, checked, {"accepted": True}, root / "publish")
    assert not (root / "Main.lean").exists()
    monkeypatch.setattr(
        preparation,
        "assert_preparation_current",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Preparation stale")),
    )
    with pytest.raises(ValueError, match="Preparation stale"):
        slice_gate.require_stage(root, base, "representative_slice")


@pytest.mark.parametrize("reuse", [False, True])
def test_corrected_slice_review_pass_allows_complete_translation_without_publication(
    prepared, reuse
):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice", reuse=reuse)
    # A rejected source review supplies correction feedback, never phase authority.
    with pytest.raises(ValueError):
        slice_gate.save_reviewed_slice(
            root, checked, {"accepted": False, "feedback": "Correct the quantifier"}
        )
    reviewed = slice_gate.save_reviewed_slice(
        root, checked, {"accepted": True, "reviews": [{"decision": "PASS"}]}
    )
    assert not reviewed["publication_authority"]
    assert slice_gate.slice_status(root, base)["phase"] == "complete"
    complete = _checked(root, base, phase="complete", reuse=reuse)["record"]
    assert slice_gate.assert_complete_slice(root, complete)["slice_id"] == reviewed["slice_id"]
    assert len(complete["units"]) == 3 and len(checked["record"]["units"]) == 2
    assert (
        complete["inventory"]["audit_complete"]
        and not checked["record"]["inventory"]["audit_complete"]
    )
    assert not (root / "Main.lean").exists()
    if reuse:
        assert reviewed["files"] == {} and complete["checks"]["obligations"] == []


@pytest.mark.parametrize("change", ["file", "declaration", "representation", "known_clause"])
def test_changed_slice_invalidates_expansion_and_preserves_historical_review(prepared, change):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    receipt = slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    complete = _checked(root, base, phase="complete")["record"]
    if change == "file":
        complete["lean_files"]["Main/Formalization/Paper/Slice.lean"] = "different"
    elif change == "declaration":
        complete["checks"]["declarations"][0]["body_hash"] = "different"
    elif change == "representation":
        complete["units"][0]["representation_note"] = "A different representation"
    else:
        complete["units"][0]["clause_dispositions"] = [{"obligation_id": "changed-checklist"}]
    with pytest.raises(ValueError, match="recheck and review phase representative_slice"):
        slice_gate.assert_complete_slice(root, complete)
    assert slice_gate.slice_status(root, base)["phase"] == "representative_slice"
    assert (
        document_directory(root, base["document_id"])
        / "slice-receipts"
        / (receipt["slice_id"] + ".json")
    ).exists()


def test_slice_proposal_keeps_complete_original_evidence_but_no_whole_source_audit(prepared):
    _, base = prepared
    candidate = apply_proposal(base, _proposal(base, phase="representative_slice"))
    assert len(candidate["inventory"]["items"]) == 3 and len(candidate["units"]) == 2
    assert candidate["source_files"] == base["source_files"]
    assert candidate["full_inventory"] == base["inventory"]
    assert set(row["id"] for row in candidate["inventory"]["regions"]) == slice_gate.slice_regions(
        base
    )
    damaged = _proposal(base, phase="representative_slice")
    damaged["units"][0]["required_source_ids"].append(base["inventory"]["items"][-1]["id"])
    with pytest.raises(ValueError, match="only its selected source"):
        apply_proposal(base, damaged)


def test_source_scope_or_preparation_changes_cannot_inherit_old_slice(prepared):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    changed = copy.deepcopy(base)
    changed["inventory"]["items"][0]["scope"] = "excluded"
    assert slice_gate.slice_status(root, changed)["phase"] == "representative_slice"
    changed = copy.deepcopy(base)
    changed["preparation_plan_id"] = "other-plan"
    assert slice_gate.slice_status(root, changed)["phase"] == "representative_slice"


@pytest.mark.parametrize("extra_command", ["", "\nopen Nat\n", "\nset_option maxRecDepth 1000\n"])
def test_only_import_only_aggregator_may_change_during_expansion(prepared, extra_command):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    aggregator = checked["record"]["layout"]["aggregator"]
    store = Path(checked["record"]["_directory"])
    checked["record"]["lean_files"][aggregator] = put_blob(
        store / "blobs", ("import Main.Formalization.Paper.Slice\n" + extra_command).encode()
    )
    checked["record"] = save_revision(store, checked["record"])
    receipt = slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    complete = _checked(root, base, phase="complete")["record"]
    complete["lean_files"][aggregator] = put_blob(
        store / "blobs", b"import Main.Formalization.Paper.Slice\nimport Main.More\n"
    )
    if extra_command:
        assert aggregator in receipt["files"]
        with pytest.raises(ValueError, match="recheck and review phase representative_slice"):
            slice_gate.assert_complete_slice(root, complete)
    else:
        assert aggregator not in receipt["files"]
        assert slice_gate.assert_complete_slice(root, complete) == receipt


@pytest.mark.parametrize("review_passed", [False, True])
def test_relaunch_from_selected_slice_keeps_whole_document_region_denominator(
    prepared, review_passed
):
    root, base = prepared
    base["inventory"]["regions"].append(
        {
            "id": "region-non-item-context",
            "file": "paper.tex",
            "byte_start": 0,
            "byte_end": 0,
            "disposition": "supporting_context",
            "item_ids": [],
            "reason": "Whole-document mathematical context requires review",
        }
    )
    base = save_revision(document_directory(root, base["document_id"]), base)
    checked = _checked(root, base, phase="representative_slice")
    if review_passed:
        slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    relaunched = create_document(root, root / "paper.tex")
    assert relaunched["revision_id"] == checked["record"]["revision_id"]
    if not review_passed:
        corrected = _checked(root, relaunched, phase="representative_slice")
        assert corrected["record"]["full_inventory"] == base["inventory"]
        slice_gate.save_reviewed_slice(root, corrected, {"accepted": True})
        relaunched = create_document(root, root / "paper.tex")
    from leanflow_cli.native.harness_formalization_stages import stage_assignment

    assignment, _ = stage_assignment(
        root,
        {
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": base["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": relaunched["revision_id"],
        },
        {},
        "Translate the complete document",
    )
    assert set(assignment["formalization_stage"]["required_region_ids"]) == {
        row["id"] for row in coalesce_regions(base["inventory"]["regions"])
    }
    assert (
        assignment["source_region_partition"]["canonical_regions"] == base["inventory"]["regions"]
    )
    complete = _checked(root, relaunched, phase="complete")["record"]
    expected = {row["id"] for row in base["inventory"]["regions"]}
    assert {row["id"] for row in complete["inventory"]["regions"]} == expected
    assert "region-non-item-context" in expected
    assert slice_gate.assert_complete_slice(root, complete)
    missing = _proposal(relaunched, phase="complete")
    missing["coverage"] = [
        row for row in missing["coverage"] if row["region_id"] != "region-non-item-context"
    ]
    with pytest.raises(ValueError, match="every selected document region"):
        apply_proposal(relaunched, missing)


def test_source_actions_on_slice_repair_complete_inventory_and_revoke_slice(prepared):
    from leanflow_cli.formalization.actions import apply_action

    root, base = prepared
    base["inventory"]["regions"].append(
        {
            "id": "outside-slice",
            "file": "paper.tex",
            "byte_start": 0,
            "byte_end": 0,
            "disposition": "unresolved",
            "item_ids": [],
            "reason": "Needs explicit coverage decision",
        }
    )
    base = save_revision(document_directory(root, base["document_id"]), base)
    checked = _checked(root, base, phase="representative_slice")
    slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    repaired = copy.deepcopy(checked["record"])
    apply_action(
        repaired,
        {
            "type": "classify_region",
            "region_id": "outside-slice",
            "classification": "supporting_context",
            "reason": "Original context inspected by the user",
        },
    )
    canonical = repaired["full_inventory"]
    outside = next(row for row in canonical["regions"] if row["id"] == "outside-slice")
    assert outside["disposition"] == "supporting_context" and outside["actor"] == "user"
    assert slice_gate.slice_status(root, repaired)["phase"] == "representative_slice"
    last = canonical["items"][-1]
    apply_action(
        repaired,
        {
            "type": "set_scope",
            "item_id": last["id"],
            "scope": "excluded",
            "reason": "Explicit user scope choice after reviewing the complete document",
        },
    )
    assert repaired["full_inventory"]["items"][-1]["scope"] == "excluded"
    assert repaired["inventory"]["items"][-1]["scope"] == "excluded"


def test_complete_gate_rejects_narrowed_coverage_even_with_valid_slice_receipt(prepared):
    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    complete = _checked(root, base, phase="complete")["record"]
    complete["full_inventory"] = copy.deepcopy(base["inventory"])
    complete["inventory"]["regions"] = complete["inventory"]["regions"][:-1]
    with pytest.raises(ValueError, match="Complete source coverage"):
        slice_gate.assert_complete_slice(root, complete)


@pytest.mark.parametrize("action_type", ["add_item", "update_boundary", "retry_extraction"])
def test_slice_source_repairs_update_canonical_items_regions_and_reopen_coverage(
    prepared, action_type
):
    from leanflow_cli.formalization.actions import apply_action

    root, base = prepared
    checked = _checked(root, base, phase="representative_slice")
    slice_gate.save_reviewed_slice(root, checked, {"accepted": True})
    record = copy.deepcopy(checked["record"])
    original_source = (root / "paper.tex").read_bytes()
    last = base["inventory"]["items"][-1]
    action = {"type": action_type, "reason": "Verify and repair the exact frozen source"}
    if action_type != "retry_extraction":
        action["source_refs"] = last["statement_spans"]
    if action_type == "add_item":
        action.update(title="User-detected extra statement", kind="statement")
    elif action_type == "update_boundary":
        first = record["inventory"]["items"][0]
        first["proof_authorization"] = {"authorized": True, "actor": "user"}
        record["full_inventory"]["items"][0]["proof_authorization"] = first["proof_authorization"]
        action["item_id"] = first["id"]
    apply_action(record, action)
    canonical = record["full_inventory"]
    assert canonical["items"] == record["inventory"]["items"]
    assert canonical["audit_complete"] is False
    assert {row["id"] for row in record["inventory"]["regions"]} == slice_gate.slice_regions(record)
    assert len(canonical["regions"]) > len(record["inventory"]["regions"])
    assert (root / "paper.tex").read_bytes() == original_source
    if action_type == "add_item":
        assert len(canonical["items"]) == 4 and canonical["items"][-1]["scope_actor"] == "user"
        incomplete = _proposal(record, phase="complete")
        incomplete["units"].pop()
        with pytest.raises(ValueError, match="Missing source item mappings"):
            apply_proposal(record, incomplete)
    elif action_type == "update_boundary":
        first = canonical["items"][0]
        assert first["text"] == last["text"] and "proof_authorization" not in first
    else:
        assert record["units"] == [] and not any(
            row.get("proof_authorization") for row in canonical["items"]
        )
