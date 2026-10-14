"""Register resolution archive copies before creation and preserve interrupted or independent versions."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, identity
from leanflow_cli.formalization.preparation_fs_inputs import copy_entry, entry_identity
from leanflow_cli.formalization.preparation_resolution_progress import save_archive_intent
from leanflow_cli.formalization.publication_io import entry_path


def archive_intents(journal: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate immutable copy intents without treating expected bytes as completed archives."""
    rows = journal.get("archive_copy_intents", [])
    prefix = f".leanflow/preparation-transactions/{journal['transaction_id']}/"
    if not isinstance(rows, list):
        raise ValueError("Preparation archive copy intents are invalid")
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "expected", "source", "observation_id", "intent_id"}
            or not isinstance(row.get("expected"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["expected"])
            or not isinstance(row.get("path"), str)
            or not re.fullmatch(
                re.escape(prefix + "versions/" + row["expected"]) + r"(?:-[0-9]+)?", row["path"]
            )
            or row["path"] in seen
            or not isinstance(row.get("source"), str)
            or not (row["source"] in journal["writes"] or row["source"].startswith(prefix))
            or not isinstance(row.get("observation_id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["observation_id"])
            or row.get("intent_id")
            != identity({key: value for key, value in row.items() if key != "intent_id"})
        ):
            raise ValueError("Preparation archive copy intent identity changed")
        seen.add(row["path"])
    return rows


def resolution_archive_paths(root: Path, journal: dict[str, Any]) -> set[str]:
    """Expose registered targets and legacy orphan archive entries as actual observed versions."""
    result = {row["path"] for row in archive_intents(journal)}
    directory = contained(
        root, f".leanflow/preparation-transactions/{journal['transaction_id']}/versions"
    )
    if directory.exists():
        result.update(path.relative_to(root).as_posix() for path in directory.iterdir())
    return result


def archive_resolution_versions(
    root: Path,
    journal: dict[str, Any],
    observed: dict[str, Any],
    *,
    directory: Path,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Preserve every confirmed actual identity without overwriting partial or independent copies."""
    archives: dict[str, str] = {}
    intents = journal.setdefault("archive_copy_intents", [])
    registered = {row["path"] for row in intents}
    prefix = f".leanflow/preparation-transactions/{journal['transaction_id']}/versions/"

    def point(name: str) -> None:
        if checkpoint:
            checkpoint(name)

    for group in ("destinations", "retained_slots"):
        for source, expected in observed[group].items():
            if expected is None or expected in archives:
                continue
            if entry_identity(entry_path(root, source)) != expected:
                raise ValueError("Preparation source version changed during archive copying")
            relative = prefix + expected
            index = 0
            while (
                relative in observed["retained_slots"]
                and observed["retained_slots"][relative] != expected
            ) or entry_identity(entry_path(root, relative)) not in {None, expected}:
                index += 1
                relative = prefix + expected + "-" + str(index)
            target = entry_path(root, relative)
            if relative not in registered:
                payload = {
                    "path": relative,
                    "expected": expected,
                    "source": source,
                    "observation_id": observed["observation_id"],
                }
                intent = {**payload, "intent_id": identity(payload)}
                save_archive_intent(directory, journal, intent)
                intents.append(intent)
                registered.add(relative)
                point("resolution_archive_registered:" + relative)
            if entry_identity(target) is None:
                point("resolution_archive_copy_pending:" + relative)
                copied = copy_entry(
                    entry_path(root, source),
                    target,
                    check=lambda: point("resolution_archive_copy:" + relative),
                )
                if copied != expected:
                    raise ValueError("Preparation archived source version changed")
            if entry_identity(target) != expected:
                raise ValueError("Preparation archived resolution version changed")
            archives[expected] = relative
            if entry_identity(entry_path(root, source)) != expected:
                raise ValueError("Preparation source version changed while archiving resolution")
            point("resolution_archive_copied:" + relative)
    return archives
