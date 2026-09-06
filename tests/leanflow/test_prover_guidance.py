"""Exercise addressed guidance, durable offsets, selected skills and dependency search."""

import json
from pathlib import Path

from leanflow_cli.workflows.prover.session_guidance import GuidanceInbox, skill_guidance
from leanflow_cli.workflows.prover.session_tools import SessionTools


def test_guidance_is_addressed_and_does_not_replay_after_resume(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    context = {"run_id": "run", "job_id": "worker-1"}
    reader = GuidanceInbox(tmp_path, ledger, context, "prover")
    inbox.write_text(
        "\n".join(
            json.dumps({"agent_id": who, "message": who})
            for who in ["orchestrator", "worker-2", "worker-1"]
        )
        + "\n"
    )
    assert [message["message"] for message in reader.poll()] == ["worker-1"]
    assert GuidanceInbox(tmp_path, ledger, context, "prover").poll() == []


def test_new_session_does_not_repeat_old_plan_guidance(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text('{"agent_id":"orchestrator","message":"old"}\n')
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    reader = GuidanceInbox(tmp_path, ledger, {"run_id": "run"}, "orchestrator")
    assert reader.poll() == []
    with inbox.open("a") as handle:
        handle.write('{"agent_id":"orchestrator","message":"new"}\n')
    assert reader.poll()[0]["message"] == "new"


def test_guidance_addressed_before_session_start_is_not_skipped(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text('{"agent_id":"worker-1","message":"Use induction on n."}\n')
    ledger = tmp_path / "runtime"
    ledger.mkdir()

    reader = GuidanceInbox(tmp_path, ledger, {"run_id": "run", "job_id": "worker-1"}, "prover")

    assert [item["message"] for item in reader.poll()] == ["Use induction on n."]


def test_consumed_guidance_remains_in_the_resumed_pinned_contract(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    context = {"run_id": "run", "job_id": "worker-1"}
    reader = GuidanceInbox(tmp_path, ledger, context, "prover")
    inbox.write_text('{"agent_id":"worker-1","message":"Keep the endpoint case separate."}\n')
    assert len(reader.poll()) == 1

    resumed = GuidanceInbox(tmp_path, ledger, context, "prover")

    assert resumed.poll() == []
    assert "Keep the endpoint case separate." in resumed.contract()
    assert resumed.contract() == reader.contract()


def test_manager_starts_at_the_offset_incorporated_into_its_plan(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    old = '{"agent_id":"orchestrator","message":"Already in the supplied PLAN."}\n'
    inbox.write_text(old)
    context = {"run_id": "run", "inbox_offset": inbox.stat().st_size}
    with inbox.open("a") as handle:
        handle.write('{"agent_id":"orchestrator","message":"Arrived after PLAN capture."}\n')
    ledger = tmp_path / "runtime"
    ledger.mkdir()

    reader = GuidanceInbox(tmp_path, ledger, context, "orchestrator")

    assert [item["message"] for item in reader.poll()] == ["Arrived after PLAN capture."]


def test_guidance_contract_has_a_bounded_retention_window(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    context = {"run_id": "run", "job_id": "worker-1"}
    reader = GuidanceInbox(tmp_path, ledger, context, "prover")
    inbox.write_text(
        "".join(
            json.dumps({"agent_id": "worker-1", "message": f"Observation {index}: " + "x" * 1000})
            + "\n"
            for index in range(40)
        )
    )

    assert len(reader.poll()) == 40
    contract = reader.contract()

    assert len(contract) <= 16000
    assert "Observation 39:" in contract
    assert "Observation 0:" not in contract
    assert "Earlier guidance outside this bounded window" in contract
    assert GuidanceInbox(tmp_path, ledger, context, "prover").contract() == contract


def test_cursor_only_ledger_upgrade_recovers_previously_consumed_guidance(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text('{"agent_id":"worker-1","message":"Preserve the strict inequality."}\n')
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    (ledger / "guidance-offset.json").write_text(json.dumps({"offset": inbox.stat().st_size}))

    reader = GuidanceInbox(tmp_path, ledger, {"run_id": "run", "job_id": "worker-1"}, "prover")
    reader.poll()

    assert "Preserve the strict inequality." in reader.contract()


def test_direct_planner_guidance_is_not_assumed_to_be_in_the_shared_plan(tmp_path: Path) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text('{"agent_id":"planner-1","message":"Review the boundary hypothesis."}\n')
    ledger = tmp_path / "runtime"
    ledger.mkdir()
    context = {"run_id": "run", "job_id": "planner-1", "inbox_offset": inbox.stat().st_size}

    reader = GuidanceInbox(tmp_path, ledger, context, "planner")

    assert [item["message"] for item in reader.poll()] == ["Review the boundary hypothesis."]


def test_selected_skills_are_loaded_with_distinct_orchestrator_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_SKILL", raising=False)
    extra = tmp_path / "extra.md"
    extra.write_text("Use the supplied finite group hypotheses.")
    monkeypatch.setenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", str(extra))
    assert "finite group hypotheses" in skill_guidance(tmp_path)
    assert "PLAN_job.md" in skill_guidance(tmp_path)
    assert "Never prove" in skill_guidance(tmp_path, "orchestrator")


def test_search_includes_gitignored_installed_dependencies(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".lake/\n")
    dependency = tmp_path / ".lake/packages/example/Lemmas.lean"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("theorem useful_library_lemma : True := by trivial\n")
    workspace = tmp_path / ".leanflow/job"
    workspace.mkdir(parents=True)
    tools = SessionTools(role="prover", project_root=tmp_path, workspace=workspace, context={})
    result = tools.invoke("search_project", {"query": "useful_library_lemma"})
    assert result["success"]
    assert [row["path"] for row in result["results"]] == [str(dependency.resolve())]


def test_one_research_agent_per_job_survives_resume(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    calls = []

    def research(question):
        calls.append(question)
        return {"success": True, "report": "Recorded external evidence"}

    for expected in (True, False):
        tools = SessionTools(
            role="prover",
            project_root=tmp_path,
            workspace=workspace,
            context={},
            research_job=research,
        )
        assert (
            tools.invoke("research_job", {"question": "Find the precise source definition"})[
                "success"
            ]
            == expected
        )
    assert len(calls) == 1
    child = SessionTools(
        role="research",
        project_root=tmp_path,
        workspace=workspace,
        context={},
        research_job=research,
    )
    assert not child.invoke("research_job", {"question": "recursive request"})["success"]
