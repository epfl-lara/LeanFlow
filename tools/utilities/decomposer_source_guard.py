"""Find source-backed constraints that invalidate decomposition candidates."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_decomposition_shape import inspect_helper_skeleton
from leanflow_cli.lean.lean_parsing import (
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)

SOURCE_SCAN_MAX_BYTES = 4 * 1024 * 1024

_LEAN_NAME = r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*"
_DECLARATION_RE = re.compile(
    rf"^[ \t]*(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+)\s+)*"
    rf"(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
    rf"(?P<kind>lemma|theorem|example|def|abbrev|axiom|opaque|instance|class|"
    rf"structure|inductive)\s+(?P<name>{_LEAN_NAME})\b"
)
_BLOCK_RE = re.compile(rf"^[ \t]*(?P<kind>namespace|section|end)\b(?:\s+(?P<name>{_LEAN_NAME}))?")
_OTHER_BOUNDARY_RE = re.compile(
    r"^[ \t]*(?:@\[|open\b|export\b|attribute\b|variable\b|include\b|omit\b)"
)
_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b", flags=re.IGNORECASE)
_CONSTRAINT_NAME_RE = re.compile(
    r"(?:consistent|consistency|do_not_imply_false|does_not_imply_false|"
    r"not_imply_false|counterexample_to_false)",
    flags=re.IGNORECASE,
)
_TERMINAL_CONTRADICTION_RE = re.compile(
    r"(?:terminal|residue[ _-]*cover|exhaust|contradict|impossible[ _-]*final|"
    r"no[ _-]*residue|all[^\n]{0,40}failed)",
    flags=re.IGNORECASE,
)
_FALSE_TAIL_RE = re.compile(r"(?:^|:|→|->)\s*False\s*$", flags=re.IGNORECASE)
_FALSE_HYPOTHESIS_RE = re.compile(r"[({]\s*[^:)}]+:\s*False\s*[)}]", flags=re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?:[^\W\d]|_)[\w']*|\d+")
_TOKEN_STOPWORDS = frozenset(
    {
        "by",
        "false",
        "lemma",
        "private",
        "protected",
        "theorem",
        "true",
        "where",
        "with",
    }
)
_ROUTE_STOPWORDS = _TOKEN_STOPWORDS | frozenset(
    {
        "condition",
        "conditions",
        "consistent",
        "consistency",
        "cover",
        "do",
        "does",
        "helper",
        "imply",
        "not",
    }
)


@dataclass(frozen=True)
class SourceConstraint:
    """Describe one sorry-free source declaration that constrains a helper route."""

    name: str
    statement: str
    declaration_sha256: str
    kind: str


@dataclass(frozen=True)
class SourceDeclarationStatus:
    """Describe one declaration's current source presence and placeholder state."""

    name: str
    full_name: str
    start_line: int
    end_line: int
    has_placeholder: bool


@dataclass(frozen=True)
class DecomposerSourceContext:
    """Hold target location and target-scoped source constraints."""

    target_start_line: int = 0
    target_end_line: int = 0
    target_statement: str = ""
    constraints: tuple[SourceConstraint, ...] = ()
    declarations: tuple[SourceDeclarationStatus, ...] = ()
    source_sha256: str = ""
    status: str = "unavailable"


@dataclass(frozen=True)
class _Declaration:
    """Hold one namespace-resolved, source-bounded declaration."""

    name: str
    full_name: str
    kind: str
    start_line: int
    end_line: int
    text: str
    statement: str


def _leaf_name(value: str) -> str:
    """Return a namespace-insensitive declaration name."""
    return str(value or "").strip().removeprefix("_root_.").rsplit(".", 1)[-1]


def _resolve_source_path(file_path: str, cwd: str) -> Path:
    """Resolve the requested source without changing process state."""
    path = Path(str(file_path or "")).expanduser()
    if not path.is_absolute():
        path = Path(str(cwd or "")).expanduser() / path if cwd else Path.cwd() / path
    return path.resolve()


def _qualified_name(name: str, namespace_parts: Sequence[str]) -> str:
    """Return a declaration's namespace-qualified Lean name."""
    declared = str(name or "").strip()
    if declared.startswith("_root_."):
        return declared.removeprefix("_root_.")
    prefix = ".".join(part for part in namespace_parts if part)
    return f"{prefix}.{declared}" if prefix else declared


def _pop_block(blocks: list[tuple[str, tuple[str, ...]]], name: str) -> None:
    """Pop one syntactic block, preferring a matching named block."""
    if not blocks:
        return
    wanted = str(name or "").strip()
    if not wanted:
        blocks.pop()
        return
    for index in range(len(blocks) - 1, -1, -1):
        block_name = ".".join(blocks[index][1])
        if block_name == wanted or block_name.rsplit(".", 1)[-1] == wanted:
            del blocks[index:]
            return
    blocks.pop()


