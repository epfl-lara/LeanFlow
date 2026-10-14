"""Exercise conflict-preserving publication under real host rename and locking semantics."""

import json
import os
import threading
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import digest, put_blob, read_blob
from leanflow_cli.formalization.publication import (
    PublicationCancelled,
    PublicationConflict,
    PublicationUnsupported,
    assert_no_pending_publication,
    freeze_publication_plan,
    publication_guard,
    publish_revision,
    recover_publication,
    rename_exclusive,
)
from leanflow_cli.formalization.publication_journal import current_journal, load_journal

BEFORE = {"Old.lean": b"old", "New.lean": None}
OUTPUTS = {"Old.lean": b"new", "New.lean": b"added"}


def _publish(root: Path, checkpoint=None, **kwargs):
    return publish_revision(
        root=root,
        directory=kwargs.pop("directory", root / ".leanflow/transaction"),
        revision_id="rev-1",
        plan=freeze_publication_plan("rev-1", BEFORE, OUTPUTS, owned_paths=OUTPUTS),
        before=BEFORE,
        outputs=OUTPUTS,
        validate=lambda: None,
        checkpoint=checkpoint,
        **kwargs,
    )


def _journal(root: Path):
    path = current_journal(root / ".leanflow/transaction")
    assert path is not None
    return path, load_journal(path)


def _crash(step):
    if step == "before_commit":
        raise SystemExit("simulated process death")


def _prepared(root: Path):
    (root / "Old.lean").write_bytes(b"old")
    with pytest.raises(SystemExit):
        _publish(root, _crash)


def test_exclusive_rename_preserves_destination(tmp_path: Path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"proposal")
    target.write_bytes(b"user")
    with pytest.raises(FileExistsError):
        rename_exclusive(source, target)
    assert source.read_bytes() == b"proposal"
    assert target.read_bytes() == b"user"


def test_publication_retains_and_audits_actual_displaced_inode(tmp_path: Path):
    old = tmp_path / "Old.lean"
    old.write_bytes(b"old")
    with old.open("r+b") as editor:
        receipt = _publish(tmp_path)
        editor.write(b"user edit after commit")
        editor.flush()
    assert receipt["state"] == receipt["status"] == "COMMITTED"
    assert old.read_bytes() == b"new"
    path, journal = _journal(tmp_path)
    assert (
        path.parent / journal["writes"]["Old.lean"]["captured"]
    ).read_bytes() == b"user edit after commit"
    with pytest.raises(PublicationConflict, match="Late user edit"):
        assert_no_pending_publication(tmp_path)
    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert old.read_bytes() == b"new"


@pytest.mark.parametrize(
    "boundary",
    [
        "registered",
        "written:Old.lean",
        "prepared",
        "captured:Old.lean",
        "installed:Old.lean",
        "installed:New.lean",
        "before_commit",
    ],
)
def test_crash_recovery_restores_known_old_revision(tmp_path: Path, boundary: str):
    (tmp_path / "Old.lean").write_bytes(b"old")

    def crash(step):
        if step == boundary:
            raise SystemExit("simulated process death")

    with pytest.raises(SystemExit):
        _publish(tmp_path, crash)
    result = recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert result["state"] == "ROLLED_BACK"
    assert (tmp_path / "Old.lean").read_bytes() == b"old"
    assert not (tmp_path / "New.lean").exists()
    assert_no_pending_publication(tmp_path)


def test_edit_between_validation_and_capture_is_restored_and_stays_conflicted(tmp_path: Path):
    old = tmp_path / "Old.lean"
    old.write_bytes(b"old")

    def edit(step):
        if step == "prepared":
            old.write_bytes(b"late user edit")

    with pytest.raises(PublicationConflict):
        _publish(tmp_path, edit)
    assert old.read_bytes() == b"late user edit"
    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert old.read_bytes() == b"late user edit"
    assert not (tmp_path / "New.lean").exists()


