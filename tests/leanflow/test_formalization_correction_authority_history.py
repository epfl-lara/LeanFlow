"""Keep trusted correction decisions monotonic through real selection and source intake."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from leanflow_cli.formalization import corrections, freshness
from leanflow_cli.formalization.evidence import digest, durable_json, identity
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.formalization.lean_policy import POLICY_VERSION
from leanflow_cli.formalization.records import (
    create_document,
    document_directory,
    load_document,
    load_receipt,
    new_revision,
    save_receipt,
    save_revision,
)
from leanflow_cli.formalization.service import act_document
from tests.leanflow import test_formalization_corrections as correction_tests
from tests.leanflow.test_formalization_handoff import _publish_fixture_controls

correction_inputs = correction_tests.inputs


@pytest.fixture
def accepted(correction_inputs, tmp_path, monkeypatch):
    """Publish a synthetic checked receipt through the real publication kernel."""
    record = correction_tests._bind(correction_inputs)
    record["allowed_axioms"] = []
    record = save_revision(Path(record["_directory"]), record)
    _, review = correction_tests._review(record)
    payload = {
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "environment_id": record["environment_id"],
        "acceptance_policy": {
            "policy_id": POLICY_VERSION,
            "checker_sha256": digest(INSPECTOR),
            "allowed_axioms": [],
        },
        "source_corrections": record["checks"]["correction_evidence"],
    }
    handoff = {**payload, "handoff_id": "handoff-" + identity(payload)}
    directory = document_directory(tmp_path, record["document_id"])
    durable_json(directory / "handoff.json", handoff)
    receipt = save_receipt(
        record,
        {
            "status": "published",
            "revision_id": record["revision_id"],
            "handoff": handoff,
            "reviews": [review],
        },
    )
    _publish_fixture_controls(
        tmp_path, record["document_id"], record["revision_id"], staged=directory
    )
    monkeypatch.setattr(freshness, "environment_identity", lambda root: record["environment_id"])
    initial = freshness.published_acceptance_freshness(tmp_path, record, receipt)
    assert initial["current"], initial
    return record, receipt, correction_inputs[2]


def _decision(root, record, action, version, enabled):
    act_document(
        root,
        record["document_id"],
        record["revision_id"],
        {**action, "version": version, "enabled": enabled},
        acknowledge_only=True,
    )
    return load_document(root, record["document_id"], reconcile_receipt=False)


@pytest.mark.parametrize("regrant", [False, True])
def test_selecting_old_acceptance_retains_revocation_and_requires_fresh_review(
    accepted, tmp_path, regrant
):
    original, receipt, action = accepted
    source = tmp_path / "paper.tex"
    unchanged = source.read_bytes()
    current = _decision(tmp_path, original, action, 2, False)
    if regrant:
        current = _decision(tmp_path, current, action, 3, True)
    latest = copy.deepcopy(current["correction_authorities"])
    result = act_document(
        tmp_path,
        current["document_id"],
        current["revision_id"],
        {"type": "select_revision", "revision_id": original["revision_id"]},
        acknowledge_only=True,
    )
    selected = load_document(tmp_path, current["document_id"], reconcile_receipt=False)
    assert result["revision_id"] == selected["revision_id"] != original["revision_id"]
    assert selected["correction_authorities"] == latest
    assert selected["status"] == "inventory_pending" and selected["checks"] == {}
    assert selected["reviews"] == [] and selected["handoff"] is None
    assert load_receipt(selected) is None
    assert load_receipt(original) == receipt
    assert source.read_bytes() == unchanged
    # Restoring the original source/handoff/receipt cannot resurrect v1 authority.
    state = freshness.published_acceptance_freshness(tmp_path, original, receipt)
    assert state["status"] == "acceptance_stale" and not state["current"]
    assert "Correction authority decisions changed" in state["reason"]


def test_equal_history_selection_keeps_existing_immutable_revision(accepted, tmp_path):
    original, receipt, _ = accepted
    result = act_document(
        tmp_path,
        original["document_id"],
        original["revision_id"],
        {"type": "select_revision", "revision_id": original["revision_id"]},
        acknowledge_only=True,
    )
    assert result["revision_id"] == original["revision_id"]
    assert load_receipt(load_document(tmp_path, original["document_id"])) == receipt
    assert freshness.published_acceptance_freshness(tmp_path, original, receipt)["current"]


def test_selection_refuses_divergent_trusted_authority_branches(accepted, tmp_path):
    original, _, action = accepted
    revoked = _decision(tmp_path, original, action, 2, False)
    alternate = new_revision(original)
    corrections.record_correction_authority(alternate, {**action, "version": 2, "enabled": True})
    alternate = save_revision(Path(original["_directory"]), alternate)
    with pytest.raises(ValueError, match="divergent trusted correction authority history"):
        act_document(
            tmp_path,
            original["document_id"],
            alternate["revision_id"],
            {"type": "select_revision", "revision_id": revoked["revision_id"]},
            acknowledge_only=True,
        )
    assert (
        load_document(tmp_path, original["document_id"], reconcile_receipt=False)[
            "correction_authorities"
        ]
        == alternate["correction_authorities"]
    )


def test_direct_source_reintake_preserves_history_without_regranting_changed_source(
    accepted, tmp_path
):
    original, receipt, action = accepted
    current = _decision(tmp_path, original, action, 2, False)
    source = tmp_path / "paper.tex"
    source.write_bytes(source.read_bytes() + b"\n% Changed frozen source bundle\n")
    unchanged = source.read_bytes()
    refreshed = create_document(tmp_path, source, reconcile_receipt=False)
    assert refreshed["source_bundle"] != original["source_bundle"]
    assert refreshed["correction_authorities"] == current["correction_authorities"]
    assert refreshed["checks"] == {} and load_receipt(refreshed) is None
    # The history survives another load/intake, rather than an in-memory overlay.
    assert (
        create_document(tmp_path, source, reconcile_receipt=False)["correction_authorities"]
        == current["correction_authorities"]
    )
    assert source.read_bytes() == unchanged
    revised_action = {
        **action,
        "source_bundle": refreshed["source_bundle"],
        "source_item_ids": [refreshed["inventory"]["items"][0]["id"]],
        "clause_ids": [],
    }
    with pytest.raises(ValueError, match="versioned trusted user decision"):
        _decision(tmp_path, refreshed, revised_action, 1, True)
    renewed = _decision(tmp_path, refreshed, revised_action, 3, True)
    assert [row["version"] for row in renewed["correction_authorities"]] == [1, 2, 3]
    assert renewed["correction_authorities"][-1]["source_bundle"] == refreshed["source_bundle"]
    assert not freshness.published_acceptance_freshness(tmp_path, original, receipt)["current"]


def test_old_record_authority_guard_is_read_only_and_never_reconciles_receipts(
    accepted, tmp_path, monkeypatch
):
    original, _, action = accepted
    _decision(tmp_path, original, action, 2, False)

    def forbidden(*args, **kwargs):
        pytest.fail("Authority currentness attempted receipt reconciliation")

    monkeypatch.setattr(
        "leanflow_cli.formalization.receipt_recovery.reconcile_committed_receipt", forbidden
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="authority decisions changed"):
        corrections.assert_current_correction_authority(tmp_path, original)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_concurrent_selection_and_user_decision_cannot_lose_authority_history(accepted, tmp_path):
    original, _, action = accepted
    current = _decision(tmp_path, original, action, 2, False)
    start = Barrier(2)

    def change(value):
        start.wait(5)
        try:
            act_document(
                tmp_path,
                current["document_id"],
                current["revision_id"],
                value,
                acknowledge_only=True,
            )
            return "committed"
        except ValueError as error:
            assert "revision changed" in str(error)
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        selection = pool.submit(
            change, {"type": "select_revision", "revision_id": original["revision_id"]}
        )
        decision = pool.submit(change, {**action, "version": 3, "enabled": True})
        assert sorted([selection.result(10), decision.result(10)]) == ["committed", "stale"]
    selected = load_document(tmp_path, original["document_id"], reconcile_receipt=False)
    assert selected["correction_authorities"][:2] == current["correction_authorities"]
    assert len(selected["correction_authorities"]) in {2, 3}
    assert selected["checks"] == {} and load_receipt(selected) is None
