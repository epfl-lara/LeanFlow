"""Verify portable benchmark helpers using isolated campaigns and process fixtures."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "testdata/workflow_projects/LeanIMOBench/linux"


def _load(name: str) -> ModuleType:
    """Load a helper without executing its command entry point."""
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPTS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _campaign(root: Path, cells: list[dict]) -> None:
    """Create a disposable campaign with proof and rebuildable artifacts."""
    root.mkdir(exist_ok=True)
    (root / "cell-configs").mkdir()
    for cell in cells:
        directory = root / "cells" / cell["id"]
        (directory / ".lake").mkdir(parents=True)
        (directory / ".lake/library").write_text("rebuildable")
        (directory / "Proof.lean").write_text("proof evidence")
        (root / "cell-configs" / f"{cell['id']}.json").write_text("{}")
    (root / "campaign.json").write_text(
        json.dumps({"cells": cells, "status": "paused", "pause_reason": "failure"})
    )


@pytest.mark.parametrize("name", ["reclaim-finished-cells", "reset-blocked-cells"])
def test_cleanup_help_requires_no_campaign_access(name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / name), "--help"], text=True, capture_output=True
    )
    assert result.returncode == 0
    assert "--campaign-dir" in result.stdout
    assert "--apply" in result.stdout


def test_docker_wrapper_mounts_explicit_paths_and_forwards_lanes(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign with spaces"
    repo = tmp_path / "checkout"
    home = tmp_path / "home"
    for directory in (campaign, repo, *(home / name for name in (".elan", ".leanflow", ".codex"))):
        directory.mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "docker.json"
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['DOCKER_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "run-campaign-docker"),
            "--campaign-dir",
            str(campaign),
            "--repo-dir",
            str(repo),
            "--home-dir",
            str(home),
            "--image",
            "example:test",
            "--",
            "sh",
            "-c",
            "command -v rg",
        ],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "LEANFLOW_CAMPAIGN_LANES": "3",
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    arguments = json.loads(log.read_text())
    assert f"{campaign}:{campaign}" in arguments
    assert f"{repo}:{repo}:ro" in arguments
    assert f"HOME={home}" in arguments
    assert "LEANFLOW_CAMPAIGN_LANES=3" in arguments
    assert arguments[-4:] == ["example:test", "sh", "-c", "command -v rg"]


def test_reclaim_preserves_evidence_and_protected_cells(tmp_path: Path, monkeypatch) -> None:
    helper = _load("reclaim-finished-cells")
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    cells = [
        {"id": "eligible", "status": "completed", "verified": True, "finished_at": old},
        {"id": "held", "status": "budget_exhausted", "finished_at": old},
        {"id": "unverified", "status": "completed", "finished_at": old},
        {"id": "resume", "status": "budget_exhausted", "resume_run_id": "run", "finished_at": old},
        {
            "id": "recent",
            "status": "budget_exhausted",
            "finished_at": datetime.now(UTC).isoformat(),
        },
        {"id": "provider", "status": "provider_error", "finished_at": old},
        {"id": "running", "status": "running", "finished_at": old},
        {"id": "unknown", "status": "unknown", "finished_at": old},
    ]
    _campaign(tmp_path, cells)
    monkeypatch.setattr(helper, "in_use", lambda path: path.parent.name == "held")
    assert helper.sweep(tmp_path) == 1
    assert (tmp_path / "cells/eligible/.lake").exists()
    assert helper.sweep(tmp_path, apply=True) == 1
    assert not (tmp_path / "cells/eligible/.lake").exists()
    assert (tmp_path / "cells/eligible/Proof.lean").read_text() == "proof evidence"
    for cell in cells[1:]:
        assert (tmp_path / "cells" / cell["id"] / ".lake").exists()


def test_process_guard_handles_open_files_and_fails_closed(tmp_path: Path) -> None:
    helper = _load("reclaim-finished-cells")
    target = tmp_path / "cell/.lake"
    target.mkdir(parents=True)
    proc = tmp_path / "proc"
    process = proc / "123"
    (process / "fd").mkdir(parents=True)
    (process / "cwd").symlink_to(tmp_path)
    assert helper.in_use(target, proc) is False
    (process / "fd/0").symlink_to(target / "library")
    assert helper.in_use(target, proc) is True
    assert helper.in_use(target, tmp_path / "missing-proc") is True
    (process / "fd/0").unlink()
    (process / "cwd").unlink()
    assert helper.in_use(target, proc) is True


@pytest.mark.parametrize(
    "name,function",
    [("reclaim-finished-cells", "owned_path"), ("reset-blocked-cells", "owned_paths")],
)
@pytest.mark.parametrize("cell_id", ["../outside", "/outside", ".."])
def test_cleanup_rejects_escaping_ids(
    tmp_path: Path, name: str, function: str, cell_id: str
) -> None:
    helper = _load(name)
    with pytest.raises(ValueError):
        getattr(helper, function)(tmp_path, cell_id)


@pytest.mark.parametrize(
    "name,function",
    [("reclaim-finished-cells", "owned_path"), ("reset-blocked-cells", "owned_paths")],
)
def test_cleanup_rejects_symlink_escapes(tmp_path: Path, name: str, function: str) -> None:
    helper = _load(name)
    campaign = tmp_path / "campaign"
    outside = tmp_path / "outside"
    (campaign / "cells").mkdir(parents=True)
    (campaign / "cell-configs").mkdir()
    outside.mkdir()
    (campaign / "cells/escape").symlink_to(outside)
    with pytest.raises(ValueError):
        getattr(helper, function)(campaign, "escape")


def test_reset_archives_attempts_and_preserves_spent_cells(tmp_path: Path) -> None:
    helper = _load("reset-blocked-cells")
    cells = [
        {
            "id": "blocked",
            "status": "environment_error",
            "metrics": {"api_calls": 0},
            "run_id": "old",
            "error": "infra",
        },
        {"id": "spent", "status": "error", "metrics": {"api_calls": 1}},
        {"id": "active", "status": "running", "metrics": {"api_calls": 0}},
    ]
    _campaign(tmp_path, cells)
    original = (tmp_path / "campaign.json").read_text()
    assert helper.reset(tmp_path) == 1
    assert (tmp_path / "campaign.json").read_text() == original
    assert helper.reset(tmp_path, apply=True) == 1
    campaign = json.loads((tmp_path / "campaign.json").read_text())
    assert campaign["status"] == "prepared"
    assert "pause_reason" not in campaign
    blocked = campaign["cells"][0]
    assert blocked["status"] == "pending"
    assert blocked["executions"][0]["run_id"] == "old"
    assert not (tmp_path / "cells/blocked").exists()
    assert not (tmp_path / "cell-configs/blocked.json").exists()
    assert (tmp_path / "cells/spent/Proof.lean").exists()
    assert campaign["cells"][1:] == cells[1:]
