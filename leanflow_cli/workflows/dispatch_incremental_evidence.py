"""Persist bounded worker-checked helper evidence across dispatch interruption."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.native.runtime_cleanup import NativeTerminationSignal
from leanflow_cli.workflows import dispatch_ledger_compaction
from leanflow_cli.workflows.dispatch_models import JobSpec

JOURNAL_VERSION = 1
MAX_CHECKED_HELPERS = 8
MAX_DECLARATION_BYTES = 128 * 1024
MAX_JOURNAL_BYTES = 512 * 1024
CHECKED_HELPER_STATUS = "worker_checked_parent_recheck_required"
EVIDENCE_AUTHORITY = "worker_observation_only"


@contextlib.contextmanager
def _defer_graceful_termination() -> Iterator[None]:
    """Defer SIGHUP/SIGTERM until an atomic evidence checkpoint is renamed."""
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    sig_block = getattr(signal, "SIG_BLOCK", None)
    sig_setmask = getattr(signal, "SIG_SETMASK", None)
    termination_signals = {
        value
        for name in ("SIGHUP", "SIGTERM")
        if isinstance((value := getattr(signal, name, None)), int)
    }
    if (
        not callable(pthread_sigmask)
        or sig_block is None
        or sig_setmask is None
        or not termination_signals
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous_mask = pthread_sigmask(sig_block, termination_signals)
    try:
        yield
    finally:
        # A pending graceful signal is delivered here, after fsync+rename. Its
        # installed native handler still raises the normal termination boundary.
        pthread_sigmask(sig_setmask, previous_mask)


def _now_iso() -> str:
    """Return one stable UTC timestamp for durable evidence metadata."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _payload_sha256(value: Any) -> str:
    """Return a deterministic digest for one JSON-compatible value."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_spec_binding(spec: JobSpec) -> dict[str, Any]:
    """Return the exact spec in its terminal-compaction-stable representation."""
    record: dict[str, Any] = {"state": "killed", "spec": spec.to_mapping()}
    dispatch_ledger_compaction.compact_terminal_dispatch_records([record])
    raw_spec = record.get("spec")
    return dict(raw_spec) if isinstance(raw_spec, Mapping) else {}


def _normalized_symbol(value: Any) -> str:
    """Return a comparison form for one Lean declaration name."""
    return str(value or "").strip().removeprefix("_root_.")


def _normalized_path(value: Any) -> str:
    """Return an absolute comparison path using the workflow project root."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        project_root = str(os.environ.get("LEANFLOW_PROJECT_ROOT", "") or "").strip()
        if project_root:
            path = Path(project_root).expanduser() / path
    return str(path.resolve(strict=False))


def _canonical_helper(
    raw: Mapping[str, Any],
    *,
    expected_target_symbol: str,
    expected_active_file: str,
) -> dict[str, Any] | None:
    """Return one exact worker-check artifact or reject it fail closed."""
    helper = dict(raw)
    declaration = helper.get("declaration")
    if not isinstance(declaration, str) or not declaration.strip():
        return None
    if len(declaration.encode("utf-8")) > MAX_DECLARATION_BYTES:
        return None
    declaration_sha256 = str(helper.get("declaration_sha256", "") or "").strip()
    if declaration_sha256 != hashlib.sha256(declaration.encode("utf-8")).hexdigest():
        return None
    anchor_target_symbol = str(helper.get("anchor_target_symbol", "") or "").strip()
    active_file = str(helper.get("active_file", "") or "").strip()
    if (
        not anchor_target_symbol
        or not active_file
        or _normalized_symbol(anchor_target_symbol) != _normalized_symbol(expected_target_symbol)
        or _normalized_path(active_file) != _normalized_path(expected_active_file)
        or helper.get("parent_recheck_required") is not True
    ):
        return None
    raw_check = helper.get("worker_check")
    if not isinstance(raw_check, Mapping):
        return None
    worker_check = dict(raw_check)
    raw_declarations = worker_check.get("replacement_declarations")
    if not isinstance(raw_declarations, Sequence) or isinstance(
        raw_declarations, (str, bytes, bytearray)
    ):
        return None
    replacement_declarations = [
        str(value).strip() for value in raw_declarations if str(value).strip()
    ]
    if (
        str(worker_check.get("tool", "") or "") != "lean_incremental_check"
        or str(worker_check.get("action", "") or "") != "check_helper"
        or worker_check.get("valid_without_sorry") is not True
        or worker_check.get("has_errors") is not False
        or worker_check.get("has_sorry") is not False
        or str(worker_check.get("verification_scope", "") or "") != "helper_candidate"
        or worker_check.get("replacement_matches_target") is not False
        or not replacement_declarations
    ):
        return None
    normalized_check: dict[str, Any] = {
        "tool": "lean_incremental_check",
        "action": "check_helper",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "verification_scope": "helper_candidate",
        "replacement_matches_target": False,
        "replacement_declarations": replacement_declarations[:20],
    }
    elapsed_s = worker_check.get("elapsed_s")
    if isinstance(elapsed_s, (int, float)) and not isinstance(elapsed_s, bool):
        normalized_check["elapsed_s"] = max(0.0, float(elapsed_s))
    return {
        "anchor_target_symbol": anchor_target_symbol,
        "active_file": active_file,
        "declaration": declaration,
        "declaration_sha256": declaration_sha256,
        "worker_check": normalized_check,
        "parent_recheck_required": True,
    }


