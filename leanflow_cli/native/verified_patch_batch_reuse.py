"""Reuse one source-bound verified-patch compile with batched axiom profiles.

``apply_verified_patch`` already compiles the complete edited file.  This
module authenticates that broad result against the exact current source, then
combines it with one all-or-nothing ``#print axioms`` batch for every changed
proof declaration.  The resulting checks are declaration-scoped acceptance
evidence; malformed, stale, partial, or placeholder-bearing input fails closed
and leaves the native runner's ordinary per-declaration gates in charge.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_diagnostics import diagnostic_items
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit)\b")


@dataclass(frozen=True)
class VerifiedPatchBatchDecision:
    """Describe exact checks recovered from one authenticated patch result."""

    reusable: bool
    reason: str
    checks: Mapping[str, Mapping[str, Any]]
    source_sha256: str = ""
    axiom_batch_started: bool = False


def source_revision_sha256(path: str | Path) -> str:
    """Return the exact source-byte digest, or an empty fail-closed marker."""
    try:
        return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
    except OSError:
        return ""


def _canonical_file(value: object) -> str:
    """Return one normalized path spelling without requiring file existence."""
    try:
        return str(Path(str(value or "")).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return ""


def _same_file(left: object, right: object) -> bool:
    """Return whether two path spellings identify the same file."""
    left_path = _canonical_file(left)
    right_path = _canonical_file(right)
    return bool(
        left_path and right_path and os.path.normcase(left_path) == os.path.normcase(right_path)
    )


def _entry_for_target(
    entries: Sequence[Mapping[str, Any]], target: str
) -> Mapping[str, Any] | None:
    """Resolve one declaration exactly, or by one unambiguous short name."""
    wanted = str(target or "").strip().removeprefix("_root_.")
    if not wanted:
        return None
    exact = [
        entry
        for entry in entries
        if str(entry.get("name", "") or "").strip().removeprefix("_root_.") == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    short = wanted.split(".")[-1]
    matching = [
        entry
        for entry in entries
        if str(entry.get("name", "") or "").strip().split(".")[-1] == short
    ]
    return matching[0] if len(matching) == 1 else None


def _report_field(report: object, key: str, default: object = None) -> object:
    """Read one axiom-report field from a dataclass-like object or mapping."""
    if isinstance(report, Mapping):
        return report.get(key, default)
    return getattr(report, key, default)


def _target_messages(
    output: str,
    *,
    start_line: int,
    end_line: int,
) -> list[dict[str, Any]]:
    """Return broad-check diagnostics whose locations belong to a declaration."""
    messages: list[dict[str, Any]] = []
    for item in diagnostic_items(str(output or "")):
        line = item.get("line")
        if not isinstance(line, int) or line < start_line or line > end_line:
            continue
        messages.append(dict(item))
    return messages


def _rejection(reason: str, *, axiom_batch_started: bool = False) -> VerifiedPatchBatchDecision:
    """Return one empty, fail-closed reuse decision."""
    return VerifiedPatchBatchDecision(
        False,
        reason,
        {},
        axiom_batch_started=axiom_batch_started,
    )


def build_reusable_checks(
    patch_result: Mapping[str, Any],
    *,
    active_file: str,
    assignment_target: str,
    declaration_targets: Sequence[str],
    allowed_axioms: Iterable[str],
    inspect_axioms_many: Callable[[Sequence[str], str], Mapping[str, object]],
) -> VerifiedPatchBatchDecision:
    """Build declaration checks from an exact file compile and one axiom batch.

    Require the patch tool's pre/post verification digest, canonical path and
    file-exact result to match the current bytes.  Every requested declaration
    must be a uniquely resolved theorem/lemma with no executable placeholder,
    and the axiom batch must return a complete source-bound report for all of
    them.  Any uncertainty returns no checks so callers can run fresh gates.
    """
    payload = dict(patch_result or {})
    if (
        payload.get("success") is not True
        or payload.get("check_passed") is not True
        or str(payload.get("status", "") or "").strip().lower()
        not in {"patch_elaborated", "verified"}
    ):
        return _rejection("broad_verification_failed")
    if str(payload.get("check_mode", "") or "").strip().lower() != "file_exact":
        return _rejection("broad_scope_not_file_exact")
    if not _same_file(payload.get("path"), active_file):
        return _rejection("active_file_changed")
    if str(payload.get("theorem_id", "") or "").strip() != str(assignment_target or "").strip():
        return _rejection("assignment_target_changed")
    verification = dict(payload.get("verification") or {})
    if (
        verification.get("ok") is not True
        or str(verification.get("mode", "") or "").strip().lower() != "file_exact"
        or not _same_file(verification.get("target"), active_file)
    ):
        return _rejection("broad_verification_identity_mismatch")
    verified_revision = str(payload.get("verified_source_revision_sha256", "") or "").strip()
    if (
        _SHA256_RE.fullmatch(verified_revision) is None
        or payload.get("verification_source_unchanged") is not True
        or source_revision_sha256(active_file) != verified_revision
    ):
        return _rejection("verified_source_changed")

    targets = tuple(
        dict.fromkeys(
            str(target or "").strip() for target in declaration_targets if str(target or "").strip()
        )
    )
    if not targets:
        return _rejection("no_declaration_targets")
    try:
        source = Path(active_file).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _rejection("source_unreadable")
    entries = _declaration_line_index_from_text(source)
    resolved_entries: dict[str, Mapping[str, Any]] = {}
    for target in targets:
        entry = _entry_for_target(entries, target)
        if entry is None:
            return _rejection(f"declaration_unresolved:{target}")
        kind = str(entry.get("kind", "") or "").strip().lower()
        declaration = str(entry.get("text", "") or "")
        if kind not in {"theorem", "lemma"}:
            return _rejection(f"declaration_not_proof:{target}")
        if _PLACEHOLDER_RE.search(_strip_lean_comments_and_strings(declaration)):
            return _rejection(f"declaration_has_placeholder:{target}")
        resolved_entries[target] = entry

    try:
        reports = dict(inspect_axioms_many(targets, active_file) or {})
    except Exception:
        return _rejection("axiom_batch_raised", axiom_batch_started=True)
    if set(reports) != set(targets):
        return _rejection("axiom_batch_incomplete", axiom_batch_started=True)
    # The axiom service checks the source and import-environment fingerprints
    # around its harness. Recheck the source once more before exposing any
    # cached declaration authority to the runner.
    if source_revision_sha256(active_file) != verified_revision:
        return _rejection("source_changed_during_axiom_batch", axiom_batch_started=True)

    allowed = {str(axiom or "").strip() for axiom in allowed_axioms if str(axiom or "").strip()}
    broad_output = str(verification.get("output", "") or "")
    checks: dict[str, Mapping[str, Any]] = {}
    canonical_file = _canonical_file(active_file)
    for target in targets:
        report = reports[target]
        if (
            _report_field(report, "inspection_succeeded", False) is not True
            or str(_report_field(report, "target", "") or "").strip() != target
            or not _same_file(_report_field(report, "file_path", ""), active_file)
        ):
            return _rejection(
                f"axiom_profile_unavailable:{target}",
                axiom_batch_started=True,
            )
        raw_axioms = _report_field(report, "axioms", ())
        if (
            not isinstance(raw_axioms, Sequence)
            or isinstance(raw_axioms, (str, bytes))
            or any(not isinstance(item, str) or not item.strip() for item in raw_axioms)
        ):
            return _rejection(
                f"axiom_profile_malformed:{target}",
                axiom_batch_started=True,
            )
        axioms = sorted({str(item).strip() for item in raw_axioms})
        if len(axioms) != len(raw_axioms):
            return _rejection(
                f"axiom_profile_ambiguous:{target}",
                axiom_batch_started=True,
            )
        blockers = sorted(set(axioms) - allowed)
        entry = resolved_entries[target]
        declaration = str(entry.get("text", "") or "")
        declaration_sha256 = hashlib.sha256(declaration.encode("utf-8")).hexdigest()
        messages = _target_messages(
            broad_output,
            start_line=max(1, int(entry.get("line", 1) or 1)),
            end_line=max(1, int(entry.get("end_line", 1) or 1)),
        )
        warnings = sum(
            1
            for item in messages
            if str(item.get("severity", "") or "").strip().lower() == "warning"
        )
        if blockers:
            blocker_message = (
                f"axiom guard: `{target}` verifies but DEPENDS on disallowed axiom(s): "
                + ", ".join(blockers)
            )
            messages = [*messages, {"severity": "error", "message": blocker_message}]
            output = blocker_message
        else:
            output = (
                f"reused exact file elaboration and one source-bound batched axiom profile "
                f"for `{target}`"
            )
        incremental = {
            "success": True,
            "ok": True,
            "target": target,
            "file": canonical_file,
            "has_errors": False,
            "has_sorry": False,
            "errors": 0,
            "warnings": warnings,
            "sorry": 0,
            "messages": messages,
            "source_sha256": verified_revision,
            "declaration_sha256": declaration_sha256,
        }
        checks[target] = {
            "success": True,
            "ok": not blockers,
            "mode": "incremental_target",
            "backend": "lean_file_axiom_batch",
            "command": "lake env lean <verified-patch axiom batch>",
            "target": target,
            "file": canonical_file,
            "output": output,
            "messages": messages,
            "has_errors": bool(blockers),
            "has_sorry": False,
            "errors": len(blockers),
            "warnings": warnings,
            "sorry": 0,
            "source_sha256": verified_revision,
            "declaration_sha256": declaration_sha256,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": axioms,
            "axiom_profile_blockers": blockers,
            "axiom_profile_source": "verified_patch_batch",
            "verification_reused": True,
            "verification_reuse_reason": (
                "same file-exact post-patch source and complete all-target axiom batch"
            ),
            "cache": {"cache_hit": True, "kind": "verified_patch_batch"},
            "incremental": incremental,
        }
    return VerifiedPatchBatchDecision(
        True,
        "exact_verified_patch_batch",
        checks,
        verified_revision,
        axiom_batch_started=True,
    )
