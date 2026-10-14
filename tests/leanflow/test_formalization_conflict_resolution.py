"""Exercise explicit publication reconciliation, restart, retained history, and late edits."""

import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization.conflict_resolution import (
    publication_conflicts,
    resolve_conflict_artifact,
    resolve_publication_conflict,
)
from leanflow_cli.formalization.evidence import digest, read_blob
from leanflow_cli.formalization.freshness import assert_revision_not_revoked
from leanflow_cli.formalization.publication import (
    PublicationConflict,
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
    recover_pending_publication,
    recover_publication,
)
from leanflow_cli.formalization.publication_journal import load_registry
from tests.leanflow.test_formalization_publication import _journal, _prepared, _publish


def _request(root: Path) -> dict:
    observed = publication_conflicts(root)["transactions"][-1]
    return {
        **{
            name: observed[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        },
        "choice": "keep_current",
        "reason": "Reviewed all retained versions and reconciled canonical source in the editor",
    }


def _conflicted(root: Path) -> None:
    _prepared(root)
    (root / "Old.lean").write_bytes(b"user chosen source")
    with pytest.raises(PublicationConflict):
        recover_publication(root, root / ".leanflow/transaction")


def test_concurrent_save_resolves_without_erasing_history_and_restart_admits_work(tmp_path):
    _conflicted(tmp_path)
    path, before = _journal(tmp_path)
    preserved = (path.parent / before["writes"]["Old.lean"]["captured"]).read_bytes()
    result = resolve_publication_conflict(tmp_path, _request(tmp_path))
    _, resolved = _journal(tmp_path)
    assert resolved["state"] == "RESOLVED"
    assert resolved["conflicts"] == before["conflicts"]
    assert resolved["resolutions"][-1] == result
    assert (tmp_path / "Old.lean").read_bytes() == b"user chosen source"
    assert (path.parent / before["writes"]["Old.lean"]["captured"]).read_bytes() == preserved
    for name, checksum in result["version_blobs"].items():
        content = read_blob(path.parent / "blobs", checksum)
        if name == "destinations/Old.lean":
            assert content == b"user chosen source"
    assert recover_pending_publication(tmp_path) is None
    assert_no_pending_publication(tmp_path)
    registry = load_registry(tmp_path)
    assert registry["active"] is None and registry["selected_revision"] is None
    assert registry["revoked_revisions"][-1]["resolution_id"] == result["resolution_id"]
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "rev-1")


@pytest.mark.parametrize(
    "boundary", ["resolution_versions_archived", "journal:RESOLVED", "resolution_settled"]
)
def test_actual_process_exit_during_resolution_recovers_supported_admission(tmp_path, boundary):
    _conflicted(tmp_path)
    worker = """
import os, sys
from pathlib import Path
from leanflow_cli.formalization.conflict_resolution import publication_conflicts, resolve_publication_conflict
root = Path(sys.argv[1])
item = publication_conflicts(root)['transactions'][-1]
request = {key: item[key] for key in ('transaction_id', 'observation_id', 'destinations', 'retained_slots')}
request.update(choice='keep_current', reason='Reviewed and reconciled canonical source')
def checkpoint(name):
    if name == sys.argv[2]:
        os._exit(91)
resolve_publication_conflict(root, request, checkpoint=checkpoint)
"""
    process = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path), boundary],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 91, process.stderr
    if boundary == "resolution_versions_archived":
        resolve_publication_conflict(tmp_path, _request(tmp_path))
    else:
        recover_pending_publication(tmp_path)
    assert_no_pending_publication(tmp_path)
    assert (tmp_path / "Old.lean").read_bytes() == b"user chosen source"
    assert _journal(tmp_path)[1]["conflicts"]


