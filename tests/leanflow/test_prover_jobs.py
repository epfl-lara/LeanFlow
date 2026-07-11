"""Phase 5 (4/6) tests: shape-A prover jobs — env hygiene, gate, lifecycle.

No real subprocess anywhere: spawn_workflow, lean_incremental_check, and
the lock registry are faked. The assertions pin the §5.7 contract — the
child env can never leak parent-run identity, the parent's OWN gate is
the only source of `proved`, locks are held for the child's lifetime,
and a timed-out child still yields a full, gate-checked result.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows import plan_state, prover_jobs
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec

STUB_FILE = "ProveDemo/Generated.lean"


def _spec(**inputs: Any) -> JobSpec:
    return JobSpec(
        job_id="run.orchestrator.pv-001",
        archetype="prover",
        requester_role="orchestrator",
        objective="discharge the stub file",
        budget=JobBudget(api_steps=40, wall_clock_s=120),
        deliverable="prove_outcome",
        inputs={"stub_file": STUB_FILE, "decl_names": ["h1", "h2"], **inputs},
    )


class _FakeProcess:
    def __init__(self, *, exit_code: int = 0, hang: bool = False):
        self.pid = 4242
        self._exit_code = exit_code
        self._hang = hang
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None):
        self.waits.append(timeout)
        if self._hang and len(self.waits) == 1:
            raise subprocess.TimeoutExpired(cmd="prove", timeout=timeout or 0)
        return self._exit_code

    def poll(self):
        return None if self._hang else self._exit_code

    def terminate(self):
        pass

    def kill(self):
        pass


def _fake_spawn(monkeypatch, tmp_path, process: _FakeProcess):
    calls: list[dict] = []

    def fake(command, *, interactive=False, extra_env=None, **kwargs):
        calls.append({"command": command, "interactive": interactive, "extra_env": extra_env})
        plan = SimpleNamespace(project=SimpleNamespace(root=tmp_path))
        return plan, process

    import leanflow_cli.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "spawn_workflow", fake)
    return calls


def _fake_locks(monkeypatch, *, grant: bool = True):
    events: list[tuple[str, str]] = []
    ttls: list[int] = []

    def fake_acquire(path, *, owner_id, purpose="", ttl_seconds=1800, force=False):
        events.append(("acquire", owner_id))
        ttls.append(ttl_seconds)
        return {"success": grant} if grant else {"success": False, "error": "held"}

    def fake_release(path, *, owner_id, force=False):
        events.append(("release", owner_id))
        return {"success": True, "released": True}

    monkeypatch.setattr(prover_jobs, "acquire_file_lock", fake_acquire)
    monkeypatch.setattr(prover_jobs, "release_file_lock", fake_release)
    return events, ttls


def _fake_verdicts(monkeypatch, verdicts: dict[str, str]):
    calls: list[dict] = []

    def fake(stub_file, decl_names, *, project_root):
        calls.append({"stub_file": stub_file, "names": list(decl_names), "root": project_root})
        return dict(verdicts)

    monkeypatch.setattr(prover_jobs, "decl_verdicts", fake)
    return calls


# ---------------------------------------------------------------------------
# Env hygiene
# ---------------------------------------------------------------------------


def test_build_job_env_hygiene(monkeypatch):
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-parent-run")
    monkeypatch.setenv("LEANFLOW_NATIVE_RUNNER_OWNER", "parent-owner")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", "bp.json")

    env = prover_jobs.build_job_env(_spec())

    assert env["LEANFLOW_WORKFLOW_RUN_ID"] == ""  # child mints fresh
    assert env["LEANFLOW_WORKFLOW_PARENT_RUN_ID"] == "prove-parent-run"  # N3 edge
    assert env["LEANFLOW_DISPATCH_JOB_ID"] == "run.orchestrator.pv-001"
    assert env["LEANFLOW_JOB_LINEAGE"] == "run.orchestrator.pv-001"
    assert env["AGENT_MAX_TURNS"] == "40"
    assert env["LEANFLOW_NATIVE_RUNNER_OWNER"] == ""  # never release parent locks
    assert env["LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE"] == ""
    assert env["LEANFLOW_FORMALIZATION_BLUEPRINT"] == ""


def test_prover_light_tier_overrides_job_model(monkeypatch):
    """models.prover_light routes stub grinding to the light tier; empty
    (the default) leaves the parent's model untouched."""
    monkeypatch.setattr(prover_jobs, "_prover_light_model", lambda: "small/fast-prover")
    env = prover_jobs.build_job_env(_spec())
    assert env["LEANFLOW_NATIVE_MODEL"] == "small/fast-prover"

    monkeypatch.setattr(prover_jobs, "_prover_light_model", lambda: "")
    env = prover_jobs.build_job_env(_spec())
    assert "LEANFLOW_NATIVE_MODEL" not in env


def test_research_mode_doubles_job_turns(monkeypatch):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    assert prover_jobs.build_job_env(_spec())["AGENT_MAX_TURNS"] == "80"
    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    assert prover_jobs.build_job_env(_spec())["AGENT_MAX_TURNS"] == "40"


