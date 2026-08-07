"""Validate helper declarations and their axioms with one-shot exact Lean."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean import lean_axiom_batch
from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _statement_signature_text,
    _strip_lean_comments_and_strings,
)

_STANDARD_AXIOMS = frozenset({"propext", "Quot.sound", "Classical.choice"})
_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b", flags=re.IGNORECASE)


@dataclass(frozen=True)
class HelperHarness:
    """Hold one exact pre-anchor helper harness and its axiom queries."""

    source: str
    declarations: tuple[str, ...]
    axiom_plan: lean_axiom_batch.AxiomBatchPlan


def _error_payload(
    *,
    file_path: Path,
    theorem_id: str,
    error: str,
    error_code: str,
    has_sorry: bool = False,
    declarations: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a fail-closed result in the public ``check_helper`` schema."""
    return {
        "success": False,
        "ok": False,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_helper",
        "file": str(file_path),
        "target": theorem_id,
        "valid_without_sorry": False,
        "has_errors": not has_sorry,
        "has_sorry": has_sorry,
        "verification_scope": "helper_candidate",
        "replacement_matches_target": False,
        "replacement_declarations": list(declarations),
        "anchor_target": theorem_id,
        "anchor_temporary_sorry": True,
        "axiom_profile_requested": True,
        "axiom_profile_checked": False,
        "axiom_profile_axioms": [],
        "axiom_profile_blockers": [],
        "axiom_profile_error": error,
        "error": error,
        "error_code": error_code,
        "output": error,
        "messages": [],
    }


def _allowed_axioms() -> set[str]:
    """Return standard axioms plus the workflow's explicit allowlist."""
    allowed = set(_STANDARD_AXIOMS)
    raw = str(os.getenv("LEANFLOW_NATIVE_ALLOWED_AXIOMS", "") or "")
    allowed.update(token for token in re.split(r"[,\s]+", raw) if token)
    return allowed


def _source_segments(source_text: str) -> tuple[str, list[Any], str]:
    """Return LeanProbe parser segments without constructing a Lean session."""
    try:
        from lean_probe.core import segment_file
    except Exception as exc:
        return "", [], f"lean-probe segmenter unavailable: {str(exc)[:300]}"
    try:
        header, segments = segment_file(source_text)
    except Exception as exc:
        return "", [], f"could not segment active Lean source: {str(exc)[:300]}"
    return str(header or ""), list(segments or []), ""


def _target_segment(segments: list[Any], theorem_id: str) -> tuple[Any | None, str]:
    """Resolve one exact-or-unambiguous short-name anchor segment."""
    wanted = str(theorem_id or "").strip()
    exact = [segment for segment in segments if str(getattr(segment, "name", "")) == wanted]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"anchor declaration {wanted!r} is ambiguous"
    short = wanted.rsplit(".", 1)[-1]
    matches = [
        segment
        for segment in segments
        if str(getattr(segment, "name", "")).rsplit(".", 1)[-1] == short
    ]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"anchor declaration {wanted!r} is ambiguous"
    return None, f"anchor declaration {wanted!r} not found"


def _helper_declarations(helper_source: str) -> tuple[tuple[str, ...], str]:
    """Return named helper declarations or a deterministic rejection reason."""
    entries = _declaration_line_index_from_text(helper_source)
    names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in entries
        if str(entry.get("name", "") or "").strip()
    )
    if not names or any(name.startswith("[anonymous ") for name in names):
        return (), "helper replacement must contain at least one named declaration"
    short_names = [name.rsplit(".", 1)[-1] for name in names]
    if len(set(names)) != len(names) or len(set(short_names)) != len(short_names):
        return (), "helper replacement contains ambiguous declaration names"
    return names, ""


def _helper_insertion_prefix(source_text: str, helper_source: str, target_start: int) -> str:
    """Return the exact normalized prefix used by the ephemeral helper harness."""
    prefix = source_text[:target_start]
    return "".join(
        (
            prefix.rstrip(),
            "\n\n" if prefix.strip() else "",
            helper_source.rstrip(),
            "\n\n",
        )
    )


def build_integrated_helper_source(
    source_text: str,
    helper_source: str,
    theorem_id: str,
) -> str:
    """Insert a helper using the exact pre-anchor transform checked by the harness.

    Return an empty value when the anchor cannot be resolved unambiguously. The
    source suffix is preserved byte-for-byte; callers may therefore bind a
    later committed edit to the exact prefix that Lean already elaborated.
    """
    _header, segments, segment_error = _source_segments(source_text)
    if segment_error:
        return ""
    target, target_error = _target_segment(segments, theorem_id)
    if target is None or target_error:
        return ""
    target_start = int(getattr(target, "start", -1))
    if not 0 <= target_start <= len(source_text):
        return ""
    return (
        _helper_insertion_prefix(source_text, helper_source, target_start)
        + source_text[target_start:]
    )


