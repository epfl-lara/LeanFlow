"""Publish derived cache batches with exact rollback and a durable refresh admission barrier."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identity,
    sync_directory,
)
from leanflow_cli.formalization.publication_io import rename_exclusive
from leanflow_cli.workflows.prover.scoped_cache_barrier import (
    CacheVersion as CacheVersion,
)
from leanflow_cli.workflows.prover.scoped_cache_barrier import (
    assert_cache_ready as assert_cache_ready,
)
from leanflow_cli.workflows.prover.scoped_cache_barrier import (
    cache_refresh_needed as cache_refresh_needed,
)
from leanflow_cli.workflows.prover.scoped_cache_barrier import (
    clear_cache_refresh,
    exclusive_json,
    read_cache_file,
)
from leanflow_cli.workflows.prover.scoped_cache_barrier import (
    mark_cache_refresh_needed as mark_cache_refresh_needed,
)

CacheReplacement = tuple[bytes | None, CacheVersion | None]
_BASE = ".leanflow/scoped-import-cache"
_PARTS = (
    ".olean",
    ".olean.server",
    ".olean.private",
    ".ir",
    ".ir.sig",
    ".ilean",
    ".trace",
    ".hash",
)


def cache_version(path: Path) -> CacheVersion | None:
    """Bind an observed cache inode and bytes without following an unknown alias."""
    value = read_cache_file(path)
    return value[0] if value is not None else None


def _row_progress(retained: Path, plan_id: str, row: dict[str, Any], state: str) -> None:
    """Flush bounded row progress without rewriting the full immutable batch plan."""
    row["state"] = state
    payload = {
        "schema_version": 1,
        "plan_id": plan_id,
        **{key: item for key, item in row.items() if key != "content"},
    }
    durable_json(
        retained / "progress" / (digest(row["relative"]) + ".json"),
        {**payload, "record_id": identity(payload)},
    )


def _restore_batch(
    root: Path, retained: Path, plan_id: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Restore exact original inodes only into absent targets and retain every unknown replacement."""
    restored, preserved = [], []
    for row in reversed(rows):
        if row["state"] == "READY":
            continue
        relative = row["relative"]
        target = contained(root, relative)
        previous = retained / "before" / relative
        removed = retained / "rollback" / relative
        try:
            current = cache_version(target)
            installed = row.get("incoming_version")
            if installed is not None and current == installed:
                removed.parent.mkdir(parents=True, exist_ok=True)
                _row_progress(retained, plan_id, row, "ROLLBACK_CAPTURE_PENDING")
                rename_exclusive(target, removed)
                if cache_version(removed) != installed:
                    if cache_version(target) is None:
                        rename_exclusive(removed, target)
                    preserved.append(relative + ": changed rollback capture preserved")
                    continue
                current = cache_version(target)
            prior = cache_version(previous)
            if prior is not None:
                if current is None:
                    _row_progress(retained, plan_id, row, "RESTORE_PENDING")
                    rename_exclusive(previous, target)
                    if cache_version(target) == row["before"]:
                        restored.append(relative)
                    else:
                        preserved.append(
                            relative + ": unknown captured original restored without clobber"
                        )
                else:
                    preserved.append(
                        relative + ": independent target and retained original preserved"
                    )
            elif row["before"] is not None and current != row["before"]:
                preserved.append(
                    relative + ": recorded original unavailable; current version preserved"
                )
            elif current is not None and current != row["before"]:
                preserved.append(relative + ": independent target preserved")
            _row_progress(retained, plan_id, row, "ROLLED_BACK")
        except (OSError, ValueError) as error:
            preserved.append(relative + ": " + str(error))
    return {
        "restored_artifacts": restored,
        "preserved_unknown_versions": preserved,
        "refresh_required": True,
    }


def _fence_rollback(root: Path, retained: Path, coverage: set[str]) -> tuple[bool, str | None]:
    """Require a durable admission blocker before compensating cache mutations."""
    try:
        if cache_refresh_needed(root) is None:
            mark_cache_refresh_needed(
                root,
                "Stopped scoped cache batch requires fresh compilation: " + str(retained),
                affected_paths=sorted(coverage),
            )
        if cache_refresh_needed(root) is not None:
            return True, None
        return False, "Scoped cache refresh blocker could not be observed"
    except ValueError as error:
        # Unreadable metadata itself refuses admission and is untouched by rollback.
        return True, str(error)
    except OSError as error:
        return False, str(error)


