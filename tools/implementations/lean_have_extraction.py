"""Extract a large local ``have`` into a verified private helper lemma."""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core import verified_edit_authority
from leanflow_cli.lean.lean_have_extraction import HaveCandidate, candidates, ranked_candidates
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


def _helper_name(
    theorem_id: str,
    have_name: str,
    source: str,
    *,
    requested_name: str = "",
) -> str:
    """Return a collision-free private helper name."""
    requested = re.sub(r"\W+", "_", str(requested_name or "")).strip("_")
    stem = re.sub(r"\W+", "_", f"{theorem_id}_{have_name}").strip("_")
    base = requested or f"leanflow_{stem or 'extracted_have'}"
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


def _private_helper(
    statement: str,
    candidate: HaveCandidate,
    *,
    context_prefix: str = "",
) -> str:
    """Combine the extracted context signature with the original checked proof body."""
    declaration = _freshen_explicit_universes(statement)
    let_prelude = _leading_result_let_prelude(
        declaration,
        context_prefix=context_prefix,
        indent=candidate.indent,
    )
    declaration = re.sub(r"^theorem\b", "private lemma", declaration, count=1)
    declaration, replacement_count = re.subn(
        r":=\s*(?:by\s*)?sorry\s*$",
        ":= by",
        declaration,
    )
    if replacement_count != 1:
        return ""
    proof = textwrap.dedent(candidate.proof).strip()
    body_parts = [part for part in (let_prelude, proof) if part]
    return declaration + ("\n" + textwrap.indent("\n".join(body_parts), "  ") if body_parts else "")


def _freshen_explicit_universes(statement: str) -> str:
    """Rename ``extract_goal`` universe binders away from the active file scope.

    Mathlib's ``extract_goal`` prints the anchor declaration's generated universe
    names verbatim.  Re-inserting a helper such as ``foo.{u_2, u_1}`` before the
    anchor can therefore redeclare names that the file already owns.  Give every
    explicit binder a deterministic declaration-local name and rewrite its uses
    before the helper reaches LeanProbe.
    """
    match = re.match(
        r"(?s)^(theorem\s+[^\s.{]+)\.\{([^{}]+)\}(.*)$",
        str(statement or ""),
    )
    if match is None:
        return statement
    names = [name.strip() for name in match.group(2).split(",")]
    if not names or any(not re.fullmatch(r"[A-Za-z_][\w']*", name) for name in names):
        return statement
    digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()[:10]
    rewritten_tail = match.group(3)
    fresh_names: list[str] = []
    for index, name in enumerate(names, start=1):
        fresh = f"leanflow_u_{digest}_{index}"
        fresh_names.append(fresh)
        rewritten_tail = re.sub(
            rf"(?<![\w']){re.escape(name)}(?![\w'])",
            fresh,
            rewritten_tail,
        )
    return f"{match.group(1)}.{{{', '.join(fresh_names)}}}{rewritten_tail}"


