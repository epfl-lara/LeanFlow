"""Retain receipt outcomes immutably and authenticate history before trusting projections."""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from leanflow_cli.formalization.publication import freeze_publication_plan, publish_revision
from leanflow_cli.formalization.records import (
    create_document,
    document_directory,
    load_document,
    load_receipt,
    new_revision,
    receipt_events,
    rehydrate_document,
    save_receipt,
    save_revision,
)


@pytest.fixture
def document(tmp_path: Path) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    return create_document(tmp_path, source)


def _payload(record: dict, status: str) -> dict:
    return {
        "schema_version": 1,
        "revision_id": record["revision_id"],
        "status": status,
        "reviews": [],
    }


def test_receipt_updates_keep_accepted_and_rejected_evidence(document: dict) -> None:
    accepted = save_receipt(document, _payload(document, "review_passed"))
    accepted_path = (
        Path(document["_directory"])
        / "receipt-history"
        / document["revision_id"]
        / f"{accepted['receipt_id']}.json"
    )
    original = accepted_path.read_bytes()
    rejected = save_receipt(document, _payload(document, "review_rejected"))
    assert accepted["receipt_id"] != rejected["receipt_id"]
    assert accepted_path.read_bytes() == original
    assert load_receipt(document) == rejected
    history = receipt_events(document)
    assert [event["payload"]["status"] for event in history] == ["review_passed", "review_rejected"]
    assert history[-1]["previous_receipt_id"] == accepted["receipt_id"]
    assert save_receipt(document, _payload(document, "review_rejected")) == rejected
    assert len(receipt_events(document)) == 2


def test_publication_preparation_does_not_publish_before_commit(document: dict) -> None:
    staged = save_receipt(document, _payload(document, "accepted_staged"))
    published = save_receipt(document, _payload(document, "published"), select=False)
    assert load_receipt(document) == staged
    assert len(receipt_events(document, published["receipt_id"])) == 2
    assert save_receipt(document, published) == published
    assert load_receipt(document) == published
    assert len(receipt_events(document)) == 2


def test_prepared_publication_cannot_overwrite_new_rejection(document: dict) -> None:
    save_receipt(document, _payload(document, "accepted_staged"))
    published = save_receipt(document, _payload(document, "published"), select=False)
    rejected = save_receipt(document, _payload(document, "review_rejected"))
    with pytest.raises(ValueError, match="Selected receipt changed"):
        save_receipt(document, published)
    assert load_receipt(document) == rejected


@pytest.mark.parametrize(
    "mutation", ["projection", "ancestor", "missing_ancestor", "prepared_payload", "legacy"]
)
def test_receipt_corruption_never_restores_acceptance(document: dict, mutation: str) -> None:
    first = save_receipt(document, _payload(document, "review_rejected"))
    latest = save_receipt(document, _payload(document, "published"))
    directory = Path(document["_directory"])
    projection = directory / "receipts" / f"{document['revision_id']}.json"
    ancestor = (
        directory / "receipt-history" / document["revision_id"] / f"{first['receipt_id']}.json"
    )
    if mutation == "projection":
        value = json.loads(projection.read_text())
        value["status"] = "accepted_staged"
        projection.write_text(json.dumps(value))
    elif mutation == "ancestor":
        value = json.loads(ancestor.read_text())
        value["payload"]["status"] = "review_passed"
        ancestor.write_text(json.dumps(value))
    elif mutation == "missing_ancestor":
        ancestor.unlink()
    elif mutation == "prepared_payload":
        with pytest.raises(ValueError, match="Prepared receipt payload"):
            save_receipt(document, {**latest, "status": "review_passed"})
        return
    else:
        projection.write_text(json.dumps(_payload(document, "published")))
    with pytest.raises((ValueError, OSError)):
        load_receipt(document)


