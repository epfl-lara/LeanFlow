"""Reuse parent-owned Lean acceptance only while all checked source inputs are identical."""

from __future__ import annotations

import copy
import json
import re
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.models import Node, digest
from leanflow_cli.workflows.prover.source import lean_code_mask

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

_IMPORT_COMMAND = re.compile(r"(?m)^[ \t]*import(?=[ \t])")
_IMPORT_WORD = re.compile(r"(?<![\w'.])import(?![\w'])")
_MODULE_COMPONENT = r"(?:«[^»./\\\r\n]+»|[^\W\d][\w']*)"
_IMPORT_NAME = re.compile(rf"[ \t]+({_MODULE_COMPONENT}(?:\.{_MODULE_COMPONENT})*)")


def imported_modules(source: str) -> list[str]:
    """Read simple import headers; reject other syntax so cache keys fail closed."""
    masked = lean_code_mask(source)
    commands = list(_IMPORT_COMMAND.finditer(masked))
    if {match.end() - len("import") for match in commands} != {
        match.start() for match in _IMPORT_WORD.finditer(masked)
    }:
        raise ValueError("unsupported import command layout")
    modules: list[str] = []
    for match in commands:
        # Read quoted names from original bytes; comments may follow a complete
        # name, but unfamiliar prefixes or continuations require the broad key.
        name = _IMPORT_NAME.match(source, match.end())
        line_end = source.find("\n", match.end())
        if line_end < 0:
            line_end = len(source)
        if name is None or masked[name.end() : line_end].strip():
            raise ValueError("unsupported import module syntax")
        modules.append(name.group(1).replace("«", "").replace("»", ""))
    return modules


def import_closure(runtime: ProverRuntime, file: str) -> dict[str, str]:
    """Fingerprint every managed source this file imports, directly or transitively.

    Only these renders determine the compiled environment a candidate was
    checked against, so publishing an unrelated helper cannot change the
    verdict. The file itself is excluded: the exact checked source carries it.
    An unparseable header fails closed to fingerprinting every managed source.
    """
    by_module = {path[:-5].replace("/", "."): path for path in runtime.documents}
    closure: dict[str, str] = {}
    pending = [file]
    seen = {file}
    while pending:
        path = pending.pop()
        document = runtime.documents.get(path)
        if document is None:
            continue
        rendered = document.render()
        if path != file:
            closure[path] = digest(rendered)
        try:
            modules = imported_modules(rendered)
        except ValueError:
            return {
                path: digest(document.render())
                for path, document in runtime.documents.items()
                if path != file
            }
        for module in modules:
            target = by_module.get(module)
            if target is not None and target not in seen:
                seen.add(target)
                pending.append(target)
    return closure


def submission_key(runtime: ProverRuntime, node: Node, source: str) -> str:
    """Fingerprint the exact candidate, protected type, policy, and every imported source."""
    return digest(
        json.dumps(
            {
                "source": source,
                "node": node.id,
                "name": node.name,
                "file": node.file,
                "original": node.original,
                "verifier": id(runtime.verifier),
                "revision": node.revision,
                "statement": node.statement,
                "signature": node.signature_sha256,
                "mutable_names": node.signature_mutable_names,
                "dependencies": [
                    {
                        "id": dep,
                        "status": runtime.dag.by_id()[dep].status,
                        "signature": runtime.dag.by_id()[dep].signature_sha256,
                        "proof": runtime.dag.by_id()[dep].proof_sha256,
                    }
                    for dep in node.dependencies
                ],
                "axioms": runtime.config.allowed_axioms,
                "environment": {
                    name: (
                        digest((runtime.root / name).read_text())
                        if (runtime.root / name).is_file()
                        else None
                    )
                    for name in (
                        "lean-toolchain",
                        "lakefile.toml",
                        "lakefile.lean",
                        "lake-manifest.json",
                    )
                },
                "imports": import_closure(runtime, node.file),
            },
            sort_keys=True,
        )
    )


class SubmissionCache:
    """Retain at most one verified result per node, only in controller memory.

    No model result or saved JSON can populate this cache. Conditional proofs
    and input changes during verification are deliberately excluded.
    """

    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, dict[str, Any]]] = {}

    def remember(
        self, node: Node, before: str, after: str, result: dict[str, Any], *, conditional: bool
    ) -> None:
        """Retain a closed independent acceptance only if its inputs stayed unchanged."""
        if not conditional and before == after and result.get("accepted") is True:
            self.entries[node.id] = (before, copy.deepcopy(result))

    def take(self, node: Node, key: str) -> dict[str, Any] | None:
        """Consume a matching acceptance once; discard stale evidence."""
        entry = self.entries.pop(node.id, None)
        return copy.deepcopy(entry[1]) if entry is not None and entry[0] == key else None
