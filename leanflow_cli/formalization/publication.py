"""Publish selected frozen revisions with project-wide recovery and conflict preservation."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, durable_json, put_blob, sync_directory
from leanflow_cli.formalization.publication_io import (
    PublicationCancelled,
    PublicationConflict,
    PublicationUnsupported,
    check_filesystem,
    entry_hash,
    entry_path,
    mkdir_durable,
    publication_lock,
    rename_exclusive,
)
from leanflow_cli.formalization.publication_journal import (
    audit_retained,
    current_journal,
    load_journal,
    load_registry,
    save_journal,
    save_registry,
    settle_terminal,
)
from leanflow_cli.formalization.publication_plan import (
    freeze_publication_plan,
    validate_publication_plan,
)
from leanflow_cli.formalization.publication_recovery import recover_locked

__all__ = [
    "PublicationCancelled",
    "PublicationConflict",
    "PublicationUnsupported",
    "assert_no_pending_publication",
    "freeze_publication_plan",
    "publication_guard",
    "publish_revision",
    "recover_pending_publication",
    "recover_publication",
    "rename_exclusive",
]


def _directory(root: Path, directory: Path) -> Path:
    cursor = directory.absolute()
    while cursor.resolve() != root and cursor.parent != cursor:
        if cursor.is_symlink():
            raise ValueError("Publication state cannot traverse a symlink")
        cursor = cursor.parent
    relative = directory.resolve().relative_to(root).as_posix()
    if not relative.startswith(".leanflow/"):
        raise ValueError("Publication journals must remain in project-owned private state")
    return contained(root, relative)


@contextmanager
def publication_guard(root: Path) -> Iterator[None]:
    """Hold project ownership while auditing recovery and executing a consumer action."""
    root = root.resolve()
    with publication_lock(root):
        registry = load_registry(root)
        audit_retained(root, registry)
        if registry["active"] is not None:
            path = contained(root, registry["transactions"][registry["active"]]["journal"])
            journal = load_journal(path)
            if journal["state"] in {"COMMITTED", "ROLLED_BACK"} and not journal["conflicts"]:
                settle_terminal(root, registry, journal)
            else:
                raise PublicationConflict(
                    f"Formalization publication requires recovery before further work: {path}"
                )
        yield


def assert_no_pending_publication(root: Path) -> None:
    """Refuse work while any revision requires recovery or has retained user edits."""
    with publication_guard(root):
        pass


def recover_pending_publication(root: Path) -> dict[str, Any] | None:
    """Recover registered interrupted work at startup while preserving every conflict.

    Call before loading document or handoff evidence. Ongoing consumers must use
    the conservative guard instead, so a nested check never rolls back its publisher.
    """
    root = root.resolve()
    with publication_lock(root):
        registry = load_registry(root)
        audit_retained(root, registry)
        if registry["active"] is None:
            return None
        journal_path = contained(root, registry["transactions"][registry["active"]]["journal"])
        recovered = recover_locked(root, journal_path, registry)
        audit_retained(root, registry)
        return recovered


def recover_publication(
    root: Path,
    directory: Path,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Recover under the same project lock used by publishers and evidence consumers."""
    root = root.resolve()
    directory = _directory(root, directory)
    with publication_lock(root):
        registry = load_registry(root)
        journal_path = current_journal(directory)
        if journal_path is None:
            return None
        journal = load_journal(journal_path)
        if registry["active"] not in {None, journal["transaction_id"]}:
            raise PublicationConflict("Another project publication must recover first")
        if journal["transaction_id"] not in registry["transactions"]:
            registry["transactions"][journal["transaction_id"]] = {
                "journal": journal_path.relative_to(root).as_posix()
            }
            registry["active"] = journal["transaction_id"]
            save_registry(root, registry)
        return recover_locked(root, journal_path, registry, checkpoint)


