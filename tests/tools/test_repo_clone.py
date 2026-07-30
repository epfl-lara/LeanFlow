"""Phase 5 §5.6 tests: repo_clone sandbox, cap, idempotency, registration.

No network anywhere: clones use local `git init` fixture repositories
(git accepts filesystem paths for any scheme check we bypass via direct
tool-function calls in most tests; the scheme gate itself is unit-tested).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.implementations import repo_clone as rc


def _make_fixture_repo(root: Path, *, blob_bytes: int = 0) -> Path:
    src = root / "fixture-src"
    src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "Lemma.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
    if blob_bytes:
        (src / "big.bin").write_bytes(b"\0" * blob_bytes)
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(src),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return src


def _clone_local(src: Path, monkeypatch, **kwargs) -> dict:
    """Drive the tool against a local fixture by relaxing the scheme gate."""
    monkeypatch.setattr(rc, "_ALLOWED_SCHEMES", {"https", "git", ""})
    return json.loads(rc.repo_clone_tool(str(src), **kwargs))


def test_repo_clone_is_unavailable_in_clean_room(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")

    out = json.loads(rc.repo_clone_tool("https://example.com/repo.git"))

    assert "disabled for this clean-room run" in out["error"]
    assert rc.check_repo_clone_available() is False


def test_repo_clone_lands_in_workspace(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)

    out = _clone_local(src, monkeypatch, name="mathlib-fork")

    assert out["success"] is True
    dest = Path(out["path"])
    assert dest.parent == (tmp_path / ".leanflow" / "workspace" / "repos").resolve()
    assert dest.name == "mathlib-fork"
    assert (dest / "Lemma.lean").is_file()
    assert out["bytes"] > 0
    assert len(out["head_commit"]) == 40
    assert out["cached"] is False


def test_repo_clone_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)

    first = _clone_local(src, monkeypatch, name="again")
    second = _clone_local(src, monkeypatch, name="again")

    assert second["cached"] is True
    assert second["path"] == first["path"]
    assert second["head_commit"] == first["head_commit"]


def test_repo_clone_refuses_preexisting_non_git_destination(monkeypatch, tmp_path):
    """The tool must never rmtree a directory it did not create."""
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)
    precious = tmp_path / ".leanflow" / "workspace" / "repos" / "mine"
    precious.mkdir(parents=True)
    (precious / "data.txt").write_text("keep me", encoding="utf-8")

    out = _clone_local(src, monkeypatch, name="mine")

    assert "error" in out and "not a git clone" in out["error"]
    assert (precious / "data.txt").read_text(encoding="utf-8") == "keep me"


def test_repo_clone_cache_hit_verifies_url_identity(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src_a = _make_fixture_repo(tmp_path / "a")
    src_b = _make_fixture_repo(tmp_path / "b")

    first = _clone_local(src_a, monkeypatch, name="same")
    assert first["success"] is True
    second = _clone_local(src_b, monkeypatch, name="same")

    assert "error" in second and "already holds a clone of" in second["error"]
    # The original clone is untouched.
    assert Path(first["path"]).joinpath("Lemma.lean").is_file()


def test_repo_clone_cache_hit_rejects_originless_repo(monkeypatch, tmp_path):
    """A git dir with no origin must never pass as a cache hit for any url."""
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)
    originless = tmp_path / ".leanflow" / "workspace" / "repos" / "bare"
    originless.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(originless)], check=True)

    out = _clone_local(src, monkeypatch, name="bare")

    assert "error" in out and "<no origin>" in out["error"]


def test_repo_clone_cache_hit_verifies_ref_identity(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)

    assert _clone_local(src, monkeypatch, name="refy")["success"] is True
    out = _clone_local(src, monkeypatch, name="refy", ref="other-branch")

    assert "error" in out and "requested ref" in out["error"]


def test_repo_clone_enforces_size_cap_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path, blob_bytes=200_000)

    out = _clone_local(src, monkeypatch, name="huge", max_bytes=1000)

    assert "error" in out and "cap" in out["error"]
    assert not (tmp_path / ".leanflow" / "workspace" / "repos" / "huge").exists()


def test_repo_clone_env_cap_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEANFLOW_REPO_CLONE_MAX_BYTES", "1000")
    src = _make_fixture_repo(tmp_path, blob_bytes=200_000)

    out = _clone_local(src, monkeypatch, name="huge2")

    assert "error" in out and "cap" in out["error"]


def test_repo_clone_sanitizes_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = _make_fixture_repo(tmp_path)

    out = _clone_local(src, monkeypatch, name="../../etc")

    assert out["success"] is True
    dest = Path(out["path"])
    assert dest.parent == (tmp_path / ".leanflow" / "workspace" / "repos").resolve()
    assert ".." not in dest.name


def test_repo_clone_rejects_bad_inputs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert "error" in json.loads(rc.repo_clone_tool(""))
    assert "error" in json.loads(rc.repo_clone_tool("http://example.com/x.git"))
    assert "error" in json.loads(rc.repo_clone_tool("ssh://host/x.git"))
    out = json.loads(rc.repo_clone_tool("https://example.com/x.git", ref="--upload-pack=evil"))
    assert "error" in out and "ref" in out["error"]
    # Nothing was created for any rejection.
    assert not (tmp_path / ".leanflow").exists()


def test_repo_clone_rejects_symlinked_repos_escape(monkeypatch, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / ".leanflow" / "workspace").mkdir(parents=True)
    outside.mkdir()
    (project / ".leanflow" / "workspace" / "repos").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(project)
    src = _make_fixture_repo(tmp_path)

    out = _clone_local(src, monkeypatch, name="esc")

    assert "error" in out and "escapes the project sandbox" in out["error"]
    assert not any(outside.iterdir())


def test_repo_clone_failed_clone_reports_and_cleans(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    out = _clone_local(tmp_path / "does-not-exist", monkeypatch, name="gone")

    assert "error" in out and "git clone failed" in out["error"]
    assert not (tmp_path / ".leanflow" / "workspace" / "repos" / "gone").exists()


def test_repo_clone_timeout_is_bounded_configurable_and_cleans(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEANFLOW_REPO_CLONE_TIMEOUT_SECONDS", "7")
    observed: dict[str, int] = {}

    def timeout(argv, **kwargs):
        observed["seconds"] = kwargs["timeout"]
        destination = Path(argv[-1])
        destination.mkdir(parents=True)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(rc.subprocess, "run", timeout)

    out = json.loads(rc.repo_clone_tool("https://example.com/slow.git", name="slow-clone"))

    assert observed == {"seconds": 7}
    assert "timed out after 7s" in out["error"]
    assert not (tmp_path / ".leanflow" / "workspace" / "repos" / "slow-clone").exists()


def test_repo_clone_registered_in_web_toolsets():
    from core.toolsets import resolve_toolset
    from tools.registry import registry

    assert "repo_clone" in resolve_toolset("web")
    assert "repo_clone" in resolve_toolset("leanflow-prove-worker")
    defs = registry.get_definitions({"repo_clone"}, quiet=True)
    assert [d["function"]["name"] for d in defs] == ["repo_clone"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
