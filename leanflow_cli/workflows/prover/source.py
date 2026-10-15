"""Protect original Lean bytes and replace only lexically identified sorry holes."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    declaration_statement_text,
)
from leanflow_cli.workflows.prover.models import Dag, Node, digest


class SourceConflictError(RuntimeError):
    """Report source consistency failures separately from rejected Lean declarations."""


def read_source(path: Path) -> str:
    """Read UTF-8 source without newline translation."""
    return path.read_bytes().decode("utf-8")


def write_source(path: Path, text: str) -> None:
    """Preserve source bytes, including the user's existing newline convention."""
    path.write_bytes(text.encode("utf-8"))


def project_path(root: Path, relative: str) -> Path:
    """Resolve a controller write without traversing a symlink or escaping the project."""
    path = root / relative
    path.resolve().relative_to(root.resolve())
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError(f"controller source path traverses a symlink: {relative}")
        cursor = cursor.parent
    return path


def lean_lexical_regions(source: str) -> Iterator[tuple[int, int, bool]]:
    """Yield literal/comment spans, rejecting unsupported executable interpolations."""
    index = 0
    while index < len(source):
        end = index
        comment = source.startswith(("--", "/-"), index)
        if source.startswith("--", index):
            newline = source.find("\n", index)
            end = len(source) if newline < 0 else newline
        elif source.startswith("/-", index):
            depth, end = 1, index + 2
            while end < len(source) and depth:
                if source.startswith("/-", end):
                    depth += 1
                    end += 2
                elif source.startswith("-/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                raise ValueError("unterminated Lean block comment")
        elif source.startswith('s!"', index) or re.match(r's!r#*"', source[index:]):
            # Interpolation contains executable Lean terms; treating it as an
            # ordinary string would hide commands and proof holes from surgery.
            raise ValueError("Lean string interpolation requires a syntax-aware source adapter")
        elif source[index] == "r" and (raw := re.match(r'r(#+)?"', source[index:])):
            closing = '"' + (raw.group(1) or "")
            finish = source.find(closing, index + len(raw.group()))
            if finish < 0:
                raise ValueError("unterminated Lean raw string")
            end = finish + len(closing)
        elif source[index] == "'" and (
            character := re.match(
                r"'(?:[^'\\\n]|\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.))'", source[index:]
            )
        ):
            end = index + len(character.group())
        elif source[index] in {'"', "«"}:
            closing = '"' if source[index] == '"' else "»"
            end = index + 1
            while end < len(source):
                if closing == '"' and source[end] == "\\":
                    end += 2
                elif source[end] == closing:
                    end += 1
                    break
                else:
                    end += 1
            else:
                raise ValueError("unterminated Lean string or quoted name")
        if end > index:
            yield index, min(end, len(source)), comment
            index = end
        else:
            index += 1


def lean_code_mask(source: str, *, preserve_literals: bool = False) -> str:
    """Blank literals and comments without hiding executable interpolation syntax."""
    chars = list(source)
    for start, end, comment in lean_lexical_regions(source):
        if preserve_literals and not comment:
            continue
        for position in range(start, end):
            if chars[position] != "\n":
                chars[position] = " "
    return "".join(chars)


def sorry_spans(source: str) -> list[tuple[int, int]]:
    """Locate literal sorry tokens, excluding names such as sorry' and Foo.sorry."""
    return [
        (match.start(), match.end())
        for match in re.finditer(r"(?<![\w'.])sorry(?![\w'])", lean_code_mask(source))
    ]


def import_only_source(source: str) -> bool:
    """Recognize simple import aggregators without classifying declaration files as empty."""
    lines = [line.strip() for line in lean_code_mask(source).splitlines() if line.strip()]
    return any(re.match(r"import\s", line) for line in lines) and all(
        re.fullmatch(
            r"import\s+[\w.'.]+(?:\s+[\w.'.]+)*|prelude|set_option\s+[\w.]+\s+(?:\d+|true|false)",
            line,
        )
        for line in lines
    )


def validate_hole_replacement(replacement: str) -> None:
    """Reject command syntax crossing the boundary of an authorized proof hole.

    Lean's named-target checker deliberately stops before the next command.
    A completed tactic followed by a declaration or attribute must therefore
    never reach that checker as an apparently ordinary hole replacement.
    """
    mask = lean_code_mask(replacement)
    command = re.search(
        r"(?<![\w'.])(?:theorem|lemma|example|def|abbrev|opaque|axiom|constant|"
        r"instance|class|structure|inductive|coinductive|mutual|namespace|section|"
        r"end|import|prelude|universe|universes|variable|variables|include|omit|"
        r"attribute|export|syntax|macro|macro_rules|elab|elab_rules|initialize|"
        r"builtin_initialize|run_cmd|run_meta|simproc|dsimproc|deriving)(?![\w'])|#[A-Za-z_]",
        mask,
    )
    if command:
        raise ValueError(f"a sorry replacement cannot introduce command {command.group()!r}")
    # Both forms can also scope a legitimate term/tactic. Require the local `in`
    # wrapper; an unscoped command could change later protected declarations.
    for match in re.finditer(r"(?m)^\s*(?:set_option|open)\b[^\n]*", mask):
        if not re.search(r"\bin\b", match.group()):
            raise ValueError("a sorry replacement cannot introduce an unscoped command")


@dataclass
class SourceDocument:
    """Render original bytes with controller-owned insertions and authorized hole replacements."""

    path: str
    baseline: str
    replacements: dict[str, str] = field(default_factory=dict)
    imports: list[str] = field(default_factory=list)
    generated: bool = False
    proof_imports: list[str] = field(default_factory=list)

    def render(self, overrides: dict[int, str] | None = None) -> str:
        replacements = {int(key): value for key, value in self.replacements.items()}
        replacements.update(overrides or {})
        for replacement in replacements.values():
            validate_hole_replacement(replacement)
        text = self.baseline
        spans = sorry_spans(text) if replacements else []
        if any(index < 0 or index >= len(spans) for index in replacements):
            raise ValueError("source hole index is no longer valid")
        for index in reversed(range(len(spans))):
            start, end = spans[index]
            if index in replacements:
                text = text[:start] + replacements[index] + text[end:]
        modules = list(dict.fromkeys([*self.imports, *self.proof_imports]))
        if modules:
            imports = "\n".join(f"import {module}" for module in modules) + "\n"
            # Lean's optional prelude command must precede every import.
            match = re.match(r"(\s*prelude[^\n]*\n)", text)
            position = match.end() if match else 0
            text = text[:position] + imports + text[position:]
        return text

    def assert_current(self, root: Path) -> None:
        """Fail closed when another actor changed a canonical source since the last commit."""
        path = project_path(root, self.path)
        if path.is_symlink() or not path.is_file() or read_source(path) != self.render():
            raise SourceConflictError(f"protected source changed outside controller: {self.path}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_sources(root: Path) -> list[Path]:
    """Enumerate user Lean sources, pruning excluded trees and refusing incomplete scans."""
    if not root.is_dir():
        return []

    def scan_failed(error: OSError) -> None:
        """Refuse partial source authority after an unreadable or disappearing directory."""
        raise error

    result = []
    for current, directories, files in os.walk(root, onerror=scan_failed, followlinks=False):
        directories[:] = [
            name for name in directories if name not in {".lake", ".git", ".leanflow", ".venv"}
        ]
        # Glob matching also includes ordinary directories named *.lean.
        for name in [*directories, *files]:
            path = Path(current) / name
            if path.match("*.lean") and not path.is_symlink():
                result.append(path)
    return sorted(result)


def discover(
    root: Path,
    targets: list[Path],
    *,
    fill_definitions: bool,
    authorized_holes: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[Dag, dict[str, SourceDocument]]:
    """Snapshot all original Lean source and enumerate authorized holes in requested files."""
    documents = {
        str(path.relative_to(root)): SourceDocument(str(path.relative_to(root)), read_source(path))
        for path in project_sources(root)
    }
    dag = Dag()
    if authorized_holes is not None and set(authorized_holes) != {
        str(path.resolve().relative_to(root)) for path in targets
    }:
        raise ValueError("formalization handoff file scope differs from its authorized holes")
    for path in targets:
        relative = str(path.resolve().relative_to(root))
        document = documents.get(relative)
        if document is None:
            raise ValueError(f"target is not a regular project Lean file: {relative}")
        source = document.baseline
        spans = sorry_spans(source)
        authorization = authorized_holes.get(relative, []) if authorized_holes is not None else []
        authorized_indexes: set[int] = set()
        for item in authorization:
            indexes = item.get("holes", [])
            if not indexes or any(
                type(index) is not int or index < 0 or index >= len(spans) for index in indexes
            ):
                raise ValueError("formalization handoff contains an invalid hole index")
            if authorized_indexes.intersection(indexes):
                raise ValueError("formalization handoff authorizes a hole more than once")
            if item.get("hole_spans") != [list(spans[index]) for index in indexes]:
                raise ValueError("formalization handoff hole positions changed")
            authorized_indexes.update(indexes)
        line_offsets = [0]
        for line in source.splitlines(keepends=True):
            line_offsets.append(line_offsets[-1] + len(line))
        entries = _declaration_line_index_from_text(lean_code_mask(source))
        assigned: set[int] = set()
        for entry in entries:
            start = line_offsets[entry["line"] - 1]
            end = line_offsets[min(entry["end_line"], len(line_offsets) - 1)]
            holes = [index for index, (position, _) in enumerate(spans) if start <= position < end]
            if not holes:
                continue
            selected: dict[str, Any] | None = None
            if authorized_holes is not None:
                if not authorized_indexes.intersection(holes):
                    continue
                matches = [
                    item
                    for item in authorization
                    if set(item["holes"]).issubset(holes) and item["source_name"] == entry["name"]
                ]
                if len(matches) != 1:
                    raise ValueError("formalization handoff declaration/hole identity differs")
                selected = matches[0]
                selected_range = selected.get("declaration_span", [start, end])
                if (
                    not isinstance(selected_range, list)
                    or len(selected_range) != 2
                    or any(type(position) is not int for position in selected_range)
                    or not 0 <= selected_range[0] < selected_range[1] <= end
                ):
                    raise ValueError("formalization handoff declaration range differs")
                start, end = selected_range
                holes = [index for index, (a, b) in enumerate(spans) if start <= a < b <= end]
                if holes != selected["holes"]:
                    raise ValueError("formalization handoff declaration range contains other holes")
            kind = str(entry["kind"])
            if kind not in {"theorem", "lemma", "example"} and not fill_definitions:
                raise ValueError(
                    f"definition {entry['name']} contains sorry; enable fill_definitions explicitly"
                )
            name = str(selected["declaration"] if selected else entry["name"])
            if name.startswith("[anonymous"):
                raise ValueError(
                    f"anonymous declaration at {relative}:{entry['line']} requires a name before proving"
                )
            assigned.update(holes)
            declaration = source[start:end].rstrip()
            node_id = "n_" + digest(f"{relative}:{name}:{start}")[:16]
            dag.nodes.append(
                Node(
                    id=node_id,
                    name=name,
                    statement=declaration_statement_text(declaration),
                    file=relative,
                    module=str(selected["module"]) if selected else relative[:-5].replace("/", "."),
                    kind=kind,
                    original=True,
                    holes=holes,
                    line_start=source.count("\n", 0, start) + 1 if selected else entry["line"],
                    line_end=source.count("\n", 0, end) + 1 if selected else entry["end_line"],
                )
            )
            dag.roots.append(node_id)
        expected = authorized_indexes if authorized_holes is not None else set(range(len(spans)))
        if assigned != expected:
            raise ValueError(
                f"cannot safely associate every sorry with a named declaration in {relative}"
            )
    dag.validate(max(128, len(dag.nodes)))
    return dag, documents


def declaration_source(
    document: SourceDocument, node: Node, *, candidate: list[str] | None = None
) -> str:
    """Build exact source for a candidate without writing canonical files."""
    if candidate is not None and len(candidate) != len(node.holes):
        raise ValueError(f"expected {len(node.holes)} sorry replacements for {node.name}")
    overrides = dict(zip(node.holes, candidate or [], strict=False))
    return document.render(overrides)


def extract_scratch_replacements(before: str, after: str, holes: list[int]) -> list[str] | None:
    """Recover only literal-hole replacements from a scratch file with unchanged surrounding bytes."""
    spans = sorry_spans(before)
    selected = [spans[index] for index in holes if index < len(spans)]
    if len(selected) != len(holes) or not selected:
        return None
    parts: list[str] = []
    cursor = 0
    for start, end in selected:
        parts.append(re.escape(before[cursor:start]))
        parts.append("([\\s\\S]*?)")
        cursor = end
    parts.append(re.escape(before[cursor:]))
    match = re.fullmatch("".join(parts), after)
    if match is None:
        return None
    return list(match.groups())
