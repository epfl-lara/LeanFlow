"""Rank queue targets using structured, target-scoped research evidence."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from leanflow_cli.workflows import research_findings
from leanflow_cli.workflows.plan_state import Blueprint, GraphNode

EXACT_TARGET_PRIORITY = 0
STRONG_SUFFIX_PRIORITY = 1
NEUTRAL_PRIORITY = 2
MINIMUM_SUFFIX_TOKENS = 4

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_DIRECT_EVIDENCE_FIELDS = frozenset(
    {
        "declaration",
        "declaration_name",
        "helper",
        "helper_name",
        "lean_status",
        "lemma",
        "lemma_name",
        "name",
        "proof_declaration",
        "symbol",
        "target",
        "target_symbol",
        "theorem",
        "theorem_name",
        "verification_status",
        "verified_declaration",
        "verified_declarations",
        "verified_helper",
        "verified_helpers",
    }
)
_NEGATIVE_FIELD_MARKERS = (
    "blocked",
    "failure",
    "issue",
    "missing",
    "open_sorr",
    "unresolved",
)


def _normalized_identifier(identifier: str) -> str:
    """Return a case-insensitive declaration basename."""
    return str(identifier or "").strip().rsplit(".", 1)[-1].lower()


def _identifier_tokens(identifier: str) -> tuple[str, ...]:
    """Return non-empty underscore tokens from one identifier."""
    return tuple(token for token in _normalized_identifier(identifier).split("_") if token)


def _has_strong_suffix(candidate: str, target: str) -> bool:
    """Return whether identifiers share a conservative four-token suffix."""
    candidate_tokens = _identifier_tokens(candidate)
    target_tokens = _identifier_tokens(target)
    if min(len(candidate_tokens), len(target_tokens)) < MINIMUM_SUFFIX_TOKENS:
        return False
    shared = 0
    for left, right in zip(reversed(candidate_tokens), reversed(target_tokens), strict=False):
        if left != right:
            break
        shared += 1
    return shared >= MINIMUM_SUFFIX_TOKENS


def _field_carries_positive_evidence(field: str, *, artifact_context: bool) -> bool:
    """Return whether a structured field may identify usable proof evidence."""
    normalized = str(field or "").strip().lower()
    if any(marker in normalized for marker in _NEGATIVE_FIELD_MARKERS):
        return False
    return (
        artifact_context
        or normalized in _DIRECT_EVIDENCE_FIELDS
        or normalized.startswith(("proved_", "verified_", "kernel_verified_"))
    )


def _structured_evidence_strings(
    value: Any,
    *,
    field: str = "",
    evidence_context: bool = False,
    artifact_context: bool = False,
) -> Iterator[str]:
    """Yield strings only from proof-positive or artifact metadata fields."""
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key or "")
            normalized = key.strip().lower()
            child_artifact = artifact_context or "artifact" in normalized
            child_evidence = _field_carries_positive_evidence(key, artifact_context=child_artifact)
            yield from _structured_evidence_strings(
                child,
                field=key,
                evidence_context=child_evidence,
                artifact_context=child_artifact,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _structured_evidence_strings(
                child,
                field=field,
                evidence_context=evidence_context,
                artifact_context=artifact_context,
            )
        return
    if evidence_context and isinstance(value, (str, bytes)):
        text = value.decode(errors="replace") if isinstance(value, bytes) else value
        if text.strip():
            yield text


def _finding_priority(finding: Mapping[str, Any], *, target_symbol: str) -> int:
    """Return exact, strong-suffix, or neutral rank for one finding."""
    if research_findings.foreground_use_role(finding) == "evidence_only":
        return NEUTRAL_PRIORITY
    target = _normalized_identifier(target_symbol)
    if not target:
        return NEUTRAL_PRIORITY
    finding_target = _normalized_identifier(str(finding.get("target_symbol", "") or ""))
    if finding_target == target:
        return EXACT_TARGET_PRIORITY

    priority = NEUTRAL_PRIORITY
    structured_payload = {
        "deliverable": finding.get("deliverable") or {},
        "artifact_paths": finding.get("artifact_paths") or [],
        "artifacts": finding.get("artifacts") or [],
    }
    for text in _structured_evidence_strings(structured_payload):
        for candidate in _IDENTIFIER_RE.findall(text):
            normalized = _normalized_identifier(candidate)
            if normalized == target:
                return EXACT_TARGET_PRIORITY
            if _has_strong_suffix(normalized, target):
                priority = STRONG_SUFFIX_PRIORITY
    return priority


def priority_by_target(
    summary: Mapping[str, Any] | None,
    *,
    blueprint: Blueprint,
) -> dict[str, int]:
    """Return research-evidence ranks for named blueprint nodes.

    Findings must be attached to the target itself or a same-file split
    ancestor. This prevents an unrelated job in the same source file from
    influencing queue order.
    """
    named_nodes = tuple(node for node in blueprint.nodes if node.name)
    if not named_nodes:
        return {}
    raw_findings = [
        finding
        for finding in dict(summary or {}).get("research_findings") or []
        if isinstance(finding, Mapping)
    ]
    limit = max(1, len(raw_findings))
    index = research_findings.build_relevant_findings_index(summary)
    priorities: dict[str, int] = {}
    for node in named_nodes:
        findings = research_findings.relevant_findings(
            summary,
            target_symbol=node.name,
            active_file=node.file,
            blueprint=blueprint,
            limit=limit,
            index=index,
        )
        priorities[node.name] = min(
            (_finding_priority(finding, target_symbol=node.name) for finding in findings),
            default=NEUTRAL_PRIORITY,
        )
    return priorities


def curriculum_key(
    node: GraphNode | None,
    *,
    priority: int,
) -> tuple[int, int]:
    """Compose research priority with the existing statement-length proxy."""
    length = len(node.statement) if node is not None and node.statement else 1_000_000
    return priority, length
