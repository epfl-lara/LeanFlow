"""Store immutable document revisions separately from disposable native sessions."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identifier,
    identity,
    put_blob,
    read_blob,
)
from leanflow_cli.formalization.publication_case_paths import (
    assert_exact_control_path,
    assert_receipt_control_path,
    exact_receipt_control_path,
)
from leanflow_cli.formalization.receipt_history import copy_receipts
from leanflow_cli.formalization.receipt_history import load_receipt as load_receipt
from leanflow_cli.formalization.receipt_history import receipt_events as receipt_events
from leanflow_cli.formalization.receipt_history import save_receipt as save_receipt

SCHEMA_VERSION = 1
POLICY_ID = "formalization-draft-v1"


class DocumentSelectionConflict(ValueError):
    """Expose divergent local and published selections without guessing which wins."""

    def __init__(self, private_revision: str, published_revision: str) -> None:
        self.private_revision = private_revision
        self.published_revision = published_revision
        super().__init__(
            "Private and published formalization revisions diverged; explicitly select "
            f"{private_revision} or {published_revision} before continuing"
        )


class DocumentLockCancelled(RuntimeError):
    """Stop a queued document operation before it can change stored evidence."""


def _check_document_wait(cancelled: Callable[[], bool] | None, deadline: float | None) -> None:
    """Refuse bounded work without changing the caller's cancellation state."""
    if cancelled is not None and cancelled():
        raise DocumentLockCancelled("Document ownership wait was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise DocumentLockCancelled("Document ownership wait exhausted its deadline")


def _assert_document_lock(
    path: Path, descriptor: int, previous: os.stat_result | None = None
) -> None:
    """Require the opened independent regular entry to remain this document's lock."""
    opened, current = os.fstat(descriptor), path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or current.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or previous is not None
        and (opened.st_dev, opened.st_ino) != (previous.st_dev, previous.st_ino)
    ):
        raise ValueError("Document ownership lock is aliased, nonregular or changed")


def selection_pointer(
    directory: Path, revision: str, *, revoke_authority: bool = False
) -> dict[str, Any]:
    """Preserve automatic campaign authority, revoking it for an explicit user selection."""
    current = contained(directory, "current.json")
    assert_receipt_control_path(directory, "current.json")
    previous = json.loads(current.read_text()) if current.is_file() else {}
    token = previous.get("selection_token") if not revoke_authority else None
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "revision_id": revision,
        "selection_token": identifier(token) if token else "selection-" + uuid.uuid4().hex,
    }
    if revoke_authority:
        result["explicit_selection"] = {
            "document_id": identifier(directory.name),
            "revision_id": revision,
            "selection_token": result["selection_token"],
        }
    if directory.parent.name == "formalizations" and directory.parent.parent.name == ".leanflow":
        published = document_directory(directory.parents[2], directory.name, published=True)
        path = contained(published, "current.json")
        if path.is_file() and exact_receipt_control_path(published, "current.json"):
            result["observed_published_revision"] = identifier(
                json.loads(path.read_text())["revision_id"]
            )
    return result


def current_selection_token(root: Path, document_id: str, *, initialize: bool = False) -> str:
    """Read campaign authority without locking; initialize only at a new campaign boundary."""
    directory = document_directory(root, document_id)
    if initialize:
        with document_lock(root, document_id):
            record = load_document(root, document_id)
            rehydrate_document(record, directory)
            current = contained(directory, "current.json")
            value = json.loads(current.read_text()) if current.is_file() else {}
            if not value.get("selection_token"):
                durable_json(current, selection_pointer(directory, record["revision_id"]))
            return current_selection_token(root, document_id)
    current = contained(directory, "current.json")
    assert_receipt_control_path(directory, "current.json")
    value = json.loads(current.read_text()) if current.is_file() else {}
    token = value.get("selection_token")
    if not token:
        raise ValueError("Formalization selection authority is unavailable; restart the launch")
    return identifier(token)


