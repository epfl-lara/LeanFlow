"""Protect original Lean bytes and replace only lexically identified sorry holes."""

from __future__ import annotations

import re
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


def lean_code_mask(source: str) -> str:
    """Blank comments, strings, and quoted names while preserving byte-position indices."""
    chars = list(source)
    index = 0
    while index < len(source):
        end = index
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
            for position in range(index, min(end, len(chars))):
                if chars[position] != "\n":
                    chars[position] = " "
            index = end
        else:
            index += 1
    return "".join(chars)


def sorry_spans(source: str) -> list[tuple[int, int]]:
    """Locate literal sorry tokens, excluding names such as sorry' and Foo.sorry."""
    return [
        (match.start(), match.end())
        for match in re.finditer(r"(?<![\w'.])sorry(?![\w'])", lean_code_mask(source))
    ]


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
        r"builtin_initialize|run_cmd|deriving)(?![\w'])|#[A-Za-z_]",
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
        if self.imports:
            imports = "\n".join(f"import {module}" for module in self.imports) + "\n"
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
    """Enumerate user Lean sources without dependencies or workflow scratch files."""
    return sorted(
        path
        for path in root.rglob("*.lean")
        if not any(
            part in {".lake", ".git", ".leanflow", ".venv"} for part in path.relative_to(root).parts
        )
        and not path.is_symlink()
    )


def discover(
    root: Path, targets: list[Path], *, fill_definitions: bool
) -> tuple[Dag, dict[str, SourceDocument]]:
    """Snapshot all original Lean source and enumerate authorized holes in requested files."""
    documents = {
        str(path.relative_to(root)): SourceDocument(str(path.relative_to(root)), read_source(path))
        for path in project_sources(root)
    }
    dag = Dag()
    for path in targets:
        relative = str(path.resolve().relative_to(root))
        document = documents.get(relative)
        if document is None:
            raise ValueError(f"target is not a regular project Lean file: {relative}")
        source = document.baseline
        spans = sorry_spans(source)
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
            kind = str(entry["kind"])
            if kind not in {"theorem", "lemma", "example"} and not fill_definitions:
                raise ValueError(
                    f"definition {entry['name']} contains sorry; enable fill_definitions explicitly"
                )
            name = str(entry["name"])
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
                    module=relative[:-5].replace("/", "."),
                    kind=kind,
                    original=True,
                    holes=holes,
                    line_start=entry["line"],
                    line_end=entry["end_line"],
                )
            )
            dag.roots.append(node_id)
        if len(assigned) != len(spans):
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
