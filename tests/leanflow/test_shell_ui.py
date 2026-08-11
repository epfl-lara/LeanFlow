from __future__ import annotations

import pytest
from rich.console import Console

from leanflow_cli import main as main_module
from leanflow_cli.cli import cli_handlers
from leanflow_cli.cli.banner import render_help, render_workflow_status_panel
from leanflow_cli.main import InteractiveShell, main
from leanflow_cli.runtime.runtime_provider import list_runtime_provider_targets
from leanflow_cli.workflow import (
    NativeLaunchPlan,
    NativeWorkflowSpec,
    describe_launch_plan,
    parse_workflow_command,
    resolve_workflow_request,
)
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    append_workflow_run_log,
    load_workflow_live_status,
    reset_workflow_run_log,
    save_workflow_live_status,
)


def test_cli_handlers_reexport_identity():
    # Phase 3 extraction: the moved handler/formatter functions must stay importable from
    # leanflow_cli.main (re-export shim) and be the exact same objects defined in
    # leanflow_cli.cli.cli_handlers, so existing main.<name> references and tests keep working.
    for name in cli_handlers.__all__:
        assert hasattr(main_module, name), f"main lost re-export of {name}"
        assert getattr(main_module, name) is getattr(
            cli_handlers, name
        ), f"{name} is not the same object on main and cli_handlers"


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


def test_render_workflow_status_panel_marks_stale_dead_snapshot():
    console = Console(record=True, width=120)

    render_workflow_status_panel(
        console,
        status={
            "phase": "dead",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": "/tmp/project",
            "provider": "custom",
            "model": "zai-org/GLM-5.1",
            "active_skill": "lean-theorem-queue-worker",
            "parallel_agents": 1,
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
            "build_status": "unknown",
            "project_sorry_count": 1,
            "latest_checkpoint_label": "[none]",
            "held_locks": 0,
            "updated_at": "2026-04-22T14:33:57+00:00",
            "stale_snapshot": True,
            "stale_process_id": 8111,
        },
        activities=[],
    )

    output = console.export_text()
    assert "dead (stale snapshot)" in output
    assert "Stale PID" in output
    assert "8111" in output


def test_render_workflow_status_panel_marks_prior_snapshot_pending_reconciliation():
    console = Console(record=True, width=120)

    render_workflow_status_panel(
        console,
        status={
            "phase": "reconciling",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": "/tmp/project",
            "provider": "codex",
            "model": "gpt-5",
            "active_skill": "lean-theorem-queue-worker",
            "parallel_agents": 1,
            "active_file_label": "Main.lean",
            "target_symbol": "prior_goal",
            "build_status": "prior build result",
            "project_sorry_count": 1,
            "latest_checkpoint_label": "prior checkpoint",
            "held_locks": 0,
            "updated_at": "2026-07-16T01:00:00+00:00",
            "startup_reconciliation_pending": True,
        },
        activities=[],
    )

    output = console.export_text()
    assert "Proof state" in output
    assert "prior durable snapshot; reconciliation pending" in output
    assert "prior_goal" in output


def test_render_workflow_status_panel_shows_terminal_exit_outcome():
    console = Console(record=True, width=120)

    render_workflow_status_panel(
        console,
        status={
            "phase": "exited",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": "/tmp/project",
            "provider": "codex",
            "model": "gpt-5",
            "active_skill": "lean-theorem-queue-worker",
            "parallel_agents": 1,
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
            "build_status": "warning: declaration uses sorry",
            "project_sorry_count": 1,
            "latest_checkpoint_label": "pre-exit checkpoint",
            "held_locks": 0,
            "updated_at": "2026-07-16T03:00:00+00:00",
            "exit_code": 2,
            "reason": "explicit interactive exit",
        },
        activities=[],
    )

    output = console.export_text()
    assert "Exit" in output
    assert "2: explicit interactive exit" in output


def test_render_workflow_status_panel_shows_project_prove_manager_queue():
    console = Console(record=True, width=120)

    render_workflow_status_panel(
        console,
        status={
            "phase": "in-progress",
            "workflow_kind": "prove",
            "workflow_command": "/prove",
            "project_root": "/tmp/project",
            "provider": "custom",
            "model": "zai-org/GLM-5.1",
            "active_skill": "lean-theorem-queue-worker",
            "parallel_agents": 1,
            "active_file_label": "Demo/Base.lean",
            "target_symbol": "base",
            "project_prove_manager": True,
            "project_prove_file_queue": ["Demo/Base.lean", "Demo/Later.lean"],
            "project_prove_completed_files": ["Demo/Intro.lean"],
            "project_prove_plan_source": "llm",
            "build_status": "unknown",
            "project_sorry_count": 2,
            "latest_checkpoint_label": "[none]",
            "held_locks": 0,
            "updated_at": "2026-04-22T14:33:57+00:00",
        },
        activities=[],
    )

    output = console.export_text()
    assert "Project manager" in output
    assert "llm; 2 queued, 1 completed" in output
    assert "Demo/Base.lean, Demo/Later.lean" in output


