"""Share a bounded catalog of exact downloaded resources across private job stages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.session_research import MAX_RESOURCE_BYTES
from leanflow_cli.workflows.prover.source import project_path

MAX_RESOURCES = 16
MAX_CATALOG_CHARACTERS = 12000
MAX_CANDIDATES = 128
MAX_INVENTORY_HASH_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_BYTES = 512 * 1024


def _receipt_path(workspace: Path) -> Path:
    """Locate fetch receipts outside the workspace writable by model tools and Lean."""
    path = workspace.parent / ".runtime" / workspace.name / "resource-receipts.json"
    for candidate in (path, path.parent, path.parent.parent):
        if candidate.is_symlink():
            raise ValueError("Resource receipt paths cannot traverse symlinks")
    return path


def _receipts(workspace: Path) -> dict[str, Any]:
    """Read a bounded private receipt ledger, failing closed on missing or invalid data."""
    try:
        path = _receipt_path(workspace)
        if not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
            return {}
        content = json.loads(path.read_text(encoding="utf-8"))
        records = content.get("resources", {}) if isinstance(content, dict) else {}
        return records if isinstance(records, dict) and len(records) <= MAX_CANDIDATES else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _receipt_record(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Select the provenance fields which must match the fetch-owned receipt."""
    return {key: metadata.get(key) for key in ("path", "url", "sha256", "bytes")}


def record_download(workspace: Path, metadata: Mapping[str, Any]) -> None:
    """Atomically record one successful deterministic fetch outside model write authority."""
    receipt = _receipt_path(workspace)
    records = _receipts(workspace)
    name = Path(str(metadata["path"])).name
    record = _receipt_record(metadata)
    if len(json.dumps({"version": 1, "resources": {name: record}}).encode()) > MAX_RECEIPT_BYTES:
        raise ValueError("Resource receipt exceeds the private ledger size limit")
    records.pop(name, None)
    records[name] = record
    payload = {"version": 1, "resources": records}
    while len(records) > MAX_CANDIDATES or len(json.dumps(payload).encode()) > MAX_RECEIPT_BYTES:
        records.pop(next(iter(records)))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(receipt, payload)


def _matches(path: Path, record: Mapping[str, Any], root: Path) -> bool:
    """Revalidate an exact read grant, including symlinks and content provenance."""
    try:
        if str(path) != record.get("path") or path.parent.name != "resources":
            return False
        project_path(root, str(path.relative_to(root)))
        size = path.stat().st_size
        if not path.is_file() or not 0 <= size <= MAX_RESOURCE_BYTES or size != record.get("bytes"):
            return False
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == record.get("sha256")
    except (OSError, ValueError):
        return False


def resource_inventory(
    jobs: list[dict[str, Any]], *, project_root: Path, run_directory: Path
) -> dict[str, Any]:
    """Collect recent metadata-backed downloads without sharing notes or proof scratch.

    Only finished-job artifacts with private fetch receipts qualify. The catalog
    and total hashing work are bounded independently of campaign length; each
    entry grants access to one exact resource, never to a job directory.
    """
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    candidates, characters, hashed_bytes = 0, 0, 0
    for job in reversed(jobs):
        if not job.get("accounted") or not isinstance(job.get("artifacts"), list):
            continue
        workspace = run_directory / "jobs" / str(job.get("id", ""))
        if str(workspace) != job.get("workspace"):
            continue
        receipts = _receipts(workspace)
        if not receipts:
            continue
        for raw in reversed(job["artifacts"]):
            if not isinstance(raw, str):
                continue
            path = Path(raw)
            if path.parent != workspace / "resources":
                continue
            candidates += 1
            if candidates > MAX_CANDIDATES:
                return {"items": items, "truncated": True}
            manifest = path.with_name(path.name + ".metadata.json")
            try:
                project_path(project_root, str(manifest.relative_to(project_root)))
                if not manifest.is_file() or manifest.stat().st_size > 16384:
                    continue
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or receipts.get(path.name) != _receipt_record(
                    metadata
                ):
                    continue
                size_bytes = path.stat().st_size
                if not 0 <= size_bytes <= MAX_RESOURCE_BYTES:
                    continue
                if hashed_bytes + size_bytes > MAX_INVENTORY_HASH_BYTES:
                    return {"items": items, "truncated": True}
                # Charge before validation so corrupt hashes cannot evade the work cap.
                hashed_bytes += size_bytes
                if not _matches(path, metadata, project_root):
                    continue
                url = metadata.get("url")
                if (
                    not isinstance(url, str)
                    or len(url) > 2000
                    or not url.startswith(("https://", "http://"))
                ):
                    continue
                record = {
                    "job_id": job["id"],
                    "path": str(path),
                    "url": url,
                    "sha256": metadata["sha256"],
                    "bytes": metadata["bytes"],
                }
                identity = (url, str(metadata["sha256"]))
                if identity in seen:
                    continue
                size = len(json.dumps(record, ensure_ascii=False))
                if len(items) >= MAX_RESOURCES or characters + size > MAX_CATALOG_CHARACTERS - 128:
                    return {"items": items, "truncated": True}
                seen.add(identity)
                characters += size
                items.append(record)
            except (OSError, ValueError, UnicodeError):
                continue
    return {"items": items, "truncated": False}


def shared_resource(
    context: Mapping[str, Any], project_root: Path, *, path: Path | None = None, url: str = ""
) -> dict[str, Any] | None:
    """Return one still-valid catalog entry selected by its exact path or URL."""
    catalog = context.get("resources", {})
    entries = catalog.get("items", []) if isinstance(catalog, Mapping) else []
    if not isinstance(entries, list):
        return None
    for record in entries[:MAX_RESOURCES]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            continue
        if (path is not None and str(path) != record["path"]) or (
            path is None and url != record.get("url")
        ):
            continue
        if _matches(Path(record["path"]), record, project_root):
            return dict(record)
    return None
