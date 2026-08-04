"""Regression tests for scratch-only research terminal enforcement."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import tools.implementations.terminal_tool  # noqa: F401 - load real module below
from tools.utilities import scratch_terminal_guard
from tools.utilities.scratch_terminal_guard import validate_scratch_terminal_command

terminal_module = sys.modules["tools.implementations.terminal_tool"]


@pytest.fixture(autouse=True)
def trusted_read_only_executables(monkeypatch):
    """Keep command-resolution tests independent of host package layout."""
    monkeypatch.setattr(
        scratch_terminal_guard.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la FormalConjectures",
        "rg -n 'erdos_242' FormalConjectures | head -20",
        "grep -n sorry FormalConjectures/ErdosProblems/242.lean",
        "git status --short",
        "git diff -- FormalConjectures/ErdosProblems/242.lean",
        "find FormalConjectures -name '*.lean' -print",
        "lake env lean FormalConjectures/ErdosProblems/242.lean",
    ],
)
def test_guard_allows_audited_read_only_diagnostics(tmp_path, command):
    decision = validate_scratch_terminal_command(
        command,
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is True
    assert decision.reason == ""


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf FormalConjectures",
        "mv A.lean B.lean",
        "cp A.lean B.lean",
        "install -m 644 A.lean B.lean",
        "sed -i 's/sorry/by simp/' A.lean",
        "black .",
        "ruff check --fix .",
        "prettier --write A.lean",
        "git restore -- A.lean",
        "git reset --hard",
        "git apply change.patch",
        "git diff --output=patch.txt",
        "find . -delete",
        "find . -exec rm {} +",
        "file --compile custom.magic",
        "file --compile=custom.magic",
        "rg --pre 'rm -rf FormalConjectures' theorem .",
        "lake build",
        "lake env sh -c 'rm A.lean'",
        "lake env lean -o A.olean A.lean",
        "lake env lean -bA.bc A.lean",
        "lake env lean --setup=Setup.json A.lean",
        "lake env lean -R.. A.lean",
        "lean --run A.lean",
    ],
)
def test_guard_blocks_mutating_or_exec_capable_commands(tmp_path, command):
    decision = validate_scratch_terminal_command(
        command,
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is False
    assert decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "cat A.lean > B.lean",
        "cat A.lean >> B.lean",
        "cat A.lean 2>/dev/null",
        "cat A.lean | tee B.lean",
        "rg theorem . && rm A.lean",
        "rg theorem .; cp A.lean B.lean",
        "sh -c 'rm A.lean'",
        "bash -lc 'cp A.lean B.lean'",
        "env sh -c 'mv A.lean B.lean'",
        "command rm A.lean",
        "./read-only-looking-wrapper A.lean",
        'python -c \'open("A.lean", "w").write("sorry")\'',
        "echo $(rm A.lean)",
        'rg "$(rm A.lean)" .',
        "FOO=1 rg theorem .",
        "rg theorem . &",
    ],
)
def test_guard_blocks_shell_escape_surfaces_and_writer_pipelines(tmp_path, command):
    decision = validate_scratch_terminal_command(
        command,
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is False


def test_guard_blocks_workdir_and_lean_input_path_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    escaped_cwd = validate_scratch_terminal_command(
        "rg theorem .",
        workdir=str(outside),
        project_root=str(project),
    )
    escaped_lean_input = validate_scratch_terminal_command(
        "lake env lean ../outside/Probe.lean",
        workdir=str(project),
        project_root=str(project),
    )

    assert escaped_cwd.allowed is False
    assert escaped_lean_input.allowed is False


def test_guard_resolves_relative_workdir_before_checking_lean_input(tmp_path):
    project = tmp_path / "project"
    (project / "subdir").mkdir(parents=True)

    local_input = validate_scratch_terminal_command(
        "lake env lean Main.lean",
        workdir="subdir",
        project_root=str(project),
    )
    escaped_input = validate_scratch_terminal_command(
        "lake env lean ../../Outside.lean",
        workdir="subdir",
        project_root=str(project),
    )

    assert local_input.allowed is True
    assert local_input.workdir == str((project / "subdir").resolve())
    assert escaped_input.allowed is False


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "rg root /etc",
        "find /tmp -name '*.lean' -print",
        "jq -f ~/.ssh/config Project.json",
        "grep --file=/etc/passwd theorem Main.lean",
        "rg --ignore-file ../../host-ignore theorem .",
        "readlink ../../outside",
    ],
)
def test_guard_confines_all_read_operands_to_project(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()

    decision = validate_scratch_terminal_command(
        command,
        workdir=str(project),
        project_root=str(project),
    )

    assert decision.allowed is False
    assert "project" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "file -f/etc/passwd",
        "file -f host-paths.txt",
        "file --files-from=host-paths.txt",
        "file -z archive.gz",
        "find -files0-from host-paths.txt -print",
        "du --files0-from=host-paths.txt",
        "wc --files0-from host-paths.txt",
        "md5sum --check checksums.txt",
        "sha256sum -cchecksums.txt",
        "shasum -c checksums.txt",
        "diff3 --diff-program sh A B C",
        "tail -f Main.lean",
        "tail --pid=1 --follow=name Main.lean",
    ],
)
def test_guard_rejects_second_order_reads_exec_and_persistent_watchers(tmp_path, command):
    """A local manifest must not smuggle host paths into an allowed reader."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "host-paths.txt").write_text("/etc/passwd\n", encoding="utf-8")
    (project / "checksums.txt").write_text(
        "0" * 64 + "  /etc/passwd\n",
        encoding="utf-8",
    )

    decision = validate_scratch_terminal_command(
        command,
        workdir=str(project),
        project_root=str(project),
    )

    assert decision.allowed is False
    assert decision.reason


