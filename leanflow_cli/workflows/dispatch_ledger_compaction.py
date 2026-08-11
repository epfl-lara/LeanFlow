"""Compact terminal dispatch rows without weakening durable recovery."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import research_route_context
from leanflow_cli.workflows.dispatch_models import TERMINAL_STATES

ROUTE_CONTEXT_INPUT_KEY = "recent_route_context"
ROUTE_CONTEXT_SHA256_INPUT_KEY = "recent_route_context_sha256"
OBJECTIVE_SHA256_INPUT_KEY = "objective_sha256"
DISPATCH_ARCHIVE_KEY = "dispatch_payload_archive"
DISPATCH_ARCHIVE_VERSION = 1
DISPATCH_ARCHIVE_DIRNAME = "dispatch-archives"

_SECOND_STAGE_MIN_SAVINGS_BYTES = 1_024
_SUMMARY_TEXT_CAP = 1_200
_PATH_TEXT_CAP = 2_000
_MAX_ARCHIVE_COMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_STABLE_LIFECYCLE_FIELDS = (
    "state",
    "consumed",
    "created_at",
    "started_at",
    "finished_at",
    "launch_nonce",
    "launch_started_at",
    "launch_attempt",
    "process_id",
    "process_group_id",
    "process_session_id",
    "process_token_sha256",
    "process_released_at",
    "process_release_reason",
    "process_release_evidence_sha256",
    "process_release_observed_started_at",
    "process_release_report_key",
    "process_release_reported_at",
)


class DispatchLedgerArchiveError(RuntimeError):
    """Report a missing, corrupt, or identity-mismatched dispatch archive."""


def _payload_sha256(value: Any) -> str:
    """Return a stable hash for one JSON-like context payload."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _objective_sha256(value: str) -> str:
    """Return the digest of the exact worker objective before compaction."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    """Return the exact canonical JSON encoding used for archive integrity."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_text(value: Any, cap: int = _SUMMARY_TEXT_CAP) -> str:
    """Return one single-line bounded diagnostic string."""
    return " ".join(str(value or "").split())[: max(0, int(cap))]


def _safe_archive_stem(job_id: str, payload_sha256: str) -> str:
    """Return a short collision-resistant filename for one immutable row."""
    job_sha256 = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return f"{job_sha256[:24]}-{payload_sha256[:32]}.json.gz"


def _archive_relative_path(job_id: str, payload_sha256: str) -> Path:
    """Return the checkpointed path for one immutable dispatch payload."""
    return Path(DISPATCH_ARCHIVE_DIRNAME) / _safe_archive_stem(job_id, payload_sha256)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace one archive and fsync it before publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_payload(record: Mapping[str, Any]) -> tuple[bytes, str]:
    """Return the lossless row envelope and its canonical payload digest."""
    payload = {
        "version": DISPATCH_ARCHIVE_VERSION,
        "record": dict(record),
    }
    encoded = _canonical_json_bytes(payload)
    return encoded, hashlib.sha256(encoded).hexdigest()


