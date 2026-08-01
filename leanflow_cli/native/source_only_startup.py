"""Build fail-closed source-only snapshots for unresolved prove startup."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_ONLY_PROOF_STATE_AUTHORITY = "source_only_unverified"
SOURCE_ONLY_DIAGNOSTICS = "not queried during source-only startup"
SOURCE_ONLY_GOALS = "not queried during source-only startup"
SOURCE_ONLY_BUILD_STATUS = "unverified source-only startup snapshot; Lean was not queried"
DEFERRED_VERIFICATION_BUILD_STATUS = (
    "startup exact verification exceeded its bounded preflight; foreground repair required"
)


@dataclass(frozen=True)
class SourceRevision:
    """Identify one stable read of an active Lean source file."""

    path: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int

    def to_mapping(self) -> dict[str, Any]:
        """Return the revision fields persisted with a source-only snapshot."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> SourceRevision | None:
        """Restore a persisted revision, rejecting incomplete or malformed fields."""
        data = dict(raw or {})
        try:
            revision = cls(
                path=str(data.get("path", "") or "").strip(),
                sha256=str(data.get("sha256", "") or "").strip(),
                size_bytes=int(data["size_bytes"]),
                device=int(data["device"]),
                inode=int(data["inode"]),
                mtime_ns=int(data["mtime_ns"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not revision.path or len(revision.sha256) != 64 or revision.size_bytes < 0:
            return None
        return revision


def _stat_identity(stat: Any) -> tuple[int, int, int, int]:
    """Return the file identity fields that must stay stable around a read."""
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def capture_source_revision(active_file: str) -> SourceRevision | None:
    """Capture one stable, readable source revision or return ``None`` on uncertainty."""
    if not str(active_file or "").strip():
        return None
    try:
        path = Path(active_file).expanduser().resolve(strict=True)
        if not path.is_file():
            return None
        before = path.stat()
        source = path.read_bytes()
        after = path.stat()
    except OSError:
        return None
    if _stat_identity(before) != _stat_identity(after) or len(source) != int(after.st_size):
        return None
    return SourceRevision(
        path=str(path),
        sha256=hashlib.sha256(source).hexdigest(),
        size_bytes=len(source),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        mtime_ns=int(after.st_mtime_ns),
    )


def source_revision_is_current(revision: SourceRevision) -> bool:
    """Return whether the active source still has the exact captured revision."""
    current = capture_source_revision(revision.path)
    return current == revision


def snapshot_source_revision_is_current(live_state: Mapping[str, Any] | None) -> bool:
    """Recheck the exact source revision carried by a source-only live state."""
    if not is_source_only_unverified(live_state):
        return True
    revision = SourceRevision.from_mapping(dict((live_state or {}).get("source_revision") or {}))
    return revision is not None and source_revision_is_current(revision)


def is_source_only_unverified(live_state: Mapping[str, Any] | None) -> bool:
    """Return whether a live state explicitly carries non-kernel source authority."""
    return str((live_state or {}).get("proof_state_authority", "") or "").strip() == (
        SOURCE_ONLY_PROOF_STATE_AUTHORITY
    )


def build_source_only_snapshot(
    base_state: Mapping[str, Any],
    *,
    workflow_kind: str,
    source_sorry_count: int,
    revision: SourceRevision,
    document_ambiguous: bool = False,
    frontier_ambiguous: bool = False,
) -> dict[str, Any]:
    """Return an explicitly unverified snapshot when deterministic eligibility holds.

    The caller owns queue construction and graph precedence. This leaf owns the
    safety boundary: only an unresolved file-scoped ``prove`` item whose selected
    queue reason includes ``contains sorry`` can receive source-only authority.
    """
    state = dict(base_state)
    current_item = dict(state.get("current_queue_item") or {})
    reasons = {
        str(reason or "").strip().casefold() for reason in current_item.get("reasons", []) or []
    }
    eligible = bool(
        str(workflow_kind or "").strip().casefold() == "prove"
        and str(state.get("declaration_scope", "") or "").strip() == "file"
        and str(state.get("active_file", "") or "").strip() == revision.path
        and isinstance(source_sorry_count, int)
        and source_sorry_count > 0
        and str(current_item.get("label", "") or "").strip()
        and "contains sorry" in reasons
        and not document_ambiguous
        and not frontier_ambiguous
    )
    if not eligible:
        return {}

    target_symbol = str(current_item.get("label", "") or "").strip()
    active_file_label = str(state.get("active_file_label", "") or revision.path)
    queue_summary = str(state.get("declaration_queue_summary", "") or "[none]")
    state.update(
        {
            "proof_state_authority": SOURCE_ONLY_PROOF_STATE_AUTHORITY,
            "used_source_only_snapshot": True,
            "source_revision": revision.to_mapping(),
            "source_revision_sha256": revision.sha256,
            "target_symbol": target_symbol,
            "diagnostics": SOURCE_ONLY_DIAGNOSTICS,
            "goals": SOURCE_ONLY_GOALS,
            "build_status": SOURCE_ONLY_BUILD_STATUS,
            "last_verification": {},
            "proof_solved": False,
            "sorry_count": source_sorry_count,
            "queue_needs_final_file_sweep": False,
            "queue_frontier_exhausted": False,
            "capability_report": {},
        }
    )
    # Absence is intentional: a source-only snapshot is not even negative
    # kernel evidence and therefore must not participate in verification
    # promotion or compatibility truthiness checks.
    state.pop("verification_ok", None)
    state["message"] = "\n".join(
        [
            "[LEANFLOW-NATIVE SOURCE-ONLY UNVERIFIED STATE]",
            "This startup snapshot was built only from stable source bytes and queue/graph state.",
            "Lean diagnostics, goals, capabilities, and verification were not queried.",
            "It can select work but cannot certify completion or advance the queue.",
            "",
            f"Active file: {active_file_label}",
            f"Active file path: {revision.path}",
            f"Target theorem: {target_symbol}",
            f"Source sorry count: {source_sorry_count}",
            f"Source revision: {revision.sha256}",
            "",
            "Diagnostics:",
            SOURCE_ONLY_DIAGNOSTICS,
            "",
            "Goals:",
            SOURCE_ONLY_GOALS,
            "",
            "Queue horizon:",
            queue_summary,
            "",
            "Verification authority:",
            SOURCE_ONLY_BUILD_STATUS,
        ]
    )
    return state


def build_deferred_verification_snapshot(
    base_state: Mapping[str, Any],
    *,
    workflow_kind: str,
    revision: SourceRevision,
    verification_diagnostics: str,
) -> dict[str, Any]:
    """Return a sorry-free resume snapshot after bounded exact verification times out.

    A durable assignment is required so a slow proof is handed back to the
    foreground model instead of being mistaken for verified completion. The
    snapshot carries no kernel authority and defers eager incremental warmup,
    preventing startup from immediately replaying the same expensive file.
    """
    state = dict(base_state)
    current_item = dict(state.get("current_queue_item") or {})
    target_symbol = str(current_item.get("label", "") or "").strip()
    eligible = bool(
        str(workflow_kind or "").strip().casefold() == "prove"
        and str(state.get("declaration_scope", "") or "").strip() == "file"
        and str(state.get("active_file", "") or "").strip() == revision.path
        and target_symbol
    )
    if not eligible:
        return {}

    diagnostics = str(verification_diagnostics or "").strip()
    state.update(
        {
            "proof_state_authority": SOURCE_ONLY_PROOF_STATE_AUTHORITY,
            "used_source_only_snapshot": True,
            "source_revision": revision.to_mapping(),
            "source_revision_sha256": revision.sha256,
            "target_symbol": target_symbol,
            "diagnostics": diagnostics,
            "goals": SOURCE_ONLY_GOALS,
            "build_status": DEFERRED_VERIFICATION_BUILD_STATUS,
            "last_verification": {},
            "proof_solved": False,
            "sorry_count": 0,
            "queue_needs_final_file_sweep": False,
            "queue_frontier_exhausted": False,
            "capability_report": {},
            "defer_incremental_warmup": True,
            "current_blocker": DEFERRED_VERIFICATION_BUILD_STATUS,
            "blocker_summary": DEFERRED_VERIFICATION_BUILD_STATUS,
        }
    )
    state.pop("verification_ok", None)
    state["message"] = "\n".join(
        [
            "[LEANFLOW-NATIVE DEFERRED EXACT VERIFICATION]",
            "The stable source is sorry-free, but bounded startup verification timed out.",
            "This is not proof success or mathematical failure.",
            "Foreground work must optimize or verify the restored declaration before advancing.",
            "",
            f"Active file: {revision.path}",
            f"Target theorem: {target_symbol}",
            f"Source revision: {revision.sha256}",
            "",
            f"Diagnostics: {diagnostics or DEFERRED_VERIFICATION_BUILD_STATUS}",
        ]
    )
    return state