def test_resolution_does_not_revoke_a_later_new_revision_publication(tmp_path):
    _conflicted(tmp_path)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    before = {"Old.lean": b"user chosen source"}
    outputs = {"Old.lean": b"new reviewed revision"}
    accepted = publish_revision(
        root=tmp_path,
        directory=tmp_path / ".leanflow/new-revision",
        revision_id="rev-2",
        plan=freeze_publication_plan("rev-2", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )
    recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert (
        load_registry(tmp_path)["selected_revision"]["transaction_id"] == accepted["transaction_id"]
    )
    assert_revision_not_revoked(tmp_path, "rev-2")
    assert_no_pending_publication(tmp_path)


def test_conflict_versions_page_completely_and_open_frozen_opaque_artifacts(tmp_path):
    _conflicted(tmp_path)
    first = publication_conflicts(tmp_path, version_limit=1)["transactions"][0]
    versions = []
    offset = 0
    while offset is not None:
        page = publication_conflicts(
            tmp_path, transaction_id=first["transaction_id"], version_offset=offset, version_limit=1
        )["transactions"][0]
        assert page["observation_id"] == first["observation_id"]
        versions.extend(page["versions"])
        offset = page["next_version_offset"]
    assert len(versions) == first["version_count"]
    assert {row["group"] for row in versions} == {
        "destinations",
        "retained_slots",
        "planned_before",
        "planned_after",
    }
    found = set()
    for version in versions:
        if not version["available"]:
            continue
        artifact = resolve_conflict_artifact(
            tmp_path, first["transaction_id"], first["observation_id"], version["version_id"]
        )
        path = Path(artifact["path"])
        assert path.stat().st_mode & 0o222 == 0
        assert digest(path.read_bytes()) == artifact["sha256"] == version["sha256"]
        found.add(path.read_bytes())
    assert {b"old", b"new", b"user chosen source"} <= found
    assert publication_conflicts(tmp_path, transaction_id="unknown")["transactions"] == []
    with pytest.raises(ValueError):
        resolve_conflict_artifact(
            tmp_path, first["transaction_id"], first["observation_id"], "../../secret"
        )
    (tmp_path / "Old.lean").write_bytes(b"new observation")
    with pytest.raises(PublicationConflict, match="observation changed"):
        resolve_conflict_artifact(
            tmp_path, first["transaction_id"], first["observation_id"], versions[0]["version_id"]
        )
    assert_revision_not_revoked(tmp_path, "rev-2")


@pytest.mark.parametrize("slot", [False, True])
def test_resolution_rejects_changed_destination_or_retained_hash(tmp_path, slot):
    _conflicted(tmp_path)
    request = _request(tmp_path)
    path, journal = _journal(tmp_path)
    target = (
        path.parent / journal["writes"]["Old.lean"]["captured"] if slot else tmp_path / "Old.lean"
    )
    target.write_bytes(b"unobserved edit")
    with pytest.raises(PublicationConflict, match="observation changed"):
        resolve_publication_conflict(tmp_path, request)
    assert target.read_bytes() == b"unobserved edit"
    assert _journal(tmp_path)[1]["state"] == "CONFLICTED"


def test_edit_while_archiving_does_not_commit_resolution(tmp_path):
    _conflicted(tmp_path)

    def edit(boundary):
        if boundary == "resolution_versions_archived":
            (tmp_path / "Old.lean").write_bytes(b"edit during resolution")

    with pytest.raises(PublicationConflict, match="before resolution commitment"):
        resolve_publication_conflict(tmp_path, _request(tmp_path), checkpoint=edit)
    assert _journal(tmp_path)[1]["state"] == "CONFLICTED"
    assert (tmp_path / "Old.lean").read_bytes() == b"edit during resolution"


@pytest.mark.parametrize("boundary", ["resolution_versions_archived", "journal:RESOLVED"])
def test_resolution_crash_has_explicit_durable_boundary(tmp_path, boundary):
    _conflicted(tmp_path)
    request = _request(tmp_path)

    def crash(step):
        if step == boundary:
            raise SystemExit("process exit")

    with pytest.raises(SystemExit):
        resolve_publication_conflict(tmp_path, request, checkpoint=crash)
    if boundary == "resolution_versions_archived":
        assert _journal(tmp_path)[1]["state"] == "CONFLICTED"
        resolve_publication_conflict(tmp_path, _request(tmp_path))
    else:
        assert _journal(tmp_path)[1]["state"] == "RESOLVED"
        assert load_registry(tmp_path)["active"] is not None
        with pytest.raises(ValueError, match="new mathematical acceptance"):
            assert_revision_not_revoked(tmp_path, "rev-1")
        recover_pending_publication(tmp_path)
    assert_no_pending_publication(tmp_path)
    assert load_registry(tmp_path)["active"] is None
    assert load_registry(tmp_path)["revoked_revisions"]


def test_late_open_descriptor_resolution_preserves_versions_and_reopens_on_another_write(tmp_path):
    old = tmp_path / "Old.lean"
    old.write_bytes(b"old")
    with old.open("r+b") as editor:
        _publish(tmp_path)
        editor.write(b"first retained edit")
        editor.flush()
        with pytest.raises(PublicationConflict, match="Late user edit"):
            assert_no_pending_publication(tmp_path)
        first = resolve_publication_conflict(tmp_path, _request(tmp_path))
        assert_no_pending_publication(tmp_path)
        editor.seek(0)
        editor.write(b"second retained edit")
        editor.truncate()
        editor.flush()
        with pytest.raises(PublicationConflict, match="Late user edit"):
            assert_no_pending_publication(tmp_path)
        assert _journal(tmp_path)[1]["state"] == "CONFLICTED"
        with pytest.raises(PublicationConflict, match="changed after resolution"):
            recover_pending_publication(tmp_path)
        second = resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert first["resolution_id"] != second["resolution_id"]
    path, journal = _journal(tmp_path)
    assert len(journal["resolutions"]) == 2
    assert digest(b"first retained edit") in first["version_blobs"].values()
    assert digest(b"second retained edit") in second["version_blobs"].values()
    assert (
        read_blob(path.parent / "blobs", digest(b"first retained edit")) == b"first retained edit"
    )
    assert old.read_bytes() == b"new"
    assert_no_pending_publication(tmp_path)


def test_originally_absent_destinations_remain_absent_after_explicit_resolution(tmp_path):
    _conflicted(tmp_path)
    assert not (tmp_path / "New.lean").exists()
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert not (tmp_path / "New.lean").exists()
    assert_no_pending_publication(tmp_path)


def test_simultaneous_historical_descriptor_conflicts_resolve_in_either_order(tmp_path):
    source = tmp_path / "Old.lean"
    source.write_bytes(b"old")
    with source.open("a+b") as first_editor:
        first = _publish(tmp_path)
        with source.open("a+b") as second_editor:
            second = publish_revision(
                root=tmp_path,
                directory=tmp_path / ".leanflow/second",
                revision_id="rev-2",
                plan=freeze_publication_plan(
                    "rev-2",
                    {"Old.lean": b"new"},
                    {"Old.lean": b"second"},
                    owned_paths={"Old.lean"},
                ),
                before={"Old.lean": b"new"},
                outputs={"Old.lean": b"second"},
                validate=lambda: None,
            )
            first_editor.write(b" first retained save")
            first_editor.flush()
            second_editor.write(b" second retained save")
            second_editor.flush()
            shown = publication_conflicts(tmp_path)["transactions"]
            assert {row["transaction_id"] for row in shown if row["queued"]} == {
                first["transaction_id"],
                second["transaction_id"],
            }
            assert len(load_registry(tmp_path)["conflict_queue"]) == 2
            with pytest.raises(PublicationConflict):
                assert_no_pending_publication(tmp_path)
            for transaction in (second, first):
                row = next(
                    item
                    for item in shown
                    if item["transaction_id"] == transaction["transaction_id"]
                )
                request = {
                    key: row[key]
                    for key in (
                        "transaction_id",
                        "observation_id",
                        "destinations",
                        "retained_slots",
                    )
                }
                request.update(
                    choice="keep_current", reason="Reviewed both retained editor versions"
                )
                resolve_publication_conflict(tmp_path, request)
                if transaction is second:
                    assert load_registry(tmp_path)["conflict_queue"] == [first["transaction_id"]]
                    with pytest.raises(PublicationConflict):
                        assert_no_pending_publication(tmp_path)
    assert source.read_bytes() == b"second"
    assert load_registry(tmp_path)["conflict_queue"] == []
    assert recover_pending_publication(tmp_path) is None
    assert_no_pending_publication(tmp_path)


def test_resolved_historical_write_keeps_new_inflight_publication_recoverable(tmp_path):
    source = tmp_path / "Old.lean"
    source.write_bytes(b"old")
    with source.open("a+b") as editor:
        first = _publish(tmp_path)
        editor.write(b" first save")
        editor.flush()
        publication_conflicts(tmp_path)
        resolve_publication_conflict(tmp_path, _request(tmp_path))

        def crash(boundary):
            if boundary == "before_commit":
                raise SystemExit("in-flight second publication")

        with pytest.raises(SystemExit):
            publish_revision(
                root=tmp_path,
                directory=tmp_path / ".leanflow/second",
                revision_id="rev-2",
                plan=freeze_publication_plan(
                    "rev-2",
                    {"Old.lean": b"new"},
                    {"Old.lean": b"second"},
                    owned_paths={"Old.lean"},
                ),
                before={"Old.lean": b"new"},
                outputs={"Old.lean": b"second"},
                validate=lambda: None,
                checkpoint=crash,
            )
        second_id = load_registry(tmp_path)["active"]
        editor.write(b" later save")
        editor.flush()
        shown = publication_conflicts(tmp_path)["transactions"]
        assert load_registry(tmp_path)["active"] == second_id
        assert load_registry(tmp_path)["conflict_queue"] == [first["transaction_id"]]
        assert any(row["state"] == "CONFLICTED" for row in shown)
        with pytest.raises(PublicationConflict):
            recover_pending_publication(tmp_path)
        assert source.read_bytes() == b"new"
        assert load_registry(tmp_path)["active"] == first["transaction_id"]
        resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert_no_pending_publication(tmp_path)


def test_new_destination_edits_after_resolution_need_acceptance_but_do_not_permanently_block(
    tmp_path,
):
    _conflicted(tmp_path)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    (tmp_path / "Old.lean").write_bytes(b"further mathematical edit")
    assert_no_pending_publication(tmp_path)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "rev-1")
