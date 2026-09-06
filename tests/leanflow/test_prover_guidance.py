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