def test_guard_rejects_project_symlink_that_reads_outside(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret", encoding="utf-8")
    (project / "escape").symlink_to(outside)

    decision = validate_scratch_terminal_command(
        "cat escape",
        workdir=str(project),
        project_root=str(project),
    )

    assert decision.allowed is False
    assert "project" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "rg -L secret .",
        "grep -Rn secret .",
        "find -L . -type f -print",
        "du -shL .",
        "file -L Main.lean",
        "ls -laL .",
    ],
)
def test_guard_rejects_recursive_symlink_follow_modes(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()
    (project / "escape").symlink_to(tmp_path)

    decision = validate_scratch_terminal_command(
        command,
        workdir=str(project),
        project_root=str(project),
    )

    assert decision.allowed is False
    assert "symlink" in decision.reason


def test_guard_keeps_project_local_files_readable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Main.lean").write_text("theorem stable : True := by trivial\n", encoding="utf-8")

    decision = validate_scratch_terminal_command(
        "cat Main.lean",
        workdir=str(project),
        project_root=str(project),
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    "command",
    [
        "ps eww -ax",
        "ps auxe",
        "ps -axo pid=,command=",
        "pgrep -a leanflow",
        "pgrep --list-full leanflow",
        "jq -n env",
        "pgrep -F/etc/host.pid leanflow",
    ],
)
def test_guard_rejects_process_environment_and_full_argument_disclosure(tmp_path, command):
    decision = validate_scratch_terminal_command(
        command,
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is False


def test_guard_keeps_bounded_process_topology_diagnostics(tmp_path):
    decision = validate_scratch_terminal_command(
        "ps -axo pid=,ppid=,pgid=,stat=",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is True


def test_guard_renders_arguments_without_shell_glob_expansion(tmp_path):
    decision = validate_scratch_terminal_command(
        "rg theorem * | head -5",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is True
    assert decision.command == "/usr/bin/rg --no-config theorem '*' | /usr/bin/head -5"
    assert decision.workdir == str(tmp_path.resolve())


def test_guard_hardens_read_only_git_against_index_and_external_helpers(tmp_path):
    decision = validate_scratch_terminal_command(
        "git diff -- A.lean",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert decision.allowed is True
    assert decision.command == (
        "GIT_OPTIONAL_LOCKS=0 /usr/bin/git --no-pager -c core.fsmonitor=false "
        "diff --no-ext-diff --no-textconv -- A.lean"
    )


def test_guard_rejects_missing_and_project_local_executables(monkeypatch, tmp_path):
    monkeypatch.setattr(scratch_terminal_guard.shutil, "which", lambda _name: None)
    missing = validate_scratch_terminal_command(
        "rg theorem .",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )
    project_rg = tmp_path / "rg"
    monkeypatch.setattr(scratch_terminal_guard.shutil, "which", lambda _name: str(project_rg))
    project_local = validate_scratch_terminal_command(
        "rg theorem .",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
    )

    assert missing.allowed is False
    assert project_local.allowed is False


def _terminal_config(cwd: str) -> dict[str, object]:
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 30,
    }


def test_terminal_boundary_denies_before_environment_creation_even_with_force(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(terminal_module, "_active_environments", {})
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("denied scratch command created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "cp A.lean B.lean",
            task_id="scratch-guard",
            force=True,
        )
    )

    assert payload["status"] == "scratch_only_terminal_denied"
    assert payload["exit_code"] == -1
    assert "read-only" in payload["error"]


def test_empirical_worker_still_cannot_run_python_through_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ARCHETYPE", "empirical")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("denied empirical Python created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "python3 -c 'print(2 + 2)'",
            task_id="empirical-terminal-guard",
            force=True,
        )
    )

    assert payload["status"] == "scratch_only_terminal_denied"
    assert "outside the read-only diagnostic allowlist" in payload["error"]


@pytest.mark.parametrize("mode", ["background", "pty"])
def test_terminal_boundary_rejects_unbounded_execution_modes(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(terminal_module, "_active_environments", {})
    kwargs = {mode: True}

    payload = json.loads(
        terminal_module.terminal_tool(
            "rg theorem .",
            task_id="scratch-guard",
            **kwargs,
        )
    )

    assert payload["status"] == "scratch_only_terminal_denied"


def test_terminal_boundary_executes_read_only_scratch_command(monkeypatch, tmp_path):
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return {"output": "FormalConjectures/ErdosProblems/242.lean", "returncode": 0}

    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_active_environments",
        {"scratch-guard": SimpleNamespace(execute=execute)},
    )
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "rg -l erdos_242 FormalConjectures | head -1",
            task_id="scratch-guard",
        )
    )

    assert payload["exit_code"] == 0
    assert calls == [
        (
            "/usr/bin/rg --no-config -l erdos_242 FormalConjectures | /usr/bin/head -1",
            {"timeout": 30, "cwd": str(tmp_path.resolve())},
        )
    ]


