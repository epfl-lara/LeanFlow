"""Run and reconcile several independent attack directions for one goal.

Several rival attack directions on one goal, each a stub FILE discharged
by a nested prover job, run sequentially under the dispatch cap.
No new machinery: direction files are stated like decomposer stubs (same
shape guard, same in-place validation, all-or-nothing), the graph gets
``stated`` nodes with ``split_of`` edges to the goal via ``apply_delta``,
and jobs flow through the DispatchService ledger.

Merge protocol (mechanical, graph-only): the first direction whose FULL
declaration set passes the parent gate wins — the goal's ``depends_on``
edges rewire to the winning stubs, sibling directions' unproved nodes are
``parked`` (their files are kept as documentation), and the
choice lands in the decision log. If every direction exhausts, each has a
decision packet as the rigorous account and the orchestrator's normal
negate/park routes take over.

Direction-tagged ``statements_to_state`` come from the orchestrator's model
decision, and jobs require
``LEANFLOW_DISPATCH_ENABLED`` — with either off this module never runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanflow_cli.runtime import file_locks
from leanflow_cli.workflows import decomposer, plan_state, prover_jobs
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec

logger = logging.getLogger(__name__)

#: Ceiling on rival directions explored for one goal.
DEFAULT_MAX_PROVE_DIRECTIONS = 3

#: Per-direction child budget (turns for the nested /prove).
DEFAULT_DIRECTION_API_STEPS = 120


def max_prove_directions() -> int:
    try:
        value = int(os.getenv("LEANFLOW_MAX_PROVE_DIRECTIONS", "") or DEFAULT_MAX_PROVE_DIRECTIONS)
    except ValueError:
        value = DEFAULT_MAX_PROVE_DIRECTIONS
    return max(1, min(3, value))


def direction_api_steps() -> int:
    try:
        value = int(os.getenv("LEANFLOW_PROVER_JOB_API_STEPS", "") or DEFAULT_DIRECTION_API_STEPS)
    except ValueError:
        value = DEFAULT_DIRECTION_API_STEPS
    return max(1, value)


@dataclass(frozen=True)
class DirectionOutcome:
    direction: str
    stub_file: str = ""
    decl_names: tuple[str, ...] = ()
    job_id: str = ""
    status: str = ""  # stated-failed | job-done | job-failed | won | skipped
    verdicts: dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "stub_file": self.stub_file,
            "decl_names": list(self.decl_names),
            "job_id": self.job_id,
            "status": self.status,
            "verdicts": dict(self.verdicts),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MultiDirectionOutcome:
    ok: bool
    reason: str = ""
    winner: str = ""
    directions: tuple[DirectionOutcome, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "winner": self.winner,
            "directions": [d.to_payload() for d in self.directions],
        }


def directions_from_statements(entries: Sequence[Any]) -> dict[str, list[dict[str, Any]]]:
    """Group ``statements_to_state`` by their ``direction`` tag.

    Fail closed on mixed input: if ANY statable entry (name + statement)
    lacks a direction tag, return {} and let the single-direction path
    handle the whole list — multi-direction mode must never silently drop
    a statement.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        direction = str(entry.get("direction", "") or "").strip()
        name = str(entry.get("name", "") or "").strip()
        statement = str(entry.get("statement", "") or "").strip()
        if not name or not statement:
            continue  # nothing statable — no path can use it
        if not direction:
            return {}  # mixed tagged/untagged: fail closed to the normal route
        grouped.setdefault(direction, []).append(
            {"name": name, "statement": statement, "direction": direction}
        )
    return grouped


def _import_header(goal_file_text: str) -> str:
    """The leading import/comment block of the goal file (stubs need it)."""
    kept: list[str] = []
    for line in goal_file_text.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("import ", "--", "/-", "-/"))
            or stripped.endswith("-/")
        ):
            kept.append(line)
            continue
        break
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def direction_file_path(goal_file: str, direction: str) -> str:
    """Sibling of the goal file: ``<Goal>_<direction>.lean`` (sanitized)."""
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in direction) or "dir"
    goal = Path(goal_file)
    return str(goal.with_name(f"{goal.stem}_{safe}.lean"))


