"""Verify shared campaign inputs, guarded replay and durable dependency cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from core.home import leanflow_home
from leanflow_cli.runtime.runtime_install import source_identity
from leanflow_cli.runtime.storage import StoragePolicy, cleanup_storage, register_entry
from leanflow_cli.runtime.storage import process_views as process_inventory
from leanflow_cli.runtime.storage.process_views import ProcessViews
from leanflow_cli.workflows.prover import native_snapshot
from scripts.lean_imo_campaign import artifacts, runner, storage
from scripts.lean_imo_campaign.matrix import cells


def _git(root: Path, *arguments: str) -> str:
    """Create deterministic local fixture commits without user Git configuration."""
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def campaign_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    """Create one exact managed runtime, ready dependency and 18 statement-only inputs."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    # These fake processes never start. Real-user process inventory can be
    # transiently incomplete on busy hosts; its fail-closed behavior is tested
    # separately with real children in test_storage.py.
    monkeypatch.setattr(process_inventory, "process_views", lambda: ProcessViews((), (), True))
    monkeypatch.setattr(native_snapshot, "process_views", lambda: ProcessViews((), (), True))
    home.mkdir()
    (home / "config.yaml").write_text("schema_version: 2\n")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/__init__.py").write_text('"""Frozen scripts."""\n')
    (repo / "core").mkdir()
    (repo / "core/__init__.py").write_text('"""Frozen core."""\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.0.1"\n')
    (repo / ".gitignore").write_text(".lake/\n")
    for name in ("lean-bounded-prover", "lean-prover-orchestrator"):
        skill = repo / "leanflow_skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("frozen skill contract")
    benchmark = repo / "testdata/workflow_projects/LeanIMOBench"
    benchmark.mkdir(parents=True)
    (benchmark / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    (benchmark / "lakefile.toml").write_text(
        'name = "LeanIMOBench"\n[[lean_lib]]\nname = "LeanIMOBench"\n'
    )
    package = benchmark / ".lake/packages/Lib"
    package.mkdir(parents=True)
    (package / "Lib.lean").write_text("theorem helper : True := by trivial\n")
    (package / ".gitignore").write_text(".lake/\n")
    _git(package, "init", "-q")
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "Ready library")
    revision = _git(package, "rev-parse", "HEAD")
    (benchmark / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "Lib",
                        "type": "git",
                        "url": "https://example.invalid/Lib",
                        "rev": revision,
                    }
                ]
            }
        )
    )
    built = package / ".lake/build/lib/lean"
    built.mkdir(parents=True)
    (built / "Lib.olean").write_bytes(b"fixture artifact")
    statements = benchmark / "LeanIMOBench"
    statements.mkdir()
    problems = []
    for index in range(18):
        name = f"P{index}.lean"
        (statements / name).write_text("theorem target : True := by sorry\n")
        problems.append({"id": f"p{index}", "file": f"LeanIMOBench/{name}", "leap_solved": False})
    (benchmark / "manifest.json").write_text(json.dumps({"problems": problems}))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Source fixture")
    runtime = home / "runtimes" / "fixture-runtime"
    python = runtime / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    identity = source_identity(repo)
    register_entry(
        runtime,
        kind="runtime",
        identity={"source": identity, "mode": "editable"},
        metadata={"ready": True, "install_mode": "editable", "source_root": str(repo)},
    )
    (home / "install-root").write_text(f"repo_root={repo}\nvenv_dir={runtime / 'venv'}\n")
    directory = tmp_path / "campaign"
    directory.mkdir()
    return repo, benchmark, directory, runtime


def test_freeze_keeps_source_provenance_without_baseline_or_python_dependency_copies(
    campaign_inputs,
) -> None:
    repo, benchmark, directory, runtime = campaign_inputs
    identity = artifacts.freeze(repo, benchmark, directory)
    assert identity["python"] == str(runtime / "venv/bin/python")
    assert identity["python_runtime_identity"] == source_identity(repo)
    assert len(identity["statement_files"]) == 18
    assert (directory / "runtime/core/__init__.py").read_bytes() == (
        repo / "core/__init__.py"
    ).read_bytes()
    assert not (directory / "python-env").exists()
    assert not (directory / "baseline/.lake/packages").exists()
    key = identity["dependency_cache_key"]
    assert (
        leanflow_home()
        / "cache/dependencies/v1"
        / key
        / "packages/Lib/.lake/build/lib/lean/Lib.olean"
    ).is_file()
    assert str(runtime / "venv/bin/python") in (directory / "run-campaign").read_text()
    assert (benchmark / "LeanIMOBench/P0.lean").read_text().endswith("sorry\n")


@pytest.mark.parametrize("local_search", [False, True])
def test_campaign_accepts_captured_companion_and_search_identity(
    campaign_inputs, tmp_path, local_search
):
    """Validate real coordinated-install receipts rather than recomputing an incomplete identity."""
    from scripts.lean_imo_campaign.storage import installed_runtime

    repo, _benchmark, _directory, runtime = campaign_inputs
    companion = tmp_path / "LeanProbe"
    (companion / "src/lean_probe").mkdir(parents=True)
    (companion / "pyproject.toml").write_text("[project]\nname='lean-probe'\n")
    (companion / "src/lean_probe/__init__.py").write_text("API_REVISION = 1\n")
    mode = "editable:local-search" if local_search else "editable"
    expected = source_identity(repo, mode=mode, lean_probe_source=companion)
    register_entry(
        runtime,
        kind="runtime",
        identity={"source": expected},
        metadata={
            "ready": True,
            "install_mode": "editable",
            "source_root": str(repo),
            "lean_probe_source": str(companion),
            "local_search": local_search,
        },
    )
    with installed_runtime(repo) as captured:
        assert captured["python_runtime_identity"] == expected
    (companion / "src/lean_probe/__init__.py").write_text("API_REVISION = 2\n")
    with pytest.raises(ValueError, match="Cannot freeze this campaign's Python runtime"):
        with installed_runtime(repo):
            pytest.fail("Changed companion source must not be accepted")


def test_freeze_requires_exact_current_source_and_does_not_clone_an_old_environment(
    campaign_inputs,
) -> None:
    repo, benchmark, directory, _ = campaign_inputs
    (repo / "core/__init__.py").write_text("changed source")
    with pytest.raises(ValueError, match="Reinstall LeanFlow from this exact source"):
        artifacts.freeze(repo, benchmark, directory)
    assert not (directory / "runtime").exists()
    assert not (directory / "python-env").exists()


def test_freeze_rejects_legacy_unmanaged_install_with_reinstall_remedy(campaign_inputs) -> None:
    repo, benchmark, directory, _ = campaign_inputs
    (leanflow_home() / "install-root").write_text(
        f"repo_root={repo}\nvenv_dir={leanflow_home() / 'venv'}\n"
    )
    with pytest.raises(ValueError, match="immutable managed runtime.*Reinstall"):
        artifacts.freeze(repo, benchmark, directory)


def test_active_campaign_leases_runtime_and_dependencies_but_idle_history_does_not_pin(
    campaign_inputs,
) -> None:
    repo, benchmark, directory, runtime = campaign_inputs
    identity = artifacts.freeze(repo, benchmark, directory)
    # A subsequent installation points elsewhere; only active campaign leases
    # should retain the earlier runtime and dependency cache.
    home = leanflow_home()
    (home / "install-root").write_text(f"repo_root={repo}\nvenv_dir={home / 'current/venv'}\n")
    dependency = home / "cache/dependencies/v1" / identity["dependency_cache_key"]
    policy = StoragePolicy(cache_target_gib=0, min_free_gib=0)
    with storage.lease_frozen_runtime(directory):
        report = cleanup_storage(policy=policy)
        assert str(runtime) not in report["removed"]
        assert str(dependency) not in report["removed"]
    report = cleanup_storage(policy=policy)
    assert str(runtime) in report["removed"]
    assert str(dependency) in report["removed"]
    assert (directory / "provenance.json").is_file()
    assert (directory / "baseline/LeanIMOBench/P0.lean").is_file()
    with pytest.raises(ValueError, match="reinstall.*recorded"):
        storage.frozen_python(directory)


def test_missing_cached_dependencies_fail_before_any_dispatch(campaign_inputs, monkeypatch) -> None:
    repo, benchmark, directory, _ = campaign_inputs
    identity = artifacts.freeze(repo, benchmark, directory)
    dependency = leanflow_home() / "cache/dependencies/v1" / identity["dependency_cache_key"]
    from leanflow_cli.runtime.storage.entries import remove_owned_directory

    remove_owned_directory(dependency)
    row = cells([{"id": "p0", "file": "LeanIMOBench/P0.lean"}])[0]
    (directory / "campaign.json").write_text(json.dumps({"version": 2, "cells": [row]}))
    monkeypatch.setattr(runner, "require_native_campaign", lambda _: None)
    monkeypatch.setattr(runner, "launch", lambda *_: pytest.fail("Model dispatch must not happen"))
    runner.run(directory)
    result = json.loads((directory / "campaign.json").read_text())
    assert result["status"] == "paused"
    assert result["cells"][0]["status"] == "environment_error"
    assert "dependencies were evicted" in result["cells"][0]["error"]
    assert "project setup" in result["cells"][0]["error"]


def test_legacy_private_python_env_replay_remains_supported(tmp_path: Path) -> None:
    python = tmp_path / "python-env/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("legacy interpreter")
    (tmp_path / "provenance.json").write_text(json.dumps({"python": str(python)}))
    assert storage.frozen_python(tmp_path) == python
    with storage.lease_frozen_runtime(tmp_path):
        assert python.read_text() == "legacy interpreter"


def test_finished_cell_releases_libraries_after_persist_preserving_proofs_and_checkpoints(
    campaign_inputs, monkeypatch
) -> None:
    repo, benchmark, directory, _ = campaign_inputs
    artifacts.freeze(repo, benchmark, directory)
    monkeypatch.setattr(artifacts, "initialize_lake", lambda _: {})
    cell = cells([{"id": "p0", "file": "LeanIMOBench/P0.lean"}])[0]
    project = artifacts.prepare(directory, cell)
    proof = project / "LeanIMOBench/P0.lean"
    proof.write_text("proof progress")
    checkpoint = project / ".leanflow/workflow-state/checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("durable checkpoint")
    scratch = project / ".lake/build/Scratch.lean"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("scratch proof")
    cell.update(project=str(project), lane=1)
    (directory / "campaign.json").write_text(json.dumps({"version": 2, "cells": [cell]}))
    events = []

    class Process:
        def poll(self):
            return 0

    def launch(_directory, _campaign, row):
        row.update(status="running", project=str(project), started_epoch=time.time())
        return Process()

    def report(_directory, campaign):
        events.append(("persist", campaign["cells"][0]["status"]))

    original_release = runner.release_cell_dependencies

    def release(_directory, row):
        events.append(("cleanup", row["status"]))
        original_release(_directory, row)

    monkeypatch.setattr(runner, "launch", launch)
    monkeypatch.setattr(runner, "report", report)
    monkeypatch.setattr(
        runner, "refresh", lambda row: row.update(controller_status="completed", verified=True)
    )
    monkeypatch.setattr(runner, "release_cell_dependencies", release)
    monkeypatch.setattr(runner, "require_native_campaign", lambda _: None)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner.run(directory)
    assert events.index(("persist", "completed")) < events.index(("cleanup", "completed"))
    assert not (project / ".lake/packages").exists()
    assert (project / ".leanflow-dependencies-released").is_file()
    assert proof.read_text() == "proof progress"
    assert checkpoint.read_text() == "durable checkpoint"
    assert scratch.read_text() == "scratch proof"
    storage.restore_cell_dependencies(directory, project)
    assert (project / ".lake/packages/Lib/.lake/build/lib/lean/Lib.olean").is_file()
    assert checkpoint.read_text() == "durable checkpoint"


def test_changed_dependency_inputs_remain_intact_after_cell_finishes(
    campaign_inputs, monkeypatch
) -> None:
    repo, benchmark, directory, _ = campaign_inputs
    artifacts.freeze(repo, benchmark, directory)
    monkeypatch.setattr(artifacts, "initialize_lake", lambda _: {})
    cell = cells([{"id": "p0", "file": "LeanIMOBench/P0.lean"}])[0]
    project = artifacts.prepare(directory, cell)
    (project / "lakefile.toml").write_text(
        'name = "Changed"\nmoreLeanArgs = ["-DmaxRecDepth=9000"]\n'
    )
    storage.release_cell_dependencies(directory, {"project": str(project)})
    assert (project / ".lake/packages/Lib/Lib.lean").is_file()
    assert not (project / ".leanflow-dependencies-released").exists()


def test_orphan_child_defers_cell_cleanup_without_dropping_registered_owner(
    campaign_inputs, monkeypatch
) -> None:
    from types import SimpleNamespace

    from leanflow_cli.workflows.prover import native_snapshot

    repo, benchmark, directory, _ = campaign_inputs
    artifacts.freeze(repo, benchmark, directory)
    monkeypatch.setattr(artifacts, "initialize_lake", lambda _: {})
    cell = cells([{"id": "p0", "file": "LeanIMOBench/P0.lean"}])[0]
    project = artifacts.prepare(directory, cell)
    marker = project / ".leanflow-cache-owner"
    owner = marker.read_text()
    monkeypatch.setattr(
        native_snapshot, "process_views", lambda: SimpleNamespace(uses=lambda _: True)
    )
    storage.release_cell_dependencies(directory, {"project": str(project)})
    assert (project / ".lake/packages/Lib/Lib.lean").is_file()
    assert marker.read_text() == owner
