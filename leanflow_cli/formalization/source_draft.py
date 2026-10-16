"""Retain unaccepted author previews while preserving source and formalization acceptance gates."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identifier,
    identity,
)
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.records import (
    current_selection_token,
    document_directory,
    document_lock,
    load_document,
    load_receipt,
    rehydrate_document,
)
from leanflow_cli.formalization.source_draft_artifacts import freeze_artifacts, read_regular

MAX_PREVIEW_RECORD_BYTES = 256 * 1024
_VERSION_POINTER = "formalization_preview_pointer"
_UNCERTAIN_CODES = {
    "unresolved_loader_effect",
    "inherited_tex_meanings",
    "unresolved_conditional",
    "unresolved_environment_structure",
    "unresolved_command",
    "unsupported_tex_effect",
}


def assert_unaccepted_draft(record: dict[str, Any]) -> None:
    """Refuse a preview which would shadow an already accepted selected translation."""
    receipt = load_receipt(record)
    if (receipt or {}).get("status") in {"published", "accepted_staged"} or record.get(
        "status"
    ) in {"published", "accepted_staged"}:
        raise ValueError(
            "Source drafts require an unaccepted source inventory revision; select a fresh source revision rather than shadow an accepted translation"
        )


def _authenticate_preview(preview: Any, run_id: str) -> dict[str, Any]:
    """Authenticate immutable preview identity without imposing today's selected revision."""
    if (
        not isinstance(preview, dict)
        or preview.get("preview_id")
        != "draft-preview-" + identity({k: v for k, v in preview.items() if k != "preview_id"})
        or any(
            preview.get(key) is not False
            for key in ("accepted", "publication_authority", "proof_authority")
        )
        or preview.get("status") not in {"unaccepted_source_uncertain", "unaccepted_formalization"}
        or preview.get("run_id") != run_id
    ):
        raise ValueError("Unaccepted draft preview metadata changed")
    return preview


def _read_run_preview(
    directory: Path, run_id: str, preview_id: str | None = None
) -> dict[str, Any] | None:
    """Follow a typed latest pointer or read an exact immutable historical delivery version."""
    if preview_id is not None:
        version = contained(directory, f"draft-previews/versions/{identifier(preview_id)}.json")
        if version.exists():
            preview = _authenticate_preview(
                json.loads(read_regular(version, MAX_PREVIEW_RECORD_BYTES)), run_id
            )
            if (
                preview["preview_id"] != preview_id
                or preview["status"] != "unaccepted_formalization"
            ):
                raise ValueError("Draft preview version differs from its immutable identity")
            return preview
    target = contained(directory, f"draft-previews/{identifier(run_id)}.json")
    if not target.exists():
        return None
    value = json.loads(read_regular(target, MAX_PREVIEW_RECORD_BYTES))
    if isinstance(value, dict) and value.get("kind") == _VERSION_POINTER:
        if (
            set(value) != {"schema_version", "kind", "run_id", "preview_id"}
            or value.get("schema_version") != 1
            or value.get("run_id") != run_id
        ):
            raise ValueError("Draft preview run pointer changed")
        key = identifier(value["preview_id"])
        if preview_id is not None and preview_id != key:
            return None
        version = contained(directory, f"draft-previews/versions/{key}.json")
        preview = _authenticate_preview(
            json.loads(read_regular(version, MAX_PREVIEW_RECORD_BYTES)), run_id
        )
        if preview["preview_id"] != key or preview["status"] != "unaccepted_formalization":
            raise ValueError("Draft preview run pointer differs from its immutable version")
        return preview
    preview = _authenticate_preview(value, run_id)
    return preview if preview_id is None or preview_id == preview["preview_id"] else None


def _archive_preview(directory: Path, preview: dict[str, Any]) -> None:
    """Preserve one content-addressed delivery record without overwriting existing evidence."""
    version = contained(
        directory, f"draft-previews/versions/{identifier(preview['preview_id'])}.json"
    )
    if version.exists():
        if json.loads(read_regular(version, MAX_PREVIEW_RECORD_BYTES)) != preview:
            raise ValueError("Immutable draft preview version changed")
    else:
        durable_json(version, preview)


def _frozen_spans(inventory: dict[str, Any], row: dict[str, Any]) -> bool:
    """Require authentic half-open source coordinates rather than a generic blocker waiver."""
    spans = row.get("spans", [row["span"]] if "span" in row else [])
    if not isinstance(spans, list) or not spans:
        return False
    for span in spans:
        if not isinstance(span, dict):
            return False
        file = inventory.get("files", {}).get(span.get("file"), {})
        start, end = span.get("byte_start"), span.get("byte_end")
        if (
            not file
            or file.get("encoding") == "binary"
            or span.get("file_hash") != file.get("sha256")
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= file.get("size", -1)
        ):
            return False
    return True


