"""Require independent, source-grounded native reviews before formalization promotion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.runtime.harness import BackendError, HarnessResult, IsolationPolicy
from core.campaign_limits import CampaignLimits
from leanflow_cli.native import harness_formalization as review
from leanflow_cli.native import harness_workflow as workflow
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation


def _verdict(**changes):
    return {
        "decision": "PASS",
        "coverage_complete": True,
        "definitions_complete": True,
        "feedback": "The source definition and theorem are represented faithfully; the theorem remains a proof obligation.",
        "evidence": [
            {
                "source_reference": "Section 1, theorem 1",
                "lean_file": "Main.lean",
                "assessment": "Same quantifiers and conclusion.",
            }
        ],
        **changes,
    }


@pytest.fixture
def run_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    requests = []
    monkeypatch.setattr(
        review, "build_runtime_isolation", lambda backend, cwd, **kwargs: IsolationPolicy(cwd)
    )

    def invoke(
        response: str, *, status: str = "completed", calls: int | None = None, failure: bool = False
    ):
        budget = CampaignLimits(tmp_path / "limits.json", {"agent_calls": calls})

        def native(request, on_event=None):
            requests.append(request)
            if failure:
                raise BackendError("native configuration unavailable")
            request.on_started()
            return HarnessResult(
                status,
                response,
                "independent-review",
                {"input_tokens": 5, "output_tokens": 7},
                error=response if status != "completed" else "",
            )

        monkeypatch.setattr(review, "run_harness", native)
        result = review.review_formalization(
            receipt={
                "accepted": True,
                "files": {
                    "Main.lean": "theorem source_statement : True := by sorry\n",
                    "Blueprint.md": "Section 1 maps to source_statement; proof remains.",
                },
            },
            source_context="Section 1: theorem 1 states True.",
            backend="codex",
            environment={"OPENAI_API_KEY": "test-owned-key"},
            directory=tmp_path / "reviews",
            budget=budget,
        )
        return result, budget, requests

    return invoke


def test_faithful_draft_review_preserves_native_defaults_and_charges_once(run_review):
    result, budget, requests = run_review(json.dumps(_verdict()))
    assert result["accepted"]
    assert result["status"] == "completed"
    request = requests[0]
    assert request.session_id == ""
    assert request.previous_usage is None
    assert request.model == ""
    assert request.effort == ""
    assert request.tools is False
    assert "Theorems may contain clearly identified" in request.prompt
    assert "Definitions, data structures" in request.prompt
    assert request.env["OPENAI_API_KEY"] == "test-owned-key"
    assert len(budget.state["invocations"]) == 1
    call = next(iter(budget.state["invocations"].values()))
    assert call["status"] == "finished"
    assert call["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert result["source_context_sha256"]
    assert set(result["proposal_sha256"]) == {"Main.lean", "Blueprint.md"}


@pytest.mark.parametrize(
    "response",
    [
        "PASS",
        "```json\n{}\n```",
        "{}",
        json.dumps(_verdict(coverage_complete=False)),
        json.dumps(_verdict(definitions_complete=False)),
        json.dumps(_verdict(evidence=[])),
        json.dumps(
            _verdict(
                evidence=[
                    {
                        "source_reference": "Section 1",
                        "lean_file": "Blueprint.md",
                        "assessment": "The outline looks reasonable.",
                    }
                ]
            )
        ),
        json.dumps(
            _verdict(
                evidence=[
                    {
                        "source_reference": "Section 1",
                        "lean_file": "Unknown.lean",
                        "assessment": "Okay",
                    }
                ]
            )
        ),
    ],
)
def test_malformed_or_unsupported_pass_cannot_accept(run_review, response):
    result, _, _ = run_review(response)
    assert result["accepted"] is False
    assert result["status"] == "invalid_review"
    assert result["decision"] == "BLOCK"


def test_block_feedback_is_actionable_and_native_failure_distinct(run_review):
    result, _, _ = run_review(
        json.dumps(
            _verdict(
                decision="BLOCK",
                coverage_complete=False,
                feedback="Add the source's missing positivity assumption.",
            )
        )
    )
    assert result["status"] == "completed"
    assert result["accepted"] is False
    assert "positivity" in result["feedback"]


def test_native_weekly_limit_is_not_a_formalization_rejection(run_review):
    result, _, _ = run_review("You've hit your weekly limit", status="failed")
    assert result["status"] == "failed"
    assert result["accepted"] is False
    assert "weekly limit" in result["error"]


def test_pass_review_evidence_must_cover_every_proposed_lean_file():
    decision = review._decision(
        json.dumps(_verdict()),
        {"Main.lean": "theorem p : True := by sorry", "Extra.lean": "def q : Nat := 1"},
    )
    assert decision["accepted"] is False
    assert decision["status"] == "invalid_review"
    assert "every proposed Lean file" in decision["feedback"]


def test_exhausted_global_call_limit_does_not_launch_review(run_review):
    result, budget, requests = run_review(json.dumps(_verdict()), calls=0)
    assert result["status"] == "limit_reached"
    assert requests == []
    assert budget.state["invocations"] == {}


def test_prelaunch_failure_releases_call_reservation(run_review):
    result, budget, _ = run_review("", failure=True)
    assert result["status"] == "startup_error"
    assert next(iter(budget.state["invocations"].values()))["status"] == "not_started"


@pytest.fixture
def formalization_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real workflow and reviewer with only native and Lean execution stubbed."""
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda project: []
    )
    monkeypatch.delenv("LEAN_PATH", raising=False)
    source = tmp_path / "Main.lean"
    original = "-- Existing project\n"
    source.write_text(original)
    requests = []
    checks = []
    monkeypatch.setattr(
        workflow, "build_runtime_isolation", lambda backend, cwd, **kwargs: IsolationPolicy(cwd)
    )
    monkeypatch.setattr(
        review, "build_runtime_isolation", lambda backend, cwd, **kwargs: IsolationPolicy(cwd)
    )

    def verify(**kwargs):
        assert source.read_text() == original
        files = {
            name: (kwargs["snapshot"] / name).read_text() for name in ("Main.lean", "Blueprint.md")
        }
        checks.append(files)
        return {
            "accepted": True,
            "environment_generation": lean_environment_generation(tmp_path),
            "files": files,
            "sha256": {
                name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()
            },
        }

    monkeypatch.setattr(workflow, "verify_proposal", verify)

    def invoke(*, verdicts, calls, startup_error=False):
        remaining_verdicts = iter(verdicts)

        def native(request, on_event=None):
            requests.append(request)
            assert source.read_text() == original
            if not request.tools and startup_error:
                raise BackendError("Reviewer authentication unavailable")
            request.on_started()
            if request.tools:
                (request.cwd / "Main.lean").write_text(
                    "theorem source_statement : True := by sorry\n"
                )
                (request.cwd / "Blueprint.md").write_text(
                    "Source theorem 1 maps to source_statement. The proof remains open."
                )
                return HarnessResult(
                    "completed",
                    "Draft ready.",
                    "draft-session",
                    {"input_tokens": 5, "output_tokens": 5},
                )
            assert checks, "The independent source review must follow Lean acceptance"
            assert json.dumps(checks[-1]["Main.lean"]) in request.prompt
            assert "Source theorem 1 states True" in request.prompt
            return HarnessResult(
                "completed",
                json.dumps(next(remaining_verdicts)),
                "review-session",
                {"input_tokens": 7, "output_tokens": 3},
            )

        monkeypatch.setattr(workflow, "run_harness", native)
        monkeypatch.setattr(review, "run_harness", native)
        result = workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_NATIVE_PROVIDER": "claude",
                "LEANFLOW_FORMALIZATION_CONTEXT": "Source theorem 1 states True.",
                "LEANFLOW_PROVER_AGENT_CALLS": str(calls),
            }
        )
        directory = tmp_path / ".leanflow/native-workflows" / result["run_id"]
        accounting = json.loads((directory / "limits.json").read_text())
        return result, accounting, requests, source, checks

    return invoke


