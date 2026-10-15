"""Exercise the native session to compact activity to full evidence read path."""

from __future__ import annotations

import json
from pathlib import Path

from agent.runtime.harness import HarnessResult
from leanflow_cli.cli.run_event_detail import read_event_detail
from leanflow_cli.workflows.prover.agent_session import run_session
from leanflow_cli.workflows.prover.observer import RunObserver
from leanflow_cli.workflows.prover.store import RunStore
from leanflow_cli.workflows.workflow_state import workflow_run_activity_path
from tests.leanflow.test_prover_runtime import project


def test_native_output_is_joined_by_exact_evidence_and_never_marks_a_proof_accepted(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    for name in (
        "LEANFLOW_PROJECT_ROOT",
        "LEANFLOW_WORKFLOW_RUN_ID",
        "LEANFLOW_NATIVE_WORKFLOW_KIND",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMEX_API_KEY", "opaque-local-key")
    run_id, job_id = "prove-native-output", "prover_00001"
    observer = RunObserver(root, run_id)
    store = RunStore(root, run_id)
    store.on_event = observer.event
    workspace = store.directory / "jobs" / job_id
    workspace.mkdir(parents=True)
    explanation = "Check the induction hypothesis.\n" * 200

    def native(request, on_event):
        request.on_started()
        on_event({"type": "harness.settings", "invocation_id": "invocation-1"})
        on_event({"type": "thread.started", "thread_id": "native-session"})
        for fragment in ("Live ", "public progress. "):
            on_event(
                {
                    "type": "item.delta",
                    "item_id": "reasoning1",
                    "item_type": "reasoning",
                    "field": "text",
                    "delta": fragment,
                }
            )
        on_event(
            {
                "type": "item.completed",
                "item": {"id": "reasoning1", "type": "reasoning", "text": explanation},
            }
        )
        on_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer1",
                    "type": "agent_message",
                    "text": "Candidate ready; opaque-local-key must be hidden",
                },
            }
        )
        return HarnessResult(
            "completed",
            final_response="A candidate",
            session_id="native-session",
            cleanup_verified=True,
        )

    monkeypatch.setattr("agent.runtime.harness.run_harness", native)
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.check_process.preflight_check",
        lambda **_: {"success": True, "ok": True},
    )
    run_session(
        role="prover",
        prompt="prove",
        project_root=root,
        workspace=workspace,
        config={"provider": "lemex"},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={"job_id": job_id, "run_id": run_id},
        on_event=lambda kind, detail: store.event(kind, {"job_id": job_id, **detail}),
    )
    rows = [
        json.loads(line) for line in workflow_run_activity_path(run_id).read_text().splitlines()
    ]
    reasoning_rows = [row for row in rows if row["type"] == "agent-reasoning"]
    assert reasoning_rows[0]["details"]["partial"] is True
    assert reasoning_rows[0]["details"]["invocation_id"] == "invocation-1"
    assert reasoning_rows[0]["event_id"] != reasoning_rows[-1]["event_id"]
    reasoning = reasoning_rows[-1]
    assert reasoning["details"]["partial"] is False
    live = read_event_detail(
        root / ".leanflow/workflow-state", run_id, reasoning_rows[0]["event_id"]
    )
    assert live["evidence"]["record"]["details"]["text"] == "Live "
    assert reasoning["agent_id"] == job_id
    assert len(reasoning["details"]["content_preview"]) < len(explanation)
    assert "accepted" not in reasoning["details"]
    detail = read_event_detail(root / ".leanflow/workflow-state", run_id, reasoning["event_id"])
    assert detail["evidence"]["match"] == "evidence_id"
    assert detail["evidence"]["record"]["details"]["text"] == explanation
    assert "opaque-local-key" not in (workspace / "events.jsonl").read_text()
    assert "opaque-local-key" not in json.dumps(rows)
