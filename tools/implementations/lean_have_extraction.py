"""Extract a large local ``have`` into a verified private helper lemma.

The extraction is a bounded, fail-closed transaction:

1. Probe: re-elaborate the target up to the selected ``have`` with a ``run_tac``
   that logs the local context (names, kinds and universe levels) exactly as
   Mathlib's ``extract_goal`` will revert it, followed by ``extract_goal`` itself.
2. Build the helper: the printed statement becomes a ``private lemma`` whose
   body re-introduces every reverted local under its original name and then
   runs the original proof unchanged.
3. Verify: the helper must elaborate in the warm REPL, the rewritten call site
   (an explicit application of the helper) must elaborate, and the helper must
   pass the independent ephemeral compile with the axiom gate.
4. Bank: apply one verified patch that inserts the helper(s) and rewrites the
   target.

``extract_goal`` prints a binder-less Pi type whenever the goal starts with a
named ``∀``; both that shape and the binder form are handled through the
recorded context.  Extraction first uses ``extract_goal``'s relevance cleanup
and falls back to the full local context (``extract_goal *``) when the cleanup
drops a name the proof uses or the minimal helper does not elaborate.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core import verified_edit_authority
from leanflow_cli.lean.lean_have_extraction import (
    HaveCandidate,
    _mask_noncode,
    candidates,
    ranked_candidates,
)
from leanflow_cli.lean.lean_incremental import lean_incremental_check
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _declaration_matches_target,
)
from tools.implementations.lean_patch import apply_verified_patch_tool

CONTEXT_MARKER = "LEANFLOW_CTX"
FULL_CONTEXT_MARKER = "LEANFLOW_CTX_ALL"
LEVELS_MARKER = "LEANFLOW_LEVELS"
EXTRACTION_MODES: tuple[str, ...] = ("cleanup", "full_context")

_CONTEXT_ENTRY_RE = re.compile(r"(«[^»]*»|[^\s:]+):(var|hyp|let|inst)")
_INACCESSIBLE_MARK = "✝"
_BRACKETS = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_SIMPLE_INTRO_RE = re.compile(r"intro((?:\s+(?:[A-Za-z_«][\w'.«»]*|_))+)\s*$")


@dataclass(frozen=True)
class ContextEntry:
    """One local declaration as Lean reported it before ``extract_goal`` reverted it."""

    name: str
    kind: str  # var | hyp | let | inst


@dataclass(frozen=True)
class ExtractedContext:
    """Local context recorded by the probe for one extraction attempt."""

    entries: tuple[ContextEntry, ...]
    full: tuple[ContextEntry, ...]
    levels: tuple[str, ...]


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


def _is_accessible(name: str) -> bool:
    """Return whether a local name can be written in Lean source."""
    return bool(name) and _INACCESSIBLE_MARK not in name


def _context_dump_lines(mode: str) -> tuple[str, ...]:
    """Return the ``run_tac`` block that logs the local context for one mode.

    The block mirrors what Mathlib's ``extract_goal`` reverts: the cleaned
    context in ``cleanup`` mode (or the unchanged context when the goal is
    ``False``), and the whole context in ``full_context`` mode.  It also logs
    the universe level parameters of the reverted statement, which the Pi-form
    output does not declare.
    """
    cleanup = "pure g" if mode == "full_context" else "g.cleanup"
    return (
        "run_tac do",
        "  let g ← Lean.Elab.Tactic.getMainGoal",
        "  let dump : Lean.MVarId → Lean.MetaM (Array String) := fun g => g.withContext do",
        "    let lctx ← Lean.getLCtx",
        "    let mut out : Array String := #[]",
        "    for d? in lctx.decls.toList do",
        "      let some d := d? | continue",
        "      if d.isAuxDecl then continue",
        '      let kind ← if d.isLet then pure "let"',
        '        else if d.binderInfo.isInstImplicit then pure "inst"',
        '        else if ← Lean.Meta.isProp d.type then pure "hyp"',
        '        else pure "var"',
        '      let shown := s!"{d.userName.eraseMacroScopes}"',
        '        ++ (if d.userName.hasMacroScopes then "✝" else "")',
        '      out := out.push s!"{shown}:{kind}"',
        "    pure out",
        "  let all ← dump g",
        "  let (retained, levels) ← Lean.withoutModifyingState do",
        "    let isFalse := (← Lean.instantiateMVars (← g.getType)).consumeMData.isConstOf ``False",
        f"    let g' ← if isFalse then pure g else {cleanup}",
        "    let retained ← dump g'",
        "    let (_, g'') ← g'.revert (clearAuxDeclsInsteadOfRevert := true)"
        " (← g'.getDecl).lctx.getFVarIds",
        "    let ty ← Lean.instantiateMVars (← g''.getType)",
        "    let levels := (Lean.collectLevelParams {} ty).params.map toString",
        "    pure (retained, levels)",
        f'  Lean.logInfo m!"{FULL_CONTEXT_MARKER} {{String.intercalate " " all.toList}}"',
        f'  Lean.logInfo m!"{CONTEXT_MARKER} {{String.intercalate " " retained.toList}}"',
        f'  Lean.logInfo m!"{LEVELS_MARKER} {{String.intercalate " " levels.toList}}"',
    )


def _instrumented_candidate(candidate: HaveCandidate, helper_name: str, *, mode: str) -> str:
    """Insert the context dump and ``extract_goal`` before the original local proof."""
    proof = textwrap.dedent(candidate.proof).strip("\n")
    tactic_indent = candidate.indent + "  "
    dump = "\n".join(tactic_indent + line for line in _context_dump_lines(mode))
    star = " *" if mode == "full_context" else ""
    parts = [
        candidate.header,
        "\n" + dump,
        f"\n{tactic_indent}extract_goal{star} using {helper_name}",
    ]
    if proof.strip():
        parts.append("\n" + textwrap.indent(proof, tactic_indent))
    return "".join(parts)


def _truncated_declaration(
    declaration: str,
    candidate: HaveCandidate,
    replacement: str,
) -> str:
    """Close the target immediately after one candidate for bounded prefix checking."""
    return declaration[: candidate.start] + replacement + f"\n{candidate.indent}sorry"


def _message_texts(payload: Mapping[str, Any]) -> list[str]:
    """Return every diagnostic text of a check payload in order."""
    texts: list[str] = []
    for message in list(payload.get("messages") or []):
        text = (
            str(message.get("message", "") or "")
            if isinstance(message, Mapping)
            else str(message or "")
        )
        if text.strip():
            texts.append(text.strip())
    return texts


def _extracted_statement(payload: Mapping[str, Any], helper_name: str) -> str:
    """Return the standalone theorem emitted by Mathlib's ``extract_goal`` tactic."""
    for text in _message_texts(payload):
        if re.match(rf"^theorem\s+{re.escape(helper_name)}\b", text):
            return text
    output = str(payload.get("output", "") or "")
    match = re.search(rf"theorem\s+{re.escape(helper_name)}\b.*?:=\s*sorry", output, re.DOTALL)
    return match.group(0).strip() if match else ""


