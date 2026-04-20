from __future__ import annotations

from pathlib import Path

from opengauss_cli import workflow as workflow_mod
from opengauss_cli.workflow import parse_workflow_command, resolve_workflow_request


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
        "discover_opengauss_project",
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

    assert single.toolset_name == "opengauss-native"
    assert single.child_env["OPENGAUSS_NATIVE_USER_APPROVED_SWARM"] == "0"
    assert swarm.toolset_name == "opengauss-native-swarm"
    assert swarm.active_skill == "lean-autonomous-swarm"
    assert swarm.child_env["OPENGAUSS_NATIVE_USER_APPROVED_SWARM"] == "1"