# ---------------------------------------------------------------------------
# Lifecycle: happy path, crash, timeout, lock discipline
# ---------------------------------------------------------------------------


def test_happy_path_done_with_gate_verdicts(monkeypatch, tmp_path):
    process = _FakeProcess(exit_code=0)
    spawn_calls = _fake_spawn(monkeypatch, tmp_path, process)
    lock_events, lock_ttls = _fake_locks(monkeypatch)
    gate_calls = _fake_verdicts(monkeypatch, {"h1": "proved", "h2": "sorry"})
    monkeypatch.setattr(prover_jobs, "reconcile_job_graph", lambda *a, **k: 1)

    result = prover_jobs.launch_stub_prove_job(_spec())

    assert result["status"] == "done"
    deliverable = result["deliverable"]
    assert deliverable["decl_verdicts"] == {"h1": "proved", "h2": "sorry"}
    assert deliverable["proved"] == ["h1"]
    assert deliverable["exit_code"] == 0 and deliverable["process_id"] == 4242
    assert "1 node(s) proved via gate" in deliverable["notes"]

    assert spawn_calls[0]["command"] == f"/prove {STUB_FILE}"
    assert spawn_calls[0]["interactive"] is False
    assert spawn_calls[0]["extra_env"]["LEANFLOW_DISPATCH_JOB_ID"] == "run.orchestrator.pv-001"
    assert process.waits[0] == 120  # the job budget's wall clock
    assert gate_calls[0]["names"] == ["h1", "h2"]
    # Lock held for the child's lifetime, then released, same owner.
    assert lock_events == [
        ("acquire", "dispatch:run.orchestrator.pv-001"),
        ("release", "dispatch:run.orchestrator.pv-001"),
    ]
    # The lease must outlive the wall clock by the escalation + gate slack.
    assert lock_ttls == [120 + prover_jobs.LOCK_TTL_SLACK_S]


def test_nonzero_exit_is_failed_but_verdicts_survive(monkeypatch, tmp_path):
    _fake_spawn(monkeypatch, tmp_path, _FakeProcess(exit_code=3))
    _fake_locks(monkeypatch)
    _fake_verdicts(monkeypatch, {"h1": "error", "h2": "missing"})
    monkeypatch.setattr(prover_jobs, "reconcile_job_graph", lambda *a, **k: 0)

    result = prover_jobs.launch_stub_prove_job(_spec())

    assert result["status"] == "failed"
    assert "child exited 3" in result["deliverable"]["notes"]
    assert result["deliverable"]["decl_verdicts"] == {"h1": "error", "h2": "missing"}


def test_timeout_terminates_and_still_gates(monkeypatch, tmp_path):
    process = _FakeProcess(hang=True)
    _fake_spawn(monkeypatch, tmp_path, process)
    _fake_locks(monkeypatch)
    _fake_verdicts(monkeypatch, {"h1": "proved", "h2": "error"})
    monkeypatch.setattr(prover_jobs, "reconcile_job_graph", lambda *a, **k: 1)
    killed: list[Any] = []
    monkeypatch.setattr(prover_jobs, "_terminate_job_process", lambda p: killed.append(p))

    result = prover_jobs.launch_stub_prove_job(_spec())

    assert killed == [process]
    assert result["status"] == "timeout"
    assert "wall clock" in result["deliverable"]["notes"]
    # Even a killed child's partial progress is gate-checked, never lost.
    assert result["deliverable"]["proved"] == ["h1"]


def test_lock_conflict_raises_and_never_spawns(monkeypatch, tmp_path):
    spawn_calls = _fake_spawn(monkeypatch, tmp_path, _FakeProcess())
    _fake_locks(monkeypatch, grant=False)

    with pytest.raises(RuntimeError, match="file lock unavailable"):
        prover_jobs.launch_stub_prove_job(_spec())

    assert spawn_calls == []


def test_missing_stub_file_raises(monkeypatch):
    with pytest.raises(ValueError, match="no inputs.stub_file"):
        prover_jobs.launch_stub_prove_job(_spec(stub_file=""))


# ---------------------------------------------------------------------------
# The parent-side gate
# ---------------------------------------------------------------------------


