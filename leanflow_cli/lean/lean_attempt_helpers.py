"""Validate multi-attempt candidates and normalize Lean source text.

These side-effect-free helpers summarize diagnostics, strip comments and
strings, and normalize diff-style paths for ``lean_services``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MULTI_ATTEMPT_MIN_CANDIDATES = 2
MULTI_ATTEMPT_MAX_CANDIDATES = 6
MULTI_ATTEMPT_MAX_LINES = 12
MULTI_ATTEMPT_MAX_CHARS = 700


def _strip_diff_path_prefix(file_path: str) -> str:
    normalized = str(file_path or "").strip()
    if normalized.startswith("a//") or normalized.startswith("b//"):
        return normalized[2:]
    return normalized


def _strip_comments_and_strings(text: str) -> str:
    text = re.sub(r"/-.*?-/", "", text, flags=re.DOTALL)
    text = re.sub(r"--.*", "", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return text


def _summarize_attempt_diagnostics(attempts: list[dict[str, Any]]) -> list[str]:
    summaries: list[str] = []
    for attempt in attempts:
        diagnostics = attempt.get("diagnostics")
        if not isinstance(diagnostics, list):
            continue
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, Mapping):
                continue
            message = " ".join(str(diagnostic.get("message", "") or "").split()).strip()
            if message:
                summaries.append(message[:220])
                break
        if len(summaries) >= 2:
            break
    return summaries


def _normalize_multi_attempt_candidates(attempts: list[str]) -> list[str]:
    """Return non-empty, trimmed, order-preserving distinct tactic candidates."""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in list(attempts or []):
        text = str(item or "").strip()
        if text and text not in seen:
            normalized.append(text)
            seen.add(text)
    return normalized


def _multi_attempt_validation_reasons(attempts: list[str]) -> list[str]:
    reasons: list[str] = []
    count = len(attempts)
    if count < MULTI_ATTEMPT_MIN_CANDIDATES or count > MULTI_ATTEMPT_MAX_CANDIDATES:
        reasons.append(
            f"lean_multi_attempt expects {MULTI_ATTEMPT_MIN_CANDIDATES}-{MULTI_ATTEMPT_MAX_CANDIDATES} concrete tactic candidates at one proof location"
        )
    declaration_pattern = re.compile(r"^\s*(theorem|lemma|example|def|instance|class|structure)\b")
    local_proof_block_pattern = re.compile(r"^\s*(?:have|suffices)\b[^\n]*?(?::=|:)\s*by\s*$")
    for snippet in attempts:
        sanitized = _strip_comments_and_strings(snippet)
        if re.search(r"\bsorry\b", sanitized):
            reasons.append("lean_multi_attempt candidates must not contain `sorry`")
            break
    for snippet in attempts:
        lines = [line for line in str(snippet).splitlines() if line.strip()]
        if (
            len(str(snippet)) > MULTI_ATTEMPT_MAX_CHARS
            or len(lines) > MULTI_ATTEMPT_MAX_LINES
            or declaration_pattern.match(str(snippet))
            or (len(lines) >= 3 and bool(lines) and local_proof_block_pattern.match(lines[0]))
        ):
            reasons.append(
                "lean_multi_attempt expects short local tactic candidates, not full proof blocks"
            )
            break
    return list(dict.fromkeys(reasons))