def _parse_context_entries(body: str) -> tuple[ContextEntry, ...]:
    return tuple(
        ContextEntry(name=match.group(1), kind=match.group(2))
        for match in _CONTEXT_ENTRY_RE.finditer(body)
    )


def _marker_body(text: str, marker: str) -> str | None:
    if text == marker:
        return ""
    if text.startswith(marker + " "):
        return text[len(marker) + 1 :]
    return None


def _extracted_context(payload: Mapping[str, Any]) -> ExtractedContext | None:
    """Return the local context the probe logged, or ``None`` when it is missing."""
    retained: tuple[ContextEntry, ...] | None = None
    full: tuple[ContextEntry, ...] | None = None
    levels: tuple[str, ...] = ()
    for text in _message_texts(payload):
        first_line = text.splitlines()[0] if text else ""
        full_body = _marker_body(first_line, FULL_CONTEXT_MARKER)
        if full_body is not None:
            full = _parse_context_entries(full_body)
            continue
        levels_body = _marker_body(first_line, LEVELS_MARKER)
        if levels_body is not None:
            levels = tuple(levels_body.split())
            continue
        retained_body = _marker_body(first_line, CONTEXT_MARKER)
        if retained_body is not None:
            retained = _parse_context_entries(retained_body)
    if retained is None or full is None:
        return None
    return ExtractedContext(entries=retained, full=full, levels=levels)


