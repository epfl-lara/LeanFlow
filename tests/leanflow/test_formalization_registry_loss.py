"""Keep surviving revocation and publication history fail-closed when its root index is lost."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import handoff
from leanflow_cli.formalization.conflict_resolution import (
    publication_conflicts,
    resolve_publication_conflict,
)
from leanflow_cli.formalization.evidence import digest, durable_json
from leanflow_cli.formalization.freshness import assert_revision_not_revoked
from leanflow_cli.formalization.publication import (
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
    recover_pending_publication,
)
from leanflow_cli.formalization.publication_io import PublicationConflict
from leanflow_cli.formalization.publication_journal import (
    load_registry,
    registry_path,
    save_registry,
)
from leanflow_cli.formalization.publication_registry_loss import PublicationAuthorityLoss
from leanflow_cli.formalization.records import document_directory
from leanflow_cli.formalization.service import inspect_document, show_document
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.source import SourceDocument
from tests.leanflow import test_formalization_handoff as handoff_tests
from tests.leanflow.test_formalization_conflict_resolution import _conflicted, _request
from tests.leanflow.test_formalization_publication_lifecycle import _publish_run
from tests.leanflow.test_formalization_publication_startup import _interrupt

reviewed_project = handoff_tests.reviewed
admitted_snapshot_layout = handoff_tests.admitted_snapshot_layout


def _acknowledge(root: Path) -> dict:
    observed = publication_conflicts(root)["transactions"][0]
    assert observed["unavailable_registry"]
    return resolve_publication_conflict(
        root,
        {
            **{
                name: observed[name]
                for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
            },
            "choice": "keep_current",
            "reason": "Inspected surviving journal and canonical bytes; accept no historical mathematical authority",
        },
    )


def test_genuinely_fresh_project_and_private_intake_do_not_require_loss_recovery(tmp_path):
    assert load_registry(tmp_path)["transactions"] == {}
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    assert not inspect_document(tmp_path, tmp_path / "paper.tex")["can_prove"]
    assert load_registry(tmp_path)["transactions"] == {}
    assert not registry_path(tmp_path).exists()
    assert_no_pending_publication(tmp_path)
    assert publication_conflicts(tmp_path)["transactions"] == []


@pytest.mark.parametrize("folder", ["formalization", "Formalization"])
def test_ordinary_lean_libraries_do_not_imply_lost_publication_authority(tmp_path, folder):
    library = tmp_path / folder
    library.mkdir()
    (library / "Basic.lean").write_text("theorem example : True := True.intro")
    (library / "OrdinaryModule").mkdir()
    (library / "OrdinaryModule/Helper.lean").write_text("def helper := 1")
    if folder == "Formalization":
        durable_json(
            library / "paper/current.json", {"schema_version": 1, "revision_id": "ordinary"}
        )
    assert load_registry(tmp_path)["transactions"] == {}
    assert_no_pending_publication(tmp_path)
    assert not registry_path(tmp_path).exists()


def test_nonpublication_native_runs_do_not_consume_owned_discovery_budget(tmp_path, monkeypatch):
    from leanflow_cli.formalization import publication_registry_inventory

    for index in range(8):
        (tmp_path / f".leanflow/native-workflows/run-{index}").mkdir(parents=True)
    monkeypatch.setattr(publication_registry_inventory, "MAX_ENTRIES", 1)
    assert load_registry(tmp_path)["transactions"] == {}
    assert publication_conflicts(tmp_path)["transactions"] == []


@pytest.mark.parametrize("alias", [False, True])
def test_non_directory_portable_namespace_is_not_publication_evidence(tmp_path, alias):
    path = tmp_path / "formalization"
    if alias:
        (tmp_path / "OrdinaryLeanLibrary").mkdir()
        path.symlink_to(tmp_path / "OrdinaryLeanLibrary", target_is_directory=True)
    else:
        path.write_text("ordinary source artifact")
    assert load_registry(tmp_path)["transactions"] == {}
    assert_no_pending_publication(tmp_path)


def test_restored_older_valid_registry_cannot_hide_an_unregistered_revocation(tmp_path):
    save_registry(tmp_path, load_registry(tmp_path))
    older = registry_path(tmp_path).read_bytes()
    _conflicted(tmp_path)
    resolved = resolve_publication_conflict(tmp_path, _request(tmp_path))
    registry_path(tmp_path).write_bytes(older)
    with pytest.raises(PublicationAuthorityLoss, match="unindexed|incomplete"):
        assert_revision_not_revoked(tmp_path, "rev-1")
    worker = """