def _write_stub_file(tmp_path) -> str:
    (tmp_path / "ProveDemo").mkdir()
    (tmp_path / STUB_FILE).write_text(
        "\n".join(
            [
                "theorem h1 : True := trivial",
                "",
                "theorem h2 : True := by sorry",
                "",
                "-- sorry in a comment must not count",
                "theorem h3 : True := trivial",
                "",
                "theorem h4 : True := by exact (by sorry)",
                "",
                "theorem h5 : True := trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_decl_verdicts_gate_rules(monkeypatch, tmp_path):
    root = _write_stub_file(tmp_path)
    checks: list[str] = []

    def fake_check(**kwargs):
        checks.append(kwargs["theorem_id"])
        # h3: elaboration error. h5: macro-hidden sorry the text scan cannot
        # see — LeanProbe's ok=False (no-errors-AND-no-sorry) must catch it.
        return {"success": True, "ok": kwargs["theorem_id"] not in {"h3", "h5"}}

    import leanflow_cli.lean.lean_incremental as inc

    monkeypatch.setattr(inc, "lean_incremental_check", fake_check)

    verdicts = prover_jobs.decl_verdicts(
        STUB_FILE, ["h1", "h2", "h3", "h4", "h5", "ghost"], project_root=root
    )

    assert verdicts == {
        "h1": "proved",
        "h2": "sorry",
        "h3": "error",
        "h4": "sorry",  # word-boundary regex catches `sorry)` too
        "h5": "error",  # probe-level sorry detection (ok=False)
        "ghost": "missing",
    }
    # sorry-bodied and missing decls never reach the checker.
    assert checks == ["h1", "h3", "h5"]


def test_decl_verdicts_fail_closed_without_ok_field(monkeypatch, tmp_path):
    """A payload missing `ok` (older probe / error shape) is never proved."""
    root = _write_stub_file(tmp_path)
    import leanflow_cli.lean.lean_incremental as inc

    monkeypatch.setattr(
        inc, "lean_incremental_check", lambda **kw: {"success": True, "has_errors": False}
    )

    verdicts = prover_jobs.decl_verdicts(STUB_FILE, ["h1"], project_root=root)

    assert verdicts == {"h1": "error"}


def test_terminate_escalation_signals_the_group(monkeypatch):
    import signal as _signal

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(prover_jobs.os, "killpg", lambda pid, sig: sent.append((pid, int(sig))))

    class Stubborn:
        pid = 777

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="prove", timeout=timeout or 0)

    prover_jobs._terminate_job_process(Stubborn())

    assert [sig for _pid, sig in sent] == [
        int(_signal.SIGINT),
        int(_signal.SIGTERM),
        int(_signal.SIGKILL),
        int(_signal.SIGKILL),  # the unconditional final group sweep
    ]
    assert all(pid == 777 for pid, _sig in sent)


def test_leader_exit_still_sweeps_group_with_sigkill(monkeypatch):
    """Leader death != group death: a descendant ignoring SIGINT must not
    outlive the lock release — the group gets a final SIGKILL regardless."""
    import signal as _signal

    sent: list[int] = []
    monkeypatch.setattr(prover_jobs.os, "killpg", lambda pid, sig: sent.append(int(sig)))

    class PoliteLeader:
        pid = 778
        _signalled = False

        def wait(self, timeout=None):
            return 130  # leader exits on the first SIGINT

    prover_jobs._terminate_job_process(PoliteLeader())

    assert sent == [int(_signal.SIGINT), int(_signal.SIGKILL)]


def test_decl_verdicts_missing_file(tmp_path):
    verdicts = prover_jobs.decl_verdicts("Nope.lean", ["a"], project_root=str(tmp_path))
    assert verdicts == {"a": "missing"}


# ---------------------------------------------------------------------------
# Graph reconciliation: proved only via the parent's gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def graph(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))
    node_id = plan_state.node_id_for("h1", STUB_FILE)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(id=node_id, name="h1", file=STUB_FILE, status="stated"),
                plan_state.GraphNode(
                    id=plan_state.node_id_for("h2", STUB_FILE),
                    name="h2",
                    file=STUB_FILE,
                    status="stated",
                ),
            )
        )
    )
    return node_id


def test_reconcile_flips_only_gate_proved(graph):
    flipped = prover_jobs.reconcile_job_graph(
        {"h1": "proved", "h2": "sorry", "ghost": "proved"},
        stub_file=STUB_FILE,
        job_id="run.orchestrator.pv-001",
    )

    assert flipped == 1
    bp = plan_state.load_blueprint()
    assert bp.node_by_id(graph).status == "proved"
    assert bp.node_by_id(plan_state.node_id_for("h2", STUB_FILE)).status == "stated"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert '"via_gate": true' in journal and "dispatch job run.orchestrator.pv-001" in journal


def test_reconcile_noop_when_plan_state_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    assert prover_jobs.reconcile_job_graph({"h1": "proved"}, stub_file=STUB_FILE, job_id="j") == 0


# ---------------------------------------------------------------------------
# Dispatch seam
# ---------------------------------------------------------------------------


def test_dispatch_routes_prover_jobs_through_spawn_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    from leanflow_cli.workflows.dispatch_service import DispatchService

    monkeypatch.setattr(
        prover_jobs,
        "launch_stub_prove_job",
        lambda spec: {"status": "done", "deliverable": {"proved": ["h1"]}},
    )
    service = DispatchService(root_job_id="run")
    spec = _spec()
    service.propose(spec)

    entry = service.deploy(spec.job_id)

    assert entry.state == "done"
    assert entry.result["deliverable"]["proved"] == ["h1"]
    summary = json.loads(
        (tmp_path / ".leanflow" / "workflow-state" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["dispatch_ledger"][0]["state"] == "done"
