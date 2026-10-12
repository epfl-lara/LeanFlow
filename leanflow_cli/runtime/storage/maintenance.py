"""Report and reclaim owned storage before admitting work under disk pressure."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.runtime.storage.entries import (
    managed_candidates,
    protected_install_paths,
    read_entry,
    recorded_entry_size,
    remove_owned_directory,
    unfinished_install_active,
)
from leanflow_cli.runtime.storage.leases import (
    StorageSafetyError,
    entry_is_leased,
    storage_home,
    storage_lock,
)
from leanflow_cli.runtime.storage.policy import StoragePolicy, storage_policy
from leanflow_cli.runtime.storage.process_views import ProcessViews, cached_process_views
from leanflow_cli.runtime.storage.workspaces import workspace_status


class StoragePressure(RuntimeError):
    """Stop new work until sufficient disk space is available for durable checkpoints."""

    def __init__(self, free_bytes: int, required_bytes: int) -> None:
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes
        super().__init__(
            f"Storage reserve unavailable: {free_bytes / 1024**3:.2f} GiB free; "
            f"{required_bytes / 1024**3:.2f} GiB required. "
            "Unused managed caches were checked; free disk space and resume."
        )


def _disk_free(path: Path) -> int:
    """Inspect the target filesystem even before its destination directory exists."""
    selected = path.absolute()
    while not selected.exists():
        selected = selected.parent
    return shutil.disk_usage(selected).free


def _inventory(
    home: Path, inspect: Callable[[], ProcessViews]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Capture validated entries and protections under the transaction lock."""
    installed = protected_install_paths(home)
    entries: list[dict[str, Any]] = []
    unknown: list[str] = []
    active_sources: list[Path] = []
    for entry in managed_candidates(home):
        payload = read_entry(entry, home)
        if payload is None:
            unknown.append(str(entry))
            continue
        kind = str(payload["kind"])
        runtime = kind in {"runtime", "runtime-release"}
        reason = ""
        try:
            if entry_is_leased(entry, home):
                reason = "active or unverifiable lease"
            elif runtime and installed is None:
                reason = "unverifiable installation metadata"
            elif runtime and any(path.is_relative_to(entry) for path in installed or []):
                reason = "current installation or process"
            elif unfinished_install_active(payload):
                reason = "active or unverifiable installer"
            elif runtime and inspect().uses(entry):
                reason = "live child process or unverifiable process inventory"
            if kind == "runtime" and reason:
                source = payload.get("source_root")
                if isinstance(source, str) and Path(source).is_absolute():
                    active_sources.append(Path(source))
            size = recorded_entry_size(entry, payload)
        except (OSError, StorageSafetyError):
            unknown.append(str(entry))
            continue
        entries.append(
            {
                "path": str(entry),
                "kind": kind,
                "bytes": size,
                "last_used": payload["last_used"],
                "protected": bool(reason),
                "protection": reason,
            }
        )
    for details in entries:
        if details["kind"] == "runtime-release" and any(
            path.is_relative_to(Path(details["path"])) for path in active_sources
        ):
            details["protected"] = True
            details["protection"] = "source of an active runtime"
    return entries, unknown


def _report(
    home: Path, policy: StoragePolicy, path: Path, inspect: Callable[[], ProcessViews]
) -> dict[str, Any]:
    entries, unknown = _inventory(home, inspect)
    views = workspace_status(home, inspect=inspect)
    return {
        "home": str(home),
        "cache_target_bytes": policy.cache_target_bytes,
        "min_free_bytes": policy.min_free_bytes,
        "automatic_cleanup": policy.automatic_cleanup,
        "size_basis": "Allocated bytes per managed tree; shared copy-on-write blocks can be counted more than once.",
        "free_bytes": _disk_free(path),
        "managed_bytes": sum(entry["bytes"] for entry in entries),
        "pinned_bytes": sum(entry["bytes"] for entry in entries if entry["protected"]),
        "workspace_bytes": views["bytes"],
        "workspace_pinned_bytes": views["pinned_bytes"],
        "entries": entries,
        "preserved_unknown": unknown + list(views["preserved"]),
    }


def storage_status(
    *, policy: StoragePolicy | None = None, path: Path | None = None
) -> dict[str, Any]:
    """Report owned storage and active protections without deleting dependency bytes."""
    home = storage_home()
    selected = policy or storage_policy()
    with storage_lock(home):
        return _report(home, selected, path or home, cached_process_views())


def cleanup_storage(
    *, policy: StoragePolicy | None = None, path: Path | None = None, required_bytes: int = 0
) -> dict[str, Any]:
    """Remove abandoned views and least recently used unpinned managed entries."""
    if required_bytes < 0:
        raise ValueError("required storage bytes must be nonnegative")
    home = storage_home()
    selected = policy or storage_policy()
    selected_path = path or home
    with storage_lock(home):
        inspect = cached_process_views()
        before = _report(home, selected, selected_path, inspect)
        views = workspace_status(home, clean=True, inspect=inspect)
        removed = list(views["removed"])
        failed = list(views["preserved"])
        entries, _ = _inventory(home, inspect)
        total = sum(entry["bytes"] for entry in entries)
        for entry in sorted(entries, key=lambda value: value["last_used"]):
            if (
                total <= selected.cache_target_bytes
                and _disk_free(selected_path) >= selected.min_free_bytes + required_bytes
                and entry["kind"] not in {"staging", "runtime", "runtime-release"}
            ):
                continue
            if entry["protected"]:
                continue
            candidate = Path(entry["path"])
            try:
                # Recheck ownership immediately before deletion. The same lock
                # guards publication and leases, so a new user cannot race GC.
                if read_entry(candidate, home) is None or entry_is_leased(candidate, home):
                    failed.append(str(candidate))
                    continue
                remove_owned_directory(candidate)
            except (OSError, StorageSafetyError):
                failed.append(str(candidate))
                continue
            total -= entry["bytes"]
            removed.append(str(candidate))
        after = _report(home, selected, selected_path, inspect)
        after.update(
            removed=removed,
            cleanup_errors=failed,
            reclaimed_bytes=max(
                0,
                before["managed_bytes"]
                + before["workspace_bytes"]
                - after["managed_bytes"]
                - after["workspace_bytes"],
            ),
        )
        return after


def ensure_storage_capacity(
    path: Path | None = None, *, policy: StoragePolicy | None = None, required_bytes: int = 0
) -> dict[str, Any]:
    """Clean before admission and refuse new work if the disk reserve remains unavailable."""
    if required_bytes < 0:
        raise ValueError("required storage bytes must be nonnegative")
    selected = policy or storage_policy()
    home = storage_home()
    with storage_lock(home):
        if (
            selected.automatic_cleanup
            or _disk_free(path or home) < selected.min_free_bytes + required_bytes
        ):
            report = cleanup_storage(policy=selected, path=path, required_bytes=required_bytes)
        else:
            report = _report(home, selected, path or home, cached_process_views())
        if report["free_bytes"] < selected.min_free_bytes + required_bytes:
            raise StoragePressure(report["free_bytes"], selected.min_free_bytes + required_bytes)
        return report