def _result_projection(result: Mapping[str, Any], *, result_sha256: str) -> dict[str, Any]:
    """Return a bounded audit projection whose exact authority is the archive."""
    projection: dict[str, Any] = {
        "archive_result_sha256": result_sha256,
    }
    status = _bounded_text(result.get("status", ""), 160)
    if status:
        projection["status"] = status

    raw_paths = result.get("artifact_paths")
    if isinstance(raw_paths, str):
        paths: Sequence[Any] = (raw_paths,)
    elif isinstance(raw_paths, Sequence) and not isinstance(raw_paths, (str, bytes, bytearray)):
        paths = raw_paths
    else:
        paths = ()
    bounded_paths = [
        str(path or "")[:_PATH_TEXT_CAP] for path in paths[:100] if str(path or "").strip()
    ]
    if bounded_paths:
        projection["artifact_paths"] = bounded_paths

    raw_plan_delta = result.get("plan_delta")
    if isinstance(raw_plan_delta, Sequence) and not isinstance(
        raw_plan_delta, (str, bytes, bytearray)
    ):
        projection["plan_delta_count"] = len(raw_plan_delta)
        projection["plan_delta_sha256"] = _payload_sha256(raw_plan_delta)

    raw_deliverable = result.get("deliverable")
    if isinstance(raw_deliverable, Mapping):
        deliverable = dict(raw_deliverable)
        compact_deliverable: dict[str, Any] = {
            "archive_deliverable_sha256": _payload_sha256(deliverable),
        }
        summary = _bounded_text(deliverable.get("summary", ""))
        if summary:
            compact_deliverable["summary"] = summary
        for key, value in deliverable.items():
            normalized_key = str(key)
            if normalized_key.endswith("_sha256"):
                compact_deliverable[normalized_key] = _bounded_text(value, 128)
            elif normalized_key in {
                "status",
                "reported_status",
                "classification",
                "checked_helper_status",
                "checked_replacement_status",
                "parent_recheck_required",
                "verification_caveat",
                "verification_note",
            }:
                if isinstance(value, bool):
                    compact_deliverable[normalized_key] = value
                else:
                    compact_deliverable[normalized_key] = _bounded_text(value)

        raw_helpers = deliverable.get("checked_helpers")
        if isinstance(raw_helpers, Sequence) and not isinstance(
            raw_helpers, (str, bytes, bytearray)
        ):
            helper_projections: list[dict[str, Any]] = []
            for raw_helper in raw_helpers:
                if not isinstance(raw_helper, Mapping):
                    continue
                helper = dict(raw_helper)
                declaration = helper.get("declaration")
                declaration_sha256 = _bounded_text(helper.get("declaration_sha256", ""), 128)
                if isinstance(declaration, str) and declaration:
                    exact_sha256 = hashlib.sha256(declaration.encode("utf-8")).hexdigest()
                    if declaration_sha256 and declaration_sha256 != exact_sha256:
                        # A contradictory model-authored hash is not authority.
                        declaration_sha256 = exact_sha256
                    elif not declaration_sha256:
                        declaration_sha256 = exact_sha256
                compact_helper = {
                    "anchor_target_symbol": _bounded_text(
                        helper.get("anchor_target_symbol", ""), 500
                    ),
                    "active_file": str(helper.get("active_file", "") or "")[:_PATH_TEXT_CAP],
                    "declaration_sha256": declaration_sha256,
                    "parent_recheck_required": bool(helper.get("parent_recheck_required", False)),
                    "archive_declaration_required": True,
                }
                worker_check = helper.get("worker_check")
                if isinstance(worker_check, Mapping):
                    compact_helper["worker_check"] = dict(worker_check)
                helper_projections.append(compact_helper)
            if helper_projections:
                compact_deliverable["checked_helpers"] = helper_projections
        projection["deliverable"] = compact_deliverable

    for key, value in result.items():
        normalized_key = str(key)
        if normalized_key.endswith("_sha256") and normalized_key not in projection:
            projection[normalized_key] = _bounded_text(value, 128)
    return projection


