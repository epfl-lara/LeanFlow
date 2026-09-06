"""Drive standard and research proving with one bounded, authoritative controller."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Dag, Node, digest
from leanflow_cli.workflows.prover.planning import json_report
from leanflow_cli.workflows.prover.scheduler import ready_nodes
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    declaration_source,
    discover,
    extract_scratch_replacements,
    lean_code_mask,
    project_path,
    read_source,
    sorry_spans,
    write_source,
)
from leanflow_cli.workflows.prover.store import RunStore, now
from leanflow_cli.workflows.prover.verification import LeanVerifier

Session = Callable[..., dict[str, Any]]


class BudgetExhausted(RuntimeError):
    """The campaign cannot reserve another request allocation."""


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
                "Attempt each root directly; use concrete local subproofs when helpful. "
                "Only independently checked proofs count as complete.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _restore(self) -> None:
        """Resume durable progress without granting interrupted jobs a fresh allocation."""
        self.state = json.loads((self.store.directory / "state.json").read_text())
        self.elapsed_before = float(self.state.get("metrics", {}).get("elapsed_s", 0))
        self.store.inbox_offset = int(self.state.get("inbox_offset", 0))
        saved = dict(self.state["config"])
        saved["allowed_axioms"] = tuple(saved["allowed_axioms"])
        self.config = ProverConfig(**saved)
        self.dag = Dag.from_dict(self.state["dag"])
        documents = json.loads((self.store.directory / "source.json").read_text())
        self.documents = {key: SourceDocument(**value) for key, value in documents.items()}
        from leanflow_cli.workflows.prover.source_transaction import recover_proof

        recover_proof(self)
        self._assert_sources()
        self.state.pop("error", None)
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
                else:
                    job["api_calls"] = int(job["api_budget"])
                if job["role"] == "prover" and job["api_calls"] < job["api_budget"]:
                    self.resume_jobs[job["node_id"]] = job
                    job["status"] = "resume_pending"
                elif job["role"] == "negation" and job.get("negation_assignment"):
                    self.resume_negation_jobs[job["node_id"]] = job
                    job["status"] = "resume_pending"
                else:
                    job["status"] = "interrupted"
        self.consumed = sum(int(job.get("api_calls", 0)) for job in self.state.get("jobs", []))
        for node in self.dag.nodes:
            if node.status == "running" or node.id in self.resume_jobs:
                node.status = (
                    "retry"
                    if node.id in self.resume_jobs or node.attempts <= self.config.max_restarts
                    else "blocked"
                )
        self.state.update(status="running", phase="resume", terminal=False)
        self.state["metrics"]["api_calls"] = self.consumed
        self._persist()

    def _persist(self) -> None:
        with self.lock:
            self._refresh_metrics()
            self.store.write(self.state, self.dag, self.documents)
            if self.observer is not None:
                self.observer.publish(self.state)

    def _refresh_metrics(self) -> None:
        jobs = self.state.get("jobs", [])
        metrics = self.state["metrics"]
        for field in ("api_calls", "input_tokens", "output_tokens"):
            metrics[field] = sum(int(job.get(field, 0) or 0) for job in jobs)
        known = [float(job["cost_usd"]) for job in jobs if job.get("cost_usd") is not None]
        metrics["cost_usd"] = sum(known) if known else None
        metrics["cost_complete"] = all(job.get("cost_usd") is not None for job in jobs)
        metrics["reserved_api_calls"] = self.reserved
        metrics["elapsed_s"] = round(self._elapsed(), 3)

    def _elapsed(self) -> float:
        """Count active campaign time cumulatively across process resumes."""
        return self.elapsed_before + time.monotonic() - self.started

    def _assert_sources(self) -> None:
        for document in self.documents.values():
            document.assert_current(self.root)

    def _reserve(self, requested: int) -> int:
        with self.lock:
            remaining = self.config.total_api_calls - self.consumed - self.reserved
            if remaining <= 0 or self._elapsed() >= self.config.wall_time_s:
                raise BudgetExhausted("campaign API or wall-clock budget exhausted")
            allocation = min(requested, remaining)
            self.reserved += allocation
            return allocation

    def _context(self, node: Node | None = None) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "assignment": node.to_dict() if node else {},
            "dag": self.dag.to_dict(),
            "plan": self.state["plan_markdown"],
            "notes": node.notes if node else "",
            "goal": self.goal,
            "permitted_dependencies": list(node.dependencies) if node else [],
        }

    def _new_job(
        self, role: str, *, node: Node | None = None, prompt: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Prepare one reserved job and its retained proof context."""
        from leanflow_cli.workflows.prover.job_controller import new_job

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
            "agent_id": agent_id,
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
            if scratch.is_file() and not scratch.is_symlink():
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
        return candidate, report

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
        write_source(check_path, declaration_source(document, node, candidate=candidate))
        result = self.verifier.check(node, check_path)
        self.store.event(
            "candidate_checked",
            {"node_id": node.id, "accepted": bool(result.get("accepted")), "result": result},
        )
        if not result.get("accepted"):
            if result.get("error_code") in {
                "isolation_unavailable",
                "check_setup_failed",
                "lean_interact_start_failed",
                "lean_probe_unavailable",
                "local_repl_missing",
            }:
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
        from leanflow_cli.workflows.prover.source_transaction import begin_proof

        transaction = begin_proof(self, node, candidate, previous_document, document.render())
        try:
            temporary = path.with_suffix(".leanflow-tmp")
            write_source(temporary, document.render())
            temporary.replace(path)
            # Generated dependencies must have fresh importable artifacts before promotion.
            if document.generated:
                compiled = self.verifier.compile_module(node.file)
                if not compiled.get("accepted"):
                    raise InfrastructureFailure(
                        "The independently checked proof could not be compiled into an importable helper module. Its candidate is retained; restore the Lean build environment and resume. "
                        + str(compiled),
                        status="environment_error",
                    )
        except Exception as error:
            document.replacements = previous
            write_source(path, document.render())
            (self.store.directory / "source-transaction.json").unlink(missing_ok=True)
            node.candidate = list(candidate)
            node.status = "candidate"
            node.notes += "\nProof integration paused: " + str(error)
            self._persist()
            if isinstance(error, InfrastructureFailure):
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
        self._finish_job(job, result)
        node = self.dag.by_id().get(job["node_id"])
        if node is None or node.revision != job["node_revision"]:
            job["status"] = "stale"
            self._persist()
            return
        candidate, report = self._candidate(job, result, node)
        previous_notes = node.notes
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
        # Renewals are separate jobs, with a retained concrete handoff and a finite total cap.
        progress = bool(
            candidate
            or report.get("promising") is True
            or (node.notes and node.notes != previous_notes)
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
                and current.dependencies != node.dependencies
            ):
                current.status = "pending"
            self._persist()

    def _promote_candidates(self) -> None:
        index = self.dag.by_id()
        for node in self.dag.nodes:
            if node.status != "candidate" or any(
                index[dep].status != "proved" for dep in node.dependencies
            ):
                continue
            candidate = list(node.candidate)
            node.candidate = []
            if not self._accept(node, candidate, "controller"):
                node.status = "retry" if node.attempts <= self.config.max_restarts else "blocked"
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
        status = "blocked"
        resuming = self.state["phase"] == "resume"
        if self.observer is not None:
            self.observer.start(self.state, resumed=self.state["phase"] == "resume")
        try:
            if hasattr(self.verifier, "preflight"):
                self.state["phase"] = "preflight"
                self._persist()
                preflight = self.verifier.preflight(self.store.directory / "checks")
                self.state["preflight"] = preflight
                if preflight.get("accepted") is not True:
                    raise InfrastructureFailure(
                        str(preflight.get("error", preflight)), status="environment_error"
                    )
            if self.config.mode == "research" and not resuming:
                self._research_plan(
                    "Inspect available sources and design an honest informal proof outline and dependency graph."
                )
            self.state["phase"] = "proving"
            for node_id in list(self.resume_negation_jobs):
                node = self.dag.by_id().get(node_id)
                if node is not None:
                    self._recover(node)
            workers = 1 if self.config.mode == "standard" else self.config.parallelism
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="leanflow-prover"
            ) as pool:
                try:
                    while True:
                        self._messages()
                        self._assert_sources()
                        self._promote_candidates()
                        if self.stopping or self._elapsed() >= self.config.wall_time_s:
                            self.stopping = True
                            if self._elapsed() >= self.config.wall_time_s:
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
                                except BudgetExhausted:
                                    self.stopping = True
                                    break
                                node.status = "running"
                                if not job.get("resumed"):
                                    node.attempts += 1
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
                        self._persist()
                except BaseException:
                    self.cancelled.set()
                    raise
            index = self.dag.by_id()
            if all(index[root].status == "proved" for root in self.dag.roots):
                self.state["phase"] = "verifying"
                self._assert_sources()
                final = self.verifier.final(
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
                )
                self.state["verification"] = final
                status = "completed" if final.get("accepted") else "verification_failed"
            elif self.stopping:
                status = (
                    "disproved"
                    if self.state.get("disproof")
                    else self.state.get("stop_status", "budget_exhausted")
                )
            elif any(
                job.get("status") in {"budget_exhausted", "timeout"} for job in self.state["jobs"]
            ):
                status = "budget_exhausted"
        except BudgetExhausted as error:
            status = "budget_exhausted"
            self.state["error"] = str(error)
        except KeyboardInterrupt:
            self.cancelled.set()
            status = "interrupted"
        except InfrastructureFailure as error:
            status = error.status
            self.state["error"] = str(error)
        except Exception as error:
            status = "error"
            self.state["error"] = str(error)
        finally:
            self.stopping = True
            for future, job in list(self.pending.items()):
                try:
                    self._finish_job(job, future.result())
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