def _compact_diagnostics(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound an unbounded probe payload before returning it as failure evidence."""
    keep = (
        "success",
        "ok",
        "has_errors",
        "has_sorry",
        "timed_out",
        "error",
        "error_code",
        "backend",
        "action",
        "elapsed_s",
    )
    compact: dict[str, Any] = {key: payload.get(key) for key in keep if key in payload}
    texts = _message_texts(payload)
    compact["messages"] = [
        text if len(text) <= 1200 else text[:1200] + "…[truncated]" for text in texts[:12]
    ]
    compact["message_count"] = len(texts)
    return compact


def _mentioned_names(proof: str, names: Iterable[str]) -> set[str]:
    """Return the accessible local names that the proof text refers to."""
    masked = _mask_noncode(str(proof or ""))
    mentioned: set[str] = set()
    for name in names:
        if not _is_accessible(name):
            continue
        if re.search(rf"(?<![\w'.«»]){re.escape(name)}(?![\w'«»])", masked):
            mentioned.add(name)
    return mentioned


def _uses_classical(prefix: str) -> bool:
    """Return whether the proof enabled classical reasoning before the candidate."""
    return bool(re.search(r"(?<![\w'.])classical(?![\w'])", _mask_noncode(str(prefix or ""))))


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


def _declare_levels(statement: str, levels: Sequence[str]) -> str:
    """Add the universe binders that ``extract_goal``'s Pi-form output omits."""
    if not levels or re.match(r"^theorem\s+[^\s.{]+\.\{", statement):
        return statement
    if any(not re.fullmatch(r"[A-Za-z_][\w']*", level) for level in levels):
        return statement
    return re.sub(
        r"^(theorem\s+[^\s.{]+)",
        lambda match: f"{match.group(1)}.{{{', '.join(levels)}}}",
        statement,
        count=1,
    )


def _top_level_character(text: str, wanted: str, *, start: int = 0) -> int:
    """Return the first delimiter-free character offset in generated Lean text."""
    closing = set(_BRACKETS.values())
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
        if char in _BRACKETS:
            stack.append(_BRACKETS[char])
            continue
        if char in closing:
            if stack and char == stack[-1]:
                stack.pop()
            continue
        if char == wanted and not stack:
            return index
    return -1


def _group_end(text: str, start: int) -> int:
    """Return the offset of the bracket closing the group opened at ``start``."""
    closing = set(_BRACKETS.values())
    stack = [_BRACKETS[text[start]]]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
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
        if char in _BRACKETS:
            stack.append(_BRACKETS[char])
            continue
        if char in closing and stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index
    return -1


def _signature_binder_count(statement: str) -> int | None:
    """Count the locals that ``extract_goal`` printed as binders before the colon.

    ``extract_goal`` prints ``theorem name (a b : T) [inst : C] : result`` when the
    goal has no leading named ``∀``, and ``theorem name : ∀ ..., ...`` otherwise.
    Reverted locals appear as binders in local-context order, so this count says
    how many leading context entries need no ``intro`` in the helper body.
    """
    head = re.match(
        r"(?s)^(?:theorem|lemma|private lemma)\s+[^\s.{]+(?:\.\{[^{}]*\})?",
        statement,
    )
    if head is None:
        return None
    rest = statement[head.end() :]
    colon = _top_level_character(rest, ":")
    if colon < 0:
        return None
    binders = rest[:colon]
    count = 0
    index = 0
    while index < len(binders):
        char = binders[index]
        if char.isspace():
            index += 1
            continue
        if char not in _BRACKETS:
            return None
        end = _group_end(binders, index)
        if end < 0:
            return None
        group = binders[index + 1 : end]
        group_colon = _top_level_character(group, ":")
        if group_colon < 0:
            if char != "[":
                return None
            count += 1
        else:
            names = group[:group_colon].split()
            if not names:
                return None
            count += len(names)
        index = end + 1
    return count


def _private_helper(
    statement: str,
    candidate: HaveCandidate,
    context: ExtractedContext,
    *,
    classical: bool = False,
) -> str:
    """Combine the extracted signature with the original proof under its own names.

    Every reverted local that is not already a binder of the printed signature is
    re-introduced with ``intro`` under its original name, so the copied proof sees
    the same context it was written against.  Inaccessible names are introduced
    anonymously, exactly as they were inaccessible before.
    """
    declaration = _declare_levels(statement, context.levels)
    declaration = _freshen_explicit_universes(declaration)
    binder_count = _signature_binder_count(declaration)
    if binder_count is None or binder_count > len(context.entries):
        return ""
    declaration = re.sub(r"^theorem\b", "private lemma", declaration, count=1)
    declaration, replacement_count = re.subn(
        r":=\s*(?:by\s*)?sorry\s*$",
        ":= by",
        declaration,
    )
    if replacement_count != 1:
        return ""
    intro_names = [
        entry.name if _is_accessible(entry.name) else "_"
        for entry in context.entries[binder_count:]
    ]
    proof_lines = textwrap.dedent(candidate.proof).strip().splitlines()
    if intro_names and proof_lines:
        # Merge with the proof's own leading `intro` so the helper does not
        # trigger the duplicated-intro suggestion of the linter.
        merge = _SIMPLE_INTRO_RE.fullmatch(proof_lines[0].strip())
        if merge is not None and not proof_lines[0][:1].isspace():
            intro_names.extend(merge.group(1).split())
            proof_lines = proof_lines[1:]
    body: list[str] = []
    if classical:
        body.append("classical")
    if intro_names:
        body.append("intro " + " ".join(intro_names))
    body.extend(proof_lines)
    return declaration + ("\n" + textwrap.indent("\n".join(body), "  ") if body else "")


def _switched_candidate(
    candidate: HaveCandidate,
    helper_name: str,
    context: ExtractedContext,
) -> str:
    """Replace the local proof with an explicit application of the helper.

    The helper's binders are the reverted locals in context order, so the call
    passes them back positionally with ``@``.  Let-bound locals are ``let``
    binders of the helper type rather than arguments, instances are resolved
    again at the call site, and inaccessible hypotheses are recovered by
    ``assumption``.
    """
    arguments: list[str] = []
    for entry in context.entries:
        if entry.kind == "let":
            continue
        if _is_accessible(entry.name):
            arguments.append(entry.name)
        elif entry.kind == "inst":
            arguments.append("(by infer_instance)")
        elif entry.kind == "hyp":
            arguments.append("(by assumption)")
        else:
            arguments.append("_")
    call = " ".join([f"@{helper_name}", *arguments])
    return candidate.header + f"\n{candidate.indent}  exact {call}"


def _v4a_replace(path: Path, old: str, new: str) -> str:
    """Build one exact V4A replacement patch."""
    removed = "\n".join(f"-{line}" for line in old.splitlines())
    added = "\n".join(f"+{line}" for line in new.splitlines())
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n{removed}\n{added}\n*** End Patch"


def _check_failed(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("has_errors")) or bool(payload.get("timed_out"))


def _helper_elaborated(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("success") is True
        and payload.get("ok") is True
        and payload.get("valid_without_sorry") is True
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
    effective_timeout_s = max(1, int(timeout_s or 300))

    def check(**kwargs: Any) -> dict[str, Any]:
        return lean_incremental_check(
            file_path=str(path),
            theorem_id=theorem_id,
            cwd=str(root),
            timeout_s=effective_timeout_s,
            timeout_ceiling_s=effective_timeout_s,
            **kwargs,
        )

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
        classical = _uses_classical(rewritten[: candidate.start])
        attempts: list[dict[str, Any]] = []
        accepted: dict[str, Any] | None = None
        for mode in EXTRACTION_MODES:
            instrumented = _instrumented_candidate(candidate, helper_name, mode=mode)
            truncated = _truncated_declaration(rewritten, candidate, instrumented)
            probe = check(
                action="check_target",
                replacement="\n\n".join([*helpers, truncated]),
                allow_placeholders_for_elaboration=True,
                preserve_diagnostics=True,
            )
            statement = _extracted_statement(probe, helper_name)
            context = _extracted_context(probe)
            if _check_failed(probe) or not statement or context is None:
                return _failure(
                    "goal_extraction_failed",
                    "Lean did not emit a standalone theorem signature and local context "
                    "for the selected have.",
                    theorem_id=theorem_id,
                    have_name=candidate.name,
                    extraction_mode=mode,
                    diagnostics=_compact_diagnostics(probe),
                    attempts=attempts,
                    completed_plans=reports,
                )
            if mode == "cleanup":
                retained = {item.name for item in context.entries}
                dropped = sorted(
                    _mentioned_names(candidate.proof, [item.name for item in context.full])
                    - retained
                )
                if dropped:
                    attempts.append(
                        {
                            "extraction_mode": mode,
                            "status": "cleanup_dropped_used_names",
                            "dropped_names": dropped,
                        }
                    )
                    continue
            helper = _private_helper(statement, candidate, context, classical=classical)
            if not helper:
                return _failure(
                    "goal_extraction_failed",
                    "Lean emitted a helper signature in an unsupported shape.",
                    theorem_id=theorem_id,
                    have_name=candidate.name,
                    extraction_mode=mode,
                    extracted_statement=statement,
                    context=[asdict(item) for item in context.entries],
                    attempts=attempts,
                    completed_plans=reports,
                )
            elaboration = check(action="check_helper", replacement=helper)
            if _helper_elaborated(elaboration):
                accepted = {
                    "extraction_mode": mode,
                    "statement": statement,
                    "context": context,
                    "helper": helper,
                    "elaboration_check": elaboration,
                }
                break
            attempts.append(
                {
                    "extraction_mode": mode,
                    "status": "helper_elaboration_failed",
                    "helper": helper,
                    "diagnostics": elaboration,
                }
            )
        if accepted is None:
            return _failure(
                "helper_verification_failed",
                "The extracted helper did not elaborate in any extraction mode.",
                theorem_id=theorem_id,
                have_name=candidate.name,
                helper_name=helper_name,
                diagnostics=(attempts[-1].get("diagnostics") if attempts else None),
                attempts=attempts,
                completed_plans=reports,
            )
        helper = str(accepted["helper"])
        context = accepted["context"]
        switched = _switched_candidate(candidate, helper_name, context)
        next_rewritten = rewritten[: candidate.start] + switched + rewritten[candidate.end :]
        prefix_check = check(
            action="check_target",
            replacement="\n\n".join(
                [*helpers, helper, _truncated_declaration(rewritten, candidate, switched)]
            ),
            allow_placeholders_for_elaboration=True,
        )
        if _check_failed(prefix_check):
            return _failure(
                "helper_switch_failed",
                "The private helper elaborated, but replacing the local have did not.",
                theorem_id=theorem_id,
                have_name=candidate.name,
                helper_name=helper_name,
                extraction_mode=accepted["extraction_mode"],
                diagnostics=prefix_check,
                attempts=attempts,
                completed_plans=reports,
            )
        helper_check = check(
            action="check_helper",
            replacement=helper,
            include_axiom_profile=True,
        )
        blockers = list(helper_check.get("axiom_profile_blockers") or [])
        if not (
            _helper_elaborated(helper_check)
            and helper_check.get("axiom_profile_checked") is True
            and not blockers
        ):
            return _failure(
                "helper_verification_failed",
                "The extracted helper did not pass its independent LeanProbe and axiom gates.",
                theorem_id=theorem_id,
                have_name=candidate.name,
                helper_name=helper_name,
                extraction_mode=accepted["extraction_mode"],
                diagnostics=helper_check,
                attempts=attempts,
                completed_plans=reports,
            )
        helpers.append(helper)
        rewritten = next_rewritten
        used_axioms.update(str(item) for item in helper_check.get("axiom_profile_axioms") or ())
        reports.append(
            {
                "have_name": candidate.name,
                "helper_name": helper_name,
                "extraction_mode": accepted["extraction_mode"],
                "extracted_statement": accepted["statement"],
                "context": [asdict(item) for item in context.entries],
                "extracted_lines": candidate.line_count,
                "extracted_chars": len(candidate.source),
                "attempts": attempts,
                "helper_elaboration_check": accepted["elaboration_check"],
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
            timeout_s=effective_timeout_s,
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
                "extraction_modes": [str(report["extraction_mode"]) for report in reports],
                "helpers": reports,
                "transactional_batch": len(reports) > 1,
            }
        }
    )
    return json.dumps(result, ensure_ascii=False)
