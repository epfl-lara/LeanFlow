"""Define immutable result and capability records for Lean services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class LeanCapabilityReport:
    cwd: str
    project_root: str
    project_valid: bool
    project_error: str
    binaries: dict[str, bool]
    mcp_tools: dict[str, str]
    search_providers: list[str]
    helper_tools: dict[str, bool]
    workers: list[str]
    degraded_reasons: list[str]
    mcp_server_roles: dict[str, str] = field(default_factory=dict)
    managed_mcp_servers: dict[str, bool] = field(default_factory=dict)
    power_modes: dict[str, Any] = field(default_factory=dict)
    incremental: dict[str, Any] = field(default_factory=dict)
    remote_search_policy: str = "public-fallbacks-enabled"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["worker_specs"] = list(self.workers)
        payload["workers_scope"] = "registered_lean_workflow_specs"
        payload["workers_note"] = (
            "This field reports registered Lean workflow specs, not live research or "
            "planner-agent capacity."
        )
        return payload


@dataclass(frozen=True)
class LeanSorryFinding:
    file: str
    line: int
    declaration: str
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanInspection:
    target: str
    project_root: str
    diagnostics: str
    goals: str
    sorry_count: int | None
    project_sorry_count: int | None
    blocker_kind: str
    queue_items: list[dict[str, Any]] = field(default_factory=list)
    capability_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanVerificationResult:
    ok: bool
    mode: str
    command: str
    target: str
    output: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanSearchResult:
    query: str
    mode: str
    attempted_providers: list[str]
    results: list[dict[str, Any]]
    degraded_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanAxiomReport:
    target: str
    file_path: str
    ok: bool
    axioms: list[str]
    custom_axioms: list[str]
    classical: bool
    choice: bool
    note: str
    inspection_succeeded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowRouteDecision:
    workflow_kind: str
    skill_name: str
    route_action: str
    blocker_kind: str
    recommended_worker: str
    search_exhausted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerRequest:
    worker: str
    goal: str
    context: str
    file_path: str = ""
    line: int | None = None
    use_file_lock: bool = True
    allow_delegation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerResult:
    worker: str
    mode: str
    dispatched: bool
    summary: str
    result: Any = None
    lock: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