def draft_policy(inventory: dict[str, Any], *, explicit: bool = False) -> dict[str, Any]:
    """Admit drafting only with an explicit frozen companion and known uncertainty diagnostics."""
    companion = inventory.get("companion_pdf") or {}
    file = companion.get("file", "")
    metadata = inventory.get("files", {}).get(file, {})
    paired = (
        inventory.get("source_kind") == "latex"
        and companion.get("selection") == "explicit"
        and isinstance(file, str)
        and file.lower().endswith(".pdf")
        and metadata.get("encoding") == "binary"
        and bool(companion.get("sha256"))
        and companion["sha256"] == metadata.get("sha256")
    )
    if explicit and not paired:
        raise ValueError("--source-draft-only requires an explicitly paired frozen PDF")
    from leanflow_cli.formalization.source_reconciliation import lexical_admission

    hard = lexical_admission(inventory) if not explicit else []
    strict: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for row in inventory.get("diagnostics", []):
        if not row.get("blocking"):
            continue
        support = (
            row.get("code") == "missing_support_file"
            and row.get("support_command") in {"includegraphics", "bibliography", "addbibresource"}
            and isinstance(row.get("support_target"), str)
            and bool(row["support_target"].strip())
            and (explicit or row in hard)
        )
        structured = bool(row.get("id")) and _frozen_spans(inventory, row)
        uncertainty = explicit and structured and row.get("code") in _UNCERTAIN_CODES
        (deferred if paired and (support or uncertainty) else strict).append(row)
    return {
        "enabled": bool(paired),
        "draft_only": bool(explicit or deferred),
        "strict": strict,
        "deferred": deferred,
    }


