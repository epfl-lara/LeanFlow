"""Keep compacted setup evidence expected even when its whole archive disappears."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import uuid
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.workflows.project_setup import (
    baseline_history,
    history_archive,
    invocation_evidence,
)
from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.prover import source_mutation


def _archive(tmp_path: Path, count: int = 1) -> tuple[Path, list[str]]:
    """Preserve exact distinct records using the real bounded archive writer."""
    archive = tmp_path / "archive"
    identifiers = [uuid.uuid4().hex for _ in range(count)]
    for identifier in identifiers:
        history_archive.preserve(archive, identifier, json.dumps({"id": identifier}).encode())
    return archive, identifiers


def test_whole_archive_loss_never_means_empty_history(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path)
    shutil.rmtree(archive)
    with pytest.raises(ValueError, match="archive"):
        history_archive.members(archive)


def test_valid_older_header_and_membership_cannot_hide_newer_records(tmp_path: Path) -> None:
    archive, identifiers = _archive(tmp_path)
    before = {name: (archive / name).read_bytes() for name in ("head.json", "index-0000.json")}
    history_archive.preserve(archive, uuid.uuid4().hex, b"second preserved record")
    for name, raw in before.items():
        (archive / name).write_bytes(raw)
    assert (archive / f"{identifiers[0]}.json").is_file()
    with pytest.raises(ValueError, match="anchor"):
        history_archive.members(archive)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "old"])
def test_archive_anchor_is_required_and_bound_to_complete_history(
    tmp_path: Path, damage: str
) -> None:
    archive, _ = _archive(tmp_path)
    anchor = archive.with_name("archive.anchor")
    first = anchor.read_bytes() if anchor.exists() else b"{}"
    history_archive.preserve(archive, uuid.uuid4().hex, b"second preserved record")
    if damage == "missing":
        anchor.unlink(missing_ok=True)
    else:
        anchor.write_bytes(b"[]" if damage == "corrupt" else first)
    with pytest.raises(ValueError, match="anchor"):
        history_archive.members(archive)


def test_archive_anchor_precedes_any_active_journal_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    journal = transaction.begin(root)
    transaction.promote(root, journal, {}, check=lambda: None)
    transaction.finish(root, journal)
    active = root / transaction.BASE / f"{journal['id']}.json"
    archive = active.parent / "archive"
    original_unlink = Path.unlink
    reached = False

    def interrupt(path: Path, *args: object, **kwargs: object) -> None:
        """Crash at the retirement boundary only after positive archive membership exists."""
        nonlocal reached
        if path == active:
            reached = True
            assert archive.with_name("archive.anchor").is_file()
            assert journal["id"] in history_archive.members(archive)
            raise OSError("injected crash before active unlink")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(OSError, match="injected crash"):
        baseline_history.compact_history(root, threshold=1)
    assert reached and active.is_file()
    assert len(transaction.all_records(root)) == 1
    assert transaction.pending_issue(root) is None


def _crash_before_anchor(root: str, home: str) -> None:
    """Kill a real compactor before its first external anchor becomes durable."""
    os.environ["LEANFLOW_HOME"] = home
    original = history_archive.durable_json

    def fail(path: Path, value: object) -> None:
        if (
            path.name == "archive.anchor"
            and isinstance(value, dict)
            and value.get("records", 0) > 0
        ):
            os._exit(71)
        original(path, value)

    history_archive.durable_json = fail
    baseline_history.compact_history(Path(root), threshold=1)
    os._exit(72)


def test_crash_before_anchor_cannot_retire_active_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    journal = transaction.begin(root)
    transaction.promote(root, journal, {}, check=lambda: None)
    transaction.finish(root, journal)
    active = root / transaction.BASE / f"{journal['id']}.json"
    child = multiprocessing.get_context("spawn").Process(
        target=_crash_before_anchor, args=(str(root), str(home))
    )
    child.start()
    child.join(5)
    if child.is_alive():
        child.kill()
        child.join(5)
        pytest.fail("Compaction crash fixture failed to close")
    assert child.exitcode == 71
    assert active.is_file()
    assert "archive" in (transaction.pending_issue(root) or "")


def test_lost_transaction_archive_still_blocks_detached_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_bytes(b"original")
    journal = transaction.begin(root)
    (Path(journal["stage"]) / "lean-toolchain").write_bytes(b"prepared")
    transaction.promote(root, journal, {"lean-toolchain": b"original"}, check=lambda: None)
    transaction.finish(root, journal)
    baseline_history.compact_history(root, threshold=1)
    displaced = source_mutation.directory(root, journal["mutations"][0]) / "displaced"
    displaced.write_bytes(b"late detached writer")
    shutil.rmtree(root / transaction.BASE / "archive")
    assert "archive" in (transaction.pending_issue(root) or "")
    assert displaced.read_bytes() == b"late detached writer"
    with pytest.raises(RuntimeError, match="archive"):
        transaction.begin(root)


def test_entire_invocation_archive_loss_revokes_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    identifier = uuid.uuid4().hex
    durable_json(
        invocation_evidence.register(root, identifier),
        {"id": identifier, "root": str(root), "cleanup_verified": True},
    )
    invocation_evidence.compact(root)
    assert invocation_evidence.expected(root) == []
    assert invocation_evidence.closed(root)
    shutil.rmtree(invocation_evidence.directory(root) / "archive")
    assert not invocation_evidence.closed(root)
