"""Exercise exact companion download, immutable reuse and interrupted recovery."""

import subprocess
from pathlib import Path

import pytest

from leanflow_cli.runtime import install_companion


@pytest.fixture
def published_source(tmp_path, monkeypatch):
    """Publish two local revisions to prove that the installer respects its pin."""
    root = tmp_path / "remote"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='lean-probe'\nversion='0.4.1.dev0'\n")
    (root / "src/lean_probe").mkdir(parents=True)
    module = root / "src/lean_probe/__init__.py"
    module.write_text("API_REVISION = 1\n")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    git("init", "--quiet")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "first")
    commit = git("rev-parse", "HEAD")
    module.write_text("API_REVISION = 2\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "second")
    monkeypatch.setattr(install_companion, "LEAN_PROBE_URL", str(root))
    monkeypatch.setattr(install_companion, "LEAN_PROBE_COMMIT", commit)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state with spaces"))
    return commit


def test_pinned_fetch_and_reuse_preserve_exact_bytes(published_source):
    source = install_companion.fetch_pinned_lean_probe()
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        == published_source
    )
    assert "API_REVISION = 1" in (source / "src/lean_probe/__init__.py").read_text()
    assert install_companion.fetch_pinned_lean_probe() == source


def test_changed_cached_source_is_not_trusted(published_source):
    source = install_companion.fetch_pinned_lean_probe()
    (source / "src/lean_probe/injected.py").write_text("behavior = 'changed'\n")
    with pytest.raises(RuntimeError, match="changed"):
        install_companion.fetch_pinned_lean_probe()


def test_timeout_retains_private_staging_for_unconfirmed_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path))

    def git(*args, cwd: Path):
        raise subprocess.TimeoutExpired("git", 180)

    monkeypatch.setattr(install_companion, "_git", git)
    with pytest.raises(RuntimeError, match="retained temporary source"):
        install_companion.fetch_pinned_lean_probe()
    root = tmp_path / "vendor/lean-probe-source"
    assert len(list(root.glob("fetch-*"))) == 1
    assert not (root / install_companion.LEAN_PROBE_COMMIT).exists()
