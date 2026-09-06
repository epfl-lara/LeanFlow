"""Reuse parent-owned Lean acceptance only while all checked source inputs are identical."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.models import Node, digest

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def submission_key(runtime: ProverRuntime, node: Node, source: str) -> str:
    """Fingerprint the exact candidate, protected type, policy, and every mutable source."""
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
                "documents": {
                    path: digest(document.render()) for path, document in runtime.documents.items()
                },
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
