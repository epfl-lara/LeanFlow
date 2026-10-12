"""Keep controller cancellation distinct from provider errors and admissions."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows.prover import agent_session
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from tests.leanflow.test_prover_live_progress import make_runtime


def test_already_cancelled_callback_does_not_become_runtime_error(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.session = agent_session.run_session
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime.cancelled.set()
    result = runtime._invoke(job, context, "Prove the goal")
    assert result["status"] == "interrupted", result
    assert result["api_calls"] == 0


@pytest.mark.parametrize(
    "status", ["error", "provider_error", "environment_error", "source_conflict", "context_limit"]
)
def test_session_failure_kind_survives_controller_stop(tmp_path, status):
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("research", prompt="Inspect the local goal")
    with pytest.raises(InfrastructureFailure) as raised:
        runtime._finish_job(job, {"status": status, "api_calls": 1, "error": "recorded failure"})
    assert raised.value.status == status
    assert job["status"] == status