def _top_level_character(text: str, wanted: str, *, start: int = 0) -> int:
    """Return the first delimiter-free character offset in generated Lean text."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(max(0, start), len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in pairs:
            stack.append(pairs[char])
            continue
        if char in closing:
            if stack and char == stack[-1]:
                stack.pop()
            continue
        if char == wanted and not stack:
            return index
    return -1


def _source_local_let(
    context_prefix: str,
    name: str,
    *,
    indent: str,
) -> str:
    """Return the last exact source-level local let with the requested name."""
    lines = str(context_prefix or "").splitlines()
    start_pattern = re.compile(rf"^{re.escape(indent)}let\s+{re.escape(name)}\b")
    starts = [index for index, line in enumerate(lines) if start_pattern.match(line)]
    if not starts:
        return ""
    start = starts[-1]
    end = len(lines)
    base_width = len(indent.expandtabs(2))
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        line_indent = line[: len(line) - len(line.lstrip(" \t"))]
        if len(line_indent.expandtabs(2)) <= base_width:
            end = index
            break
    return textwrap.dedent("\n".join(lines[start:end])).strip()


def _leading_result_let_prelude(
    statement: str,
    *,
    context_prefix: str = "",
    indent: str = "",
) -> str:
    """Recreate result-level ``let`` binders as named locals for a copied proof.

    ``extract_goal`` reverts local let declarations into the result type.  The
    original proof still refers to those local names, so introduce the same lets
    in the helper body and change the zeta-reduced goal back to the named form.
    """
    end_match = re.search(r":=\s*(?:by\s*)?sorry\s*$", statement)
    if end_match is None:
        return ""
    result_start = _top_level_character(statement[: end_match.start()], ":")
    if result_start < 0:
        return ""
    result = statement[result_start + 1 : end_match.start()].strip()
    bindings: list[tuple[str, str]] = []
    while result.startswith("let "):
        semicolon = _top_level_character(result, ";")
        if semicolon < 0:
            return ""
        binding = result[:semicolon].strip()
        name_match = re.match(r"let\s+([A-Za-z_«][\w'.«»]*)\b", binding)
        if name_match is None:
            return ""
        bindings.append((name_match.group(1), binding))
        result = result[semicolon + 1 :].strip()
    if not bindings or not result:
        return ""
    change = "change " + result.replace("\n", "\n  ")
    source_bindings = [
        _source_local_let(context_prefix, name, indent=indent) or generated
        for name, generated in bindings
    ]
    return "\n".join([*source_bindings, change])


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
    action: str = "extract",
    have_name: str = "",
    have_names: Sequence[str] | None = None,
    helper_names: Mapping[str, str] | None = None,
    minimum_lines: int = 8,
    max_helpers: int = 1,
    timeout_s: int = 300,
    owner_id: str = "",
) -> str:
    """Inventory or transactionally extract a bounded set of local ``have`` proofs."""
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
    available = candidates(declaration)
    minimum = max(2, int(minimum_lines or 8))
    requested_names = tuple(
        dict.fromkeys(
            [
                *([str(have_name).strip()] if str(have_name or "").strip() else []),
                *[
                    str(name or "").strip()
                    for name in (have_names or ())
                    if str(name or "").strip()
                ],
            ]
        )
    )
    ranked = ranked_candidates(declaration, minimum_lines=minimum)
    inventory = [
        {
            "have_name": candidate.name,
            "line_count": candidate.line_count,
            "source_chars": len(candidate.source),
            "source_start": candidate.start,
            "suggested_helper_name": _helper_name(theorem_id, candidate.name, source),
            "estimated_context_reduction_chars": max(
                0, len(candidate.source) - len(candidate.header)
            ),
        }
        for candidate in ranked
    ]
    normalized_action = str(action or "extract").strip().lower().replace("-", "_")
    if normalized_action in {"inventory", "inspect", "list", "plan"}:
        return json.dumps(
            {
                "success": True,
                "status": "candidate_inventory",
                "theorem_id": theorem_id,
                "candidate_count": len(inventory),
                "candidates": inventory,
                "transactional_batch_limit": 4,
            },
            ensure_ascii=False,
        )
    available_by_name = {candidate.name: candidate for candidate in available}
    if requested_names:
        missing = [name for name in requested_names if name not in available_by_name]
        if missing:
            return _failure(
                "no_extractable_have",
                "One or more requested local have proofs are not active extractable blocks.",
                theorem_id=theorem_id,
                missing_have_names=missing,
                available_have_names=[candidate.name for candidate in available],
            )
        selected_names = requested_names
    else:
        selected_names = tuple(
            candidate.name for candidate in ranked[: min(4, max(1, int(max_helpers or 1)))]
        )
    if not selected_names:
        return _failure(
            "no_extractable_have",
            "No complete top-level local have proof matched the extraction request.",
            theorem_id=theorem_id,
            have_name=have_name,
        )
    selected_names = tuple(
        candidate.name
        for candidate in sorted(
            (available_by_name[name] for name in selected_names),
            key=lambda candidate: candidate.start,
        )
    )[:4]
    requested_helper_names = {
        str(key or "").strip(): str(value or "").strip()
        for key, value in dict(helper_names or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    rewritten = declaration
    helpers: list[str] = []
    reports: list[dict[str, Any]] = []
    used_axioms: set[str] = set()
    for selected_name in selected_names:
        candidate = next(
            (item for item in candidates(rewritten) if item.name == selected_name),
            None,
        )
        if candidate is None:
            return _failure(
                "batch_candidate_changed",
                "A selected local have disappeared while planning the transactional batch.",
                theorem_id=theorem_id,
                have_name=selected_name,
                completed_plans=reports,
            )
        helper_name = _helper_name(
            theorem_id,
            candidate.name,
            source + "\n" + "\n\n".join(helpers),
            requested_name=requested_helper_names.get(candidate.name, ""),
        )
        instrumented = _instrumented_candidate(candidate, helper_name)
        truncated = _truncated_declaration(rewritten, candidate, instrumented)
        probe_replacement = "\n\n".join([*helpers, truncated])
        probe = lean_incremental_check(
            action="check_target",
            file_path=str(path),
            theorem_id=theorem_id,
            cwd=str(root),
            replacement=probe_replacement,
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
                completed_plans=reports,
            )
        helper = _private_helper(
            statement,
            candidate,
            context_prefix=rewritten[: candidate.start],
        )
        if not helper:
            return _failure(
                "goal_extraction_failed",
                "Lean emitted a helper signature in an unsupported shape.",
                theorem_id=theorem_id,
                have_name=candidate.name,
                extracted_statement=statement,
                completed_plans=reports,
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
                completed_plans=reports,
            )
        switched = _switched_candidate(candidate, helper_name)
        next_rewritten = rewritten[: candidate.start] + switched + rewritten[candidate.end :]
        prefix_check = lean_incremental_check(
            action="check_target",
            file_path=str(path),
            theorem_id=theorem_id,
            cwd=str(root),
            replacement="\n\n".join(
                [*helpers, helper, _truncated_declaration(rewritten, candidate, switched)]
            ),
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
                completed_plans=reports,
            )
        helpers.append(helper)
        rewritten = next_rewritten
        used_axioms.update(str(item) for item in helper_check.get("axiom_profile_axioms") or ())
        reports.append(
            {
                "have_name": candidate.name,
                "helper_name": helper_name,
                "extracted_lines": candidate.line_count,
                "extracted_chars": len(candidate.source),
                "helper_check": helper_check,
                "switch_prefix_check": prefix_check,
            }
        )
    combined = "\n\n".join([*helpers, rewritten])
    patch = _v4a_replace(path, declaration, combined)
    before_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    after_source = source.replace(declaration, combined, 1)
    after_sha256 = hashlib.sha256(after_source.encode("utf-8")).hexdigest()
    authority = verified_edit_authority.register(
        path=str(path),
        theorem_id=theorem_id,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        verified_declaration=str(reports[-1]["helper_name"]),
        axiom_profile_axioms=tuple(sorted(used_axioms)),
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
                "have_name": reports[0]["have_name"],
                "helper_name": reports[0]["helper_name"],
                "extracted_lines": sum(int(report["extracted_lines"]) for report in reports),
                "extracted_chars": sum(int(report["extracted_chars"]) for report in reports),
                "helper_count": len(reports),
                "helpers": reports,
                "transactional_batch": len(reports) > 1,
            }
        }
    )
    return json.dumps(result, ensure_ascii=False)
