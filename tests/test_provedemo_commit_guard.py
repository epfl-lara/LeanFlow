from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".githooks"


def _run(
    cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def _init_temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run(["git", "init"], repo).returncode == 0
    assert _run(["git", "config", "user.name", "Test User"], repo).returncode == 0
    assert _run(["git", "config", "user.email", "test@example.com"], repo).returncode == 0
    assert _run(["git", "config", "core.hooksPath", str(HOOKS_DIR)], repo).returncode == 0
    return repo


def test_pre_commit_blocks_provedemo_fixture_changes(tmp_path):
    repo = _init_temp_repo(tmp_path)
    target = (
        repo / "testdata" / "workflow_projects" / "ProveDemo" / "ProveDemo" / "RealTheorems.lean"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert _run(["git", "add", str(target.relative_to(repo))], repo).returncode == 0
    result = _run(["git", "commit", "-m", "test"], repo)

    assert result.returncode != 0
    assert "ProveDemo" in result.stderr
    assert "ALLOW_PROVEDEMO_COMMIT=1" in result.stderr


def test_pre_commit_blocks_doc_formalization_demo_fixture_changes(tmp_path):
    repo = _init_temp_repo(tmp_path)
    target = (
        repo
        / "testdata"
        / "workflow_projects"
        / "DocFormalizationDemo"
        / "docs"
        / "PythagoreanPolynomialParametrization"
        / "pyth.tex"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\\title{changed}\n", encoding="utf-8")

    assert _run(["git", "add", str(target.relative_to(repo))], repo).returncode == 0
    result = _run(["git", "commit", "-m", "test"], repo)

    assert result.returncode != 0
    assert "DocFormalizationDemo" in result.stderr
    assert "ALLOW_DOCFORMALIZATIONDEMO_COMMIT=1" in result.stderr


def test_pre_commit_allows_explicit_override(tmp_path):
    repo = _init_temp_repo(tmp_path)
    target = (
        repo / "testdata" / "workflow_projects" / "ProveDemo" / "ProveDemo" / "RealTheorems.lean"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert _run(["git", "add", str(target.relative_to(repo))], repo).returncode == 0
    env = dict(os.environ)
    env["ALLOW_PROVEDEMO_COMMIT"] = "1"
    result = _run(["git", "commit", "-m", "test"], repo, env=env)

    assert result.returncode == 0


def test_pre_commit_allows_doc_formalization_demo_explicit_override(tmp_path):
    repo = _init_temp_repo(tmp_path)
    target = (
        repo
        / "testdata"
        / "workflow_projects"
        / "DocFormalizationDemo"
        / "docs"
        / "QuantizingPythagoreanTriples"
        / "Pythagore2.tex"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\\title{changed}\n", encoding="utf-8")

    assert _run(["git", "add", str(target.relative_to(repo))], repo).returncode == 0
    env = dict(os.environ)
    env["ALLOW_DOCFORMALIZATIONDEMO_COMMIT"] = "1"
    result = _run(["git", "commit", "-m", "test"], repo, env=env)

    assert result.returncode == 0