def _build_harness(
    *,
    source_text: str,
    helper_source: str,
    theorem_id: str,
    anchor_skeleton: str,
) -> tuple[HelperHarness | None, str, str]:
    """Build the exact pre-anchor source plus marked helper axiom queries."""
    _header, segments, segment_error = _source_segments(source_text)
    if segment_error:
        return None, "source_segmentation_failed", segment_error
    target, target_error = _target_segment(segments, theorem_id)
    if target is None:
        return None, "anchor_target_not_found", target_error

    declarations, declaration_error = _helper_declarations(helper_source)
    if declaration_error:
        return None, "missing_helper_declaration", declaration_error

    target_start = int(getattr(target, "start", -1))
    declaration_start = int(getattr(target, "declaration_start", -1))
    target_end = int(getattr(target, "end", -1))
    if not (0 <= target_start <= declaration_start <= target_end <= len(source_text)):
        return None, "source_segmentation_failed", "anchor source offsets are invalid"
    anchor_signature = _statement_signature_text(anchor_skeleton).strip()
    signature_offset = source_text.find(
        anchor_signature,
        declaration_start,
        target_end,
    )
    if not anchor_signature or signature_offset < 0:
        return (
            None,
            "anchor_signature_mismatch",
            "temporary target skeleton does not match the exact anchor declaration",
        )
    # Preserve documentation and attributes attached to the target while
    # replacing only its declaration signature/body with the sorry skeleton.
    preamble = source_text[target_start:signature_offset]
    prefix = source_text[:target_start]
    prefix_entries = _declaration_line_index_from_text(prefix)
    prefix_short_names = {
        str(entry.get("name", "") or "").strip().rsplit(".", 1)[-1]
        for entry in prefix_entries
        if str(entry.get("name", "") or "").strip()
    }
    if prefix_short_names.intersection(name.rsplit(".", 1)[-1] for name in declarations):
        return (
            None,
            "ambiguous_helper_declaration",
            "helper declaration name collides with the exact pre-anchor environment",
        )

    insertion_prefix = _helper_insertion_prefix(source_text, helper_source, target_start)
    harness = "".join((insertion_prefix, preamble, anchor_skeleton.rstrip(), "\n"))
    entries = _declaration_line_index_from_text(harness)
    plan = lean_axiom_batch.build_axiom_batch_plan(
        harness,
        entries,
        declarations[0],
        requested_targets=declarations[1:],
        prefetch_siblings=False,
        truncate_after_last_query=False,
    )
    if plan is None:
        return None, "helper_axiom_harness_failed", "could not build helper axiom harness"
    return HelperHarness(plan.source, declarations, plan), "", ""


def _axiom_profile(
    harness: HelperHarness,
    output: str,
) -> tuple[list[str] | None, list[str]]:
    """Return the complete helper axiom union and disallowed dependencies."""
    profiles = lean_axiom_batch.parse_axiom_batch_output(output, harness.axiom_plan.queries)
    if profiles is None:
        return None, []
    requested_ids = [identity for _name, identity in harness.axiom_plan.requested_identities]
    if any(identity not in profiles for identity in requested_ids):
        return None, []
    axioms = sorted(
        {
            axiom
            for identity in requested_ids
            for axiom in profiles[identity].axioms
            if str(axiom).strip()
        }
    )
    blockers = sorted(set(axioms) - _allowed_axioms())
    return axioms, blockers


def check_helper_ephemerally(
    *,
    source_text: str,
    helper_source: str,
    theorem_id: str,
    file_path: Path,
    project_root: Path,
    anchor_skeleton: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Validate a helper against the exact pre-target environment with Lake.

    The returned artifact remains advisory and retains the existing
    parent-recheck contract even when a foreground caller requests it.
    """
    resolved_file = file_path.expanduser().resolve()
    stripped = _strip_lean_comments_and_strings(str(helper_source or ""))
    if _PLACEHOLDER_RE.search(stripped):
        return _error_payload(
            file_path=resolved_file,
            theorem_id=theorem_id,
            error="helper candidate contains sorry/admit",
            error_code="helper_placeholder",
            has_sorry=True,
        )

    harness, error_code, error = _build_harness(
        source_text=source_text,
        helper_source=helper_source,
        theorem_id=theorem_id,
        anchor_skeleton=anchor_skeleton,
    )
    if harness is None:
        return _error_payload(
            file_path=resolved_file,
            theorem_id=theorem_id,
            error=error,
            error_code=error_code,
        )

    checked = dict(
        lean_ephemeral_source_check(
            harness.source,
            cwd=project_root,
            timeout_s=timeout_s,
        )
        or {}
    )
    backend_ok = checked.get("success") is True and checked.get("ok") is True
    output = str(checked.get("output", "") or "")
    axioms, blockers = _axiom_profile(harness, output) if backend_ok else (None, [])
    profile_checked = axioms is not None
    helper_ok = backend_ok and profile_checked and not blockers
    result = {
        **checked,
        "success": bool(checked.get("success")),
        "ok": helper_ok,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_helper",
        "file": str(resolved_file),
        "target": theorem_id,
        "valid_without_sorry": helper_ok,
        "has_errors": not backend_ok,
        "has_sorry": False,
        "verification_scope": "helper_candidate",
        "replacement_matches_target": False,
        "replacement_declarations": list(harness.declarations),
        "anchor_target": theorem_id,
        "anchor_temporary_sorry": True,
        "anchor_backend_ok": backend_ok,
        "axioms": list(axioms or []),
        "axiom_profile_requested": True,
        "axiom_profile_checked": profile_checked,
        "axiom_profile_axioms": list(axioms or []),
        "axiom_profile_blockers": blockers,
        "axiom_profile_error": (
            "" if profile_checked else "helper candidate has no auditable axiom result"
        ),
        "messages": list(checked.get("messages") or []),
    }
    if not backend_ok:
        result.setdefault("error_code", "helper_elaboration_failed")
        result.setdefault("error", "exact-project helper harness did not elaborate")
    elif not profile_checked:
        result.update(
            {
                "error": "helper candidate has no auditable axiom result",
                "error_code": "helper_axiom_profile_unavailable",
            }
        )
    elif blockers:
        result.update(
            {
                "error": "helper candidate depends on disallowed axioms: " + ", ".join(blockers),
                "error_code": "helper_axiom_profile",
            }
        )
    else:
        result.update(
            {
                "error": "",
                "error_code": "",
                "output": "helper candidate elaborated without errors, placeholders, or disallowed axioms",
            }
        )
    return result
