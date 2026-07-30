"""Detect unresolved proof bodies that should become graph-visible helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PROOF_START_RE = re.compile(r":=\s*by\b")
_MILESTONE_RE = re.compile(r"(?m)^\s*(?:have|suffices|let|calc)\b")
_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|by\s*\?)\b")


@dataclass(frozen=True)
class PartialProofStructure:
    """Describe whether one unresolved declaration has become too monolithic."""

    needs_decomposition: bool = False
    milestone_count: int = 0
    proof_line_count: int = 0


def assess(declaration: str) -> PartialProofStructure:
    """Return a conservative decomposition signal for one declaration slice."""
    source = str(declaration or "")
    proof_start = _PROOF_START_RE.search(source)
    if proof_start is None:
        return PartialProofStructure()
    proof = source[proof_start.end() :]
    if _PLACEHOLDER_RE.search(proof) is None:
        return PartialProofStructure()
    milestone_count = len(_MILESTONE_RE.findall(proof))
    proof_line_count = len(proof.splitlines())
    needs_decomposition = milestone_count >= 3 or (
        milestone_count >= 2 and (proof_line_count >= 35 or len(proof) >= 1600)
    )
    return PartialProofStructure(
        needs_decomposition=needs_decomposition,
        milestone_count=milestone_count,
        proof_line_count=proof_line_count,
    )


def feedback_lines(target_symbol: str, declaration: str) -> tuple[str, ...]:
    """Build concise next-action guidance for a monolithic partial proof."""
    structure = assess(declaration)
    if not structure.needs_decomposition:
        return ()
    return (
        "",
        "[LEANFLOW-NATIVE PROOF STRUCTURE NUDGE]",
        (
            f"- `{target_symbol}` now contains {structure.milestone_count} local proof "
            f"milestones across {structure.proof_line_count} proof lines while remaining unresolved"
        ),
        (
            "- preserve the working branch, but extract stable milestones into named top-level "
            "helper declarations so the plan graph can track and verify them independently"
        ),
        (
            "- next action: use `lean_decompose_helpers` with the exact remaining invariant, "
            "or insert one focused helper before the target and exact-check it; do not grow "
            "another monolithic target-body branch"
        ),
    )
