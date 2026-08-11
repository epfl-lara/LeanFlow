"""Build and parse one axiom query embedded in an exact incremental check."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings


@dataclass(frozen=True)
class InlineAxiomQuery:
    """Describe one declaration-plus-query chunk and its isolated markers."""

    target: str
    requested_target: str
    declaration_sha256: str
    begin_marker: str
    end_marker: str
    source: str


@dataclass(frozen=True)
class InlineAxiomProfile:
    """Hold complete parsed evidence from one marked incremental query."""

    axioms: tuple[str, ...]
    output: str
    message_start: int
    message_end: int


def _split_trailing_scope_closures(declaration: str) -> tuple[str, str]:
    """Split namespace or section ``end`` commands from a segment suffix.

    LeanProbe assigns trailing scope closures to the final declaration segment.
    An inline command appended after that segment therefore runs outside the
    declaration's namespace. Keep comments and whitespace with the closures so
    the axiom query remains immediately after the declaration and inside every
    still-open scope.
    """
    lines = declaration.split("\n")
    sanitized_lines = _strip_lean_comments_and_strings(declaration).split("\n")
    if len(lines) != len(sanitized_lines):
        return declaration, ""

    split_at = len(lines)
    found_closure = False
    while split_at > 0:
        code = sanitized_lines[split_at - 1].strip()
        if not code:
            split_at -= 1
            continue
        if re.fullmatch(r"end(?:\s+.+)?", code):
            found_closure = True
            split_at -= 1
            continue
        break
    if not found_closure:
        return declaration, ""
    return (
        "\n".join(lines[:split_at]).rstrip(),
        "\n".join(lines[split_at:]).lstrip("\n"),
    )


def build_inline_axiom_query(
    declaration_source: str,
    *,
    target: str,
    requested_target: str,
) -> InlineAxiomQuery | None:
    """Append one marker-isolated ``#print axioms`` query to a declaration.

    Return ``None`` when an exact declaration or safe single-line target name is
    unavailable. Callers must treat that as missing verification evidence.
    """
    declaration = str(declaration_source or "").strip()
    exact_target = str(target or "").strip()
    requested = str(requested_target or "").strip()
    if (
        not declaration
        or not exact_target
        or not requested
        or any(character in exact_target for character in "\r\n")
        or any(character in requested for character in "\r\n")
    ):
        return None
    declaration_sha256 = hashlib.sha256(declaration.encode("utf-8")).hexdigest()
    identity = (
        hashlib.sha256(f"{requested}\0{exact_target}\0{declaration_sha256}".encode())
        .hexdigest()[:24]
        .upper()
    )
    begin_marker = f"LEANFLOW_INCREMENTAL_AXIOMS_BEGIN_{identity}"
    end_marker = f"LEANFLOW_INCREMENTAL_AXIOMS_END_{identity}"
    declaration_body, trailing_closures = _split_trailing_scope_closures(declaration)
    source_parts = [
        declaration_body,
        "",
        f'#check ("{begin_marker}" : String)',
        f"#print axioms {exact_target}",
        f'#check ("{end_marker}" : String)',
        "",
    ]
    if trailing_closures:
        source_parts.extend((trailing_closures, ""))
    source = "\n".join(source_parts)
    return InlineAxiomQuery(
        target=exact_target,
        requested_target=requested,
        declaration_sha256=declaration_sha256,
        begin_marker=begin_marker,
        end_marker=end_marker,
        source=source,
    )


def parse_inline_axiom_messages(
    messages: Sequence[Mapping[str, Any]],
    query: InlineAxiomQuery,
) -> tuple[InlineAxiomProfile | None, str]:
    """Parse exactly one complete marked profile from LeanProbe messages.

    Marker loss, duplication, reordering, or ambiguous dependency text returns
    no profile so the acceptance layer can fall back or reject fail-closed.
    """
    texts = [str(item.get("message", "") or "") for item in messages]
    begin_hits = [index for index, text in enumerate(texts) if query.begin_marker in text]
    end_hits = [index for index, text in enumerate(texts) if query.end_marker in text]
    if len(begin_hits) != 1 or len(end_hits) != 1:
        return None, "inline axiom query markers are missing or ambiguous"
    message_start = begin_hits[0]
    message_end = end_hits[0]
    if message_start >= message_end:
        return None, "inline axiom query markers are out of order"

    window = "\n".join(texts[message_start : message_end + 1])
    if window.count(query.begin_marker) != 1 or window.count(query.end_marker) != 1:
        return None, "inline axiom query markers are duplicated"
    begin = window.find(query.begin_marker) + len(query.begin_marker)
    end = window.find(query.end_marker, begin)
    if end < begin:
        return None, "inline axiom query output is incomplete"
    profile_output = window[begin:end].strip()
    dependency_lists = re.findall(r"depends on axioms:\s*\[([^\]]*)\]", profile_output)
    no_axiom_matches = re.findall(r"does not depend on any axioms", profile_output)
    if (len(dependency_lists), len(no_axiom_matches)) not in {(1, 0), (0, 1)}:
        return None, "inline axiom dependency output is missing or ambiguous"
    axioms = tuple(
        sorted(
            {
                token
                for dependency_list in dependency_lists
                for token in (item.strip() for item in dependency_list.split(","))
                if token
            }
        )
    )
    return (
        InlineAxiomProfile(
            axioms=axioms,
            output=profile_output,
            message_start=message_start,
            message_end=message_end,
        ),
        "",
    )
