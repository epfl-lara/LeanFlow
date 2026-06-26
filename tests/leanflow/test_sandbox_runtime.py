from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.runtime.sandbox_runtime import (
    SandboxSettings,
    build_sandbox_image,
    container_run_command,
    copy_project_tree,
    export_sandbox_patch,
    normalize_leanflow_args,
    prepare_sandbox_run,
    resolve_container_engine,
    sandbox_status,
)
from leanflow_cli.workflows.project import initialize_leanflow_project


def _settings(tmp_path: Path) -> SandboxSettings:
    env_file = tmp_path / "keys.env"
    env_file.write_text("LEANFLOW_OPENAI_API_KEY=test-key\n", encoding="utf-8")
    return SandboxSettings(
        engine="docker",
        image="leanflow/test:local",
        env_file=env_file,
        cache_dir=tmp_path / "cache",
        runs_dir=tmp_path / "runs",
    )


def test_normalize_leanflow_args_accepts_shell_style_workflows() -> None:
    assert normalize_leanflow_args(["/prove", "Main.lean"]) == ("workflow", "prove", "Main.lean")
    assert normalize_leanflow_args(["autoformalize", "docs/Foo"]) == (
        "workflow",
        "autoformalize",
        "docs/Foo",
    )
    assert normalize_leanflow_args(["workflow", "status"]) == ("workflow", "status")


def test_resolve_container_engine_prefers_rootless_podman_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.sys.platform", "linux")
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"podman", "docker"} else None,
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.check_container_engine_usable", lambda _name: ""
    )
    assert resolve_container_engine("auto") == "podman"


def test_resolve_container_engine_falls_back_to_usable_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.sys.platform", "linux")
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"podman", "docker"} else None,
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.check_container_engine_usable",
        lambda name: "podman broken" if name == "podman" else "",
    )
    assert resolve_container_engine("auto") == "docker"


def test_copy_project_tree_excludes_state_and_secrets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (project / "Main.lean").write_text("def x := 1\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("git", encoding="utf-8")
    (project / ".lake").mkdir()
    (project / ".lake" / "build").write_text("cache", encoding="utf-8")
    (project / ".leanflow" / "workflow-state").mkdir(parents=True)
    (project / ".leanflow" / "workflow-state" / "status.json").write_text("{}", encoding="utf-8")
    (project / ".leanflow" / "project.yaml").write_text(
        "name: demo\nlean_root: .\n", encoding="utf-8"
    )

    destination = tmp_path / "copy"
    copy_project_tree(project, destination)

    assert (destination / "Main.lean").is_file()
    assert (destination / ".leanflow" / "project.yaml").is_file()
    assert not (destination / ".env").exists()
    assert not (destination / ".git").exists()
    assert not (destination / ".lake").exists()
    assert not (destination / ".leanflow" / "workflow-state").exists()


def test_prepare_sandbox_run_commits_baseline_and_preserves_manifest(tmp_path: Path) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (project_root / "Main.lean").write_text("def x := 1\n", encoding="utf-8")
    initialize_leanflow_project(project_root, name="demo")

    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["prove", "Main.lean"],
        run_id="run-1",
        settings=_settings(tmp_path),
    )

    assert run.command == ("workflow", "prove", "Main.lean")
    assert run.worktree.is_dir()
    assert (run.worktree / ".leanflow" / "project.yaml").is_file()
    assert (run.worktree / ".git").is_dir()


def test_container_run_command_mounts_only_sandbox_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (project_root / "Main.lean").write_text("def x := 1\n", encoding="utf-8")
    initialize_leanflow_project(project_root, name="demo")
    settings = _settings(tmp_path)
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["workflow", "status"],
        run_id="run-2",
        settings=settings,
    )

    argv = container_run_command(
        engine="docker",
        image=settings.image,
        sandbox_run=run,
        settings=settings,
        tty=False,
    )
    joined = "\n".join(argv)
    assert f"src={run.worktree.resolve()},dst=/workspace" in joined
    assert f"src={run.run_dir.resolve()},dst=/sandbox-run" in joined
    assert str(project_root.resolve()) not in joined
    assert "--read-only" in argv
    assert "--env-file" in argv
    assert "/opt/leanflow/.venv/bin/leanflow" in joined


def test_export_sandbox_patch_captures_worktree_edits(tmp_path: Path) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (project_root / "Main.lean").write_text("def x := 1\n", encoding="utf-8")
    initialize_leanflow_project(project_root, name="demo")
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["project", "show"],
        run_id="run-patch",
        settings=_settings(tmp_path),
    )

    (run.worktree / "Main.lean").write_text("def x := 2\n", encoding="utf-8")

    assert export_sandbox_patch(run) is True
    assert "def x := 2" in run.patch_path.read_text(encoding="utf-8")


def test_sandbox_status_reports_missing_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.load_config", lambda: {})
    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.shutil.which", lambda _name: None)

    payload = sandbox_status()

    assert payload["engine_ready"] is False
    assert payload["image_ready"] is False
    assert "Install Docker or Podman" in payload["engine_error"]


def test_sandbox_status_reports_unusable_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.settings_from_config", lambda **_kwargs: settings
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.resolve_container_engine", lambda _requested: "docker"
    )

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(["docker", "info"], 1, stderr="permission denied\n")

    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.subprocess.run", _fake_run)

    payload = sandbox_status()

    assert payload["engine_ready"] is False
    assert payload["image_ready"] is False
    assert "permission denied" in payload["engine_error"]


def test_sandbox_status_includes_recent_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    run_dir = settings.runs_dir / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps({"run_id": "run-a", "status": "failed"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.settings_from_config", lambda **_kwargs: settings
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.resolve_container_engine", lambda _requested: "docker"
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.check_container_engine_usable", lambda _engine: ""
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.image_exists", lambda _engine, _image: True
    )

    payload = sandbox_status()

    assert payload["engine_ready"] is True
    assert payload["image_ready"] is True
    assert payload["recent_runs"][0]["run_id"] == "run-a"


def test_sandbox_containerfile_installs_workflow_cli_dependencies() -> None:
    repo = Path(__file__).resolve().parents[2]
    containerfile = (repo / "containers" / "leanflow-sandbox.Containerfile").read_text(
        encoding="utf-8"
    )

    assert "ripgrep" in containerfile
    assert "poppler-utils" in containerfile


def test_build_sandbox_image_can_bake_local_lean_explore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "containers").mkdir(parents=True)
    (repo / "containers" / "leanflow-sandbox.Containerfile").write_text(
        "FROM scratch\n", encoding="utf-8"
    )
    settings = _settings(tmp_path)
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.settings_from_config", lambda **_kwargs: settings
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.resolve_container_engine", lambda _requested: "docker"
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.ensure_container_engine_usable", lambda _engine: None
    )
    monkeypatch.setattr("leanflow_cli.runtime.sandbox_runtime.repository_root", lambda: repo)
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.subprocess.call",
        lambda argv: captured.setdefault("argv", argv) and 0,
    )

    assert build_sandbox_image(local_lean_explore=True) == 0

    argv = captured["argv"]
    assert "--build-arg" in argv
    assert "LEANFLOW_SANDBOX_EXTRAS=mcp,lean-explore" in argv
