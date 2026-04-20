from __future__ import annotations

from rich.console import Console

from epflemma_cli.banner import render_help
from epflemma_cli.main import InteractiveShell
from epflemma_cli.workflow_state import append_workflow_run_log, reset_workflow_run_log
from epflemma_cli.runtime_provider import list_runtime_provider_targets
from epflemma_cli.workflow import NativeLaunchPlan, NativeWorkflowSpec, describe_launch_plan


def test_render_help_mentions_forgiving_workflow_commands():
    console = Console(record=True, width=120)

    render_help(console)

    output = console.export_text()
    assert "/project" in output
    assert "/provider" in output
    assert "/workflow" in output
    assert "/skills" in output
    assert "prove Main.lean" in output
    assert "/workflow log 120" in output


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
        child_env={"EPFLEMMA_NATIVE_ACTIVE_SKILL": "lean-proof-loop"},
        argv=["python", "-m", "epflemma_cli.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="epflemma-native",
    )

    summary = describe_launch_plan(plan)

    assert summary["provider"] == "local:vllm"
    assert summary["model"] == "google/gemma-4-31B-it"
    assert summary["command"] == "/lean4:prove Main.lean"
    assert summary["skill"] == "lean-proof-loop"
    assert summary["agents"] == "1"


def test_interactive_project_init_reports_already_initialized(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    shell = InteractiveShell()

    assert shell._run_project_command(["init", str(root)]) == 0
    first = capsys.readouterr().out
    assert "Initialized project: Demo" in first

    assert shell._run_project_command(["init", str(root)]) == 0
    second = capsys.readouterr().out
    assert "Project already initialized: Demo" in second


def test_interactive_workflow_log_prints_saved_runner_log(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    reset_workflow_run_log()
    append_workflow_run_log("alpha\nbeta\ngamma\n")

    shell = InteractiveShell()

    assert shell._run_workflow_status_command(["log", "2"]) == 0
    output = capsys.readouterr().out
    assert "beta" in output
    assert "gamma" in output


def test_prompt_message_includes_project_and_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".epflemma").mkdir()
    (root / ".epflemma" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .epflemma/runtime\n  cache: .epflemma/cache\n  workflows: .epflemma/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".epflemma" / "runtime").mkdir()
    (root / ".epflemma" / "cache").mkdir()
    (root / ".epflemma" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root

    fragments = shell._prompt_message()
    text = "".join(fragment for _, fragment in fragments)
    assert "Demo" in text
    assert "idle" in text
    assert text.endswith("\n› ")


def test_project_command_without_args_shows_current_project(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".epflemma").mkdir()
    (root / ".epflemma" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .epflemma/runtime\n  cache: .epflemma/cache\n  workflows: .epflemma/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".epflemma" / "runtime").mkdir()
    (root / ".epflemma" / "cache").mkdir()
    (root / ".epflemma" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root

    assert shell._run_project_command([]) == 0
    output = capsys.readouterr().out
    assert "Demo" in output
    assert "EPFLemma Project" in output


def test_project_command_without_args_has_polished_empty_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()
    shell.cwd = tmp_path

    assert shell._run_project_command([]) == 1
    output = capsys.readouterr().out
    assert "No active Lean workspace is open here." in output
