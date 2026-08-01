"""Extract a large local ``have`` into a verified private helper lemma."""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from core import verified_edit_authority
from leanflow_cli.lean.lean_have_extraction import HaveCandidate, select_candidate
from leanflow_cli.lean.lean_incremental import lean_incremental_check
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _declaration_matches_target,
)
from tools.implementations.lean_patch import apply_verified_patch_tool


def _failure(status: str, message: str, **fields: Any) -> str:
    """Return one stable failed extraction payload."""
    return json.dumps(
        {"success": False, "status": status, "message": message, **fields},
        ensure_ascii=False,
    )


def _helper_name(theorem_id: str, have_name: str, source: str) -> str:
    """Return a collision-free private helper name."""
    stem = re.sub(r"\W+", "_", f"{theorem_id}_{have_name}").strip("_")
    base = f"leanflow_{stem or 'extracted_have'}"
    name = base
    index = 2
    while re.search(rf"\b(?:theorem|lemma)\s+{re.escape(name)}\b", source):
        name = f"{base}_{index}"
        index += 1
    return name


def _instrumented_candidate(candidate: HaveCandidate, helper_name: str) -> str:
    """Insert ``extract_goal`` before the original local proof."""
    proof = textwrap.dedent(candidate.proof).strip("\n")
    tactic_indent = candidate.indent + "  "
    body = textwrap.indent(proof, tactic_indent) if proof.strip() else ""
    parts = [candidate.header, f"\n{tactic_indent}extract_goal using {helper_name}"]
    if body:
        parts.append("\n" + body)
    return "".join(parts)


def _truncated_declaration(
    declaration: str,
    candidate: HaveCandidate,
    replacement: str,
) -> str:
    """Close the target immediately after one candidate for bounded prefix checking."""
    return declaration[: candidate.start] + replacement + f"\n{candidate.indent}sorry"


def _extracted_statement(payload: dict[str, Any], helper_name: str) -> str:
    """Return the standalone theorem emitted by Mathlib's ``extract_goal`` tactic."""
    for message in list(payload.get("messages") or []):
        text = (
            str(message.get("message", "") or "").strip()
            if isinstance(message, dict)
            else str(message or "").strip()
        )
        if re.match(rf"^theorem\s+{re.escape(helper_name)}\b", text):
            return text
    output = str(payload.get("output", "") or "")
    match = re.search(rf"theorem\s+{re.escape(helper_name)}\b.*?:=\s*sorry", output, re.DOTALL)
    return match.group(0).strip() if match else ""


def _private_helper(statement: str, candidate: HaveCandidate) -> str:
    """Combine the extracted context signature with the original checked proof body."""
    declaration = re.sub(r"^theorem\b", "private lemma", statement, count=1)
    declaration = re.sub(r":=\s*(?:by\s*)?sorry\s*$", ":= by", declaration)
    if declaration == statement:
        return ""
    proof = textwrap.dedent(candidate.proof).strip()
    return declaration + ("\n" + textwrap.indent(proof, "  ") if proof else "")


def _switched_candidate(candidate: HaveCandidate, helper_name: str) -> str:
    """Replace the local proof with an automatically discharged helper call."""
    return candidate.header + f"\n{candidate.indent}  solve_by_elim [{helper_name}]"


def _v4a_replace(path: Path, old: str, new: str) -> str:
    """Build one exact V4A replacement patch."""
    removed = "\n".join(f"-{line}" for line in old.splitlines())
    added = "\n".join(f"+{line}" for line in new.splitlines())
    return (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        f"{removed}\n{added}\n"
        "*** End Patch"
    )


