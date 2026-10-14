"""Invalidate published display authority before any proof-certificate storage exists."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from leanflow_cli.formalization import freshness, handoff
from leanflow_cli.formalization.evidence import digest, durable_json, put_blob
from leanflow_cli.formalization.proof_receipts import current_proof_receipt
from leanflow_cli.formalization.records import (
    document_directory,
    load_document,
    load_receipt,
    new_revision,
    save_receipt,
    save_revision,
)
from leanflow_cli.formalization.service import show_document
from tests.leanflow import test_formalization_handoff as handoff_tests

reviewed_project = handoff_tests.reviewed


def _published(root: Path, zero_obligations: bool) -> dict:
    record = load_document(root, "paper")
    if not zero_obligations:
        return record
    registry = copy.deepcopy(record["checks"])
    record = new_revision(record)
    registry["obligations"] = []
    registry["declarations"][0]["pending"] = False
    path = root / "src/Main.lean"
    path.write_text(
        path.read_text().replace("target : True := by sorry", "target : True := by trivial")
    )
    record["checks"] = registry
    record["project_source_hashes"]["src/Main.lean"] = digest(path.read_bytes())
    record["lean_files"]["src/Main.lean"] = digest(path.read_bytes())
    directory = document_directory(root, "paper", published=True)
    record = save_revision(directory, record)
    result = handoff.build_handoff(
        root,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=registry,
        files=record["project_source_hashes"],
        environment_id="environment",
    )
    durable_json(directory / "handoff.json", result)
    save_receipt(
        record,
        {
            "status": "published",
            "revision_id": record["revision_id"],
            "handoff": result,
            "reviews": [
                {
                    "verdicts": [
                        {"unit_id": "target", "source_ids": ["source-1"], "decision": "PASS"}
                    ]
                }
            ],
        },
    )
    return record


@pytest.mark.parametrize("zero_obligations", [False, True])
@pytest.mark.parametrize("changed_input", ["compiler", "dependency", "controller"])
def test_snapshot_invalidates_pending_and_zero_hole_acceptance_without_certificates(
    reviewed_project, monkeypatch, zero_obligations, changed_input
):
    root, _ = reviewed_project
    record = _published(root, zero_obligations)
    watched = {name: root / f"{name}.bytes" for name in ("compiler", "dependency", "controller")}
    for path in watched.values():
        path.write_bytes(b"accepted contents")

    def environment_identity(path):
        return (
            "environment"
            if all(item.read_bytes() == b"accepted contents" for item in watched.values())
            else "changed"
        )

    monkeypatch.setattr(freshness, "environment_identity", environment_identity)
    directory = document_directory(root, "paper", published=True)
    assert not (directory / "proof-revisions").exists()
    original_receipt = load_receipt(record)
    initial = show_document(root, "paper")
    assert initial["can_prove"] is (not zero_obligations)
    assert initial["proof_completion"] == ("checked" if zero_obligations else "pending")
    watched[changed_input].write_bytes(b"changed bound contents")
    invalid = show_document(root, "paper")
    assert invalid["status"] == "environment_stale"
    assert invalid["acceptance_freshness"]["current"] is False
    assert "environment changed" in invalid["acceptance_freshness"]["reason"]
    assert not invalid["can_prove"] and invalid["handoff"] is None
    assert invalid["proof_completion"] == "pending"
    assert not invalid["proof_revision_id"]
    assert load_receipt(record) == original_receipt
    assert not (directory / "proof-revisions").exists()
    with pytest.raises(ValueError, match="environment changed"):
        current_proof_receipt(root, record, original_receipt)


@pytest.mark.parametrize("zero_obligations", [False, True])
def test_checker_policy_change_is_explicit_stale_display(
    reviewed_project, monkeypatch, zero_obligations
):
    root, _ = reviewed_project
    record = _published(root, zero_obligations)
    monkeypatch.setattr(freshness, "environment_identity", lambda root: "environment")
    monkeypatch.setattr(freshness, "INSPECTOR", b"new kernel-checker bytes")
    invalid = show_document(root, "paper")
    assert invalid["status"] == "checker_stale"
    assert not invalid["can_prove"] and invalid["proof_completion"] == "pending"
    assert "Lean checker changed" in invalid["acceptance_freshness"]["reason"]
    assert load_receipt(record)["status"] == "published"


def test_freshness_validation_is_read_only_and_does_not_need_certificate_directory(
    reviewed_project, monkeypatch
):
    root, _ = reviewed_project
    record = _published(root, False)
    monkeypatch.setattr(freshness, "environment_identity", lambda root: "environment")
    before = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert freshness.published_acceptance_freshness(root, record, load_receipt(record)) == {
        "current": True,
        "status": "current",
        "reason": "",
    }
    after = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "damage", ["region", "item", "mapping", "representative_slice", "reviewed_context", "none"]
)
def test_zero_obligation_completion_requires_complete_coverage_without_certificates(
    reviewed_project, damage
):
    root, _ = reviewed_project
    published = _published(root, True)
    record = new_revision(published)
    record["checks"] = copy.deepcopy(published["checks"])
    record.update(workflow_protocol=2, proposal_phase="complete", slice_source_ids=["source-1"])
    directory = document_directory(root, "paper", published=True)
    record["source_files"] = {
        "src/Main.lean": put_blob(directory / "blobs", (root / "src/Main.lean").read_bytes())
    }
    record["inventory"].update(
        audit_complete=True,
        regions=[
            {
                "id": "region-context",
                "file": "src/Main.lean",
                "byte_start": 0,
                "byte_end": 0,
                "disposition": "supporting_context",
                "item_ids": [],
                "reason": "All mathematical context was independently reviewed",
            }
        ],
    )
    record["full_inventory"] = copy.deepcopy(record["inventory"])
    if damage == "region":
        record["inventory"]["regions"] = []
    elif damage == "item":
        record["inventory"]["items"] = []
    elif damage == "mapping":
        record["units"][0]["required_source_ids"] = []
    elif damage == "reviewed_context":
        record["full_inventory"]["regions"][0].update(
            disposition="unresolved", reason="The original context needed a complete source audit"
        )
    elif damage == "representative_slice":
        record["proposal_phase"] = "representative_slice"
        record["full_inventory"]["regions"][0]["disposition"] = "unresolved"
    record = save_revision(directory, record)
    result = handoff.build_handoff(
        root,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=record["checks"],
        files=record["project_source_hashes"],
        environment_id=record["environment_id"],
    )
    durable_json(directory / "handoff.json", result)
    save_receipt(
        record,
        {"status": "published", "revision_id": record["revision_id"], "handoff": result},
    )
    assert not (directory / "proof-revisions").exists()
    snapshot = show_document(root, "paper")
    assert not snapshot["can_prove"]
    assert {region["id"] for region in snapshot["regions"]} == {"region-context"}
    if damage in {"none", "reviewed_context"}:
        assert snapshot["proof_completion"] == "checked" and snapshot["handoff"] == result
        assert snapshot["counts"]["unresolved"] == 0
        assert snapshot["regions"][0]["disposition"] == "supporting_context"
        assert current_proof_receipt(root, record, load_receipt(record)) is None
        if damage == "reviewed_context":
            save_receipt(
                record, {"status": "review_rejected", "revision_id": record["revision_id"]}
            )
            unreviewed = show_document(root, "paper")
            assert unreviewed["regions"][0]["disposition"] == "unresolved"
            assert unreviewed["proof_completion"] == "pending" and unreviewed["handoff"] is None
    else:
        assert snapshot["proof_completion"] == "pending" and snapshot["handoff"] is None
        assert any("Complete source coverage" in message for message in snapshot["diagnostics"])
        with pytest.raises(ValueError, match="Complete source coverage"):
            current_proof_receipt(root, record, load_receipt(record))
        with pytest.raises(ValueError, match="Complete source coverage"):
            handoff.validate_handoff(root, directory / "handoff.json", record["revision_id"])