def _materialize_direction_file(
    path: Path,
    *,
    rel_path: str,
    content: str,
    names: Sequence[str],
    cwd: str,
) -> str:
    """Create and validate one reserved direction source, returning an error."""
    try:
        # O_EXCL: exclusive creation — refuses existing files AND symlinks
        # (dangling ones included), closing the check-then-write race.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return f"direction file already exists: {rel_path}"
    except OSError as exc:
        return f"cannot write direction file: {exc}"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        path.unlink(missing_ok=True)
        return f"cannot write direction file: {exc}"

    from leanflow_cli.lean.lean_incremental import lean_incremental_check

    for name in names:
        try:
            check = lean_incremental_check(
                action="check_target", file_path=str(path), theorem_id=name, cwd=cwd
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            return f"validation crashed for {name}: {exc}"
        if not check.get("success") or check.get("has_errors"):
            path.unlink(missing_ok=True)
            return f"stub {name} does not elaborate in {rel_path}"
    return ""


def state_direction_file(
    *,
    direction: str,
    statements: Sequence[Mapping[str, Any]],
    goal_file: str,
    cwd: str,
) -> tuple[str, tuple[str, ...], str]:
    """Create one direction's stub file; (path, decl names, error reason).

    All-or-nothing: every statement must pass the decomposer stub-shape
    guard, the file must not already exist (direction files are never
    clobbered — they are documentation), and every declaration must
    validate in place (sorry warnings pass, errors delete the file).
    """
    root = Path(cwd or ".")
    goal_path = root / goal_file if not os.path.isabs(goal_file) else Path(goal_file)
    try:
        goal_text = goal_path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", (), f"goal file unreadable: {exc}"

    skeletons: list[str] = []
    names: list[str] = []
    for entry in statements:
        skeleton = decomposer.normalize_statement(str(entry.get("statement", "") or ""))
        if not decomposer.stub_shape_ok(skeleton):
            return "", (), f"stub-shape violation in direction {direction!r}"
        name = decomposer._helper_name(skeleton)
        if not name:
            return "", (), f"unnamed stub in direction {direction!r}"
        claimed = str(entry.get("name", "") or "").strip()
        if claimed and claimed != name:
            # The graph, the job, and the gate must all speak the PARSED
            # name; a mismatched claim would create phantom nodes.
            return "", (), f"statement name mismatch: claimed {claimed!r}, parsed {name!r}"
        skeletons.append(skeleton)
        names.append(name)
    if not skeletons:
        return "", (), f"direction {direction!r} has no statements"

    rel_path = direction_file_path(goal_file, direction)
    path = root / rel_path if not os.path.isabs(rel_path) else Path(rel_path)
    header = _import_header(goal_text)
    content = (header + "\n\n" if header else "") + "\n\n".join(skeletons) + "\n"
    owner_id = str(os.getenv("LEANFLOW_NATIVE_RUNNER_OWNER", "") or "").strip() or (
        f"multi-direction:{os.getpid()}"
    )
    reservation = file_locks.acquire_file_lock(
        str(path),
        owner_id=owner_id,
        purpose="multi-direction source creation",
    )
    if reservation.get("success") is not True:
        return "", (), str(reservation.get("error", "direction source is reserved") or "")
    materialization_error = ""
    release_error = ""
    try:
        materialization_error = _materialize_direction_file(
            path,
            rel_path=rel_path,
            content=content,
            names=names,
            cwd=cwd,
        )
    finally:
        released = file_locks.release_file_lock(str(path), owner_id=owner_id)
        if released.get("success") is not True:
            release_error = str(released.get("error", "direction reservation release failed") or "")
    if materialization_error or release_error:
        return "", (), materialization_error or release_error
    return rel_path, tuple(names), ""


def _merge_direction_into_graph(
    *,
    direction: str,
    statements: Sequence[Mapping[str, Any]],
    stub_file: str,
    goal_symbol: str,
    goal_file: str,
) -> None:
    """Stated nodes + split_of->goal edges through the one graph door."""
    delta = {
        "nodes": [
            {
                "name": str(entry["name"]),
                "file": stub_file,
                "statement": str(entry["statement"]),
                "notes": f"direction:{direction}",
                "split_of": {"name": goal_symbol, "file": goal_file},
            }
            for entry in statements
        ]
    }
    bp, changes = plan_state.apply_delta(
        plan_state.load_blueprint(), delta, generated_by="orchestrator", journal=False
    )
    try:
        plan_state.save_blueprint(bp)
    except plan_state.PlanStateRevisionConflict:
        bp, changes = plan_state.apply_delta(
            plan_state.load_blueprint(), delta, generated_by="orchestrator", journal=False
        )
        plan_state.save_blueprint(bp)
    plan_state.journal_delta_changes(changes, generated_by="orchestrator")


def _rewire_goal_to_winner(
    *, goal_symbol: str, goal_file: str, stub_file: str, winner_names: Sequence[str]
) -> None:
    """goal --depends_on--> each winning stub (apply_delta adds, never removes)."""
    delta = {
        "nodes": [],
        "edges": [
            {
                "source": {"name": goal_symbol, "file": goal_file},
                "target": {"name": name, "file": stub_file},
                "kind": "depends_on",
            }
            for name in winner_names
        ],
    }
    bp, changes = plan_state.apply_delta(
        plan_state.load_blueprint(), delta, generated_by="orchestrator", journal=False
    )
    try:
        plan_state.save_blueprint(bp)
    except plan_state.PlanStateRevisionConflict:
        bp, changes = plan_state.apply_delta(
            plan_state.load_blueprint(), delta, generated_by="orchestrator", journal=False
        )
        plan_state.save_blueprint(bp)
    plan_state.journal_delta_changes(changes, generated_by="orchestrator")


def _enforce_gate_truth(
    *, stub_file: str, names: Sequence[str], verdicts: Mapping[str, str], job_id: str
) -> None:
    """Make the LOCAL gate the sole graph authority for this stub file.

    First ``plan_state.reconcile`` with truth derived from our own verdicts
    — the one sanctioned downgrade path — so a lying transport's fabricated
    ``proved`` promotions do not survive; then promote local-proved nodes
    via the gate. Journal-after-save discipline throughout; never raises.
    """
    if not plan_state.plan_state_enabled():
        return
    truth = {
        (stub_file, str(name)): plan_state.DeclTruth(
            present=verdicts.get(str(name), "missing") != "missing",
            has_sorry=verdicts.get(str(name)) == "sorry",
            has_error_diag=verdicts.get(str(name)) == "error",
        )
        for name in names
    }
    try:
        bp, changes = plan_state.reconcile(plan_state.load_blueprint(), truth)
        if changes:
            try:
                plan_state.save_blueprint(bp)
            except plan_state.PlanStateRevisionConflict:
                bp, changes = plan_state.reconcile(plan_state.load_blueprint(), truth)
                if changes:
                    plan_state.save_blueprint(bp)
            for change in changes:
                plan_state.append_journal_event(change)
    except Exception:
        logger.debug("direction gate-truth reconcile failed", exc_info=True)
    prover_jobs.reconcile_job_graph(verdicts, stub_file=stub_file, job_id=job_id)


def _park_direction_nodes(outcome: DirectionOutcome, *, winner: str) -> None:
    """Park a losing direction's unproved nodes (files stay — N1).

    Journal-after-save discipline: park with ``journal=False``, persist
    (one reload-reapply on revision conflict), then journal exactly the
    changes that were persisted.
    """
    if not plan_state.plan_state_enabled() or not outcome.stub_file:
        return
    why = f"direction {outcome.direction!r} lost to {winner!r}"

    def _apply(bp: Any) -> tuple[Any, list[dict[str, str]]]:
        events: list[dict[str, str]] = []
        for name in outcome.decl_names:
            node = bp.node_by_id(plan_state.node_id_for(name, outcome.stub_file))
            if node is None or node.status in {"proved", "false", "parked"}:
                continue
            events.append({"node_id": node.id, "name": node.name, "from_status": node.status})
            bp = plan_state.set_node_status(bp, node.id, "parked", why=why, journal=False)
        return bp, events

    try:
        bp, events = _apply(plan_state.load_blueprint())
        if not events:
            return
        try:
            plan_state.save_blueprint(bp)
        except plan_state.PlanStateRevisionConflict:
            bp, events = _apply(plan_state.load_blueprint())
            if not events:
                return
            plan_state.save_blueprint(bp)
        for event in events:
            plan_state.journal_node_status(
                node_id=event["node_id"],
                name=event["name"],
                from_status=event["from_status"],
                to_status="parked",
                via_gate=False,
                why=why,
            )
    except Exception:
        logger.debug("direction parking failed", exc_info=True)


def run_multi_direction(
    *,
    goal_symbol: str,
    goal_file: str,
    statements_to_state: Sequence[Any],
    cwd: str = "",
    service: Any = None,
) -> MultiDirectionOutcome:
    """Sequential frontier discharge over rival directions; never raises."""
    try:
        from leanflow_cli.workflows.dispatch_service import DispatchService, dispatch_enabled

        grouped = directions_from_statements(statements_to_state)
        if not grouped:
            return MultiDirectionOutcome(ok=False, reason="no direction-tagged statements")
        if not goal_symbol or not goal_file:
            return MultiDirectionOutcome(ok=False, reason="no goal target for directions")
        if service is None:
            if not dispatch_enabled():
                return MultiDirectionOutcome(ok=False, reason="dispatch is disabled")
            service = DispatchService()

        ordered = list(grouped.items())[: max_prove_directions()]
        results: list[DirectionOutcome] = []
        winner: DirectionOutcome | None = None
        for direction, statements in ordered:
            if winner is not None:
                results.append(DirectionOutcome(direction=direction, status="skipped"))
                continue
            stub_file, names, err = state_direction_file(
                direction=direction, statements=statements, goal_file=goal_file, cwd=cwd
            )
            if err:
                results.append(
                    DirectionOutcome(direction=direction, status="stated-failed", reason=err)
                )
                continue
            _merge_direction_into_graph(
                direction=direction,
                statements=statements,
                stub_file=stub_file,
                goal_symbol=goal_symbol,
                goal_file=goal_file,
            )
            spec = JobSpec(
                job_id=service.mint_job_id("prover", role="orchestrator"),
                archetype="prover",
                requester_role="orchestrator",
                objective=(
                    f"Prove every declaration in {stub_file} (direction {direction!r} "
                    f"toward `{goal_symbol}`)."
                ),
                budget=JobBudget(
                    api_steps=direction_api_steps(),
                    wall_clock_s=_wall_clock_s(),
                ),
                deliverable="prove_outcome",
                inputs={"stub_file": stub_file, "decl_names": list(names)},
            )
            service.propose(spec)
            entry = service.deploy(spec.job_id)
            # Kernel truth: the winner is decided by OUR OWN gate over the
            # on-disk file — never by the ledger's account (an injected
            # service is trusted only as job transport) — and the local
            # verdicts are enforced on the graph, undoing any fabricated
            # promotions a lying transport may have written.
            gate_verdicts = prover_jobs.decl_verdicts(stub_file, names, project_root=cwd)
            _enforce_gate_truth(
                stub_file=stub_file, names=names, verdicts=gate_verdicts, job_id=spec.job_id
            )
            all_proved = bool(names) and all(gate_verdicts.get(n) == "proved" for n in names)
            outcome = DirectionOutcome(
                direction=direction,
                stub_file=stub_file,
                decl_names=names,
                job_id=spec.job_id,
                status=(
                    "won" if all_proved else ("job-done" if entry.state == "done" else "job-failed")
                ),
                verdicts=gate_verdicts,
                reason="" if all_proved else f"ledger state {entry.state}",
            )
            results.append(outcome)
            if all_proved:
                winner = outcome

        if winner is not None:
            _rewire_goal_to_winner(
                goal_symbol=goal_symbol,
                goal_file=goal_file,
                stub_file=winner.stub_file,
                winner_names=winner.decl_names,
            )
            for outcome in results:
                if outcome.direction != winner.direction:
                    _park_direction_nodes(outcome, winner=winner.direction)
            goal_node = plan_state.node_id_for(goal_symbol, goal_file)
            plan_state.record_decision_packet(
                {
                    "packet_id": f"dir-{goal_node}-{winner.direction}",
                    "scope": "multi-direction",
                    "target_symbol": goal_symbol,
                    "options": [d.direction for d in results],
                    "decision": winner.direction,
                    "decided_by": "multi-direction-gate",
                }
            )
            return MultiDirectionOutcome(
                ok=True,
                reason=f"direction {winner.direction!r} fully proved",
                winner=winner.direction,
                directions=tuple(results),
            )

        # All exhausted: each direction leaves a packet — the rigorous account.
        goal_node = plan_state.node_id_for(goal_symbol, goal_file)
        for outcome in results:
            plan_state.record_decision_packet(
                {
                    "packet_id": f"dir-{goal_node}-{outcome.direction}",
                    "scope": "multi-direction",
                    "target_symbol": goal_symbol,
                    "options": ["negate", "park", "re-state"],
                    "direction": outcome.direction,
                    "verdicts": dict(outcome.verdicts),
                    "reason": outcome.reason,
                }
            )
        return MultiDirectionOutcome(
            ok=False,
            reason="all directions exhausted (packets recorded)",
            directions=tuple(results),
        )
    except Exception as exc:
        logger.debug("multi-direction run failed", exc_info=True)
        return MultiDirectionOutcome(ok=False, reason=f"{type(exc).__name__}: {exc}")


def _wall_clock_s() -> int:
    from leanflow_cli.workflows.prover_jobs import prover_job_wall_clock_s

    return prover_job_wall_clock_s()