def test_create_during_capture_preserves_both_actual_versions(tmp_path: Path):
    old = tmp_path / "Old.lean"
    old.write_bytes(b"old")

    def edit(step):
        if step == "captured:Old.lean":
            old.write_bytes(b"concurrent save")

    with pytest.raises(PublicationConflict):
        _publish(tmp_path, edit)
    assert old.read_bytes() == b"concurrent save"
    path, journal = _journal(tmp_path)
    assert (path.parent / journal["writes"]["Old.lean"]["captured"]).read_bytes() == b"old"


def test_open_descriptor_edit_during_publish_returns_user_bytes_to_destination(tmp_path: Path):
    old = tmp_path / "Old.lean"
    old.write_bytes(b"old")
    with old.open("r+b") as editor:

        def edit(step):
            if step == "installed:Old.lean":
                editor.write(b"changed via descriptor")
                editor.flush()

        with pytest.raises(PublicationConflict):
            _publish(tmp_path, edit)
    assert old.read_bytes() == b"changed via descriptor"


def test_recovery_retains_user_destination_and_rolls_back_uncontested_siblings(tmp_path: Path):
    _prepared(tmp_path)
    (tmp_path / "Old.lean").write_bytes(b"user")
    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert (tmp_path / "Old.lean").read_bytes() == b"user"
    assert not (tmp_path / "New.lean").exists()


def test_recovery_user_save_race_is_journaled_restored_and_never_falsely_clean(tmp_path: Path):
    _prepared(tmp_path)

    def edit(step):
        if step == "recovery_move_pending:Old.lean":
            (tmp_path / "Old.lean").write_bytes(b"late recovery save")

    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction", checkpoint=edit)
    assert (tmp_path / "Old.lean").read_bytes() == b"late recovery save"
    _, journal = _journal(tmp_path)
    assert journal["writes"]["Old.lean"]["rollback_slots"]
    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert (tmp_path / "Old.lean").read_bytes() == b"late recovery save"


def test_unknown_recovery_slot_and_new_destination_both_survive(tmp_path: Path):
    _prepared(tmp_path)

    def edit(step):
        if step == "recovery_move_pending:Old.lean":
            (tmp_path / "Old.lean").write_bytes(b"first save")
        if step == "recovery_moved:Old.lean":
            (tmp_path / "Old.lean").write_bytes(b"second save")

    with pytest.raises(PublicationConflict):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction", checkpoint=edit)
    path, journal = _journal(tmp_path)
    assert (tmp_path / "Old.lean").read_bytes() == b"second save"
    slot = journal["writes"]["Old.lean"]["rollback_slots"][0]["path"]
    assert (path.parent / slot).read_bytes() == b"first save"


@pytest.mark.parametrize("relative,data", [("Old.lean", b"new"), ("New.lean", b"added")])
def test_equal_planned_bytes_do_not_grant_ownership_of_user_save(tmp_path: Path, relative, data):
    (tmp_path / "Old.lean").write_bytes(b"old")

    def edit(step):
        if step == "prepared":
            if relative == "Old.lean":
                (tmp_path / relative).unlink()
            (tmp_path / relative).write_bytes(data)
            raise RuntimeError("stop before any output was installed")

    with pytest.raises(PublicationConflict):
        _publish(tmp_path, edit)
    assert (tmp_path / relative).read_bytes() == data


