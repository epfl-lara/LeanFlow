"""Exercise pre-journal process death and nonblocking publication loss observations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import publication_registry_inventory
from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.formalization.publication import (
    PublicationConflict,
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
    recover_pending_publication,
)
from leanflow_cli.formalization.publication_journal import (
    load_registry,
    registry_path,
    save_registry,
)
from leanflow_cli.formalization.publication_registry_inventory import (
    EFFECT_HISTORY,
    INDEX_HISTORY,
    TRANSACTION_DIRECTORY,
    PublicationAuthorityLoss,
)
from tests.leanflow.test_formalization_publication_lifecycle import _publish_run


def _process(root: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize("boundary", ["before_directory", "after_staging", "before_journal"])
def test_real_prejournal_crash_recovers_without_revoking_selected_acceptance(tmp_path, boundary):
    committed = _publish_run(tmp_path)
    result = _process(
        tmp_path,
        """
import os, sys
from pathlib import Path
from leanflow_cli.formalization import publication
root = Path(sys.argv[1])
mkdir = publication.mkdir_durable
save = publication.save_journal
def interrupted_directory(path):
    if path.name == 'staging' and BOUNDARY == 'before_directory':
        os._exit(71)
    mkdir(path)
    if path.name == 'staging' and BOUNDARY == 'after_staging':
        os._exit(71)
def interrupted_journal(path, journal, *args):
    if BOUNDARY == 'before_journal':
        os._exit(71)
    save(path, journal, *args)
publication.mkdir_durable = interrupted_directory
publication.save_journal = interrupted_journal
before = {'Main.lean': b'published'}
outputs = {'Main.lean': b'next'}
publication.publish_revision(
    root=root, directory=root/'.leanflow/next-publication', revision_id='next',
    plan=publication.freeze_publication_plan('next', before, outputs, owned_paths=outputs),
    before=before, outputs=outputs, validate=lambda: None)
""".replace("BOUNDARY", repr(boundary)),
    )
    assert result.returncode == 71, result.stderr
    recover_pending_publication(tmp_path)
    registry = load_registry(tmp_path)
    assert len(registry["transactions"]) == 2
    assert registry["active"] is None
    assert registry["selected_revision"]["transaction_id"] == committed["transaction_id"]
    assert not registry.get("revoked_revisions")
    assert (tmp_path / "Main.lean").read_bytes() == b"published"
    assert_no_pending_publication(tmp_path)


@pytest.mark.parametrize("name", [".DS_Store", "unrelated.note", "not.a.directory"])
def test_foreign_nonidentifier_history_entries_do_not_revoke_acceptance(tmp_path, name):
    committed = _publish_run(tmp_path)
    for location in (TRANSACTION_DIRECTORY, INDEX_HISTORY, EFFECT_HISTORY):
        parent = tmp_path / location
        parent.mkdir(exist_ok=True)
        if name == "not.a.directory":
            (parent / name).mkdir()
        else:
            (parent / name).write_bytes(b"foreign metadata")
    recover_pending_publication(tmp_path)
    assert (
        load_registry(tmp_path)["selected_revision"]["transaction_id"]
        == committed["transaction_id"]
    )
    assert_no_pending_publication(tmp_path)
    discovery = publication_registry_inventory.discover_authority(tmp_path)
    assert not any(name in path for path in discovery["evidence"])


@pytest.mark.parametrize(
    "location,name",
    [
        (TRANSACTION_DIRECTORY, "retained"),
        (INDEX_HISTORY, "retained.json"),
        (EFFECT_HISTORY, "retained.json"),
    ],
)
def test_unindexed_owned_identity_still_blocks_restored_index(tmp_path, location, name):
    _publish_run(tmp_path)
    parent = tmp_path / location
    parent.mkdir(exist_ok=True)
    (parent / name).write_bytes(b"retained control identity")
    with pytest.raises(PublicationAuthorityLoss):
        load_registry(tmp_path)


def test_foreign_metadata_during_commit_does_not_interrupt_registry_settlement(tmp_path):
    def checkpoint(boundary):
        if boundary == "before_commit":
            for location in (TRANSACTION_DIRECTORY, INDEX_HISTORY, EFFECT_HISTORY):
                parent = tmp_path / location
                parent.mkdir(exist_ok=True)
                (parent / ".DS_Store").write_bytes(b"foreign metadata")

    committed = _publish_run(tmp_path, checkpoint)
    assert committed["state"] == "COMMITTED"
    assert (
        load_registry(tmp_path)["selected_revision"]["transaction_id"]
        == committed["transaction_id"]
    )
    assert_no_pending_publication(tmp_path)


def test_retirement_marker_alone_prevents_a_clean_registry_after_history_loss(tmp_path):
    marker = tmp_path / EFFECT_HISTORY / "retained.json"
    durable_json(marker, {"revision_id": "historical"})
    with pytest.raises(PublicationAuthorityLoss):
        load_registry(tmp_path)
    discovery = publication_registry_inventory.discover_authority(tmp_path)
    assert discovery["revision_ids"] == ["historical"]
    assert discovery["transactions"] == {
        "retained": f"{TRANSACTION_DIRECTORY}/retained/publication.json"
    }


def _unknown_journal(root: Path) -> None:
    _publish_run(root)
    registry = load_registry(root)
    registry["transactions"]["unknown"] = {
        "journal": ".leanflow/native-workflows/lost/publication/transactions/unknown/publication.json"
    }
    save_registry(root, registry)


def test_unknown_scope_current_fifo_is_observed_without_a_blocking_read(tmp_path):
    _unknown_journal(tmp_path)
    current = tmp_path / "formalization/paper/current.json"
    current.parent.mkdir(parents=True)
    os.mkfifo(current)
    result = _process(
        tmp_path,
        """
