"""Reproduce interrupted planning, cancellation, accounting, and source transactions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.planning import apply_proposal
from leanflow_cli.workflows.prover.planning_controller import materialize
from leanflow_cli.workflows.prover.runtime import ProverRuntime


class Verifier:
    def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"accepted": True}

    def compile_module(self, relative: str) -> dict[str, Any]:
        return {"accepted": True}

    def final(self, *args: Any) -> dict[str, Any]:
        return {"accepted": True}


def runtime_at(root: Path, **options: Any) -> ProverRuntime:
    if not (root / "Main.lean").exists():
        (root / "Main.lean").write_text("theorem goal : True := by sorry\n")
        (root / "lakefile.toml").write_text('name = "Demo"\n')
    return ProverRuntime(
        root=root,
        targets=[root / "Main.lean"],
        config=ProverConfig(mode="research"),
        verifier=Verifier(),
        **options,
    )


@pytest.mark.parametrize("phase", ["inspect", "preflight", "planning", "reviewing"])
def test_resume_unfinished_research_still_plans(tmp_path: Path, phase: str) -> None:
    roles = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        result = (
            {"accepted": True}
            if kwargs["role"] == "review"
            else (
                {"proof": "trivial"}
                if kwargs["role"] == "prover"
                else {"plan": "Use triviality", "nodes": []}
            )
        )
        return {"status": "candidate", "api_calls": 1, "final_response": json.dumps(result)}

    first = runtime_at(tmp_path, session=session)
    first.state["phase"] = phase
    first._persist()
    resumed = runtime_at(tmp_path, session=session, run_id=first.run_id, resume=True)
    assert resumed.run()["status"] == "completed"
    assert roles[:3] == ["orchestrator", "orchestrator", "review"]


def test_cancelled_outline_never_starts_another_model_job(tmp_path: Path) -> None:
    roles = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        runtime.cancelled.set()
        return {"status": "interrupted", "api_calls": 1, "final_response": ""}

    runtime = runtime_at(tmp_path, session=session)
    result = runtime.run()
    assert result["status"] == "interrupted"
    assert roles == ["orchestrator"]
    assert result["metrics"]["reserved_api_calls"] == 0


def test_job_reconciliation_is_idempotent(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    job, _ = runtime._new_job("research", prompt="bounded question")
    result = {"status": "complete", "api_calls": 2}
    runtime._finish_job(job, result)
    runtime._finish_job(job, result)
    assert runtime.consumed == 2
    assert runtime.reserved == 0


def _proposal(runtime: ProverRuntime) -> tuple[Any, Any]:
    node = runtime.dag.nodes[0]
    return apply_proposal(
        runtime.dag,
        {
            "nodes": [
                {
                    "id": "helper",
                    "name": "help",
                    "statement": "theorem help : True ∧ True := by sorry",
                    "file": "LeanFlowProofs/Help.lean",
                },
                {"id": node.id, "dependencies": ["helper"]},
            ]
        },
        max_nodes=10,
    )


def test_materialization_crash_recovers_sources_and_registration(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    dag, skeletons = _proposal(runtime)
    runtime.verifier.compile_module = lambda relative: (_ for _ in ()).throw(SystemExit("crash"))
    with pytest.raises(SystemExit):
        materialize(runtime, dag, skeletons)
    resumed = runtime_at(tmp_path, session=lambda **kw: {}, run_id=runtime.run_id, resume=True)
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert not (tmp_path / "LeanFlowProofs/Help.lean").exists()
    assert len(resumed.dag.nodes) == 1


def test_materialization_recovery_preserves_external_edits(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    dag, skeletons = _proposal(runtime)
    runtime.verifier.compile_module = lambda relative: (_ for _ in ()).throw(SystemExit())
    with pytest.raises(SystemExit):
        materialize(runtime, dag, skeletons)
    helper = tmp_path / "LeanFlowProofs/Help.lean"
    helper.write_text("-- human correction\n" + helper.read_text())
    before = helper.read_bytes()
    with pytest.raises(ValueError, match="source changed"):
        runtime_at(tmp_path, session=lambda **kw: {}, run_id=runtime.run_id, resume=True)
    assert helper.read_bytes() == before


def test_helper_restatement_of_ancestor_is_not_decomposition(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    with pytest.raises(ValueError, match="restates"):
        node = runtime.dag.nodes[0]
        apply_proposal(
            runtime.dag,
            {
                "nodes": [
                    {
                        "id": "copy",
                        "name": "copy",
                        "statement": "theorem copy : True := by sorry",
                        "file": "LeanFlowProofs/Copy.lean",
                    },
                    {"id": node.id, "dependencies": ["copy"]},
                ]
            },
            max_nodes=10,
        )


@pytest.mark.parametrize("crash_stage", [0, 1, 2])
def test_resume_each_planning_job_preserves_its_allocation_and_finished_reports(
    tmp_path: Path, crash_stage: int
) -> None:
    calls = []
    crashed = False

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal crashed
        calls.append(kwargs["context"]["job_id"])
        workspace = Path(kwargs["workspace"])
        ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if not crashed and len(calls) - 1 == crash_stage:
            crashed = True
            ledger.write_text(json.dumps({"used": 1, "limit": kwargs["api_budget"]}))
            raise SystemExit("interrupted planning job")
        used = 2 if ledger.exists() else 1
        report = (
            {"accepted": True}
            if kwargs["role"] == "review"
            else (
                {"proof": "trivial"}
                if kwargs["role"] == "prover"
                else {"plan": "Concrete plan", "nodes": []}
            )
        )
        return {"status": "candidate", "api_calls": used, "final_response": json.dumps(report)}

    first = runtime_at(tmp_path, session=session)
    with pytest.raises(SystemExit):
        first.run()
    resumed = runtime_at(tmp_path, session=session, run_id=first.run_id, resume=True)
    result = resumed.run()
    assert result["status"] == "completed", result.get("error")
    assert len(result["jobs"]) == 4
    assert len(calls) == 5
    assert result["metrics"]["api_calls"] == 5
    assert result["metrics"]["reserved_api_calls"] == 0
    assert result["jobs"][crash_stage]["resumed"] is True


@pytest.mark.parametrize("commit_state", [False, True])
def test_materialization_recovers_before_or_after_atomic_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_state: bool
) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    before = (tmp_path / "Main.lean").read_bytes()
    dag, skeletons = _proposal(runtime)
    if commit_state:
        persist = runtime._persist

        def crash() -> None:
            persist()
            raise SystemExit("after state checkpoint")

        monkeypatch.setattr(runtime, "_persist", crash)
    else:
        from leanflow_cli.workflows.prover import store

        write = store.atomic_json_write

        def crash_write(path: Path, data: Any) -> None:
            if path.name == "state.json":
                raise SystemExit("after source checkpoint")
            write(path, data)

        monkeypatch.setattr(store, "atomic_json_write", crash_write)
    with pytest.raises(SystemExit):
        materialize(runtime, dag, skeletons)
    monkeypatch.undo()
    resumed = runtime_at(tmp_path, session=lambda **kw: {}, run_id=runtime.run_id, resume=True)
    assert ((tmp_path / "Main.lean").read_bytes() != before) is commit_state
    assert (len(resumed.dag.nodes) == 2) is commit_state
    assert (tmp_path / "LeanFlowProofs/Help.lean").exists() is commit_state


def test_compile_failure_never_rolls_back_over_an_external_source_edit(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    node = runtime.dag.nodes[0]
    runtime.documents[node.file].generated = True
    runtime._persist()
    path = tmp_path / node.file

    def compile(relative: str) -> dict[str, Any]:
        path.write_text("-- human edit during compilation\n" + path.read_text())
        return {"accepted": False, "error": "compile failed"}

    runtime.verifier.compile_module = compile
    with pytest.raises(Exception, match="source changed"):
        runtime._accept(node, ["trivial"], "controller")
    assert path.read_text().startswith("-- human edit during compilation")
    assert (runtime.store.directory / "source-transaction.json").exists()


def test_stale_provider_failure_preserves_newer_proved_node(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    node.revision += 1
    node.status = "proved"
    runtime._handle_result(
        job, {"status": "provider_error", "api_calls": 1, "error": "obsolete provider failure"}
    )
    assert node.status == "proved"
    assert job["status"] == "stale"
    assert runtime.consumed == 1 and runtime.reserved == 0


def test_dispatch_waits_for_unused_reservations_before_declaring_budget_exhausted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Main.lean"
    path.write_text("\n".join(f"theorem goal{i} : True := by sorry" for i in range(3)))
    (tmp_path / "lakefile.toml").write_text('name="Demo"\n')

    def session(**kwargs: Any) -> dict[str, Any]:
        name = kwargs["context"]["assignment"]["name"]
        if name == "goal1":
            time.sleep(0.08)
        return {
            "status": "candidate",
            "api_calls": {"goal0": 3, "goal1": 1, "goal2": 2}[name],
            "final_response": json.dumps({"proof": "trivial"}),
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research", parallelism=2, job_api_calls=3, total_api_calls=6),
        verifier=Verifier(),
        session=session,
    )
    runtime.state["phase"] = "resume"
    runtime.state["resume_phase"] = "proving"
    result = runtime.run()
    assert result["status"] == "completed", result.get("error")
    assert result["metrics"]["api_calls"] == 6
    assert result["metrics"]["reserved_api_calls"] == 0


def test_failed_job_preparation_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import job_controller

    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    monkeypatch.setattr(
        job_controller, "write_source", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert runtime.reserved == 0
    assert runtime.state["jobs"] == []


def test_original_proofs_refresh_importable_module(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    compiled = []
    runtime.verifier.compile_module = lambda relative: compiled.append(relative) or {
        "accepted": True
    }
    assert runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
    assert compiled == ["Main.lean"]


def test_invalid_negation_submission_is_uncertified(tmp_path: Path, monkeypatch) -> None:
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.models import Node
    from leanflow_cli.workflows.prover.negation import NegationTask

    runtime = runtime_at(
        tmp_path,
        session=lambda **kw: {
            "status": "candidate",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "trivial\ndef injected : Nat := 3"}),
        },
    )
    node = runtime.dag.nodes[0]
    task = NegationTask(
        Node("negative", "negative", "theorem negative : False", node.file, node.module, holes=[0]),
        "theorem negative : False := by sorry\n",
    )
    monkeypatch.setattr(negation_job, "prepare_negation", lambda *args: task)
    result = negation_job.attempt_negation(runtime, node)
    assert result["certified"] is False
    assert "replacement" in result["notes"]


def test_resume_revalidates_saved_dag_bound(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    runtime.state["config"]["max_nodes"] = 1
    runtime.dag, _ = _proposal(runtime)
    runtime._persist()
    with pytest.raises(ValueError, match="node"):
        runtime_at(tmp_path, session=lambda **kw: {}, run_id=runtime.run_id, resume=True)


@pytest.mark.parametrize("failure", ["provider_error", "interrupted"])
def test_shutdown_preserves_finished_sibling_candidate_for_zero_model_resume(
    tmp_path: Path, failure: str
) -> None:
    import threading

    source = tmp_path / "Main.lean"
    source.write_text("theorem first : True := by sorry\ntheorem second : True := by sorry\n")
    returned = threading.Event()

    def session(**kwargs):
        if kwargs["context"]["assignment"]["name"] == "first":
            returned.wait(2)
            return {"status": failure, "api_calls": 1, "error": "service unavailable"}
        returned.set()
        time.sleep(0.05)
        return {
            "status": "candidate",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "trivial"}),
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(mode="research", parallelism=2),
        verifier=Verifier(),
        session=session,
    )
    runtime.state.update(phase="resume", resume_phase="proving")
    result = runtime.run()
    assert result["status"] == failure
    assert runtime.dag.nodes[1].candidate == ["trivial"]
    assert runtime.dag.nodes[1].status == "candidate"
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(),
        verifier=Verifier(),
        session=lambda **kw: pytest.fail("candidate needs no model"),
        run_id=runtime.run_id,
        resume=True,
    )
    resumed._promote_candidates()
    assert resumed.dag.nodes[1].status == "proved"


def test_crash_after_job_report_before_dag_handoff_preserves_candidate(tmp_path: Path) -> None:
    first = runtime_at(tmp_path, session=lambda **kw: {})
    node = first.dag.nodes[0]
    job, _ = first._new_job("prover", node=node)
    node.status = "running"
    first._finish_job(
        job,
        {"status": "candidate", "api_calls": 1, "final_response": json.dumps({"proof": "trivial"})},
    )
    resumed = runtime_at(
        tmp_path,
        run_id=first.run_id,
        resume=True,
        session=lambda **kw: pytest.fail("no repeated model request"),
    )
    resumed._promote_candidates()
    assert resumed.dag.nodes[0].status == "proved"
    assert resumed.consumed == 1


def test_accepted_plan_resumes_only_missing_research_report(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.planning_controller import research_plan

    seen = []
    runtime = runtime_at(
        tmp_path,
        session=lambda **kw: seen.append(kw["role"])
        or {"status": "complete", "api_calls": 1, "final_response": "retained resource evidence"},
    )
    runtime.state["planning_request"] = {
        "reason": "research",
        "affected": None,
        "refinement": True,
        "refinement_charged": True,
        "steps": {},
        "accepted_proposal": {
            "plan": "accepted outline",
            "research_jobs": [{"question": "first"}, {"question": "second"}],
        },
        "research_jobs": {},
    }
    runtime.state["metrics"]["plan_refinements"] = 1
    prompt = "Investigate this bounded question; save resources locally and return concrete evidence with source paths. Do not prove Lean declarations.\nfirst"
    job, _ = runtime._new_job("research", prompt=prompt)
    runtime.state["planning_request"]["research_jobs"]["0"] = job["id"]
    runtime._finish_job(
        job, {"status": "complete", "api_calls": 1, "final_response": "finished first report"}
    )
    resumed = runtime_at(tmp_path, session=runtime.session, run_id=runtime.run_id, resume=True)
    assert research_plan(resumed, "ignored restart reason")
    assert seen == ["research"]
    assert "finished first report" in resumed.state["plan_markdown"]
    assert resumed.state["metrics"]["plan_refinements"] == 1
    assert resumed.consumed == 2
    assert "planning_request" not in resumed.state


def test_materialization_detects_edit_before_first_source_write(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path, session=lambda **kw: {})
    dag, skeletons = _proposal(runtime)
    source = tmp_path / "Main.lean"
    original = source.read_text()

    def compile(relative: str) -> dict[str, Any]:
        source.write_text("-- external edit\n" + original)
        return {"accepted": True}

    runtime.verifier.compile_module = compile
    with pytest.raises(Exception, match="source changed"):
        materialize(runtime, dag, skeletons)
    assert source.read_text() == "-- external edit\n" + original
