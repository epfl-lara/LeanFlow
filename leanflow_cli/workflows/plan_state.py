"""Living plan-state artifacts for the /prove redesign (Phase 1, specs P1.1).

Owns the documentation-driven proving substrate: the dependency graph
(``blueprint.json`` — machine authority; "dependency graph", never bare
"blueprint", to avoid colliding with the formalization ``Blueprint.md``),
the machine summary (``summary.json``), the human render (``plan.md``,
regenerated sections + a preserved free-form Notes tail), and the append-only
lab notebook (``journal.jsonl`` — the source of truth; snapshots are
rebuildable from it).

Everything here no-ops (or returns empty state) unless ``LEANFLOW_PLAN_STATE``
is truthy, so the flag-off hot path is byte-identical. Writes are crash-atomic
(``core.utils.atomic_json_write``); the blueprint ``revision`` check turns an
accidental second writer into a loud conflict instead of a lost update
(Phase 1 invariant: single writer = the native runner process).

Kernel-truth rules enforced here: ``proved`` is writable only through the
gate-accept sync path (``via_gate=True``); ``false`` only through negation
promotion (Phase 3 — the status ships now so the schema doesn't churn);
``reconcile`` is the anti-drift pass and may downgrade ``proved`` when the
on-disk declaration regressed, but never promotes to ``proved``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.queue_models import TheoremKey
from leanflow_cli.workflows.workflow_json_io import read_json_file
from leanflow_cli.workflows.workflow_state import _locked_append
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

logger = logging.getLogger(__name__)

NODE_STATUSES = (
    "conjectured",
    "stated",
    "audited",
    "proving",
    "proved",
    "blocked",
    "false",
    "split",
    "parked",
)
EDGE_KINDS = ("depends_on", "split_of", "evidence", "alternative_of")
# N1 terminal vocabulary — the only writable final-report statuses. The
# render-only "in-progress" default is NOT writable: a run may not end there.
FINAL_REPORT_STATUSES = ("proved", "disproved", "documented")

PLAN_MD_GENERATED_MARKER = "<!-- generated: do not edit above the Notes section -->"
_NOTES_HEADING = "## Notes"


class PlanStateRevisionConflict(RuntimeError):
    """The dependency graph on disk moved past the revision this write is based on."""


try:  # POSIX advisory locking (same degradation policy as workflow_state._locked_append)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _blueprint_write_lock(path: Path) -> Iterator[None]:
    """Serialize the read-check-write revision transaction across processes.

    Closes the TOCTOU window in save_blueprint: without it two writers could
    both read revision N and both write N+1, losing one update silently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with _WRITE_LOCK, lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                logger.debug(
                    "flock unavailable for %s; write not cross-process locked",
                    lock_path,
                    exc_info=True,
                )
        yield


def plan_state_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_PLAN_STATE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class PlanStatePaths:
    plan_md: Path
    summary_json: Path
    blueprint_json: Path
    journal_jsonl: Path


def plan_state_paths(state_root: Path | None = None) -> PlanStatePaths:
    """Resolve the artifact paths under the workflow-state root.

    Precedence: the ``LEANFLOW_PLAN_STATE_DIR`` override (test convenience),
    then an explicit ``state_root`` (used when the caller already knows the
    target project — e.g. building a child env before spawn), then discovery.
    """
    override = str(os.getenv("LEANFLOW_PLAN_STATE_DIR", "") or "").strip()
    if override:
        root = Path(override).expanduser()
    elif state_root is not None:
        root = state_root
    else:
        root = workflow_state_root()
    return PlanStatePaths(
        plan_md=root / "plan.md",
        summary_json=root / "summary.json",
        blueprint_json=root / "blueprint.json",
        journal_jsonl=root / "journal.jsonl",
    )


