from __future__ import annotations

from rich.console import Console

from opengauss_cli.banner import render_help
from opengauss_cli.runtime_provider import list_runtime_provider_targets
from opengauss_cli.workflow import NativeLaunchPlan, NativeWorkflowSpec, describe_launch_plan


def test_render_help_mentions_forgiving_workflow_commands():
    console = Console(record=True, width=120)

    render_help(console)

    output = console.export_text()
    assert "/project" in output
    assert "/provider" in output
    assert "/workflow" in output
    assert "/skills" in output
    assert "prove Main.lean" in output


def test_list_runtime_provider_targets_includes_local_and_zai():
    names = {entry["name"] for entry in list_runtime_provider_targets()}

    assert "local" in names
    assert "zai" in names
    assert "custom" in names


def test_describe_launch_plan_formats_provider_and_model(tmp_path):
    spec = NativeWorkflowSpec(
        workflow_kind="prove",
        frontend_command="/prove",
        canonical_command="/prove",
        backend_command="/lean4:prove Main.lean",
        workflow_args="Main.lean",
    )
    plan = NativeLaunchPlan(
        project=type("Project", (), {"label": "Demo", "root": tmp_path})(),
        workflow=spec,
        runtime={
            "provider": "local",
            "runtime": "vllm",
            "model": "google/gemma-4-31B-it",
            "base_url": "http://127.0.0.1:8000/v1",
        },
        child_env={"OPENGAUSS_NATIVE_ACTIVE_SKILL": "lean-proof-loop"},
        argv=["python", "-m", "opengauss_cli.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="opengauss-native",
    )

    summary = describe_launch_plan(plan)

    assert summary["provider"] == "local:vllm"
    assert summary["model"] == "google/gemma-4-31B-it"
    assert summary["command"] == "/lean4:prove Main.lean"
    assert summary["skill"] == "lean-proof-loop"
    assert summary["agents"] == "1"
