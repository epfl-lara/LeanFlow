"""Keep publication evidence independent of disposable author runs and acknowledge actual loss."""

import json
import shutil
from pathlib import Path

import pytest

from leanflow_cli.formalization.conflict_resolution import (
    publication_conflicts,
    resolve_publication_conflict,
)
from leanflow_cli.formalization.freshness import assert_revision_not_revoked
from leanflow_cli.formalization.publication import (
    PublicationConflict,
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
    recover_pending_publication,
    recover_publication,
)
from leanflow_cli.formalization.publication_journal import load_registry, save_registry
from leanflow_cli.formalization.receipt_recovery import reconcile_committed_receipt
from leanflow_cli.formalization.records import create_document, load_receipt
from tests.leanflow.test_formalization_conflict_resolution import _conflicted, _request
from tests.leanflow.test_formalization_publication import _journal
from tests.leanflow.test_formalization_receipts import _commit_receipt


def _publish_run(root: Path, checkpoint=None):
    """Publish actual canonical bytes from a run-owned pointer directory."""
    (root / "Main.lean").write_bytes(b"original")
    before = {"Main.lean": b"original"}
    outputs = {"Main.lean": b"published"}
    return publish_revision(
        root=root,
        directory=root / ".leanflow/native-workflows/author-run/publication",
        revision_id="revision-1",
        plan=freeze_publication_plan("revision-1", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
        checkpoint=checkpoint,
    )


@pytest.mark.parametrize("interrupted", [False, True])
def test_author_run_cleanup_preserves_committed_and_pending_publication(tmp_path, interrupted):
    def checkpoint(boundary):
        if interrupted and boundary == "before_commit":
            raise SystemExit("process interrupted")

    if interrupted:
        with pytest.raises(SystemExit):
            _publish_run(tmp_path, checkpoint)
    else:
        _publish_run(tmp_path, checkpoint)
    registry = load_registry(tmp_path)
    entry = next(iter(registry["transactions"].values()))
    assert entry["journal"].startswith(".leanflow/formalization-publication-transactions/")
    shutil.rmtree(tmp_path / ".leanflow/native-workflows/author-run")
    recover_pending_publication(tmp_path)
    assert_no_pending_publication(tmp_path)
    assert (tmp_path / "Main.lean").read_bytes() == (b"original" if interrupted else b"published")


def test_run_cleanup_preserves_open_descriptor_late_writes(tmp_path):
    (tmp_path / "Main.lean").write_bytes(b"original")
    with (tmp_path / "Main.lean").open("r+b") as descriptor:
        _publish_run(tmp_path)
        shutil.rmtree(tmp_path / ".leanflow/native-workflows/author-run")
        descriptor.write(b"late retained user edit")
        descriptor.truncate()
        descriptor.flush()
        with pytest.raises(PublicationConflict, match="Late user edit"):
            assert_no_pending_publication(tmp_path)
        shown = publication_conflicts(tmp_path)["transactions"][0]
        assert any(row["available"] for row in shown["versions"])


def test_missing_legacy_run_journal_has_observed_loss_resolution_without_synthetic_plan(tmp_path):
    receipt = _publish_run(tmp_path)
    registry = load_registry(tmp_path)
    key = receipt["transaction_id"]
    original = tmp_path / registry["transactions"][key]["journal"]
    legacy = tmp_path / ".leanflow/native-workflows/author-run/publication/transactions" / key
    legacy.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(original.parent), legacy)
    registry["transactions"][key] = {
        "journal": str((legacy / "publication.json").relative_to(tmp_path))
    }
    save_registry(tmp_path, registry)
    shutil.rmtree(tmp_path / ".leanflow/native-workflows/author-run")
    with pytest.raises(PublicationConflict, match="unavailable"):
        assert_no_pending_publication(tmp_path)
    shown = publication_conflicts(tmp_path)["transactions"][0]
    assert shown["unavailable_journal"] is True
    assert shown["versions"] == [] and shown["planned_versions"] is None
    request = {
        **{
            name: shown[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        },
        "choice": "keep_current",
        "reason": "Inspected the current source and acknowledge the deleted historical journal and versions",
    }
    result = resolve_publication_conflict(tmp_path, request)
    assert result["unavailable_journal"] is True
    assert not legacy.exists()
    assert (tmp_path / "Main.lean").read_bytes() == b"published"
    assert_no_pending_publication(tmp_path)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision-1")
    retained = json.loads((tmp_path / ".leanflow/formalization-publications.json").read_text())
    assert retained["transactions"][key]["journal"].endswith("publication.json")
    assert retained["transactions"][key]["loss"]["resolutions"][-1] == result


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_resolution_archive_is_inspectable_and_exactly_acknowledged(tmp_path, corrupt):
    _conflicted(tmp_path)
    first = resolve_publication_conflict(tmp_path, _request(tmp_path))
    path, _ = _journal(tmp_path)
    checksum = next(iter(first["version_blobs"].values()))
    archive = path.parent / "blobs" / checksum
    if corrupt:
        archive.write_bytes(b"unexpected user archive bytes")
    else:
        archive.unlink()
    with pytest.raises(PublicationConflict):
        assert_no_pending_publication(tmp_path)
    shown = publication_conflicts(tmp_path)["transactions"][0]
    assert any("archive unavailable" in issue for issue in shown["conflicts"])
    assert shown["unavailable_versions"][f"blobs/{checksum}"] == checksum
    stale = _request(tmp_path)
    archive.write_bytes(b"new unobserved archive version")
    with pytest.raises(PublicationConflict, match="observation changed"):
        resolve_publication_conflict(tmp_path, stale)
    archive.unlink()
    result = resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert result["unavailable_versions"][f"blobs/{checksum}"] == checksum
    assert not archive.exists()
    assert_no_pending_publication(tmp_path)
    assert _journal(tmp_path)[1]["resolutions"][0] == first


def test_acknowledged_missing_journal_allows_fresh_publication_through_retained_pointer(tmp_path):
    previous = _publish_run(tmp_path)
    registry = load_registry(tmp_path)
    entry = registry["transactions"][previous["transaction_id"]]
    journal_path = tmp_path / entry["journal"]
    journal_path.unlink()
    directory = tmp_path / ".leanflow/native-workflows/author-run/publication"
    with pytest.raises(PublicationConflict, match="unavailable"):
        recover_publication(tmp_path, directory)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert recover_publication(tmp_path, directory) is None
    registry = load_registry(tmp_path)
    registry.pop("revoked_revisions")
    save_registry(tmp_path, registry)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision-1")
    before = {"Main.lean": b"published"}
    outputs = {"Main.lean": b"fresh acceptance"}
    next_publication = publish_revision(
        root=tmp_path,
        directory=directory,
        revision_id="revision-2",
        plan=freeze_publication_plan("revision-2", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )
    assert next_publication["transaction_id"] != previous["transaction_id"]
    assert_revision_not_revoked(tmp_path, "revision-2")
    assert_no_pending_publication(tmp_path)


@pytest.mark.parametrize("state", ["missing", "acknowledged", "revoked"])
def test_receipt_reconciliation_does_not_resurrect_missing_or_revoked_publication(tmp_path, state):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    document = create_document(tmp_path, source)
    staged, _ = _commit_receipt(tmp_path, document)
    registry = load_registry(tmp_path)
    entry = next(iter(registry["transactions"].values()))
    if state == "revoked":
        registry["revoked_revisions"] = [{"revision_id": document["revision_id"]}]
        save_registry(tmp_path, registry)
    else:
        (tmp_path / entry["journal"]).unlink()
        if state == "acknowledged":
            resolve_publication_conflict(tmp_path, _request(tmp_path))
    reconcile_committed_receipt(tmp_path, document)
    assert load_receipt(document) == staged


def test_missing_journal_acknowledgment_rejects_changed_current_authority(tmp_path):
    previous = _publish_run(tmp_path)
    entry = load_registry(tmp_path)["transactions"][previous["transaction_id"]]
    (tmp_path / entry["journal"]).unlink()
    stale = _request(tmp_path)
    (tmp_path / "Main.lean").write_bytes(b"unobserved current user edit")
    with pytest.raises(PublicationConflict, match="observation changed"):
        resolve_publication_conflict(tmp_path, stale)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert (tmp_path / "Main.lean").read_bytes() == b"unobserved current user edit"
    assert_no_pending_publication(tmp_path)


def test_reappearing_acknowledged_journal_requires_new_observation_before_recovery(tmp_path):
    previous = _publish_run(tmp_path)
    entry = load_registry(tmp_path)["transactions"][previous["transaction_id"]]
    path = tmp_path / entry["journal"]
    original = path.read_bytes()
    path.unlink()
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    path.write_bytes(original)
    directory = tmp_path / ".leanflow/native-workflows/author-run/publication"
    with pytest.raises(PublicationConflict, match="unavailable or changed"):
        recover_publication(tmp_path, directory)
    assert (tmp_path / "Main.lean").read_bytes() == b"published"
    assert path.read_bytes() == original
    resolution = resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert resolution["observation"]["retained_slots"][entry["journal"]] is not None
    assert_no_pending_publication(tmp_path)
