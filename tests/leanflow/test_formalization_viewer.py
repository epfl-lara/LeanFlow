"""Open immutable viewer bytes during controller ownership without bypassing proof authority."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from leanflow_cli.formalization.evidence import digest, put_blob
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    document_lock,
    load_document,
    new_revision,
    save_revision,
)
from leanflow_cli.formalization.service import resolve_artifact, show_document
from leanflow_cli.formalization.source_draft import freeze_preview
from tests.leanflow.test_source_inventory import _pdf


@pytest.fixture
def viewer_record(tmp_path: Path) -> dict:
    """Freeze real source, companion PDF and ordinary/context Lean evidence."""
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
    pdf = tmp_path / "paper.pdf"
    _pdf(pdf, ["Theorem 1. Zero equals zero."])
    record = create_document(tmp_path, source, companion_pdf=pdf)
    directory = document_directory(tmp_path, record["document_id"])
    record["lean_files"] = {
        "Main/Statements.lean": put_blob(
            directory / "blobs", b"theorem Paper.zero : 0 = 0 := by sorry\n"
        )
    }
    record["context_lean_files"] = {
        "Main/Context.lean": put_blob(directory / "blobs", b"def Paper.zero : Nat := 0\n")
    }
    return save_revision(directory, record)


_OPEN = """
import json, sys
from pathlib import Path
from leanflow_cli.formalization.service import resolve_artifact, show_document
payload = json.loads(sys.argv[1])
root = Path(payload['root'])
shown = show_document(root, payload['document_id'], verify_current=False)
opened = resolve_artifact(root, payload['document_id'], payload['revision_id'], payload['artifact_id'])
print(json.dumps({'revision_id': shown['revision_id'], 'artifact': opened}))
"""


@pytest.mark.parametrize(
    "file", ["paper.tex", "paper.pdf", "Main/Statements.lean", "Main/Context.lean"]
)
def test_frozen_viewer_opens_in_real_child_while_both_controller_locks_are_held(
    tmp_path: Path, viewer_record: dict, file: str
) -> None:
    """Reproduce the editor's show/artifact chain while a different process owns both locks."""
    record = viewer_record
    shown = show_document(tmp_path, record["document_id"], verify_current=False)
    artifact = next(row for row in shown["artifacts"] if row["path"] == file)
    payload = {
        "root": str(tmp_path),
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "artifact_id": artifact["id"],
    }
    source_before = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    with publication_lock(tmp_path), document_lock(tmp_path, record["document_id"]):
        result = subprocess.run(
            [sys.executable, "-c", _OPEN, json.dumps(payload)],
            text=True,
            capture_output=True,
            timeout=4,
            check=True,
        )
    opened = json.loads(result.stdout)
    assert opened["revision_id"] == record["revision_id"]
    assert digest(Path(opened["artifact"]["path"]).read_bytes()) == artifact["sha256"]
    assert all((tmp_path / name).read_bytes() == data for name, data in source_before.items())
    assert load_document(tmp_path, record["document_id"])["revision_id"] == record["revision_id"]


def test_concurrent_viewer_creation_publishes_one_complete_regular_file(
    tmp_path: Path, viewer_record: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import service_viewer

    record = viewer_record
    artifact = show_document(tmp_path, record["document_id"], verify_current=False)["artifacts"][0]
    barrier = Barrier(4)
    original = service_viewer.rename_exclusive

    def publish(source: Path, target: Path) -> None:
        barrier.wait(timeout=4)
        original(source, target)

    monkeypatch.setattr(service_viewer, "rename_exclusive", publish)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                resolve_artifact,
                tmp_path,
                record["document_id"],
                record["revision_id"],
                artifact["id"],
            )
            for _ in range(4)
        ]
        opened = [future.result(timeout=6) for future in futures]
    targets = {row["path"] for row in opened}
    assert len(targets) == 1
    target = Path(targets.pop())
    assert digest(target.read_bytes()) == artifact["sha256"]
    assert target.stat().st_nlink == 1
    assert target.stat().st_mode & 0o222 == 0
    assert list(target.parent.iterdir()) == [target]


def test_concurrent_differing_viewer_entry_is_never_overwritten_or_returned(
    tmp_path: Path, viewer_record: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import service_viewer

    record = viewer_record
    artifact = show_document(tmp_path, record["document_id"], verify_current=False)["artifacts"][0]
    original = service_viewer.rename_exclusive
    targets = []

    def foreign_save(source: Path, target: Path) -> None:
        targets.append(target)
        target.write_bytes(b"Unrelated concurrent viewer save")
        original(source, target)

    monkeypatch.setattr(service_viewer, "rename_exclusive", foreign_save)
    with pytest.raises(ValueError, match="modified"):
        resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact["id"])
    assert targets[0].read_bytes() == b"Unrelated concurrent viewer save"
    assert list(targets[0].parent.iterdir()) == targets


def _draft_preview(root: Path, record: dict, run_id: str) -> dict:
    """Retain a real immutable draft without changing the selected source revision."""
    project = root / ".leanflow" / run_id
    module = project / "Main/Draft/Statements.lean"
    module.parent.mkdir(parents=True)
    module.write_text(f"theorem Paper.{run_id} : True := by sorry\n")
    (project / "Main/Draft.lean").write_text("import Main.Draft.Statements\n")
    return freeze_preview(
        root,
        record,
        project,
        layout={"owned_prefix": "Main/Draft", "aggregator": "Main/Draft.lean"},
        run_id=run_id,
        selection_token=current_selection_token(root, record["document_id"]),
        model_report="Unaccepted draft statements; theorem proofs remain sorry.",
    )


