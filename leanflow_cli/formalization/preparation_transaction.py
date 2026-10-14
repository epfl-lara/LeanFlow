"""Commit and reconcile setup with separate durable authority and mathematical source protection."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, identity
from leanflow_cli.formalization.preparation_disposal import (
    dispose_preparation_incoming as dispose_preparation_incoming,
)
from leanflow_cli.formalization.preparation_fs_inputs import (
    CONFIG,
    MANAGED,
    copy_entry,
    import_root_path,
    managed_path,
    validate_import_root,
)
from leanflow_cli.formalization.preparation_fs_inputs import (
    entry_identity as entry_identity,
)
from leanflow_cli.formalization.preparation_fs_inputs import (
    protected_inputs as protected_inputs,
)
from leanflow_cli.formalization.preparation_journal import (
    TERMINAL,
    audit_retained,
    load_journal,
    load_registry,
    observed_versions,
    save_journal,
    save_registry,
    settle,
)
from leanflow_cli.formalization.preparation_journal import (
    PreparationConflict as PreparationConflict,
)
from leanflow_cli.formalization.preparation_journal import (
    preparation_directory as preparation_directory,
)
from leanflow_cli.formalization.preparation_loss import loss_projection, resolve_journal_loss
from leanflow_cli.formalization.preparation_progress import save_entry_progress
from leanflow_cli.formalization.preparation_recovery import recover_locked
from leanflow_cli.formalization.publication_io import (
    check_filesystem,
    entry_path,
    mkdir_durable,
    publication_lock,
    rename_exclusive,
)
from leanflow_cli.workflows.project_setup.cleanup import process_states


def load_preparation_transaction(root: Path) -> dict[str, Any] | None:
    """Read the latest historical setup record, including interrupted settlement."""
    registry = load_registry(root)
    transaction_id = registry["current"]
    if transaction_id in registry.get("journal_losses", {}):
        return loss_projection(root, preparation_directory(root), registry, transaction_id)
    return load_journal(root, transaction_id) if transaction_id else None


def assert_no_pending_preparation(root: Path) -> None:
    """Audit every retained setup generation and refuse pending infrastructure work."""
    root = root.resolve()
    if not (preparation_directory(root) / "registry.json").exists():
        return
    with publication_lock(root):
        registry = load_registry(root)
        if registry["active"] and registry["active"] not in registry.get("journal_losses", {}):
            journal = load_journal(root, registry["active"])
            if journal["state"] in TERMINAL:
                settle(root, registry, journal)
            elif journal["state"] != "CONFLICTED":
                raise PreparationConflict(
                    "Project preparation needs recovery or explicit conflict resolution"
                )
        audit_retained(root, registry)


def recover_preparation(
    root: Path, *, checkpoint: Callable[[str], None] | None = None
) -> dict[str, Any] | None:
    """Recover active setup first, then audit older retained descriptors before admission."""
    root = root.resolve()
    if not (preparation_directory(root) / "registry.json").exists():
        return None
    with publication_lock(root):
        registry = load_registry(root)
        current = registry["active"] or registry["current"]
        if current in registry.get("journal_losses", {}):
            audit_retained(root, registry)
            return loss_projection(root, preparation_directory(root), registry, current)
        journal = load_journal(root, current) if current else None
        if journal is not None and registry["active"]:
            recover_locked(root, registry, journal, checkpoint)
        audit_retained(root, registry)
        return journal


def preparation_conflicts(root: Path) -> dict[str, Any]:
    """Expose transaction-bound versions while ordinary startup remains blocked."""
    root = root.resolve()
    if not (preparation_directory(root) / "registry.json").exists():
        return {"schema_version": 1, "transactions": []}
    with publication_lock(root):
        registry = load_registry(root)
        audit_retained(root, registry, raise_conflicts=False)
        result = []
        for transaction_id in registry["transactions"]:
            if transaction_id in registry.get("journal_losses", {}):
                result.append(
                    loss_projection(root, preparation_directory(root), registry, transaction_id)
                )
                continue
            journal = load_journal(root, transaction_id)
            if journal["conflicts"] or journal["resolutions"]:
                result.append(
                    {
                        **observed_versions(root, journal),
                        "state": journal["state"],
                        "conflicts": journal["conflicts"],
                        "resolutions": journal["resolutions"],
                        "active": registry["active"] == transaction_id,
                        "queued": transaction_id in registry.get("conflict_queue", []),
                        "resolvable": journal["state"] == "CONFLICTED",
                        "revokes_current_receipt": registry.get("selected") == transaction_id,
                    }
                )
        return {"schema_version": 1, "transactions": result}


def commit_preparation(
    root: Path,
    staged: Path,
    *,
    managed: Mapping[str, str | None],
    protected: Mapping[str, str],
    receipt: dict[str, Any],
    validate: Callable[[], None],
    cancelled: Callable[[], bool],
    checkpoint: Callable[[str], None] | None = None,
    check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Select checked infrastructure and absent import roots with no-clobber recovery."""
    root, staged = root.resolve(), staged.resolve()
    with publication_lock(root):
        assert_no_pending_preparation(root)
        registry = load_registry(root)
        transaction_id = uuid.uuid4().hex
        slots_relative = f".leanflow/preparation-transactions/{transaction_id}"
        slots = contained(root, slots_relative)
        mkdir_durable(slots)
        writes: dict[str, dict[str, Any]] = {}
        names = sorted(managed)
        if any(right.startswith(left + "/") for left, right in zip(names, names[1:])):
            raise ValueError("Preparation writes must be disjoint entries")
        for index, (relative, before) in enumerate(sorted(managed.items())):
            source = contained(staged, relative)
            if not managed_path(relative, before):
                raise ValueError(
                    "Preparation cannot rewrite existing mathematical source or unowned infrastructure"
                )
            if relative not in MANAGED and not relative.startswith(".lake/"):
                if not import_root_path(relative, before):
                    raise ValueError("Preparation cannot rewrite existing mathematical source")
                validate_import_root(source)
            after = entry_identity(source, check=check)
            if after is None or after == before:
                continue
            if relative in CONFIG and not source.is_file():
                raise ValueError("Preparation configuration must remain an ordinary file")
            writes[relative] = {
                "before": before,
                "after": after,
                "kind": (
                    "infrastructure"
                    if relative in MANAGED or relative.startswith(".lake/")
                    else "import_root"
                ),
                "incoming": f"{slots_relative}/{index}.incoming",
                "captured": f"{slots_relative}/{index}.before",
                "stage": "ready",
                "rollback_slots": [],
            }
        owner = {"pid": os.getpid(), "start": process_states()[os.getpid()][2]}
        precommit_cleanup = receipt.get("precommit_cleanup")
        if precommit_cleanup is not None and (
            not isinstance(precommit_cleanup, dict) or precommit_cleanup.get("owner") != owner
        ):
            raise ValueError("Preparation cleanup must belong to the original controller")
        plan = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "root": str(root),
            "owner": owner,
            "writes": {
                relative: {
                    key: row[key] for key in ("before", "after", "kind", "incoming", "captured")
                }
                for relative, row in writes.items()
            },
            "protected": dict(protected),
            "receipt": receipt,
        }
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "root": str(root),
            "owner": plan["owner"],
            "state": "PREPARING",
            "plan": plan,
            "plan_id": identity(plan),
            "writes": writes,
            "order": sorted(writes),
            "conflicts": [],
            "resolutions": [],
        }
        save_journal(root, journal)
        registry["transactions"].append(transaction_id)
        registry["active"] = registry["current"] = transaction_id
        save_registry(root, registry)

        def boundary(name: str, *, committed: bool = False) -> None:
            if checkpoint:
                checkpoint(name)
            if not committed and cancelled():
                raise InterruptedError("Project preparation cancelled")

        def inspect_io() -> None:
            if check:
                check()
            if cancelled():
                raise InterruptedError("Project preparation cancelled")

        def save() -> None:
            save_journal(
                root,
                journal,
                lambda name: boundary(name, committed=journal["state"] == "COMMITTED"),
            )

        def save_entry(relative: str) -> None:
            save_entry_progress(
                preparation_directory(root),
                journal,
                relative,
                lambda name: boundary(name),
            )

        try:
            boundary("registered")
            for relative, row in writes.items():
                boundary("copy_pending:" + relative)
                source, incoming = contained(staged, relative), entry_path(root, row["incoming"])
                if source.is_dir() and os.stat(source).st_dev == os.stat(slots).st_dev:
                    inspect_io()
                    rename_exclusive(source, incoming)
                    metadata = incoming.lstat()
                    row["incoming_inode"] = [metadata.st_dev, metadata.st_ino]
                    save_entry(relative)
                    copied = entry_identity(incoming, check=inspect_io)
                else:

                    def inspect_copy() -> None:
                        if "incoming_inode" not in row and incoming.exists():
                            metadata = incoming.lstat()
                            row["incoming_inode"] = [metadata.st_dev, metadata.st_ino]
                            save_entry(relative)
                        inspect_io()

                    copied = copy_entry(source, incoming, check=inspect_copy)
                if copied != row["after"]:
                    raise PreparationConflict("Prepared bytes changed during private copying")
                metadata = entry_path(root, row["incoming"]).lstat()
                row["incoming_inode"] = [metadata.st_dev, metadata.st_ino]
                save_entry(relative)
                boundary("copied:" + relative)
            check_filesystem(root, slots, [entry_path(root, name) for name in writes])
            journal["state"] = "PREPARED"
            save()
            boundary("prepared")
            if protected_inputs(root, source_paths=protected) != dict(protected):
                raise PreparationConflict("User source changed during preparation")
            if any(
                entry_identity(entry_path(root, name), check=inspect_io) != row["before"]
                for name, row in writes.items()
            ):
                raise PreparationConflict("Infrastructure changed during preparation")
            journal["state"] = "PUBLISHING"
            save()
            boundary("publishing")
            for relative, row in writes.items():
                destination = entry_path(root, relative)
                mkdir_durable(destination.parent)
                row["stage"] = "capture_pending"
                save_entry(relative)
                boundary("capture:" + relative)
                if row["before"] is not None:
                    rename_exclusive(destination, entry_path(root, row["captured"]))
                    boundary("captured:" + relative)
                    if (
                        entry_identity(entry_path(root, row["captured"]), check=inspect_io)
                        != row["before"]
                    ):
                        raise PreparationConflict("Captured infrastructure changed")
                row["stage"] = "install_pending"
                save_entry(relative)
                boundary("install:" + relative)
                rename_exclusive(entry_path(root, row["incoming"]), destination)
                boundary("moved:" + relative)
                row["stage"] = "installed"
                save_entry(relative)
                boundary("installed:" + relative)
            boundary("before_commit")
            validate()
            current_protected = protected_inputs(root, source_paths=protected)
            for relative, row in writes.items():
                if row["kind"] == "import_root":
                    current_protected.pop(relative, None)
            if current_protected != dict(protected):
                raise PreparationConflict("User source changed before preparation commitment")
            if any(
                entry_identity(entry_path(root, name), check=inspect_io) != row["after"]
                or entry_identity(entry_path(root, row["captured"]), check=inspect_io)
                not in {None, row["before"]}
                for name, row in writes.items()
            ):
                raise PreparationConflict(
                    "Prepared or retained infrastructure changed before commitment"
                )
            journal["state"] = "COMMITTED"
            save()
            boundary("committed", committed=True)
            settle(root, registry, journal)
            boundary("settled", committed=True)
            return journal
        except Exception:
            recover_locked(root, registry, journal, checkpoint=None)
            raise