def test_deleted_original_is_not_resurrected_from_blob(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")

    def remove(step):
        if step == "prepared":
            (tmp_path / "Old.lean").unlink()

    with pytest.raises(PublicationConflict):
        _publish(tmp_path, remove)
    assert not (tmp_path / "Old.lean").exists()


def test_new_work_blocks_on_another_revision_and_guard_is_reentrant(tmp_path: Path):
    _prepared(tmp_path)
    with pytest.raises(PublicationConflict):
        _publish(tmp_path, directory=tmp_path / ".leanflow/second")
    with pytest.raises(PublicationConflict):
        assert_no_pending_publication(tmp_path)
    recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    with publication_guard(tmp_path):
        assert_no_pending_publication(tmp_path)
        with publication_guard(tmp_path):
            pass


def test_public_recovery_serializes_with_live_publisher(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")
    entered, release, recovery_started, recovery_finished = (threading.Event() for _ in range(4))
    results = []

    def pause(step):
        if step == "prepared":
            entered.set()
            assert release.wait(5)

    def publish():
        results.append(_publish(tmp_path, pause)["state"])

    def recover():
        recovery_started.set()
        results.append(recover_publication(tmp_path, tmp_path / ".leanflow/transaction")["state"])
        recovery_finished.set()

    writer = threading.Thread(target=publish)
    writer.start()
    assert entered.wait(5)
    reader = threading.Thread(target=recover)
    reader.start()
    assert recovery_started.wait(5)
    assert not recovery_finished.wait(0.1)
    release.set()
    writer.join(5)
    reader.join(5)
    assert results == ["COMMITTED", "COMMITTED"]
    assert (tmp_path / "Old.lean").read_bytes() == b"new"


@pytest.mark.parametrize(
    "boundary",
    [
        "registered",
        "prepared",
        "captured:Old.lean",
        "installed:Old.lean",
        "before_commit",
        "committed",
    ],
)
def test_cancellation_respects_durable_commit_boundary(tmp_path: Path, boundary):
    (tmp_path / "Old.lean").write_bytes(b"old")
    cancel = False

    def mark(step):
        nonlocal cancel
        if step == boundary:
            cancel = True

    if boundary == "committed":
        assert _publish(tmp_path, mark, cancelled=lambda: cancel)["state"] == "COMMITTED"
        assert (tmp_path / "Old.lean").read_bytes() == b"new"
    else:
        with pytest.raises(PublicationCancelled):
            _publish(tmp_path, mark, cancelled=lambda: cancel)
        assert (tmp_path / "Old.lean").read_bytes() == b"old"
        assert not (tmp_path / "New.lean").exists()
    assert_no_pending_publication(tmp_path)


def test_commit_receipt_is_bound_to_exact_frozen_plan(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")
    receipt = _publish(tmp_path)
    assert _publish(tmp_path)["transaction_id"] == receipt["transaction_id"]
    changed = {**OUTPUTS, "New.lean": b"different"}
    with pytest.raises(ValueError, match="different frozen plan"):
        publish_revision(
            root=tmp_path,
            directory=tmp_path / ".leanflow/transaction",
            revision_id="rev-1",
            plan=freeze_publication_plan("rev-1", BEFORE, changed, owned_paths=changed),
            before=BEFORE,
            outputs=changed,
            validate=lambda: None,
        )


@pytest.mark.parametrize(
    "path",
    [
        "./A.lean",
        "A//B.lean",
        "../A.lean",
        "/A.lean",
        "e\u0301.lean",
        "lakefile.lean",
        ".leanflow/owned.lean",
        "lake-manifest.json",
        "notes.txt",
    ],
)
def test_unsafe_or_unowned_output_paths_are_rejected(path):
    with pytest.raises(ValueError):
        freeze_publication_plan("revision", {path: None}, {path: b"x"}, owned_paths=[path])


def test_frozen_plan_rejects_aliases_and_protected_sources():
    with pytest.raises(ValueError, match="collide"):
        freeze_publication_plan(
            "rev",
            {"A.lean": None, "a.lean": None},
            {"A.lean": b"x", "a.lean": b"y"},
            owned_paths=["A.lean", "a.lean"],
        )
    with pytest.raises(ValueError, match="protected"):
        freeze_publication_plan(
            "rev",
            {"Paper.lean": None},
            {"Paper.lean": b"x"},
            owned_paths=["Paper.lean"],
            protected_paths=["Paper.lean"],
        )
    with pytest.raises(ValueError, match="ownership"):
        freeze_publication_plan("rev", {"Paper.lean": None}, {"Paper.lean": b"x"}, owned_paths=[])


def test_shared_inode_destinations_rejected(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")
    os.link(tmp_path / "Old.lean", tmp_path / "Other.lean")
    before = {"Old.lean": b"old", "Other.lean": b"old"}
    outputs = {key: b"new" for key in before}
    with pytest.raises(PublicationConflict, match="inode"):
        publish_revision(
            root=tmp_path,
            directory=tmp_path / ".leanflow/transaction",
            revision_id="rev",
            plan=freeze_publication_plan("rev", before, outputs, owned_paths=outputs),
            before=before,
            outputs=outputs,
            validate=lambda: None,
        )


def test_corrupt_blob_stops_recovery_before_any_destination_changes(tmp_path: Path):
    _prepared(tmp_path)
    path, journal = _journal(tmp_path)
    (path.parent / "blobs" / journal["writes"]["Old.lean"]["before"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="modified"):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert (tmp_path / "Old.lean").read_bytes() == b"new"
    assert (tmp_path / "New.lean").read_bytes() == b"added"


def test_atomic_blob_failure_cannot_poison_digest_path(tmp_path: Path, monkeypatch):
    import leanflow_cli.formalization.evidence as evidence

    def stop(*args, **kwargs):
        raise OSError("interrupted before publishing completed blob")

    with monkeypatch.context() as scoped:
        scoped.setattr(evidence.os, "link", stop)
        with pytest.raises(OSError):
            put_blob(tmp_path, b"complete evidence")
    assert not (tmp_path / digest(b"complete evidence")).exists()
    key = put_blob(tmp_path, b"complete evidence")
    assert read_blob(tmp_path, key) == b"complete evidence"
    (tmp_path / key).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        put_blob(tmp_path, b"complete evidence")


def test_unknown_filesystem_stages_without_project_mutation(tmp_path: Path, monkeypatch):
    import leanflow_cli.formalization.publication as publication

    (tmp_path / "Old.lean").write_bytes(b"old")

    def unsupported(*args):
        raise PublicationUnsupported("unknown filesystem")

    monkeypatch.setattr(publication, "check_filesystem", unsupported)
    with pytest.raises(PublicationUnsupported):
        _publish(tmp_path)
    assert (tmp_path / "Old.lean").read_bytes() == b"old"
    assert not (tmp_path / "New.lean").exists()
    assert_no_pending_publication(tmp_path)


def test_journal_order_tampering_cannot_hide_rollback_write(tmp_path: Path):
    _prepared(tmp_path)
    path, journal = _journal(tmp_path)
    journal["order"] = ["Old.lean"]
    path.write_text(json.dumps(journal))
    with pytest.raises(PublicationConflict, match="order"):
        recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    assert (tmp_path / "New.lean").read_bytes() == b"added"


def test_recovering_old_commit_does_not_reselect_it(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")
    old = _publish(tmp_path)
    before = {"Old.lean": b"new"}
    outputs = {"Old.lean": b"newer"}
    new = publish_revision(
        root=tmp_path,
        directory=tmp_path / ".leanflow/second",
        revision_id="rev-2",
        plan=freeze_publication_plan("rev-2", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )
    assert old["transaction_id"] != new["transaction_id"]
    recover_publication(tmp_path, tmp_path / ".leanflow/transaction")
    registry = json.loads((tmp_path / ".leanflow/formalization-publications.json").read_text())
    assert registry["selected_revision"]["transaction_id"] == new["transaction_id"]
    assert (tmp_path / "Old.lean").read_bytes() == b"newer"


def test_clean_ordinary_failure_rolls_back_before_returning(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")

    def failure(step):
        if step == "installed:Old.lean":
            raise RuntimeError("controller lost its caller")

    with pytest.raises(RuntimeError, match="lost its caller"):
        _publish(tmp_path, failure)
    assert (tmp_path / "Old.lean").read_bytes() == b"old"
    assert not (tmp_path / "New.lean").exists()
    assert_no_pending_publication(tmp_path)


def test_ordinary_failure_after_commit_reports_committed(tmp_path: Path):
    (tmp_path / "Old.lean").write_bytes(b"old")

    def failure(step):
        if step == "journal:COMMITTED":
            raise RuntimeError("caller disappeared after durable commit")

    assert _publish(tmp_path, failure)["state"] == "COMMITTED"
    assert (tmp_path / "Old.lean").read_bytes() == b"new"
    assert_no_pending_publication(tmp_path)