import json, sys
from pathlib import Path
from leanflow_cli.formalization.conflict_resolution import publication_conflicts
value = publication_conflicts(Path(sys.argv[1]))
row = next(row for row in value['transactions'] if row['transaction_id'] == 'unknown')
print(json.dumps(row['destinations']))
""",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["formalization/paper/current.json"] == "nonregular"
    assert current.is_fifo()


@pytest.mark.parametrize("folder", ["Formalization", "formalization"])
def test_unknown_scope_uses_exact_owned_document_controls(tmp_path, folder):
    _unknown_journal(tmp_path)
    durable_json(tmp_path / folder / "paper/current.json", {"revision_id": "external"})
    if folder == "formalization":
        (tmp_path / folder / "not.a.document").mkdir()
        durable_json(tmp_path / folder / "not.a.document/current.json", {"revision_id": "foreign"})
    from leanflow_cli.formalization.conflict_resolution import publication_conflicts

    publication_conflicts(tmp_path)
    loss = load_registry(tmp_path)["transactions"]["unknown"]["loss"]
    assert ("external" in loss["revision_ids"]) == (folder == "formalization")
    assert "foreign" not in loss["revision_ids"]


def test_unknown_scope_oversized_current_is_bounded(tmp_path, monkeypatch):
    _unknown_journal(tmp_path)
    durable_json(tmp_path / "formalization/paper/current.json", {"revision_id": "external"})
    monkeypatch.setattr(publication_registry_inventory, "MAX_CONTROL_FILE_BYTES", 1)
    from leanflow_cli.formalization.publication_loss import observe_journal_loss

    registry = {
        "selected_revision": None,
        "transactions": {"unknown": {"journal": ".leanflow/missing/publication.json"}},
        "active": None,
    }
    with pytest.raises(publication_registry_inventory.PublicationDiscoveryLimit):
        observe_journal_loss(tmp_path, registry, "unknown")


@pytest.mark.parametrize("destination_restored", [False, True])
@pytest.mark.parametrize("transaction_removed", [False, True])
def test_restored_noeffect_index_cannot_reconstruct_a_later_lost_committed_journal(
    tmp_path, destination_restored, transaction_removed
):
    previous = _publish_run(tmp_path)
    stale = []

    def checkpoint(boundary):
        if boundary == "registration_saved":
            stale.append(registry_path(tmp_path).read_bytes())

    before = {"Main.lean": b"published"}
    outputs = {"Main.lean": b"next"}
    committed = publish_revision(
        root=tmp_path,
        directory=tmp_path / ".leanflow/next-publication",
        revision_id="next",
        plan=freeze_publication_plan("next", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
        checkpoint=checkpoint,
    )
    record = load_registry(tmp_path)["transactions"][committed["transaction_id"]]
    journal_path = tmp_path / record["journal"]
    marker = tmp_path / EFFECT_HISTORY / f"{committed['transaction_id']}.json"
    assert marker.is_file()
    journal_path.unlink()
    if destination_restored:
        # Even the original destination inode cannot waive the independently retired authority.
        (tmp_path / "Main.lean").unlink()
        (journal_path.parent / committed["writes"]["Main.lean"]["captured"]).rename(
            tmp_path / "Main.lean"
        )
    if transaction_removed:
        shutil.rmtree(journal_path.parent)
    registry_path(tmp_path).write_bytes(stale[0])
    with pytest.raises(PublicationConflict, match="unavailable"):
        recover_pending_publication(tmp_path)
    assert not journal_path.exists()
    assert marker.is_file()
    restored = load_registry(tmp_path)
    assert restored["selected_revision"]["transaction_id"] == previous["transaction_id"]
    assert restored["transactions"][committed["transaction_id"]]["loss"]["state"] == "CONFLICTED"
    pending = restored["transactions"][committed["transaction_id"]]
    assert pending["no_effect_journal"]["state"] == "PREPARING"


@pytest.mark.parametrize("change", ["same_bytes_new_inode", "different_bytes", "displaced_inode"])
def test_noeffect_recovery_does_not_override_changed_destination_or_retained_entries(
    tmp_path, change
):
    _publish_run(tmp_path)
    before = {"Main.lean": b"published"}
    outputs = {"Main.lean": b"next"}

    def checkpoint(boundary):
        if boundary == "registration_saved":
            raise SystemExit("pre-journal interruption")

    with pytest.raises(SystemExit):
        publish_revision(
            root=tmp_path,
            directory=tmp_path / ".leanflow/next-publication",
            revision_id="next",
            plan=freeze_publication_plan("next", before, outputs, owned_paths=outputs),
            before=before,
            outputs=outputs,
            validate=lambda: None,
            checkpoint=checkpoint,
        )
    registry = load_registry(tmp_path)
    record = registry["transactions"][registry["active"]]
    if change == "same_bytes_new_inode":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"published")
        replacement.replace(tmp_path / "Main.lean")
    elif change == "different_bytes":
        (tmp_path / "Main.lean").write_bytes(b"user")
    else:
        displaced = (tmp_path / record["journal"]).parent / "displaced"
        displaced.mkdir(parents=True)
        (displaced / "retained").write_bytes(b"user")
    actual = (tmp_path / "Main.lean").read_bytes()
    with pytest.raises(PublicationConflict, match="unavailable"):
        recover_pending_publication(tmp_path)
    assert not (tmp_path / record["journal"]).exists()
    assert (tmp_path / "Main.lean").read_bytes() == actual
