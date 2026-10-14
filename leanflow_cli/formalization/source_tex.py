"""Resolve TeX include occurrences into an unabridged provenance-preserving stream."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .source_provenance import COMMAND, SourceText, TexScan, digest, group, identity, scan_tex


@dataclass
class Fragment:
    """Locate an expanded stream fragment in one original include occurrence."""

    scan: TexScan
    occurrence_id: str
    start: int
    end: int
    offset: int = 0


@dataclass
class TexBundle:
    """Collect original files, ordered occurrences, and an expandable lexical stream."""

    root: Path
    selected: Path
    document_id: str
    scans: dict[str, TexScan] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    fragments: list[Fragment] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    occurrence_marks: dict[str, list[tuple[int, int, str, str]]] = field(default_factory=dict)
    includeonly: set[str] | None = None

    def diagnostic(self, code: str, message: str, file: str, **extra: Any) -> None:
        """Record a blocking extraction uncertainty instead of declaring success."""
        self.diagnostics.append(
            {"code": code, "message": message, "file": file, "blocking": True, **extra}
        )

    def read(self, path: Path) -> TexScan:
        """Read original bytes once and reuse their exact hash throughout extraction."""
        relative = path.relative_to(self.root).as_posix()
        if relative in self.scans:
            return self.scans[relative]
        original = SourceText(relative, path.read_bytes())
        scan = scan_tex(original)
        self.files[relative] = {
            "sha256": original.sha256,
            "encoding": original.encoding,
            "text": original.text,
            "size": len(original.raw),
        }
        self.scans[relative] = scan
        self.diagnostics.extend(scan.diagnostics)
        if original.encoding != "utf-8":
            self.diagnostic(
                "encoding_unconfirmed",
                "Original bytes are preserved, but a non-UTF-8 encoding needs confirmation",
                relative,
            )
        return scan

    def resolve(self, parent: Path, value: str, suffixes: tuple[str, ...]) -> Path | None:
        """Resolve literal local references without following paths outside the bundle."""
        if any(char in value for char in "\\#{}") or not value.strip():
            return None
        candidate = Path(value.strip())
        for base in (parent.parent, self.selected.parent, self.root):
            for suffix in ("", *suffixes):
                original = base / (str(candidate) + suffix)
                cursor = original
                alias = False
                while cursor != self.root and cursor != cursor.parent:
                    alias = alias or cursor.is_symlink()
                    cursor = cursor.parent
                if alias:
                    continue
                path = original.resolve()
                if not path.is_relative_to(self.root):
                    continue
                if path.is_file():
                    return path
        return None

    def _binary(self, path: Path) -> None:
        relative = path.relative_to(self.root).as_posix()
        if relative not in self.files:
            raw = path.read_bytes()
            self.files[relative] = {"sha256": digest(raw), "encoding": "binary", "size": len(raw)}

    def expand(
        self,
        path: Path,
        occurrence_key: str,
        ancestors: tuple[Path, ...] = (),
        invocation: dict[str, Any] | None = None,
        excluded: bool = False,
    ) -> None:
        """Visit each inclusion separately, retaining cycles and skipped includes as evidence."""
        relative = path.relative_to(self.root).as_posix()
        occurrence_id = identity("occ", self.document_id + ":" + occurrence_key)
        occurrence: dict[str, Any] = {
            "id": occurrence_id,
            "file": relative,
            "context": occurrence_key,
            "invocation_span": invocation,
            "status": "excluded" if excluded else "included",
        }
        self.occurrences.append(occurrence)
        if path in ancestors or len(ancestors) >= 128:
            occurrence["status"] = "unresolved"
            self.diagnostic(
                "include_cycle" if path in ancestors else "include_depth",
                "Include expansion cannot be completed",
                relative,
                occurrence_id=occurrence_id,
            )
            return
        try:
            scan = self.read(path)
        except OSError as error:
            occurrence["status"] = "unresolved"
            self.diagnostic("source_read_error", str(error), relative, occurrence_id=occurrence_id)
            return
        marks = self.occurrence_marks.setdefault(occurrence_id, [])
        if excluded:
            marks.append((0, len(scan.source.text), "excluded", "Excluded by includeonly"))
            return
        if not ancestors:
            selected = re.search(r"\\includeonly\s*\{([^{}]*)\}", scan.visible)
            if selected:
                if any(char in selected[1] for char in "\\#"):
                    self.diagnostic(
                        "unresolved_includeonly",
                        "includeonly contains an unevaluated macro",
                        relative,
                    )
                else:
                    self.includeonly = {
                        value.strip().removesuffix(".tex") for value in selected[1].split(",")
                    }
        cursor = 0
        counters: dict[tuple[str, str], int] = {}
        for command in COMMAND.finditer(scan.visible):
            name = command.group(1).rstrip("*")
            if name not in {
                "input",
                "include",
                "subfile",
                "usepackage",
                "RequirePackage",
                "documentclass",
                "includegraphics",
                "bibliography",
                "addbibresource",
            }:
                continue
            argument_pos = command.end()
            options = group(scan.visible, argument_pos, "[")
            if options:
                argument_pos = options[1]
            argument = group(scan.visible, argument_pos)
            if argument is None and name == "input":
                plain = re.match(r"\s*([^\s{}%]+)", scan.visible[argument_pos:])
                argument = (plain[1], argument_pos + plain.end()) if plain else None
            if argument is None:
                self.diagnostic(
                    "unresolved_reference",
                    f"Cannot resolve argument to \\{name}",
                    relative,
                    span=scan.source.span(command.start(), command.end()),
                )
                continue
            value, end = argument
            marks.append((command.start(), end, "supporting_context", "Source reference"))
            if name in {
                "input",
                "include",
                "subfile",
                "usepackage",
                "RequirePackage",
                "documentclass",
            }:
                self.fragments.append(Fragment(scan, occurrence_id, cursor, command.start()))
                cursor = end
                for token in value.split(","):
                    suffix = (
                        ".sty"
                        if name in {"usepackage", "RequirePackage"}
                        else ".cls" if name == "documentclass" else ".tex"
                    )
                    resolved = self.resolve(path, token, (suffix,))
                    counter_key = (name, token)
                    counters[counter_key] = counters.get(counter_key, 0) + 1
                    child_key = (
                        occurrence_key + "/" + name + ":" + token + ":" + str(counters[counter_key])
                    )
                    if resolved:
                        skip = (
                            name == "include"
                            and self.includeonly is not None
                            and token.strip().removesuffix(".tex") not in self.includeonly
                        )
                        self.expand(
                            resolved,
                            child_key,
                            (*ancestors, path),
                            scan.source.span(command.start(), end, occurrence_id=occurrence_id),
                            skip,
                        )
                    elif suffix == ".tex" or any(char in token for char in "\\#{}"):
                        self.diagnostic(
                            "missing_include",
                            f"Cannot resolve \\{name}{{{token}}}",
                            relative,
                            span=scan.source.span(command.start(), end),
                        )
                    else:
                        # External TeX distribution packages are named, never implicitly frozen.
                        occurrence.setdefault("external_packages", []).append(token.strip())
            else:
                suffixes = (
                    (".bib", ".bbl")
                    if name in {"bibliography", "addbibresource"}
                    else (".pdf", ".png", ".jpg", ".jpeg", ".eps")
                )
                for token in value.split(","):
                    resolved = self.resolve(path, token, suffixes)
                    if resolved:
                        self._binary(resolved)
                    else:
                        self.diagnostic(
                            "missing_support_file",
                            f"Cannot resolve \\{name}{{{token}}}",
                            relative,
                            span=scan.source.span(command.start(), end),
                            support_command=name,
                            support_target=token.strip(),
                        )
        self.fragments.append(Fragment(scan, occurrence_id, cursor, len(scan.visible)))

    def expanded(self) -> str:
        """Build the structural stream while preserving a map back to original files."""
        pieces: list[str] = []
        cursor = 0
        for fragment in self.fragments:
            fragment.offset = cursor
            piece = fragment.scan.visible[fragment.start : fragment.end]
            pieces.append(piece)
            cursor += len(piece)
        return "".join(pieces)

    def spans(self, start: int, end: int) -> list[dict[str, Any]]:
        """Project an expanded interval to exact original spans, including occurrence identity."""
        result: list[dict[str, Any]] = []
        for fragment in self.fragments:
            first, last = max(start, fragment.offset), min(
                end, fragment.offset + fragment.end - fragment.start
            )
            if first < last:
                result.append(
                    fragment.scan.source.span(
                        fragment.start + first - fragment.offset,
                        fragment.start + last - fragment.offset,
                        occurrence_id=fragment.occurrence_id,
                    )
                )
        return result

    def original_text(self, spans: list[dict[str, Any]]) -> str:
        """Reconstruct the complete original material represented by projected spans."""
        return "".join(
            self.scans[span["file"]]
            .source.raw[span["byte_start"] : span["byte_end"]]
            .decode(self.scans[span["file"]].source.encoding)
            for span in spans
        )
