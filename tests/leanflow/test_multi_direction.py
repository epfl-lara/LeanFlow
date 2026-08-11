"""Phase 5 (5/6) tests: multi-direction proving — N4 merge protocol.

The §5.8 acceptance test lives here: two rival directions where the
second proves — the first is parked, not deleted; the goal's depends_on
rewires to the winning stubs; the decision is recorded. Dispatch and the
incremental checker are faked; file mechanics are real (tmp project).
"""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.runtime import file_locks
from leanflow_cli.workflows import multi_direction as md
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.dispatch_models import LedgerEntry
from leanflow_cli.workflows.orchestrator import OrchestratorRoute

GOAL_FILE = "Demo/Main.lean"


@pytest.fixture()
def project(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))
    (tmp_path / "Demo").mkdir()
    (tmp_path / GOAL_FILE).write_text(
        "import Mathlib.Tactic\n\n-- goal file\ntheorem goal : True := by sorry\n",
        encoding="utf-8",
    )
    goal_id = plan_state.node_id_for("goal", GOAL_FILE)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(plan_state.GraphNode(id=goal_id, name="goal", file=GOAL_FILE, status="proving"),)
        )
    )
    return tmp_path


def _statements() -> list[dict[str, Any]]:
    return [
        {
            "name": "via_induction",
            "statement": "lemma via_induction : True := by sorry",
            "direction": "dirA",
        },
        {
            "name": "via_duality",
            "statement": "lemma via_duality : True := by sorry",
            "direction": "dirB",
        },
    ]


class _FakeService:
    """Ledger-shaped fake: dirA's job fails, dirB's job proves everything."""

    def __init__(self, verdicts_by_file: dict[str, dict[str, str]]):
        self.verdicts_by_file = verdicts_by_file
        self.specs: list[Any] = []
        self._seq = 0

    def mint_job_id(self, archetype, *, role, parent_job_id=""):
        self._seq += 1
        return f"run.{role}.pv-{self._seq:03d}"

    def propose(self, spec):
        self.specs.append(spec)

    def deploy(self, job_id):
        spec = next(s for s in self.specs if s.job_id == job_id)
        verdicts = self.verdicts_by_file.get(spec.inputs["stub_file"], {})
        all_proved = verdicts and all(v == "proved" for v in verdicts.values())
        # Mirror prover_jobs: proved verdicts flip graph nodes via the gate.
        if plan_state.plan_state_enabled():
            from leanflow_cli.workflows import prover_jobs

            prover_jobs.reconcile_job_graph(
                verdicts, stub_file=spec.inputs["stub_file"], job_id=job_id
            )
        return LedgerEntry(
            spec=spec,
            state="done" if all_proved else "failed",
            result={"deliverable": {"decl_verdicts": verdicts}},
        )


def _fake_checker(monkeypatch, *, fail_names: set[str] = frozenset()):
    import leanflow_cli.lean.lean_incremental as inc

    monkeypatch.setattr(
        inc,
        "lean_incremental_check",
        lambda **kw: {"success": True, "has_errors": kw["theorem_id"] in fail_names},
    )


def _fake_gate(monkeypatch, verdicts_by_file: dict[str, dict[str, str]]):
    """The parent-side gate multi_direction re-runs to decide the winner."""
    from leanflow_cli.workflows import prover_jobs

    monkeypatch.setattr(
        prover_jobs,
        "decl_verdicts",
        lambda stub_file, names, *, project_root: dict(verdicts_by_file.get(stub_file, {})),
    )


# ---------------------------------------------------------------------------
# Grouping + file statement
# ---------------------------------------------------------------------------


def test_untagged_statements_are_not_multi_direction():
    assert md.directions_from_statements([{"name": "a", "statement": "s"}]) == {}
    assert md.directions_from_statements([]) == {}
    grouped = md.directions_from_statements(_statements())
    assert sorted(grouped) == ["dirA", "dirB"]


