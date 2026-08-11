"""Bound research-orchestrator context while preserving target and error truth."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

RESEARCH_LLM_PROMPT_MAX_CHARS = 12_000
RESEARCH_LLM_TELEMETRY_RESERVE_CHARS = 2_400

# These are content ceilings, not targets. The global cap still decides how
# much optional history fits after the declaration, diagnostics, and reply
# contract have reserved their space.
RESEARCH_SECTION_MAX_CHARS = {
    "target_statement": 1_800,
    "diagnostics": 1_600,
    "floor_decision": 700,
    "target_graph_frontier": 700,
    "campaign_global_frontier": 300,
    "graph_blocked": 400,
    "route_portfolio": 500,
    "decision_packet": 800,
    "verified_graph_facts": 1_400,
    "failed_route_signatures": 1_000,
    "research_findings": 1_600,
    "plan_generated_view": 1_000,
    "phase_policy": 800,
}

_DIAGNOSTIC_PRIORITY_RE = re.compile(
    r"(?:\berror\b|unsolved goals?|\bsorry\b|\bfailed\b|exception|traceback)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class PromptSection:
    """Describe one independently budgeted prompt section."""

    name: str
    heading: str
    content: str
    source_text: str
    max_chars: int
    required: bool = False
    original_items: int = 0


@dataclass(frozen=True)
class ResearchPromptRender:
    """Return one capped prompt and its full-source omission telemetry."""

    prompt: str
    telemetry: tuple[dict[str, Any], ...]
    hard_cap_applied: bool


def _sha256(text: str) -> str:
    """Return a stable digest for the complete pre-projection section."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_excerpt(text: str, limit: int) -> str:
    """Retain a section's beginning and conclusion inside one character cap."""
    source = str(text or "")
    bounded_limit = max(0, int(limit))
    if len(source) <= bounded_limit:
        return source
    if not bounded_limit:
        return ""
    marker = (
        "\n... [middle omitted; " f"sha256={_sha256(source)}; original_chars={len(source)}] ...\n"
    )
    if bounded_limit <= len(marker) + 24:
        return source[:bounded_limit]
    available = bounded_limit - len(marker)
    head = available // 3
    tail = available - head
    return f"{source[:head]}{marker}{source[-tail:]}"


def diagnostics_projection(diagnostics: str, *, max_chars: int) -> str:
    """Prioritize error-bearing lines before bounding diagnostic context."""
    source = str(diagnostics or "")
    if not source:
        return ""
    lines = source.splitlines()
    priority = [line for line in lines if _DIAGNOSTIC_PRIORITY_RE.search(line)]
    if not priority:
        return _bounded_excerpt(source, max_chars)
    priority_lines = set(priority)
    context = [line for line in lines if line not in priority_lines]
    projected = "Priority target/error diagnostics:\n" + "\n".join(priority)
    if context:
        projected += "\nDiagnostic context:\n" + "\n".join(context)
    return _bounded_excerpt(projected, max_chars)


def json_source(value: Any) -> str:
    """Return deterministic, Unicode-preserving JSON for digest accounting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _section_block(section: PromptSection, content: str) -> str:
    """Render one non-empty section with its stable heading."""
    return f"{section.heading}:\n{content}" if content else ""


def render_research_prompt(
    *,
    prefix: str,
    sections: tuple[PromptSection, ...],
    suffix: str,
    max_chars: int = RESEARCH_LLM_PROMPT_MAX_CHARS,
) -> ResearchPromptRender:
    """Render a hard-capped prompt with per-section digest telemetry.

    Required sections reserve space first. Optional target-scoped histories
    then consume the remaining budget in caller priority order. The complete
    source of every history is represented by its SHA-256 and omission counts,
    even when none of its prose fits.
    """
    hard_cap = max(1_000, int(max_chars))
    telemetry_heading = "Context omission telemetry (full-source digests):"
    separator_allowance = 2 * (len(sections) + 4)
    heading_allowance = sum(len(section.heading) + 2 for section in sections)
    remaining = max(
        0,
        hard_cap
        - len(prefix)
        - len(suffix)
        - len(telemetry_heading)
        - RESEARCH_LLM_TELEMETRY_RESERVE_CHARS
        - separator_allowance
        - heading_allowance,
    )

    desired = {
        section.name: _bounded_excerpt(section.content, section.max_chars) for section in sections
    }
    allocations: dict[str, str] = {}
    for required in (True, False):
        for section in sections:
            if section.required is not required:
                continue
            candidate = desired[section.name]
            if not candidate or remaining <= 0:
                allocations[section.name] = ""
                continue
            if len(candidate) <= remaining:
                allocations[section.name] = candidate
                remaining -= len(candidate)
                continue
            minimum = 160 if not required else min(320, remaining)
            if remaining < minimum:
                allocations[section.name] = ""
                continue
            allocations[section.name] = _bounded_excerpt(candidate, remaining)
            remaining = 0

    telemetry: list[dict[str, Any]] = []
    blocks: list[str] = []
    hard_cap_applied = False
    for section in sections:
        included = allocations.get(section.name, "")
        block = _section_block(section, included)
        if block:
            blocks.append(block)
        original_chars = len(section.source_text)
        included_chars = min(original_chars, len(included))
        omitted_chars = max(0, original_chars - included_chars)
        truncated = omitted_chars > 0
        hard_cap_applied = hard_cap_applied or truncated
        if section.original_items or truncated:
            telemetry.append(
                {
                    "included_chars": included_chars,
                    "included_items": (section.original_items if not truncated and included else 0),
                    "omitted_chars": omitted_chars,
                    "omitted_items": (0 if not truncated and included else section.original_items),
                    "original_chars": original_chars,
                    "original_items": section.original_items,
                    "section": section.name,
                    "sha256": _sha256(section.source_text),
                }
            )

    telemetry_json = json.dumps(telemetry, ensure_ascii=False, separators=(",", ":"))
    if len(telemetry_json) > RESEARCH_LLM_TELEMETRY_RESERVE_CHARS:
        # Section count is structurally bounded, but keep the prompt total even
        # if a future caller adds verbose section names.
        compact = [
            {
                "section": row["section"],
                "original_chars": row["original_chars"],
                "omitted_chars": row["omitted_chars"],
                "original_items": row["original_items"],
                "sha256": row["sha256"],
            }
            for row in telemetry
        ]
        telemetry_json = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    parts = [prefix, *blocks, f"{telemetry_heading}\n{telemetry_json}", suffix]
    prompt = "\n\n".join(part for part in parts if part)
    if len(prompt) > hard_cap:
        # The reserve above covers the fixed current section inventory. Fail
        # closed on a future oversized contract rather than silently cutting
        # its JSON schema or target truth.
        raise ValueError(
            f"research orchestrator prompt budget invariant failed: {len(prompt)} > {hard_cap}"
        )
    return ResearchPromptRender(
        prompt=prompt,
        telemetry=tuple(telemetry),
        hard_cap_applied=hard_cap_applied,
    )


__all__ = [
    "PromptSection",
    "RESEARCH_LLM_PROMPT_MAX_CHARS",
    "RESEARCH_SECTION_MAX_CHARS",
    "ResearchPromptRender",
    "diagnostics_projection",
    "json_source",
    "render_research_prompt",
]
