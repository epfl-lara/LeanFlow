"""Define and validate the small dependency graph used by the prover controller."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any


def digest(text: str) -> str:
    """Return a stable revision identity for source or a proposed statement."""
    return hashlib.sha256(text.encode()).hexdigest()


def safe_relative_file(value: str) -> str:
    """Reject absolute paths and traversal before resolving a planned module."""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or path.suffix != ".lean":
        raise ValueError(f"invalid Lean source path: {value}")
    if not path.parts or path.parts[0] in {".lake", ".git", ".leanflow"}:
        raise ValueError(f"reserved Lean source path: {value}")
    return str(path)


@dataclass
class Node:
    """Track a declaration, its authorized holes, and its independently checked status."""

    id: str
    name: str
    statement: str
    file: str
    module: str
    informal_justification: str = ""
    dependencies: list[str] = field(default_factory=list)
    kind: str = "theorem"
    status: str = "pending"
    line_start: int = 0
    line_end: int = 0
    conditional_dependencies: list[str] = field(default_factory=list)
    original: bool = False
    holes: list[int] = field(default_factory=list)
    attempts: int = 0
    decompositions: int = 0
    revision: int = 0
    candidate: list[str] = field(default_factory=list)
    notes: str = ""
    proof_sha256: str = ""
    signature_sha256: str = ""
    signature_mutable_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Dag:
    """Own theorem dependencies with edges directed from goals to prerequisites."""

    nodes: list[Node] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    revision: int = 0

    def by_id(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}

    def validate(self, max_nodes: int = 128) -> None:
        """Reject ambiguous identities, missing goals, and dependency cycles."""
        index = self.by_id()
        if len(index) != len(self.nodes) or len(self.nodes) > max_nodes:
            raise ValueError("DAG contains duplicate IDs or exceeds its node limit")
        if any(root not in index for root in self.roots):
            raise ValueError("DAG root does not exist")
        names: set[tuple[str, str]] = set()
        for node in self.nodes:
            safe_relative_file(node.file)
            if not node.id or not node.name or not node.statement:
                raise ValueError("DAG nodes require id, name, and statement")
            identity = (node.file, node.name)
            if identity in names:
                raise ValueError("duplicate declaration in DAG")
            names.add(identity)
            if len(set(node.dependencies)) != len(node.dependencies):
                raise ValueError("duplicate dependency")
            if any(dep not in index for dep in node.dependencies):
                raise ValueError(f"missing dependency of {node.id}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("DAG contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in index[node_id].dependencies:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in index:
            visit(node_id)

    def affected(self, changed: str) -> set[str]:
        """Return a changed node and every transitive dependent."""
        affected = {changed}
        while True:
            larger = affected | {
                node.id for node in self.nodes if any(dep in affected for dep in node.dependencies)
            }
            if larger == affected:
                return affected
            affected = larger

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "roots": self.roots,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Dag:
        dag = cls(
            nodes=[Node(**node) for node in value.get("nodes", [])],
            roots=list(value.get("roots", [])),
            revision=int(value.get("revision", 0)),
        )
        dag.validate(max(128, len(dag.nodes)))
        return dag
