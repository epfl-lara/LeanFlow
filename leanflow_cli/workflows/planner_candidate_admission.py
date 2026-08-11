"""Quarantine explicitly uncertain planner candidates before they become work."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

INTERRUPTED_EVIDENCE_STATUSES = frozenset(
    {"aborted", "canceled", "cancelled", "interrupted", "killed", "stopped"}
)

# These expressions require an explicit self-disqualification in advisory
# metadata. Ordinary hedging ("try", "candidate", "possibly useful") is not
# enough to discard an idea, and statements are intentionally excluded because
# every draft declaration contains a proof placeholder by construction.
_UNCERTAINTY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "needs-checking",
        re.compile(
            r"\b(?:needs?|requires?)\s+"
            r"(?:(?:explicit|further|independent|separate)\s+)?"
            r"(?:checking|verification|validation)\b|"
            r"\b(?:must|should)\s+be\s+(?:checked|verified|validated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "conditional-revision",
        re.compile(
            r"\bif\b[^.;\n]{0,240}\b"
            r"(?:must\s+be|needs?\s+(?:to\s+)?be)\s+"
            r"(?:adjusted|changed|replaced)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unverified",
        re.compile(
            r"\b(?:unchecked|unverified|unvalidated)\b|"
            r"\bnot\s+(?:yet\s+)?(?:checked|verified|validated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "placeholder",
        re.compile(r"(?<!\bno )(?<!\bnot a )\bplaceholder\b", re.IGNORECASE),
    ),
    (
        "conditional-failure",
        re.compile(
            r"\bif\s+(?:(?:it|this|that)\s+|the\s+"
            r"(?:claim|candidate|construction|divisibility|formula|identity|witness)\s+)"
            r"fails?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "uncertain-validity",
        re.compile(
            r"\b(?:may|might|could)\s+(?:be\s+)?(?:fail(?:ing)?|false|incorrect|invalid)\b|"
            r"\b(?:known\s+false|counterexample\s+found|refuted|contradicted\s+by)\b",
            re.IGNORECASE,
        ),
    ),
)


def explicit_uncertainty_evidence(text: str) -> tuple[dict[str, str], ...]:
    """Return stable evidence for explicit advisory self-disqualification."""
    compact = " ".join(str(text or "").split())
    if not compact:
        return ()
    evidence: list[dict[str, str]] = []
    for kind, pattern in _UNCERTAINTY_PATTERNS:
        match = pattern.search(compact)
        if match is not None:
            evidence.append({"kind": kind, "matched": match.group(0)})
    return tuple(evidence)


def candidate_uncertainty_evidence(candidate: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Return explicit uncertainty found only in one candidate's metadata."""
    evidence: list[dict[str, str]] = []
    for field in ("notes", "reason"):
        for item in explicit_uncertainty_evidence(str(candidate.get(field, "") or "")):
            evidence.append({"field": field, **item})
    return tuple(evidence)


def partition_synthesis_nodes(
    nodes: Sequence[Any],
) -> tuple[list[Any], tuple[dict[str, Any], ...]]:
    """Separate admissible synthesis nodes from explicitly unchecked ones."""
    admitted: list[Any] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(nodes):
        if not isinstance(raw, Mapping):
            admitted.append(raw)
            continue
        evidence = candidate_uncertainty_evidence(raw)
        if not evidence:
            admitted.append(raw)
            continue
        rejected.append(
            {
                "index": index,
                "name": str(raw.get("name", "") or ""),
                "evidence": [dict(item) for item in evidence],
            }
        )
    return admitted, tuple(rejected)


def interrupted_lane_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """Return requested evidence lanes that ended through cancellation."""
    interrupted: list[dict[str, str]] = []
    for record in records:
        status = str(record.get("status", "") or "").strip().casefold()
        if status not in INTERRUPTED_EVIDENCE_STATUSES:
            continue
        interrupted.append(
            {
                "lane": str(record.get("lane", "") or ""),
                "status": status,
            }
        )
    return tuple(interrupted)
