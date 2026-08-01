"""Find top-level local ``have`` proofs suitable for helper extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HAVE_START_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)have\s+(?P<name>[A-Za-z_«][\w'.«»]*)\b")


@dataclass(frozen=True)
class HaveCandidate:
    """Describe one complete local ``have ... := by`` block."""

    name: str
    start: int
    end: int
    indent: str
    source: str
    header: str
    proof: str
    line_count: int


def candidates(declaration: str) -> tuple[HaveCandidate, ...]:
    """Return outermost named ``have`` blocks ordered by source position."""
    source = str(declaration or "")
    matches = list(_HAVE_START_RE.finditer(source))
    if not matches:
        return ()
    minimum_indent = min(len(match.group("indent").expandtabs(2)) for match in matches)
    result: list[HaveCandidate] = []
    for match in matches:
        indent = match.group("indent")
        if len(indent.expandtabs(2)) != minimum_indent:
            continue
        line_end = source.find("\n", match.start())
        scan = len(source) if line_end < 0 else line_end + 1
        end = len(source)
        while scan < len(source):
            next_end = source.find("\n", scan)
            if next_end < 0:
                next_end = len(source)
            line = source[scan:next_end]
            if line.strip() and len(line) - len(line.lstrip(" \t")) <= len(indent):
                end = scan
                break
            scan = next_end + 1
        block = source[match.start() : end].rstrip()
        proof_marker = re.search(r":=\s*by\b", block)
        if proof_marker is None or re.search(r"\b(?:sorry|admit)\b", block):
            continue
        result.append(
            HaveCandidate(
                name=match.group("name"),
                start=match.start(),
                end=match.start() + len(block),
                indent=indent,
                source=block,
                header=block[: proof_marker.end()],
                proof=block[proof_marker.end() :],
                line_count=len(block.splitlines()),
            )
        )
    return tuple(result)


def select_candidate(
    declaration: str,
    *,
    have_name: str = "",
    minimum_lines: int = 8,
) -> HaveCandidate | None:
    """Select the named candidate or the largest sufficiently substantial block."""
    available = candidates(declaration)
    requested = str(have_name or "").strip()
    if requested:
        return next((candidate for candidate in available if candidate.name == requested), None)
    substantial = [candidate for candidate in available if candidate.line_count >= minimum_lines]
    return max(
        substantial,
        key=lambda candidate: (len(candidate.source), candidate.line_count),
        default=None,
    )
