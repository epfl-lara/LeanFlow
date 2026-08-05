"""Persist proof plans, dependency graphs, summaries, and journal events.

The machine authority is ``blueprint.json`` (called the dependency graph to
distinguish it from formalization ``Blueprint.md`` files). ``summary.json`` is
the machine summary, ``plan.md`` is the generated human view with a preserved
free-form Notes tail, and ``journal.jsonl`` is the append-only source of truth
from which snapshots can be rebuilt.

The module returns empty state unless ``LEANFLOW_PLAN_STATE`` is enabled.
Writes are crash-atomic, and revision checks turn an accidental second writer
into a conflict instead of a lost update.

Kernel-truth rules allow ``proved`` only through gate acceptance and ``false``
only through promoted negation evidence. Reconciliation may downgrade a
regressed declaration but never promote one to ``proved``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows import planner_candidate_admission, planner_graph_identity
from leanflow_cli.workflows.queue_models import DEFAULT_FAILED_ATTEMPT_HISTORY, TheoremKey
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state import _locked_append
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root
from tools.utilities.workflow_artifact_guard import generated_plan_view

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

# Keys owned by other cooperating summary writers — never written here.
_FOREIGN_SUMMARY_KEYS = frozenset(
    {
        "campaign",
        "campaign_metrics",
        "advisor_route_facts",
        "decomposition_provenance",
        "dispatch_ledger",
        "false_decomposition_cleanup_quarantine",
        "false_decomposition_cleanup_transactions",
        "false_decomposition_cleanups",
        "manager_nudges",
        "negation_probes",
        "negation_promotion_quarantine",
        "negation_promotion_transactions",
        "negation_promotions",
        "planner_arithmetic_reconciliation",
        "queue_manager_state",
        "research_delivery_state",
        "research_delivery_backpressure",
        "research_finding_migration",
        "research_findings",
        "pending_research_helper_candidate",
        "resolved_research_helper_candidates",
        "research_portfolio_failure_backoff",
        "resume_gate_axiom_policy_rejections",
        "source_negation_candidate_scans",
        "verification_candidate_replays",
    }
)

PLAN_MD_GENERATED_MARKER = "<!-- generated: do not edit above the Notes section -->"
_NOTES_HEADING = "## Notes"
_NOTES_HEADING_RE = re.compile(r"(?m)^## Notes[ \t]*$")
PLAN_PROMPT_VIEW_MAX_CHARS = 8_000
_RECENT_ROUTE_LIMIT = 8
_JOURNAL_TAIL_MAX_BYTES = 1024 * 1024


class PlanStateRevisionConflict(RuntimeError):
    """The dependency graph on disk moved past the revision this write is based on."""


try:  # POSIX advisory locking (same degradation policy as workflow_state._locked_append)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_WRITE_LOCK = threading.RLock()
_BLUEPRINT_LOCK_LOCAL = threading.local()


def _blueprint_lock_entries() -> dict[str, tuple[int, Any]]:
    """Return re-entrant blueprint-lock entries for the current thread."""
    entries = getattr(_BLUEPRINT_LOCK_LOCAL, "entries", None)
    if not isinstance(entries, dict):
        entries = {}
        _BLUEPRINT_LOCK_LOCAL.entries = entries
    return entries


@contextlib.contextmanager
def _blueprint_write_lock(path: Path) -> Iterator[None]:
    """Serialize the read-check-write revision transaction across processes.

    Closes the TOCTOU window in save_blueprint: without it two writers could
    both read revision N and both write N+1, losing one update silently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    key = str(lock_path.absolute())
    with _WRITE_LOCK:
        entries = _blueprint_lock_entries()
        existing = entries.get(key)
        if existing is not None:
            depth, handle = existing
            entries[key] = (depth + 1, handle)
            try:
                yield
            finally:
                current_depth, current_handle = entries[key]
                if current_depth <= 1:
                    entries.pop(key, None)
                else:
                    entries[key] = (current_depth - 1, current_handle)
            return

        with lock_path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    logger.debug(
                        "flock unavailable for %s; write not cross-process locked",
                        lock_path,
                        exc_info=True,
                    )
            entries[key] = (1, handle)
            try:
                yield
            finally:
                entries.pop(key, None)
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    source_sha256: str = ""
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
            source_sha256=str(raw.get("source_sha256", "") or ""),
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
            "source_sha256": self.source_sha256,
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
        """Ready nodes (stated or audited) whose depends_on targets are all proved.

        ``audited`` is stated-plus-fidelity-pass — auditing a node must never
        remove it from the frontier the planner and resume views work from.
        """
        by_id = {node.id: node for node in self.nodes}
        out: list[GraphNode] = []
        for node in self.nodes:
            if node.status not in {"stated", "audited"}:
                continue
            deps = [
                by_id.get(edge.target)
                for edge in self.edges
                if edge.kind == "depends_on" and edge.source == node.id
            ]
            if all(dep is not None and dep.status == "proved" for dep in deps) or not deps:
                out.append(node)
        return tuple(out)

    def has_invalid_dependency(self, node_id: str) -> bool:
        """Return whether a transitive dependency is false or human-paused."""
        by_id = {node.id: node for node in self.nodes}
        dependencies: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.kind == "depends_on":
                dependencies.setdefault(edge.source, []).append(edge.target)
        pending = list(dependencies.get(node_id, ()))
        seen: set[str] = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in seen:
                continue
            seen.add(dependency_id)
            dependency = by_id.get(dependency_id)
            if dependency is not None and dependency.status in {"false", "parked"}:
                return True
            pending.extend(dependencies.get(dependency_id, ()))
        return False

    def invalidate_false_subtree(self, node_id: str) -> Blueprint:
        """Mark ``node_id`` false and poison its invalid decomposition ancestors.

        A kernel-proved negation of a sub-lemma means the decomposition that
        stated it was wrong: every ancestor along split_of edges drops back to
        ``conjectured``. A proved ancestor normally remains an immutable kernel
        fact, but an explicit ``depends_on`` edge to the newly-false child
        proves that its recorded acceptance came through the invalid route;
        reopen it so the corrected axiom-aware gate can verify another proof.
        """
        bp = self
        node = bp.node_by_id(node_id)
        if node is None:
            return bp
        bp = bp.replace_node(replace(node, status="false"))
        parents_of = {edge.source: edge.target for edge in bp.edges if edge.kind == "split_of"}
        seen: set[str] = set()
        invalid_child = node_id
        cursor = parents_of.get(node_id)
        while cursor and cursor not in seen:
            seen.add(cursor)
            ancestor = bp.node_by_id(cursor)
            explicitly_depends_on_invalid_child = any(
                edge.kind == "depends_on" and edge.source == cursor and edge.target == invalid_child
                for edge in bp.edges
            )
            if ancestor is not None and ancestor.status != "false":
                if ancestor.status != "proved" or explicitly_depends_on_invalid_child:
                    bp = bp.replace_node(replace(ancestor, status="conjectured"))
            invalid_child = cursor
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
    _reconcile_persisted_planner_arithmetic()
    return Blueprint.from_mapping(read_json_file(plan_state_paths().blueprint_json))


@contextlib.contextmanager
def blueprint_commit_guard() -> Iterator[None]:
    """Hold the cooperative graph-writer lease across a cross-artifact commit.

    Callers may read, validate, and save the blueprint while this guard is
    held. Nested use by the same thread retains one underlying file lease, so
    a cross-artifact terminal transaction can call existing reconciliation
    code without reopening a graph race or self-deadlocking.
    """
    if not plan_state_enabled():
        yield
        return
    with _blueprint_write_lock(plan_state_paths().blueprint_json):
        yield


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
    _reconcile_persisted_planner_arithmetic()
    return read_json_file(plan_state_paths().summary_json)


