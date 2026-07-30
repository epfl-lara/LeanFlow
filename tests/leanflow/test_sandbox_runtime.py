from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.runtime import sandbox_runtime
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
    (project / ".leanflow" / "workspace" / "repos" / "prior-solution").mkdir(parents=True)
    (project / ".leanflow" / "workspace" / "repos" / "prior-solution" / "P6.lean").write_text(
        "theorem leaked : True := by trivial\n",
        encoding="utf-8",
    )
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
    assert not (destination / ".leanflow" / "workspace").exists()


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
    assert "--init" in argv
    assert (
        "/opt/leanflow/.venv/lib/python3.12/site-packages/lean_interact/cache:"
        "rw,nosuid,nodev,noexec,size=256m,mode=1777"
    ) in argv
    assert "--env-file" in argv
    assert "/opt/leanflow/.venv/bin/leanflow" in joined
    assert "set -e;" in joined
    assert "lake build repl;" not in joined
    assert "mcp bootstrap lean || {" in joined
    assert "&& touch" not in joined
    assert "TMPDIR=/leanflow-cache/tmp" in argv
    assert (settings.cache_dir / "tmp").is_dir()


def test_container_run_command_mounts_only_third_party_lake_packages(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "lean-project"
    packages = project_root / ".lake" / "packages"
    packages.mkdir(parents=True)
    (packages / "mathlib" / ".lake" / "build").mkdir(parents=True)
    (packages / "repl" / ".lake" / "build" / "bin").mkdir(parents=True)
    (packages / "repl" / ".lake" / "build" / "bin" / "repl").write_text(
        "host executable",
        encoding="utf-8",
    )
    (packages / "repl" / "REPL").mkdir()
    (packages / "repl" / ".git").mkdir()
    (packages / "Cli").mkdir()
    (project_root / ".lake" / "build").mkdir()
    (project_root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
    initialize_leanflow_project(project_root, name="demo")
    settings = _settings(tmp_path)
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["workflow", "prove", "Main.lean"],
        run_id="run-packages",
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

    assert f"src={packages.resolve()},dst=/workspace/.lake/packages,readonly" in joined
    assert str((project_root / ".lake" / "build").resolve()) not in joined
    assert "dst=/workspace/.lake/packages/repl" in joined
    assert "dst=/workspace/.lake/packages/Cli" in joined
    assert "dst=/workspace/.lake/packages/mathlib/.lake" not in joined
    assert str((packages / "mathlib" / ".lake").resolve()) not in joined
    assert str((packages / "repl" / ".lake").resolve()) not in joined
    assert (settings.cache_dir / "lake-package-overlays").is_dir()
    overlays = list((settings.cache_dir / "lake-package-overlays").glob("*"))
    assert len(overlays) == 1
    assert not (overlays[0] / "repl" / ".lake").exists()
    assert (overlays[0] / "repl" / "REPL").is_dir()
    assert (overlays[0] / "repl" / ".git").is_dir()
    assert "lake build repl;" in joined


def test_package_overlay_tolerates_concurrent_nonempty_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker Desktop may report a winning directory race as ``ENOTEMPTY``."""
    package = tmp_path / "packages" / "repl"
    package.mkdir(parents=True)
    (package / "REPL").mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    original_rename = Path.rename

    def race_rename(source: Path, target: Path) -> Path:
        Path(target).mkdir()
        raise OSError(errno.ENOTEMPTY, "directory not empty", str(target))

    monkeypatch.setattr(Path, "rename", race_rename)
    try:
        result = sandbox_runtime._prepare_sandbox_package_overlay(package, cache_root)
    finally:
        monkeypatch.setattr(Path, "rename", original_rename)

    assert result == cache_root / "repl"
    assert result.is_dir()
    assert not list(cache_root.glob(".repl.*.tmp"))


def test_container_run_command_propagates_clean_room_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    initialize_leanflow_project(project_root, name="demo")
    settings = _settings(tmp_path)
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["workflow", "prove", "Main.lean"],
        run_id="run-clean-room",
        settings=settings,
    )
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )

    argv = container_run_command(
        engine="docker",
        image=settings.image,
        sandbox_run=run,
        settings=settings,
        tty=False,
    )

    assert "LEANFLOW_DISABLE_REPOSITORY_RESEARCH=1" in argv
    assert "GIT_CONFIG_KEY_0=protocol.allow" in argv
    assert "GIT_CONFIG_VALUE_0=never" in argv
    assert "LEANFLOW_DISABLE_SOLUTION_RESEARCH=1" in argv
    assert "LEANFLOW_CLEAN_ROOM_TASK_LABELS=IMO 2026 Problem 6|IMO2026 P6" in argv


def test_container_run_command_mounts_only_codex_auth_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    initialize_leanflow_project(project_root, name="demo")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens": {}}\n', encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    (codex_home / "history.jsonl").write_text("must not mount\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    settings = _settings(tmp_path)
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["workflow", "prove", "Main.lean", "--provider", "codex"],
        run_id="run-codex",
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

    assert (
        f"src={(codex_home / 'auth.json').resolve()},dst=/opt/leanflow/auth.json,readonly" in joined
    )
    assert (
        f"src={(codex_home / 'config.toml').resolve()},dst=/opt/leanflow/config.toml,readonly"
        in joined
    )
    assert "history.jsonl" not in joined
    assert "CODEX_HOME=/opt/leanflow" in argv


def test_container_run_command_does_not_mount_codex_auth_for_other_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "lean-project"
    project_root.mkdir()
    (project_root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    initialize_leanflow_project(project_root, name="demo")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens": {}}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    settings = _settings(tmp_path)
    run = prepare_sandbox_run(
        active_cwd=project_root,
        command_args=["workflow", "prove", "Main.lean", "--provider", "anthropic"],
        run_id="run-non-codex",
        settings=settings,
    )

    argv = container_run_command(
        engine="docker",
        image=settings.image,
        sandbox_run=run,
        settings=settings,
        tty=False,
    )

    assert not any(token.endswith("dst=/opt/leanflow/auth.json,readonly") for token in argv)


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


def test_sandbox_status_can_skip_engine_probe_and_bound_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    for index in range(5):
        run_dir = settings.runs_dir / f"run-{index}"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(
            json.dumps({"run_id": f"run-{index}", "status": "failed"}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.settings_from_config", lambda **_kwargs: settings
    )
    monkeypatch.setattr(
        "leanflow_cli.runtime.sandbox_runtime.resolve_container_engine",
        lambda _requested: pytest.fail("bounded status probed the container engine"),
    )

    payload = sandbox_status(probe_engine=False, recent_run_limit=2)

    assert payload["engine_probe"] == "skipped"
    assert payload["engine_ready"] is None
    assert payload["image_ready"] is None
    assert len(payload["recent_runs"]) == 2


def test_sandbox_containerfile_installs_workflow_cli_dependencies() -> None:
    repo = Path(__file__).resolve().parents[2]
    containerfile = (repo / "containers" / "leanflow-sandbox.Containerfile").read_text(
        encoding="utf-8"
    )
    dockerignore = (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "ripgrep" in containerfile
    assert "poppler-utils" in containerfile
    assert "ARG LEANFLOW_SANDBOX_BASE=" in containerfile
    assert "python3-venv" in containerfile
    assert "requires Python 3.12+" in containerfile
    assert "pip install --no-deps -e" in containerfile
    assert containerfile.index("COPY pyproject.toml README.md") < containerfile.index(
        "COPY . /opt/leanflow"
    )
    assert "artifacts" in dockerignore
    assert "evals" in dockerignore
    assert "paper" in dockerignore
    assert "testdata" in dockerignore
    assert "tests" in dockerignore


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


def test_build_sandbox_image_can_use_cached_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "containers").mkdir(parents=True)
    (repo / "containers" / "leanflow-sandbox.Containerfile").write_text(
        "ARG LEANFLOW_SANDBOX_BASE\nFROM ${LEANFLOW_SANDBOX_BASE}\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    captured: dict[str, list[str]] = {}
    monkeypatch.setenv("LEANFLOW_SANDBOX_BASE_IMAGE", "leanflow/debian-lean:cached")
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

    assert build_sandbox_image() == 0

    assert "LEANFLOW_SANDBOX_BASE=leanflow/debian-lean:cached" in captured["argv"]
