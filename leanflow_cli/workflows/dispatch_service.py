"""Tracked, lineage-addressed job dispatch (Phase 3, specs Part II §4).

One shared lifecycle for every dispatch level: propose → deploy → running →
{done | failed | stuck | killed}, persisted in the ``dispatch_ledger`` key of
``summary.json`` and mirrored as ``dispatch-job`` activity events. Every
ledger mutation runs inside a single read-validate-write TRANSACTION (an
in-process lock plus a checked-and-retried cross-process file lock with a
per-process/thread owner id), and every state change is applied against the
PERSISTED entry — a deploy that lost a race to ``kill``/``reconcile`` keeps
the terminal verdict instead of resurrecting the job.

Jobs carry independent budgets (never the prover's shared iteration budget —
the delegate backend passes ``isolate_budget=True``) and dotted lineage ids
``<root>.<role>.<tag>-<seq>`` whose ancestors may list, track, and kill
their descendants (owner N3). ``deploy`` is sync-blocking in v1 (cap via
LEANFLOW_DISPATCH_MAX_CONCURRENT); ``join``/``poll`` are the seams the async
promotion lands behind (research-run experience is the trigger, §4.2).

Correctness invariants: deliverables are consumed once and never as raw
transcripts; prover-job disk edits are re-verified by the PARENT's
deterministic checker before any graph effect; ``reconcile`` favors agent
evidence over ledger optimism — a live pid is live evidence, and a job with
no evidence is only ``stuck`` after the two-clause patience test — so a job
can never be silently lost (N1: ``open_jobs()`` is the loud audit).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.runtime.file_locks import acquire_file_lock, release_file_lock
from leanflow_cli.workflows.dispatch_models import (
    JobSpec,
    LedgerEntry,
    descendants,
    is_ancestor,
    next_job_id,
)
from leanflow_cli.workflows.workflow_json_io import json_write_lock, read_json_file
from leanflow_cli.workflows.workflow_state import (
    _process_seems_alive,
    append_workflow_activity,
    summarize_workflow_agents,
    terminate_workflow_agent,
    terminate_workflow_agent_descendants,
)
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

logger = logging.getLogger(__name__)

_LEDGER_KEY = "dispatch_ledger"

# Patience policy (specs §4): stuck ONLY when BOTH the wall clock is well past
# the declared budget AND the activity stream has gone quiet — the second
# clause protects a long Lake build whose events keep the stream fresh.
PATIENCE_WALL_CLOCK_FACTOR = 1.5
PATIENCE_MIN_QUIET_S = 600
PATIENCE_QUIET_FACTOR = 0.25


def dispatch_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_DISPATCH_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def dispatch_max_concurrent() -> int:
    try:
        value = int(str(os.getenv("LEANFLOW_DISPATCH_MAX_CONCURRENT", "") or "").strip() or 3)
    except ValueError:
        value = 3
    return max(1, value)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def patience_exceeded(
    *, started_at: str, wall_clock_s: int, now: datetime, last_event_age_s: float | None
) -> bool:
    """The two-clause stuck test over a running entry's declared budget."""
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    over_wall = (now - started).total_seconds() > PATIENCE_WALL_CLOCK_FACTOR * wall_clock_s
    quiet_floor = max(PATIENCE_MIN_QUIET_S, PATIENCE_QUIET_FACTOR * wall_clock_s)
    gone_quiet = last_event_age_s is None or last_event_age_s > quiet_floor
    return over_wall and gone_quiet