def _reconcile_persisted_planner_arithmetic() -> None:
    """Run the lazy versioned planner migration before persisted state reuse."""
    from leanflow_cli.workflows import planner_arithmetic_reconciliation

    planner_arithmetic_reconciliation.reconcile_persisted_planner_arithmetic()


def save_summary(payload: Mapping[str, Any]) -> None:
    """Merge ``payload`` into summary.json under the shared write lock.

    Foreign keys (nudges, dispatch, and research delivery/archive state) are
    stripped from the payload entirely: their owners are the only writers, so
    even a stale ``load_summary()`` snapshot in the caller can never regress them.
    """
    if not plan_state_enabled():
        return

    def mutate(summary: dict[str, Any]) -> None:
        merged = {
            key: value for key, value in dict(payload).items() if key not in _FOREIGN_SUMMARY_KEYS
        }
        summary.update(merged)
        summary["version"] = 1
        summary["updated_at"] = _now_iso()

    update_json_file(plan_state_paths().summary_json, mutate)


def save_queue_manager_state(payload: Mapping[str, Any]) -> None:
    """Persist the deterministic manager's durable campaign state."""
    if not plan_state_enabled():
        return

    def mutate(summary: dict[str, Any]) -> None:
        summary["queue_manager_state"] = dict(payload)
        summary["version"] = 1
        summary["updated_at"] = _now_iso()

    update_json_file(plan_state_paths().summary_json, mutate)


def _failed_attempts_from_journal() -> list[dict[str, Any]]:
    """Rebuild bounded failed-attempt history for pre-snapshot campaigns."""
    path = plan_state_paths().journal_jsonl
    if not path.is_file():
        return []
    counts: dict[str, int] = {}
    attempts: dict[str, list[dict[str, Any]]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, Mapping) or event.get("event") != "proof-attempt-rejected":
            continue
        target_symbol = str(event.get("name", "") or "").strip()
        active_file = str(event.get("file", "") or "").strip()
        reason = str(event.get("reason", "") or "").strip()
        key = TheoremKey.make(target_symbol, active_file)
        if not key.is_valid() or not reason:
            continue
        storage_key = key.storage_key()
        counts[storage_key] = counts.get(storage_key, 0) + 1
        bucket = attempts.setdefault(storage_key, [])
        bucket.append(
            {
                "target_symbol": target_symbol,
                "active_file": active_file,
                "attempt": counts[storage_key],
                "cycle": int(event.get("cycle", 0) or 0),
                "proof_shape": str(event.get("proof_shape", "") or ""),
                "reason": reason,
            }
        )
        del bucket[:-DEFAULT_FAILED_ATTEMPT_HISTORY]
    return [attempt for bucket in attempts.values() for attempt in bucket]


def load_queue_manager_state() -> dict[str, Any]:
    """Load durable manager state, rebuilding old campaigns from the journal."""
    if not plan_state_enabled():
        return {}
    summary = load_summary()
    if "queue_manager_state" in summary:
        payload = summary.get("queue_manager_state")
        return dict(payload) if isinstance(payload, Mapping) else {}
    attempts = _failed_attempts_from_journal()
    return {"failed_attempts": attempts} if attempts else {}


def append_journal_event(event: Mapping[str, Any]) -> None:
    """Append one event to the lab notebook (flock-serialized, append-only)."""
    if not plan_state_enabled():
        return
    record = {"ts": _now_iso(), **dict(event)}
    _locked_append(
        plan_state_paths().journal_jsonl,
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
    )


def recent_orchestrator_routes(limit: int = _RECENT_ROUTE_LIMIT) -> tuple[dict[str, Any], ...]:
    """Return recent route decisions from a bounded journal tail.

    The journal is an append-only lab record and can grow for days during a
    research campaign. Read at most one MiB from its tail so rendering a small
    plan or resume prompt never hydrates the campaign history into RAM.
    """
    if not plan_state_enabled() or limit <= 0:
        return ()
    path = plan_state_paths().journal_jsonl
    try:
        size = path.stat().st_size
        start = max(0, size - _JOURNAL_TAIL_MAX_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(_JOURNAL_TAIL_MAX_BYTES)
    except OSError:
        return ()
    if start:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return ()
    routes: list[dict[str, Any]] = []
    for raw_line in reversed(payload.splitlines()):
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, Mapping) or event.get("event") != "orchestrator-route":
            continue
        routes.append(dict(event))
        if len(routes) >= limit:
            break
    routes.reverse()
    return tuple(routes)