from pathlib import Path
import sys
from leanflow_cli.formalization.freshness import assert_revision_not_revoked
from leanflow_cli.formalization.publication_registry_loss import PublicationAuthorityLoss
try:
    assert_revision_not_revoked(Path(sys.argv[1]), 'rev-1')
except PublicationAuthorityLoss:
    pass
else:
    raise AssertionError('Restored older index revived revoked acceptance after restart')
"""
    process = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path)], capture_output=True, text=True, timeout=15
    )
    assert process.returncode == 0, process.stderr
    assert registry_path(tmp_path).read_bytes() == older
    acknowledged = _acknowledge(tmp_path)
    registry = load_registry(tmp_path)
    assert registry["selected_revision"] is None
    assert acknowledged["requires_new_acceptance"]
    journal = tmp_path / next(iter(registry["transactions"].values()))["journal"]
    assert json.loads(journal.read_text())["resolutions"][-1] == resolved
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "rev-1")


def test_revocation_survives_only_index_loss_and_source_restore_after_process_restart(tmp_path):
    _conflicted(tmp_path)
    resolved = resolve_publication_conflict(tmp_path, _request(tmp_path))
    (tmp_path / "Old.lean").write_bytes(b"new")
    registry_path(tmp_path).unlink()
    worker = """
import sys
from pathlib import Path
from leanflow_cli.formalization.freshness import assert_revision_not_revoked
from leanflow_cli.formalization.publication_registry_loss import PublicationAuthorityLoss
root = Path(sys.argv[1])
try:
    assert_revision_not_revoked(root, 'rev-1')
except PublicationAuthorityLoss:
    pass
else:
    raise AssertionError('Lost registry restored revoked acceptance')
