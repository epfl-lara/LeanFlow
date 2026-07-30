"""Define immutable value types and lineage helpers for dispatched jobs.

Jobs carry independent API-step and wall-clock budgets. Dotted job identifiers
preserve ancestry so each owner can list, track, and terminate descendants.
Lifecycle and ledger behavior lives in ``dispatch_service``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from core.process_identity import ProcessIdentity

ARCHETYPES = (
    "prover",
    "empirical",
    "deep_search",
    "negation_probe",
    "decomposition",
)
STATES = ("proposed", "deployed", "running", "done", "failed", "stuck", "killed")
TERMINAL_STATES = frozenset({"done", "failed", "stuck", "killed"})
DISPATCH_ROLES = ("orchestrator", "planner", "decomposer", "human")  # N2
DELIVERABLES = (
    "findings_report",
    "probe_verdict",
    "prove_outcome",
    "experiment_result",
    "decomposition_report",
)
SCRATCH_ISOLATION_VERSION = 2
ASSIGNMENT_REVISION_INPUT_KEY = "assignment_statement_sha256"
SOURCE_REVISION_INPUT_KEY = "active_file_sha256"
MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY = "mathematical_delta_signature"

_ARCHETYPE_TAGS = {
    "prover": "pv",
    "empirical": "em",
    "deep_search": "ds",
    "negation_probe": "np",
    "decomposition": "dc",
}

# Legal ledger state machine (STATES x next).
_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"deployed", "killed"}),
    "deployed": frozenset({"running", "failed", "killed"}),
    "running": frozenset({"done", "failed", "stuck", "killed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "stuck": frozenset({"killed"}),
    "killed": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True)
class JobBudget:
    api_steps: int  # -> delegate max_iterations / AGENT_MAX_TURNS for spawns
    wall_clock_s: int  # patience anchor

    def is_valid(self) -> bool:
        return self.api_steps > 0 and self.wall_clock_s > 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> JobBudget:
        data = dict(raw or {})
        return cls(
            api_steps=int(data.get("api_steps", 0) or 0),
            wall_clock_s=int(data.get("wall_clock_s", 0) or 0),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"api_steps": self.api_steps, "wall_clock_s": self.wall_clock_s}


@dataclass(frozen=True)
class JobSpec:
    job_id: str  # dotted lineage id
    archetype: str
    requester_role: str  # validated against DISPATCH_ROLES (N2)
    objective: str  # self-contained (the delegate 'goal')
    budget: JobBudget
    deliverable: str  # schema id from DELIVERABLES
    inputs: dict[str, Any] = field(default_factory=dict)
    toolsets: tuple[str, ...] = ()
    scope: dict[str, Any] = field(
        default_factory=dict
    )  # {"file_locks": [...], "scratch_only": bool}
    parent_job_id: str = ""
    report_to: str = ""  # plan-state section name

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.job_id.strip():
            problems.append("job_id is required")
        if self.archetype not in ARCHETYPES:
            problems.append(f"unknown archetype {self.archetype!r}")
        if self.requester_role not in DISPATCH_ROLES:
            problems.append(
                f"requester_role {self.requester_role!r} may not dispatch (N2: "
                "the manager suggests, the prover escalates)"
            )
        if not self.objective.strip():
            problems.append("objective is required (self-contained)")
        if not self.budget.is_valid():
            problems.append("budget must declare positive api_steps and wall_clock_s")
        if self.deliverable not in DELIVERABLES:
            problems.append(f"unknown deliverable schema {self.deliverable!r}")
        if self.archetype == "decomposition":
            if self.deliverable != "decomposition_report":
                problems.append("decomposition jobs require decomposition_report deliverables")
            if self.scope.get("scratch_only") is not True:
                problems.append("decomposition jobs must be scratch_only parent-write proposals")
        elif self.deliverable == "decomposition_report":
            problems.append("decomposition_report is reserved for decomposition jobs")
        route_anchor_job_id = str(self.inputs.get("route_anchor_job_id", "") or "").strip()
        if route_anchor_job_id:
            provenance = self.inputs.get("route_anchor_provenance")
            finding_summary = str(self.inputs.get("route_anchor_finding_summary", "") or "").strip()
            if not isinstance(provenance, Mapping) or not finding_summary:
                problems.append("evidence-derived job lacks its source finding payload")
        return problems

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> JobSpec:
        data = dict(raw or {})
        return cls(
            job_id=str(data.get("job_id", "") or ""),
            archetype=str(data.get("archetype", "") or ""),
            requester_role=str(data.get("requester_role", "") or ""),
            objective=str(data.get("objective", "") or ""),
            budget=JobBudget.from_mapping(data.get("budget")),
            deliverable=str(data.get("deliverable", "") or ""),
            inputs=dict(data.get("inputs") or {}),
            toolsets=tuple(str(t) for t in (data.get("toolsets") or []) if str(t)),
            scope=dict(data.get("scope") or {}),
            parent_job_id=str(data.get("parent_job_id", "") or ""),
            report_to=str(data.get("report_to", "") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "archetype": self.archetype,
            "requester_role": self.requester_role,
            "objective": self.objective,
            "budget": self.budget.to_mapping(),
            "deliverable": self.deliverable,
            "inputs": dict(self.inputs),
            "toolsets": list(self.toolsets),
            "scope": dict(self.scope),
            "parent_job_id": self.parent_job_id,
            "report_to": self.report_to,
        }


@dataclass(frozen=True)
class LedgerEntry:
    spec: JobSpec
    state: str = "proposed"
    agent_session_ids: tuple[str, ...] = ()  # reconciled from activity/agents
    run_id: str = ""  # spawn backend only
    launch_nonce: str = ""  # async launch transaction identity
    launch_started_at: str = ""  # deployed/launching reservation time
    launch_attempt: int = 0  # monotonic per job, including crash recovery
    process_id: int = 0  # spawn backend only
    process_group_id: int = 0  # spawn backend only
    process_session_id: int = 0  # spawn backend only
    process_token_sha256: str = ""  # spawn backend only
    process_released_at: str = ""  # durable proof that terminal capacity is free
    process_release_reason: str = ""  # bounded release-evidence classifier
    process_release_evidence_sha256: str = ""  # canonical non-secret evidence digest
    process_release_observed_started_at: str = ""  # diagnostic only, never authority
    process_release_report_key: str = ""  # deterministic activity idempotency key
    process_release_reported_at: str = ""  # durable diagnostic acknowledgement
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    consumed: bool = False
    notes: str = ""

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def with_state(self, state: str, **changes: Any) -> LedgerEntry:
        if not can_transition(self.state, state):
            raise ValueError(f"illegal ledger transition {self.state} -> {state}")
        return replace(self, state=state, **changes)

    def process_identity(self) -> ProcessIdentity:
        """Return the exact persisted ownership identity for this worker."""
        return ProcessIdentity(
            pid=self.process_id,
            process_group_id=self.process_group_id,
            session_id=self.process_session_id,
            token_sha256=self.process_token_sha256,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> LedgerEntry:
        data = dict(raw or {})
        state = str(data.get("state", "proposed") or "proposed")
        return cls(
            spec=JobSpec.from_mapping(data.get("spec") or {}),
            state=state if state in STATES else "proposed",
            agent_session_ids=tuple(
                str(sid) for sid in (data.get("agent_session_ids") or []) if str(sid)
            ),
            run_id=str(data.get("run_id", "") or ""),
            launch_nonce=str(data.get("launch_nonce", "") or ""),
            launch_started_at=str(data.get("launch_started_at", "") or ""),
            launch_attempt=max(0, int(data.get("launch_attempt", 0) or 0)),
            process_id=int(data.get("process_id", 0) or 0),
            process_group_id=int(data.get("process_group_id", 0) or 0),
            process_session_id=int(data.get("process_session_id", 0) or 0),
            process_token_sha256=str(data.get("process_token_sha256", "") or ""),
            process_released_at=str(data.get("process_released_at", "") or ""),
            process_release_reason=str(data.get("process_release_reason", "") or ""),
            process_release_evidence_sha256=str(
                data.get("process_release_evidence_sha256", "") or ""
            ),
            process_release_observed_started_at=str(
                data.get("process_release_observed_started_at", "") or ""
            ),
            process_release_report_key=str(data.get("process_release_report_key", "") or ""),
            process_release_reported_at=str(data.get("process_release_reported_at", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            started_at=str(data.get("started_at", "") or ""),
            finished_at=str(data.get("finished_at", "") or ""),
            result=dict(data.get("result") or {}),
            consumed=bool(data.get("consumed", False)),
            notes=str(data.get("notes", "") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_mapping(),
            "state": self.state,
            "agent_session_ids": list(self.agent_session_ids),
            "run_id": self.run_id,
            "launch_nonce": self.launch_nonce,
            "launch_started_at": self.launch_started_at,
            "launch_attempt": self.launch_attempt,
            "process_id": self.process_id,
            "process_group_id": self.process_group_id,
            "process_session_id": self.process_session_id,
            "process_token_sha256": self.process_token_sha256,
            "process_released_at": self.process_released_at,
            "process_release_reason": self.process_release_reason,
            "process_release_evidence_sha256": self.process_release_evidence_sha256,
            "process_release_observed_started_at": self.process_release_observed_started_at,
            "process_release_report_key": self.process_release_report_key,
            "process_release_reported_at": self.process_release_reported_at,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": dict(self.result),
            "consumed": self.consumed,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Lineage (owner N3)
# ---------------------------------------------------------------------------


def next_job_id(
    existing_ids: list[str] | tuple[str, ...], parent_job_id: str, archetype: str
) -> str:
    """Mint the next dotted lineage id under ``parent_job_id``.

    Grammar: ``<parent>.<tag>-<seq>`` with a per-parent zero-padded sequence
    over ALL child ids (any archetype), so ids stay unique and ordered.
    """
    tag = _ARCHETYPE_TAGS.get(archetype)
    if tag is None:
        raise ValueError(f"unknown archetype {archetype!r}")
    parent = parent_job_id.strip()
    if not parent:
        raise ValueError("parent_job_id is required (lineage always has a root)")
    prefix = parent + "."
    children = [
        job_id
        for job_id in existing_ids
        if job_id.startswith(prefix) and "." not in job_id[len(prefix) :]
    ]
    return f"{parent}.{tag}-{len(children) + 1:03d}"


def ancestors(job_id: str) -> tuple[str, ...]:
    """Dotted prefixes of ``job_id``, outermost first (excluding itself)."""
    parts = job_id.split(".")
    return tuple(".".join(parts[: index + 1]) for index in range(len(parts) - 1))


def is_ancestor(candidate: str, job_id: str) -> bool:
    return bool(candidate) and job_id.startswith(candidate + ".")


def descendants(existing_ids: list[str] | tuple[str, ...], job_id: str) -> tuple[str, ...]:
    prefix = job_id + "."
    return tuple(candidate for candidate in existing_ids if candidate.startswith(prefix))
