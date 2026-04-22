from __future__ import annotations

from rich.console import Console

from epflemma_cli.banner import render_help
from epflemma_cli.main import InteractiveShell, main
from epflemma_cli.workflow_state import append_workflow_activity, append_workflow_run_log, load_workflow_live_status, reset_workflow_run_log, save_workflow_live_status
from epflemma_cli.runtime_provider import list_runtime_provider_targets
from epflemma_cli.workflow import NativeLaunchPlan, NativeWorkflowSpec, describe_launch_plan, resolve_workflow_request


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


def test_main_mcp_status_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "epflemma_cli.main.get_mcp_status",
        lambda: [{"name": "lean-lsp", "transport": "stdio", "tools": 3, "connected": True}],
    )

    assert main(["mcp", "status", "--json"]) == 0
    output = capsys.readouterr().out
    assert "\"name\": \"lean-lsp\"" in output


def test_interactive_mcp_status_prints_sampling_metrics(monkeypatch, capsys):
    shell = InteractiveShell()
    monkeypatch.setattr(
        "epflemma_cli.main.get_mcp_status",
        lambda: [
            {
                "name": "lean-lsp",
                "transport": "stdio",
                "tools": 3,
                "connected": True,
                "sampling": {"requests": 2, "errors": 1},
            }
        ],
    )

    assert shell._run_mcp_command("/mcp status") == 0
    output = capsys.readouterr().out
    assert "lean-lsp" in output
    assert "sampling requests=2" in output


