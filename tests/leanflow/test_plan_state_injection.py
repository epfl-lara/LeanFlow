"""P1.3 artifact-awareness injection tests: every prompt surface + child env.

Flag off: every surface is byte-identical to before. Flag on: startup,
continuation, system prompt, worker prompt, and the spawn env all carry the
plan-state artifact paths; the continuation prompt keeps the volatile frontier
digest AFTER the prefix-cache cycle marker.
"""

from __future__ import annotations

import pytest

from leanflow_cli.native import native_runner as runner


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


def _quiet_startup(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setattr(runner, "_startup_active_skill_contract", lambda _name: "")
    monkeypatch.setattr(runner, "_startup_additional_skill_contracts", lambda _name: "")
    monkeypatch.setattr(runner, "_queue_assignment_block", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type("Route", (), {"to_dict": lambda self: {}})(),
    )


def test_startup_message_carries_artifact_paths_when_enabled(plan_enabled, monkeypatch):
    _quiet_startup(monkeypatch)

    prompt = runner._startup_user_message(live_state={}, autonomy_state={})

    assert "Living plan artifacts" in prompt
    assert str(plan_enabled / "blueprint.json") in prompt


def test_startup_message_unchanged_when_disabled(monkeypatch):
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    _quiet_startup(monkeypatch)

    prompt = runner._startup_user_message(live_state={}, autonomy_state={})

    assert "Living plan artifacts" not in prompt


def test_system_prompt_carries_static_artifact_line(plan_enabled, monkeypatch):
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)

    text = runner._managed_system_prompt()

    assert "Living plan artifacts" in text
    assert str(plan_enabled / "blueprint.json") in text
    assert "bounded, read-only generated plan.md view" in text
    assert "never edit or paginate the hidden historical user-owned Notes body" in text
    assert "append" not in text
    assert "Lean source/kernel diagnostics outrank stored plan" in text
    # Static line only — no volatile digest in the system prompt.
    assert "Dependency graph digest:" not in text


def test_continuation_prompt_keeps_digest_after_cycle_marker(plan_enabled, monkeypatch):
    monkeypatch.setenv("LEANFLOW_RCP_PREFIX_CACHE", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")
    monkeypatch.setattr(runner, "_queue_assignment_block", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "_queue_needs_final_file_sweep", lambda live_state: False)
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type("Route", (), {"to_dict": lambda self: {}})(),
    )
    from leanflow_cli.workflows import plan_state

    plan_state.save_blueprint(
        plan_state.Blueprint(
            goal="g",
            nodes=(
                plan_state.GraphNode(
                    id="n1", name="frontier_thm", file="Demo.lean", status="stated"
                ),
            ),
        )
    )

    prompt = runner._autonomous_continuation_prompt({"declaration_scope": "file"}, 7, {})

    marker = "[current turn: continuation cycle 7]"
    assert marker in prompt
    paths_at = prompt.find("Living plan artifacts")
    marker_at = prompt.find(marker)
    digest_at = prompt.find("Dependency graph digest:")
    assert 0 <= paths_at < marker_at < digest_at
    assert "frontier_thm" in prompt

    # The pre-marker prefix must be byte-stable across cycles.
    next_prompt = runner._autonomous_continuation_prompt({"declaration_scope": "file"}, 8, {})
    assert prompt[:marker_at] == next_prompt[:marker_at]


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
