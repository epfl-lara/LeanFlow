from __future__ import annotations

from rich.console import Console

from epflemma_cli.banner import render_help
from epflemma_cli.main import InteractiveShell, main
from epflemma_cli.workflow_state import append_workflow_activity, append_workflow_run_log, reset_workflow_run_log
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
        workflow_kind="autoprove",
        frontend_command="/autoprove",
        canonical_command="/autoprove",
        backend_command="/lean4:autoprove Main.lean",
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
    assert summary["command"] == "/lean4:autoprove Main.lean"
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


def test_swarm_command_renders_agent_table(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        model="google/gemma-4-31B-it",
        provider="custom",
        delegate_depth=0,
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="agent-main",
        content="Inspecting theorem and diagnostics.",
    )

    shell = InteractiveShell()

    assert shell._run_swarm_command([]) == 0
    output = capsys.readouterr().out
    assert "agent-main" in output
    assert "active" in output


def test_status_agent_detail_renders_recent_activity(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-child",
        parent_agent_session_id="agent-main",
        delegate_depth=1,
        model="zai-org/GLM-5",
    )
    append_workflow_activity(
        "tool-result",
        "Tool result: terminal",
        agent_session_id="agent-child",
        tool="terminal",
        is_error=False,
    )

    shell = InteractiveShell()

    assert shell.show_status(["agent-child", "2"]) == 0
    output = capsys.readouterr().out
    assert "agent-child" in output
    assert "agent-main" in output
    assert "terminal completed" in output


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


def test_main_loads_epflemma_dotenv_before_provider_resolution(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("OPENROUTER_API_KEY=or-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_HOME", str(home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    captured: dict[str, str] = {}

    def _fake_render_provider_panel(console, *, resolved, requested, targets):
        captured["api_key"] = str(resolved.get("api_key", ""))
        captured["provider"] = str(resolved.get("provider", ""))

    monkeypatch.setattr("epflemma_cli.main.render_provider_panel", _fake_render_provider_panel)

    assert main(["provider", "--requested", "openrouter"]) == 0
    assert captured["provider"] == "openrouter"
    assert captured["api_key"] == "or-from-dotenv"


def test_main_prefers_epflemma_scoped_env_names(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "EPFLEMMA_OPENAI_BASE_URL=https://rcp.epfl.example/v1\n"
        "EPFLEMMA_OPENAI_API_KEY=epflemma-rcp-key\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "OPENAI_API_KEY=global-openai-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EPFLEMMA_HOME", str(home))
    monkeypatch.delenv("EPFLEMMA_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("EPFLEMMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    captured: dict[str, str] = {}

    def _fake_render_provider_panel(console, *, resolved, requested, targets):
        captured["api_key"] = str(resolved.get("api_key", ""))
        captured["base_url"] = str(resolved.get("base_url", ""))
        captured["provider"] = str(resolved.get("provider", ""))

    monkeypatch.setattr("epflemma_cli.main.render_provider_panel", _fake_render_provider_panel)

    assert main(["provider", "--requested", "custom"]) == 0
    assert captured["provider"] == "custom"
    assert captured["base_url"] == "https://rcp.epfl.example/v1"
    assert captured["api_key"] == "epflemma-rcp-key"
