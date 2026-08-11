"""Run dispatched prover jobs as nested file-scoped ``/prove`` workflows.

The dispatch service's spawn backend: a stub file is discharged by a full
nested native run (``spawn_workflow`` subprocess — the queue, the manager
gate, the retry ladder all engage because a file argument makes the child
file-scoped), synchronously awaited under the job's wall clock, then the
PARENT runs the kernel gate itself over the stub declarations and
reconciles the graph. Results flow one way — from the child's on-disk
artifacts through the parent's own gate — never from the child transcript.

Env hygiene (the child inherits ``os.environ`` wholesale, so the wrapper
must neutralize parent-run state): fresh run id (blank => the child mints
its own), ``LEANFLOW_WORKFLOW_PARENT_RUN_ID`` for the lineage edge,
``LEANFLOW_DISPATCH_JOB_ID``/``LEANFLOW_JOB_LINEAGE``, the job budget as
``AGENT_MAX_TURNS``, a blanked ``LEANFLOW_NATIVE_RUNNER_OWNER`` (the
child's exit must never release the parent's file locks), and blanked
``LEANFLOW_FORMALIZATION_*`` (document gates must not trip in the child).

Concurrency notes (sync v1): the parent thread blocks in ``deploy`` while
the child runs, so shared per-project state (live_status.json, the
plan-state files behind their flock + revision machinery) is written by
one side at a time by construction. The stub file itself is held under a
``dispatch:{job_id}`` file lock for the child's lifetime.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.runtime.file_locks import acquire_file_lock, release_file_lock
from leanflow_cli.workflows import plan_state, research_mode
from leanflow_cli.workflows.dispatch_models import JobSpec

logger = logging.getLogger(__name__)

#: Wall-clock default for one nested prove job (seconds).
DEFAULT_PROVER_JOB_WALL_CLOCK_S = 3600

#: Stub-lock lease slack past the wall clock: the child may live through the
#: kill escalation (<=50s) and the parent still holds the file through the
#: per-declaration gate checks — the lease must not lapse mid-way (leases
#: are expiry-cleaned on acquisition, so a lapsed lock is takeable).
LOCK_TTL_SLACK_S = 600

#: Env families that must never leak from a parent native run into the job.
_SCRUBBED_ENV_PREFIXES = ("LEANFLOW_FORMALIZATION_",)


def prover_job_wall_clock_s() -> int:
    try:
        value = int(
            os.getenv("LEANFLOW_PROVER_JOB_WALL_CLOCK_S", "") or DEFAULT_PROVER_JOB_WALL_CLOCK_S
        )
    except ValueError:
        value = DEFAULT_PROVER_JOB_WALL_CLOCK_S
    return max(60, value)


def _prover_light_model() -> str:
    """``model.prover_light`` config tier ('' = use the main model)."""
    try:
        from leanflow_cli.config import load_config

        model_cfg = load_config().get("model")
        if isinstance(model_cfg, Mapping):
            return str(model_cfg.get("prover_light", "") or "").strip()
    except Exception:
        logger.debug("prover_light tier read failed", exc_info=True)
    return ""


def build_job_env(spec: JobSpec) -> dict[str, str]:
    """The hygienic ``extra_env`` for ``spawn_workflow`` (merged last, wins)."""
    env = {
        # Blank => workflow_state mints a fresh run id in the child; the
        # parent's id becomes the lineage edge instead of being inherited.
        "LEANFLOW_WORKFLOW_RUN_ID": "",
        "LEANFLOW_WORKFLOW_PARENT_RUN_ID": os.getenv("LEANFLOW_WORKFLOW_RUN_ID", ""),
        "LEANFLOW_DISPATCH_JOB_ID": spec.job_id,
        "LEANFLOW_JOB_LINEAGE": spec.job_id,
        "AGENT_MAX_TURNS": str(
            research_mode.scaled_prover_job_turns(max(1, spec.budget.api_steps))
        ),
        # The child's exit path releases locks by owner id — the parent's
        # owner must not leak or the child exit frees the parent's locks.
        "LEANFLOW_NATIVE_RUNNER_OWNER": "",
    }
    light_model = _prover_light_model()
    if light_model:
        # Stub grinding rides the light tier (models.prover_light); the
        # extra_env merge is last, so this wins over the parent's model.
        env["LEANFLOW_NATIVE_MODEL"] = light_model
    for key in os.environ:
        if key.startswith(_SCRUBBED_ENV_PREFIXES):
            env[key] = ""  # _read_text_env treats blank as unset
    return env


@dataclass(frozen=True)
class ProverJobOutcome:
    status: str  # done | failed | timeout
    exit_code: int | None
    process_id: int
    decl_verdicts: dict[str, str]  # name -> proved | sorry | error | missing
    notes: str = ""

    @property
    def proved(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, v in self.decl_verdicts.items() if v == "proved"))

    def to_result(self) -> dict[str, Any]:
        """The dispatch backend result contract (deploy: done|* -> ledger)."""
        return {
            "status": self.status,
            "deliverable": {
                "kind": "prove_outcome",
                "exit_code": self.exit_code,
                "process_id": self.process_id,
                "decl_verdicts": dict(self.decl_verdicts),
                "proved": list(self.proved),
                "notes": self.notes,
            },
            "artifact_paths": [],
            "plan_delta": [],
        }


def decl_verdicts(
    stub_file: str, decl_names: Sequence[str], *, project_root: str
) -> dict[str, str]:
    """The parent-side kernel gate over the job's declarations.

    ``proved`` requires ALL of: the declaration is present on disk, its
    comment-stripped body carries no ``sorry`` token (word-boundary regex —
    ``sorry)`` counts), and a fresh in-place
    ``lean_incremental_check(action="check_target")`` returns ``ok`` —
    LeanProbe's ok means elaborated with NO errors and NO sorry, so even a
    macro-hidden sorry the text scan misses fails the gate. Anything weaker
    is ``sorry``/``error``/``missing`` — never trust the child's account.
    """
    from leanflow_cli.lean.lean_incremental import lean_incremental_check

    path = (
        Path(project_root or ".") / stub_file if not os.path.isabs(stub_file) else Path(stub_file)
    )
    verdicts: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {str(name): "missing" for name in decl_names}
    lines = text.splitlines()
    index = {
        str(entry.get("name", "")): entry
        for entry in _declaration_line_index_from_text(text)
        if isinstance(entry, Mapping)
    }
    for name in (str(n) for n in decl_names):
        entry = index.get(name)
        if not entry:
            verdicts[name] = "missing"
            continue
        start = max(0, int(entry.get("line", 1)) - 1)
        end = int(entry.get("end_line", entry.get("line", 1)))
        body = _strip_lean_comments_and_strings("\n".join(lines[start:end]))
        if re.search(r"\bsorry\b", body):
            verdicts[name] = "sorry"
            continue
        try:
            check = lean_incremental_check(
                action="check_target",
                file_path=str(path),
                theorem_id=name,
                cwd=project_root,
            )
        except Exception:
            logger.debug("job gate check failed for %s", name, exc_info=True)
            verdicts[name] = "error"
            continue
        # ok = elaborated with NO errors and NO sorry (fail-closed when absent).
        gate_ok = bool(check.get("success")) and bool(check.get("ok"))
        verdicts[name] = "proved" if gate_ok else "error"
    return verdicts


def reconcile_job_graph(verdicts: Mapping[str, str], *, stub_file: str, job_id: str) -> int:
    """Flip gate-proved job declarations to ``proved`` on the graph.

    Only ``proved`` verdicts act (via_gate — this IS the parent's gate);
    everything weaker is left for the regular reconcile pass. Returns the
    number of nodes flipped; never raises.
    """
    if not plan_state.plan_state_enabled():
        return 0
    proved_names = sorted(n for n, v in verdicts.items() if v == "proved")
    if not proved_names:
        return 0
    why = f"dispatch job {job_id} gate"

    def _apply(bp: Any) -> tuple[Any, list[dict[str, str]]]:
        events: list[dict[str, str]] = []
        for name in proved_names:
            node_id = plan_state.node_id_for(name, stub_file)
            node = bp.node_by_id(node_id)
            if node is None or node.status == "proved":
                continue
            events.append({"node_id": node_id, "name": node.name, "from_status": node.status})
            bp = plan_state.set_node_status(
                bp, node_id, "proved", via_gate=True, why=why, journal=False
            )
        return bp, events

    try:
        # Journal AFTER the save: the notebook describes the persisted graph.
        bp, events = _apply(plan_state.load_blueprint())
        if not events:
            return 0
        try:
            plan_state.save_blueprint(bp)
        except plan_state.PlanStateRevisionConflict:
            bp, events = _apply(plan_state.load_blueprint())
            if not events:
                return 0
            plan_state.save_blueprint(bp)
        for event in events:
            plan_state.journal_node_status(
                node_id=event["node_id"],
                name=event["name"],
                from_status=event["from_status"],
                to_status="proved",
                via_gate=True,
                why=why,
            )
        return len(events)
    except Exception:
        logger.debug("job graph reconcile failed", exc_info=True)
        return 0


def _signal_job_group(process: Any, sig: signal.Signals) -> None:
    """Signal the child's whole session (pgid == pid), pid as fallback.

    Group-wide at every escalation step: the stub-file lock is released
    when this returns, so no descendant of the child session may outlive
    the leader.
    """
    pid = int(getattr(process, "pid", 0) or 0)
    if pid <= 0:
        return
    try:
        os.killpg(pid, sig)  # start_new_session=True => pgid == pid
    except Exception:
        with suppress(Exception):
            os.kill(pid, sig)


def _terminate_job_process(process: Any) -> None:
    """SIGINT -> SIGTERM -> SIGKILL, each on the full process group.

    Ends with an unconditional group SIGKILL: the leader exiting does not
    mean the group is empty, and the stub-file lock is released right
    after this returns — no descendant may survive past that point. On an
    already-empty group the killpg is a suppressed no-op.
    """
    try:
        for sig, grace in ((signal.SIGINT, 30), (signal.SIGTERM, 10), (signal.SIGKILL, 10)):
            _signal_job_group(process, sig)
            with suppress(Exception):
                process.wait(timeout=grace)
                return
    finally:
        _signal_job_group(process, signal.SIGKILL)


def launch_stub_prove_job(spec: JobSpec) -> dict[str, Any]:
    """Run one shape-A job synchronously; the dispatch spawn backend.

    Raises only for spec/lock problems (deploy turns those into a
    ``failed`` ledger entry with the error in notes); a timed-out or
    crashed child still produces a full result with gate verdicts.
    """
    from leanflow_cli.workflow import spawn_workflow  # lazy: heavy module

    stub_file = str(spec.inputs.get("stub_file", "") or "")
    decl_names = [str(n) for n in (spec.inputs.get("decl_names") or []) if str(n)]
    if not stub_file:
        raise ValueError(f"job {spec.job_id} has no inputs.stub_file")
    wall_clock = int(spec.budget.wall_clock_s or 0) or prover_job_wall_clock_s()
    owner_id = f"dispatch:{spec.job_id}"

    lock = acquire_file_lock(
        stub_file,
        owner_id=owner_id,
        purpose=f"dispatch:{spec.archetype}",
        ttl_seconds=wall_clock + LOCK_TTL_SLACK_S,
    )
    if not lock.get("success"):
        raise RuntimeError(f"file lock unavailable for {stub_file}")
    try:
        plan, process = spawn_workflow(
            f"/prove {stub_file}",
            interactive=False,
            extra_env=build_job_env(spec),
        )
        project_root = str(plan.project.root)
        timed_out = False
        exit_code: int | None = None
        try:
            exit_code = process.wait(timeout=wall_clock)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_job_process(process)
            with suppress(Exception):
                exit_code = process.poll()
        verdicts = decl_verdicts(stub_file, decl_names, project_root=project_root)
        flipped = reconcile_job_graph(verdicts, stub_file=stub_file, job_id=spec.job_id)
        if timed_out:
            status, notes = "timeout", f"wall clock ({wall_clock}s) exceeded; child terminated"
        elif exit_code == 0:
            status, notes = "done", ""
        else:
            status, notes = "failed", f"child exited {exit_code}"
        outcome = ProverJobOutcome(
            status=status,
            exit_code=exit_code,
            process_id=int(getattr(process, "pid", 0) or 0),
            decl_verdicts=verdicts,
            notes=(notes + (f"; {flipped} node(s) proved via gate" if flipped else "")).strip("; "),
        )
        return outcome.to_result()
    finally:
        with suppress(Exception):
            release_file_lock(stub_file, owner_id=owner_id)