def _check_selection(candidates: list[Path]) -> None:
    pointers = [contained(directory, "current.json") for directory in candidates]
    if not all(
        path.is_file() and exact_receipt_control_path(directory, "current.json")
        for directory, path in zip(candidates, pointers, strict=True)
    ):
        return
    private, published = [json.loads(path.read_text()) for path in pointers]
    local_id = identifier(private["revision_id"])
    public_id = identifier(published["revision_id"])
    if local_id == public_id or private.get("observed_published_revision") == public_id:
        return
    current: str | None = local_id
    seen = set()
    while current and current not in seen:
        if current == public_id:
            return
        seen.add(current)
        paths = [
            contained(directory, f"revisions/{identifier(current)}.json")
            for directory in candidates
        ]
        path = next((path for path in paths if path.is_file()), None)
        if path is None:
            break
        record = json.loads(path.read_text())
        if current != "rev-" + identity(revision_content(record)):
            raise ValueError("Formalization revision bytes differ from their identity")
        current = record.get("parent_revision_id")
    raise DocumentSelectionConflict(local_id, public_id)


def document_directory(root: Path, document_id: str, *, published: bool = False) -> Path:
    """Locate document authority without accepting user-supplied path fragments."""
    prefix = "formalization" if published else ".leanflow/formalizations"
    return contained(root.resolve(), f"{prefix}/{identifier(document_id)}")


