"""Reuse an exact parent helper gate after its authenticated source insertion."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_helper_ephemeral import build_integrated_helper_source
from leanflow_cli.workflows import research_helper_candidate_priority
from tools.utilities.patch_parser import preview_v4a_update


@dataclass(frozen=True)
class ParentHelperReuseDecision:
    """Describe whether one post-edit helper gate can reuse parent evidence."""

    reusable: bool
    reason: str
    manager_check: Mapping[str, Any]


def _sha256_bytes(value: bytes) -> str:
    """Return the exact SHA-256 identity of source bytes."""
    return hashlib.sha256(value).hexdigest()


def _same_file(left: str, right: str) -> bool:
    """Return whether two path spellings identify the same canonical file."""
    try:
        left_path = str(Path(left).expanduser().resolve(strict=False))
        right_path = str(Path(right).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    return bool(
        left_path and right_path and os.path.normcase(left_path) == os.path.normcase(right_path)
    )


def expected_integrated_source(
    source_text: str,
    candidate: research_helper_candidate_priority.PendingResearchHelperCandidate,
) -> str:
    """Return the exact whole-file image implied by the parent helper harness."""
    integrated = build_integrated_helper_source(
        source_text,
        candidate.declaration,
        candidate.target_symbol,
    )
    if not integrated or not research_helper_candidate_priority.inserted_candidate_matches_source(
        candidate,
        integrated,
    ):
        return ""
    return integrated


def expected_integrated_source_revision_sha256(
    source_text: str,
    candidate: research_helper_candidate_priority.PendingResearchHelperCandidate,
) -> str:
    """Return the expected post-insertion source hash, or an empty value."""
    integrated = expected_integrated_source(source_text, candidate)
    return _sha256_bytes(integrated.encode("utf-8")) if integrated else ""


def exact_integrated_source_patch(
    source_text: str,
    candidate: research_helper_candidate_priority.PendingResearchHelperCandidate,
    *,
    path: str,
) -> str:
    """Build a compact V4A patch for the exact parent-checked source image."""
    expected = expected_integrated_source(source_text, candidate)
    if not expected or expected == source_text or not str(path or "").strip():
        return ""
    before_lines = source_text.splitlines()
    after_lines = expected.splitlines()
    prefix = 0
    while (
        prefix < len(before_lines)
        and prefix < len(after_lines)
        and before_lines[prefix] == after_lines[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_lines) - prefix
        and suffix < len(after_lines) - prefix
        and before_lines[-(suffix + 1)] == after_lines[-(suffix + 1)]
    ):
        suffix += 1
    context = 4
    before_start = max(0, prefix - context)
    before_stop = len(before_lines) - suffix
    after_stop = len(after_lines) - suffix
    suffix_stop = min(len(before_lines), before_stop + context)
    hunk: list[str] = []
    if before_stop == prefix:
        # For a pure insertion, anchor on the following declaration. Including
        # a prior scoped command would make the parser scope the hunk to the
        # declaration while simultaneously asking it to match text before that
        # region, an impossible anchor.
        hunk.extend(f"+{line}" for line in after_lines[prefix:after_stop])
        hunk.extend(f" {line}" for line in before_lines[before_stop:suffix_stop])
    else:
        hunk.extend(f" {line}" for line in before_lines[before_start:prefix])
        hunk.extend(f"-{line}" for line in before_lines[prefix:before_stop])
        hunk.extend(f"+{line}" for line in after_lines[prefix:after_stop])
        hunk.extend(f" {line}" for line in before_lines[before_stop:suffix_stop])
    patch = "\n".join(
        (
            "*** Begin Patch",
            f"*** Update File: {path}",
            "@@",
            *hunk,
            "*** End Patch",
        )
    )
    preview, error = preview_v4a_update(patch, source_text)
    return patch if not error and preview == expected else ""


def classify_reuse(
    candidate: research_helper_candidate_priority.PendingResearchHelperCandidate,
    *,
    target_symbol: str,
    active_file: str,
    edit_before_source_revision_sha256: str,
    allowed_axioms: Iterable[str],
) -> ParentHelperReuseDecision:
    """Promote cached evidence only for the exact authenticated source image.

    Lean declarations are elaborated sequentially. The parent check already
    elaborated the helper in the exact pre-anchor prefix. Whole-file equality
    with the deterministic insertion image proves that this prefix, declaration,
    and anchor position are unchanged; any other edit falls back to Lean.
    """

    def reject(reason: str) -> ParentHelperReuseDecision:
        return ParentHelperReuseDecision(False, reason, {})

    if not candidate.matches(target_symbol, active_file) or not _same_file(
        candidate.active_file,
        active_file,
    ):
        return reject("assignment_changed")
    if not research_helper_candidate_priority.parent_recheck_evidence_authenticated(candidate):
        return reject("parent_evidence_unavailable")
    if (
        str(edit_before_source_revision_sha256 or "").strip()
        != candidate.rechecked_source_revision_sha256
    ):
        return reject("pre_edit_source_changed")
    allowed = {str(value or "").strip() for value in allowed_axioms if str(value or "").strip()}
    if set(candidate.parent_recheck_axioms) - allowed:
        return reject("axiom_policy_changed")
    try:
        current_bytes = Path(active_file).read_bytes()
        current_text = current_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        return reject("integrated_source_unreadable")
    if _sha256_bytes(current_bytes) != candidate.expected_integrated_source_revision_sha256:
        return reject("integrated_source_changed")
    if (
        research_helper_candidate_priority.target_signature_sha256(active_file, target_symbol)
        != candidate.target_signature_sha256
    ):
        return reject("target_signature_changed")
    if not research_helper_candidate_priority.inserted_candidate_matches_source(
        candidate,
        current_text,
    ):
        return reject("helper_insertion_changed")
    axioms = list(candidate.parent_recheck_axioms)
    manager_check = {
        "success": True,
        "ok": True,
        "target": candidate.helper_name,
        "file": str(Path(active_file).expanduser().resolve(strict=False)),
        "output": "reused exact parent helper verification for authenticated insertion image",
        "has_errors": False,
        "has_sorry": False,
        "errors": 0,
        "warnings": 0,
        "sorry": 0,
        "axiom_profile_checked": True,
        "axiom_profile_axioms": axioms,
        "axiom_profile_blockers": [],
        "cache": {"cache_hit": True, "kind": "exact_parent_helper_insertion"},
        "incremental": {
            "success": True,
            "ok": True,
            "target": candidate.helper_name,
            "file": str(Path(active_file).expanduser().resolve(strict=False)),
            "has_errors": False,
            "has_sorry": False,
            "errors": 0,
            "warnings": 0,
            "sorry": 0,
        },
    }
    return ParentHelperReuseDecision(True, "exact_parent_insertion", manager_check)
