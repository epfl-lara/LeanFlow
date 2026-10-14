"""Store immutable document revisions separately from disposable native sessions."""

from __future__ import annotations

import copy
import fcntl
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identifier,
    identity,
    put_blob,
    read_blob,
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


def selection_pointer(
    directory: Path, revision: str, *, revoke_authority: bool = False
) -> dict[str, Any]:
    """Preserve automatic campaign authority, revoking it for an explicit user selection."""
    current = contained(directory, "current.json")
    previous = json.loads(current.read_text()) if current.is_file() else {}
    token = previous.get("selection_token") if not revoke_authority else None
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "revision_id": revision,
        "selection_token": identifier(token) if token else "selection-" + uuid.uuid4().hex,
    }
    if directory.parent.name == "formalizations" and directory.parent.parent.name == ".leanflow":
        published = document_directory(directory.parents[2], directory.name, published=True)
        path = contained(published, "current.json")
        if path.is_file():
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
    value = json.loads(current.read_text()) if current.is_file() else {}
    token = value.get("selection_token")
    if not token:
        raise ValueError("Formalization selection authority is unavailable; restart the launch")
    return identifier(token)


def _check_selection(candidates: list[Path]) -> None:
    pointers = [contained(directory, "current.json") for directory in candidates]
    if not all(path.is_file() for path in pointers):
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
def document_lock(root: Path, document_id: str) -> Iterator[Path]:
    """Serialize intake and user actions independently from native model execution."""
    directory = document_directory(root, document_id)
    directory.mkdir(parents=True, exist_ok=True)
    with contained(directory, "document.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield directory


def revision_content(record: dict[str, Any]) -> dict[str, Any]:
    """Exclude derived location and identity fields from the canonical revision hash."""
    return {
        key: value
        for key, value in record.items()
        if key != "revision_id" and not key.startswith("_")
    }


def save_revision(directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Persist exact revision bytes before selecting their immutable identity."""
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


def load_document(root: Path, document_id: str, revision: str | None = None) -> dict[str, Any]:
    """Load and authenticate a selected private or portable document revision."""
    candidates = [
        document_directory(root, document_id),
        document_directory(root, document_id, published=True),
    ]
    if revision is None:
        _check_selection(candidates)
    for directory in candidates:
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
        from leanflow_cli.formalization.receipt_recovery import reconcile_committed_receipt

        reconcile_committed_receipt(root, result)
        return result
    raise ValueError("Formalization document/revision not found")


def create_document(
    root: Path, source: Path, *, companion_pdf: Path | None = None
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
            previous = load_document(root, document_id)
            pairing = previous["inventory"].get("companion_pdf")
            if companion_pdf is None and pairing:
                inventory = inventory_source(root, source, companion_pdf=Path(pairing["file"]))
        files: dict[str, str] = {}
        for relative, entry in inventory["files"].items():
            data = contained(root, relative).read_bytes()
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
                rehydrate_document(previous, directory)
                return {**previous, "_directory": str(directory)}
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
    return read_blob(contained(Path(record["_directory"]), "blobs"), key)


def source_artifact_id(record: dict[str, Any], relative: str) -> str:
    """Distinguish original paths even when their frozen content is identical."""
    if relative not in record["source_files"]:
        raise ValueError("Source is not part of this frozen document")
    return "source-" + identity({"file": relative, "sha256": record["source_files"][relative]})


def rehydrate_document(record: dict[str, Any], directory: Path) -> None:
    """Copy verified portable evidence into private state before creating new revisions."""
    original = Path(record["_directory"])
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
    for relative, key in record["source_files"].items():
        path = contained(root, relative)
        if not path.is_file() or digest(path.read_bytes()) != key:
            raise ValueError(f"Original source changed since intake: {relative}")


def new_revision(record: dict[str, Any]) -> dict[str, Any]:
    """Invalidate acceptance on any edited evidence while retaining immutable history."""
    result = copy.deepcopy(record)
    result["parent_revision_id"] = record["revision_id"]
    result.pop("revision_id", None)
    result.pop("author_base_revision", None)
    result.pop("submission_id", None)
    result.update(status="inventory_pending", checks={}, reviews=[], handoff=None)
    return result