def test_scratch_terminal_denies_nonlocal_backend_before_environment_creation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setattr(
        terminal_module,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": str(tmp_path), "timeout": 30},
    )
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("nonlocal scratch terminal created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "rg theorem .",
            task_id="scratch-guard",
        )
    )

    assert payload["status"] == "scratch_only_terminal_denied"
    assert "local backend" in payload["error"]


def test_solution_clean_room_denies_nonlocal_terminal_before_environment_creation(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", raising=False)
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setattr(
        terminal_module,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": str(tmp_path), "timeout": 30},
    )
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("nonlocal clean-room terminal created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "rg theorem .",
            task_id="clean-room-guard",
        )
    )

    assert payload["status"] == "clean_room_terminal_denied"
    assert "local backend" in payload["error"]


def test_ordinary_foreground_terminal_keeps_mutating_command_authority(monkeypatch, tmp_path):
    calls: list[str] = []

    def execute(command, **_kwargs):
        calls.append(command)
        return {"output": "", "returncode": 0}

    monkeypatch.delenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", raising=False)
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_active_environments",
        {"foreground": SimpleNamespace(execute=execute)},
    )
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "cp A.lean B.lean",
            task_id="foreground",
        )
    )

    assert payload["exit_code"] == 0
    assert calls == ["cp A.lean B.lean"]


def test_clean_room_terminal_denies_git_before_environment_creation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("denied Git command created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "git clone https://example.com/repo.git",
            task_id="clean-room",
            force=True,
        )
    )

    assert payload["status"] == "repository_research_denied"
    assert payload["exit_code"] == -1
    assert "Git commands are disabled" in payload["error"]


def test_clean_room_foreground_terminal_cannot_read_outside_project(monkeypatch, tmp_path):
    outside = tmp_path.parent / "prior-solution.lean"
    outside.write_text("theorem leaked : True := by trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kwargs: pytest.fail("escaped clean-room read created an environment"),
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            f"cat {outside}",
            task_id="clean-room",
            force=True,
        )
    )

    assert payload["status"] == "clean_room_terminal_denied"
    assert payload["exit_code"] == -1
    assert "inside the assigned project" in payload["error"]


def test_clean_room_foreground_terminal_keeps_project_lean_checks(monkeypatch, tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return {"output": "", "returncode": 0}

    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: _terminal_config(str(tmp_path)))
    monkeypatch.setattr(
        terminal_module,
        "_active_environments",
        {"clean-room": SimpleNamespace(execute=execute)},
    )
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    payload = json.loads(
        terminal_module.terminal_tool(
            "lake env lean Main.lean",
            task_id="clean-room",
        )
    )

    assert payload["exit_code"] == 0
    assert calls == [
        (
            "/usr/bin/lake env lean Main.lean",
            {"timeout": 30, "cwd": str(tmp_path.resolve())},
        )
    ]
