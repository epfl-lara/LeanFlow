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


def _mask_noncode(source: str) -> str:
    """Mask comments and strings while preserving offsets and newlines."""
    chars = list(source)
    index = 0
    block_depth = 0
    in_line_comment = False
    in_string = False
    escaped = False
    while index < len(source):
        pair = source[index : index + 2]
        char = source[index]
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            else:
                chars[index] = " "
            index += 1
            continue
        if block_depth:
            if pair == "/-":
                chars[index] = chars[index + 1] = " "
                block_depth += 1
                index += 2
                continue
            if pair == "-/":
                chars[index] = chars[index + 1] = " "
                block_depth -= 1
                index += 2
                continue
            if char != "\n":
                chars[index] = " "
            index += 1
            continue
        if in_string:
            if char != "\n":
                chars[index] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if pair == "--":
            chars[index] = chars[index + 1] = " "
            in_line_comment = True
            index += 2
            continue
        if pair == "/-":
            chars[index] = chars[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if char == '"':
            chars[index] = " "
            in_string = True
        index += 1
    return "".join(chars)


def candidates(declaration: str) -> tuple[HaveCandidate, ...]:
    """Return outermost named ``have`` blocks ordered by source position."""
    source = str(declaration or "")
    masked = _mask_noncode(source)
    matches = list(_HAVE_START_RE.finditer(masked))
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
            line = masked[scan:next_end]
            if line.strip() and len(line) - len(line.lstrip(" \t")) <= len(indent):
                end = scan
                break
            scan = next_end + 1
        block = source[match.start() : end].rstrip()
        masked_block = masked[match.start() : match.start() + len(block)]
        proof_marker = re.search(r":=\s*by\b", masked_block)
        if proof_marker is None or re.search(r"\b(?:sorry|admit)\b", masked_block):
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


def ranked_candidates(
    declaration: str,
    *,
    minimum_lines: int = 8,
) -> tuple[HaveCandidate, ...]:
    """Return substantial candidates ranked by estimated context reduction."""
    eligible = [
        candidate
        for candidate in candidates(declaration)
        if candidate.line_count >= max(2, int(minimum_lines or 8))
    ]
    return tuple(
        sorted(
            eligible,
            key=lambda candidate: (-len(candidate.source), -candidate.line_count, candidate.start),
        )
    )


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
    ranked = ranked_candidates(declaration, minimum_lines=minimum_lines)
    return ranked[0] if ranked else None
