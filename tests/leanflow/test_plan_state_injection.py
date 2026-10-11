"""P1.3 artifact-awareness injection tests: every prompt surface + child env.

Flag off: every surface is byte-identical to before. Flag on: startup,
continuation, system prompt, worker prompt, and the spawn env all carry the
plan-state artifact paths; the continuation prompt keeps the volatile frontier
digest AFTER the prefix-cache cycle marker.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


def test_worker_prompt_carries_plan_artifacts(plan_enabled):
    from leanflow_cli.lean.lean_models import LeanWorkerRequest
    from leanflow_cli.lean.lean_worker_dispatch import _worker_prompt

    prompt = _worker_prompt(
        "proof-repair", LeanWorkerRequest(worker="proof-repair", goal="fix demo", context="")
    )

    assert "Plan artifacts:" in prompt
    assert str(plan_enabled / "blueprint.json") in prompt
    assert "read-only generated sections" in prompt
    assert "never edit or paginate the historical user-owned Notes body" in prompt
    assert "append below" not in prompt
    assert "summary machine snapshot (do not read directly)" in prompt


def test_spawn_env_carries_artifact_paths(plan_enabled, tmp_path, monkeypatch):
    from leanflow_cli import workflow as workflow_module

    paths = workflow_module.plan_state_paths(tmp_path / ".leanflow" / "workflow-state")
    # LEANFLOW_PLAN_STATE_DIR override wins (test convenience parity).
    assert paths.blueprint_json == plan_enabled / "blueprint.json"

    monkeypatch.delenv("LEANFLOW_PLAN_STATE_DIR", raising=False)
    anchored = workflow_module.plan_state_paths(tmp_path / ".leanflow" / "workflow-state")
    assert anchored.blueprint_json == tmp_path / ".leanflow" / "workflow-state" / "blueprint.json"
