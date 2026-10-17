"""Resolve TeX include occurrences into an unabridged provenance-preserving stream."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes

from .source_bibliography import (
    BIBLIOGRAPHY_MAX_BYTES,
    BibliographySizeLimitError,
    OversizedBibliographyFile,
    check_bibliography_size,
    discover_bibliography_support,
)
from .source_effects import LITERAL_FILE_COMMANDS
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
    includeonly_origin: tuple[str, int, int] | None = None

    def diagnostic(self, code: str, message: str, file: str, **extra: Any) -> None:
        """Record a blocking extraction uncertainty instead of declaring success."""
        self.diagnostics.append(
            {"code": code, "message": message, "file": file, "blocking": True, **extra}
        )

    def read(self, path: Path, *, inherited_uncertain: bool = False) -> TexScan:
        """Read original bytes once and reuse their exact hash throughout extraction."""
        relative = path.relative_to(self.root).as_posix()
        if relative in self.scans:
            return self.scans[relative]
        original = SourceText(relative, read_regular_bytes(path, path.lstat().st_size))

        scan = scan_tex(original, inherited_uncertain=inherited_uncertain)
        self.files[relative] = {
            "sha256": original.sha256,
            "encoding": original.encoding,
            "text": original.text,
            "size": len(original.raw),
        }
        self.scans[relative] = scan
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

    def _binary(self, path: Path, *, max_bytes: int | None = None) -> None:
        """Freeze one support original under its fixed role-specific bound when supplied."""
        if max_bytes is None and path.suffix.lower() in {".bib", ".bbl"}:
            max_bytes = BIBLIOGRAPHY_MAX_BYTES
        relative = path.relative_to(self.root).as_posix()
        if (
            max_bytes is not None
            and relative in self.files
            and self.files[relative]["size"] > max_bytes
        ):
            raise BibliographySizeLimitError(path, self.files[relative]["size"], max_bytes)
        if relative not in self.files:
            if max_bytes is not None:
                check_bibliography_size(path, max_bytes)
            raw = read_regular_bytes(
                path, max_bytes if max_bytes is not None else path.lstat().st_size
            )
            self.files[relative] = {"sha256": digest(raw), "encoding": "binary", "size": len(raw)}
            if path.suffix.lower() in {".bib", ".bbl"}:
                try:
                    text = raw.decode("utf-8")
                    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                        raise ValueError("Bibliography contains non-text control bytes")
                except (UnicodeDecodeError, ValueError):
                    self.diagnostic(
                        "bibliography_encoding_unconfirmed",
                        "Bibliography bytes are preserved; supply a verified UTF-8 transcription for review",
                        relative,
                    )
                else:
                    self.files[relative].update(encoding="utf-8", text=text)

    def _oversized_bibliography(
        self,
        path: Path,
        unavailable: OversizedBibliographyFile,
        span: dict[str, Any],
        *,
        command: str = "bibliography",
        rendered_available: bool = False,
    ) -> None:
        """Explain an unread size-limited support input without waiving source uncertainty."""
        optional_database = unavailable.role == "database" and rendered_available
        self.diagnostics.append(
            {
                "code": (
                    "missing_bibliography_database" if optional_database else "missing_support_file"
                ),
                "message": (
                    "Supplied root-jobname .bbl is frozen, but database exceeds the bounded intake size"
                    if optional_database
                    else "Bibliography support exceeds the bounded intake size"
                ),
                "file": path.relative_to(self.root).as_posix(),
                "blocking": not optional_database,
                "span": span,
                "support_command": command,
                "support_target": unavailable.target,
                "support_file": unavailable.path.relative_to(self.root).as_posix(),
                "support_role": unavailable.role,
                "support_unavailable_reason": "size_limit",
                "support_size_bytes": unavailable.size_bytes,
                "support_limit_bytes": unavailable.limit_bytes,
            }
        )

    def _bibliography(self, path: Path, value: str, span: dict[str, Any]) -> None:
        """Retain supplied bibliography bytes without claiming execution or reproducibility."""
        support = discover_bibliography_support(self.root, self.selected, path, value, self.resolve)
        relative = path.relative_to(self.root).as_posix()
        for candidate in support.files:
            self._binary(candidate, max_bytes=BIBLIOGRAPHY_MAX_BYTES)
        for unavailable in support.oversized_files:
            self._oversized_bibliography(
                path, unavailable, span, rendered_available=support.rendered_available
            )
        for token in support.missing_targets:
            self.diagnostic(
                "missing_support_file",
                f"Cannot resolve \\bibliography{{{token}}}",
                relative,
                span=span,
                support_command="bibliography",
                support_target=token,
            )
        if support.missing_rendered:
            self.diagnostic(
                "missing_support_file",
                "Bibliography databases and exact root-jobname rendered support are unavailable",
                relative,
                span=span,
                support_command="bibliography",
                support_role="rendered_bibliography",
                support_target=self.selected.with_suffix(".bbl").relative_to(self.root).as_posix(),
            )
        if support.ambiguous_jobnames:
            self.diagnostic(
                "ambiguous_bibliography",
                "Multiple supplied root-jobname bibliographies require reconciliation",
                relative,
                span=span,
                support_command="bibliography",
                support_candidates=[
                    candidate.relative_to(self.root).as_posix()
                    for candidate in support.ambiguous_jobnames
                ],
                support_case_aliases=[
                    candidate.relative_to(self.root).as_posix()
                    for candidate in support.case_aliases
                ],
            )
        for token in support.missing_databases:
            self.diagnostics.append(
                {
                    "code": "missing_bibliography_database",
                    "message": "Supplied root-jobname .bbl is frozen, but database source is unavailable",
                    "file": relative,
                    "blocking": False,
                    "span": span,
                    "support_command": "bibliography",
                    "support_target": token,
                }
            )

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
            scan = self.read(path, inherited_uncertain=bool(ancestors))
        except OSError as error:
            occurrence["status"] = "unresolved"
            self.diagnostic("source_read_error", str(error), relative, occurrence_id=occurrence_id)
            return
        marks = self.occurrence_marks.setdefault(occurrence_id, [])
        if excluded:
            marks.append((0, len(scan.source.text), "excluded", "Excluded by includeonly"))
            return
        self.diagnostics.extend(
            {
                **row,
                "occurrence_id": occurrence_id,
                "span": {**row["span"], "occurrence_id": occurrence_id},
            }
            for row in scan.diagnostics
        )
        if not ancestors:
            selections = list(re.finditer(r"\\includeonly\b", scan.visible))
            if selections:
                facts = {start: (end, names) for start, end, names in scan.includeonly_definitions}
                selected = selections[0]
                fact = facts.get(selected.start())
                if len(selections) != 1 or fact is None or fact[1] is None:
                    self.diagnostic(
                        "unresolved_includeonly",
                        "includeonly execution, scope or assignment order is uncertain; retain all includes",
                        relative,
                        span=scan.source.span(selected.start(), selected.end()),
                    )
                else:
                    self.includeonly = set(fact[1])
                    self.includeonly_origin = relative, selected.start(), fact[0]
        cursor = 0
        counters: dict[tuple[str, str], int] = {}
        for command in COMMAND.finditer(scan.visible):
            name = command.group(1).rstrip("*")
            if name not in LITERAL_FILE_COMMANDS:
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
                        would_skip = (
                            name == "include"
                            and self.includeonly is not None
                            and token.strip().removesuffix(".tex") not in self.includeonly
                        )
                        # The scanner records an exact direct directive, but
                        # unknown intervening execution can change its meaning.
                        origin = self.includeonly_origin
                        known_before = (
                            origin is not None
                            and origin[0] == relative
                            and origin[2] <= command.start()
                            and not any(
                                disposition == "unresolved"
                                and start <= max(0, command.start() - 1) < finish
                                for start, finish, disposition, _ in scan.marks
                            )
                        )
                        skip = would_skip and known_before
                        if would_skip and not known_before:
                            self.diagnostic(
                                "unresolved_includeonly",
                                "includeonly authority is not known before this include; retain its source",
                                relative,
                                span=scan.source.span(command.start(), end),
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
            elif name == "bibliography":
                self._bibliography(path, value, scan.source.span(command.start(), end))
            else:
                suffixes = (
                    (".bib", ".bbl")
                    if name == "addbibresource"
                    else (".pdf", ".png", ".jpg", ".jpeg", ".eps")
                )
                for token in value.split(","):
                    resolved = self.resolve(path, token, suffixes)
                    if resolved:
                        try:
                            self._binary(
                                resolved,
                                max_bytes=(
                                    BIBLIOGRAPHY_MAX_BYTES if name == "addbibresource" else None
                                ),
                            )
                        except BibliographySizeLimitError as error:
                            self._oversized_bibliography(
                                path,
                                OversizedBibliographyFile(
                                    resolved,
                                    token.strip(),
                                    (
                                        "database"
                                        if resolved.suffix.lower() == ".bib"
                                        else "bibliography_context"
                                    ),
                                    error.size_bytes,
                                    error.limit_bytes,
                                ),
                                scan.source.span(command.start(), end),
                                command=name,
                            )
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