class DispatchService:
    """The single dispatch authority for one runner process (sync v1)."""

    def __init__(self, *, parent_agent: Any = None, root_job_id: str = "", cap: int = 0):
        self._parent_agent = parent_agent
        self.root_job_id = root_job_id or "run"
        self._cap = cap or dispatch_max_concurrent()

    # ----- ledger transactions -------------------------------------------

    def _summary_path(self):
        return workflow_state_root() / "summary.json"

    def _transaction(
        self,
        mutate: Callable[[list[dict[str, Any]]], tuple[Any, list[LedgerEntry]]],
    ) -> Any:
        """Run one read-validate-write over the whole ledger atomically.

        ``mutate`` edits the raw ledger list in place and returns
        ``(outcome, entries_to_announce)``; validation raising inside the
        transaction aborts the write. Activity events are emitted after the
        commit, outside the lock.
        """
        path = self._summary_path()
        with json_write_lock(path):
            summary = read_json_file(path)
            ledger = [
                dict(raw) for raw in (summary.get(_LEDGER_KEY) or []) if isinstance(raw, Mapping)
            ]
            outcome, announcements = mutate(ledger)
            summary[_LEDGER_KEY] = ledger
            atomic_json_write(path, summary, sort_keys=True)
        for entry in announcements:
            self._announce(entry)
        return outcome

    @staticmethod
    def _find(ledger: list[dict[str, Any]], job_id: str) -> int:
        for index, raw in enumerate(ledger):
            if dict(raw.get("spec") or {}).get("job_id") == job_id:
                return index
        return -1

    def _announce(self, entry: LedgerEntry) -> None:
        append_workflow_activity(
            "dispatch-job",
            f"Dispatch job {entry.spec.job_id} -> {entry.state}",
            job_id=entry.spec.job_id,
            archetype=entry.spec.archetype,
            state=entry.state,
            requester_role=entry.spec.requester_role,
            agent_session_ids=list(entry.agent_session_ids),
            notes=entry.notes,
        )

    def _save_entry(self, entry: LedgerEntry) -> LedgerEntry:
        """Raw upsert (no transition check) — seeding/tests/backends only."""

        def mutate(ledger: list[dict[str, Any]]):
            payload = entry.to_mapping()
            index = self._find(ledger, entry.spec.job_id)
            if index >= 0:
                ledger[index] = payload
            else:
                ledger.append(payload)
            return entry, [entry]

        return self._transaction(mutate)

    def _transition(self, job_id: str, target: str, **changes: Any) -> LedgerEntry:
        """State change applied against the PERSISTED entry (stale-proof)."""

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            updated = current.with_state(target, **changes)
            ledger[index] = updated.to_mapping()
            return updated, [updated]

        return self._transaction(mutate)

    def _load_ledger(self) -> list[LedgerEntry]:
        summary = read_json_file(self._summary_path())
        return [
            LedgerEntry.from_mapping(raw)
            for raw in (summary.get(_LEDGER_KEY) or [])
            if isinstance(raw, Mapping)
        ]

    def _entry(self, job_id: str) -> LedgerEntry:
        for entry in self._load_ledger():
            if entry.spec.job_id == job_id:
                return entry
        raise KeyError(f"unknown dispatch job {job_id!r}")

    # ----- lifecycle -----------------------------------------------------

    def mint_job_id(self, archetype: str, *, role: str, parent_job_id: str = "") -> str:
        """Mint the next lineage id; the role becomes a path segment (N3).

        Grammar ``<root>.<role-path>.<tag>-<seq>``: a top-level dispatch by
        the planner mints ``<root>.planner.np-001``; nested dispatch passes
        the requesting JOB's id as ``parent_job_id`` to extend the chain.
        """
        parent = parent_job_id.strip() or f"{self.root_job_id}.{role}"
        existing = [entry.spec.job_id for entry in self._load_ledger()]
        return next_job_id(existing, parent, archetype)

    def propose(self, spec: JobSpec) -> LedgerEntry:
        """Validate and persist state='proposed' (always allowed — dark-plannable)."""
        problems = spec.validate()
        expected_parent = spec.job_id.rpartition(".")[0]
        if spec.parent_job_id and spec.parent_job_id != expected_parent:
            problems.append(
                f"parent_job_id {spec.parent_job_id!r} is not the direct parent "
                f"of {spec.job_id!r} (lineage must be a dotted chain)"
            )
        if problems:
            raise ValueError("; ".join(problems))
        entry = LedgerEntry(spec=spec, created_at=_now_iso())

        def mutate(ledger: list[dict[str, Any]]):
            if self._find(ledger, spec.job_id) >= 0:
                raise ValueError(f"job_id {spec.job_id!r} already exists")
            ledger.append(entry.to_mapping())
            return entry, [entry]

        return self._transaction(mutate)

    def deploy(self, job_id: str) -> LedgerEntry:
        """Sync v1: run the job to completion via its archetype backend."""
        if not dispatch_enabled():
            raise RuntimeError("dispatch is disabled (LEANFLOW_DISPATCH_ENABLED is off)")

        def start(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            in_flight = sum(
                1 for raw in ledger if str(raw.get("state", "")) in {"deployed", "running"}
            )
            if in_flight >= self._cap:
                raise RuntimeError(f"dispatch cap reached ({in_flight}/{self._cap} jobs in flight)")
            current = LedgerEntry.from_mapping(ledger[index])
            deployed = current.with_state("deployed")
            running = deployed.with_state("running", started_at=_now_iso())
            ledger[index] = running.to_mapping()
            return running, [deployed, running]

        entry = self._transaction(start)
        try:
            result = self._run_backend(entry.spec)
        except Exception as exc:
            logger.debug("dispatch backend failed", exc_info=True)
            final_state = "failed"
            changes: dict[str, Any] = {
                "finished_at": _now_iso(),
                "notes": f"backend error: {str(exc)[:300]}",
            }
        else:
            status = str(result.get("status", "") or "done")
            final_state = "done" if status in {"done", "ok", "success"} else "failed"
            changes = {"finished_at": _now_iso(), "result": dict(result)}
        try:
            return self._transition(job_id, final_state, **changes)
        except ValueError:
            # Lost the race to kill/reconcile while the backend ran: the
            # persisted terminal verdict wins; never resurrect the job.
            persisted = self._entry(job_id)
            logger.debug(
                "dispatch job %s finished as %s but ledger says %s; keeping the ledger",
                job_id,
                final_state,
                persisted.state,
            )
            return persisted

    def join(self, job_id: str, timeout_s: int | None = None) -> LedgerEntry:
        """Trivial in sync v1 (deploy blocks); the async seam lands here later."""
        return self._entry(job_id)

    def poll(self, job_id: str) -> dict[str, Any]:
        """Reconciled status snapshot for one job."""
        self.reconcile()
        entry = self._entry(job_id)
        return {
            "job_id": job_id,
            "state": entry.state,
            "archetype": entry.spec.archetype,
            "agent_session_ids": list(entry.agent_session_ids),
            "started_at": entry.started_at,
            "finished_at": entry.finished_at,
            "consumed": entry.consumed,
            "notes": entry.notes,
        }

    def kill(self, job_id: str, *, requester_job_id: str) -> dict[str, Any]:
        """Ancestor-gated kill (owner N3): only ancestors, the root, or a human."""
        allowed = requester_job_id in {self.root_job_id, "human"} or is_ancestor(
            requester_job_id, job_id
        )
        if not allowed:
            raise PermissionError(
                f"{requester_job_id!r} is not an ancestor of {job_id!r} and may not kill it"
            )
        entry = self._entry(job_id)
        if entry.is_terminal() and entry.state != "stuck":
            return {"job_id": job_id, "state": entry.state, "killed": False}
        details: dict[str, Any] = {}
        if entry.run_id or entry.process_id:
            for session_id in entry.agent_session_ids:
                details.setdefault("descendants", []).append(
                    terminate_workflow_agent_descendants(session_id)
                )
                details.setdefault("agents", []).append(terminate_workflow_agent(session_id))
        elif self._parent_agent is not None:
            # Delegate-backend children are threads: v1 kill is cooperative —
            # the parent-wide interrupt reaches ALL children (documented).
            try:
                self._parent_agent.interrupt("dispatch kill: " + job_id)
                details["parent_interrupt"] = True
            except Exception:
                details["parent_interrupt"] = False
        try:
            entry = self._transition(
                job_id,
                "killed",
                finished_at=_now_iso(),
                notes=f"killed by {requester_job_id}",
            )
        except ValueError:
            entry = self._entry(job_id)
            return {"job_id": job_id, "state": entry.state, "killed": False, **details}
        return {"job_id": job_id, "state": entry.state, "killed": True, **details}

    def consume(self, job_id: str) -> dict[str, Any]:
        """One-way result hand-off: bounded deliverable, never a raw transcript."""

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.consumed:
                raise RuntimeError(f"job {job_id} result was already consumed")
            if current.state != "done":
                raise RuntimeError(f"job {job_id} is {current.state}, not done")
            updated = LedgerEntry.from_mapping({**current.to_mapping(), "consumed": True})
            ledger[index] = updated.to_mapping()
            return dict(current.result), [updated]

        result = self._transaction(mutate)
        return {
            "deliverable": result.get("deliverable") or {},
            "artifact_paths": list(result.get("artifact_paths") or []),
            "plan_delta": list(result.get("plan_delta") or []),
        }

    def list_descendants(self, job_id: str) -> list[LedgerEntry]:
        ledger = self._load_ledger()
        ids = set(descendants([entry.spec.job_id for entry in ledger], job_id))
        return [entry for entry in ledger if entry.spec.job_id in ids]

    def open_jobs(self) -> list[LedgerEntry]:
        """Non-terminal entries — the never-silently-lost audit (N1)."""
        return [entry for entry in self._load_ledger() if not entry.is_terminal()]

    # ----- reconciliation --------------------------------------------------

    def reconcile(self) -> list[LedgerEntry]:
        """Cross-check running entries against agent evidence; never lose a job.

        A live pid is live evidence (skip further checks). Dead agents with
        no result fail immediately (evidence of death). Missing evidence is
        only ``stuck`` after the two-clause patience test — a recently
        started or still-chatty job stays running.
        """
        # Real summaries key agents by "agent_id" (workflow_state.py); accept
        # the session-id spelling too so either evidence shape reconciles.
        agents: dict[str, dict[str, Any]] = {}
        for agent in summarize_workflow_agents():
            for key in ("agent_id", "agent_session_id"):
                identifier = str(agent.get(key, "") or "")
                if identifier:
                    agents[identifier] = dict(agent)
        now = datetime.now(UTC)
        updated: list[LedgerEntry] = []
        for entry in self._load_ledger():
            if entry.state != "running":
                updated.append(entry)
                continue
            if entry.process_id:
                if _process_seems_alive(entry.process_id):
                    updated.append(entry)
                    continue
                entry = self._transition(
                    entry.spec.job_id,
                    "failed",
                    finished_at=_now_iso(),
                    notes="agent process died",
                )
                updated.append(entry)
                continue
            statuses = [
                str(agents.get(session_id, {}).get("status", "") or "missing")
                for session_id in entry.agent_session_ids
            ]
            if (
                statuses
                and all(status in {"dead", "exited"} for status in statuses)
                and not entry.result
            ):
                entry = self._transition(
                    entry.spec.job_id,
                    "failed",
                    finished_at=_now_iso(),
                    notes="agent died without a result",
                )
            elif (not statuses or all(status == "missing" for status in statuses)) and (
                patience_exceeded(
                    started_at=entry.started_at,
                    wall_clock_s=entry.spec.budget.wall_clock_s,
                    now=now,
                    last_event_age_s=None,
                )
            ):
                entry = self._transition(
                    entry.spec.job_id,
                    "stuck",
                    finished_at=_now_iso(),
                    notes="no agent evidence past the patience window",
                )
            updated.append(entry)
        return updated

    # ----- backends ---------------------------------------------------------

    def _run_backend(self, spec: JobSpec) -> dict[str, Any]:
        if spec.archetype == "prover" and not spec.scope.get("scratch_only"):
            return self._run_spawn_job(spec)
        return self._run_delegate_job(spec)

    def _run_delegate_job(self, spec: JobSpec) -> dict[str, Any]:
        """Shapes B/empirical/deep-search/negation via delegate_task (isolated budget)."""
        from tools.implementations.delegate_tool import (  # lazy, like lean_worker_dispatch
            delegate_task,
        )

        if self._parent_agent is None:
            raise RuntimeError("delegate backend requires a parent agent")
        locks: list[str] = [str(p) for p in (spec.scope.get("file_locks") or [])]
        owner_id = f"dispatch:{spec.job_id}"
        acquired: list[str] = []
        try:
            for path in locks:
                lock = acquire_file_lock(
                    path,
                    owner_id=owner_id,
                    purpose=f"dispatch:{spec.archetype}",
                    ttl_seconds=spec.budget.wall_clock_s,
                )
                if not lock.get("success"):
                    raise RuntimeError(f"file lock unavailable for {path}")
                acquired.append(path)
            context_lines = [
                f"Dispatch job {spec.job_id} ({spec.archetype}; requester {spec.requester_role}).",
                f"Deliverable schema: {spec.deliverable}. Report findings as compact JSON.",
                f"Inputs: {json.dumps(spec.inputs, ensure_ascii=False, sort_keys=True)[:1500]}",
            ]
            raw = delegate_task(
                goal=spec.objective,
                context="\n".join(context_lines),
                toolsets=list(spec.toolsets) or None,
                max_iterations=spec.budget.api_steps,
                parent_agent=self._parent_agent,
                isolate_budget=True,
            )
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            results = list(payload.get("results") or [])
            first = dict(results[0]) if results else {}
            status = str(first.get("status", "") or payload.get("error", "error"))
            return {
                "status": "done" if status in {"ok", "success", "completed"} else status,
                "deliverable": {"summary": str(first.get("summary", "") or "")[:4000]},
                "artifact_paths": [],
                "plan_delta": [],
                "api_calls": first.get("api_calls", 0),
            }
        finally:
            for path in acquired:
                try:
                    release_file_lock(path, owner_id=owner_id)
                except Exception:
                    logger.debug("dispatch lock release failed", exc_info=True)

    def _run_spawn_job(self, spec: JobSpec) -> dict[str, Any]:
        """Prover shape A: a nested file-scoped /prove (Phase 5 §5.7).

        prover_jobs owns the whole contract — hygienic child env, stub-file
        lock, synchronous wall-clock wait with kill escalation, and the
        parent-side kernel gate over the stub declarations.
        """
        from leanflow_cli.workflows import prover_jobs  # lazy: pulls workflow.py

        return prover_jobs.launch_stub_prove_job(spec)
