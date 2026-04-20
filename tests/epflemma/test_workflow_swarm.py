from __future__ import annotations

from pathlib import Path

import pytest

from epflemma_cli import workflow as workflow_mod
from epflemma_cli.workflow import (
    WORKFLOW_ALIAS_MAP,
    parse_workflow_command,
    resolve_workflow_request,
    rewrite_forgiving_workflow_command,
)


def test_parse_workflow_command_extracts_swarm_options():
    spec = parse_workflow_command("/autoprove Main.lean --agents 3 --goal finish theorem Foo")

    assert spec.workflow_kind == "autoprove"
    assert spec.backend_command == "/lean4:autoprove Main.lean"
    assert spec.parallel_agents == 3
    assert spec.explicit_goal == "finish theorem Foo"


def test_parse_workflow_command_defaults_to_single_agent():
    spec = parse_workflow_command("autoformalize \"formalize theorem\"")

    assert spec.parallel_agents == 1
    assert spec.explicit_goal == ""


def test_resolve_workflow_request_uses_swarm_toolset_only_when_user_requests_agents(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow_mod,
        "discover_epflemma_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-4-31B-it",
        },
    )

    single = resolve_workflow_request("/autoprove Main.lean", active_cwd=tmp_path)
    swarm = resolve_workflow_request("/autoprove Main.lean --agents 3", active_cwd=tmp_path)

    assert single.toolset_name == "epflemma-native"
    assert single.child_env["EPFLEMMA_NATIVE_USER_APPROVED_SWARM"] == "0"
    assert swarm.toolset_name == "epflemma-native-swarm"
    assert swarm.active_skill == "lean-autonomous-swarm"
    assert swarm.child_env["EPFLEMMA_NATIVE_USER_APPROVED_SWARM"] == "1"


# --- workflow kind mapping ---

@pytest.mark.parametrize(
    "command,expected_kind",
    [
        ("/prove Main.lean", "autoprove"),
        ("/autoprove Main.lean", "autoprove"),
        ("autoprove Main.lean", "autoprove"),
        ("prove Main.lean", "autoprove"),
        ("/formalize \"theorem\"", "autoformalize"),
        ("/autoformalize \"theorem\"", "autoformalize"),
        ("autoformalize \"theorem\"", "autoformalize"),
        ("formalize \"theorem\"", "autoformalize"),
        ("/draft Main.lean", "draft"),
        ("draft Main.lean", "draft"),
        ("/review Main.lean", "review"),
        ("review Main.lean", "review"),
        ("/checkpoint Main.lean", "checkpoint"),
        ("checkpoint Main.lean", "checkpoint"),
        ("/refactor Main.lean", "refactor"),
        ("refactor Main.lean", "refactor"),
        ("/golf Main.lean", "golf"),
        ("golf Main.lean", "golf"),
    ],
)
def test_parse_workflow_command_maps_all_aliases_to_correct_kind(command, expected_kind):
    spec = parse_workflow_command(command)
    assert spec.workflow_kind == expected_kind, f"{command!r} → {spec.workflow_kind!r}, expected {expected_kind!r}"


@pytest.mark.parametrize(
    "command,expected_backend",
    [
        ("/prove", "/lean4:autoprove"),
        ("/autoprove", "/lean4:autoprove"),
        ("/formalize", "/lean4:autoformalize"),
        ("/autoformalize", "/lean4:autoformalize"),
        ("/draft", "/lean4:draft"),
        ("/review", "/lean4:review"),
        ("/checkpoint", "/lean4:checkpoint"),
        ("/refactor", "/lean4:refactor"),
        ("/golf", "/lean4:golf"),
    ],
)
def test_parse_workflow_command_sets_correct_backend_command(command, expected_backend):
    spec = parse_workflow_command(command)
    assert spec.backend_command.startswith(expected_backend)


# --- forgiving alias rewriting ---