def freeze_preview(
    root: Path,
    record: dict[str, Any],
    snapshot: Path,
    *,
    layout: dict[str, Any],
    run_id: str,
    selection_token: str,
    model_report: str,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    recovery: bool = False,
    kind: str = "source_uncertain",
    delivery: dict[str, Any] | None = None,
    reports: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain one immutable completed-author preview without selecting a revision or receipt."""
    from leanflow_cli.formalization.publication import publication_guard

    identifier(run_id)
    assert_unaccepted_draft(record)
    if (
        kind not in {"source_uncertain", "formalization"}
        or ((kind == "formalization") != (delivery is not None))
        or (kind == "source_uncertain" and reports is not None)
    ):
        raise ValueError("Draft preview requires a controller-assigned kind and delivery census")

    def source_current() -> None:
        """Bind recovery to frozen evidence; editable agent copies grant no fidelity claim."""
        for relative, key in record["source_files"].items():
            size = complete_inventory(record)["files"][relative]["size"]
            original = (
                contained(Path(record["_directory"]), f"blobs/{key}")
                if recovery
                else contained(root, relative)
            )
            if digest(read_regular_bytes(original, size)) != key:
                raise ValueError(
                    "Frozen or original source changed before draft retention: " + relative
                )

    with (
        publication_guard(root, deadline=deadline, cancelled=cancelled),
        document_lock(
            root, record["document_id"], deadline=deadline, cancelled=cancelled
        ) as directory,
    ):
        if not recovery:
            selected = load_document(root, record["document_id"])
            if (
                selected["revision_id"] != record["revision_id"]
                or current_selection_token(root, record["document_id"]) != selection_token
            ):
                raise ValueError("Source selection changed before draft retention")
            assert_unaccepted_draft(selected)
        else:
            record = load_document(root, record["document_id"], record["revision_id"])
        source_current()
        rehydrate_document(record, directory)
        artifacts, report_id = freeze_artifacts(
            snapshot,
            contained(directory, "blobs"),
            layout,
            run_id,
            model_report,
            formalization=kind == "formalization",
        )
        summary = None
        if delivery is not None:
            from leanflow_cli.formalization.draft_delivery import freeze_delivery_artifacts

            extra, summary = freeze_delivery_artifacts(
                root,
                snapshot,
                contained(directory, "blobs"),
                run_id=run_id,
                delivery=delivery,
                reports=reports,
            )
            artifacts.extend(extra)
        source_current()
        if delivery is not None:
            from leanflow_cli.formalization.draft_delivery import assert_delivery_current

            assert_delivery_current(snapshot, delivery)
        preview = {
            "schema_version": 1,
            "status": "unaccepted_formalization" if summary else "unaccepted_source_uncertain",
            "accepted": False,
            "publication_authority": False,
            "proof_authority": False,
            "document_id": record["document_id"],
            "base_revision_id": record["revision_id"],
            "source_bundle": record["source_bundle"],
            "selection_token": selection_token,
            "preparation_id": record.get("preparation_id", ""),
            "run_id": run_id,
            "source_diagnostics": [
                row["id"]
                for row in complete_inventory(record)["diagnostics"]
                if row.get("blocking")
            ],
            "artifacts": artifacts,
            "model_report_artifact_id": report_id,
        }
        if summary is not None:
            preview.update(
                reason="review_ambiguities",
                delivery_status="complete" if summary["complete"] else "incomplete",
                delivery=summary,
            )
        preview["preview_id"] = "draft-preview-" + identity(preview)
        if (
            len((json.dumps(preview, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
            > MAX_PREVIEW_RECORD_BYTES
        ):
            raise ValueError("Draft preview metadata exceeds its bounded size")
        target = contained(directory, f"draft-previews/{run_id}.json")
        if kind == "formalization":
            previous = _read_run_preview(directory, run_id)
            if previous is not None:
                if previous["status"] != "unaccepted_formalization" or any(
                    previous.get(key) != preview[key] for key in ("document_id", "source_bundle")
                ):
                    raise ValueError(
                        "Draft campaign cannot change its source binding or preview kind"
                    )
                _archive_preview(directory, previous)
            _archive_preview(directory, preview)
            durable_json(
                target,
                {
                    "schema_version": 1,
                    "kind": _VERSION_POINTER,
                    "run_id": run_id,
                    "preview_id": preview["preview_id"],
                },
            )
        elif target.exists():
            if json.loads(read_regular(target, MAX_PREVIEW_RECORD_BYTES)) != preview:
                raise ValueError("Completed draft preview is immutable; start a new campaign")
        else:
            durable_json(target, preview)
        if not recovery or preview_is_current(root, record, preview):
            durable_json(
                contained(directory, f"draft-previews/selected-{record['revision_id']}.json"),
                {"run_id": run_id, "preview_id": preview["preview_id"]},
            )
        return preview


def load_run_preview(
    root: Path, record: dict[str, Any], run_id: str, *, preview_id: str | None = None
) -> dict[str, Any] | None:
    """Read one immutable campaign's preview without adopting another run's selected pointer."""
    directory = document_directory(root, record["document_id"])
    preview = _read_run_preview(directory, identifier(run_id), preview_id)
    if preview is None:
        return None
    if (
        preview.get("document_id") != record["document_id"]
        or preview.get("base_revision_id") != record["revision_id"]
        or preview.get("source_bundle") != record["source_bundle"]
        or preview.get("preparation_id") != record.get("preparation_id", "")
    ):
        raise ValueError("Draft preview does not belong to this source revision and campaign")
    if preview["status"] == "unaccepted_formalization" and (
        preview.get("reason") != "review_ambiguities"
        or preview.get("delivery_status") not in {"complete", "incomplete"}
        or not isinstance(preview.get("delivery"), dict)
    ):
        raise ValueError("Unaccepted formalization delivery metadata changed")
    return preview


def preview_is_current(root: Path, record: dict[str, Any], preview: dict[str, Any]) -> bool:
    """Report attachment separately from immutable historical preview identity."""
    try:
        selected = load_document(root, record["document_id"])
        if not (
            selected["revision_id"] == preview["base_revision_id"]
            and selected["source_bundle"] == preview["source_bundle"]
            and preview["selection_token"] == current_selection_token(root, record["document_id"])
        ):
            return False
        for relative, key in record["source_files"].items():
            size = complete_inventory(record)["files"][relative]["size"]
            if digest(read_regular_bytes(contained(root, relative), size)) != key:
                return False
    except (OSError, ValueError, KeyError):
        return False
    return True


def load_preview(
    root: Path, record: dict[str, Any], *, verify_current: bool = True
) -> dict[str, Any] | None:
    """Authenticate saved preview bytes, optionally requiring their present source attachment."""
    if (load_receipt(record) or {}).get("status") in {"published", "accepted_staged"} or record.get(
        "status"
    ) in {"published", "accepted_staged"}:
        return None
    directory = document_directory(root, record["document_id"])
    pointer = contained(directory, f"draft-previews/selected-{record['revision_id']}.json")
    if not pointer.exists():
        return None
    value = json.loads(read_regular(pointer, MAX_PREVIEW_RECORD_BYTES))
    preview = load_run_preview(
        root, record, identifier(value["run_id"]), preview_id=identifier(value["preview_id"])
    )
    if preview is None or preview["preview_id"] != value.get("preview_id"):
        raise ValueError("Selected draft preview is missing or changed")
    return preview if not verify_current or preview_is_current(root, record, preview) else None