assert not (root / '.leanflow/formalization-publications.json').exists()
"""
    process = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path)], capture_output=True, text=True, timeout=15
    )
    assert process.returncode == 0, process.stderr
    acknowledged = _acknowledge(tmp_path)
    assert acknowledged["requires_new_acceptance"]
    registry = load_registry(tmp_path)
    assert registry["selected_revision"] is None
    journal = tmp_path / next(iter(registry["transactions"].values()))["journal"]
    assert json.loads(journal.read_text())["resolutions"][-1] == resolved
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "rev-1")
    assert_no_pending_publication(tmp_path)


@pytest.mark.parametrize("restore_older", [False, True])
def test_can_prove_and_handoff_stay_blocked_after_revocation_then_index_loss(
    reviewed_project, monkeypatch, restore_older
):
    root, accepted = reviewed_project
    monkeypatch.setattr(
        "leanflow_cli.formalization.publication.assert_no_pending_publication",
        assert_no_pending_publication,
    )
    path = root / "src/Main.lean"
    original = path.read_bytes()
    older_index = registry_path(root).read_bytes()
    assert show_document(root, "paper")["can_prove"]
    with path.open("r+b") as editor:
        publish_revision(
            root=root,
            directory=root / ".leanflow/native-workflows/reviewed/publication",
            revision_id=accepted["revision_id"],
            before={"src/Main.lean": original},
            outputs={"src/Main.lean": original},
            plan=freeze_publication_plan(
                accepted["revision_id"],
                {"src/Main.lean": original},
                {"src/Main.lean": original},
                owned_paths={"src/Main.lean"},
            ),
            validate=lambda: None,
        )
        editor.write(b"late displaced user edit")
        editor.truncate()
        editor.flush()
        with pytest.raises(PublicationConflict):
            assert_no_pending_publication(root)
        resolve_publication_conflict(root, _request(root))
    path.write_bytes(original)
    if restore_older:
        registry_path(root).write_bytes(older_index)
    else:
        registry_path(root).unlink()
    unchanged = {str(item): digest(item.read_bytes()) for item in root.rglob("publication.json")}
    shown = show_document(root, "paper")
    assert not shown["can_prove"] and shown["handoff"] is None
    assert shown["acceptance_freshness"]["status"] == "acceptance_stale"
    with pytest.raises(PublicationAuthorityLoss):
        handoff.validate_handoff(
            root,
            document_directory(root, "paper", published=True) / "handoff.json",
            accepted["revision_id"],
        )
    if restore_older:
        assert registry_path(root).read_bytes() == older_index
    else:
        assert not registry_path(root).exists()
    assert unchanged == {
        str(item): digest(item.read_bytes()) for item in root.rglob("publication.json")
    }
    _acknowledge(root)
    assert not show_document(root, "paper")["can_prove"]
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        handoff.validate_handoff(
            root,
            document_directory(root, "paper", published=True) / "handoff.json",
            accepted["revision_id"],
        )


@pytest.mark.parametrize("restore_older", [False, True])
def test_pending_transaction_cannot_be_rolled_back_until_exact_index_loss_acknowledgment(
    tmp_path, restore_older
):
    save_registry(tmp_path, load_registry(tmp_path))
    older = registry_path(tmp_path).read_bytes()
    _interrupt(tmp_path)
    before = (tmp_path / "Main.lean").read_bytes()
    if restore_older:
        registry_path(tmp_path).write_bytes(older)
    else:
        registry_path(tmp_path).unlink()
    with pytest.raises(PublicationAuthorityLoss):
        recover_pending_publication(tmp_path)
    assert (tmp_path / "Main.lean").read_bytes() == before
    acknowledgment = _acknowledge(tmp_path)
    assert acknowledgment["discovered_transactions"]
    with pytest.raises(PublicationConflict, match="requires recovery|conflict unresolved"):
        assert_no_pending_publication(tmp_path)
    recover_pending_publication(tmp_path)
    assert_no_pending_publication(tmp_path)
    assert load_registry(tmp_path)["selected_revision"] is None
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision")


@pytest.mark.parametrize("restore_older", [False, True])
def test_surviving_final_certificate_is_not_current_when_only_registry_is_removed(
    reviewed_project, monkeypatch, admitted_snapshot_layout, restore_older
):
    root, accepted = reviewed_project
    older = registry_path(root).read_bytes()
    path = root / "src/Main.lean"
    document = SourceDocument("src/Main.lean", path.read_text(), replacements={"1": "trivial"})
    path.write_text(document.render())
    profiles = {
        "Paper.target": {
            "name": "Paper.target",
            "file": document.path,
            "type_hash": "type-target",
            "body_hash": "proved",
            "pending": False,
            "conditional": False,
            "axioms": [],
        }
    }
    monkeypatch.setattr(
        handoff,
        "inspect_draft",
        lambda *args, **kwargs: {
            "accepted": True,
            "profiles": profiles,
            **accepted["acceptance_policy"],
        },
    )
    certificate = handoff.proof_preservation_certificate(
        root, accepted, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
    )
    assert show_document(root, "paper")["proof_revision_id"] == certificate["proof_revision_id"]
    if restore_older:
        # Certificates do not change the root index. Add actual intervening
        # transaction history so restoring these bytes is a real index loss.
        _publish_run(root)
        registry_path(root).write_bytes(older)
    else:
        registry_path(root).unlink()
    shown = show_document(root, "paper")
    assert not shown["can_prove"] and shown["proof_completion"] == "pending"
    assert not shown["proof_revision_id"]
    certificate_path = (
        document_directory(root, "paper", published=True)
        / "proof-revisions"
        / (certificate["proof_revision_id"] + ".json")
    )
    original_certificate = certificate_path.read_bytes()
    observed = publication_conflicts(root)["transactions"][0]
    assert accepted["revision_id"] in observed["revoked_revision_ids"]
    _acknowledge(root)
    assert certificate_path.read_bytes() == original_certificate
    assert show_document(root, "paper")["proof_completion"] == "pending"


def test_acknowledged_loss_allows_fresh_acceptance_but_never_reselects_old_commit(tmp_path):
    previous = _publish_run(tmp_path)
    registry_path(tmp_path).unlink()
    _acknowledge(tmp_path)
    assert load_registry(tmp_path)["selected_revision"] is None
    before = {"Main.lean": b"published"}
    outputs = {"Main.lean": b"fresh acceptance"}
    result = publish_revision(
        root=tmp_path,
        directory=tmp_path / ".leanflow/new-revision",
        revision_id="revision-2",
        plan=freeze_publication_plan("revision-2", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )
    assert (
        load_registry(tmp_path)["selected_revision"]["transaction_id"] == result["transaction_id"]
    )
    assert_revision_not_revoked(tmp_path, "revision-2")
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, previous["revision_id"])


def test_loss_recovery_does_not_create_clean_empty_registry_or_acknowledge_unseen_edits(tmp_path):
    _publish_run(tmp_path)
    registry_path(tmp_path).unlink()
    with pytest.raises(PublicationAuthorityLoss):
        save_registry(
            tmp_path,
            {
                "schema_version": 1,
                "active": None,
                "transactions": {},
                "selected_revision": None,
                "conflict_queue": [],
            },
        )
    assert not registry_path(tmp_path).exists()
    observed = publication_conflicts(tmp_path)["transactions"][0]
    request = {
        **{
            name: observed[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        },
        "choice": "keep_current",
        "reason": "Inspected available history",
    }
    (tmp_path / "Main.lean").write_bytes(b"editor saved after observation")
    with pytest.raises(PublicationConflict, match="observation changed"):
        resolve_publication_conflict(tmp_path, request)
    assert not registry_path(tmp_path).exists()
    _acknowledge(tmp_path)
    assert (tmp_path / "Main.lean").read_bytes() == b"editor saved after observation"


def test_portable_import_requires_explicit_acknowledgment_and_new_source_acceptance(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    projection = donor / "formalization/paper"
    durable_json(projection / "current.json", {"schema_version": 1, "revision_id": "rev-imported"})
    durable_json(projection / "handoff.json", {"schema_version": 1, "revision_id": "rev-imported"})
    recipient = tmp_path / "recipient"
    recipient.mkdir()
    shutil.copytree(donor / "formalization", recipient / "formalization")
    with pytest.raises(PublicationAuthorityLoss):
        load_registry(recipient)
    _acknowledge(recipient)
    assert_no_pending_publication(recipient)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(recipient, "rev-imported")
    assert load_registry(recipient)["selected_revision"] is None


@pytest.mark.parametrize(
    "boundary", ["registry_loss_acknowledgment_prepared", "registry_loss_acknowledgment_committed"]
)
def test_interrupted_loss_acknowledgment_has_durable_fail_closed_restart(tmp_path, boundary):
    _publish_run(tmp_path)
    registry_path(tmp_path).unlink()
    observed = publication_conflicts(tmp_path)["transactions"][0]
    request = {
        **{
            name: observed[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        },
        "choice": "keep_current",
        "reason": "Explicitly acknowledged authority loss",
    }

    def interrupt(point):
        if point == boundary:
            raise SystemExit("process interruption")

    with pytest.raises(SystemExit):
        resolve_publication_conflict(tmp_path, request, checkpoint=interrupt)
    if boundary.endswith("prepared"):
        with pytest.raises(PublicationAuthorityLoss):
            assert_revision_not_revoked(tmp_path, "revision-1")
        assert not registry_path(tmp_path).exists()
    else:
        with pytest.raises(ValueError, match="new mathematical acceptance"):
            assert_revision_not_revoked(tmp_path, "revision-1")
        assert load_registry(tmp_path)["selected_revision"] is None


def test_index_restored_during_acknowledgment_is_preserved_without_replacement(tmp_path):
    _publish_run(tmp_path)
    original = registry_path(tmp_path).read_bytes()
    registry_path(tmp_path).unlink()
    observed = publication_conflicts(tmp_path)["transactions"][0]
    request = {
        **{
            name: observed[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        },
        "choice": "keep_current",
        "reason": "Acknowledged the exact absent registry",
    }

    def restore(point):
        if point == "registry_loss_acknowledgment_prepared":
            registry_path(tmp_path).write_bytes(original)

    with pytest.raises(PublicationConflict, match="reappeared"):
        resolve_publication_conflict(tmp_path, request, checkpoint=restore)
    assert registry_path(tmp_path).read_bytes() == original


def test_previous_acknowledgment_cannot_silently_recreate_registry_after_a_new_loss(tmp_path):
    _publish_run(tmp_path)
    registry_path(tmp_path).unlink()
    _acknowledge(tmp_path)
    saved = load_registry(tmp_path)
    registry_path(tmp_path).unlink()
    with pytest.raises(PublicationAuthorityLoss):
        save_registry(tmp_path, saved)
    assert not registry_path(tmp_path).exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_discovered_journal_remains_explicitly_blocked_after_index_acknowledgment(
    tmp_path, corrupt
):
    previous = _publish_run(tmp_path)
    registry = load_registry(tmp_path)
    path = tmp_path / registry["transactions"][previous["transaction_id"]]["journal"]
    if corrupt:
        path.write_bytes(b"unreadable retained journal")
    else:
        path.unlink()
    registry_path(tmp_path).unlink()
    _acknowledge(tmp_path)
    with pytest.raises(PublicationConflict, match="unavailable"):
        assert_no_pending_publication(tmp_path)
    record = load_registry(tmp_path)["transactions"][previous["transaction_id"]]
    assert record["loss"]["state"] == "CONFLICTED"
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    assert_no_pending_publication(tmp_path)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision-1")


@pytest.mark.parametrize("bound", ["entries", "bytes"])
def test_authority_discovery_bounds_refuse_without_writing_an_empty_index(
    tmp_path, monkeypatch, bound
):
    from leanflow_cli.formalization import publication_registry_inventory

    _publish_run(tmp_path)
    registry_path(tmp_path).unlink()
    if bound == "entries":
        monkeypatch.setattr(publication_registry_inventory, "MAX_ENTRIES", 0)
    else:
        monkeypatch.setattr(publication_registry_inventory, "MAX_CONTROL_FILE_BYTES", 1)
    with pytest.raises(PublicationAuthorityLoss, match="bound.*unsupported"):
        load_registry(tmp_path)
    with pytest.raises(PublicationAuthorityLoss, match="no acknowledgment.*truncated"):
        publication_conflicts(tmp_path)
    assert not registry_path(tmp_path).exists()


def test_interruption_after_stale_index_retirement_retains_its_bytes_and_can_be_reobserved(
    tmp_path, monkeypatch
):
    from leanflow_cli.formalization import publication_registry_loss
    from leanflow_cli.formalization.publication_registry_inventory import INDEX_HISTORY

    save_registry(tmp_path, load_registry(tmp_path))
    older = registry_path(tmp_path).read_bytes()
    _conflicted(tmp_path)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    registry_path(tmp_path).write_bytes(older)
    create = publication_registry_loss.create_registry

    def interrupted(path, registry):
        raise SystemExit("Interrupted after exact stale index retirement")

    monkeypatch.setattr(publication_registry_loss, "create_registry", interrupted)
    with pytest.raises(SystemExit):
        _acknowledge(tmp_path)
    assert not registry_path(tmp_path).exists()
    retained = list((tmp_path / INDEX_HISTORY).iterdir())
    assert len(retained) == 1 and retained[0].read_bytes() == older
    with pytest.raises(PublicationAuthorityLoss):
        assert_revision_not_revoked(tmp_path, "rev-1")
    monkeypatch.setattr(publication_registry_loss, "create_registry", create)
    _acknowledge(tmp_path)
    assert_no_pending_publication(tmp_path)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "rev-1")


def test_concurrently_reappearing_index_is_not_clobbered_after_stale_index_retirement(
    tmp_path, monkeypatch
):
    from leanflow_cli.formalization import publication_registry_loss
    from leanflow_cli.formalization.publication_registry_inventory import INDEX_HISTORY

    save_registry(tmp_path, load_registry(tmp_path))
    older = registry_path(tmp_path).read_bytes()
    _conflicted(tmp_path)
    resolve_publication_conflict(tmp_path, _request(tmp_path))
    registry_path(tmp_path).write_bytes(older)
    concurrent = json.loads(older)
    concurrent["external_selection"] = "must survive"
    create = publication_registry_loss.create_registry

    def restore(path, registry):
        durable_json(path, concurrent)
        create(path, registry)

    monkeypatch.setattr(publication_registry_loss, "create_registry", restore)
    with pytest.raises(PublicationConflict, match="restored"):
        _acknowledge(tmp_path)
    assert json.loads(registry_path(tmp_path).read_text()) == concurrent
    retained = list((tmp_path / INDEX_HISTORY).iterdir())
    assert len(retained) == 1 and retained[0].read_bytes() == older
    with pytest.raises(PublicationAuthorityLoss):
        assert_revision_not_revoked(tmp_path, "rev-1")


def test_nonregular_existing_registry_is_rejected_without_a_blocking_read(tmp_path):
    path = registry_path(tmp_path)
    path.parent.mkdir()
    os.mkfifo(path)
    worker = """