def test_render_workflow_status_panel_shows_warning_cleanup_state():
    console = Console(record=True, width=120)

    render_workflow_status_panel(
        console,
        status={
            "phase": "verified",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": "/tmp/project",
            "provider": "custom",
            "model": "zai-org/GLM-5.1",
            "active_skill": "lean-proof-loop",
            "parallel_agents": 1,
            "active_file_label": "Main.lean",
            "target_symbol": "",
            "build_status": "lake env lean Main.lean succeeded",
            "warning_cleanup_status": "verified",
            "warning_cleanup_attempted": True,
            "warning_cleanup_verified": True,
            "warning_cleanup_warning_count": 0,
            "project_sorry_count": 0,
            "latest_checkpoint_label": "[none]",
            "held_locks": 0,
            "updated_at": "2026-04-22T14:33:57+00:00",
        },
        activities=[],
    )

    output = console.export_text()
    assert "Warning cleanup" in output
    assert "verified; attempted, verified; warnings 0" in output


def test_list_runtime_provider_targets_includes_local_and_zai():
    names = {entry["name"] for entry in list_runtime_provider_targets()}

    assert "local" in names
    assert "zai" in names
    assert "custom" in names


def test_main_mcp_status_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "leanflow_cli.main.get_mcp_status",
        lambda: [{"name": "lean-lsp", "transport": "stdio", "tools": 3, "connected": True}],
    )

    assert main(["mcp", "status", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"name": "lean-lsp"' in output


def test_interactive_mcp_status_prints_sampling_metrics(monkeypatch, capsys):
    shell = InteractiveShell()
    monkeypatch.setattr(
        "leanflow_cli.shell.get_mcp_status",
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


def test_main_mcp_bootstrap_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "leanflow_cli.main.bootstrap_lean_mcp",
        lambda python_bin=None: {
            "success": True,
            "home": "/tmp/home",
            "config_path": "/tmp/home/config.yaml",
            "servers": [
                {"name": "lean-lsp", "role": "primary-state-search", "command": "/tmp/lean-lsp-mcp"}
            ],
        },
    )

    assert main(["mcp", "bootstrap", "lean", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"success": true' in output.lower()
    assert '"name": "lean-lsp"' in output


def test_interactive_mcp_bootstrap_prints_summary(monkeypatch, capsys):
    shell = InteractiveShell()
    monkeypatch.setattr(
        "leanflow_cli.shell.bootstrap_lean_mcp",
        lambda: {
            "success": True,
            "home": "/tmp/home",
            "config_path": "/tmp/home/config.yaml",
            "remote_search_policy": "public-fallbacks-enabled",
            "servers": [
                {
                    "name": "lean-proof-auto",
                    "role": "secondary-automation-context",
                    "command": "/tmp/lean-proof-auto-mcp",
                }
            ],
        },
    )

    assert shell._run_mcp_command("/mcp bootstrap lean") == 0
    output = capsys.readouterr().out
    assert "Managed Lean MCP bootstrap complete" in output
    assert "lean-proof-auto" in output
    assert "public-fallbacks-enabled" in output


def test_project_init_prints_repl_setup_progress(monkeypatch, tmp_path, capsys):
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "Demo"\n', encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    def _fake_setup(_lean_root, *, progress=None):
        if progress:
            progress("[1/6] Inspecting Lean project for REPL acceleration")
            progress(
                "[6/6] Building REPL binary with `lake build repl` (this can take several minutes)"
            )
        return {"status": "ready", "repl_path": str(root / ".lake" / "build" / "bin" / "repl")}

    monkeypatch.setattr("leanflow_cli.main.setup_project_power_modes", _fake_setup)

    assert main(["project", "init", str(root)]) == 0
    output = capsys.readouterr().out
    assert "Inspecting Lean project for REPL acceleration" in output
    assert "lake build repl" in output
    assert "REPL acceleration ready" in output


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
            "model": "google/gemma-3-27b-it",
            "base_url": "http://127.0.0.1:8000/v1",
        },
        child_env={"LEANFLOW_NATIVE_ACTIVE_SKILL": "lean-proof-loop"},
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    summary = describe_launch_plan(plan)

    assert summary["provider"] == "local:vllm"
    assert summary["model"] == "google/gemma-3-27b-it"
    assert summary["command"] == "/prove Main.lean"
    assert summary["skill"] == "lean-proof-loop"
    assert summary["agents"] == "1"


def test_resolve_workflow_request_normalizes_requested_active_file(tmp_path):
    root = tmp_path / "ProveDemo"
    (root / ".leanflow").mkdir(parents=True)
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: ProveDemo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()
    (root / "lakefile.toml").write_text("name = 'ProveDemo'\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / "ProveDemo").mkdir()
    (root / "ProveDemo" / "RealTheorems-homework.lean").write_text(
        "theorem t : True := by\n  trivial\n", encoding="utf-8"
    )

    plan = resolve_workflow_request(
        "/prove ./ProveDemo/ProveDemo/RealTheorems-homework.lean",
        active_cwd=root,
    )

    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == "ProveDemo/RealTheorems-homework.lean"
    assert (
        plan.child_env["LEANFLOW_NATIVE_WORKFLOW_COMMAND"]
        == "/prove ProveDemo/RealTheorems-homework.lean"
    )
    assert plan.workflow.workflow_args == "ProveDemo/RealTheorems-homework.lean"


def test_resolve_workflow_request_recovers_similar_requested_active_file(tmp_path):
    root = tmp_path / "ProveDemo"
    (root / ".leanflow").mkdir(parents=True)
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: ProveDemo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()
    (root / "lakefile.toml").write_text("name = 'ProveDemo'\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / "ProveDemo").mkdir()
    (root / "ProveDemo" / "RealTheorems-homework.lean").write_text(
        "theorem t : True := by\n  trivial\n", encoding="utf-8"
    )

    plan = resolve_workflow_request(
        "/prove ./wrong/subdir/RealTheorems-homework.lean",
        active_cwd=root,
    )

    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == "ProveDemo/RealTheorems-homework.lean"
    assert (
        plan.child_env["LEANFLOW_NATIVE_WORKFLOW_COMMAND"]
        == "/prove ProveDemo/RealTheorems-homework.lean"
    )
    assert plan.workflow.workflow_args == "ProveDemo/RealTheorems-homework.lean"


def test_interactive_workflow_launch_spawns_background_runner(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
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
        runtime={
            "provider": "custom",
            "model": "zai-org/GLM-5.1",
            "base_url": "https://inference.rcp.epfl.ch/v1",
        },
        child_env={"LEANFLOW_NATIVE_PROCESS_TOKEN": "shell-launch-token"},
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    monkeypatch.setattr(
        "leanflow_cli.shell.resolve_workflow_request", lambda *args, **kwargs: fake_plan
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.describe_launch_plan",
        lambda plan: {
            "workflow": "prove",
            "command": "/prove Main.lean",
            "project": "Demo",
            "project_root": str(tmp_path),
            "provider": "custom",
            "base_url": "https://inference.rcp.epfl.ch/v1",
            "model": "zai-org/GLM-5.1",
            "skill": "lean-proof-loop",
            "agents": "1",
        },
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state._process_seems_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state.process_identity_matches",
        lambda identity: True,
    )

    class _FakeProcess:
        pid = 43210

    monkeypatch.setattr(
        "leanflow_cli.shell.spawn_workflow", lambda *args, **kwargs: (fake_plan, _FakeProcess())
    )
    monkeypatch.setattr("leanflow_cli.shell.load_workflow_live_status", lambda: {})

    assert shell._run_workflow_command("/prove Main.lean") == 0
    output = capsys.readouterr().out
    assert "Workflow running in background" in output
    assert "43210" in output
    payload = load_workflow_live_status()
    assert payload["phase"] == "busy"
    assert payload["build_status"] == "workflow launching"
    assert payload["process_id"] == 43210
    assert payload["process_group_id"] == 43210
    assert payload["process_session_id"] == 43210
    assert len(payload["process_token_sha256"]) == 64


def test_interactive_workflow_launch_reuses_existing_matching_runner(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
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
        runtime={
            "provider": "custom",
            "model": "zai-org/GLM-5.1",
            "base_url": "https://inference.rcp.epfl.ch/v1",
        },
        child_env={},
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    monkeypatch.setattr(
        "leanflow_cli.shell.resolve_workflow_request", lambda *args, **kwargs: fake_plan
    )
    monkeypatch.setattr(
        shell,
        "_workflow_agents",
        lambda activity_limit=1: [
            {
                "agent_id": "12345",
                "parent_agent_id": "",
                "project_root": str(tmp_path),
                "workflow_kind": "prove",
                "workflow_command": "/prove Main.lean",
                "status": "paused",
            }
        ],
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.spawn_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not spawn duplicate workflow")
        ),
    )

    assert shell._run_workflow_command("/prove Main.lean") == 0
    output = capsys.readouterr().out
    assert "Workflow already running" in output


def test_workflow_cli_provider_override_passes_through(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    fake_plan = NativeLaunchPlan(
        project=type("Project", (), {"label": "Demo", "root": tmp_path})(),
        workflow=NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/prove Main.lean",
            workflow_args="Main.lean",
        ),
        runtime={
            "provider": "openai-codex",
            "model": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "reasoning_effort": "xhigh",
        },
        child_env={},
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    def fake_resolve(command, **kwargs):
        captured["resolve_command"] = command
        captured["resolve_provider"] = kwargs.get("requested_provider")
        return fake_plan

    def fake_run(command, **kwargs):
        captured["run_command"] = command
        captured["run_provider"] = kwargs.get("requested_provider")
        return 0

    monkeypatch.setattr("leanflow_cli.main.resolve_workflow_request", fake_resolve)
    monkeypatch.setattr("leanflow_cli.main.run_workflow", fake_run)

    assert main(["workflow", "--provider", "codex", "prove", "Main.lean"]) == 0
    assert captured["resolve_command"] == "/prove Main.lean"
    assert captured["run_command"] == "/prove Main.lean"
    assert captured["resolve_provider"] == "codex"
    assert captured["run_provider"] == "codex"


def test_workflow_cli_preserves_multiword_option_values(monkeypatch, tmp_path):
    """Decoded argv values must remain atomic through the string parser boundary."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    captured: dict[str, str] = {}
    fake_plan = NativeLaunchPlan(
        project=type("Project", (), {"label": "Demo", "root": tmp_path})(),
        workflow=NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/prove Main.lean",
            workflow_args="Main.lean",
        ),
        runtime={"provider": "custom", "model": "zai-org/GLM-5.2", "base_url": "https://example"},
        child_env={},
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-prove-worker",
    )

    def fake_resolve(command, **_kwargs):
        captured["resolve"] = command
        return fake_plan

    def fake_run(command, **_kwargs):
        captured["run"] = command
        return 0

    monkeypatch.setattr("leanflow_cli.main.resolve_workflow_request", fake_resolve)
    monkeypatch.setattr("leanflow_cli.main.run_workflow", fake_run)

    argv = [
        "workflow",
        "prove",
        "Main.lean",
        "--clean-room-label",
        "IMO 2026 Problem 2",
        "--goal",
        "finish theorem result",
    ]
    assert main(argv) == 0

    for command in (captured["resolve"], captured["run"]):
        parsed = parse_workflow_command(command)
        assert parsed.workflow_args == "Main.lean"
        assert parsed.clean_room_labels == ("IMO 2026 Problem 2",)
        assert parsed.explicit_goal == "finish theorem result"


def test_workflow_leaf_help_never_launches_runtime(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "leanflow_cli.main.resolve_workflow_request",
        lambda *args, **kwargs: pytest.fail("help resolved a workflow"),
    )
    monkeypatch.setattr(
        "leanflow_cli.main.run_workflow",
        lambda *args, **kwargs: pytest.fail("help launched a workflow"),
    )

    assert main(["workflow", "prove", "--help"]) == 0

    output = capsys.readouterr().out
    assert "usage: leanflow workflow prove FILE" in output
    assert "--research-workers N" in output
    assert "--model MODEL" in output
    assert "--clean-room" in output
    assert "--clean-room-label VALUE" in output
    assert "--no-parallel" in output


def test_combined_status_skips_expensive_sandbox_probe_unless_verbose(
    monkeypatch, tmp_path, capsys
):
    calls = []
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(main_module, "load_workflow_live_status", lambda: {})
    monkeypatch.setattr(
        main_module,
        "sandbox_status",
        lambda **kwargs: calls.append(kwargs)
        or {
            "engine": "auto",
            "engine_ready": None,
            "image": "leanflow",
            "image_ready": None,
            "recent_runs": [],
        },
    )

    assert main(["status", "--json"]) == 0
    assert calls[-1] == {"probe_engine": False, "recent_run_limit": 3}
    capsys.readouterr()
    assert main(["status", "--json", "--verbose"]) == 0
    assert calls[-1] == {"probe_engine": True, "recent_run_limit": 8}


def test_interactive_project_init_reports_already_initialized(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    shell = InteractiveShell()

    assert shell._run_project_command(["init", str(root)]) == 0
    first = capsys.readouterr().out
    assert "Initialized project: Demo" in first

    assert shell._run_project_command(["init", str(root)]) == 0
    second = capsys.readouterr().out
    assert "Project already initialized: Demo" in second


def test_interactive_workflow_log_prints_saved_runner_log(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    reset_workflow_run_log()
    append_workflow_run_log("alpha\nbeta\ngamma\n")

    shell = InteractiveShell()

    assert shell._run_workflow_status_command(["log", "2"]) == 0
    output = capsys.readouterr().out
    assert "beta" in output
    assert "gamma" in output


def test_swarm_command_renders_agent_table(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        model="google/gemma-3-27b-it",
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
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()

    monkeypatch.setattr(
        "leanflow_cli.shell.terminate_workflow_agent",
        lambda agent_id: {"success": True, "agent_id": "12345", "process_id": 24680},
    )

    assert shell._run_kill_command(["12345"]) == 0
    output = capsys.readouterr().out
    assert "Sent interrupt to workflow agent" in output
    assert "12345" in output


def test_shell_exit_interrupts_current_project_workflows(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root

    monkeypatch.setattr(
        "leanflow_cli.shell.terminate_project_workflow_agents",
        lambda project_root: {"success": True, "count": 1, "terminated": ["12345"], "failed": []},
    )

    assert shell._handle_command("/exit") is False
    output = capsys.readouterr().out
    assert "Interrupted 1 workflow agent(s)" in output
    assert "Demo before exiting the shell" in output


def test_shell_exit_requests_clean_runner_exit_before_escalating(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root
    seen: list[str] = []

    monkeypatch.setattr(
        "leanflow_cli.shell.request_project_workflow_runner_exit",
        lambda project_root: {"success": True, "count": 1, "queued": ["12345"], "failed": []},
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.workflow_agent_detail",
        lambda agent_id, activity_limit=1: {
            "agent_id": agent_id,
            "status": "exited",
            "process_id": 0,
        },
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.terminate_project_workflow_agents",
        lambda project_root: (
            seen.append(project_root)
            or {"success": True, "count": 1, "terminated": ["12345"], "failed": []}
        ),
    )

    assert shell._handle_command("/exit") is False
    output = capsys.readouterr().out
    assert not seen
    assert "Requested clean exit for 1 workflow runner(s)" in output


def test_shell_exit_interrupts_live_runner_when_no_registered_agents(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root
    signalled: list[tuple[str, int, int]] = []

    monkeypatch.setattr(
        "leanflow_cli.shell.terminate_project_workflow_agents",
        lambda project_root: {"success": True, "count": 0, "terminated": [], "failed": []},
    )
    monkeypatch.setattr(
        shell,
        "_workflow_status_payload",
        lambda: {"project_root": str(root), "phase": "paused", "process_id": 24680},
    )

    def interrupt(status):
        signalled.append(("verified", int(status["process_id"]), 2))
        return {"success": True, "process_id": int(status["process_id"])}

    monkeypatch.setattr("leanflow_cli.shell.interrupt_workflow_process", interrupt)

    assert shell._handle_command("/exit") is False
    output = capsys.readouterr().out
    assert signalled == [("verified", 24680, 2)]
    assert "Interrupted background workflow runner (pid 24680)" in output


def test_swarm_agent_view_renders_transcript_not_status_panel(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()

    agent = {
        "agent_id": "12345",
        "status": "verified",
        "model": "zai-org/GLM-5.1",
        "parent_agent_id": "",
    }
    transcript = [
        {
            "timestamp": "2026-04-20T10:00:00+00:00",
            "type": "assistant-response",
            "role": "assistant",
            "content": "Initial pass done.",
        },
    ]
    queued = {}
    prompts = iter(["Try the continuity lemma next.", "/exit"])

    monkeypatch.setattr("leanflow_cli.shell.resolve_workflow_agent_id", lambda ref: "12345")
    monkeypatch.setattr(
        "leanflow_cli.shell.workflow_agent_detail", lambda *args, **kwargs: dict(agent)
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.workflow_agent_transcript", lambda *args, **kwargs: list(transcript)
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.workflow_agent_transcript_all", lambda *args, **kwargs: list(transcript)
    )
    monkeypatch.setattr(
        "leanflow_cli.shell.enqueue_workflow_agent_message",
        lambda agent_id, text: queued.setdefault(
            "payload", {"success": True, "agent_id": agent_id, "text": text}
        ),
    )
    monkeypatch.setattr(shell.session, "prompt", lambda *args, **kwargs: next(prompts))

    assert shell._run_swarm_command(["12345", "5"]) == 0
    output = capsys.readouterr().out
    assert "Agent 12345 is verified" in output
    assert "Queued prompt for agent 12345" in output
    assert queued["payload"]["text"] == "Try the continuity lemma next."


def test_status_agent_detail_renders_recent_activity(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root

    fragments = shell._prompt_message()
    text = "".join(fragment for _, fragment in fragments)
    assert "Demo" in text
    assert "idle" in text
    assert text.endswith("\n› ")


def test_prompt_message_hides_unknown_theorem_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()
    save_workflow_live_status(
        {
            "phase": "busy",
            "target_symbol": "[unknown]",
            "active_file_label": "ProveDemo/RealTheorems-homework.lean",
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
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".leanflow").mkdir()
    (root / ".leanflow" / "project.yaml").write_text(
        "schema_version: 1\nname: Demo\nkind: lean4\nlean_root: .\ncreated_at: now\npaths:\n  runtime: .leanflow/runtime\n  cache: .leanflow/cache\n  workflows: .leanflow/workflows\nsource:\n  mode: init\n  template_source: ''\nblueprint:\n  markers: []\n",
        encoding="utf-8",
    )
    (root / ".leanflow" / "runtime").mkdir()
    (root / ".leanflow" / "cache").mkdir()
    (root / ".leanflow" / "workflows").mkdir()

    shell = InteractiveShell()
    shell.cwd = root

    assert shell._run_project_command([]) == 0
    output = capsys.readouterr().out
    assert "Demo" in output
    assert "LeanFlow Project" in output


def test_project_command_without_args_has_polished_empty_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    shell = InteractiveShell()
    shell.cwd = tmp_path

    assert shell._run_project_command([]) == 1
    output = capsys.readouterr().out
    assert "No active Lean workspace is open here." in output


def test_main_loads_leanflow_dotenv_before_provider_resolution(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("OPENROUTER_API_KEY=or-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    captured: dict[str, str] = {}

    def _fake_render_provider_panel(console, *, resolved, requested, targets):
        captured["api_key"] = str(resolved.get("api_key", ""))
        captured["provider"] = str(resolved.get("provider", ""))

    monkeypatch.setattr("leanflow_cli.main.render_provider_panel", _fake_render_provider_panel)

    assert main(["provider", "--requested", "openrouter"]) == 0
    assert captured["provider"] == "openrouter"
    assert captured["api_key"] == "or-from-dotenv"


def test_provider_command_accepts_provider_alias(monkeypatch):
    captured: dict[str, str] = {}

    def _fake_render_provider_panel(console, *, resolved, requested, targets):
        captured["requested"] = str(requested or "")

    monkeypatch.setattr("leanflow_cli.main.render_provider_panel", _fake_render_provider_panel)
    monkeypatch.setattr(
        "leanflow_cli.main.resolve_runtime_provider",
        lambda requested=None: {"provider": requested or "auto"},
    )

    assert main(["provider", "--provider", "codex"]) == 0
    assert captured["requested"] == "codex"


def test_main_prefers_leanflow_scoped_env_names(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "LEANFLOW_OPENAI_BASE_URL=https://rcp.epfl.example/v1\n"
        "LEANFLOW_OPENAI_API_KEY=leanflow-rcp-key\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "OPENAI_API_KEY=global-openai-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.delenv("LEANFLOW_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LEANFLOW_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    captured: dict[str, str] = {}

    def _fake_render_provider_panel(console, *, resolved, requested, targets):
        captured["api_key"] = str(resolved.get("api_key", ""))
        captured["base_url"] = str(resolved.get("base_url", ""))
        captured["provider"] = str(resolved.get("provider", ""))

    monkeypatch.setattr("leanflow_cli.main.render_provider_panel", _fake_render_provider_panel)

    assert main(["provider", "--requested", "custom"]) == 0
    assert captured["provider"] == "custom"
    assert captured["base_url"] == "https://rcp.epfl.example/v1"
    assert captured["api_key"] == "leanflow-rcp-key"