def test_concurrent_receipt_updates_form_one_chain(document: dict) -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(
            pool.map(lambda n: save_receipt(document, _payload(document, f"outcome-{n}")), range(3))
        )
    history = receipt_events(document)
    assert len(history) == 3
    assert {event["receipt_id"] for event in history} == {row["receipt_id"] for row in receipts}
    assert history[0]["previous_receipt_id"] is None


def test_portable_rehydration_preserves_entire_authenticated_history(
    document: dict, tmp_path: Path
) -> None:
    save_receipt(document, _payload(document, "accepted_staged"))
    published = save_receipt(document, _payload(document, "published"))
    private = Path(document["_directory"])
    portable = document_directory(tmp_path, document["document_id"], published=True)
    shutil.copytree(private, portable)
    shutil.rmtree(private)
    restored = load_document(tmp_path, document["document_id"])
    assert load_receipt(restored) == published
    rehydrate_document(restored, private)
    reloaded = {**restored, "_directory": str(private)}
    assert receipt_events(reloaded) == receipt_events(restored)
    assert load_receipt(reloaded) == published


def _commit_receipt(root: Path, document: dict, *, committed: bool = True) -> tuple[dict, dict]:
    """Die after the real durable commit while leaving the private projection staged."""
    staged = save_receipt(document, _payload(document, "accepted_staged"))
    published = save_receipt(document, _payload(document, "published"), select=False)
    prefix = f"formalization/{document['document_id']}"
    values = {
        f"{prefix}/receipts/{document['revision_id']}.json": published,
        f"{prefix}/current.json": {"schema_version": 1, "revision_id": document["revision_id"]},
    }
    for event in receipt_events(document, published["receipt_id"]):
        values[f"{prefix}/receipt-history/{document['revision_id']}/{event['receipt_id']}.json"] = (
            event
        )
    outputs = {name: (json.dumps(value) + "\n").encode() for name, value in values.items()}
    if not committed:
        for name, content in outputs.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return staged, published
    before = {name: None for name in outputs}

    def crash(step: str) -> None:
        if step == "committed":
            raise SystemExit("Process stopped before private receipt selection")

    with pytest.raises(SystemExit):
        publish_revision(
            root=root,
            directory=root / ".leanflow/receipt-publication",
            revision_id=document["revision_id"],
            plan=freeze_publication_plan(
                document["revision_id"], before, outputs, owned_paths=set(outputs)
            ),
            before=before,
            outputs=outputs,
            validate=lambda: None,
            checkpoint=crash,
        )
    return staged, published


def test_committed_publication_repairs_private_projection_after_process_death(
    document: dict, tmp_path: Path
) -> None:
    staged, published = _commit_receipt(tmp_path, document)
    assert load_receipt(document) == staged
    restored = load_document(tmp_path, document["document_id"])
    assert load_receipt(restored) == published
    assert [event["payload"]["status"] for event in receipt_events(restored)] == [
        "accepted_staged",
        "published",
    ]


@pytest.mark.parametrize("changed", ["rejected", "revision", "uncommitted"])
def test_publication_reconciliation_never_resurrects_stale_or_uncommitted_acceptance(
    document: dict, tmp_path: Path, changed: str
) -> None:
    staged, _ = _commit_receipt(tmp_path, document, committed=changed != "uncommitted")
    if changed == "rejected":
        expected = save_receipt(document, _payload(document, "review_rejected"))
    elif changed == "revision":
        candidate = new_revision(document)
        candidate["status"] = "changed"
        save_revision(Path(document["_directory"]), candidate)
        expected = None
    else:
        expected = staged
    restored = load_document(tmp_path, document["document_id"])
    assert load_receipt(restored) == expected


def test_rehydration_preserves_later_private_rejection(document: dict, tmp_path: Path) -> None:
    _, published = _commit_receipt(tmp_path, document)
    rejected = save_receipt(document, _payload(document, "review_rejected"))
    portable = document_directory(tmp_path, document["document_id"], published=True)
    remote = {**document, "_directory": str(portable)}
    assert load_receipt(remote) == published
    rehydrate_document(remote, Path(document["_directory"]))
    assert load_receipt(document) == rejected
