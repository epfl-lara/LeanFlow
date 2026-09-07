"""Drive standard and research proving with one bounded, authoritative controller."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.live_progress import LiveProgress
from leanflow_cli.workflows.prover.models import Dag, Node, digest
from leanflow_cli.workflows.prover.planning import json_report
from leanflow_cli.workflows.prover.scheduler import ready_nodes
from leanflow_cli.workflows.prover.source import (
    SourceConflictError,
    SourceDocument,
    declaration_source,
    discover,
    extract_scratch_replacements,
    lean_code_mask,
    project_path,
    read_source,
    sorry_spans,
    validate_hole_replacement,
    write_source,
)
from leanflow_cli.workflows.prover.store import RunStore, now
from leanflow_cli.workflows.prover.submission_cache import SubmissionCache, submission_key
from leanflow_cli.workflows.prover.verification import LeanVerifier

Session = Callable[..., dict[str, Any]]


class BudgetExhausted(RuntimeError):
    """The campaign cannot reserve another request allocation or check interval."""

    def __init__(self, message: str, *, code: str = "campaign_api_calls") -> None:
        super().__init__(message)
        self.code = code
        self.scope = "campaign"
        self.status = "timeout" if code == "campaign_wall_time" else "budget_exhausted"


class InfrastructureFailure(RuntimeError):
    """Report unavailable infrastructure without treating it as theorem failure."""

    def __init__(self, message: str, status: str = "provider_error") -> None:
        super().__init__(message)
        self.status = status


class ProverRuntime:
    """Own source mutations, persistent state, scheduling, and independent acceptance."""

    def __init__(
        self,
        *,
        root: Path,
        targets: list[Path],
        config: ProverConfig,
        run_id: str = "",
        session: Session | None = None,
        verifier: Any = None,
        goal: str = "",
        resume: bool = False,
        observer: Any = None,
    ) -> None:
        self.root = root.resolve()
        project_path(self.root, ".leanflow")
        self.targets = [path.resolve() for path in targets]
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex[:16]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.run_id):
            raise ValueError("invalid prover run ID")
        if session is None:
            from leanflow_cli.workflows.prover.agent_session import run_session

            session = run_session
        self.session = session
        self.verifier = verifier or LeanVerifier(self.root, config.allowed_axioms, config.timeout_s)
        self.store = RunStore(self.root, self.run_id)
        self.lock = threading.RLock()
        self.controller_thread = threading.get_ident()
        self.progress = LiveProgress(self)
        self.submission_cache = SubmissionCache()
        self.reserved = 0
        self.consumed = 0
        self.started = time.monotonic()
        self.elapsed_before = 0.0
        self.pending: dict[Future[dict[str, Any]], dict[str, Any]] = {}
        self.completions: queue.SimpleQueue[Future[dict[str, Any]]] = queue.SimpleQueue()
        self.cancelled = threading.Event()
        self.stopping = False
        self.scratch_before: dict[str, str] = {}
        self.resume_jobs: dict[str, dict[str, Any]] = {}
        self.resume_negation_jobs: dict[str, dict[str, Any]] = {}
        self.resume_role_jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.goal = goal
        self.observer = observer
        self.store.on_event = observer.event if observer is not None else None
        if resume:
            self._restore()
            if verifier is None:
                self.verifier = LeanVerifier(
                    self.root, self.config.allowed_axioms, self.config.timeout_s
                )
        else:
            if (self.store.directory / "state.json").exists():
                raise ValueError("run ID already exists; explicitly request resume")
            self.dag, self.documents = discover(
                self.root, self.targets, fill_definitions=config.fill_definitions
            )
            self.dag.validate(config.max_nodes)
            self.state: dict[str, Any] = {
                "version": 1,
                "run_id": self.run_id,
                "mode": config.mode,
                "phase": "inspect",
                "status": "running",
                "started_at": now(),
                "goal": goal,
                "config": config.to_mapping(),
                "provider": os.getenv("LEANFLOW_NATIVE_PROVIDER", ""),
                "reasoning_effort": os.getenv("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                "jobs": [],
                "changes": [],
                "targets": [str(path.relative_to(self.root)) for path in self.targets],
                "plan_markdown": self._initial_plan(),
                "metrics": {
                    "api_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": None,
                    "plan_refinements": 0,
                    "max_plan_refinements": config.plan_refinements,
                    "total_api_budget": config.total_api_calls,
                    "decompositions": 0,
                },
            }
            self._persist()

        if isinstance(self.verifier, LeanVerifier):
            self.verifier.remaining_time = self._remaining_verification_time
            self.verifier.on_operation = self.progress.call

    def _remaining_verification_time(self) -> float:
        """Enforce the global deadline before starting another deterministic check."""
        self._ensure_active()
        return max(0.01, self.config.wall_time_s - self._elapsed())

    def _initial_plan(self) -> str:
        lines = [
            "# Proof plan",
            "",
            self.goal or "Complete the authorized Lean proof holes.",
            "",
            "## Initial inspection",
            "",
        ]
        lines.extend(
            f"- `{node.name}` in `{node.file}`: {len(node.holes)} hole(s)."
            for node in self.dag.nodes
        )
        lines.extend(
            [
                "",
                "## Strategy",
                "",
                "Preserve all supplied source outside its literal sorry holes. "
                "The controller may add reviewed helper imports only after verifying that original kernel types are unchanged. "
                "Attempt each root directly; use concrete local subproofs when helpful. "
                "Only independently checked proofs count as complete.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _restore(self) -> None:
        """Resume durable progress without granting interrupted jobs a fresh allocation."""
        self.state = json.loads((self.store.directory / "state.json").read_text())
        self.state["resume_phase"] = self.state.get(
            "resume_phase", self.state.get("phase", "inspect")
        )
        self.elapsed_before = float(self.state.get("metrics", {}).get("elapsed_s", 0))
        self.store.inbox_offset = int(self.state.get("inbox_offset", 0))
        saved = dict(self.state["config"])
        saved["allowed_axioms"] = tuple(saved["allowed_axioms"])
        self.config = ProverConfig(**saved)
        saved_dag = self.state["dag"]
        for saved_node in saved_dag.get("nodes", []):
            saved_node["status"] = saved_node.pop("scheduler_status", saved_node["status"])
        self.dag = Dag.from_dict(saved_dag)
        self.dag.validate(self.config.max_nodes)
        saved_targets = [project_path(self.root, path) for path in self.state["targets"]]
        if set(self.targets) != set(saved_targets):
            raise ValueError("resume target scope differs from the saved run")
        self.targets = saved_targets
        checkpoint = self.state.get("source_checkpoint")
        if checkpoint:
            if not re.fullmatch(r"source-checkpoints/[a-f0-9]{64}\.json", checkpoint):
                raise ValueError("Invalid source checkpoint identity")
            checkpoint_path = project_path(
                self.root, str((self.store.directory / checkpoint).relative_to(self.root))
            )
            documents = json.loads(checkpoint_path.read_text())
        else:
            documents = json.loads((self.store.directory / "source.json").read_text())
        self.documents = {key: SourceDocument(**value) for key, value in documents.items()}
        from leanflow_cli.workflows.prover.source_transaction import recover_proof

        recover_proof(self)
        self._assert_sources()
        self.state.pop("error", None)
        self.state.pop("next_step", None)
        self.state.pop("stop_reason", None)
        if self.state.get("proposal_status") in {"proposed", "validating"}:
            self.state.update(
                proposal_status=(
                    "reviewed" if self.state["proposal_status"] == "validating" else "proposed"
                ),
                proposal_critique="Interrupted proposal: validation and any unfinished review must complete before scheduling.",
            )
        for operation in self.state.get("operations", []):
            if operation.get("status") == "running":
                operation.update(
                    status="failed", error="Interrupted before controller resume", finished_at=now()
                )
        self.dag.validate(self.config.max_nodes)
        for job in self.state.get("jobs", []):
            if job.get("status") in {
                "running",
                "queued",
                "provider_error",
                "environment_error",
                "error",
                "interrupted",
                "resume_pending",
            }:
                workspace = Path(job["workspace"])
                ledger_path = workspace.parent / ".runtime" / workspace.name / "request-count.json"
                if ledger_path.is_file():
                    ledger = json.loads(ledger_path.read_text())
                    job["api_calls"] = int(ledger["used"])
                    for field in (
                        "input_tokens",
                        "output_tokens",
                        "cost_usd",
                        "cost_source",
                        "costed_api_calls",
                    ):
                        if field in ledger.get("usage", {}):
                            job[field] = ledger["usage"][field]
                else:
                    job["api_calls"] = int(job["api_budget"])
                if job["role"] == "prover" and job["api_calls"] < job["api_budget"]:
                    self.resume_jobs[job["node_id"]] = job
                    job["status"] = "resume_pending"
                elif job["role"] == "negation" and job.get("negation_assignment"):
                    self.resume_negation_jobs[job["node_id"]] = job
                    job["status"] = "resume_pending"
                elif (
                    job["role"] in {"orchestrator", "review", "research"}
                    and job["api_calls"] < job["api_budget"]
                ):
                    self.resume_role_jobs[(job["role"], job.get("purpose", ""))] = job
                    job["status"] = "resume_pending"
                else:
                    job["status"] = "interrupted"
        self.consumed = sum(int(job.get("api_calls", 0)) for job in self.state.get("jobs", []))
        for node in self.dag.nodes:
            if (
                node.status in {"running", "submitted", "verifying", "integrating"}
                or node.id in self.resume_jobs
            ):
                node.status = (
                    "retry"
                    if node.id in self.resume_jobs or node.attempts <= self.config.max_restarts
                    else "blocked"
                )
        retained_nodes: set[str] = set()
        for job in reversed(self.state.get("jobs", [])):
            if job.get("role") != "prover" or job.get("node_id") in retained_nodes:
                continue
            retained_nodes.add(job["node_id"])
            result_path = Path(job.get("result_path", ""))
            if job.get("accounted") and not job.get("result_processed") and result_path.is_file():
                self._retain_candidate(job, json.loads(result_path.read_text()))
        self.state.update(status="running", phase="resume", terminal=False)
        self.state["metrics"]["api_calls"] = self.consumed
        self._persist()

    def _persist(self) -> None:
        with self.lock, self.progress.lock:
            self._refresh_metrics()
            self.store.write(self.state, self.dag, self.documents, publish=False)
            self.progress.publish()

    def _refresh_metrics(self) -> None:
        jobs = self.state.get("jobs", [])
        metrics = self.state["metrics"]
        for field in ("api_calls", "input_tokens", "output_tokens"):
            metrics[field] = sum(int(job.get(field, 0) or 0) for job in jobs)
        from leanflow_cli.workflows.prover.usage import aggregate_cost

        metrics.update(aggregate_cost(jobs))
        metrics["reserved_api_calls"] = self.reserved
        active_spent = sum(
            max(0, int(job.get("api_calls", 0)) - int(job.get("previous_api_calls", 0)))
            for job in jobs
            if not job.get("accounted") and job.get("status") == "running"
        )
        metrics["remaining_reserved_api_calls"] = max(0, self.reserved - active_spent)
        metrics["available_api_calls"] = max(
            0,
            self.config.total_api_calls
            - metrics["api_calls"]
            - metrics["remaining_reserved_api_calls"],
        )
        metrics.update(
            wall_time_s=self.config.wall_time_s,
            max_decompositions=self.config.max_decompositions,
            max_nodes=self.config.max_nodes,
        )
        metrics["elapsed_s"] = round(self._elapsed(), 3)

    def _elapsed(self) -> float:
        """Count active campaign time cumulatively across process resumes."""
        return self.elapsed_before + time.monotonic() - self.started

    def _assert_sources(self) -> None:
        for document in self.documents.values():
            document.assert_current(self.root)

    def _reserve(self, requested: int) -> int:
        with self.lock:
            self._ensure_active()
            remaining = self.config.total_api_calls - self.consumed - self.reserved
            if remaining <= 0 or self._elapsed() >= self.config.wall_time_s:
                raise BudgetExhausted("campaign API or wall-clock budget exhausted")
            allocation = min(requested, remaining)
            self.reserved += allocation
            return allocation

    def _ensure_active(self) -> None:
        """Stop new controller work immediately after cancellation or the campaign deadline."""
        if self.cancelled.is_set() or self.stopping:
            self.stopping = True
            raise InfrastructureFailure(
                "The prover run was interrupted.",
                status=(
                    "disproved"
                    if self.state.get("disproof")
                    else self.state.get("stop_status", "interrupted")
                ),
            )
        if self._elapsed() >= self.config.wall_time_s:
            self.cancelled.set()
            self.stopping = True
            self.state["stop_status"] = "timeout"
            raise BudgetExhausted("campaign wall-clock budget exhausted", code="campaign_wall_time")

    def _context(self, node: Node | None = None) -> dict[str, Any]:
        from leanflow_cli.workflows.prover.resource_handoff import resource_inventory

        return {
            "run_id": self.run_id,
            "project_root": str(self.root),
            "inbox_offset": self.store.inbox_offset,
            "assignment": node.to_dict() if node else {},
            "dag": self.dag.to_dict(),
            "plan": self.state["plan_markdown"],
            "notes": node.notes if node else "",
            "goal": self.goal,
            "permitted_dependencies": list(node.dependencies) if node else [],
            "resources": resource_inventory(
                self.state["jobs"], project_root=self.root, run_directory=self.store.directory
            ),
        }

    def _new_job(
        self, role: str, *, node: Node | None = None, prompt: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Prepare one reserved job and its retained proof context."""
        from leanflow_cli.workflows.prover.allocation import wait_for_capacity
        from leanflow_cli.workflows.prover.job_controller import new_job

        wait_for_capacity(self, role, node, prompt)
        with self.lock:
            return new_job(self, role, node=node, prompt=prompt)

    def _session_event(self, job: dict[str, Any], kind: str, details: dict[str, Any]) -> None:
        """Publish one worker event through the controller's observer."""
        from leanflow_cli.workflows.prover.job_controller import session_event

        session_event(self, job, kind, details)

    def _invoke(self, job: dict[str, Any], context: dict[str, Any], prompt: str) -> dict[str, Any]:
        """Execute a bounded session with cancellation and wall-clock limits."""
        from leanflow_cli.workflows.prover.job_controller import invoke

        return invoke(self, job, context, prompt)

    def _finish_job(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        """Reconcile final usage against the job's original reservation."""
        from leanflow_cli.workflows.prover.job_controller import finish_job

        finish_job(self, job, result)

    def _run_role(
        self,
        role: str,
        prompt: str,
        *,
        node: Node | None = None,
        context_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one fresh planning, review, research, or negation context."""
        from leanflow_cli.workflows.prover.job_controller import run_role

        return run_role(self, role, prompt, node=node, context_extra=context_extra)

    def _research_plan(
        self, reason: str, *, affected: set[str] | None = None, refinement: bool = False
    ) -> bool:
        """Delegate fresh planning stages while retaining controller mutation authority."""
        from leanflow_cli.workflows.prover.planning_controller import research_plan

        return research_plan(self, reason, affected=affected, refinement=refinement)

    def _change(self, document: SourceDocument, agent_id: str) -> None:
        baseline = self.store.baseline(document)
        change = {
            "path": str(self.root / document.path),
            "baseline_path": str(baseline),
            "status": "added" if document.generated else "modified",
            "pending": False,
            "agent_id": agent_id,
            "job_id": (
                self.state.get("materialization_job_id", "")
                if agent_id == "orchestrator"
                else agent_id
            ),
        }
        prior = next(
            (item for item in self.state["changes"] if item["path"] == change["path"]), None
        )
        if prior:
            prior.update(change)
        else:
            self.state["changes"].append(change)

    def _refresh_locations(self, dag: Dag | None = None) -> None:
        from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text

        for node in (dag or self.dag).nodes:
            for entry in _declaration_line_index_from_text(
                lean_code_mask(self.documents[node.file].render())
            ):
                if entry["name"] == node.name:
                    node.line_start, node.line_end = entry["line"], entry["end_line"]
                    break

    def _prover_prompt(self, node: Node) -> str:
        """Build the focused proof and candidate-submission contract."""
        from leanflow_cli.workflows.prover.job_controller import prover_prompt

        return prover_prompt(self, node)

    def _candidate(
        self, job: dict[str, Any], result: dict[str, Any], node: Node
    ) -> tuple[list[str], dict[str, Any]]:
        report = json_report(str(result.get("final_response", "")))
        candidate = report.get("proofs")
        if not isinstance(candidate, list) and isinstance(report.get("proof"), str):
            candidate = [report["proof"]]
        path = Path(job["workspace"]) / "candidate.txt"
        if (
            not isinstance(candidate, list)
            and path.is_file()
            and not path.is_symlink()
            and len(node.holes) == 1
        ):
            candidate = [path.read_text()]
        if not isinstance(candidate, list):
            holes = list(job.get("scratch_holes", []))
            scratch = Path(job["scratch_path"])
            if scratch.is_file() and not scratch.is_symlink() and job["id"] in self.scratch_before:
                candidate = extract_scratch_replacements(
                    self.scratch_before[job["id"]], read_source(scratch), holes
                )
        if (
            not isinstance(candidate, list)
            or len(candidate) != len(node.holes)
            or not all(isinstance(item, str) and item.strip() for item in candidate)
        ):
            return [], report
        if any(
            sorry_spans(item)
            or re.search(r"\b(?:admit|axiom|unsafe|native_decide)\b", lean_code_mask(item))
            for item in candidate
        ):
            return [], report
        try:
            for proof in candidate:
                validate_hole_replacement(proof)
        except ValueError as error:
            report["notes"] = "Rejected proof replacement: " + str(error)
            return [], report
        return candidate, report

    def _retain_candidate(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        """Preserve a completed current-revision proof for independent zero-model resume."""
        node = self.dag.by_id().get(job.get("node_id", ""))
        if (
            job.get("role") != "prover"
            or node is None
            or node.revision != job.get("node_revision")
            or node.status == "proved"
        ):
            return
        workspace = Path(job["workspace"])
        baseline = workspace.parent / ".runtime" / workspace.name / "source-before.lean"
        if job["id"] not in self.scratch_before and baseline.is_file():
            self.scratch_before[job["id"]] = read_source(baseline)
        candidate, _ = self._candidate(job, result, node)
        if candidate:
            node.candidate = candidate
            node.status = "candidate"
            node.conditional_dependencies = [
                dep for dep in node.dependencies if self.dag.by_id()[dep].status != "proved"
            ]
            self.resume_jobs.pop(node.id, None)

    def _accept(self, node: Node, candidate: list[str], job_id: str) -> bool:
        """Serialize independent source verification and its canonical installation."""
        with self.lock:
            return self._accept_locked(node, candidate, job_id)

    def _accept_locked(self, node: Node, candidate: list[str], job_id: str) -> bool:
        """Verify against current source, then install only the original literal-hole replacements."""
        self._assert_sources()
        document = self.documents[node.file]
        workspace = self.store.directory / "checks"
        workspace.mkdir(exist_ok=True)
        check_path = workspace / f"{node.id}_{node.revision}.lean"
        source = declaration_source(document, node, candidate=candidate)
        write_source(check_path, source)
        cached = self.submission_cache.take(node, submission_key(self, node, source))
        result = self.progress.call(
            "submission_check",
            (
                "Reusing independent check: source unchanged"
                if cached is not None
                else "Independently checking candidate"
            ),
            lambda: cached if cached is not None else self.verifier.check(node, check_path),
            node_id=node.id,
            job_id=job_id,
            file=node.file,
            timeout_s=self.config.timeout_s,
        )
        self.store.event(
            "candidate_checked",
            {
                "node_id": node.id,
                "accepted": bool(result.get("accepted")),
                "result": result,
                "reused_independent_submission": cached is not None,
            },
        )
        if not result.get("accepted"):
            if infrastructure_code(result):
                self._ensure_active()
                raise InfrastructureFailure(
                    "Lean verification environment unavailable: "
                    + str(result.get("error", result)),
                    status="environment_error",
                )
            node.notes += "\nParent Lean gate: " + json.dumps(result, default=str)[-10000:]
            return False
        self._assert_sources()
        previous_document = SourceDocument(**document.to_dict())
        previous = dict(document.replacements)
        for hole, proof in zip(node.holes, candidate, strict=True):
            document.replacements[str(hole)] = proof
        path = self.root / node.file
        from leanflow_cli.workflows.prover.source_transaction import (
            begin_proof,
            recover_proof,
            replace_source,
        )

        transaction = begin_proof(self, node, candidate, previous_document, document.render())
        try:
            if read_source(path) != previous_document.render():
                raise InfrastructureFailure(
                    f"protected source changed before proof installation: {node.file}",
                    status="source_conflict",
                )
            replace_source(path, document.render().encode("utf-8"))
            self._change(document, job_id)
            staged_change = next(
                item for item in self.state["changes"] if item["path"] == str(path)
            )
            staged_change.update(status="staged", pending=True)
            # Original declarations can also be imported by another proof obligation.
            compiled = self.progress.call(
                "proof_integration",
                "Compiling accepted proof into project",
                lambda: self.verifier.compile_module(node.file),
                node_id=node.id,
                job_id=job_id,
                file=node.file,
                timeout_s=self.config.timeout_s,
            )
            if not compiled.get("accepted"):
                self._ensure_active()
                raise InfrastructureFailure(
                    "The independently checked proof could not be compiled into an importable module. Its candidate is retained; restore the Lean build environment and resume. "
                    + str(compiled),
                    status="environment_error",
                )
            self._assert_sources()
        except Exception as error:
            document.replacements = previous
            try:
                recover_proof(self)
            except (ValueError, SourceConflictError) as conflict:
                raise InfrastructureFailure(str(conflict), status="source_conflict") from error
            node.candidate = list(candidate)
            node.status = "candidate"
            node.notes += "\nProof integration paused: " + str(error)
            self._persist()
            if isinstance(error, (InfrastructureFailure, BudgetExhausted)):
                raise
            raise InfrastructureFailure(
                "Checked proof integration paused; candidate retained for resume: " + str(error),
                status="environment_error",
            ) from error
        node.status = "proved"
        node.candidate = []
        node.conditional_dependencies = []
        node.proof_sha256 = digest(document.render())
        self._change(document, job_id)
        self._refresh_locations()
        self.state["plan_markdown"] += f"\n- Independently proved `{node.name}` in `{node.file}`.\n"
        self.state["source_transaction"] = transaction["id"]
        self._persist()
        (self.store.directory / "source-transaction.json").unlink(missing_ok=True)
        return True

    def _handle_result(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        node = self.dag.by_id().get(job["node_id"])
        if node is None or node.revision != job["node_revision"]:
            try:
                self._finish_job(job, result)
            except InfrastructureFailure:
                pass
            job["status"] = "stale"
            self._persist()
            return
        self._finish_job(job, result)
        if self.cancelled.is_set():
            self._retain_candidate(job, result)
            self._ensure_active()
        candidate, report = self._candidate(job, result, node)
        node.notes = str(report.get("notes", result.get("final_response", "")))[-16000:]
        if candidate:
            unresolved = [
                dep for dep in node.dependencies if self.dag.by_id()[dep].status != "proved"
            ]
            if unresolved:
                node.candidate = candidate
                node.conditional_dependencies = unresolved
                node.status = "candidate"
                self._persist()
                return
            if self._accept(node, candidate, job["id"]):
                return

        def partial_work(attempt: dict[str, Any]) -> list[str]:
            """Read concrete hole edits against the controller's immutable job baseline."""
            scratch = Path(attempt["scratch_path"])
            workspace = Path(attempt["workspace"])
            baseline = workspace.parent / ".runtime" / workspace.name / "source-before.lean"
            if not scratch.is_file() or scratch.is_symlink():
                return []
            try:
                before = self.scratch_before.get(attempt["id"])
                if before is None:
                    if not baseline.is_file() or baseline.is_symlink():
                        return []
                    before = read_source(baseline)
                proofs = extract_scratch_replacements(
                    before, read_source(scratch), list(attempt.get("scratch_holes", []))
                )
                if not proofs or len(proofs) != len(node.holes):
                    return []
                normalized = []
                for proof in proofs:
                    validate_hole_replacement(proof)
                    mask = lean_code_mask(proof)
                    if re.search(r"\b(?:admit|axiom|unsafe|native_decide)\b", mask):
                        return []
                    normalized.append(re.sub(r"\s+", "", mask))
                return normalized if all(normalized) else []
            except (OSError, UnicodeError, ValueError):
                return []

        # Reports cannot grant another pass. Retained partial work must also improve on
        # the previous attempt: copying its scratch or changing comments is not progress.
        previous = next(
            (
                item
                for item in reversed(self.state["jobs"])
                if item["id"] != job["id"]
                and item.get("node_id") == node.id
                and item.get("role") == "prover"
                and item.get("node_revision") == node.revision
            ),
            None,
        )
        partial = partial_work(job)
        progress = bool(candidate) or bool(
            partial
            and partial != ["sorry"] * len(node.holes)
            and (previous is None or partial != partial_work(previous))
        )
        if progress and node.attempts <= self.config.max_restarts:
            node.status = "retry"
            self._persist()
            return
        node.status = "blocked"
        self._persist()
        if self.config.mode == "research" and not self.stopping:
            self._recover(node)

    def _recover(self, node: Node) -> None:
        """Diagnose a blocked statement, then refine direction or split its affected branch."""
        resuming_negation = node.id in self.resume_negation_jobs
        if not resuming_negation:
            if (
                node.decompositions >= 1
                or self.state["metrics"]["decompositions"] >= self.config.max_decompositions
            ):
                return
            node.decompositions += 1
            self.state["metrics"]["decompositions"] += 1
        from leanflow_cli.workflows.prover.negation_job import attempt_negation

        report = attempt_negation(self, node)
        affected = self.dag.affected(node.id)
        if report.get("certified"):
            node.status = "false"
            node.notes += "\nExact negation independently verified: " + str(report["evidence_path"])
            if node.original:
                self.state["disproof"] = {"node_id": node.id, **report}
                self.stopping = True
                self.cancelled.set()
                self._persist()
                return
            for related in self.dag.nodes:
                if related.id in affected and related.id != node.id and related.status != "proved":
                    related.status = "pending"
                    related.revision += 1
                    related.conditional_dependencies = []
                    related.notes += "\nA planned prerequisite was refuted; retained candidate requires replanning."
        reason = f"Repair only the branch for {node.name}. Prover report: {node.notes}\nNegation investigation: {report}"
        refined = self._research_plan(
            reason, affected=affected, refinement=report.get("certified") is True
        )
        if refined:
            current = self.dag.by_id().get(node.id)
            if (
                current is not None
                and current.status == "blocked"
                and (
                    current.dependencies != node.dependencies
                    or current.attempts <= self.config.max_restarts
                )
            ):
                # An accepted local proof repair can keep the exact DAG interface.
                # Reopen that obligation without resetting its spent attempts.
                current.status = "retry"
            self._persist()

    def _promote_candidates(self) -> None:
        """Independently promote every newly closed dependency chain to a fixed point."""
        index = self.dag.by_id()
        changed = False
        while True:
            promoted = False
            for node in self.dag.nodes:
                if node.status != "candidate" or any(
                    index[dep].status != "proved" for dep in node.dependencies
                ):
                    continue
                candidate = list(node.candidate)
                node.candidate = []
                changed = True
                if self._accept(node, candidate, "controller"):
                    promoted = True
                else:
                    node.status = (
                        "retry" if node.attempts <= self.config.max_restarts else "blocked"
                    )
            if not promoted:
                break
        if changed:
            self._persist()

    def _messages(self) -> None:
        for message in self.store.messages():
            text = str(message.get("message", message.get("text", "")))[:12000]
            if message.get("agent_id", "orchestrator") == "orchestrator":
                self.state["plan_markdown"] += "\n\n## User guidance\n\n" + text
            self.store.event("user_message", message)
            if str(message.get("action", "")) in {"stop", "pause"}:
                self.stopping = True
                self.state["stop_status"] = "interrupted"
                self.cancelled.set()
        self.state["inbox_offset"] = self.store.inbox_offset

    def run(self) -> dict[str, Any]:
        """Run until verified, explicitly stopped, or a finite campaign limit is reached."""
        self.controller_thread = threading.get_ident()
        status = "blocked"
        resuming = self.state["phase"] == "resume"
        if self.observer is not None:
            self.observer.start(self.state, resumed=self.state["phase"] == "resume")
        self.progress.start()
        try:
            if hasattr(self.verifier, "preflight"):
                self.state["phase"] = "preflight"
                self._persist()
                preflight = self.progress.call(
                    "preflight",
                    "Checking isolated Lean environment",
                    lambda: self.verifier.preflight(self.store.directory / "checks"),
                    timeout_s=self.config.timeout_s,
                )
                self.state["preflight"] = preflight
                if preflight.get("accepted") is not True:
                    self._ensure_active()
                    raise InfrastructureFailure(
                        str(preflight.get("error", preflight)), status="environment_error"
                    )
            if hasattr(self.verifier, "capture_signatures") and any(
                node.original and not node.signature_sha256 for node in self.dag.nodes
            ):
                if any(
                    document.imports or document.replacements or document.generated
                    for document in self.documents.values()
                ):
                    raise InfrastructureFailure(
                        "Original declaration fingerprints are missing after source changes; recover the original baseline before resuming.",
                        status="source_conflict",
                    )
                signatures = self.verifier.capture_signatures(
                    self.dag, self.documents, self.store.directory / "checks" / "signatures"
                )
                if signatures.get("accepted") is not True:
                    self._ensure_active()
                    raise InfrastructureFailure(
                        str(signatures.get("error", signatures)), status="environment_error"
                    )
                self._persist()
            if self.config.mode == "research" and (
                not resuming
                or self.state.get("planning_request")
                or self.state.get("resume_phase")
                in {"inspect", "preflight", "planning", "reviewing"}
            ):
                self._research_plan(
                    "Inspect available sources and design an honest informal proof outline and dependency graph."
                )
            self.state["phase"] = "proving"
            self.state.pop("resume_phase", None)
            for node_id in list(self.resume_negation_jobs):
                node = self.dag.by_id().get(node_id)
                if node is not None:
                    self._recover(node)
            workers = 1 if self.config.mode == "standard" else self.config.parallelism
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="leanflow-prover"
            ) as pool:
                try:
                    next_source_scan = 0.0
                    while True:
                        self._messages()
                        if self.cancelled.is_set():
                            self._ensure_active()
                        # Acceptance and materialization always recheck exact bytes.
                        # Idle polling only needs a coarse scan for outside edits.
                        if time.monotonic() >= next_source_scan:
                            self._assert_sources()
                            next_source_scan = time.monotonic() + 5.0
                        self._promote_candidates()
                        if self.stopping or self._elapsed() >= self.config.wall_time_s:
                            self.stopping = True
                            if self._elapsed() >= self.config.wall_time_s:
                                self.state["stop_status"] = "timeout"
                                self.cancelled.set()
                        active = {job["node_id"] for job in self.pending.values()}
                        if not self.stopping:
                            candidates = (
                                ready_nodes(
                                    self.dag,
                                    order=self.config.search_order,
                                    active=active,
                                    limit=max(0, workers - len(self.pending)),
                                )
                                if len(self.pending) < workers
                                else []
                            )
                            for node in candidates:
                                try:
                                    prompt = self._prover_prompt(node)
                                    job, context = self._new_job("prover", node=node, prompt=prompt)
                                except BudgetExhausted as error:
                                    if not self.pending:
                                        self.stopping = True
                                        self.state["stop_status"] = error.status
                                    break
                                node.status = "running"
                                if not job.get("resumed"):
                                    node.attempts += 1
                                self._persist()
                                future = pool.submit(self._invoke, job, context, prompt)
                                self.pending[future] = job
                                future.add_done_callback(self.completions.put)
                        if not self.pending:
                            break
                        try:
                            completed = self.completions.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        job = self.pending.pop(completed)
                        self._handle_result(job, completed.result())
                        job["result_processed"] = True
                        self._persist()
                except BaseException:
                    self.cancelled.set()
                    raise
            index = self.dag.by_id()
            if all(index[root].status == "proved" for root in self.dag.roots):
                self.state["phase"] = "final_build"
                self._persist()
                self._assert_sources()
                final = self.progress.call(
                    "final_build",
                    "Verifying the complete project",
                    lambda: self.verifier.final(
                        list(
                            dict.fromkeys(
                                [
                                    *self.targets,
                                    *(
                                        self.root / document.path
                                        for document in self.documents.values()
                                        if document.generated
                                    ),
                                ]
                            )
                        )
                    ),
                )
                self.state["verification"] = final
                if not final.get("accepted"):
                    self._ensure_active()
                status = "completed" if final.get("accepted") else "verification_failed"
            elif self.stopping:
                status = (
                    "disproved"
                    if self.state.get("disproof")
                    else self.state.get("stop_status", "budget_exhausted")
                )
            elif self.consumed >= self.config.total_api_calls:
                status = "budget_exhausted"
        except BudgetExhausted as error:
            status = error.status
            self.state["error"] = str(error)
        except KeyboardInterrupt:
            self.cancelled.set()
            status = "interrupted"
        except InfrastructureFailure as error:
            status = error.status
            self.state["error"] = str(error)
        except Exception as error:
            status = "source_conflict" if "protected source changed" in str(error) else "error"
            self.state["error"] = str(error)
        finally:
            self.stopping = True
            for future, job in list(self.pending.items()):
                try:
                    completed_result = future.result()
                    try:
                        self._finish_job(job, completed_result)
                    finally:
                        self._retain_candidate(job, completed_result)
                except Exception as error:
                    self.store.event(
                        "job_cleanup_error", {"job_id": job["id"], "error": str(error)}
                    )
                cleanup_node = self.dag.by_id().get(job["node_id"])
                if cleanup_node is not None and cleanup_node.status == "running":
                    cleanup_node.status = (
                        "retry" if cleanup_node.attempts <= self.config.max_restarts else "blocked"
                    )
            self.pending.clear()
            if hasattr(self.verifier, "close"):
                self.verifier.close(self.store.directory / "checks")
            if status == "source_conflict":
                self.state["next_step"] = (
                    "Compare current source with this run's saved baselines and resolve the conflict before resuming. "
                    "No external source edits were overwritten. Preserve source-transaction.json "
                    "and source checkpoints; resume needs them to recover the interrupted transaction."
                )
            self.progress.close()
            from leanflow_cli.workflows.prover.stop_reason import campaign_stop_reason

            self._refresh_metrics()
            self.state["stop_reason"] = campaign_stop_reason(self, status)
            self.state.update(status=status, phase=status, finished_at=now(), terminal=True)
            self._persist()
            if self.observer is not None:
                self.observer.finish(self.state)
        return self.state


def main() -> int:
    """Keep the established module launch path through the dedicated entrypoint."""
    from leanflow_cli.workflows.prover.entrypoint import main as launch

    return launch()


def clone_resume_run(root: Path, previous: str, current: str) -> None:
    """Preserve the resume helper import while entrypoint owns execution lineage."""
    from leanflow_cli.workflows.prover.entrypoint import clone_resume_run as clone

    clone(root, previous, current)


if __name__ == "__main__":
    raise SystemExit(main())
