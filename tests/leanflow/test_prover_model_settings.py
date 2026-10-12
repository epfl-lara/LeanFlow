"""Preserve native model evidence in saved jobs without changing call accounting."""

import json

from tests.leanflow import test_prover_progress as progress_tests

runtime = progress_tests.runtime


def test_settings_are_durable_per_invocation_and_do_not_spend_calls(runtime):
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime._session_event(job, "api-request", {"api_calls": 1})
    before = job["api_calls"]
    for invocation, model in [("first-call", "native-a"), ("second-call", "native-b")]:
        runtime._session_event(
            job,
            "agent-settings",
            {
                "invocation_id": invocation,
                "backend": "lemex",
                "model": model,
                "model_source": "native_session",
                "reasoning_effort": "high",
                "reasoning_source": "native_session",
            },
        )
    saved = json.loads((runtime.store.directory / "state.json").read_text())
    records = saved["jobs"][0]["model_settings"]
    assert [r["model"] for r in records] == ["native-a", "native-b"]
    assert [r["invocation_id"] for r in records] == ["first-call", "second-call"]
    assert job["api_calls"] == before == 1
    assert job["model"] == "", "actual metadata must not overwrite the requested override"
    assert saved["jobs"][0]["api_calls"] == 1
