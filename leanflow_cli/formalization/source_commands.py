"""Record command provenance and expose expansion uncertainty to structural adapters."""

from __future__ import annotations

from typing import Any

from .source_builtins import LATEX_BUILTINS
from .source_effects import TEX_IO_EXECUTION_COMMANDS
from .source_numeric_conditionals import CONDITIONAL_PRIMITIVES
from .source_provenance import COMMAND, DEFINITION_COMMANDS, identity
from .source_tex import TexBundle


def command_references(bundle: TexBundle, expanded: str) -> list[dict[str, Any]]:
    """Identify local definitions and retain unresolved commands without expanding them."""
    definitions: dict[str, list[str]] = {}
    for scan in bundle.scans.values():
        for definition in scan.definitions:
            definitions.setdefault(definition["name"], []).append(definition["id"])
    references: list[dict[str, Any]] = []
    unknown: dict[str, list[dict[str, Any]]] = {}
    effects: dict[str, list[dict[str, Any]]] = {}
    for command in COMMAND.finditer(expanded):
        name = command[1].rstrip("*")
        spans = bundle.spans(command.start(), command.end())
        known = (
            name
            in LATEX_BUILTINS
            | DEFINITION_COMMANDS
            | CONDITIONAL_PRIMITIVES
            | TEX_IO_EXECUTION_COMMANDS
            or name in {"newif", "else", "fi", "relax", "endinput"}
            or (len(name) == 1 and not name.isalpha())
        )
        resolution = (
            "local_definition"
            if name in definitions
            else "latex_builtin" if known else "unresolved"
        )
        reference = {
            "id": identity("command", bundle.document_id + ":" + str(command.start()) + ":" + name),
            "name": name,
            "spans": spans,
            "definition_refs": definitions.get(name, []),
            "resolution": resolution,
        }
        references.append(reference)
        if name in TEX_IO_EXECUTION_COMMANDS:
            # Known effects remain hard even when an earlier opaque command
            # already made structural trust uncertain, or a local macro shadows
            # their names. A proposed lexical meaning cannot certify execution.
            effects.setdefault(name, []).extend(spans)
        elif resolution == "unresolved":
            unknown.setdefault(name, []).extend(spans)
    for name, spans in effects.items():
        bundle.diagnostic(
            "unsupported_tex_effect",
            f"Command \\{name} has TeX I/O or execution effects; lexical interpretation cannot resolve them",
            spans[0]["file"] if spans else "",
            spans=spans,
        )
    for name, spans in unknown.items():
        bundle.diagnostic(
            "unresolved_command",
            f"Command \\{name} needs package, adapter, or manual semantic reconciliation",
            spans[0]["file"] if spans else "",
            spans=spans,
        )
    return references