def install_cache_batch(
    root: Path,
    replacements: Mapping[Path, CacheReplacement],
    retained: Path,
    *,
    before_commit: Callable[[], None] | None = None,
    after_commit: Callable[[], None] | None = None,
    checkpoint: Callable[[str], None] | None = None,
    full_refresh: bool = False,
) -> dict[str, Any]:
    """Publish one derived batch under caller-held publication ownership, rolling back failures.

    Check cancellation, deadlines and whole-source drift in the pre/post guards;
    per-entry mutation checks only bounded inode/content identities. A stopped
    or crashed batch retains its durable admission blocker and all cache versions.
    """
    root = root.resolve()
    retained = contained(root, retained.relative_to(root).as_posix())
    if not retained.relative_to(root).as_posix().startswith(_BASE + "/"):
        raise ValueError("Scoped cache retention must remain in registered controller state")
    if before_commit:
        before_commit()
    prior_marker = cache_refresh_needed(root)
    coverage = {target.relative_to(root).as_posix() for target in replacements}
    if prior_marker is not None and not full_refresh:
        affected = prior_marker.get("affected_paths")
        if not isinstance(affected, list) or not set(affected) <= coverage:
            raise ValueError("Scoped cache needs a complete refresh of its prior dirty paths")
    rows: list[dict[str, Any]] = []
    for target, (content, before) in replacements.items():
        relative = target.relative_to(root).as_posix()
        target = contained(root, relative)
        if not target.name.endswith(_PARTS):
            raise ValueError("Scoped cache batch cannot write source files: " + relative)
        if cache_version(target) != before:
            raise ValueError("Local import artifact changed during compilation: " + relative)
        if (content is None and before is None) or (
            content is not None and before is not None and before[-1] == digest(content)
        ):
            continue
        rows.append(
            {
                "relative": relative,
                "content": content,
                "before": before,
                "after": digest(content) if content is not None else None,
                "state": "READY",
            }
        )
    retained.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "root": str(root),
        "retained": str(retained),
        "rows": [{key: item for key, item in row.items() if key != "content"} for row in rows],
    }
    plan_id = identity(payload)
    exclusive_json(retained / "batch.json", {**payload, "plan_id": plan_id})
    marker_id = mark_cache_refresh_needed(
        root, "Incomplete scoped cache batch: " + str(retained), affected_paths=sorted(coverage)
    )

    def boundary(name: str) -> None:
        if checkpoint:
            checkpoint(name)

    try:
        for row in rows:
            relative, content = row["relative"], row["content"]
            target = contained(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = contained(root, relative)
            previous = retained / "before" / relative
            incoming = retained / "incoming" / relative
            if content is not None:
                incoming.parent.mkdir(parents=True, exist_ok=True)
                _row_progress(retained, plan_id, row, "STAGE_PENDING")
                with incoming.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(incoming.parent)
                row["incoming_version"] = cache_version(incoming)
            if cache_version(target) != row["before"]:
                raise ValueError("Local import artifact changed before capture: " + relative)
            if row["before"] is not None:
                previous.parent.mkdir(parents=True, exist_ok=True)
                _row_progress(retained, plan_id, row, "CAPTURE_PENDING")
                boundary("capture:" + relative)
                rename_exclusive(target, previous)
                if cache_version(previous) != row["before"]:
                    raise ValueError("Unknown captured local import artifact: " + relative)
            _row_progress(retained, plan_id, row, "INSTALL_PENDING")
            boundary("install:" + relative)
            if content is not None:
                rename_exclusive(incoming, target)
                if cache_version(target) != row["incoming_version"]:
                    raise ValueError("Published local import artifact changed: " + relative)
            elif cache_version(target) is not None:
                raise ValueError("Retired local import artifact reappeared: " + relative)
            _row_progress(retained, plan_id, row, "INSTALLED")
            boundary("installed:" + relative)
        if after_commit:
            after_commit()
        for target, (content, before) in replacements.items():
            current = cache_version(contained(root, target.relative_to(root).as_posix()))
            if (content is None and current is not None) or (
                content is not None and (current is None or current[-1] != digest(content))
            ):
                raise ValueError("Scoped cache batch changed after publication: " + str(target))
        result = {
            "batch_id": plan_id,
            "artifacts": {
                target.relative_to(root).as_posix(): digest(content)
                for target, (content, before) in replacements.items()
                if content is not None
            },
            "retired_artifacts": [
                target.relative_to(root).as_posix()
                for target, (content, before) in replacements.items()
                if content is None
            ],
            "retained_before_artifacts": [
                str(retained / "before" / row["relative"])
                for row in rows
                if row["before"] is not None
            ],
            "refresh_required": False,
        }
        durable_json(retained / "outcome.json", {"status": "COMMITTED", **result})
        clear_cache_refresh(root, marker_id)
        return result
    except BaseException as error:
        fenced, blocker_error = _fence_rollback(root, retained, coverage)
        report = (
            _restore_batch(root, retained, plan_id, rows)
            if fenced
            else {
                "restored_artifacts": [],
                "preserved_unknown_versions": [],
                "refresh_required": True,
                "rollback_skipped": True,
                "retained_batch": str(retained),
            }
        )
        if blocker_error is not None:
            report["refresh_observation_error"] = blocker_error
        setattr(error, "scoped_cache_report", report)
        try:
            durable_json(
                retained / "outcome.json",
                {"status": "ROLLED_BACK" if fenced else "RETAINED", "error": str(error), **report},
            )
        except (OSError, ValueError) as record_error:
            error.add_note("Scoped cache rollback outcome could not be saved: " + str(record_error))
        raise