def _declarations(source: str) -> tuple[_Declaration, ...]:
    """Return namespace-aware declaration regions bounded by source commands."""
    raw_lines = source.splitlines(keepends=True)
    sanitized_lines = _strip_lean_comments_and_strings(source).splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in raw_lines:
        offsets.append(offset)
        offset += len(line)

    blocks: list[tuple[str, tuple[str, ...]]] = []
    found: list[tuple[int, int, str, str, str]] = []
    boundaries: list[int] = []
    for index, sanitized_line in enumerate(sanitized_lines):
        line_offset = offsets[index] if index < len(offsets) else len(source)
        block_match = _BLOCK_RE.match(sanitized_line)
        if block_match:
            boundaries.append(line_offset)
            kind = str(block_match.group("kind") or "")
            name = str(block_match.group("name") or "")
            if kind == "end":
                _pop_block(blocks, name)
            elif kind == "namespace":
                blocks.append((kind, tuple(part for part in name.split(".") if part)))
            else:
                blocks.append((kind, (name,) if name else ()))
            continue

        declaration_match = _DECLARATION_RE.match(sanitized_line)
        if declaration_match:
            boundaries.append(line_offset)
            kind = str(declaration_match.group("kind") or "")
            name = str(declaration_match.group("name") or "")
            namespace_parts = [
                part for block_kind, parts in blocks if block_kind == "namespace" for part in parts
            ]
            found.append(
                (index + 1, line_offset, kind, name, _qualified_name(name, namespace_parts))
            )
            continue
        if _OTHER_BOUNDARY_RE.match(sanitized_line):
            boundaries.append(line_offset)

    ordered_boundaries = sorted(set([*boundaries, len(source)]))
    declarations: list[_Declaration] = []
    for start_line, start, kind, name, full_name in found:
        end = next((value for value in ordered_boundaries if value > start), len(source))
        text = source[start:end].rstrip()
        assignment = _find_assignment_marker_for_statement(text)
        statement = text[:assignment].rstrip() if assignment >= 0 else text.rstrip()
        end_line = start_line + max(0, text.count("\n"))
        declarations.append(
            _Declaration(
                name=name,
                full_name=full_name,
                kind=kind,
                start_line=start_line,
                end_line=max(start_line, end_line),
                text=text,
                statement=statement,
            )
        )
    return tuple(declarations)


def _resolve_target(
    declarations: Sequence[_Declaration], theorem_id: str
) -> tuple[_Declaration | None, str]:
    """Resolve an exact, relative-qualified, or unambiguous short target name."""
    wanted = str(theorem_id or "").strip().removeprefix("_root_.")
    exact = [declaration for declaration in declarations if declaration.full_name == wanted]
    if len(exact) == 1:
        return exact[0], "loaded"
    if len(exact) > 1:
        return None, "target_ambiguous"
    if "." in wanted:
        relative_matches = [
            declaration
            for declaration in declarations
            if declaration.full_name.endswith(f".{wanted}")
        ]
        if len(relative_matches) == 1:
            return relative_matches[0], "loaded"
        if len(relative_matches) > 1:
            return None, "target_ambiguous"
        return None, "target_not_found"
    leaf_matches = [
        declaration for declaration in declarations if _leaf_name(declaration.full_name) == wanted
    ]
    if len(leaf_matches) == 1:
        return leaf_matches[0], "loaded"
    if len(leaf_matches) > 1:
        return None, "target_ambiguous"
    return None, "target_not_found"


def _matches_target_scope(name: str, theorem_id: str) -> bool:
    """Return whether a declaration is explicitly scoped to the exact target."""
    candidate = str(name or "").strip().removeprefix("_root_.").casefold()
    target = str(theorem_id or "").strip().removeprefix("_root_.").casefold()
    return bool(target and (candidate == target or candidate.startswith(target + "_")))


def _constraint_kind(name: str) -> str:
    """Return a stable source-constraint classification."""
    lowered = _leaf_name(name).casefold()
    if "do_not_imply_false" in lowered or "does_not_imply_false" in lowered:
        return "negated_false_implication"
    if "counterexample" in lowered:
        return "counterexample"
    return "consistency"