def _terminal_record_is_archive_eligible(record: Mapping[str, Any]) -> bool:
    """Return whether one row has crossed every mutable lifecycle boundary."""
    if (
        str(record.get("state", "") or "") != "done"
        or record.get("consumed") is not True
        or not str(record.get("finished_at", "") or "").strip()
        or DISPATCH_ARCHIVE_KEY in record
    ):
        return False
    spec = record.get("spec")
    result = record.get("result")
    if not isinstance(spec, Mapping) or not isinstance(result, Mapping):
        return False
    job_id = str(spec.get("job_id", "") or "").strip()
    if not job_id:
        return False

    try:
        process_id = int(record.get("process_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return True
    # Modern process-isolated jobs can reach ``done`` only after the parent
    # reaps or structurally proves exit. Legacy PID-only rows require an
    # explicit durable capacity-release verdict instead.
    modern_identity = bool(
        str(record.get("launch_nonce", "") or "").strip()
        and str(record.get("process_token_sha256", "") or "").strip()
    )
    released = bool(str(record.get("process_released_at", "") or "").strip())
    if not (modern_identity or released):
        return False
    report_key = str(record.get("process_release_report_key", "") or "").strip()
    if report_key and not str(record.get("process_release_reported_at", "") or "").strip():
        return False
    return True


def compact_consumed_dispatch_records(
    ledger: list[dict[str, Any]],
    *,
    state_root: Path,
) -> int:
    """Archive and shrink fully consumed terminal rows in place.

    The immutable gzip archive lives outside ``dispatch-jobs`` so normal
    source checkpoints retain it. Only exact ``done``/consumed rows whose
    worker lifecycle is over are eligible. The compact row keeps stable spec,
    route, digest, lifecycle, and checked-helper authority; exact prose and
    Lean source are restored transparently from the integrity-checked archive.
    """
    archived = 0
    normalized_root = Path(state_root).expanduser().resolve()
    for record in ledger:
        if not _terminal_record_is_archive_eligible(record):
            continue
        spec = record.get("spec")
        result = record.get("result")
        assert isinstance(spec, Mapping)
        assert isinstance(result, Mapping)
        job_id = str(spec.get("job_id", "") or "").strip()
        encoded, payload_sha256 = _archive_payload(record)
        if len(encoded) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            continue
        result_sha256 = _payload_sha256(result)
        compact_result = _result_projection(result, result_sha256=result_sha256)
        projected = dict(record)
        projected["result"] = compact_result
        # Count only rows whose summary representation actually gets smaller.
        # Tiny rows remain lossless inline and avoid needless archive I/O.
        projected_bytes = _canonical_json_bytes(projected)
        original_bytes = _canonical_json_bytes(record)
        if len(original_bytes) - len(projected_bytes) < _SECOND_STAGE_MIN_SAVINGS_BYTES:
            continue

        compressed = gzip.compress(encoded, compresslevel=6, mtime=0)
        if len(compressed) > _MAX_ARCHIVE_COMPRESSED_BYTES:
            continue
        compressed_sha256 = hashlib.sha256(compressed).hexdigest()
        relative_path = _archive_relative_path(job_id, payload_sha256)
        archive_path = normalized_root / relative_path
        _atomic_write_bytes(archive_path, compressed)
        # Verify the bytes before publishing their only summary reference.
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != compressed_sha256:
            raise DispatchLedgerArchiveError(
                f"dispatch archive write verification failed for {job_id!r}"
            )
        projected[DISPATCH_ARCHIVE_KEY] = {
            "version": DISPATCH_ARCHIVE_VERSION,
            "path": relative_path.as_posix(),
            "payload_sha256": payload_sha256,
            "compressed_sha256": compressed_sha256,
            "result_sha256": result_sha256,
            "uncompressed_bytes": len(encoded),
            "compressed_bytes": len(compressed),
        }
        record.clear()
        record.update(projected)
        archived += 1
    return archived


def hydrate_dispatch_record(
    raw: Mapping[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any]:
    """Restore one compact row from its authenticated checkpointed archive.

    Archive corruption fails loudly. Returning the bounded projection as if it
    were full mathematical evidence would weaken dedupe, helper recheck, and
    resume correctness.
    """
    record = dict(raw)
    metadata_raw = record.get(DISPATCH_ARCHIVE_KEY)
    if metadata_raw is None:
        return record
    if not isinstance(metadata_raw, Mapping):
        raise DispatchLedgerArchiveError("dispatch archive metadata is malformed")
    metadata = dict(metadata_raw)
    try:
        version = int(metadata.get("version", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise DispatchLedgerArchiveError("dispatch archive version is malformed") from exc
    spec = record.get("spec")
    job_id = str(spec.get("job_id", "") or "").strip() if isinstance(spec, Mapping) else ""
    payload_sha256 = str(metadata.get("payload_sha256", "") or "").strip()
    compressed_sha256 = str(metadata.get("compressed_sha256", "") or "").strip()
    relative = str(metadata.get("path", "") or "").strip()
    if (
        version != DISPATCH_ARCHIVE_VERSION
        or not job_id
        or len(payload_sha256) != 64
        or len(compressed_sha256) != 64
        or relative != _archive_relative_path(job_id, payload_sha256).as_posix()
    ):
        raise DispatchLedgerArchiveError(f"dispatch archive identity is invalid for {job_id!r}")

    normalized_root = Path(state_root).expanduser().resolve()
    archive_path = (normalized_root / relative).resolve()
    archive_dir = (normalized_root / DISPATCH_ARCHIVE_DIRNAME).resolve()
    if archive_path.parent != archive_dir or not archive_path.is_file():
        raise DispatchLedgerArchiveError(f"dispatch archive is missing for {job_id!r}")
    try:
        compressed_size = archive_path.stat().st_size
    except OSError as exc:
        raise DispatchLedgerArchiveError(f"dispatch archive is unreadable for {job_id!r}") from exc
    if compressed_size > _MAX_ARCHIVE_COMPRESSED_BYTES:
        raise DispatchLedgerArchiveError(f"dispatch archive is oversized for {job_id!r}")
    compressed = archive_path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != compressed_sha256:
        raise DispatchLedgerArchiveError(f"dispatch archive digest mismatch for {job_id!r}")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
            encoded = handle.read(_MAX_ARCHIVE_UNCOMPRESSED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise DispatchLedgerArchiveError(
            f"dispatch archive gzip is corrupt for {job_id!r}"
        ) from exc
    if len(encoded) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise DispatchLedgerArchiveError(f"dispatch archive payload is oversized for {job_id!r}")
    if hashlib.sha256(encoded).hexdigest() != payload_sha256:
        raise DispatchLedgerArchiveError(f"dispatch archive payload mismatch for {job_id!r}")
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DispatchLedgerArchiveError(
            f"dispatch archive JSON is corrupt for {job_id!r}"
        ) from exc
    archived_raw = payload.get("record") if isinstance(payload, Mapping) else None
    if not isinstance(archived_raw, Mapping):
        raise DispatchLedgerArchiveError(f"dispatch archive record is missing for {job_id!r}")
    archived = dict(archived_raw)
    archived_spec = archived.get("spec")
    archived_result = archived.get("result")
    if (
        not isinstance(archived_spec, Mapping)
        or str(archived_spec.get("job_id", "") or "").strip() != job_id
        or not isinstance(archived_result, Mapping)
        or _payload_sha256(archived_result) != str(metadata.get("result_sha256", "") or "").strip()
    ):
        raise DispatchLedgerArchiveError(f"dispatch archive evidence mismatch for {job_id!r}")
    for key in _STABLE_LIFECYCLE_FIELDS:
        if archived.get(key) != record.get(key):
            raise DispatchLedgerArchiveError(
                f"dispatch archive lifecycle mismatch for {job_id!r}: {key}"
            )
    restored = dict(record)
    restored["spec"] = dict(archived_spec)
    restored["result"] = dict(archived_result)
    return restored


def hydrate_dispatch_ledger(
    raw_ledger: Sequence[Any],
    *,
    state_root: Path,
) -> list[dict[str, Any]]:
    """Return exact mappings for every well-shaped ledger row."""
    return [
        hydrate_dispatch_record(raw, state_root=state_root)
        for raw in raw_ledger
        if isinstance(raw, Mapping)
    ]


def compact_terminal_dispatch_records(ledger: list[dict[str, Any]]) -> int:
    """Remove copied route context from terminal records in place.

    Preserve the stable route-defining objective plus a digest of the exact
    launch objective. Deliverables, checked code, result-integrity hashes, and
    lifecycle evidence remain lossless. Parent-owned prompt context copied into
    job inputs or rendered into the objective is removed after termination.
    Live jobs remain byte-for-byte unchanged for recovery.
    """
    removed = 0
    for record in ledger:
        if str(record.get("state", "") or "") not in TERMINAL_STATES:
            continue

        spec = record.get("spec")
        if isinstance(spec, dict):
            inputs = spec.get("inputs")
            if isinstance(inputs, dict) and ROUTE_CONTEXT_INPUT_KEY in inputs:
                context = inputs.pop(ROUTE_CONTEXT_INPUT_KEY)
                # The context is about to become unrecoverable. Derive its
                # authority from the exact removed payload instead of trusting
                # a stale sibling or model-authored embedded digest.
                inputs[ROUTE_CONTEXT_SHA256_INPUT_KEY] = _payload_sha256(context)
                removed += 1
            objective = spec.get("objective")
            if isinstance(objective, str):
                semantic_objective = research_route_context.semantic_worker_objective(objective)
                if semantic_objective != objective and isinstance(inputs, dict):
                    inputs[OBJECTIVE_SHA256_INPUT_KEY] = _objective_sha256(objective)
                    spec["objective"] = semantic_objective
                    removed += 1
    return removed