def test_workflow_promotes_only_after_fresh_same_backend_source_review(formalization_workflow):
    result, accounting, requests, source, checks = formalization_workflow(
        verdicts=[_verdict()], calls=2
    )
    assert result["status"] == "draft_ready"
    assert result["source_review"]["accepted"] is True
    assert source.read_text() == checks[0]["Main.lean"]
    assert len(requests) == 2
    writer, reviewer = requests
    assert writer.tools is True and reviewer.tools is False
    assert writer.backend == reviewer.backend == "claude"
    assert writer.model == reviewer.model == writer.effort == reviewer.effort == ""
    assert reviewer.session_id == "" and reviewer.previous_usage is None
    assert reviewer.cwd != writer.cwd
    assert len(accounting["invocations"]) == 2
    assert all(call["status"] == "finished" for call in accounting["invocations"].values())


def test_workflow_source_block_feedback_continues_until_shared_call_limit(formalization_workflow):
    blocked = _verdict(
        decision="BLOCK",
        coverage_complete=False,
        feedback="Restore the missing positivity assumption.",
    )
    result, accounting, requests, source, checks = formalization_workflow(
        verdicts=[blocked, blocked], calls=4
    )
    assert result["status"] == "limit_reached"
    assert source.read_text() == "-- Existing project\n"
    assert len(checks) == 2 and len(requests) == 4
    assert "positivity assumption" in requests[2].prompt
    assert requests[2].session_id == "draft-session"
    assert requests[1].session_id == requests[3].session_id == ""
    assert len(accounting["invocations"]) == 4
    assert "promoted_files" not in result


def test_workflow_review_startup_failure_never_claims_completion(formalization_workflow):
    result, accounting, requests, source, _ = formalization_workflow(
        verdicts=[], calls=2, startup_error=True
    )
    assert result["status"] == "blocked"
    assert result["source_review"]["status"] == "startup_error"
    assert "authentication unavailable" in result["error"]
    assert source.read_text() == "-- Existing project\n"
    assert len(requests) == 2
    assert [call["status"] for call in accounting["invocations"].values()] == [
        "finished",
        "not_started",
    ]
    assert "promoted_files" not in result