def lean_extract_have_tool(
    theorem_id: str,
    file_path: str,
    *,
    cwd: str = "",
    have_name: str = "",
    minimum_lines: int = 8,
    timeout_s: int = 300,
    owner_id: str = "",
) -> str:
    """Extract and transactionally bank one independently verified local ``have`` proof."""
    root = Path(cwd).expanduser().resolve() if str(cwd or "").strip() else Path.cwd().resolve()
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        return _failure("file_not_found", "Lean file not found.", file_path=str(path))
    source = path.read_text(encoding="utf-8")
    entry = next(
        (
            item
            for item in _declaration_line_index_from_text(source)
            if _declaration_matches_target(item, theorem_id)
        ),
        None,
    )
    if entry is None:
        return _failure(
            "target_not_found", "Assigned declaration not found.", theorem_id=theorem_id
        )
    declaration = str(entry.get("text", "") or "")
    candidate = select_candidate(
        declaration,
        have_name=have_name,
        minimum_lines=max(2, int(minimum_lines or 8)),
    )
    if candidate is None:
        return _failure(
            "no_extractable_have",
            "No complete top-level local have proof matched the extraction request.",
            theorem_id=theorem_id,
            have_name=have_name,
        )
    helper_name = _helper_name(theorem_id, candidate.name, source)
    instrumented = _instrumented_candidate(candidate, helper_name)
    probe = lean_incremental_check(
        action="check_target",
        file_path=str(path),
        theorem_id=theorem_id,
        cwd=str(root),
        replacement=_truncated_declaration(declaration, candidate, instrumented),
        timeout_s=max(1, int(timeout_s or 300)),
        timeout_ceiling_s=max(1, int(timeout_s or 300)),
        allow_placeholders_for_elaboration=True,
    )
    statement = _extracted_statement(probe, helper_name)
    if not statement:
        return _failure(
            "goal_extraction_failed",
            "Lean did not emit a standalone theorem signature for the selected have.",
            theorem_id=theorem_id,
            have_name=candidate.name,
            diagnostics=probe,
        )
    helper = _private_helper(statement, candidate)
    if not helper:
        return _failure(
            "goal_extraction_failed",
            "Lean emitted a helper signature in an unsupported shape.",
            theorem_id=theorem_id,
            have_name=candidate.name,
            extracted_statement=statement,
        )
    helper_check = lean_incremental_check(
        action="check_helper",
        file_path=str(path),
        theorem_id=theorem_id,
        cwd=str(root),
        replacement=helper,
        include_axiom_profile=True,
        timeout_s=max(1, int(timeout_s or 300)),
        timeout_ceiling_s=max(1, int(timeout_s or 300)),
    )
    blockers = list(helper_check.get("axiom_profile_blockers") or [])
    if not (
        helper_check.get("success") is True
        and helper_check.get("ok") is True
        and helper_check.get("valid_without_sorry") is True
        and helper_check.get("axiom_profile_checked") is True
        and not blockers
    ):
        return _failure(
            "helper_verification_failed",
            "The extracted helper did not pass its independent LeanProbe and axiom gates.",
            theorem_id=theorem_id,
            have_name=candidate.name,
            helper_name=helper_name,
            diagnostics=helper_check,
        )
    switched = _switched_candidate(candidate, helper_name)
    prefix_check = lean_incremental_check(
        action="check_target",
        file_path=str(path),
        theorem_id=theorem_id,
        cwd=str(root),
        replacement=(helper + "\n\n" + _truncated_declaration(declaration, candidate, switched)),
        timeout_s=max(1, int(timeout_s or 300)),
        timeout_ceiling_s=max(1, int(timeout_s or 300)),
        allow_placeholders_for_elaboration=True,
    )
    if prefix_check.get("has_errors") is True or prefix_check.get("timed_out") is True:
        return _failure(
            "helper_switch_failed",
            "The private helper passed, but replacing the local have did not elaborate.",
            theorem_id=theorem_id,
            have_name=candidate.name,
            helper_name=helper_name,
            diagnostics=prefix_check,
        )
    rewritten = declaration[: candidate.start] + switched + declaration[candidate.end :]
    combined = helper + "\n\n" + rewritten
    patch = _v4a_replace(path, declaration, combined)
    before_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    after_source = source.replace(declaration, combined, 1)
    after_sha256 = hashlib.sha256(after_source.encode("utf-8")).hexdigest()
    authority = verified_edit_authority.register(
        path=str(path),
        theorem_id=theorem_id,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        verified_declaration=helper_name,
        axiom_profile_axioms=tuple(helper_check.get("axiom_profile_axioms") or ()),
    )
    result = json.loads(
        apply_verified_patch_tool(
            str(path),
            patch,
            cwd=str(root),
            check_mode="incremental",
            theorem_id=theorem_id,
            owner_id=owner_id,
            timeout_s=max(1, int(timeout_s or 300)),
            verified_edit_authority_token=authority,
        )
    )
    result.update(
        {
            "extraction": {
                "theorem_id": theorem_id,
                "have_name": candidate.name,
                "helper_name": helper_name,
                "extracted_lines": candidate.line_count,
                "helper_check": helper_check,
                "switch_prefix_check": prefix_check,
            }
        }
    )
    return json.dumps(result, ensure_ascii=False)