def _recent_journal_events() -> tuple[dict[str, Any], ...]:
    """Return decoded events from the bounded journal tail in chronological order."""
    if not plan_state_enabled():
        return ()
    path = plan_state_paths().journal_jsonl
    try:
        size = path.stat().st_size
        start = max(0, size - _JOURNAL_TAIL_MAX_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(_JOURNAL_TAIL_MAX_BYTES)
    except OSError:
        return ()
    if start:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return ()
    events: list[dict[str, Any]] = []
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(event, Mapping):
            events.append(dict(event))
    return tuple(events)


def _assignment_graph_names(
    bp: Blueprint,
    assignment: Mapping[str, Any],
) -> set[str]:
    """Return names in the current assignment's transitive dependency family."""
    normalized = _normalized_queue_assignment(assignment)
    if not normalized:
        return {node.name for node in bp.nodes}
    target_id = node_id_for(normalized["target_symbol"], normalized["active_file"])
    if bp.node_by_id(target_id) is None:
        matching_target = next(
            (
                node
                for node in bp.nodes
                if node.name == normalized["target_symbol"]
                and (not normalized["active_file"] or node.file == normalized["active_file"])
            ),
            None,
        )
        if matching_target is not None:
            target_id = matching_target.id
    related_ids = {target_id}
    changed = True
    while changed:
        changed = False
        for edge in bp.edges:
            dependency_id = ""
            if edge.kind == "depends_on" and edge.source in related_ids:
                dependency_id = edge.target
            elif edge.kind == "split_of" and edge.target in related_ids:
                dependency_id = edge.source
            if dependency_id and dependency_id not in related_ids:
                related_ids.add(dependency_id)
                changed = True
    names = {
        node.name for node in bp.nodes if node.id in related_ids and str(node.name or "").strip()
    }
    names.add(normalized["target_symbol"])
    return names


def recent_exploration_outcomes(
    bp: Blueprint,
    assignment: Mapping[str, Any],
    *,
    limit: int = 8,
) -> tuple[dict[str, str], ...]:
    """Summarize typed outcomes for the current theorem from the journal tail.

    Keep raw history in ``journal.jsonl`` while exposing a small operational
    account of proved, repaired, rejected, invalidated, and superseded routes.
    This prevents useful dead-branch knowledge from disappearing into logs or
    leaking into the next theorem's prompt.
    """
    if limit <= 0:
        return ()
    allowed_names = _assignment_graph_names(bp, assignment)
    outcomes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in reversed(_recent_journal_events()):
        event_name = str(event.get("event", "") or "").strip()
        subject = str(
            event.get("name") or event.get("target_symbol") or event.get("parent_target") or ""
        ).strip()
        if allowed_names and subject and subject not in allowed_names:
            continue
        outcome_type = ""
        detail = ""
        if event_name == "proof-attempt-rejected":
            reason = str(event.get("reason", "") or "").strip()
            lowered = reason.lower()
            outcome_type = (
                "blocked_by_source_order"
                if "source order" in lowered or "declared before" in lowered
                else "rejected_by_kernel"
            )
            proof_shape = str(event.get("proof_shape", "") or "").strip()
            detail = " — ".join(part for part in (proof_shape, reason) if part)
        elif event_name == "node-status":
            from_status = str(event.get("from", "") or "").strip()
            to_status = str(event.get("to", "") or "").strip()
            why = str(event.get("why", "") or "").strip()
            if to_status == "proved":
                repaired = from_status in {"false", "parked"} or any(
                    token in why.lower() for token in ("repair", "reconcile", "retry")
                )
                outcome_type = "proved_after_repair" if repaired else "proved"
            elif to_status == "false":
                outcome_type = "rejected_by_kernel"
            elif to_status == "parked":
                outcome_type = "superseded"
            elif from_status == "proved":
                outcome_type = "invalidated"
            detail = why or f"{from_status or '?'} -> {to_status or '?'}"
        elif event_name.endswith("-rejected"):
            outcome_type = "rejected_by_admission"
            detail = str(event.get("reason") or event.get("detail") or event_name).strip()
        elif event_name == "planner-unsynthesized-findings":
            outcome_type = "research_preserved"
            reason = str(event.get("reason", "") or "").strip()
            lanes = ", ".join(str(lane) for lane in (event.get("lanes") or []) if str(lane))
            detail = " — ".join(
                part for part in (reason, f"lanes: {lanes}" if lanes else "") if part
            )
        elif "superseded" in event_name:
            outcome_type = "superseded"
            detail = str(event.get("reason") or event.get("detail") or event_name).strip()
        if not outcome_type or not subject:
            continue
        key = (outcome_type, subject, detail)
        if key in seen:
            continue
        seen.add(key)
        outcomes.append(
            {
                "type": outcome_type,
                "subject": subject,
                "detail": detail,
                "ts": str(event.get("ts", "") or ""),
                "event": event_name,
            }
        )
        if len(outcomes) >= limit:
            break
    outcomes.reverse()
    return tuple(outcomes)


# ---------------------------------------------------------------------------
# Graph mutations (journaled)
# ---------------------------------------------------------------------------


def set_node_status(
    bp: Blueprint,
    node_id: str,
    status: str,
    *,
    via_gate: bool = False,
    why: str = "",
    journal: bool = True,
) -> Blueprint:
    """Set a node's status under the kernel-truth rules.

    ``proved`` requires ``via_gate=True`` (the deterministic gate-accept sync
    is the only prover of proved-ness); a ``proved`` node is immutable to
    ordinary actors; ``false`` is reserved for verified negation promotion.
    ``journal=False`` defers the notebook write to the caller (the
    conflicted-save-then-retry discipline: journal only what was persisted,
    via :func:`journal_node_status`).
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
    # ``owner`` is an in-progress lease, not historical provenance. A
    # gate-backed terminal node must never retain the runner that previously
    # owned its ``proving`` assignment.
    updated = bp.replace_node(
        replace(node, status=status, owner="" if status == "proved" else node.owner)
    )
    if journal:
        journal_node_status(
            node_id=node_id,
            name=node.name,
            from_status=node.status,
            to_status=status,
            via_gate=via_gate,
            why=why,
        )
    return updated


def revoke_gate_acceptance(bp: Blueprint, node_id: str, *, why: str) -> Blueprint:
    """Revoke a proved status after its deterministic acceptance evidence becomes invalid.

    This is the gate-side counterpart to ``set_node_status(..., via_gate=True)``.
    Preserve a prior statement-fidelity pass when one is recorded; otherwise
    return the declaration to ordinary stated work. The revocation is always
    journaled so a resumed campaign can explain why kernel-clean surface truth
    was no longer sufficient.
    """
    node = bp.node_by_id(node_id)
    if node is None or node.status != "proved":
        return bp
    fidelity_audited = "fidelity: audited" in {
        part.strip() for part in str(node.notes or "").split(";")
    }
    status = "audited" if fidelity_audited else "stated"
    updated = bp.replace_node(replace(node, status=status))
    journal_node_status(
        node_id=node_id,
        name=node.name,
        from_status="proved",
        to_status=status,
        via_gate=True,
        why=why,
    )
    return updated


def journal_node_status(
    *, node_id: str, name: str, from_status: str, to_status: str, via_gate: bool, why: str
) -> None:
    """Journal one node-status change (deferred-journal companion)."""
    append_journal_event(
        {
            "event": "node-status",
            "node_id": node_id,
            "name": name,
            "from": from_status,
            "to": to_status,
            "via_gate": via_gate,
            "why": why,
        }
    )


def upsert_node_for_assignment(
    bp: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
    statement: str,
    source_sha256: str = "",
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
            source_sha256=source_sha256,
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
        source_sha256=source_sha256 or existing.source_sha256,
        status="proving" if existing.status not in {"proved", "false"} else existing.status,
        owner=owner or existing.owner,
    )
    if updated.status != existing.status:
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


def update_node_effort(bp: Blueprint, node_id: str, *, attempts: int, api_steps: int) -> Blueprint:
    """Raise one graph node's observed foreground attempt and API-step totals."""
    node = bp.node_by_id(node_id)
    if node is None:
        return bp
    updated = replace(
        node,
        attempts=max(node.attempts, max(0, int(attempts))),
        api_steps=max(node.api_steps, max(0, int(api_steps))),
    )
    return bp if updated == node else bp.replace_node(updated)


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
# Planner delta merge.
# ---------------------------------------------------------------------------

#: Ceiling on nodes accepted from one delta — bounds runaway synthesizer output.
DELTA_MAX_NODES = 24


def _delta_ref(entry: Any, default_file: str) -> tuple[str, str]:
    """Normalize a node reference: 'name' or {'name','file'} -> (name, file)."""
    if isinstance(entry, Mapping):
        return (
            str(entry.get("name", "") or "").strip(),
            str(entry.get("file", "") or "").strip() or default_file,
        )
    return str(entry or "").strip(), default_file


def apply_delta(
    bp: Blueprint, delta: Mapping[str, Any], *, generated_by: str = "planner", journal: bool = True
) -> tuple[Blueprint, list[dict[str, Any]]]:
    """Merge a planner/synthesizer graph delta; returns (blueprint, changes).

    Pure with respect to persistence (caller saves via ``save_blueprint``,
    keeping the single-writer revision machinery intact). ``journal=False``
    defers the journal writes to the caller — pass it when the save may hit
    a revision conflict and be re-applied, then journal the FINAL change
    set once after the save succeeds (replay consistency: the notebook must
    describe the graph that was actually persisted).

    Kernel-truth rules: a delta node's status is DERIVED from its payload —
    statement present => ``stated``, otherwise ``conjectured`` — and any
    status the delta claims is ignored outright; an existing node NEVER
    changes status through this path and keeps a non-empty statement — the
    planner may only fill blanks. Reused textual identities must carry the
    same proof-insensitive declaration signature before they can inherit an
    existing node or receive edges. Edges are deduped, self-edges and
    references to nodes outside the merged graph are dropped (reported in
    changes).
    """
    changes: list[dict[str, Any]] = []
    raw_nodes = [entry for entry in (delta.get("nodes") or []) if isinstance(entry, Mapping)]
    if len(raw_nodes) > DELTA_MAX_NODES:
        changes.append(
            {"event": "plan-delta-truncated", "dropped_nodes": len(raw_nodes) - DELTA_MAX_NODES}
        )
        raw_nodes = raw_nodes[:DELTA_MAX_NODES]

    goal = str(delta.get("goal", "") or "").strip()
    if goal and not bp.goal:
        bp = replace(bp, goal=goal)
        changes.append({"event": "plan-delta-goal", "goal": goal[:200]})

    # Resolve declaration identity before mutating the graph. A private Lean
    # declaration may reuse a textual name with a different signature, while
    # node_id_for intentionally remains stable on (name, file). Such a delta
    # must fail closed instead of borrowing an existing kernel-proved status.
    signatures_by_node_id: dict[str, str] = {
        node.id: signature
        for node in bp.nodes
        if planner_graph_identity.is_full_declaration_signature(node.statement)
        and (signature := planner_graph_identity.declaration_signature(node.statement))
    }
    existing_nodes_by_id = {node.id: node for node in bp.nodes}
    unauthenticated_kernel_node_ids = {
        node.id
        for node in bp.nodes
        if node.status in {"proved", "false"} and node.id not in signatures_by_node_id
    }
    signature_conflicts: dict[str, tuple[str, str]] = {}
    legacy_core_migrations: dict[str, str] = {}
    for entry in raw_nodes:
        name = str(entry.get("name", "") or "").strip()
        file = str(entry.get("file", "") or "").strip()
        statement = str(entry.get("statement", "") or "").strip()
        if not name or not file or not statement:
            continue
        node_id = node_id_for(name, file)
        proposed_signature = planner_graph_identity.declaration_signature(statement)
        if not proposed_signature:
            continue
        known_signature = signatures_by_node_id.get(node_id, "")
        if known_signature and known_signature != proposed_signature:
            signature_conflicts.setdefault(
                node_id,
                (known_signature, proposed_signature),
            )
            continue
        existing_node = existing_nodes_by_id.get(node_id)
        existing_statement = (
            str(existing_node.statement or "").strip() if existing_node is not None else ""
        )
        if not known_signature and existing_node is not None and existing_statement:
            if planner_graph_identity.legacy_core_matches_declaration(
                existing_statement,
                statement,
                generated_by=existing_node.generated_by,
            ):
                # Store a proof-insensitive full head: the planner's `by sorry`
                # body cannot replace the source proof behind a proved node.
                legacy_core_migrations[node_id] = proposed_signature
                signatures_by_node_id[node_id] = proposed_signature
                unauthenticated_kernel_node_ids.discard(node_id)
                continue
            signature_conflicts.setdefault(
                node_id,
                (
                    planner_graph_identity.declaration_signature(existing_statement),
                    proposed_signature,
                ),
            )
            continue
        if (
            not known_signature
            and existing_node is not None
            and existing_node.status in {"proved", "false"}
        ):
            # Legacy snapshots can carry kernel truth without the statement
            # bytes needed to authenticate it. Never attach a new formal
            # declaration to that truth on textual name alone.
            signature_conflicts.setdefault(node_id, ("", proposed_signature))
            continue
        signatures_by_node_id[node_id] = proposed_signature

    pending_edges: list[tuple[str, str, str]] = []  # (source_id, target_id, kind)
    for entry in raw_nodes:
        name = str(entry.get("name", "") or "").strip()
        file = str(entry.get("file", "") or "").strip()
        if not name or not file:
            changes.append({"event": "plan-delta-node-skipped", "reason": "missing name/file"})
            continue
        statement = str(entry.get("statement", "") or "").strip()
        node_id = node_id_for(name, file)
        existing = bp.node_by_id(node_id)
        conflict = signature_conflicts.get(node_id)
        if conflict is not None:
            existing_signature, proposed_signature = conflict
            changes.append(
                {
                    "event": "plan-delta-node-signature-conflict",
                    "node_id": node_id,
                    "name": name,
                    "file": file,
                    "existing_signature_sha256": planner_graph_identity.signature_sha256(
                        existing_signature
                    ),
                    "proposed_signature_sha256": planner_graph_identity.signature_sha256(
                        proposed_signature
                    ),
                }
            )
            # Preserve the historical notes-only fill behavior without
            # accepting the conflicting declaration or any of its edges.
            if existing is not None:
                updated = replace(
                    existing,
                    notes=existing.notes or str(entry.get("notes", "") or "").strip(),
                )
                if updated != existing:
                    bp = bp.replace_node(updated)
                    changes.append({"event": "plan-delta-node-filled", "node_id": node_id})
            continue
        if existing is None:
            # Status is DERIVED, never trusted: 'stated' is a claim that a
            # formal statement exists (it makes the node frontier-eligible),
            # so only an actual statement earns it.
            status = "stated" if statement else "conjectured"
            node = GraphNode(
                id=node_id,
                kind=str(entry.get("kind", "") or "").strip() or "lemma",
                name=name,
                file=file,
                statement=statement,
                status=status,
                notes=str(entry.get("notes", "") or "").strip(),
                generated_by=generated_by,
            )
            bp = bp.replace_node(node)
            changes.append(
                {"event": "node-created", "node_id": node_id, "name": name, "file": file}
            )
        else:
            # Fill blanks only; status and non-empty statements are immutable here.
            migrated_statement = legacy_core_migrations.pop(node_id, "")
            updated = replace(
                existing,
                statement=migrated_statement or existing.statement or statement,
                notes=existing.notes or str(entry.get("notes", "") or "").strip(),
            )
            if updated != existing:
                bp = bp.replace_node(updated)
                changes.append({"event": "plan-delta-node-filled", "node_id": node_id})
            if migrated_statement:
                changes.append(
                    {
                        "event": "plan-delta-node-signature-migrated",
                        "node_id": node_id,
                        "name": name,
                        "file": file,
                        "signature_sha256": planner_graph_identity.signature_sha256(
                            migrated_statement
                        ),
                    }
                )
        for dep in entry.get("depends_on") or []:
            dep_name, dep_file = _delta_ref(dep, file)
            if dep_name:
                pending_edges.append((node_id, node_id_for(dep_name, dep_file), "depends_on"))
        parent_name, parent_file = _delta_ref(entry.get("split_of"), file)
        if parent_name:
            pending_edges.append((node_id, node_id_for(parent_name, parent_file), "split_of"))

    for entry in delta.get("edges") or []:
        if not isinstance(entry, Mapping):
            continue
        src_name, src_file = _delta_ref(entry.get("source") or entry.get("from"), "")
        dst_name, dst_file = _delta_ref(entry.get("target") or entry.get("to"), "")
        kind = str(entry.get("kind", "") or "").strip()
        if src_name and src_file and dst_name and dst_file and kind in EDGE_KINDS:
            pending_edges.append(
                (node_id_for(src_name, src_file), node_id_for(dst_name, dst_file), kind)
            )
        else:
            changes.append({"event": "plan-delta-edge-skipped", "reason": "unresolvable"})

    known = {node.id for node in bp.nodes}
    have = {(edge.source, edge.target, edge.kind) for edge in bp.edges}
    added: list[GraphEdge] = []
    for source_id, target_id, kind in pending_edges:
        if source_id in signature_conflicts or target_id in signature_conflicts:
            changes.append(
                {
                    "event": "plan-delta-edge-skipped",
                    "reason": "declaration signature conflict",
                }
            )
            continue
        if (
            source_id in unauthenticated_kernel_node_ids
            or target_id in unauthenticated_kernel_node_ids
        ):
            changes.append(
                {
                    "event": "plan-delta-edge-skipped",
                    "reason": "unauthenticated kernel declaration",
                }
            )
            continue
        if source_id == target_id or (source_id, target_id, kind) in have:
            continue
        if source_id not in known or target_id not in known:
            changes.append({"event": "plan-delta-edge-skipped", "reason": "unknown node"})
            continue
        added.append(GraphEdge(source=source_id, target=target_id, kind=kind))
        have.add((source_id, target_id, kind))
    if added:
        bp = replace(bp, edges=(*bp.edges, *added))
        changes.append({"event": "plan-delta-edges", "added": len(added)})

    if journal:
        journal_delta_changes(changes, generated_by=generated_by)
    return bp, changes


def journal_delta_changes(changes: Sequence[Mapping[str, Any]], *, generated_by: str) -> None:
    """Journal an apply_delta change set (used after a deferred-journal save)."""
    for change in changes:
        append_journal_event({**dict(change), "generated_by": generated_by})


#: Bounds for the prose summary keys the planner merge owns.
_GROUNDING_CAP = 40
_STRATEGY_CAP = 20
_STRATEGY_SCOPE_KEY = "strategy_notes_scope"
_CHECKPOINT_ADVISORY_CAP = 20
_CHECKPOINT_ADVISORY_ITEM_CAP = 8


def _normalized_strategy_scope(value: Any) -> dict[str, str]:
    """Return one complete assignment identity for actionable strategy prose."""
    if not isinstance(value, Mapping):
        return {}
    target_symbol = str(value.get("target_symbol", "") or "").strip()
    active_file = str(value.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return {}
    return {"target_symbol": target_symbol, "active_file": active_file}


def _strategy_scope_matches(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Return whether two strategy scopes name the same declaration assignment."""
    left_scope = _normalized_strategy_scope(left)
    right_scope = _normalized_strategy_scope(right)
    if not left_scope or not right_scope:
        return False
    if left_scope["target_symbol"] != right_scope["target_symbol"]:
        return False
    left_file = left_scope["active_file"]
    right_file = right_scope["active_file"]
    if left_file == right_file:
        return True
    try:
        return Path(left_file).resolve(strict=False) == Path(right_file).resolve(strict=False)
    except OSError:
        return False


def merge_planner_findings(
    summary: Mapping[str, Any],
    *,
    grounding: Sequence[str] = (),
    strategy: Sequence[str] = (),
    target_symbol: str = "",
    active_file: str = "",
) -> dict[str, Any]:
    """Pure merge of synthesizer prose into the summary mapping.

    Appends deduplicated one-liners to ``grounding_findings`` /
    ``strategy_notes`` under hard caps (oldest kept — grounding is an
    append-only lab record, not a rolling window). Actionable strategy prose
    is reset and re-scoped when synthesis moves to another assignment; callers
    that omit scope retain the legacy merge behavior.
    """
    merged = dict(summary)
    incoming_scope = _normalized_strategy_scope(
        {"target_symbol": target_symbol, "active_file": active_file}
    )
    for key, incoming, cap in (("grounding_findings", grounding, _GROUNDING_CAP),):
        current = [str(item) for item in (merged.get(key) or [])]
        seen = set(current)
        for item in incoming:
            text = " ".join(str(item or "").split())
            if text and text not in seen:
                current.append(text)
                seen.add(text)
        merged[key] = current[:cap]
    current_strategy = [str(item) for item in (merged.get("strategy_notes") or [])]
    if strategy and incoming_scope:
        existing_scope = _normalized_strategy_scope(merged.get(_STRATEGY_SCOPE_KEY))
        if not _strategy_scope_matches(existing_scope, incoming_scope):
            current_strategy = []
        merged[_STRATEGY_SCOPE_KEY] = incoming_scope
    seen_strategy = set(current_strategy)
    for item in strategy:
        text = " ".join(str(item or "").split())
        if text and text not in seen_strategy:
            current_strategy.append(text)
            seen_strategy.add(text)
    merged["strategy_notes"] = current_strategy[:_STRATEGY_CAP]
    return merged


def record_checkpoint_advisory(
    *,
    checkpoint_id: str,
    created_at: str,
    target_symbol: str,
    active_file: str,
    negative_evidence: Sequence[str],
) -> bool:
    """Persist scoped checkpoint dead-branch evidence and report whether it changed.

    The evidence remains explicitly advisory: it can prevent duplicate route
    exploration but cannot promote graph truth or replace a fresh Lean check.
    """
    scope = _normalized_strategy_scope({"target_symbol": target_symbol, "active_file": active_file})
    items = []
    for value in negative_evidence:
        text = _bounded_line(value, 500)
        if text and text not in items:
            items.append(text)
        if len(items) >= _CHECKPOINT_ADVISORY_ITEM_CAP:
            break
    if not scope or not checkpoint_id or not items:
        return False

    def mutate(summary: dict[str, Any]) -> None:
        current = [
            dict(entry)
            for entry in (summary.get("checkpoint_advisories") or [])
            if isinstance(entry, Mapping)
        ]
        current = [
            entry for entry in current if str(entry.get("checkpoint_id", "")) != checkpoint_id
        ]
        current.append(
            {
                "checkpoint_id": checkpoint_id,
                "created_at": str(created_at or ""),
                **scope,
                "negative_evidence": items,
                "authority": "advisory-negative-evidence",
            }
        )
        summary["checkpoint_advisories"] = current[-_CHECKPOINT_ADVISORY_CAP:]

    update_json_file(plan_state_paths().summary_json, mutate)
    return True


def _current_checkpoint_advisory(
    summary: Mapping[str, Any],
    assignment: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the newest advisory record for the exact live assignment."""
    if not assignment:
        return {}
    for raw in reversed(list(summary.get("checkpoint_advisories") or [])):
        if not isinstance(raw, Mapping):
            continue
        if _strategy_scope_matches(raw, assignment):
            evidence = []
            for value in raw.get("negative_evidence") or []:
                text = _bounded_line(value, 500)
                if text:
                    evidence.append(text)
                if len(evidence) >= _CHECKPOINT_ADVISORY_ITEM_CAP:
                    break
            if evidence:
                return {**dict(raw), "negative_evidence": evidence}
    return {}


# ---------------------------------------------------------------------------
# Declaration-truth reconciliation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclTruth:
    present: bool
    has_sorry: bool
    has_error_diag: bool = False
    declaration_text: str = ""
    source_sha256: str = ""


def reconcile(
    bp: Blueprint, truth: Mapping[tuple[str, str], DeclTruth]
) -> tuple[Blueprint, list[dict[str, Any]]]:
    """Anti-drift pass against on-disk declaration truth.

    Downgrades ``proved`` back to ``stated`` when the declaration reappears
    with a sorry/errors, to ``conjectured`` when it vanishes or transitively
    depends on a false/parked node; promotes ``conjectured`` to ``stated`` when
    a named stub now exists on disk, except for planner/decomposer candidates
    whose own metadata explicitly says they still need validation; NEVER
    promotes to ``proved`` (kernel-gate-only). Returns the new graph plus change
    events; only files present in ``truth`` are judged (absent files were not
    scanned).
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
        explicitly_uncertain_advisory = node.generated_by.strip().lower() in {
            "planner",
            "decomposer",
        } and bool(planner_candidate_admission.candidate_uncertainty_evidence(node.to_mapping()))
        if node.status == "proved" and updated.has_invalid_dependency(node.id):
            new_status = "conjectured"
        elif node.status == "proved" and (not present or dirty):
            new_status = "stated" if present else "conjectured"
        elif (
            not present
            and node.generated_by.strip().lower()
            in {"planner", "decomposer", "prover-edit", "prover-edit-backfill"}
            and node.status in {"stated", "audited", "proving", "blocked"}
        ):
            # Generated nodes can outlive a rolled-back, retired, or interrupted
            # source transaction. An absent declaration is only an advisory
            # conjecture; leaving it stated makes a nonexistent helper reappear
            # as the active dependency frontier after restart.
            new_status = "conjectured"
        elif node.status == "conjectured" and present and not explicitly_uncertain_advisory:
            new_status = "stated"
        refreshed_statement = (
            str(decl.declaration_text or "") if present and decl is not None else node.statement
        )
        refreshed_source = (
            str(decl.source_sha256 or "") if present and decl is not None else node.source_sha256
        )
        refreshed = replace(
            node,
            statement=refreshed_statement or node.statement,
            source_sha256=refreshed_source or node.source_sha256,
            status=new_status,
            owner="" if new_status == "proved" else node.owner,
        )
        if refreshed != node:
            updated = updated.replace_node(refreshed)
        if new_status != node.status:
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


def retire_inactive_proving_nodes(
    bp: Blueprint,
    truth: Mapping[tuple[str, str], DeclTruth],
    *,
    active_node_id: str = "",
) -> tuple[Blueprint, list[dict[str, Any]]]:
    """Retire process-local ``proving`` states outside the active assignment.

    Queue assignments can change without first producing a theorem outcome,
    especially across a restart or deterministic route change. ``proving`` is
    an ownership marker for the one live assignment, so every other node must
    return to non-kernel work state. Preserve a recorded fidelity audit, and
    use explicit declaration absence only when the file scan proved it.
    """
    updated = bp
    events: list[dict[str, Any]] = []
    for node in bp.nodes:
        if node.status != "proving" or node.id == active_node_id:
            continue
        decl = truth.get((node.file, node.name))
        if decl is not None and not decl.present:
            status = "conjectured"
        else:
            fidelity_audited = "fidelity: audited" in {
                part.strip() for part in str(node.notes or "").split(";")
            }
            status = "audited" if fidelity_audited else "stated"
        updated = updated.replace_node(replace(node, status=status))
        events.append(
            {
                "event": "plan-graph-assignment-retired",
                "node_id": node.id,
                "name": node.name,
                "file": node.file,
                "from": "proving",
                "to": status,
                "why": "inactive queue assignment",
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


def _line(text: Any) -> str:
    """One physical line: collapse ALL whitespace (incl. newlines).

    Every caller-controlled string is rendered through this, so no goal /
    note / packet field / report summary can fabricate a heading line and
    hijack the '## Notes' tail anchor.
    """
    return " ".join(str(text or "").split())


def _bounded_line(text: Any, limit: int = 500) -> str:
    """Return one normalized line within the prompt-facing prose ceiling."""
    normalized = _line(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 16)].rstrip() + " ...[truncated]"


def _current_route_decision(
    summary: Mapping[str, Any], recent_routes: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """Return the campaign's current route, falling back to journal history."""
    campaign = summary.get("campaign")
    if isinstance(campaign, Mapping):
        current = campaign.get("last_route_decision")
        if isinstance(current, Mapping) and str(current.get("route", "") or "").strip():
            payload = dict(current)
            if recent_routes:
                latest = dict(recent_routes[-1])
                latest_target = str(latest.get("target_symbol") or latest.get("name") or "").strip()
                current_target = str(payload.get("target_symbol", "") or "").strip()
                if str(latest.get("route", "") or "") == str(payload.get("route", "") or "") and (
                    not latest_target or not current_target or latest_target == current_target
                ):
                    payload = {**latest, **payload}
            return payload
    return dict(recent_routes[-1]) if recent_routes else {}


def _current_queue_assignment(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable queue assignment identity without its stale body slice."""
    manager = summary.get("queue_manager_state")
    if not isinstance(manager, Mapping):
        return {}
    assignment = manager.get("current_queue_assignment")
    return _normalized_queue_assignment(assignment)


def _current_strategy_notes(
    summary: Mapping[str, Any],
    assignment: Mapping[str, Any],
) -> list[str]:
    """Return actionable strategy prose only for its exact live assignment.

    Legacy unscoped notes remain available when no queue assignment exists,
    but are suppressed under an active assignment because their theorem scope
    cannot be proven and stale steps can direct edits at the wrong target.
    """
    notes = [str(item) for item in (summary.get("strategy_notes") or [])]
    if not notes or not assignment:
        return notes
    scope = _normalized_strategy_scope(summary.get(_STRATEGY_SCOPE_KEY))
    return notes if _strategy_scope_matches(scope, assignment) else []


def _normalized_queue_assignment(assignment: Any) -> dict[str, Any]:
    """Return a validated queue assignment identity from a runtime or snapshot value."""
    if not isinstance(assignment, Mapping):
        return {}
    target = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    if not target or not active_file:
        return {}
    return {"target_symbol": target, "active_file": active_file}


def _route_summary(route: Mapping[str, Any]) -> str:
    """Render bounded operational route identity without advisory rationale.

    Route reasons may originate in an LLM decision and can contain unchecked
    mathematical claims. The append-only journal retains that prose for audit,
    but prompt-facing plan and resume views expose only route-diversity
    metadata that cannot masquerade as kernel-verified knowledge.
    """
    name = _bounded_line(route.get("target_symbol") or route.get("name") or "")
    active_file = _bounded_line(route.get("active_file") or route.get("file") or "")
    target = f" for `{name}`" if name else ""
    if active_file:
        target += f" ({active_file})"
    metadata: list[str] = []
    for key in ("trigger", "source", "epoch", "routes_used"):
        value = _bounded_line(route.get(key, ""), 80)
        if value:
            metadata.append(f"{key}={value}")
    boundary = " [routing metadata only"
    if metadata:
        boundary += "; " + "; ".join(metadata)
    boundary += "]"
    return f"`{_bounded_line(route.get('route', 'unknown'), 80)}`{target}{boundary}"


def _route_matches_assignment(route: Mapping[str, Any], assignment: Mapping[str, Any]) -> bool:
    """Return whether a route belongs to the deterministic queue assignment.

    A campaign route remains useful historical evidence after queue rotation,
    but it must not be presented as the current route for a different theorem.
    Declaration identity is authoritative; when both records include a file,
    require those paths to identify the same file as well.
    """
    route_target = str(route.get("target_symbol") or route.get("name") or "").strip()
    assignment_target = str(assignment.get("target_symbol", "") or "").strip()
    if not route_target or route_target != assignment_target:
        return False
    route_file = str(route.get("active_file") or route.get("file") or "").strip()
    assignment_file = str(assignment.get("active_file", "") or "").strip()
    if not route_file or not assignment_file:
        return True
    if route_file == assignment_file:
        return True
    try:
        return Path(route_file).resolve(strict=False) == Path(assignment_file).resolve(strict=False)
    except OSError:
        return False


def _assignment_dependency_frontier(
    bp: Blueprint,
    assignment: Mapping[str, Any],
) -> tuple[GraphNode, ...]:
    """Return ready direct dependencies for the exact queue assignment.

    A global frontier is scheduling inventory, not the current theorem's proof
    plan. Once the deterministic queue owns an assignment, generated views
    expose only its direct dependency/split family beneath that assignment.
    """
    normalized = _normalized_queue_assignment(assignment)
    if not normalized:
        return bp.frontier()
    target_id = node_id_for(normalized["target_symbol"], normalized["active_file"])
    direct_dependency_ids = {
        edge.target for edge in bp.edges if edge.kind == "depends_on" and edge.source == target_id
    } | {edge.source for edge in bp.edges if edge.kind == "split_of" and edge.target == target_id}
    return tuple(node for node in bp.frontier() if node.id in direct_dependency_ids)


def render_plan_md(
    bp: Blueprint,
    summary: Mapping[str, Any],
    *,
    recent_routes: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One-way render of the machine state (JSON is authority)."""
    counts = _status_counts(bp)
    current_route = _current_route_decision(summary, recent_routes)
    assignment = _current_queue_assignment(summary)
    if current_route and assignment and not _route_matches_assignment(current_route, assignment):
        # Queue rotation retires theorem-local strategy immediately. Keep the
        # old decision in the historical log, but never label it as current
        # while the next scope-entry consultation is still in flight.
        current_route = {}
    lines = [
        "# Proving Plan",
        "",
        PLAN_MD_GENERATED_MARKER,
        "",
        "## Goal",
        "",
        _line(bp.goal) or _line(summary.get("goal", "")) or "[not set]",
        "",
        "## Current state",
        "",
        (
            " · ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
            or "empty graph"
        ),
        "- freshness: graph statuses are kernel-reconciled; current Lean source and queue "
        "assignment outrank stored declaration bodies",
        "",
        "## Strategy",
        "",
    ]
    strategy = _current_strategy_notes(summary, assignment)
    if assignment:
        lines.append(
            f"- current deterministic assignment: `{_bounded_line(assignment['target_symbol'], 160)}` "
            f"({_bounded_line(assignment['active_file'], 240)})"
        )
    if current_route:
        lines.append(f"- current orchestrator route: {_route_summary(current_route)}")
        lines.append(
            "- route rationales are omitted from generated views because they are advisory, "
            "not kernel-verified mathematical facts"
        )
    if strategy:
        lines.extend(f"- {_bounded_line(note)}" for note in strategy[:20])
    if not assignment and not current_route and not strategy:
        lines.append("- [none yet]")
    checkpoint_advisory = _current_checkpoint_advisory(summary, assignment)
    if checkpoint_advisory:
        lines.extend(["", "## Advisory dead-branch record", ""])
        lines.append(
            "- checkpoint evidence is route-history guidance only; revalidate it against the "
            "current source and Lean state before relying on it"
        )
        lines.extend(
            f"- {_bounded_line(item, 500)}"
            for item in checkpoint_advisory.get("negative_evidence", [])
        )
    lines.extend(["", "## Frontier", ""])
    frontier = _assignment_dependency_frontier(bp, assignment)
    if assignment:
        target_node = bp.node_by_id(
            node_id_for(assignment["target_symbol"], assignment["active_file"])
        )
        target_status = f" [{target_node.status}]" if target_node is not None else ""
        lines.append(
            f"- current assignment: `{_line(assignment['target_symbol'])}` "
            f"({_line(assignment['active_file'])}){target_status}"
        )
        lines.extend(
            f"- dependency frontier: `{_line(node.name)}` ({_line(node.file)})"
            for node in frontier[:19]
        )
        if not frontier:
            lines.append("- dependency frontier: [empty]")
    elif frontier:
        lines.extend(f"- `{_line(node.name)}` ({_line(node.file)})" for node in frontier[:20])
    else:
        lines.append("- [empty]")
    deferred_items = [
        dict(item)
        for item in (summary.get("deferred_queue_items") or [])
        if isinstance(item, Mapping)
    ]
    lines.extend(["", "## Deferred queue items (still pending)", ""])
    if deferred_items:
        for item in deferred_items[:20]:
            lines.append(
                f"- `{_line(item.get('target_symbol', '[unknown]'))}` "
                f"({_line(item.get('active_file', '[unknown]'))}) — "
                f"{_line(item.get('reason', 'route cooled down'))}; return when "
                f"{_line(item.get('return_condition', 'a distinct route is available'))}"
            )
    else:
        lines.append("- [none]")
    lines.extend(["", "## Grounding", ""])
    findings = list(summary.get("grounding_findings") or [])
    if findings:
        lines.extend(f"- {_line(finding)}" for finding in findings[:20])
    else:
        lines.append("- [none yet]")
    lines.extend(["", "## Exploration outcomes", ""])
    outcomes = recent_exploration_outcomes(bp, assignment, limit=12)
    if outcomes:
        for outcome in outcomes:
            detail = _bounded_line(outcome.get("detail", ""), 500)
            suffix = f" — {detail}" if detail else ""
            lines.append(f"- [{_line(outcome['type'])}] `{_line(outcome['subject'])}`{suffix}")
    else:
        lines.append("- [none yet]")
    lines.extend(["", "## Decision log", ""])
    packets = list(summary.get("decision_packets") or [])
    if packets:
        for packet in packets[-10:]:
            lines.append(
                f"- {_line(packet.get('packet_id', '?'))}: {_line(packet.get('scope', '?'))} "
                f"`{_line(packet.get('target_symbol', '?'))}` -> "
                f"{_line(packet.get('decision')) or 'undecided'}"
            )
    for route in recent_routes[-_RECENT_ROUTE_LIMIT:]:
        timestamp = _bounded_line(route.get("ts", "?"), 80) or "?"
        trigger = _bounded_line(route.get("trigger", "?"), 80) or "?"
        lines.append(f"- {timestamp} [{trigger}] route {_route_summary(route)}")
    if not packets and not recent_routes:
        lines.append("- [none]")
    lines.extend(["", "## Dead ends & proven false", ""])
    dead = [node for node in bp.nodes if node.status in {"false", "parked"}]
    if dead:
        lines.extend(
            f"- `{_line(node.name)}` [{node.status}] ({_line(node.file)})" for node in dead
        )
    dead_outcomes = [
        outcome
        for outcome in outcomes
        if outcome["type"]
        in {
            "blocked_by_source_order",
            "invalidated",
            "rejected_by_admission",
            "rejected_by_kernel",
            "superseded",
        }
    ]
    if dead_outcomes:
        for outcome in dead_outcomes[-8:]:
            detail = _bounded_line(outcome.get("detail", ""), 500)
            suffix = f" — {detail}" if detail else ""
            lines.append(
                f"- `{_line(outcome['subject'])}` " f"[{_line(outcome['type'])} attempt]{suffix}"
            )
    if not dead and not dead_outcomes:
        lines.append("- [none]")
    lines.extend(["", "## Final report", ""])
    final_report = dict(summary.get("final_report") or {})
    if final_report:
        lines.append(f"- status: {_line(final_report.get('status', 'in-progress'))}")
        if final_report.get("summary"):
            lines.append(f"- summary: {_line(final_report['summary'])}")
    else:
        lines.append("- status: in-progress")
    lines.append("")
    return "\n".join(lines)


def generated_plan_prompt_view(
    plan_md_text: str, *, max_chars: int = PLAN_PROMPT_VIEW_MAX_CHARS
) -> str:
    """Return a bounded generated-only plan view for model prompts.

    ``## Notes`` is intentionally excluded: it is a verbatim user-owned lab
    tail whose inventories and copied theorem bodies can become stale. Preserve
    the current-state-heavy start and the end of an oversized generated render
    so the goal/strategy/frontier and recent decision/final-report sections both
    remain visible, with explicit source-hash/count omission telemetry.
    """
    return generated_plan_view(plan_md_text, max_chars=max_chars)


def read_generated_plan_prompt_view(*, max_chars: int = PLAN_PROMPT_VIEW_MAX_CHARS) -> str:
    """Read only plan.md's generated prefix and return its bounded prompt view.

    Stop at the canonical Notes boundary while streaming the file so long
    user-owned history never enters orchestrator memory merely to be stripped
    afterward. Missing or unreadable plans yield an empty view.
    """
    if not plan_state_enabled():
        return ""
    _reconcile_persisted_planner_arithmetic()
    try:
        with plan_state_paths().plan_md.open("r", encoding="utf-8") as handle:
            generated_lines: list[str] = []
            for line in handle:
                if line.strip() == _NOTES_HEADING:
                    break
                generated_lines.append(line)
    except OSError:
        return ""
    return generated_plan_prompt_view("".join(generated_lines), max_chars=max_chars)


def save_plan_md(bp: Blueprint, summary: Mapping[str, Any]) -> None:
    """Regenerate plan.md, preserving the free-form '## Notes' tail verbatim.

    The tail anchor is a line-start heading match — '## Notes' appearing
    INSIDE rendered prose (every prose line renders with a '- ' prefix) can
    never hijack the tail boundary.
    """
    if not plan_state_enabled():
        return
    path = plan_state_paths().plan_md
    notes_tail = f"{_NOTES_HEADING}\n\n[free-form notes below survive regeneration]\n"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        match = _NOTES_HEADING_RE.search(existing)
        if match:
            notes_tail = existing[match.start() :]
    _atomic_write_text(
        path,
        render_plan_md(bp, summary, recent_routes=recent_orchestrator_routes()) + "\n" + notes_tail,
    )


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
# Prompt-surface blocks.
# ---------------------------------------------------------------------------


def artifact_paths_block() -> str:
    """Byte-stable artifact-path lines (safe for the RCP prefix-cache prefix)."""
    if not plan_state_enabled():
        return ""
    paths = plan_state_paths()
    return "\n".join(
        [
            "Living plan artifacts (managed plan reads expose bounded, read-only generated "
            "sections; never edit or paginate the historical user-owned Notes body):",
            "- authority order: current queue assignment + Lean source/kernel diagnostics > "
            "generated graph view > preserved historical Notes",
            f"- plan: {paths.plan_md}",
            f"- dependency graph machine snapshot (do not read directly): {paths.blueprint_json}",
            f"- summary machine snapshot (do not read directly): {paths.summary_json}",
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
    summary = load_summary()
    counts = _status_counts(bp)
    lines = [
        "Dependency graph digest:",
        "- " + " · ".join(f"{status}: {count}" for status, count in sorted(counts.items())),
    ]
    assignment = _current_queue_assignment(summary)
    if assignment:
        lines.append(
            f"- deterministic assignment: `{_bounded_line(assignment['target_symbol'], 160)}` "
            f"({_bounded_line(assignment['active_file'], 240)})"
        )
    route = _current_route_decision(summary, recent_orchestrator_routes(limit=1))
    if route and (not assignment or _route_matches_assignment(route, assignment)):
        lines.append(f"- current route: {_route_summary(route)}")
    for outcome in recent_exploration_outcomes(bp, assignment, limit=2):
        detail = _bounded_line(outcome.get("detail", ""), 180)
        suffix = f": {detail}" if detail else ""
        lines.append(
            f"- outcome [{_bounded_line(outcome['type'], 40)}] "
            f"`{_bounded_line(outcome['subject'], 120)}`{suffix}"
        )
    frontier = _assignment_dependency_frontier(bp, assignment)
    for node in frontier[:8]:
        label = "dependency frontier" if assignment else "frontier"
        lines.append(f"- {label}: `{node.name}` ({node.file})")
    return "\n".join(lines[:10])


def resume_context_block(*, current_queue_assignment: Mapping[str, Any] | None = None) -> str:
    """Return the '[LEANFLOW PLAN-STATE RESUME]' startup handoff block.

    Persisted artifacts, not checkpoint prose, are the resume authority.
    Renders goal, counters,
    frontier, open decision packets, and dead ends; '' when plan-state is
    off or no graph exists yet (caller falls back to checkpoint replay).
    ``current_queue_assignment`` lets the runner override the durable identity
    after deterministic startup selection rotates the queue.
    """
    if not plan_state_enabled():
        return ""
    bp = load_blueprint()
    summary = load_summary()
    if not bp.nodes and not bp.goal and not summary:
        return ""
    counts = _status_counts(bp)
    recent_routes = recent_orchestrator_routes(limit=4)
    lines = [
        "[LEANFLOW PLAN-STATE RESUME]",
        f"- goal: {_bounded_line(bp.goal or str(summary.get('goal', '') or ''), 1000) or '[not set]'}",
        "- state: "
        + (
            " · ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
            or "empty graph"
        ),
    ]
    assignment = (
        _current_queue_assignment(summary)
        if current_queue_assignment is None
        else _normalized_queue_assignment(current_queue_assignment)
    )
    if assignment:
        lines.append(
            f"- current deterministic assignment: `{_bounded_line(assignment['target_symbol'], 160)}` "
            f"({_bounded_line(assignment['active_file'], 240)})"
        )
    campaign = summary.get("campaign")
    if isinstance(campaign, Mapping):
        epoch = _bounded_line(campaign.get("epoch", ""), 40)
        streak = _bounded_line(campaign.get("no_progress_route_streak", ""), 40)
        route_limit = _bounded_line(campaign.get("no_progress_route_limit", ""), 40)
        if epoch:
            route_state = f"- campaign epoch: {epoch}"
            if streak:
                route_state += f" · route streak: {streak}"
                if route_limit:
                    route_state += f"/{route_limit}"
            lines.append(route_state)
    route = _current_route_decision(summary, recent_routes)
    if route and (not assignment or _route_matches_assignment(route, assignment)):
        lines.append(f"- current orchestrator route: {_route_summary(route)}")
    for recent in recent_routes[-3:]:
        lines.append(f"- recent route decision: {_route_summary(recent)}")
    if route or recent_routes:
        lines.append(
            "- routing metadata boundary: route identities, triggers, sources, epochs, and "
            "diversity streaks are operational only; advisory route rationales are omitted "
            "because they are not kernel-verified mathematical facts"
        )
    checkpoint_advisory = _current_checkpoint_advisory(summary, assignment)
    if checkpoint_advisory:
        lines.append(
            "- advisory dead-branch boundary: the following checkpoint conclusions prevent "
            "duplicate exploration but are not kernel-verified facts; refresh source and Lean "
            "state before relying on them"
        )
        lines.extend(
            f"- prior negative evidence: {_bounded_line(item, 500)}"
            for item in checkpoint_advisory.get("negative_evidence", [])
        )
    for node in _assignment_dependency_frontier(bp, assignment)[:8]:
        label = "dependency frontier" if assignment else "frontier"
        lines.append(f"- {label}: `{node.name}` ({node.file})")
    open_packets = [
        dict(packet)
        for packet in (summary.get("decision_packets") or [])
        if isinstance(packet, Mapping) and not packet.get("decision")
    ]
    for packet in open_packets[-5:]:
        options = ", ".join(str(option) for option in (packet.get("options") or []))
        lines.append(
            f"- open decision packet {packet.get('packet_id', '?')}: "
            f"{packet.get('scope', '?')} `{packet.get('target_symbol', '?')}`"
            + (f" (options: {options})" if options else "")
        )
    for node in [n for n in bp.nodes if n.status in {"false", "parked"}][:8]:
        lines.append(f"- dead end: `{node.name}` [{node.status}]")
    lines.append(
        "- resume authority: generated graph status plus deterministic queue inventory; current "
        "Lean source and queue assignment outrank stored graph statements; refresh "
        "source/diagnostics before using any stored declaration body"
    )
    lines.append(
        "- plan.md Notes are preserved historical context, not inventory or declaration truth; "
        "recompute sorry counts from the deterministic queue and Lean source"
    )
    return "\n".join(lines)


def artifact_context_block() -> str:
    """The single injection string for non-prefix-cached prompt surfaces."""
    if not plan_state_enabled():
        return ""
    paths_block = artifact_paths_block()
    digest = frontier_digest_block()
    return f"{paths_block}\n\n{digest}".strip() if digest else paths_block