def _bounded_canonical_helpers(
    helpers: Sequence[Mapping[str, Any]],
    *,
    spec: JobSpec,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest distinct canonical helpers that fit the journal cap."""
    inputs = dict(spec.inputs or {})
    target_symbol = str(inputs.get("target_symbol", "") or "").strip()
    active_file = str(inputs.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return [], len(helpers)
    selected_reversed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    total_bytes = 0
    dropped = 0
    for raw in reversed(list(helpers)):
        if not isinstance(raw, Mapping):
            dropped += 1
            continue
        helper = _canonical_helper(
            raw,
            expected_target_symbol=target_symbol,
            expected_active_file=active_file,
        )
        if helper is None:
            dropped += 1
            continue
        identity = (
            _normalized_symbol(helper["anchor_target_symbol"]),
            _normalized_path(helper["active_file"]),
            str(helper["declaration_sha256"]),
        )
        if identity in seen:
            dropped += 1
            continue
        helper_bytes = len(json.dumps(helper, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if (
            len(selected_reversed) >= MAX_CHECKED_HELPERS
            or total_bytes + helper_bytes > MAX_JOURNAL_BYTES
        ):
            dropped += 1
            continue
        seen.add(identity)
        selected_reversed.append(helper)
        total_bytes += helper_bytes
    selected_reversed.reverse()
    return selected_reversed, dropped


def publish_checked_helpers(
    path: Path,
    *,
    launch_nonce: str,
    spec: JobSpec,
    helpers: Sequence[Mapping[str, Any]],
) -> bool:
    """Atomically checkpoint canonical worker evidence after each accepted check."""
    nonce = str(launch_nonce or "").strip()
    if not nonce:
        return False

    def commit() -> bool:
        """Build and atomically replace one exact evidence checkpoint."""
        canonical, dropped = _bounded_canonical_helpers(helpers, spec=spec)
        if not canonical:
            return False
        payload = {
            "version": JOURNAL_VERSION,
            "launch_nonce": nonce,
            "job_id": spec.job_id,
            "job_spec_binding_sha256": _payload_sha256(_stable_spec_binding(spec)),
            "checked_helper_status": CHECKED_HELPER_STATUS,
            "evidence_authority": EVIDENCE_AUTHORITY,
            "parent_recheck_required": True,
            "checked_helpers": canonical,
            "dropped_helper_count": max(0, dropped),
            "updated_at": _now_iso(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, payload, sort_keys=True)
        return True

    try:
        with _defer_graceful_termination():
            return commit()
    except NativeTerminationSignal as termination:
        # The installed native handler has already changed SIGHUP/SIGTERM to
        # SIG_IGN before raising. A process-directed signal delivered through
        # another thread can still interrupt despite this thread's POSIX mask;
        # retry once under that handler-owned quiet period, then preserve the
        # exact original cancellation boundary.
        try:
            commit()
        except BaseException:
            raise termination
        raise


def load_checked_helpers(
    path: Path,
    *,
    launch_nonce: str,
    spec: JobSpec,
) -> list[dict[str, Any]]:
    """Load only nonce/spec-bound canonical evidence from one bounded journal."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_JOURNAL_BYTES + 32 * 1024:
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    if not isinstance(raw, Mapping):
        return []
    payload = dict(raw)
    if (
        payload.get("version") != JOURNAL_VERSION
        or str(payload.get("launch_nonce", "") or "") != str(launch_nonce or "").strip()
        or str(payload.get("job_id", "") or "") != spec.job_id
        or str(payload.get("job_spec_binding_sha256", "") or "")
        != _payload_sha256(_stable_spec_binding(spec))
        or str(payload.get("checked_helper_status", "") or "") != CHECKED_HELPER_STATUS
        or str(payload.get("evidence_authority", "") or "") != EVIDENCE_AUTHORITY
        or payload.get("parent_recheck_required") is not True
    ):
        return []
    raw_helpers = payload.get("checked_helpers")
    if not isinstance(raw_helpers, list):
        return []
    canonical, _dropped = _bounded_canonical_helpers(
        [item for item in raw_helpers if isinstance(item, Mapping)],
        spec=spec,
    )
    # Any malformed row makes the journal unauthoritative instead of silently
    # accepting a valid subset from a tampered or partially written payload.
    if len(canonical) != len(raw_helpers):
        return []
    return canonical


def interrupted_result(
    *,
    spec: JobSpec,
    helpers: Sequence[Mapping[str, Any]],
    artifact_path: Path,
) -> dict[str, Any]:
    """Build a consumable partial finding without granting proof authority."""
    canonical, _dropped = _bounded_canonical_helpers(helpers, spec=spec)
    if not canonical:
        return {}
    return {
        "status": "done",
        "deliverable": {
            "status": "interrupted_with_worker_checked_helper_evidence",
            "summary": (
                f"Recovered {len(canonical)} exact helper declaration(s) checked inside the "
                "interrupted worker. The foreground parent must rerun Lean against current source "
                "before using any helper as proof evidence."
            ),
            "checked_helpers": canonical,
            "checked_helper_status": CHECKED_HELPER_STATUS,
            "evidence_authority": EVIDENCE_AUTHORITY,
            "parent_recheck_required": True,
        },
        "artifact_paths": [str(artifact_path)],
        "plan_delta": [],
        "api_calls": 0,
        "partial_worker_evidence": True,
    }