def test_frozen_draft_opens_in_real_child_while_controller_owns_both_locks(
    tmp_path: Path, viewer_record: dict
) -> None:
    record = viewer_record
    preview = _draft_preview(tmp_path, record, "initial")
    artifact = next(row for row in preview["artifacts"] if row["path"].endswith("Statements.lean"))
    payload = {
        "root": str(tmp_path),
        "document_id": record["document_id"],
        "revision_id": record["revision_id"],
        "artifact_id": artifact["id"],
    }
    with publication_lock(tmp_path), document_lock(tmp_path, record["document_id"]):
        result = subprocess.run(
            [sys.executable, "-c", _OPEN, json.dumps(payload)],
            text=True,
            capture_output=True,
            timeout=4,
            check=True,
        )
    opened = json.loads(result.stdout)["artifact"]
    assert Path(opened["path"]).read_text() == "theorem Paper.initial : True := by sorry\n"
    assert opened["sha256"] == artifact["sha256"]
    assert load_document(tmp_path, record["document_id"])["revision_id"] == record["revision_id"]


def test_preview_change_during_materialization_rejects_the_removed_artifact(
    tmp_path: Path, viewer_record: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import service_viewer

    record = viewer_record
    preview = _draft_preview(tmp_path, record, "initial")
    artifact = next(row for row in preview["artifacts"] if row["path"].endswith("Statements.lean"))
    original = service_viewer._materialize

    def replace_preview(*args, **kwargs):
        result = original(*args, **kwargs)
        _draft_preview(tmp_path, record, "replacement")
        return result

    monkeypatch.setattr(service_viewer, "_materialize", replace_preview)
    with pytest.raises(ValueError, match="does not belong"):
        resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact["id"])


@pytest.mark.parametrize("tamper", ["bytes", "symlink", "hardlink", "oversize"])
def test_modified_or_aliased_cache_is_preserved_and_rejected(
    tmp_path: Path, viewer_record: dict, tamper: str
) -> None:
    record = viewer_record
    artifact = show_document(tmp_path, record["document_id"], verify_current=False)["artifacts"][0]
    opened = resolve_artifact(
        tmp_path, record["document_id"], record["revision_id"], artifact["id"]
    )
    target = Path(opened["path"])
    original = target.read_bytes()
    target.unlink()
    if tamper == "symlink":
        other = tmp_path / "foreign"
        other.write_bytes(original)
        target.symlink_to(other)
    elif tamper == "hardlink":
        target.write_bytes(original)
        (tmp_path / "foreign").hardlink_to(target)
    elif tamper == "oversize":
        with target.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
    else:
        target.write_bytes(b"Changed viewer bytes")
    before = target.lstat()
    with pytest.raises(ValueError):
        resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact["id"])
    after = target.lstat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def test_selection_change_during_materialization_rejects_the_old_mapping(
    tmp_path: Path, viewer_record: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import service_viewer

    record = viewer_record
    artifact = show_document(tmp_path, record["document_id"], verify_current=False)["artifacts"][0]
    original = service_viewer._materialize

    def change_selection(*args, **kwargs):
        result = original(*args, **kwargs)
        changed = new_revision(record)
        changed["setup_notes"] = "Concurrent controller selection"
        save_revision(document_directory(tmp_path, record["document_id"]), changed)
        return result

    monkeypatch.setattr(service_viewer, "_materialize", change_selection)
    with pytest.raises(ValueError, match="revision changed"):
        resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact["id"])


@pytest.mark.parametrize("guard", ["publication", "document"])
def test_proof_artifact_keeps_full_publication_and_document_guards(
    tmp_path: Path, viewer_record: dict, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    def forbidden(*args, **kwargs):
        raise RuntimeError("Current proof authority was required")

    target = (
        "leanflow_cli.formalization.publication.publication_guard"
        if guard == "publication"
        else "leanflow_cli.formalization.service_viewer.document_lock"
    )
    monkeypatch.setattr(target, forbidden)
    with pytest.raises(RuntimeError, match="Current proof authority"):
        resolve_artifact(
            tmp_path,
            viewer_record["document_id"],
            viewer_record["revision_id"],
            "proof-lean-" + "a" * 64,
        )


@pytest.mark.parametrize("tamper", ["bytes", "symlink", "hardlink", "oversize", "fifo"])
def test_unsafe_blob_is_rejected_before_a_viewer_copy_is_created(
    tmp_path: Path, viewer_record: dict, tamper: str
) -> None:
    record = viewer_record
    artifact = show_document(tmp_path, record["document_id"], verify_current=False)["artifacts"][0]
    directory = document_directory(tmp_path, record["document_id"])
    blob = directory / "blobs" / artifact["sha256"]
    data = blob.read_bytes()
    blob.unlink()
    if tamper == "fifo":
        os.mkfifo(blob)
    elif tamper == "symlink":
        other = tmp_path / "foreign"
        other.write_bytes(data)
        blob.symlink_to(other)
    elif tamper == "hardlink":
        blob.write_bytes(data)
        (tmp_path / "foreign").hardlink_to(blob)
    elif tamper == "oversize":
        with blob.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
    else:
        blob.write_bytes(b"Changed frozen blob")
    with pytest.raises(ValueError):
        resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact["id"])
    assert not (directory / "viewer").exists()