def resolve_preparation_conflict(
    root: Path,
    observed: dict[str, Any],
    reason: str,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Keep reconciled canonical entries, archive all actual versions, and revoke readiness."""
    root = root.resolve()
    if not isinstance(reason, str) or not reason.strip():
        raise PreparationConflict("Preparation resolution requires a reconciliation reason")
    with publication_lock(root):
        registry = load_registry(root)
        transaction_id = observed.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or transaction_id not in registry["transactions"]
            or (
                registry["active"] != transaction_id
                and transaction_id not in registry.get("conflict_queue", [])
            )
        ):
            raise PreparationConflict(
                "Preparation resolution transaction is not the active conflict"
            )
        if transaction_id in registry.get("journal_losses", {}):
            try:
                result = resolve_journal_loss(
                    root, preparation_directory(root), registry, observed, reason
                )
            except ValueError as error:
                raise PreparationConflict(str(error)) from error
            save_registry(root, registry)
            if checkpoint:
                checkpoint("resolution_settled")
            return result
        journal = load_journal(root, transaction_id)
        if journal["state"] != "CONFLICTED" or not journal["conflicts"]:
            raise PreparationConflict("Recover preparation before resolving its preserved conflict")
        current = observed_versions(root, journal)
        if observed != current:
            raise PreparationConflict(
                "Preparation versions changed; inspect again before resolving"
            )
        from leanflow_cli.formalization.preparation_resolution import archive_resolution_versions

        try:
            archives = archive_resolution_versions(
                root,
                journal,
                current,
                directory=preparation_directory(root),
                checkpoint=checkpoint,
            )
        except ValueError as error:
            raise PreparationConflict(str(error)) from error
        if checkpoint:
            checkpoint("resolution_versions_archived")
        from leanflow_cli.formalization.preparation_resolution_progress import apply_archive_intents

        try:
            apply_archive_intents(preparation_directory(root), journal)
        except ValueError as error:
            raise PreparationConflict(str(error)) from error
        actual = observed_versions(root, journal)
        registered = {row["path"] for row in journal.get("archive_copy_intents", [])}
        if (
            actual["destinations"] != current["destinations"]
            or actual["history_id"] != current["history_id"]
            or any(
                actual["retained_slots"].get(path) != expected
                for path, expected in current["retained_slots"].items()
            )
            or set(actual["retained_slots"]) - set(current["retained_slots"]) - registered
            or any(
                entry_identity(entry_path(root, path)) != expected
                for expected, path in archives.items()
            )
        ):
            raise PreparationConflict("Preparation versions changed before resolution commitment")
        payload = {
            "observation": current,
            "choice": "keep_current",
            "reason": reason,
            "archives": archives,
            "unavailable_versions": {
                path: expected
                for prior in journal["resolutions"]
                for expected, path in prior["archives"].items()
                if current["retained_slots"].get(path) != expected
            },
            "receipt_revoked": True,
        }
        resolution = {**payload, "resolution_id": "prep-resolution-" + identity(payload)}
        journal["resolutions"].append(resolution)
        journal["state"] = "RESOLVED"
        save_journal(root, journal, checkpoint)
        settle(root, registry, journal)
        if checkpoint:
            checkpoint("resolution_settled")
        return journal
