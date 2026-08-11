"""Build authoritative in-file source context for Lean proof advisors."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
)

SOURCE_SCAN_MAX_BYTES = 4 * 1024 * 1024
SOURCE_CONTEXT_MAX_CHARS = 16_000
SOURCE_DECLARATION_MAX_CHARS = 4_000
SOURCE_DECLARATION_LIMIT = 8

_IDENTIFIER_RE = re.compile(
    r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*",
    flags=re.UNICODE,
)
_SOURCE_BODY_KINDS = frozenset(
    {"def", "abbrev", "opaque", "instance", "class", "structure", "inductive"}
)


@dataclass(frozen=True)
class AdvisorSourceContext:
    """Hold the exact target and referenced declarations visible before it."""

    target_statement: str = ""
    referenced_declarations: tuple[str, ...] = ()
    referenced_names: tuple[str, ...] = ()
    provisional_names: tuple[str, ...] = ()
    source_sha256: str = ""
    status: str = "unavailable"

    def render(self) -> str:
        """Return a bounded model-facing source block."""
        parts: list[str] = []
        if self.target_statement:
            parts.append(f"Exact assigned declaration signature:\n{self.target_statement}")
        if self.referenced_declarations:
            parts.append(
                "Exact referenced declarations already in scope:\n"
                + "\n\n".join(self.referenced_declarations)
            )
        if self.provisional_names:
            parts.append(
                "Provisional determine declarations (their current bodies are conjectures, "
                "not established answers, and may be revised from mathematical evidence):\n"
                + ", ".join(self.provisional_names)
            )
        return _bounded_text("\n\n".join(parts), SOURCE_CONTEXT_MAX_CHARS)


def _bounded_text(value: str, limit: int) -> str:
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


def _resolve_source_path(file_path: str, cwd: str) -> Path:
    """Resolve one requested Lean source path without changing process state."""
    path = Path(str(file_path or "")).expanduser()
    if not path.is_absolute():
        path = Path(str(cwd or "")).expanduser() / path if cwd else Path.cwd() / path
    return path.resolve()


def _leaf_name(value: str) -> str:
    """Return a namespace-insensitive Lean declaration name."""
    return str(value or "").strip().removeprefix("_root_.").rsplit(".", 1)[-1]


def _statement(text: str) -> str:
    """Return one declaration signature without its proof or value body."""
    declaration = str(text or "").strip()
    marker = _find_assignment_marker_for_statement(declaration)
    return declaration[:marker].rstrip() if marker >= 0 else declaration


def _identifier_positions(evidence: str) -> dict[str, int]:
    """Return the first mention offset for each exact and leaf identifier."""
    positions: dict[str, int] = {}
    for match in _IDENTIFIER_RE.finditer(str(evidence or "")):
        token = match.group(0).removeprefix("_root_.")
        for candidate in {token, _leaf_name(token)}:
            positions.setdefault(candidate, match.start())
    return positions


def _entry_text(entry: dict[str, Any]) -> str:
    """Return the useful exact source for one referenced declaration."""
    text = str(entry.get("text", "") or "").strip()
    kind = str(entry.get("kind", "") or "").strip()
    if kind not in _SOURCE_BODY_KINDS:
        text = _statement(text)
    return _bounded_text(text, SOURCE_DECLARATION_MAX_CHARS)


def load_advisor_source_context(
    *,
    theorem_id: str,
    file_path: str,
    cwd: str = "",
    evidence: str = "",
) -> AdvisorSourceContext:
    """Load the exact target and directly referenced prior declarations."""
    try:
        path = _resolve_source_path(file_path, cwd)
        if path.stat().st_size > SOURCE_SCAN_MAX_BYTES:
            return AdvisorSourceContext(status="source_too_large")
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, RuntimeError):
        return AdvisorSourceContext(status="source_unavailable")

    entries = _declaration_line_index_from_text(source)
    wanted = str(theorem_id or "").strip().removeprefix("_root_.")
    wanted_leaf = _leaf_name(wanted)
    target_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if str(entry.get("name", "") or "").strip().removeprefix("_root_.")
            in {wanted, wanted_leaf}
        ),
        -1,
    )
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if target_index < 0:
        return AdvisorSourceContext(source_sha256=source_sha256, status="target_not_found")

    target = entries[target_index]
    positions = _identifier_positions(evidence)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries[:target_index]):
        name = str(entry.get("name", "") or "").strip().removeprefix("_root_.")
        leaf = _leaf_name(name)
        position = min(
            (positions[key] for key in {name, leaf} if key in positions),
            default=-1,
        )
        if position < 0:
            continue
        candidates.append((position, -index, entry))
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = candidates[:SOURCE_DECLARATION_LIMIT]
    selected.sort(key=lambda item: -item[1])

    declarations = tuple(_entry_text(entry) for _, _, entry in selected)
    names = tuple(str(entry.get("name", "") or "").strip() for _, _, entry in selected)
    source_lines = source.splitlines()
    provisional_names = tuple(
        str(entry.get("name", "") or "").strip()
        for _, _, entry in selected
        if "answer to be determined"
        in "\n".join(
            source_lines[
                max(0, int(entry.get("line", 1) or 1) - 9) : max(
                    0, int(entry.get("line", 1) or 1) - 1
                )
            ]
        ).lower()
    )
    return AdvisorSourceContext(
        target_statement=_statement(str(target.get("text", "") or "")),
        referenced_declarations=declarations,
        referenced_names=names,
        provisional_names=provisional_names,
        source_sha256=source_sha256,
        status="loaded",
    )


def advisor_source_conflicts(advice: str, context: AdvisorSourceContext) -> tuple[str, ...]:
    """Return known declarations that the advisor treats as hypothetical or redefines."""
    text = str(advice or "")
    conflicts: list[str] = []
    provisional = {_leaf_name(name) for name in context.provisional_names}
    for full_name in context.referenced_names:
        if _leaf_name(full_name) in provisional:
            continue
        name = re.escape(_leaf_name(full_name))
        patterns = (
            rf"\b(?:noncomputable\s+)?def\s+`?{name}`?\b",
            rf"\b(?:assuming|suppose)\s+(?:that\s+)?`?{name}`?\s+"
            rf"(?:is|=|means|denotes|is\s+defined)",
            rf"\b(?:definition|body)\s+of\s+`?{name}`?\s+(?:is|=)",
        )
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            conflicts.append(full_name)
    return tuple(conflicts)