def publish_revision(
    *,
    root: Path,
    directory: Path,
    revision_id: str,
    plan: Mapping[str, Any],
    before: Mapping[str, bytes | None],
    outputs: Mapping[str, bytes],
    validate: Callable[[], None],
    validate_commit: Callable[[], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Commit exactly the frozen ownership plan while retaining displaced actual inodes.

    External readers may observe intermediate files. The journal and registry
    define commitment for cooperating consumers; flushed records are tested for
    process interruption, not independently certified for storage-device power loss.
    """
    frozen = validate_publication_plan(plan, revision_id, before, outputs)
    root = root.resolve()
    directory = _directory(root, directory)

    def boundary(name: str, *, committed: bool = False) -> None:
        if checkpoint:
            checkpoint(name)
        if not committed and cancelled and cancelled():
            raise PublicationCancelled("Formalization publication was cancelled before commitment")

    with publication_lock(root):
        registry = load_registry(root)
        audit_retained(root, registry)
        previous_path = current_journal(directory)
        previous = load_journal(previous_path) if previous_path else None
        if registry["active"] is not None:
            if not previous or registry["active"] != previous["transaction_id"]:
                raise PublicationConflict("An earlier project publication requires recovery")
            assert previous_path is not None
            previous = recover_locked(root, previous_path, registry)
        if previous and previous["state"] == "COMMITTED":
            if previous["plan_id"] != frozen["plan_id"]:
                raise ValueError("Publication directory already committed a different frozen plan")
            if any(
                entry_hash(entry_path(root, relative)) != row["after"]
                for relative, row in frozen["writes"].items()
            ):
                raise PublicationConflict("Previously committed destinations have changed")
            return previous
        validate()
        boundary("validated")
        targets = [contained(root, relative) for relative in frozen["writes"]]
        check_filesystem(root, directory, targets)
        for relative, row in frozen["writes"].items():
            if entry_hash(entry_path(root, relative)) != row["before"]:
                raise PublicationConflict(
                    f"Destination changed after the frozen acceptance baseline: {relative}"
                )
        for target in targets:
            if target.exists() and any(
                contained(root, protected).exists() and target.samefile(contained(root, protected))
                for protected in frozen["protected_paths"]
            ):
                raise PublicationConflict(
                    "Publication destination aliases a protected source inode"
                )
        transaction_id = uuid.uuid4().hex
        transaction = contained(directory, f"transactions/{transaction_id}")
        mkdir_durable(transaction / "staging")
        mkdir_durable(transaction / "displaced")
        writes = {
            relative: {
                **row,
                "captured": f"displaced/{index}.before",
                "staged": f"staging/{index}.after",
                "rollback_slots": [],
                "stage": "staged",
                "mode": (
                    stat.S_IMODE(entry_path(root, relative).stat().st_mode)
                    if row["before"] is not None
                    else 0o644
                ),
            }
            for index, (relative, row) in enumerate(frozen["writes"].items())
        }
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "revision_id": revision_id,
            "plan_id": frozen["plan_id"],
            "plan": frozen,
            "state": "PREPARING",
            "status": "PREPARING",
            "writes": writes,
            "order": list(writes),
            "created_dirs": [],
            "conflicts": [],
        }
        journal_path = transaction / "publication.json"
        save_journal(journal_path, journal)
        durable_json(
            directory / "publication.json",
            {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "journal": f"transactions/{transaction_id}/publication.json",
            },
        )
        registry["transactions"][transaction_id] = {
            "journal": journal_path.relative_to(root).as_posix()
        }
        registry["active"] = transaction_id
        save_registry(root, registry)

        def save() -> None:
            save_journal(
                journal_path,
                journal,
                lambda name: boundary(name, committed=journal["state"] == "COMMITTED"),
            )

        try:
            boundary("registered")
            for relative, entry in writes.items():
                put_blob(transaction / "blobs", outputs[relative])
                prior = before[relative]
                if prior is not None:
                    put_blob(transaction / "blobs", prior)
                with contained(transaction, entry["staged"]).open("xb") as handle:
                    handle.write(outputs[relative])
                    boundary(f"written:{relative}")
                    handle.flush()
                    os.fchmod(handle.fileno(), entry["mode"])
                    os.fsync(handle.fileno())
                    info = os.fstat(handle.fileno())
                    entry["staged_inode"] = [info.st_dev, info.st_ino]
                    boundary(f"flushed:{relative}")
            sync_directory(transaction / "staging")
            journal["state"] = "PREPARED"
            save()
            boundary("prepared")
            journal["state"] = "PUBLISHING"
            save()
            for relative, entry in writes.items():
                target = entry_path(root, relative)
                missing: list[Path] = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    created: dict[str, Any] = {
                        "path": parent.relative_to(root).as_posix(),
                        "inode": None,
                    }
                    journal["created_dirs"].append(created)
                    save()
                    try:
                        parent.mkdir()
                    except FileExistsError:
                        continue
                    sync_directory(parent.parent)
                    info = parent.stat()
                    created["inode"] = [info.st_dev, info.st_ino]
                    save()
                captured = contained(transaction, entry["captured"])
                if entry["before"] is not None:
                    entry["stage"] = "capture_pending"
                    save()
                    boundary(f"capture_pending:{relative}")
                    rename_exclusive(target, captured)
                    boundary(f"captured:{relative}")
                    if entry_hash(captured) != entry["before"]:
                        raise PublicationConflict(f"Concurrent edit captured for {relative}")
                    entry["stage"] = "captured"
                    save()
                entry["stage"] = "install_pending"
                save()
                rename_exclusive(contained(transaction, entry["staged"]), target)
                boundary(f"installed:{relative}")
                entry["stage"] = "installed"
                save()
            boundary("before_commit")
            if validate_commit:
                validate_commit()
            for relative, entry in writes.items():
                if entry_hash(entry_path(root, relative)) != entry["after"]:
                    raise PublicationConflict(f"Destination changed before commitment: {relative}")
                captured_hash = entry_hash(contained(transaction, entry["captured"]))
                if captured_hash is not None and captured_hash != entry["before"]:
                    raise PublicationConflict(f"Open-descriptor edit retained for {relative}")
            if cancelled and cancelled():
                raise PublicationCancelled(
                    "Formalization publication was cancelled before commitment"
                )
            journal["state"] = "COMMITTED"
            save()
            settle_terminal(root, registry, journal)
            boundary("committed", committed=True)
            return journal
        except Exception as error:
            persisted = load_journal(journal_path)
            if persisted["state"] == "COMMITTED":
                settle_terminal(root, registry, persisted)
                return persisted
            try:
                recover_locked(root, journal_path, registry)
            except PublicationConflict as conflict:
                raise conflict from error
            if isinstance(error, (FileExistsError, FileNotFoundError)):
                raise PublicationConflict(
                    "Concurrent destination change prevented publication"
                ) from error
            raise