def test_describe_launch_plan_formats_provider_and_model(tmp_path):
    spec = NativeWorkflowSpec(
        workflow_kind="prove",
        frontend_command="/prove",
        canonical_command="/prove",
        backend_command="/prove Main.lean",
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
    assert summary["command"] == "/prove Main.lean"
    assert summary["skill"] == "lean-proof-loop"
    assert summary["agents"] == "1"


def test_resolve_workflow_request_normalizes_requested_active_file(tmp_path):
    root = tmp_path / "GaussTest"
    (root / ".epflemma").mkdir(parents=True)
    (root / ".epflemma" / "project.yaml").write_text(
        "schema_version: 1\nname: GaussTest\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .epflemma/runtime\n  cache: .epflemma/cache\n  workflows: .epflemma/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".epflemma" / "runtime").mkdir()
    (root / ".epflemma" / "cache").mkdir()
    (root / ".epflemma" / "workflows").mkdir()
    (root / "lakefile.toml").write_text("name = 'GaussTest'\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / "GaussTest").mkdir()
    (root / "GaussTest" / "RealTheorems-homework.lean").write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    plan = resolve_workflow_request(
        "/prove ./GaussTest/GaussTest/RealTheorems-homework.lean",
        active_cwd=root,
    )

    assert plan.child_env["EPFLEMMA_NATIVE_ACTIVE_FILE"] == "GaussTest/RealTheorems-homework.lean"
    assert plan.child_env["EPFLEMMA_NATIVE_WORKFLOW_COMMAND"] == "/prove GaussTest/RealTheorems-homework.lean"
    assert plan.workflow.workflow_args == "GaussTest/RealTheorems-homework.lean"


def test_resolve_workflow_request_recovers_similar_requested_active_file(tmp_path):
    root = tmp_path / "GaussTest"
    (root / ".epflemma").mkdir(parents=True)
    (root / ".epflemma" / "project.yaml").write_text(
        "schema_version: 1\nname: GaussTest\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .epflemma/runtime\n  cache: .epflemma/cache\n  workflows: .epflemma/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".epflemma" / "runtime").mkdir()
    (root / ".epflemma" / "cache").mkdir()
    (root / ".epflemma" / "workflows").mkdir()
    (root / "lakefile.toml").write_text("name = 'GaussTest'\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / "GaussTest").mkdir()
    (root / "GaussTest" / "RealTheorems-homework.lean").write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    plan = resolve_workflow_request(
        "/prove ./wrong/subdir/RealTheorems-homework.lean",
        active_cwd=root,
    )

    assert plan.child_env["EPFLEMMA_NATIVE_ACTIVE_FILE"] == "GaussTest/RealTheorems-homework.lean"
    assert plan.child_env["EPFLEMMA_NATIVE_WORKFLOW_COMMAND"] == "/prove GaussTest/RealTheorems-homework.lean"
    assert plan.workflow.workflow_args == "GaussTest/RealTheorems-homework.lean"


def test_interactive_workflow_launch_spawns_background_runner(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()
    shell.cwd = tmp_path

    fake_plan = NativeLaunchPlan(
        project=type("Project", (), {"label": "Demo", "root": tmp_path})(),
        workflow=NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/prove Main.lean",
            workflow_args="Main.lean",
        ),
        runtime={"provider": "custom", "model": "zai-org/GLM-5.1", "base_url": "https://inference.rcp.epfl.ch/v1"},
        child_env={},
        argv=["python", "-m", "epflemma_cli.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="epflemma-native",
    )

    monkeypatch.setattr("epflemma_cli.main.resolve_workflow_request", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr("epflemma_cli.main.describe_launch_plan", lambda plan: {"workflow": "prove", "command": "/prove Main.lean", "project": "Demo", "project_root": str(tmp_path), "provider": "custom", "base_url": "https://inference.rcp.epfl.ch/v1", "model": "zai-org/GLM-5.1", "skill": "lean-proof-loop", "agents": "1"})

    class _FakeProcess:
        pid = 43210

    monkeypatch.setattr("epflemma_cli.main.spawn_workflow", lambda *args, **kwargs: (fake_plan, _FakeProcess()))
    monkeypatch.setattr("epflemma_cli.main.load_workflow_live_status", lambda: {})

    assert shell._run_workflow_command("/prove Main.lean") == 0
    output = capsys.readouterr().out
    assert "Workflow running in background" in output
    assert "43210" in output
    payload = load_workflow_live_status()
    assert payload["phase"] == "busy"
    assert payload["build_status"] == "workflow launching"


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


def test_top_level_kill_command_interrupts_agent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()

    monkeypatch.setattr(
        "epflemma_cli.main.terminate_workflow_agent",
        lambda agent_id: {"success": True, "agent_id": "12345", "process_id": 24680},
    )

    assert shell._run_kill_command(["12345"]) == 0
    output = capsys.readouterr().out
    assert "Sent interrupt to workflow agent" in output
    assert "12345" in output


def test_shell_exit_interrupts_current_project_workflows(monkeypatch, tmp_path, capsys):
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

    monkeypatch.setattr(
        "epflemma_cli.main.terminate_project_workflow_agents",
        lambda project_root: {"success": True, "count": 1, "terminated": ["12345"], "failed": []},
    )

    assert shell._handle_command("/exit") is False
    output = capsys.readouterr().out
    assert "Interrupted 1 workflow agent(s)" in output
    assert "Demo before exiting the shell" in output


def test_swarm_agent_view_renders_transcript_not_status_panel(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        user_message="Prove theorem foo",
        model="zai-org/GLM-5.1",
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="12345",
        content="I will inspect diagnostics first.",
    )
    append_workflow_activity(
        "tool-call",
        "Tool call: terminal",
        agent_session_id="12345",
        tool="terminal",
        arguments={"command": "lake env lean Demo/RealTheorems-homework.lean"},
    )
    append_workflow_activity(
        "tool-result",
        "Tool result: terminal",
        agent_session_id="12345",
        tool="terminal",
        is_error=True,
        result='{"exit_code":1,"output":"error: type mismatch"}',
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="12345",
        completed=True,
    )

    shell = InteractiveShell()
    monkeypatch.setattr(shell.session, "prompt", lambda *args, **kwargs: "/exit")

    assert shell._run_swarm_command(["12345", "5"]) == 0
    output = capsys.readouterr().out
    assert "Swarm View" in output
    assert "Prove theorem foo" in output
    assert "inspect diagnostics first" in output
    assert "Following agent output" in output
    assert "lake env lean Demo/RealTheorems-homework.lean" in output
    assert "exit 1: error: type mismatch" in output
    assert "Returning to the main shell" in output
    assert "Enter a follow-up prompt" not in output


def test_swarm_agent_view_can_queue_follow_up_prompt(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()

    agent = {
        "agent_id": "12345",
        "status": "verified",
        "model": "zai-org/GLM-5.1",
        "parent_agent_id": "",
    }
    transcript = [
        {"timestamp": "2026-04-20T10:00:00+00:00", "type": "assistant-response", "role": "assistant", "content": "Initial pass done."},
    ]
    queued = {}
    prompts = iter(["Try the continuity lemma next.", "/exit"])

    monkeypatch.setattr("epflemma_cli.main.resolve_workflow_agent_id", lambda ref: "12345")
    monkeypatch.setattr("epflemma_cli.main.workflow_agent_detail", lambda *args, **kwargs: dict(agent))
    monkeypatch.setattr("epflemma_cli.main.workflow_agent_transcript", lambda *args, **kwargs: list(transcript))
    monkeypatch.setattr("epflemma_cli.main.workflow_agent_transcript_all", lambda *args, **kwargs: list(transcript))
    monkeypatch.setattr(
        "epflemma_cli.main.enqueue_workflow_agent_message",
        lambda agent_id, text: queued.setdefault("payload", {"success": True, "agent_id": agent_id, "text": text}),
    )
    monkeypatch.setattr(shell.session, "prompt", lambda *args, **kwargs: next(prompts))

    assert shell._run_swarm_command(["12345", "5"]) == 0
    output = capsys.readouterr().out
    assert "Agent 12345 is verified" in output
    assert "Queued prompt for agent 12345" in output
    assert queued["payload"]["text"] == "Try the continuity lemma next."


def test_status_agent_detail_renders_recent_activity(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-child",
        parent_agent_session_id="agent-main",
        delegate_depth=1,
        model="zai-org/GLM-5.1",
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


def test_prompt_message_hides_unknown_theorem_placeholder(monkeypatch, tmp_path):
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
    save_workflow_live_status(
        {
            "phase": "busy",
            "target_symbol": "[unknown]",
            "active_file_label": "GaussTest/RealTheorems-homework.lean",
            "build_status": "unknown",
        }
    )

    shell = InteractiveShell()
    shell.cwd = root

    fragments = shell._prompt_message()
    text = "".join(fragment for _, fragment in fragments)
    assert "[unknown]" not in text
    assert "RealTheorems-homework.lean" in text


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