def test_direction_cap(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MAX_PROVE_DIRECTIONS", "9")
    assert md.max_prove_directions() == 3
    monkeypatch.setenv("LEANFLOW_MAX_PROVE_DIRECTIONS", "0")
    assert md.max_prove_directions() == 1


def test_state_direction_file_copies_imports_and_validates(project, monkeypatch):
    _fake_checker(monkeypatch)

    rel, names, err = md.state_direction_file(
        direction="dirA",
        statements=[
            {"name": "via_induction", "statement": "lemma via_induction : True := by sorry"}
        ],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert err == "" and names == ("via_induction",)
    text = (project / rel).read_text(encoding="utf-8")
    assert text.startswith("import Mathlib.Tactic")
    assert "lemma via_induction : True := by sorry" in text
    assert rel == "Demo/Main_dirA.lean"


def test_state_direction_file_all_or_nothing(project, monkeypatch):
    _fake_checker(monkeypatch, fail_names={"bad"})

    rel, _names, err = md.state_direction_file(
        direction="dirA",
        statements=[{"name": "bad", "statement": "lemma bad : True := by sorry"}],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert rel == "" and "does not elaborate" in err
    assert not (project / "Demo/Main_dirA.lean").exists()  # cleaned up


def test_state_direction_file_never_clobbers(project, monkeypatch):
    _fake_checker(monkeypatch)
    (project / "Demo/Main_dirA.lean").write_text("-- precious\n", encoding="utf-8")

    _rel, _names, err = md.state_direction_file(
        direction="dirA",
        statements=[{"name": "x", "statement": "lemma x : True := by sorry"}],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert "already exists" in err
    assert (project / "Demo/Main_dirA.lean").read_text(encoding="utf-8") == "-- precious\n"


def test_state_direction_file_honors_terminal_project_namespace(project, monkeypatch):
    _fake_checker(monkeypatch)
    monkeypatch.setenv("LEANFLOW_HOME", str(project / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_RUNNER_OWNER", "writer-owner")
    reserved = file_locks.acquire_namespace_lock(
        str(project),
        owner_id="terminal-owner",
        purpose="terminal mathematical outcome",
        strict=True,
    )
    assert reserved["success"] is True
    try:
        rel, _names, err = md.state_direction_file(
            direction="dirA",
            statements=[{"name": "x", "statement": "lemma x : True := by sorry"}],
            goal_file=GOAL_FILE,
            cwd=str(project),
        )
    finally:
        released = file_locks.release_namespace_lock(
            str(project), owner_id="terminal-owner", strict=True
        )
        assert released["success"] is True

    assert rel == ""
    assert "terminal-owner" in err
    assert not (project / "Demo/Main_dirA.lean").exists()


def test_stub_shape_guard_applies(project, monkeypatch):
    _fake_checker(monkeypatch)

    _rel, _names, err = md.state_direction_file(
        direction="dirA",
        statements=[
            {
                "name": "smuggle",
                "statement": "theorem a : True := by sorry\ntheorem b : True := by sorry",
            }
        ],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert "stub-shape violation" in err


# ---------------------------------------------------------------------------
# The §5.8 acceptance test: second direction wins
# ---------------------------------------------------------------------------


def test_second_direction_wins_first_is_parked_not_deleted(project, monkeypatch):
    _fake_checker(monkeypatch)
    verdicts = {
        "Demo/Main_dirA.lean": {"via_induction": "error"},
        "Demo/Main_dirB.lean": {"via_duality": "proved"},
    }
    _fake_gate(monkeypatch, verdicts)
    service = _FakeService(verdicts)

    outcome = md.run_multi_direction(
        goal_symbol="goal",
        goal_file=GOAL_FILE,
        statements_to_state=_statements(),
        cwd=str(project),
        service=service,
    )

    assert outcome.ok and outcome.winner == "dirB"
    statuses = {d.direction: d.status for d in outcome.directions}
    assert statuses == {"dirA": "job-failed", "dirB": "won"}

    # Loser parked, not deleted (N1: the file is documentation).
    assert (project / "Demo/Main_dirA.lean").is_file()
    bp = plan_state.load_blueprint()
    loser = bp.node_by_id(plan_state.node_id_for("via_induction", "Demo/Main_dirA.lean"))
    assert loser.status == "parked"
    assert loser.notes == "direction:dirA"  # the direction tag survives
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "lost to 'dirB'" in journal  # the parking reason is journaled

    # Winner proved via the gate; goal depends_on rewired to the winner.
    winner_id = plan_state.node_id_for("via_duality", "Demo/Main_dirB.lean")
    assert bp.node_by_id(winner_id).status == "proved"
    goal_id = plan_state.node_id_for("goal", GOAL_FILE)
    rewired = [
        (e.source, e.target, e.kind)
        for e in bp.edges
        if e.source == goal_id and e.kind == "depends_on"
    ]
    assert (goal_id, winner_id, "depends_on") in rewired
    # And no depends_on edge points at the losing direction.
    loser_id = plan_state.node_id_for("via_induction", "Demo/Main_dirA.lean")
    assert (goal_id, loser_id, "depends_on") not in rewired

    # The choice is in the decision log, keyed by the goal NODE (file-scoped:
    # same-named goals in other files can never collide).
    packets = plan_state.load_summary()["decision_packets"]
    dir_packets = [p for p in packets if p.get("scope") == "multi-direction"]
    assert dir_packets and dir_packets[-1]["decision"] == "dirB"
    assert dir_packets[-1]["packet_id"] == f"dir-{goal_id}-dirB"


def test_winner_short_circuits_remaining_directions(project, monkeypatch):
    _fake_checker(monkeypatch)
    statements = _statements() + [
        {
            "name": "via_algebra",
            "statement": "lemma via_algebra : True := by sorry",
            "direction": "dirC",
        }
    ]
    verdicts = {"Demo/Main_dirA.lean": {"via_induction": "proved"}}
    _fake_gate(monkeypatch, verdicts)
    service = _FakeService(verdicts)

    outcome = md.run_multi_direction(
        goal_symbol="goal",
        goal_file=GOAL_FILE,
        statements_to_state=statements,
        cwd=str(project),
        service=service,
    )

    assert outcome.ok and outcome.winner == "dirA"
    statuses = {d.direction: d.status for d in outcome.directions}
    assert statuses["dirB"] == "skipped" and statuses["dirC"] == "skipped"
    assert not (project / "Demo/Main_dirB.lean").exists()  # never stated


def test_all_exhausted_leaves_packets(project, monkeypatch):
    _fake_checker(monkeypatch)
    verdicts = {
        "Demo/Main_dirA.lean": {"via_induction": "error"},
        "Demo/Main_dirB.lean": {"via_duality": "sorry"},
    }
    _fake_gate(monkeypatch, verdicts)
    service = _FakeService(verdicts)

    outcome = md.run_multi_direction(
        goal_symbol="goal",
        goal_file=GOAL_FILE,
        statements_to_state=_statements(),
        cwd=str(project),
        service=service,
    )

    assert not outcome.ok and "exhausted" in outcome.reason
    packets = [
        p
        for p in plan_state.load_summary()["decision_packets"]
        if p.get("scope") == "multi-direction"
    ]
    assert {p["direction"] for p in packets} == {"dirA", "dirB"}
    assert all(not p.get("decision") for p in packets)  # undecided: routes decide


def test_lying_service_cannot_fabricate_a_winner(project, monkeypatch):
    """Kernel truth: the ledger's account never decides — only OUR gate."""
    _fake_checker(monkeypatch)
    # The service CLAIMS everything proved; the local gate disagrees.
    _fake_gate(
        monkeypatch,
        {
            "Demo/Main_dirA.lean": {"via_induction": "error"},
            "Demo/Main_dirB.lean": {"via_duality": "error"},
        },
    )
    service = _FakeService(
        {
            "Demo/Main_dirA.lean": {"via_induction": "proved"},
            "Demo/Main_dirB.lean": {"via_duality": "proved"},
        }
    )

    outcome = md.run_multi_direction(
        goal_symbol="goal",
        goal_file=GOAL_FILE,
        statements_to_state=_statements(),
        cwd=str(project),
        service=service,
    )

    assert not outcome.ok and outcome.winner == ""
    # The transport's fabricated `proved` promotions did not survive: the
    # local gate's truth was enforced on the graph (reconcile downgraded).
    bp = plan_state.load_blueprint()
    for name, file in (
        ("via_induction", "Demo/Main_dirA.lean"),
        ("via_duality", "Demo/Main_dirB.lean"),
    ):
        node = bp.node_by_id(plan_state.node_id_for(name, file))
        assert node is not None and node.status != "proved"


def test_mixed_tagged_untagged_fails_closed():
    """One untagged statable entry disables multi-direction entirely —
    the single-direction path must see the WHOLE list (nothing dropped)."""
    mixed = _statements() + [{"name": "solo", "statement": "lemma solo : True := by sorry"}]
    assert md.directions_from_statements(mixed) == {}


def test_name_mismatch_rejected(project, monkeypatch):
    _fake_checker(monkeypatch)

    _rel, _names, err = md.state_direction_file(
        direction="dirA",
        statements=[{"name": "claimed_name", "statement": "lemma real_name : True := by sorry"}],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert "statement name mismatch" in err
    assert not (project / "Demo/Main_dirA.lean").exists()


def test_dangling_symlink_cannot_be_written_through(project, monkeypatch):
    _fake_checker(monkeypatch)
    target = project / "outside-target.lean"
    (project / "Demo/Main_dirA.lean").symlink_to(target)  # dangling

    _rel, _names, err = md.state_direction_file(
        direction="dirA",
        statements=[{"name": "x", "statement": "lemma x : True := by sorry"}],
        goal_file=GOAL_FILE,
        cwd=str(project),
    )

    assert "already exists" in err
    assert not target.exists()  # nothing written through the symlink


def test_dispatch_disabled_fails_soft(project, monkeypatch):
    monkeypatch.delenv("LEANFLOW_DISPATCH_ENABLED", raising=False)
    outcome = md.run_multi_direction(
        goal_symbol="goal",
        goal_file=GOAL_FILE,
        statements_to_state=_statements(),
        cwd=str(project),
    )
    assert not outcome.ok and "dispatch is disabled" in outcome.reason


# ---------------------------------------------------------------------------
# Runner wiring: dark unless direction tags + success path
# ---------------------------------------------------------------------------


def _apply_route(route: OrchestratorRoute, history: list) -> str:
    return runner._orchestrator_apply_route(
        route,
        history,
        {"_orchestrator_last_ctx": {"target_symbol": "goal", "active_file": GOAL_FILE}},
        {},
        agent=None,
    )


def test_runner_uses_multi_direction_for_tagged_statements(project, monkeypatch):
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.multi_direction,
        "run_multi_direction",
        lambda **kwargs: md.MultiDirectionOutcome(ok=True, winner="dirB", reason="won"),
    )
    history: list[dict] = []

    action = _apply_route(
        OrchestratorRoute(
            route="decompose",
            reason="llm directions",
            target={"statements_to_state": _statements()},
        ),
        history,
    )

    assert action == "continue"
    assert history and "direction 'dirB' fully proved" in history[-1]["content"]


def test_runner_ignores_untagged_statements(project, monkeypatch):
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    def explode(**kwargs):
        raise AssertionError("multi-direction must not run without direction tags")

    monkeypatch.setattr(runner.multi_direction, "run_multi_direction", explode)
    history: list[dict] = []

    action = _apply_route(
        OrchestratorRoute(
            route="decompose",
            reason="plain decompose",
            target={"statements_to_state": [{"name": "a", "statement": "s"}]},
        ),
        history,
    )

    assert action == "continue"  # normal decompose path (directive fallback)


def test_runner_falls_through_when_no_winner(project, monkeypatch):
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.multi_direction,
        "run_multi_direction",
        lambda **kwargs: md.MultiDirectionOutcome(ok=False, reason="all directions exhausted"),
    )
    history: list[dict] = []

    action = _apply_route(
        OrchestratorRoute(
            route="decompose",
            reason="llm directions",
            target={"statements_to_state": _statements()},
        ),
        history,
    )

    assert action == "continue"
    # Fell through to the directive fallback, not the winner banner.
    assert history and "fully proved" not in history[-1]["content"]