from pathlib import Path
import sys
from leanflow_cli.formalization.publication_journal import load_registry
from leanflow_cli.formalization.publication_io import PublicationConflict
try:
    load_registry(Path(sys.argv[1]))
except PublicationConflict:
    pass
else:
    raise AssertionError('Nonregular index was accepted')
"""
    process = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path)], capture_output=True, text=True, timeout=5
    )
    assert process.returncode == 0, process.stderr
    assert path.is_fifo()


def test_healthy_index_size_can_exceed_automatic_loss_recovery_entry_budget(tmp_path, monkeypatch):
    from leanflow_cli.formalization import publication_registry_inventory

    _publish_run(tmp_path)
    before = {"Main.lean": (tmp_path / "Main.lean").read_bytes()}
    outputs = {"Main.lean": b"second accepted publication"}
    publish_revision(
        root=tmp_path,
        directory=tmp_path / ".leanflow/native-workflows/second/publication",
        revision_id="revision-2",
        plan=freeze_publication_plan("revision-2", before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )
    monkeypatch.setattr(publication_registry_inventory, "MAX_ENTRIES", 1)
    assert len(load_registry(tmp_path)["transactions"]) == 2
    assert_revision_not_revoked(tmp_path, "revision-2")
    registry_path(tmp_path).unlink()
    with pytest.raises(PublicationAuthorityLoss, match="unsupported"):
        load_registry(tmp_path)


def test_restored_complete_preacknowledgment_index_cannot_erase_known_registry_loss(tmp_path):
    _publish_run(tmp_path)
    complete_before_loss = registry_path(tmp_path).read_bytes()
    durable_json(
        registry_path(tmp_path),
        {
            "schema_version": 1,
            "active": None,
            "transactions": {},
            "selected_revision": None,
            "conflict_queue": [],
        },
    )
    _acknowledge(tmp_path)
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision-1")
    registry_path(tmp_path).write_bytes(complete_before_loss)
    # All transaction IDs are present, but the retained stale index proves that
    # this older copy predates an explicit authority-loss acknowledgment.
    with pytest.raises(PublicationAuthorityLoss, match="retired registry history"):
        assert_revision_not_revoked(tmp_path, "revision-1")
    assert registry_path(tmp_path).read_bytes() == complete_before_loss
    _acknowledge(tmp_path)
    assert load_registry(tmp_path)["selected_revision"] is None
    with pytest.raises(ValueError, match="new mathematical acceptance"):
        assert_revision_not_revoked(tmp_path, "revision-1")