@contextmanager
def document_lock(
    root: Path,
    document_id: str,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[Path]:
    """Serialize document effects; optionally bound queued local recovery waits."""
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("Document wait requires a finite absolute deadline")
    _check_document_wait(cancelled, deadline)
    directory = document_directory(root, document_id)
    assert_exact_control_path(root, directory.relative_to(root.resolve()).as_posix())
    directory.mkdir(parents=True, exist_ok=True)
    path = contained(directory, "document.lock")
    try:
        previous = path.lstat()
    except FileNotFoundError:
        previous = None
    if previous is not None and (not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1):
        raise ValueError("Document ownership lock is aliased, nonregular or changed")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        _assert_document_lock(path, descriptor, previous)
        if cancelled is None and deadline is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            while True:
                _check_document_wait(cancelled, deadline)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic() if deadline is not None else 0.05
                    time.sleep(max(0.0, min(0.05, remaining)))
            _check_document_wait(cancelled, deadline)
        _assert_document_lock(path, descriptor)
        yield directory
    finally:
        os.close(descriptor)


def revision_content(record: dict[str, Any]) -> dict[str, Any]:
    """Exclude derived location and identity fields from the canonical revision hash."""
    return {
        key: value
        for key, value in record.items()
        if key != "revision_id" and not key.startswith("_")
    }


def save_revision(directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Persist exact revision bytes before selecting their immutable identity."""
    assert_receipt_control_path(directory, "current.json")
    assert_receipt_control_path(directory, "revisions")
    content = revision_content(record)
    revision = "rev-" + identity(content)
    result = {**content, "revision_id": revision}
    path = contained(directory, f"revisions/{revision}.json")
    if path.exists():
        if json.loads(path.read_text()) != result:
            raise ValueError("An immutable formalization revision was changed")
    else:
        durable_json(path, result)
    durable_json(
        contained(directory, "current.json"),
        selection_pointer(directory, revision),
    )
    return {**result, "_directory": str(directory)}


def load_document(
    root: Path,
    document_id: str,
    revision: str | None = None,
    *,
    reconcile_receipt: bool = True,
) -> dict[str, Any]:
    """Authenticate a revision, optionally recovering its committed receipt projection."""
    candidates = [
        document_directory(root, document_id),
        document_directory(root, document_id, published=True),
    ]
    if revision is None:
        _check_selection(candidates)
    for directory in candidates:
        if not exact_receipt_control_path(directory, "current.json"):
            continue
        current = contained(directory, "current.json")
        if revision:
            selected = identifier(revision)
        elif current.is_file():
            selected = identifier(str(json.loads(current.read_text())["revision_id"]))
        else:
            continue
        path = contained(directory, f"revisions/{selected}.json")
        if not path.is_file():
            continue
        assert_receipt_control_path(directory, f"revisions/{selected}.json")
        record = json.loads(path.read_text())
        if (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("document_id") != document_id
        ):
            raise ValueError("Invalid formalization revision schema or document identity")
        if (
            selected != "rev-" + identity(revision_content(record))
            or record.get("revision_id") != selected
        ):
            raise ValueError("Formalization revision bytes differ from their identity")
        result = {**record, "_directory": str(directory)}
        if reconcile_receipt:
            from leanflow_cli.formalization.receipt_recovery import reconcile_committed_receipt

            reconcile_committed_receipt(root, result)
        return result
    raise ValueError("Formalization document/revision not found")


def create_document(
    root: Path,
    source: Path,
    *,
    companion_pdf: Path | None = None,
    reconcile_receipt: bool = True,
) -> dict[str, Any]:
    """Freeze a complete source inventory without modifying any project source."""
    from leanflow_cli.formalization.extraction_actions import (
        has_user_extraction_work,
        should_refresh_raw_extraction,
    )
    from leanflow_cli.formalization.source_inventory import inventory_source

    root = root.resolve()
    inventory = inventory_source(root, source, companion_pdf=companion_pdf)
    document_id = identifier(str(inventory["document_id"]))
    with document_lock(root, document_id) as directory:
        previous: dict[str, Any] | None = None
        if (
            contained(directory, "current.json").is_file()
            or contained(
                document_directory(root, document_id, published=True), "current.json"
            ).is_file()
        ):
            previous = load_document(root, document_id, reconcile_receipt=reconcile_receipt)
            pairing = previous["inventory"].get("companion_pdf")
            if companion_pdf is None and pairing:
                inventory = inventory_source(root, source, companion_pdf=Path(pairing["file"]))
        files: dict[str, str] = {}
        for relative, entry in inventory["files"].items():
            try:
                data = read_regular_bytes(contained(root, relative), entry["size"])
            except (OSError, ValueError) as error:
                raise ValueError(
                    "Source changed while the formalization bundle was frozen"
                ) from error
            if digest(data) != str(entry["sha256"]).removeprefix("sha256:"):
                raise ValueError("Source changed while the formalization bundle was frozen")
            files[relative] = put_blob(directory / "blobs", data)
        bundle = identity(files)
        if previous:
            if previous["source_bundle"] == bundle and (
                has_user_extraction_work(previous)
                or (
                    previous["inventory"].get("extraction_version")
                    == inventory.get("extraction_version")
                    and not should_refresh_raw_extraction(previous, inventory)
                )
            ):
                if reconcile_receipt:
                    rehydrate_document(previous, directory)
                    return {**previous, "_directory": str(directory)}
                return previous
        return save_revision(
            directory,
            {
                "schema_version": SCHEMA_VERSION,
                "document_id": document_id,
                "parent_revision_id": previous["revision_id"] if previous else None,
                "source_bundle": bundle,
                "source_files": files,
                "inventory": inventory,
                "units": [],
                "layout": None,
                "status": "inventory_pending",
                "policy_id": POLICY_ID,
                "actions": [],
                "checks": {},
                "reviews": [],
                "handoff": None,
            },
        )


def source_bytes(record: dict[str, Any], relative: str) -> bytes:
    """Read original frozen source; never substitute a live changed file."""
    key = record["source_files"].get(relative)
    if not key:
        raise ValueError("Source is not part of this frozen document")
    assert_receipt_control_path(Path(record["_directory"]), f"blobs/{key}")
    return read_blob(contained(Path(record["_directory"]), "blobs"), key)


def source_artifact_id(record: dict[str, Any], relative: str) -> str:
    """Distinguish original paths even when their frozen content is identical."""
    if relative not in record["source_files"]:
        raise ValueError("Source is not part of this frozen document")
    return "source-" + identity({"file": relative, "sha256": record["source_files"][relative]})


def rehydrate_document(record: dict[str, Any], directory: Path) -> None:
    """Copy verified portable evidence into private state before creating new revisions."""
    original = Path(record["_directory"])
    assert_receipt_control_path(directory, "current.json")
    for name in (
        "blobs",
        "revisions",
        "receipts",
        "receipt-history",
        "review-cache",
        "current.json",
    ):
        assert_receipt_control_path(original, name)
    if original == directory:
        return
    blobs = contained(original, "blobs")
    if blobs.exists():
        for path in sorted(blobs.iterdir()):
            put_blob(contained(directory, "blobs"), read_blob(blobs, path.name))
    for category in ("revisions",):
        parent = contained(original, category)
        if not parent.exists():
            continue
        for path in sorted(parent.iterdir()):
            revision = identifier(path.stem)
            value = json.loads(contained(parent, path.name).read_text())
            if value.get("revision_id") != revision:
                raise ValueError("Portable evidence revision identity does not match its filename")
            if category == "revisions" and revision != "rev-" + identity(revision_content(value)):
                raise ValueError("Portable revision content was modified")
            target = contained(directory, f"{category}/{path.name}")
            if target.exists() and json.loads(target.read_text()) != value:
                raise ValueError("Private and portable evidence disagree")
            if not target.exists():
                durable_json(target, value)
    copy_receipts(original, directory)
    cache = contained(original, "review-cache")
    if cache.exists():
        for path in sorted(cache.iterdir()):
            if path.suffix != ".json" or not re.fullmatch(r"[a-f0-9]{64}", path.stem):
                raise ValueError("Portable review cache filename is invalid")
            value = json.loads(contained(cache, path.name).read_text())
            if not isinstance(value, dict) or value.get("cache_key") != path.stem:
                raise ValueError("Portable review cache identity differs from its filename")
            target = contained(directory, f"review-cache/{path.name}")
            if target.exists() and json.loads(target.read_text()) != value:
                raise ValueError("Private and portable review evidence disagree")
            if not target.exists():
                durable_json(target, value)
    current = contained(directory, "current.json")
    if not current.exists():
        original_pointer = contained(original, "current.json")
        selected = (
            identifier(json.loads(original_pointer.read_text())["revision_id"])
            if original_pointer.is_file()
            else record["revision_id"]
        )
        durable_json(current, selection_pointer(directory, selected))
    if original.parent.name == "formalization":
        from leanflow_cli.formalization.receipt_recovery import reconcile_committed_receipt

        reconcile_committed_receipt(
            original.parent.parent, {**record, "_directory": str(directory)}
        )


def assert_source_current(root: Path, record: dict[str, Any]) -> None:
    """Require live sources to match the reviewed source revision before publication."""
    from leanflow_cli.formalization.inventory_authority import complete_inventory

    inventory = complete_inventory(record)
    for relative, key in record["source_files"].items():
        metadata = inventory.get("files", {}).get(relative, {})
        size = metadata.get("size")
        if type(size) is not int or size < 0:
            raise ValueError(f"Frozen source size is unavailable; repeat source intake: {relative}")
        path = contained(root, relative)
        try:
            data = read_regular_bytes(path, size)
        except (OSError, ValueError) as error:
            raise ValueError(f"Original source changed since intake: {relative}") from error
        if digest(data) != key:
            raise ValueError(f"Original source changed since intake: {relative}")


def _has_explicit_author_selection(record: dict[str, Any], selection_token: str) -> bool:
    """Require a new explicit selection of this exact legacy candidate and campaign token."""
    location = record.get("_directory")
    if not isinstance(location, str) or not selection_token:
        return False
    directory = Path(location)
    if directory.name != record["document_id"]:
        return False
    assert_receipt_control_path(directory, "current.json")
    current = contained(directory, "current.json")
    if not current.is_file():
        return False
    pointer = json.loads(current.read_text())
    return bool(
        pointer.get("revision_id") == record["revision_id"]
        and pointer.get("selection_token") == selection_token
        and pointer.get("explicit_selection")
        == {
            "document_id": record["document_id"],
            "revision_id": record["revision_id"],
            "selection_token": selection_token,
        }
    )


def assert_author_selection(record: dict[str, Any], run_id: str, selection_token: str) -> None:
    """Refuse another campaign's selected candidate until explicit selection revokes its ownership."""
    if not record.get("author_base_revision"):
        return
    if run_id and record.get("author_run_id") == run_id:
        return
    owner_token = record.get("author_selection_token")
    if owner_token and owner_token != selection_token:
        return
    if not owner_token and _has_explicit_author_selection(record, selection_token):
        return
    raise ValueError(
        "Another formalization campaign owns this selected candidate. Resume its run or "
        "explicitly select a source revision before starting a new campaign"
    )


def new_revision(record: dict[str, Any]) -> dict[str, Any]:
    """Invalidate acceptance on any edited evidence while retaining immutable history."""
    result = copy.deepcopy(record)
    result["parent_revision_id"] = record["revision_id"]
    result.pop("revision_id", None)
    result.pop("author_base_revision", None)
    result.pop("author_run_id", None)
    result.pop("author_selection_token", None)
    result.pop("submission_id", None)
    result.update(status="inventory_pending", checks={}, reviews=[], handoff=None)
    return result
