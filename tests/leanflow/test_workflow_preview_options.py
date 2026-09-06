"""Preview options must never launch a run, wherever they are placed.

Backlog J09: ``leanflow workflow prove Main.lean --dry-run --json`` fed both
options into the workflow's remainder arguments and started a campaign. These
tests pin both supported positions, malformed placement, and the D07 preview
payload that reports the dedicated prover's effective configuration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leanflow_cli.main import main
from leanflow_cli.workflow import (
    NativeLaunchPlan,
    NativeWorkflowSpec,
    describe_launch_plan,
    launch_plan_payload,
    parse_workflow_command,
    prover_config_preview,
    resolve_workflow_request,
)


def _plan(tmp_path: Path, child_env: dict[str, str] | None = None) -> NativeLaunchPlan:
    return NativeLaunchPlan(
        project=type("Project", (), {"label": "Demo", "root": tmp_path, "lean_root": tmp_path})(),
        workflow=NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/prove Main.lean",
            workflow_args="Main.lean",
        ),
        runtime={"provider": "openai-codex", "model": "gpt-6-astra", "base_url": "https://x"},
        child_env=dict(child_env or {}),
        argv=["python", "-m", "leanflow_cli.workflows.prover.runtime"],
        active_skill="lean-bounded-prover",
        toolset_name="leanflow-prover-session",
    )


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Route the CLI at a fake plan and fail loudly if anything tries to launch."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    def fake_resolve(command, **kwargs):
        captured["command"] = command
        captured["preview"] = kwargs.get("preview")
        return _plan(tmp_path)

    monkeypatch.setattr("leanflow_cli.main.resolve_workflow_request", fake_resolve)
    monkeypatch.setattr(
        "leanflow_cli.main.run_workflow",
        lambda *args, **kwargs: pytest.fail("a preview must never launch a workflow"),
    )
    return captured


@pytest.mark.parametrize(
    "argv",
    [
        ["workflow", "--dry-run", "--json", "prove", "Main.lean"],
        ["workflow", "prove", "Main.lean", "--dry-run", "--json"],
        ["workflow", "--dry-run", "prove", "Main.lean", "--json"],
        ["workflow", "prove", "--dry-run", "Main.lean", "--json"],
    ],
)
def test_preview_options_are_recognized_in_every_position(cli, capsys, argv):
    assert main(argv) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert payload["workflow"]["args"] == "Main.lean"
    assert cli["command"] == "/prove Main.lean"
    assert cli["preview"] is True


def test_misplaced_dry_run_without_json_renders_the_panel_and_never_launches(cli, capsys):
    assert main(["workflow", "prove", "Main.lean", "--dry-run"]) == 0

    assert "Launching Workflow" in capsys.readouterr().out
    assert cli["command"] == "/prove Main.lean"
    assert cli["preview"] is True


def test_json_without_dry_run_is_rejected_before_any_plan_or_run(monkeypatch, cli, capsys):
    monkeypatch.setattr(
        "leanflow_cli.main.resolve_workflow_request",
        lambda *args, **kwargs: pytest.fail("no plan may be resolved for a rejected command"),
    )

    for argv in (
        ["workflow", "prove", "Main.lean", "--json"],
        ["workflow", "--json", "prove", "Main.lean"],
    ):
        assert main(argv) == 2
        error = capsys.readouterr().err
        assert "--json requires --dry-run" in error
        assert "No run was started" in error


def test_preview_tokens_inside_prompt_text_stay_prompt_text(cli, capsys):
    argv = [
        "workflow",
        "--dry-run",
        "--json",
        "prove",
        "Main.lean",
        "--prompt",
        "compare --dry-run outputs",
    ]

    assert main(argv) == 0

    parsed = parse_workflow_command(str(cli["command"]))
    assert parsed.explicit_goal == "compare --dry-run outputs"
    assert parsed.workflow_args == "Main.lean"


def test_workflow_status_json_is_not_treated_as_a_launch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("leanflow_cli.main.load_workflow_live_status", lambda: {})
    monkeypatch.setattr("leanflow_cli.main.read_workflow_activity", lambda limit=8: [])

    assert main(["workflow", "status", "--json"]) == 1
    assert "No run was started" not in capsys.readouterr().err


@pytest.mark.parametrize("token", ["--dry-run", "--json"])
def test_parse_workflow_command_rejects_misplaced_preview_options(token):
    with pytest.raises(ValueError, match="preview option"):
        parse_workflow_command(f"/prove Main.lean {token}")


def test_shell_and_spawn_paths_fail_before_project_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "leanflow_cli.workflow.discover_leanflow_project",
        lambda cwd: pytest.fail("a rejected command must not discover a project"),
    )

    with pytest.raises(ValueError, match="no run was started"):
        resolve_workflow_request("/prove Main.lean --dry-run --json", active_cwd=tmp_path)


def test_prove_preview_reports_effective_prover_configuration(monkeypatch, tmp_path):
    from leanflow_cli.workflows.prover.config import ENV_NAMES

    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEANFLOW_PROVER_MODE", "research")
    plan = _plan(
        tmp_path,
        {
            "LEANFLOW_PROVER_MODE": "research",
            "LEANFLOW_PROVER_PARALLELISM": "4",
            "LEANFLOW_PROVER_TOTAL_API_CALLS": "2000",
            "LEANFLOW_PROVER_WALL_TIME_S": "28800",
            "LEANFLOW_NATIVE_MODEL": "gpt-6-astra",
            "LEANFLOW_NATIVE_ALLOWED_AXIOMS": "propext,Classical.choice",
        },
    )

    payload = launch_plan_payload(plan)
    prover = payload["prover"]

    assert prover["error"] == ""
    assert prover["effective"]["mode"] == "research"
    assert prover["effective"]["parallelism"] == 4
    assert prover["effective"]["total_api_calls"] == 2000
    assert prover["effective"]["model"] == "gpt-6-astra"
    assert prover["effective"]["allowed_axioms"] == ["propext", "Classical.choice"]
    assert prover["sources"]["mode"] == "environment"
    assert prover["sources"]["parallelism"] == "launcher"
    assert prover["sources"]["model"] == "runtime-provider"
    assert prover["sources"]["allowed_axioms"] == "launcher"
    assert prover["sources"]["max_nodes"] == "default"
    assert prover["env_names"]["parallelism"] == "LEANFLOW_PROVER_PARALLELISM"
    assert any("run id" in item for item in payload["deferred"])
    summary = describe_launch_plan(plan)["prover_config"]
    assert summary.startswith("research · bottom-up · 4 prover slot(s) · 2000 calls")
    assert "8 h wall" in summary
    assert payload["summary"]["prover_config"] == summary


def test_prove_preview_reports_invalid_configuration_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_PROVER_PARALLELISM", raising=False)
    plan = _plan(tmp_path, {"LEANFLOW_PROVER_PARALLELISM": "0"})

    payload = launch_plan_payload(plan)

    assert payload["prover"]["effective"] is None
    assert "parallelism" in payload["prover"]["error"]
    assert payload["summary"]["prover_config"].startswith("invalid:")
    assert prover_config_preview(plan)["sources"]["parallelism"] == "launcher"


def test_non_prove_preview_has_no_prover_section(tmp_path):
    plan = replace(
        _plan(tmp_path),
        workflow=NativeWorkflowSpec(
            workflow_kind="review",
            frontend_command="/review",
            canonical_command="/review",
            backend_command="/review Main.lean",
            workflow_args="Main.lean",
        ),
    )

    payload = launch_plan_payload(plan)

    assert payload["prover"] is None
    assert "prover_config" not in payload["summary"]
