"""Require proof artifacts, rather than optimistic reports, to renew prover jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime


class RejectingVerifier:
    """Keep complete candidates at the retry decision for these controller tests."""

    def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"accepted": False}


@pytest.fixture
def runtime(tmp_path: Path) -> ProverRuntime:
    """Create one protected goal with no provider or Lean process dependency."""
    target = tmp_path / "Main.lean"
    target.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    return ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(job_api_calls=2, max_restarts=3),
        session=lambda **kwargs: {},
        verifier=RejectingVerifier(),
    )


def finish(
    runtime: ProverRuntime,
    *,
    replacement: str | None = None,
    notes: str = "",
    promising: bool = False,
    proof: str | None = None,
    outside_edit: bool = False,
) -> dict[str, Any]:
    """Finish a dispatched job with optional work in its authorized scratch hole."""
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    node.attempts += 1
    node.status = "running"
    scratch = Path(job["scratch_path"])
    if replacement is not None:
        scratch.write_text(runtime.scratch_before[job["id"]].replace("sorry", replacement))
    if outside_edit:
        scratch.write_text(scratch.read_text().replace("goal : True", "goal : False"))
    report: dict[str, Any] = {"notes": notes, "promising": promising}
    if proof is not None:
        report["proof"] = proof
    runtime._handle_result(
        job,
        {
            "status": "budget_exhausted",
            "api_calls": 2,
            "final_response": json.dumps(report),
        },
    )
    return job


@pytest.mark.parametrize(
    ("notes", "promising"),
    [("I need more time to investigate.", False), ("", True), ("Next attempt looks good.", True)],
)
def test_report_alone_does_not_renew(runtime: ProverRuntime, notes: str, promising: bool) -> None:
    finish(runtime, notes=notes, promising=promising)
    assert runtime.dag.nodes[0].status == "blocked"
    assert runtime.consumed == 2


def test_partial_hole_work_renews_without_promising_claim(runtime: ProverRuntime) -> None:
    finish(runtime, replacement="\n  have h : True := by sorry\n  exact h")
    assert runtime.dag.nodes[0].status == "retry"
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert "have h : True := by sorry" in Path(job["scratch_path"]).read_text()
    assert (runtime.root / "Main.lean").read_text() == "theorem goal : True := by sorry\n"


def test_carried_partial_work_does_not_renew_again(runtime: ProverRuntime) -> None:
    finish(runtime, replacement="\n  have h : True := by sorry\n  exact h", notes="partial")
    assert runtime.dag.nodes[0].status == "retry"
    finish(runtime, notes="I will try another approach next time.", promising=True)
    assert runtime.dag.nodes[0].status == "blocked"


def test_new_partial_work_is_compared_with_previous_attempt(runtime: ProverRuntime) -> None:
    finish(runtime, replacement="\n  have h : True := by sorry\n  exact h", notes="partial")
    finish(runtime, replacement="\n  have h : True := by trivial\n  sorry")
    assert runtime.dag.nodes[0].status == "retry"


def test_previous_partial_is_loaded_after_controller_restart(runtime: ProverRuntime) -> None:
    finish(runtime, replacement="\n  have h : True := by sorry\n  exact h", notes="partial")
    runtime.scratch_before.clear()
    finish(runtime, notes="I still need more time.", promising=True)
    assert runtime.dag.nodes[0].status == "blocked"


def test_comments_on_carried_partial_work_do_not_renew(runtime: ProverRuntime) -> None:
    partial = "\n  have h : True := by sorry\n  exact h"
    finish(runtime, replacement=partial, notes="partial")
    finish(runtime, replacement=partial + " -- still working", notes="another report")
    assert runtime.dag.nodes[0].status == "blocked"


def test_previous_revision_does_not_suppress_new_partial_work(runtime: ProverRuntime) -> None:
    partial = "\n  have h : True := by sorry\n  exact h"
    finish(runtime, replacement=partial, notes="partial")
    runtime.dag.nodes[0].revision += 1
    finish(runtime, replacement=partial)
    assert runtime.dag.nodes[0].status == "retry"


@pytest.mark.parametrize(
    "replacement",
    ["  sorry -- still investigating", "sorry\ntheorem extra : True := by sorry"],
)
def test_comments_or_commands_do_not_count_as_proof_progress(
    runtime: ProverRuntime, replacement: str
) -> None:
    finish(runtime, replacement=replacement, notes="changed scratch", promising=True)
    assert runtime.dag.nodes[0].status == "blocked"


def test_changes_outside_authorized_holes_do_not_renew(runtime: ProverRuntime) -> None:
    finish(
        runtime,
        replacement="\n  have h : True := by sorry\n  exact h",
        notes="changed statement",
        promising=True,
        outside_edit=True,
    )
    assert runtime.dag.nodes[0].status == "blocked"


def test_concrete_candidate_can_renew_after_parent_rejection(runtime: ProverRuntime) -> None:
    finish(runtime, proof="exact True.intro")
    assert runtime.dag.nodes[0].status == "retry"


def test_concrete_progress_still_obeys_restart_cap(runtime: ProverRuntime) -> None:
    runtime.dag.nodes[0].attempts = runtime.config.max_restarts
    finish(runtime, replacement="\n  have h : True := by sorry\n  exact h")
    assert runtime.dag.nodes[0].status == "blocked"
