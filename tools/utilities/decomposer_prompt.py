"""Build target-scoped, hard-bounded Lean decomposition prompts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.lean.lean_parsing import _find_assignment_marker_for_statement
from tools.utilities.decomposer_source_guard import DecomposerSourceContext, SourceConstraint

DECOMPOSER_USER_PROMPT_MAX_CHARS = 52_000
THEOREM_STATEMENT_MAX_CHARS = 14_000
DIAGNOSTICS_MAX_CHARS = 6_000
GOALS_MAX_CHARS = 4_000
CURRENT_ATTEMPT_MAX_CHARS = 6_000
FAILED_ATTEMPTS_MAX_CHARS = 10_000
SOURCE_CONSTRAINTS_MAX_CHARS = 5_000

_KNOWN_LINTER_NOISE_RE = re.compile(
    r"(?:Missing AMS attribute|Missing problem category attribute|"
    r"linter\.style\.(?:ams_attribute|category_attribute))",
    flags=re.IGNORECASE,
)
_GOAL_SIGNAL_RE = re.compile(
    r"(?:unsolved goal|type mismatch|declaration uses ['`]?sorry|unknown identifier|"
    r"failed to synthesize|application type mismatch)",
    flags=re.IGNORECASE,
)
_NEGATIVE_EVIDENCE_RE = re.compile(
    r"(?:counterexample|negative evidence|do_not_imply_false|does not imply false|"
    r"consistent terminal|noncoverage|non-coverage|obstruction|contradicted by source)",
    flags=re.IGNORECASE,
)
_ATTEMPT_START_RE = re.compile(r"(?m)(?=^- attempt:\s*)")


@dataclass(frozen=True)
class DecomposerPromptContext:
    """Hold the bounded model-facing sections and shaping telemetry."""

    theorem_statement: str
    current_diagnostics: str
    current_goals: str
    current_attempt: str
    recent_failed_attempts: str
    source_constraints: str
    source_availability: str
    stats: dict[str, Any]


def _bounded_text(value: Any, limit: int) -> str:
    """Return text within a hard character limit while preserving both ends."""
    text = str(value or "").strip()
    cap = max(0, int(limit))
    if len(text) <= cap:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    marker = f"\n...[bounded; sha256={digest}; original_chars={len(text)}]...\n"
    if cap <= len(marker):
        return marker[:cap]
    remaining = cap - len(marker)
    head = remaining // 2
    return text[:head] + marker + text[-(remaining - head) :]


def _json_mapping(text: str) -> dict[str, Any] | None:
    """Return a JSON object from a plain or fenced diagnostic payload."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _diagnostic_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return structured diagnostic items without retaining their container."""
    items: list[dict[str, Any]] = []
    for key in ("messages", "items", "diagnostics"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(dict(item) for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            nested = value.get("items") or value.get("messages")
            if isinstance(nested, list):
                items.extend(dict(item) for item in nested if isinstance(item, Mapping))
    return items


def _line_number(item: Mapping[str, Any]) -> int:
    """Return one diagnostic's one-based source line when available."""
    value = item.get("line")
    if value is None and isinstance(item.get("location"), Mapping):
        value = dict(item["location"]).get("line")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _shape_structured_diagnostics(
    payload: Mapping[str, Any],
    *,
    theorem_id: str,
    target_start_line: int,
    target_end_line: int,
) -> tuple[str, int]:
    """Keep errors and target-local diagnostics while dropping file-wide lint noise."""
    relevant: list[dict[str, Any]] = []
    omitted = 0
    target_leaf = str(theorem_id or "").rsplit(".", 1)[-1].casefold()
    for item in _diagnostic_items(payload):
        severity = str(item.get("severity", "") or "").strip().casefold()
        message = str(item.get("message", "") or item.get("text", "") or "").strip()
        line = _line_number(item)
        target_line = bool(
            line
            and target_start_line
            and target_start_line <= line <= max(target_start_line, target_end_line)
        )
        target_name = bool(target_leaf and target_leaf in message.casefold())
        important = (
            severity == "error"
            or target_line
            or target_name
            or bool(_GOAL_SIGNAL_RE.search(message))
        )
        if severity != "error" and _KNOWN_LINTER_NOISE_RE.search(message):
            omitted += 1
            continue
        if not important:
            omitted += 1
            continue
        relevant.append(
            {
                "severity": severity or "unknown",
                "message": _bounded_text(message, 500),
                **({"line": line} if line else {}),
                **({"column": item.get("column")} if item.get("column") is not None else {}),
            }
        )

    relevant.sort(
        key=lambda item: (
            0 if item.get("severity") == "error" else 1,
            int(item.get("line", 0) or 0),
        )
    )
    if len(relevant) > 8:
        omitted += len(relevant) - 8
        relevant = relevant[:8]
    summary: dict[str, Any] = {}
    for key in ("ok", "success", "error_count", "warning_count", "sorry"):
        value = payload.get(key)
        if isinstance(value, (bool, int, float)) or value is None:
            if key in payload:
                summary[key] = value
        elif isinstance(value, str):
            summary[key] = _bounded_text(value, 200)
    for key, count_key in (("errors", "error_count"), ("warnings", "warning_count")):
        value = payload.get(key)
        if isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = _bounded_text(value, 200)
        elif isinstance(value, (list, tuple)):
            summary.setdefault(count_key, len(value))
    failed_dependencies = payload.get("failed_dependencies")
    if isinstance(failed_dependencies, list) and failed_dependencies:
        summary["failed_dependencies"] = [
            _bounded_text(item, 300) for item in failed_dependencies[:8]
        ]
    summary.update(
        {
            "target_scope": {
                "theorem": theorem_id,
                "start_line": target_start_line,
                "end_line": target_end_line,
            },
            "relevant_messages": relevant,
            "omitted_unrelated_messages": omitted,
        }
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    return _bounded_text(rendered, DIAGNOSTICS_MAX_CHARS), omitted


def _shape_raw_diagnostics(text: str, *, theorem_id: str) -> tuple[str, int]:
    """Select useful lines from an unstructured diagnostic string."""
    selected: list[str] = []
    omitted = 0
    target_leaf = str(theorem_id or "").rsplit(".", 1)[-1].casefold()
    for line in str(text or "").splitlines():
        if _KNOWN_LINTER_NOISE_RE.search(line):
            omitted += 1
            continue
        lowered = line.casefold()
        if (
            "error" in lowered
            or (target_leaf and target_leaf in lowered)
            or _GOAL_SIGNAL_RE.search(line)
            or _NEGATIVE_EVIDENCE_RE.search(line)
        ):
            selected.append(line)
        elif line.strip():
            omitted += 1
    rendered = "\n".join(selected)
    if omitted:
        rendered += f"\n[omitted {omitted} unrelated diagnostic line(s)]"
    return _bounded_text(rendered, DIAGNOSTICS_MAX_CHARS), omitted


def shape_diagnostics(
    text: str,
    *,
    theorem_id: str,
    target_start_line: int,
    target_end_line: int,
) -> tuple[str, int]:
    """Return target-scoped diagnostics and an omitted-message count."""
    payload = _json_mapping(text)
    if payload is not None:
        return _shape_structured_diagnostics(
            payload,
            theorem_id=theorem_id,
            target_start_line=target_start_line,
            target_end_line=target_end_line,
        )
    return _shape_raw_diagnostics(text, theorem_id=theorem_id)


def _shape_theorem_statement(statement: str) -> str:
    """Keep the exact target signature while dropping its stale proof body."""
    text = str(statement or "").strip()
    assignment = _find_assignment_marker_for_statement(text)
    signature = text[:assignment].rstrip() if assignment >= 0 else text
    return _bounded_text(signature, THEOREM_STATEMENT_MAX_CHARS)


def _attempt_blocks(text: str) -> tuple[str, list[str]]:
    """Split a persisted failed-attempt summary into header and attempt blocks."""
    raw = str(text or "").strip()
    first = re.search(r"(?m)^- attempt:\s*", raw)
    if first is None:
        return "", []
    header = raw[: first.start()].strip()
    blocks = [
        block.strip() for block in _ATTEMPT_START_RE.split(raw[first.start() :]) if block.strip()
    ]
    return header, blocks


def shape_failed_attempts(text: str) -> tuple[str, int]:
    """Keep recent attempts plus older explicit negative evidence under a hard cap."""
    header, blocks = _attempt_blocks(text)
    if not blocks:
        bounded = _bounded_text(text, FAILED_ATTEMPTS_MAX_CHARS)
        return bounded, int(bool(text) and len(str(text)) > len(bounded))
    recent_indices = set(range(max(0, len(blocks) - 4), len(blocks)))
    negative_indices = [
        index
        for index, block in enumerate(blocks)
        if index not in recent_indices and _NEGATIVE_EVIDENCE_RE.search(block)
    ][-2:]
    selected_indices = sorted(recent_indices | set(negative_indices))
    omitted = len(blocks) - len(selected_indices)
    parts = [_bounded_text(header, 900)] if header else []
    if omitted:
        parts.append(f"[omitted {omitted} older non-negative attempt(s)]")
    parts.extend(_bounded_text(blocks[index], 1_450) for index in selected_indices)
    return _bounded_text("\n".join(parts), FAILED_ATTEMPTS_MAX_CHARS), omitted


def _render_source_constraints(constraints: Sequence[SourceConstraint]) -> str:
    """Render sorry-free target constraints without their proof bodies."""
    parts: list[str] = []
    for constraint in constraints[:3]:
        parts.append(
            f"- {constraint.name} [{constraint.kind}; sha256={constraint.declaration_sha256}]:\n"
            f"  {_bounded_text(constraint.statement, 1_250)}"
        )
    return _bounded_text("\n".join(parts), SOURCE_CONSTRAINTS_MAX_CHARS)


def _render_source_availability(
    source_context: DecomposerSourceContext,
    evidence: str,
) -> tuple[str, int]:
    """Render mentioned declarations from the authoritative current source index."""
    lines = str(evidence or "").splitlines()
    rendered: list[str] = []
    stale_claim_count = 0
    for declaration in source_context.declarations:
        names = tuple(
            dict.fromkeys(
                name
                for name in (declaration.full_name, declaration.name)
                if str(name or "").strip()
            )
        )
        matching_lines = [
            line
            for line in lines
            if any(re.search(rf"(?<![\w']){re.escape(name)}(?![\w'])", line) for name in names)
        ]
        if not matching_lines:
            continue
        placeholder_status = (
            "contains a placeholder"
            if declaration.has_placeholder
            else "present without placeholders"
        )
        rendered.append(
            f"- `{declaration.full_name}`: {placeholder_status} at current-source "
            f"lines {declaration.start_line}-{declaration.end_line}"
        )
        if not declaration.has_placeholder and any(
            re.search(
                r"(?:not\s+yet\s+(?:banked|inserted|promoted)|"
                r"not\s+(?:banked|inserted|present)|unbanked)",
                line,
                flags=re.IGNORECASE,
            )
            for line in matching_lines
        ):
            stale_claim_count += 1
    if not rendered:
        return "", 0
    header = (
        "Current source declaration index (authoritative for presence; overrides stale "
        "narrative absence or integration claims):"
    )
    return _bounded_text("\n".join([header, *rendered[:16]]), 5_000), stale_claim_count


def shape_decomposer_prompt_context(
    *,
    theorem_id: str,
    theorem_statement: str,
    current_diagnostics: str,
    current_goals: str,
    current_attempt: str,
    recent_failed_attempts: str,
    source_context: DecomposerSourceContext,
) -> DecomposerPromptContext:
    """Build every bounded optional section for one decomposition request."""
    diagnostics, omitted_diagnostics = shape_diagnostics(
        current_diagnostics,
        theorem_id=theorem_id,
        target_start_line=source_context.target_start_line,
        target_end_line=source_context.target_end_line,
    )
    attempts, omitted_attempts = shape_failed_attempts(recent_failed_attempts)
    availability, stale_status_claims = _render_source_availability(
        source_context,
        "\n".join(
            part
            for part in (
                current_diagnostics,
                current_goals,
                current_attempt,
                recent_failed_attempts,
            )
            if part
        ),
    )
    context = DecomposerPromptContext(
        theorem_statement=_shape_theorem_statement(theorem_statement),
        current_diagnostics=diagnostics,
        current_goals=_bounded_text(current_goals, GOALS_MAX_CHARS),
        current_attempt=_bounded_text(current_attempt, CURRENT_ATTEMPT_MAX_CHARS),
        recent_failed_attempts=attempts,
        source_constraints=_render_source_constraints(source_context.constraints),
        source_availability=availability,
        stats={},
    )
    stats = {
        "target_start_line": source_context.target_start_line,
        "target_end_line": source_context.target_end_line,
        "source_status": source_context.status,
        "source_constraint_count": len(source_context.constraints),
        "omitted_diagnostic_count": omitted_diagnostics,
        "omitted_failed_attempt_count": omitted_attempts,
        "stale_source_status_claim_count": stale_status_claims,
        "section_chars": {
            "theorem_statement": len(context.theorem_statement),
            "current_diagnostics": len(context.current_diagnostics),
            "current_goals": len(context.current_goals),
            "current_attempt": len(context.current_attempt),
            "recent_failed_attempts": len(context.recent_failed_attempts),
            "source_constraints": len(context.source_constraints),
            "source_availability": len(context.source_availability),
        },
    }
    return DecomposerPromptContext(
        theorem_statement=context.theorem_statement,
        current_diagnostics=context.current_diagnostics,
        current_goals=context.current_goals,
        current_attempt=context.current_attempt,
        recent_failed_attempts=context.recent_failed_attempts,
        source_constraints=context.source_constraints,
        source_availability=context.source_availability,
        stats=stats,
    )


def compose_decomposer_user_prompt(
    *,
    context: DecomposerPromptContext,
    file_path: str,
    theorem_id: str,
    cwd: str,
    max_helper_count: int,
    question: str,
    json_contract: str,
    source_declarations: str = "",
) -> tuple[str, dict[str, Any]]:
    """Compose the final user prompt while preserving its question and JSON contract."""
    prefix_parts = [
        f"File: {_bounded_text(file_path, 1_000)}",
        f"Theorem: {_bounded_text(theorem_id, 500)}",
        f"Working directory: {_bounded_text(cwd, 1_000)}" if cwd else "",
        f"Maximum helper count: {max_helper_count}",
        (
            f"Theorem statement/signature:\n{context.theorem_statement}"
            if context.theorem_statement
            else ""
        ),
        (
            "Source-backed negative/consistency constraints (sorry-free declarations):\n"
            f"{context.source_constraints}"
            if context.source_constraints
            else ""
        ),
        (
            "Authoritative referenced in-file declarations:\n"
            f"{_bounded_text(source_declarations, 16_000)}"
            if source_declarations
            else ""
        ),
        context.source_availability,
        (
            f"Target-scoped current diagnostics:\n{context.current_diagnostics}"
            if context.current_diagnostics
            else ""
        ),
        f"Current goals:\n{context.current_goals}" if context.current_goals else "",
        f"Current attempt:\n{context.current_attempt}" if context.current_attempt else "",
        (
            f"Relevant recent failed attempts:\n{context.recent_failed_attempts}"
            if context.recent_failed_attempts
            else ""
        ),
    ]
    tail_parts = [
        (
            f"Question:\n{_bounded_text(question, 2_000)}"
            if question
            else "Question:\nDecompose this hard proof into helper lemmas that the main agent can insert and prove one at a time."
        ),
        f"Required JSON shape:\n{_bounded_text(json_contract, 3_000)}",
    ]
    prefix = "\n\n".join(part for part in prefix_parts if part)
    tail = "\n\n".join(tail_parts)
    available = max(0, DECOMPOSER_USER_PROMPT_MAX_CHARS - len(tail) - 2)
    bounded_prefix = _bounded_text(prefix, available)
    prompt = f"{bounded_prefix}\n\n{tail}" if bounded_prefix else tail
    stats = dict(context.stats)
    stats.update(
        {
            "user_prompt_chars": len(prompt),
            "user_prompt_max_chars": DECOMPOSER_USER_PROMPT_MAX_CHARS,
            "hard_cap_applied": len(prefix) > available,
        }
    )
    return prompt, stats
