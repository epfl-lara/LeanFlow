"""Pair candidate environments over bounded independent conditional paths without selecting TeX branches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .source_tex import Fragment, TexBundle

MAX_FRONTIER = 128
MAX_CONDITIONAL_DEPTH = 64
MAX_ENVIRONMENT_DEPTH = 128
MAX_WITNESSES = 1024
MAX_TOKENS = 100000
MAX_HISTORY_NODES = 100000
MAX_HISTORY_REFERENCES = 1000000
_ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")

Opening = tuple[str, int, int]
Block = tuple[str, int, int, int, int]
Frontier = dict[tuple[Opening, ...], int]
Witness = tuple[str, str, int, int]


@dataclass(frozen=True)
class _Token:
    """Locate one conditional delimiter or literal environment in the expanded stream."""

    kind: str
    start: int
    end: int
    name: str = ""
    value: bool | None = None


@dataclass(frozen=True, slots=True)
class _HistoryNode:
    """Share one completed block or a join of successful prefixes by arena identity."""

    block: Block | None
    parents: tuple[int, ...]


class _Failure(Exception):
    """Stop speculative parsing on unsupported structure or an exceeded resource bound."""

    def __init__(
        self,
        message: str,
        token: _Token,
        *,
        code: str = "environment_structure_limit",
        source_spans: list[dict[str, Any]] | None = None,
    ) -> None:
        self.message, self.token = message, token
        self.code, self.source_spans = code, source_spans


def _tokens(bundle: TexBundle, expanded: str) -> list[_Token]:
    """Project scanner-owned delimiters through original include occurrences."""
    tokens: list[_Token] = []

    def append(token: _Token) -> None:
        if len(tokens) >= MAX_TOKENS:
            raise _Failure(
                "Conditional environment tokens exceed their materialization bound", token
            )
        if token.kind in {"ifcase", "or"}:
            raise _Failure(
                "Case conditional alternatives require unsupported structural semantics",
                token,
                code="unsupported_environment_conditional",
            )
        tokens.append(token)

    for match in _ENVIRONMENT.finditer(expanded):
        append(_Token(match[1], match.start(), match.end(), match[2]))
    fragments: dict[str, list[Fragment]] = {}
    for fragment in bundle.fragments:
        if fragment.start < fragment.end:
            fragments.setdefault(fragment.occurrence_id, []).append(fragment)
    for occurrence in bundle.occurrences:
        if occurrence["status"] == "excluded" or occurrence["file"] not in bundle.scans:
            # includeonly exclusions deliberately contribute no expanded tokens.
            continue
        scan = bundle.scans[occurrence["file"]]
        selected = sorted(fragments.get(occurrence["id"], []), key=lambda part: part.start)
        overlaps = any(left.end > right.start for left, right in zip(selected, selected[1:]))
        cursor = 0
        for start, end, kind, value in scan.conditional_events:
            while cursor < len(selected) and selected[cursor].end <= start:
                cursor += 1
            matched_fragment = selected[cursor] if cursor < len(selected) else None
            if (
                overlaps
                or matched_fragment is None
                or matched_fragment.start > start
                or matched_fragment.end < end
                or matched_fragment.scan is not scan
            ):
                raise _Failure(
                    "A conditional delimiter has no unique complete expanded-source projection",
                    _Token(kind, start, end),
                    code="unmapped_environment_conditional",
                    source_spans=[scan.source.span(start, end, occurrence_id=occurrence["id"])],
                )
            offset = matched_fragment.offset - matched_fragment.start
            append(_Token(kind, offset + start, offset + end, value=value))
    return sorted(tokens, key=lambda token: (token.start, token.end, token.kind, token.name))


def _stack_key(stack: tuple[Opening, ...]) -> tuple[tuple[int, int, str], ...]:
    """Order equivalent frontier traversal by exact source openings instead of hash order."""
    return tuple((start, end, name) for name, start, end in stack)


class _Paths:
    """Merge equivalent stacks while retaining all successful prefixes and rejected-path witnesses."""

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.branches: dict[int, tuple[int, int]] = {}
        self.witnesses: list[Witness] = []
        self.rejected = False
        self.history_references = 0
        self.histories: list[_HistoryNode | None] = [None]
        self.history_index: dict[tuple[Block | None, tuple[int, ...]], int] = {}
        self.rejected_state_transitions = 0
        self.witnesses_truncated = False
        stack: list[tuple[int, int | None]] = []
        for index, token in enumerate(tokens):
            if token.kind == "if":
                if len(stack) >= MAX_CONDITIONAL_DEPTH:
                    raise _Failure("Conditional environment nesting exceeds its depth bound", token)
                stack.append((index, None))
            elif token.kind == "else":
                if not stack or stack[-1][1] is not None:
                    raise _Failure("Conditional delimiter structure cannot be paired", token)
                opening, _ = stack.pop()
                stack.append((opening, index))
            elif token.kind == "fi":
                if not stack:
                    raise _Failure("Conditional delimiter structure cannot be paired", token)
                opening, alternative = stack.pop()
                self.branches[opening] = (alternative if alternative is not None else index, index)
        if stack:
            raise _Failure("Conditional delimiter structure cannot be paired", tokens[stack[0][0]])

    def reject(self, code: str, message: str, start: int, end: int) -> None:
        """Retain a bounded first witness for each stopped malformed path."""
        self.rejected = True
        self.rejected_state_transitions += 1
        witness = code, message, start, end
        if witness not in self.witnesses:
            if len(self.witnesses) < MAX_WITNESSES:
                self.witnesses.append(witness)
            else:
                self.witnesses_truncated = True

    def merge(self, target: Frontier, incoming: Frontier, token: _Token) -> Frontier:
        """Union successful histories only when their pending environment stacks agree."""
        for stack in sorted(incoming, key=_stack_key):
            blocks = incoming[stack]
            if stack not in target and len(target) >= MAX_FRONTIER:
                raise _Failure(
                    "Conditional environment alternatives exceed their frontier bound", token
                )
            target[stack] = self.union(target.get(stack, 0), blocks, token)
        return target

    def history_node(self, block: Block | None, parents: tuple[int, ...], token: _Token) -> int:
        """Intern one bounded persistent node without copying any completed-prefix history."""
        key = block, parents
        existing = self.history_index.get(key)
        if existing is not None:
            return existing
        if len(self.histories) - 1 >= MAX_HISTORY_NODES:
            raise _Failure("Candidate block histories exceed their node-allocation bound", token)
        if self.history_references + len(parents) > MAX_HISTORY_REFERENCES:
            raise _Failure(
                "Candidate block histories exceed their reference-allocation bound", token
            )
        self.history_references += len(parents)
        result = len(self.histories)
        self.histories.append(_HistoryNode(block, parents))
        self.history_index[key] = result
        return result

    def union(self, left: int, right: int, token: _Token) -> int:
        """Join equivalent-stack histories with at most two shared parent references."""
        if left == right or right == 0:
            return left
        if left == 0:
            return right
        return self.history_node(None, tuple(sorted((left, right))), token)

    def collect(self, history: int) -> set[Block]:
        """Visit each surviving history node once and deduplicate its completed blocks."""
        pending, seen, blocks = [history], set(), set()
        while pending:
            current = pending.pop()
            if current == 0 or current in seen:
                continue
            seen.add(current)
            node = self.histories[current]
            assert node is not None
            if node.block is not None:
                blocks.add(node.block)
            pending.extend(node.parents)
        return blocks

    def walk(self, start: int, end: int, states: Frontier) -> Frontier:
        """Explore independent branch choices without correlating equal-looking tests."""
        index = start
        while index < end and states:
            token = self.tokens[index]
            if token.kind == "if":
                alternative, closing = self.branches[index]
                then = (
                    dict(self.walk(index + 1, alternative, states))
                    if token.value is not False
                    else {}
                )
                otherwise = (
                    self.walk(alternative + 1, closing, states)
                    if token.value is not True and alternative < closing
                    else dict(states) if token.value is not True else {}
                )
                states = self.merge(then, otherwise, token)
                index = closing + 1
                continue
            next_states: Frontier = {}
            for stack in sorted(states, key=_stack_key):
                blocks = states[stack]
                if token.kind == "begin":
                    if len(stack) >= MAX_ENVIRONMENT_DEPTH:
                        raise _Failure(
                            "Candidate environment nesting exceeds its depth bound", token
                        )
                    updated = stack + ((token.name, token.start, token.end),)
                    next_states[updated] = blocks
                elif token.kind == "end":
                    if not stack or stack[-1][0] != token.name:
                        self.reject(
                            "unmatched_environment",
                            f"Unmatched or misnested end of {token.name}",
                            token.start,
                            token.end,
                        )
                        continue
                    name, opening, body_start = stack[-1]
                    completed = (name, opening, body_start, token.start, token.end)
                    completed_blocks = self.history_node(
                        completed, (blocks,) if blocks else (), token
                    )
                    self.merge(next_states, {stack[:-1]: completed_blocks}, token)
            states = next_states
            index += 1
        return states


def conditional_blocks(bundle: TexBundle, expanded: str) -> list[dict[str, Any]] | None:
    """Recover candidates from balanced paths, retaining uncertainty or strict structural failure."""
    active_files = {
        occurrence["file"]
        for occurrence in bundle.occurrences
        if occurrence["status"] != "excluded"
    }
    scans = [bundle.scans[name] for name in sorted(active_files) if name in bundle.scans]
    if any(
        row["code"] in {"environment_structure_limit", "unsupported_environment_conditional"}
        for scan in scans
        for row in scan.diagnostics
    ):
        return []
    events = [scan.conditional_events for scan in scans]
    if not any(events):
        return None
    try:
        tokens = _tokens(bundle, expanded)
        if not any(token.kind == "if" and token.value is None for token in tokens):
            return None
        paths = _Paths(tokens)
        states = paths.walk(0, len(tokens), {(): 0})
    except _Failure as error:
        spans = error.source_spans or bundle.spans(error.token.start, error.token.end)
        bundle.diagnostic(
            error.code,
            error.message + "; no structural completeness is claimed",
            spans[0]["file"] if spans else "",
            spans=spans,
        )
        return []
    for stack in sorted(states, key=_stack_key):
        if stack:
            name, opening, _ = stack[-1]
            paths.reject(
                "unclosed_environment", f"Unclosed {name} environment", opening, stack[-1][2]
            )
    paths.witnesses.sort(key=lambda witness: (witness[2], witness[3], witness[0], witness[1]))
    balanced = states.get(())
    if balanced is None:
        for code, message, start, end in paths.witnesses:
            spans = bundle.spans(start, end)
            bundle.diagnostic(code, message, spans[0]["file"] if spans else "", spans=spans)
        return []
    if paths.rejected:
        spans = []
        seen = set()
        for _, _, start, end in paths.witnesses:
            for span in bundle.spans(start, end):
                key = span["file"], span["occurrence_id"], span["byte_start"], span["byte_end"]
                if key not in seen:
                    spans.append(span)
                    seen.add(key)
        bundle.diagnostic(
            "unresolved_environment_structure",
            "Some independent conditional paths have malformed environment nesting; "
            "balanced paths supply source candidates only, without branch correlation or coverage approval",
            spans[0]["file"] if spans else "",
            spans=spans,
            violation_witnesses=[
                {"code": code, "message": message, "source_spans": bundle.spans(start, end)}
                for code, message, start, end in paths.witnesses
            ],
            independent_conditional_paths=True,
            rejected_state_transitions=paths.rejected_state_transitions,
            violation_witnesses_truncated=paths.witnesses_truncated,
            violation_witnesses_deduplicated=True,
        )
    return [
        {
            "environment": name,
            "start": start,
            "end": end,
            "body_start": body_start,
            "body_end": body_end,
            "uncertain": True,
        }
        for name, start, body_start, body_end, end in sorted(
            paths.collect(balanced),
            key=lambda block: (block[1], block[4], block[0], block[2], block[3]),
        )
    ]
