"""Recover pre-failure submissions without trusting saved acceptance or resetting budgets."""

import json
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from tests.leanflow.test_prover_check_failures import LAKE_ERROR
from tests.leanflow.test_prover_runtime import (
    ProverConfig,
    ProverRuntime,
    Verifier,
    project,
    session,
)


@pytest.mark.parametrize("infrastructure", [True, False])
def test_resume_recovers_older_candidate_only_after_environment_failure(
    tmp_path: Path, infrastructure: bool
) -> None:
    target = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[target], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    result = {**session(), "status": "completed"}
    runtime._finish_job(job, result)
    job["result_processed"] = True
    # Old releases consumed the result despite a broken verification environment.
    runtime.store.event(
        "candidate_checked",
        {
            "node_id": node.id,
            "accepted": False,
            "result": {
                "accepted": False,
                "error": LAKE_ERROR if infrastructure else "unsolved goals",
            },
        },
    )
    retry, _ = runtime._new_job("prover", node=node)
    with pytest.raises(InfrastructureFailure):
        runtime._finish_job(
            retry, {"status": "error", "api_calls": 1, "error": "controller stopped"}
        )
    ledger = Path(retry["workspace"]).parent / ".runtime" / retry["id"] / "request-count.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"used": 1}))
    node.status = "retry"
    runtime.state.update(status="environment_error", phase="environment_error")
    runtime._persist()
    calls = runtime.state["metrics"]["api_calls"]
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(),
        session=session,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.state["metrics"]["api_calls"] == calls
    assert resumed.dag.nodes[0].candidate == (["trivial"] if infrastructure else [])
    assert resumed.dag.nodes[0].status == ("candidate" if infrastructure else "retry")
    if infrastructure:
        assert resumed.state["jobs"][-1]["status"] == "interrupted"
    assert resumed.verifier.checked == [], "Saved results never count as independent acceptance"
    if infrastructure:
        # A second stop during re-verification must not revive the empty retry
        # and hide the already recovered candidate behind it again.
        resumed.state.update(status="environment_error", phase="environment_error")
        resumed._persist()
        again = ProverRuntime(
            root=tmp_path,
            targets=[target],
            config=ProverConfig(),
            session=session,
            verifier=Verifier(),
            run_id=runtime.run_id,
            resume=True,
        )
        assert again.dag.nodes[0].candidate == ["trivial"]
        assert again.dag.nodes[0].status == "candidate"
        assert node.id not in again.resume_jobs
        assert again.state["metrics"]["api_calls"] == calls


def test_later_resume_never_retries_an_integrated_node(tmp_path: Path) -> None:
    target = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[target], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, {**session(), "status": "completed"})
    runtime._accept(node, ["trivial"], job["id"])
    # Characterize an old snapshot with an abandoned resume entry after integration.
    job["status"] = "resume_pending"
    ledger = Path(job["workspace"]).parent / ".runtime" / job["id"] / "request-count.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"used": 1}))
    runtime._persist()
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(),
        session=session,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.dag.nodes[0].status == "proved"
    assert node.id not in resumed.resume_jobs


def test_interrupt_during_promotion_keeps_the_unverified_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[target], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]
    node.candidate, node.status = ["trivial"], "candidate"

    def interrupt(*args: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "_accept", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime._promote_candidates()
    assert node.candidate == ["trivial"] and node.status == "candidate"


@pytest.mark.parametrize("verdict_recorded", [False, True])
def test_resume_recovers_candidate_cleared_by_an_older_interrupted_check(
    tmp_path: Path, verdict_recorded: bool
) -> None:
    target = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[target], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, {**session(), "status": "completed"})
    job["result_processed"] = True
    node.status, node.candidate = "candidate", []
    runtime.state.update(
        status="interrupted",
        phase="interrupted",
        operations=[
            {
                "kind": "submission_check",
                "status": "failed",
                "node_id": node.id,
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    if verdict_recorded:
        runtime.store.event(
            "candidate_checked",
            {"node_id": node.id, "accepted": False, "result": {"error": "unsolved goals"}},
        )
    runtime._persist()
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(),
        session=session,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.dag.nodes[0].candidate == ([] if verdict_recorded else ["trivial"])
    assert resumed.dag.nodes[0].status == "candidate"
    assert resumed.verifier.checked == []
