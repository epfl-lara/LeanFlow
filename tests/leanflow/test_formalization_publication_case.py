"""Exercise real case-folding publication consumers while preserving ordinary Lean libraries."""

from __future__ import annotations

import copy
import multiprocessing
import os
import shutil
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from leanflow_cli.formalization import handoff, publication, publication_membership
from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.formalization.publication_io import PublicationUnsupported
from leanflow_cli.formalization.publication_journal import (
    load_registry,
    registry_path,
    save_registry,
)
from leanflow_cli.formalization.publication_registry_inventory import PublicationAuthorityLoss
from leanflow_cli.formalization.records import (
    current_selection_token,
    document_directory,
    load_document,
    load_receipt,
    save_receipt,
    save_revision,
    selection_pointer,
)
from leanflow_cli.formalization.service import inspect_document, list_documents, show_document
from leanflow_cli.workflows.prover.source import discover
from tests.leanflow import test_formalization_handoff as handoff_tests

reviewed = handoff_tests.reviewed


@pytest.mark.parametrize("component", ["formalization", "current.json", "revisions"])
def test_case_renamed_portable_authority_never_becomes_a_fresh_project(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    root, accepted = reviewed
    portable = tmp_path / "portable"
    shutil.copytree(root, portable, ignore=shutil.ignore_patterns(".leanflow", "portable"))
    if component == "formalization":
        original = portable / component
        renamed = portable / "Formalization"
    else:
        original = portable / "formalization/paper" / component
        renamed = original.with_name(component.capitalize())
    original.rename(renamed)
    if original.exists():
        # This branch executes on the actual case-insensitive APFS host. Preserve
        # evidence that the bytes remain reachable through the old spelling.
        try:
            record = load_document(portable, "paper")
            old_receipt = load_receipt(record)
        except ValueError:
            old_receipt = None
        assert old_receipt is None
    monkeypatch.setattr(publication, "assert_no_pending_publication", _real_pending)
    with pytest.raises(PublicationAuthorityLoss):
        load_registry(portable)
    if original.exists():
        try:
            shown = show_document(portable, "paper")
        except ValueError:
            pass
        else:
            assert shown["can_prove"] is False
        with pytest.raises(ValueError):
            handoff.validate_handoff(
                portable,
                portable / "formalization/paper/handoff.json",
                accepted["revision_id"],
                checked_environment_id="environment",
            )


_real_pending = publication.assert_no_pending_publication


@pytest.mark.parametrize(
    "component",
    [
        "formalization",
        "current.json",
        "handoff.json",
        "revisions",
        "receipts",
        "receipt-history",
        "revision_file",
        "receipt_file",
        "history_revision",
        "history_file",
    ],
)
def test_healthy_index_cannot_authorize_case_aliased_controls(
    reviewed, component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, accepted = reviewed
    if component == "formalization":
        original = root / component
        renamed = original.with_name("Formalization")
    elif component == "revision_file":
        original = root / f"formalization/paper/revisions/{accepted['revision_id']}.json"
        renamed = original.with_suffix(".JSON")
    elif component == "receipt_file":
        original = root / f"formalization/paper/receipts/{accepted['revision_id']}.json"
        renamed = original.with_suffix(".JSON")
    elif component == "history_revision":
        original = root / f"formalization/paper/receipt-history/{accepted['revision_id']}"
        renamed = original.with_name(original.name.upper())
    elif component == "history_file":
        original = next(
            (root / f"formalization/paper/receipt-history/{accepted['revision_id']}").glob("*.json")
        )
        renamed = original.with_suffix(".JSON")
    else:
        original = root / "formalization/paper" / component
        renamed = original.with_name(component.capitalize())
    original.rename(renamed)
    monkeypatch.setattr(publication, "assert_no_pending_publication", _real_pending)
    with pytest.raises(PublicationAuthorityLoss):
        load_registry(root)
    with pytest.raises(ValueError):
        handoff.validate_handoff(
            root,
            root / "formalization/paper/handoff.json",
            accepted["revision_id"],
            checked_environment_id="environment",
        )


@pytest.mark.parametrize("namespace", ["Formalization", "formalization"])
def test_ordinary_library_and_private_intake_remain_usable(tmp_path: Path, namespace: str) -> None:
    library = tmp_path / namespace / "Library"
    library.mkdir(parents=True)
    source = library / "User.lean"
    source.write_text("theorem ordinary : True := by sorry\n")
    if namespace == "Formalization":
        (library / "current.json").write_text('{"user_library":true}')
    (library / "data.json").write_text('{"ordinary":true}')
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    intake = inspect_document(tmp_path, tmp_path / "paper.tex")
    assert len(list_documents(tmp_path)["documents"]) == 1
    private = document_directory(tmp_path, intake["document_id"])
    token = current_selection_token(tmp_path, intake["document_id"])
    pointer = selection_pointer(private, intake["revision_id"])
    assert pointer["selection_token"] == token
    assert "observed_published_revision" not in pointer
    with publication.publication_guard(tmp_path):
        dag, _ = discover(tmp_path, [source], fill_definitions=False)
    assert len(dag.roots) == 1
    assert load_registry(tmp_path)["transactions"] == {}
    assert not registry_path(tmp_path).exists()


@pytest.mark.parametrize("kind", ["case_alias", "ordinary_file"])
def test_prospective_case_collision_preserves_ordinary_library_before_registration(
    tmp_path: Path, kind: str
) -> None:
    if kind == "case_alias":
        library = tmp_path / "Formalization/Library"
        library.mkdir(parents=True)
        (library / "User.lean").write_text("def ordinary : Nat := 1\n")
        if not (tmp_path / "formalization").exists():
            pytest.skip("Actual case-folding filesystem is required for the namespace collision")
    else:
        (tmp_path / "formalization").write_text("ordinary source artifact")
    original = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    output = {"formalization/new-document/current.json": b'{"revision_id":"reviewed"}'}
    before = dict.fromkeys(output)
    with pytest.raises(PublicationUnsupported, match="case alias|collides"):
        publication.publish_revision(
            root=tmp_path,
            directory=tmp_path / ".leanflow/test-publication",
            revision_id="reviewed",
            plan=publication.freeze_publication_plan(
                "reviewed", before, output, owned_paths=output
            ),
            before=before,
            outputs=output,
            validate=lambda: None,
        )
    assert {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".leanflow" not in path.relative_to(tmp_path).parts
    } == original
    assert not (tmp_path / "Formalization/new-document").exists()
    assert not registry_path(tmp_path).exists()
    assert not (tmp_path / ".leanflow/formalization-publication-transactions").exists()


def test_an_empty_earlier_index_never_authenticates_an_imported_receipt(
    reviewed, tmp_path: Path
) -> None:
    root, accepted = reviewed
    prior = tmp_path / "genuinely-fresh-project"
    prior.mkdir()
    save_registry(prior, load_registry(prior))
    earlier_index = registry_path(prior).read_bytes()
    portable = tmp_path / "portable-with-old-index"
    shutil.copytree(
        root,
        portable,
        ignore=shutil.ignore_patterns(".leanflow", prior.name, portable.name),
    )
    path = registry_path(portable)
    path.parent.mkdir()
    path.write_bytes(earlier_index)
    assert load_registry(portable)["transactions"] == {}
    assert not show_document(portable, "paper")["can_prove"]
    with pytest.raises(PublicationAuthorityLoss, match="matching committed"):
        handoff.validate_handoff(
            portable,
            portable / "formalization/paper/handoff.json",
            accepted["revision_id"],
            checked_environment_id="environment",
        )


def test_a_later_document_does_not_revoke_an_earlier_committed_handoff(reviewed) -> None:
    root, accepted = reviewed
    record = copy.deepcopy(load_document(root, "paper"))
    record["document_id"] = "second"
    directory = document_directory(root, "second", published=True)
    record = save_revision(directory, record)
    second = handoff.build_handoff(
        root,
        document_id="second",
        revision_id=record["revision_id"],
        registry=record["checks"],
        files=record["project_source_hashes"],
        environment_id=record["environment_id"],
    )
    durable_json(directory / "handoff.json", second)
    save_receipt(
        directory,
        {"status": "published", "revision_id": record["revision_id"], "handoff": second},
    )
    handoff_tests._publish_fixture_controls(root, "second", record["revision_id"])
    assert load_registry(root)["selected_revision"]["revision_id"] == record["revision_id"]
    assert show_document(root, "paper")["can_prove"]
    assert (
        handoff.validate_handoff(
            root,
            root / "formalization/paper/handoff.json",
            accepted["revision_id"],
            checked_environment_id="environment",
        )
        == accepted
    )


def test_large_accepted_revision_has_no_new_recovery_json_size_cap(reviewed) -> None:
    root, _ = reviewed
    record = copy.deepcopy(load_document(root, "paper"))
    record["setup_notes"] = "Retained mathematical context.\n" * 300_000
    staged = document_directory(root, "paper")
    record = save_revision(staged, record)
    accepted = handoff.build_handoff(
        root,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=record["checks"],
        files=record["project_source_hashes"],
        environment_id=record["environment_id"],
    )
    durable_json(staged / "handoff.json", accepted)
    save_receipt(
        staged,
        {"status": "published", "revision_id": record["revision_id"], "handoff": accepted},
    )
    handoff_tests._publish_fixture_controls(root, "paper", record["revision_id"], staged=staged)
    assert (staged / "revisions" / f"{record['revision_id']}.json").stat().st_size > 8 * 1024**2
    assert show_document(root, "paper")["can_prove"]


def _hash_observer(path_name: str, channel: Connection, phase: str) -> None:
    """Pause an actual descriptor read at the competing editor's substitution boundary."""
    path = Path(path_name)
    actual_open, actual_fstat = os.open, os.fstat
    observed = False

    def open_file(target, flags, *args, **kwargs):
        if phase == "open" and Path(target) == path:
            channel.send("paused")
            assert channel.recv() == "continue"
        return actual_open(target, flags, *args, **kwargs)

    def fstat(descriptor):
        nonlocal observed
        result = actual_fstat(descriptor)
        if phase == "read" and not observed:
            observed = True
            channel.send("paused")
            assert channel.recv() == "continue"
        return result

    publication_membership.os.open = open_file
    publication_membership.os.fstat = fstat
    try:
        publication_membership._control_digest(path)
    except (OSError, ValueError):
        channel.send("rejected")
    else:
        channel.send("accepted")
    finally:
        channel.close()


@pytest.mark.parametrize("substitute", ["fifo", "same_bytes_new_inode"])
@pytest.mark.parametrize("phase", ["open", "read"])
def test_publication_control_hash_rejects_competing_substitution_without_blocking(
    tmp_path: Path, substitute: str, phase: str
) -> None:
    path = tmp_path / "control.json"
    path.write_bytes(b"original controller bytes")
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_hash_observer, args=(str(path), remote, phase))
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "paused"
        path.rename(tmp_path / "retained-control.json")
        if substitute == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"original controller bytes")
        local.send("continue")
        assert local.poll(2), "Control hashing waited on a substituted FIFO"
        assert local.recv() == "rejected"
        process.join(2)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        local.close()