@pytest.mark.parametrize(
    "raw,expected_start",
    [
        ("autoprove Main.lean", "/prove Main.lean"),
        ("prove Main.lean", "/prove Main.lean"),
        ("autoformalize \"x\"", "/formalize \"x\""),
        ("formalize \"x\"", "/formalize \"x\""),
        ("draft Main.lean", "/draft Main.lean"),
        ("review Main.lean", "/review Main.lean"),
        ("checkpoint Main.lean", "/checkpoint Main.lean"),
        ("refactor Main.lean", "/refactor Main.lean"),
        ("golf Main.lean", "/golf Main.lean"),
    ],
)
def test_rewrite_forgiving_workflow_command_maps_bare_aliases(raw, expected_start):
    result = rewrite_forgiving_workflow_command(raw)
    assert result == expected_start, f"{raw!r} → {result!r}, expected {expected_start!r}"


def test_rewrite_forgiving_workflow_command_passthrough_for_slash_commands():
    assert rewrite_forgiving_workflow_command("/prove Main.lean") == "/prove Main.lean"
    assert rewrite_forgiving_workflow_command("/golf Main.lean") == "/golf Main.lean"


def test_rewrite_forgiving_workflow_command_passthrough_for_unknown():
    assert rewrite_forgiving_workflow_command("unknown-command foo") == "unknown-command foo"
    assert rewrite_forgiving_workflow_command("") == ""


# --- agents flag parsing ---

def test_parse_workflow_command_agents_clamped_to_minimum_1():
    spec = parse_workflow_command("/prove Main.lean --agents 0")
    assert spec.parallel_agents == 1


def test_parse_workflow_command_agents_rejects_non_integer():
    with pytest.raises(ValueError, match="integer"):
        parse_workflow_command("/prove Main.lean --agents notanumber")


def test_parse_workflow_command_agents_requires_value():
    with pytest.raises(ValueError, match="value"):
        parse_workflow_command("/prove Main.lean --agents")


# --- goal flag parsing ---

def test_parse_workflow_command_goal_is_empty_by_default():
    spec = parse_workflow_command("/prove Main.lean")
    assert spec.explicit_goal == ""


def test_parse_workflow_command_goal_captures_remaining_text():
    spec = parse_workflow_command("/prove Main.lean --goal prove absLipschitz theorem using abs_abs_sub")
    assert spec.explicit_goal == "prove absLipschitz theorem using abs_abs_sub"


def test_parse_workflow_command_goal_and_agents_together():
    spec = parse_workflow_command("/prove Main.lean --agents 2 --goal prove Foo")
    assert spec.parallel_agents == 2
    assert spec.explicit_goal == "prove Foo"


# --- rejected inputs ---

def test_parse_workflow_command_raises_for_unknown_command():
    with pytest.raises(ValueError, match="unsupported"):
        parse_workflow_command("/unknown-workflow Main.lean")


def test_parse_workflow_command_raises_for_empty_command():
    with pytest.raises(ValueError):
        parse_workflow_command("")


# --- workflow args extraction ---

def test_parse_workflow_command_preserves_file_arg():
    spec = parse_workflow_command("/prove GaussTest/RealTheorems-homework.lean")
    assert spec.workflow_args == "GaussTest/RealTheorems-homework.lean"
    assert "GaussTest/RealTheorems-homework.lean" in spec.backend_command


def test_parse_workflow_command_no_workflow_args_when_only_command():
    spec = parse_workflow_command("/prove")
    assert spec.workflow_args == ""


# --- skill selection ---

def test_resolve_workflow_request_assigns_correct_default_skill_for_formalize(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow_mod,
        "discover_epflemma_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-4-31B-it",
        },
    )

    plan = resolve_workflow_request("/formalize \"state Lipschitz theorem\"", active_cwd=tmp_path)

    assert plan.active_skill == "lean-formalization"
    assert plan.toolset_name == "epflemma-native"


def test_all_workflow_aliases_are_covered_by_alias_map():
    forgiving_kinds = {"autoprove", "autoformalize", "draft", "review", "checkpoint", "refactor", "golf"}
    mapped_kinds = {v[0] for v in WORKFLOW_ALIAS_MAP.values()}
    assert forgiving_kinds == mapped_kinds