def node_id_for(target_symbol: str, active_file: str) -> str:
    """Stable node id reusing TheoremKey's normalized identity."""
    storage_key = TheoremKey.make(target_symbol, active_file).storage_key()
    return "n" + hashlib.sha1(storage_key.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str = "theorem"  # theorem | lemma | def | conjecture
    name: str = ""
    file: str = ""
    statement: str = ""
    status: str = "stated"
    attempts: int = 0
    api_steps: int = 0
    owner: str = ""
    notes: str = ""
    decision_packets: tuple[str, ...] = ()
    generated_by: str = ""  # decomposer | planner | empirical | human | queue-sync

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GraphNode:
        status = str(raw.get("status", "stated") or "stated")
        return cls(
            id=str(raw.get("id", "") or ""),
            kind=str(raw.get("kind", "theorem") or "theorem"),
            name=str(raw.get("name", "") or ""),
            file=str(raw.get("file", "") or ""),
            statement=str(raw.get("statement", "") or ""),
            status=status if status in NODE_STATUSES else "stated",
            attempts=int(raw.get("attempts", 0) or 0),
            api_steps=int(raw.get("api_steps", 0) or 0),
            owner=str(raw.get("owner", "") or ""),
            notes=str(raw.get("notes", "") or ""),
            decision_packets=tuple(str(p) for p in (raw.get("decision_packets") or []) if str(p)),
            generated_by=str(raw.get("generated_by", "") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "file": self.file,
            "statement": self.statement,
            "status": self.status,
            "attempts": self.attempts,
            "api_steps": self.api_steps,
            "owner": self.owner,
            "notes": self.notes,
            "decision_packets": list(self.decision_packets),
            "generated_by": self.generated_by,
        }


@dataclass(frozen=True)
class GraphEdge:
    source: str  # serialized as "from"
    target: str  # serialized as "to"
    kind: str = "depends_on"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GraphEdge:
        kind = str(raw.get("kind", "depends_on") or "depends_on")
        return cls(
            source=str(raw.get("from", "") or ""),
            target=str(raw.get("to", "") or ""),
            kind=kind if kind in EDGE_KINDS else "depends_on",
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "kind": self.kind}


@dataclass(frozen=True)
class Blueprint:
    """The dependency graph snapshot (kernel-reconciled; journal-rebuildable)."""

    goal: str = ""
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    revision: int = 0
    updated_at: str = ""

    def node_by_id(self, node_id: str) -> GraphNode | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def replace_node(self, node: GraphNode) -> Blueprint:
        nodes = tuple(node if existing.id == node.id else existing for existing in self.nodes)
        if all(existing.id != node.id for existing in self.nodes):
            nodes = (*self.nodes, node)
        return replace(self, nodes=nodes)

    def frontier(self) -> tuple[GraphNode, ...]:
        """Stated nodes whose depends_on targets are all proved."""
        by_id = {node.id: node for node in self.nodes}
        out: list[GraphNode] = []
        for node in self.nodes:
            if node.status != "stated":
                continue
            deps = [
                by_id.get(edge.target)
                for edge in self.edges
                if edge.kind == "depends_on" and edge.source == node.id
            ]
            if all(dep is not None and dep.status == "proved" for dep in deps) or not deps:
                out.append(node)
        return tuple(out)

    def invalidate_false_subtree(self, node_id: str) -> Blueprint:
        """Mark ``node_id`` false and poison its split_of ancestors to conjectured.

        A kernel-proved negation of a sub-lemma means the decomposition that
        stated it was wrong: every ancestor along split_of edges drops back to
        ``conjectured`` — except ``proved`` ancestors, which are immutable
        kernel facts and keep their status.
        """
        bp = self
        node = bp.node_by_id(node_id)
        if node is None:
            return bp
        bp = bp.replace_node(replace(node, status="false"))
        parents_of = {edge.source: edge.target for edge in bp.edges if edge.kind == "split_of"}
        seen: set[str] = set()
        cursor = parents_of.get(node_id)
        while cursor and cursor not in seen:
            seen.add(cursor)
            ancestor = bp.node_by_id(cursor)
            if ancestor is not None and ancestor.status not in {"proved", "false"}:
                bp = bp.replace_node(replace(ancestor, status="conjectured"))
            cursor = parents_of.get(cursor)
        return bp

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Blueprint:
        return cls(
            goal=str(raw.get("goal", "") or ""),
            nodes=tuple(
                GraphNode.from_mapping(node)
                for node in (raw.get("nodes") or [])
                if isinstance(node, Mapping)
            ),
            edges=tuple(
                GraphEdge.from_mapping(edge)
                for edge in (raw.get("edges") or [])
                if isinstance(edge, Mapping)
            ),
            revision=int(raw.get("revision", 0) or 0),
            updated_at=str(raw.get("updated_at", "") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": 1,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "goal": self.goal,
            "nodes": [node.to_mapping() for node in self.nodes],
            "edges": [edge.to_mapping() for edge in self.edges],
        }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_blueprint() -> Blueprint:
    """Tolerant read: empty graph on a missing file (corruption raises loudly)."""
    if not plan_state_enabled():
        return Blueprint()
    return Blueprint.from_mapping(read_json_file(plan_state_paths().blueprint_json))


def save_blueprint(bp: Blueprint) -> Blueprint:
    """Atomically persist ``bp`` with a bumped revision.

    Refuses a stale-revision write: if the on-disk revision moved past the
    revision this ``bp`` was loaded at, raise :class:`PlanStateRevisionConflict`
    (journaled) instead of silently losing the other writer's update.
    """
    if not plan_state_enabled():
        return bp
    path = plan_state_paths().blueprint_json
    with _blueprint_write_lock(path):
        on_disk = read_json_file(path)
        disk_revision = int(on_disk.get("revision", 0) or 0)
        if on_disk and disk_revision != bp.revision:
            append_journal_event(
                {
                    "event": "plan-state-revision-conflict",
                    "artifact": "blueprint.json",
                    "disk_revision": disk_revision,
                    "write_revision": bp.revision,
                }
            )
            raise PlanStateRevisionConflict(
                f"blueprint.json is at revision {disk_revision}, write was based on {bp.revision}; "
                "reload and reapply (single-writer invariant violated)"
            )
        bumped = replace(bp, revision=bp.revision + 1, updated_at=_now_iso())
        atomic_json_write(path, bumped.to_mapping(), sort_keys=True)
    return bumped


def load_summary() -> dict[str, Any]:
    if not plan_state_enabled():
        return {}
    return read_json_file(plan_state_paths().summary_json)


def save_summary(payload: Mapping[str, Any]) -> None:
    if not plan_state_enabled():
        return
    merged = dict(payload)
    merged["version"] = 1
    merged["updated_at"] = _now_iso()
    atomic_json_write(plan_state_paths().summary_json, merged, sort_keys=True)


def append_journal_event(event: Mapping[str, Any]) -> None:
    """Append one event to the lab notebook (flock-serialized, append-only)."""
    if not plan_state_enabled():
        return
    record = {"ts": _now_iso(), **dict(event)}
    _locked_append(
        plan_state_paths().journal_jsonl,
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
    )


# ---------------------------------------------------------------------------
# Graph mutations (journaled)
# ---------------------------------------------------------------------------


def set_node_status(
    bp: Blueprint, node_id: str, status: str, *, via_gate: bool = False, why: str = ""
) -> Blueprint:
    """Set a node's status under the kernel-truth rules.

    ``proved`` requires ``via_gate=True`` (the deterministic gate-accept sync
    is the only prover of proved-ness); a ``proved`` node is immutable to
    ordinary actors; ``false`` is reserved for negation promotion (Phase 3).
    """
    if status not in NODE_STATUSES:
        raise ValueError(f"unknown node status {status!r}")
    node = bp.node_by_id(node_id)
    if node is None:
        return bp
    if node.status == status:
        return bp
    if status == "proved" and not via_gate:
        raise ValueError("proved is writable only by the gate-accept sync path")
    if status == "false":
        raise ValueError("false requires negation promotion (invalidate_false_subtree)")
    if node.status == "proved":
        # via_gate only proves; downgrades belong exclusively to reconcile().
        raise ValueError("proved nodes are immutable outside the kernel-truth paths")
    updated = bp.replace_node(replace(node, status=status))
    append_journal_event(
        {
            "event": "node-status",
            "node_id": node_id,
            "name": node.name,
            "from": node.status,
            "to": status,
            "via_gate": via_gate,
            "why": why,
        }
    )
    return updated


def upsert_node_for_assignment(
    bp: Blueprint, *, target_symbol: str, active_file: str, statement: str
) -> tuple[Blueprint, GraphNode]:
    """Get-or-create the graph node for a queue assignment; mark it proving."""
    node_id = node_id_for(target_symbol, active_file)
    existing = bp.node_by_id(node_id)
    owner = str(os.getenv("LEANFLOW_NATIVE_RUNNER_OWNER", "") or "")
    if existing is None:
        node = GraphNode(
            id=node_id,
            name=target_symbol,
            file=active_file,
            statement=statement,
            status="proving",
            owner=owner,
            generated_by="queue-sync",
        )
        append_journal_event(
            {
                "event": "node-created",
                "node_id": node_id,
                "name": target_symbol,
                "file": active_file,
            }
        )
        return bp.replace_node(node), node
    updated = replace(
        existing,
        statement=statement or existing.statement,
        status="proving" if existing.status not in {"proved", "false"} else existing.status,
        owner=owner or existing.owner,
    )
    if updated != existing:
        append_journal_event(
            {
                "event": "node-status",
                "node_id": node_id,
                "name": existing.name,
                "from": existing.status,
                "to": updated.status,
                "via_gate": False,
                "why": "queue assignment",
            }
        )
    return bp.replace_node(updated), updated


def record_decision_packet(packet: Mapping[str, Any]) -> None:
    """Persist a budget-breakpoint decision packet (the N1 artifact chain).

    Idempotent by ``packet_id`` — a retry after a partial failure (crash
    between the summary write and the graph cross-link) repairs the missing
    pieces instead of duplicating the packet.
    """
    if not plan_state_enabled():
        return
    payload = dict(packet)
    packet_id = str(payload.get("packet_id", "") or "")
    summary = load_summary()
    packets = [
        dict(entry)
        for entry in (summary.get("decision_packets") or [])
        if isinstance(entry, Mapping)
    ]
    existing_index = next(
        (
            index
            for index, entry in enumerate(packets)
            if packet_id and str(entry.get("packet_id", "")) == packet_id
        ),
        None,
    )
    if existing_index is None:
        packets.append(payload)
    else:
        packets[existing_index] = payload
    summary["decision_packets"] = packets
    save_summary(summary)
    node_id = str(payload.get("node_id", "") or "")
    if node_id and packet_id:
        bp = load_blueprint()
        node = bp.node_by_id(node_id)
        if node is not None and packet_id not in node.decision_packets:
            bp = bp.replace_node(
                replace(node, decision_packets=(*node.decision_packets, packet_id))
            )
            save_blueprint(bp)
    if existing_index is None:
        append_journal_event(
            {
                "event": "decision-packet",
                "packet_id": packet_id,
                "scope": payload.get("scope", ""),
                "target_symbol": payload.get("target_symbol", ""),
            }
        )


# ---------------------------------------------------------------------------
# Reconciliation (P1.2, pure part)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclTruth:
    present: bool
    has_sorry: bool
    has_error_diag: bool = False


def reconcile(
    bp: Blueprint, truth: Mapping[tuple[str, str], DeclTruth]
) -> tuple[Blueprint, list[dict[str, Any]]]:
    """Anti-drift pass against on-disk declaration truth.

    Downgrades ``proved`` back to ``stated`` when the declaration reappears
    with a sorry/errors or vanishes; promotes ``conjectured`` to ``stated``
    when a named stub now exists on disk; NEVER promotes to ``proved``
    (kernel-gate-only). Returns the new graph plus change events; only files
    present in ``truth`` are judged (absent files were not scanned).
    """
    events: list[dict[str, Any]] = []
    scanned_files = {file for file, _symbol in truth}
    updated = bp
    for node in bp.nodes:
        if not node.file or not node.name or node.file not in scanned_files:
            continue
        decl = truth.get((node.file, node.name))
        present = bool(decl and decl.present)
        dirty = bool(decl and (decl.has_sorry or decl.has_error_diag))
        new_status = node.status
        if node.status == "proved" and (not present or dirty):
            new_status = "stated" if present else "conjectured"
        elif node.status == "conjectured" and present:
            new_status = "stated"
        if new_status != node.status:
            updated = updated.replace_node(replace(node, status=new_status))
            events.append(
                {
                    "event": "plan-graph-reconcile",
                    "node_id": node.id,
                    "name": node.name,
                    "file": node.file,
                    "from": node.status,
                    "to": new_status,
                }
            )
    return updated, events


# ---------------------------------------------------------------------------
# plan.md render + final report
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def status_counters(bp: Blueprint) -> dict[str, int]:
    """Public counters view for summary.json (only non-zero statuses)."""
    return _status_counts(bp)


def _status_counts(bp: Blueprint) -> dict[str, int]:
    counts = {status: 0 for status in NODE_STATUSES}
    for node in bp.nodes:
        counts[node.status] = counts.get(node.status, 0) + 1
    return {status: count for status, count in counts.items() if count}


def render_plan_md(bp: Blueprint, summary: Mapping[str, Any]) -> str:
    """One-way render of the machine state (JSON is authority)."""
    counts = _status_counts(bp)
    lines = [
        "# Proving Plan",
        "",
        PLAN_MD_GENERATED_MARKER,
        "",
        "## Goal",
        "",
        bp.goal or str(summary.get("goal", "") or "") or "[not set]",
        "",
        "## Current state",
        "",
        (
            " · ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
            or "empty graph"
        ),
        "",
        "## Frontier",
        "",
    ]
    frontier = bp.frontier()
    if frontier:
        lines.extend(f"- `{node.name}` ({node.file})" for node in frontier[:20])
    else:
        lines.append("- [empty]")
    lines.extend(["", "## Grounding", ""])
    findings = list(summary.get("grounding_findings") or [])
    if findings:
        lines.extend(f"- {finding}" for finding in findings[:20])
    else:
        lines.append("- [none yet]")
    lines.extend(["", "## Decision log", ""])
    packets = list(summary.get("decision_packets") or [])
    if packets:
        for packet in packets[-10:]:
            lines.append(
                f"- {packet.get('packet_id', '?')}: {packet.get('scope', '?')} "
                f"`{packet.get('target_symbol', '?')}` -> "
                f"{packet.get('decision') or 'undecided'}"
            )
    else:
        lines.append("- [none]")
    lines.extend(["", "## Dead ends & proven false", ""])
    dead = [node for node in bp.nodes if node.status in {"false", "parked"}]
    if dead:
        lines.extend(f"- `{node.name}` [{node.status}] ({node.file})" for node in dead)
    else:
        lines.append("- [none]")
    lines.extend(["", "## Final report", ""])
    final_report = dict(summary.get("final_report") or {})
    if final_report:
        lines.append(f"- status: {final_report.get('status', 'in-progress')}")
        if final_report.get("summary"):
            lines.append(f"- summary: {final_report['summary']}")
    else:
        lines.append("- status: in-progress")
    lines.append("")
    return "\n".join(lines)


def save_plan_md(bp: Blueprint, summary: Mapping[str, Any]) -> None:
    """Regenerate plan.md, preserving the free-form '## Notes' tail verbatim."""
    if not plan_state_enabled():
        return
    path = plan_state_paths().plan_md
    notes_tail = f"{_NOTES_HEADING}\n\n[free-form notes below survive regeneration]\n"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        marker_index = existing.find(_NOTES_HEADING)
        if marker_index >= 0:
            notes_tail = existing[marker_index:]
    _atomic_write_text(path, render_plan_md(bp, summary) + "\n" + notes_tail)


def write_final_report(status: str, *, detail: Mapping[str, Any] | None = None) -> None:
    """N1 concrete-result guarantee: persist the terminal artifact.

    ``status`` is one of proved | disproved | documented — ``documented`` is
    the worst allowed terminal state and must carry the packets/graph/notes
    that constitute the rigorous account. Called from every stop path when
    plan-state is on, so silent give-up is structurally impossible.
    """
    if not plan_state_enabled():
        return
    if status not in FINAL_REPORT_STATUSES:
        raise ValueError(f"unknown final-report status {status!r}")
    summary = load_summary()
    # The validated status always wins — detail must not smuggle another one in.
    report = {**dict(detail or {}), "status": status}
    summary["final_report"] = report
    save_summary(summary)
    save_plan_md(load_blueprint(), summary)
    append_journal_event({"event": "final-report", "status": status})


# ---------------------------------------------------------------------------
# Prompt-surface blocks (P1.3)
# ---------------------------------------------------------------------------


def artifact_paths_block() -> str:
    """Byte-stable artifact-path lines (safe for the RCP prefix-cache prefix)."""
    if not plan_state_enabled():
        return ""
    paths = plan_state_paths()
    return "\n".join(
        [
            "Living plan artifacts (read before planning; the dependency graph "
            "blueprint.json is machine authority):",
            f"- plan: {paths.plan_md}",
            f"- dependency graph: {paths.blueprint_json}",
            f"- summary: {paths.summary_json}",
            f"- journal: {paths.journal_jsonl}",
        ]
    )


def frontier_digest_block() -> str:
    """<=10-line volatile digest (goes after the prompt's cycle marker)."""
    if not plan_state_enabled():
        return ""
    bp = load_blueprint()
    if not bp.nodes:
        return ""
    counts = _status_counts(bp)
    lines = [
        "Dependency graph digest:",
        "- " + " · ".join(f"{status}: {count}" for status, count in sorted(counts.items())),
    ]
    frontier = bp.frontier()
    for node in frontier[:8]:
        lines.append(f"- frontier: `{node.name}` ({node.file})")
    return "\n".join(lines[:10])


def artifact_context_block() -> str:
    """The single injection string for non-prefix-cached prompt surfaces."""
    if not plan_state_enabled():
        return ""
    paths_block = artifact_paths_block()
    digest = frontier_digest_block()
    return f"{paths_block}\n\n{digest}".strip() if digest else paths_block