def load_decomposer_source_context(
    *, theorem_id: str, file_path: str, cwd: str = ""
) -> DecomposerSourceContext:
    """Read target location and sorry-free target-scoped constraints."""
    try:
        path = _resolve_source_path(file_path, cwd)
        size = path.stat().st_size
        if size > SOURCE_SCAN_MAX_BYTES:
            return DecomposerSourceContext(status="source_too_large")
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, RuntimeError):
        return DecomposerSourceContext(status="source_unavailable")

    declarations = _declarations(source)
    target, target_status = _resolve_target(declarations, theorem_id)
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if target is None:
        return DecomposerSourceContext(source_sha256=source_sha256, status=target_status)

    constraints: list[SourceConstraint] = []
    for declaration in declarations:
        if declaration.kind not in {"lemma", "theorem"}:
            continue
        if not _matches_target_scope(declaration.full_name, target.full_name):
            continue
        if not _CONSTRAINT_NAME_RE.search(_leaf_name(declaration.full_name)):
            continue
        sanitized = _strip_lean_comments_and_strings(declaration.text)
        if _PLACEHOLDER_RE.search(sanitized):
            continue
        if not ("∃" in declaration.statement or "¬" in declaration.statement):
            continue
        constraints.append(
            SourceConstraint(
                name=declaration.full_name,
                statement=declaration.statement,
                declaration_sha256=hashlib.sha256(declaration.text.encode("utf-8")).hexdigest(),
                kind=_constraint_kind(declaration.full_name),
            )
        )
    declaration_statuses = tuple(
        SourceDeclarationStatus(
            name=declaration.name,
            full_name=declaration.full_name,
            start_line=declaration.start_line,
            end_line=declaration.end_line,
            has_placeholder=bool(
                _PLACEHOLDER_RE.search(_strip_lean_comments_and_strings(declaration.text))
            ),
        )
        for declaration in declarations
    )
    return DecomposerSourceContext(
        target_start_line=target.start_line,
        target_end_line=target.end_line,
        target_statement=target.statement,
        constraints=tuple(constraints[:6]),
        declarations=declaration_statuses,
        source_sha256=source_sha256,
        status="loaded",
    )


def _tokens(text: str, *, stopwords: frozenset[str]) -> set[str]:
    """Return stable significant identifiers from source or route prose."""
    return {
        token
        for raw in _TOKEN_RE.findall(re.sub(r"[_.-]+", " ", str(text or "")))
        if (token := raw.casefold()) not in stopwords and (len(token) > 1 or token.isdigit())
    }


def _route_overlap(route_text: str, constraint: SourceConstraint) -> bool:
    """Return whether a constraint names the same decomposition route."""
    route_tokens = _tokens(route_text, stopwords=_ROUTE_STOPWORDS)
    constraint_tokens = _tokens(constraint.name, stopwords=_ROUTE_STOPWORDS)
    return bool(route_tokens & constraint_tokens)


def _conditional_route_matches_constraint(
    signature: str,
    constraint: SourceConstraint,
) -> bool:
    """Return whether a conditional False route overlaps a promoted negation fact."""
    if constraint.kind != "negated_false_implication":
        return False
    signature_tokens = _tokens(_declaration_tail(signature), stopwords=_TOKEN_STOPWORDS)
    constraint_tokens = _tokens(_declaration_tail(constraint.statement), stopwords=_TOKEN_STOPWORDS)
    return len(signature_tokens & constraint_tokens) >= 2


def _declaration_tail(statement: str) -> str:
    """Return a declaration signature after its declared name."""
    match = _DECLARATION_RE.match(str(statement or ""))
    return str(statement or "")[match.end() :] if match is not None else str(statement or "")


def helper_source_conflict_reason(
    helper: Mapping[str, Any],
    *,
    theorem_id: str,
    constraints: Sequence[SourceConstraint],
) -> str:
    """Return why a route-specific terminal-False helper conflicts with source facts."""
    if not constraints:
        return ""
    skeleton = str(helper.get("lean_skeleton", "") or helper.get("skeleton", "") or "")
    shape = inspect_helper_skeleton(
        skeleton,
        expected_name=str(helper.get("name", "") or "").strip(),
    )
    if not shape.valid or not _matches_target_scope(
        _leaf_name(shape.declared_name), _leaf_name(theorem_id)
    ):
        return ""
    if not _FALSE_TAIL_RE.search(shape.signature) or _FALSE_HYPOTHESIS_RE.search(shape.signature):
        return ""
    hints = helper.get("proof_hints", helper.get("hints", []))
    hint_text = " ".join(str(item) for item in hints) if isinstance(hints, list) else str(hints)
    route_text = " ".join(
        (
            shape.declared_name,
            str(helper.get("purpose", "") or helper.get("why", "") or ""),
            hint_text,
        )
    )
    if not _TERMINAL_CONTRADICTION_RE.search(route_text):
        return ""
    closed_false = bool(
        re.fullmatch(r"\s*:\s*False\s*", _declaration_tail(shape.signature), re.IGNORECASE)
    )
    matching = [
        constraint
        for constraint in constraints
        if _route_overlap(route_text, constraint)
        and (closed_false or _conditional_route_matches_constraint(shape.signature, constraint))
    ]
    if not matching:
        return ""
    fact_names = ", ".join(constraint.name for constraint in matching[:3])
    return (
        "Proposed target-scoped terminal contradiction reuses a route blocked by "
        f"sorry-free consistency/negation evidence: {fact_names}. The cited prefix is not "
        "an exhaustive contradiction; require a new exact promoted exhaustion theorem or "
        "choose a non-False coverage or witness-producing helper instead."
    )
