"""Verify exact dependency compatibility, immutable publication and private reuse."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.runtime import dependency_cache as cache
from leanflow_cli.runtime.dependency_cache import checkout, identity


def _git(root: Path, *arguments: str) -> str:
    """Run local-only Git fixture commands with deterministic author identity."""
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Cache Test",
        "GIT_AUTHOR_EMAIL": "cache@example.invalid",
        "GIT_COMMITTER_NAME": "Cache Test",
        "GIT_COMMITTER_EMAIL": "cache@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create one ready pinned dependency in a canonical project and private cache home."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    root = tmp_path / "first"
    package = root / ".lake/packages/Lib"
    package.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    (root / "lakefile.toml").write_text(
        'name = "First"\n[[lean_lib]]\nname = "First"\nroots = ["First"]\n'
    )
    (package / "Lib.lean").write_text("theorem helper : True := by trivial\n")
    (package / ".gitignore").write_text(".lake/\n")
    _git(package, "init", "-q")
    _git(package, "add", "Lib.lean", ".gitignore")
    _git(package, "commit", "-qm", "Pinned library")
    revision = _git(package, "rev-parse", "HEAD")
    manifest = {
        "version": "1.2.0",
        "name": "First",
        "packages": [
            {"name": "Lib", "type": "git", "url": "https://example.invalid/Lib", "rev": revision}
        ],
    }
    (root / "lake-manifest.json").write_text(json.dumps(manifest))
    build = package / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Lib.olean").write_bytes(b"ready compiled artifact")
    return root


def _second_project(project: Path) -> Path:
    """Create an uninstalled project with the same pinned inputs and different proof targets."""
    second = project.parent / "second"
    second.mkdir()
    shutil.copyfile(project / "lean-toolchain", second / "lean-toolchain")
    manifest = json.loads((project / "lake-manifest.json").read_text())
    manifest["name"] = "Second"
    (second / "lake-manifest.json").write_text(json.dumps(manifest))
    (second / "lakefile.toml").write_text(
        'name = "Second"\n[[lean_lib]]\nname = "OtherProofs"\nroots = ["OtherProofs"]\n'
    )
    return second


def test_missing_project_reuses_ready_cache_and_preserves_private_git(project: Path) -> None:
    entry = cache.ensure_dependency_entry(project)
    assert entry is not None
    second = _second_project(project)
    assert cache.dependency_cache_key(second) == entry.name
    target = second / ".lake/packages"
    assert cache.materialize_dependencies(second, target, seed=False)
    assert _git(target / "Lib", "rev-parse", "HEAD") == _git(
        project / ".lake/packages/Lib", "rev-parse", "HEAD"
    )
    for relative in ("Lib/Lib.lean", "Lib/.lake/build/lib/lean/Lib.olean"):
        original = project / ".lake/packages" / relative
        cached = entry / "packages" / relative
        private = target / relative
        assert original.stat().st_ino != cached.stat().st_ino != private.stat().st_ino
        assert cached.stat().st_mode & 0o222 == 0
        assert private.stat().st_mode & stat.S_IWUSR
        private.write_bytes(b"private progress")
        assert original.read_bytes() == cached.read_bytes() != private.read_bytes()


def test_snapshot_materialization_omits_git_metadata(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot/packages"
    assert cache.materialize_dependencies(project, target, include_git=False)
    assert not (target / "Lib/.git").exists()
    assert (target / "Lib/.lake/build/lib/lean/Lib.olean").is_file()


@pytest.mark.parametrize("change", ["toolchain", "build", "platform", "architecture", "revision"])
def test_compatibility_key_isolates_all_artifact_inputs(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    first = cache.dependency_cache_key(project)
    if change == "toolchain":
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.34.0")
    elif change == "build":
        (project / "lakefile.toml").write_text(
            'name = "First"\nmoreLeanArgs = ["-DmaxRecDepth=6000"]\n'
        )
    elif change == "platform":
        monkeypatch.setattr(identity.platform, "system", lambda: "different-system")
    elif change == "architecture":
        monkeypatch.setattr(identity.platform, "machine", lambda: "different-architecture")
    else:
        manifest = json.loads((project / "lake-manifest.json").read_text())
        manifest["packages"][0]["rev"] = "a" * 40
        (project / "lake-manifest.json").write_text(json.dumps(manifest))
    assert cache.dependency_cache_key(project) != first


def test_changed_revision_and_modified_sources_cannot_seed(project: Path) -> None:
    package = project / ".lake/packages/Lib"
    (package / "Lib.lean").write_text("unverified source edit")
    with pytest.raises(ValueError, match="modified tracked"):
        cache.ensure_dependency_entry(project)
    _git(package, "checkout", "--", "Lib.lean")
    manifest = json.loads((project / "lake-manifest.json").read_text())
    manifest["packages"][0]["rev"] = "a" * 40
    (project / "lake-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="locked revision"):
        cache.ensure_dependency_entry(project)


def test_source_only_project_does_not_poison_later_publication(project: Path) -> None:
    artifact = project / ".lake/packages/Lib/.lake/build/lib/lean/Lib.olean"
    artifact.unlink()
    assert cache.ensure_dependency_entry(project) is None
    artifact.write_bytes(b"verified artifact")
    assert cache.ensure_dependency_entry(project) is not None


def test_lookup_never_publishes_or_downloads_source_only_inputs(project: Path) -> None:
    second = _second_project(project)
    assert not cache.materialize_dependencies(second, second / ".lake/packages", seed=False)
    assert not (second / ".lake").exists()


def test_concurrent_publication_has_one_complete_entry(project: Path) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        entries = list(executor.map(lambda _: cache.ensure_dependency_entry(project), range(4)))
    assert len(set(entries)) == 1
    entry = entries[0]
    assert entry is not None
    assert (
        entry / "packages/Lib/.lake/build/lib/lean/Lib.olean"
    ).read_bytes() == b"ready compiled artifact"
    assert not list(entry.parent.glob(".staging-*"))


def test_failed_copy_never_publishes_partial_cache(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args, **kwargs):
        raise OSError("interrupted clone")

    monkeypatch.setattr(checkout, "copy_file", interrupted)
    with pytest.raises(OSError, match="interrupted clone"):
        cache.ensure_dependency_entry(project)
    assert not list(cache._cache_root().iterdir())


@pytest.mark.parametrize("kind", ["package", "file", "git", "destination", "cache"])
def test_links_cannot_escape_private_cache_boundaries(
    project: Path, tmp_path: Path, kind: str
) -> None:
    package = project / ".lake/packages/Lib"
    if kind == "package":
        moved = tmp_path / "moved-package"
        package.rename(moved)
        package.symlink_to(moved)
    elif kind == "file":
        (package / "escape").symlink_to(tmp_path)
    elif kind == "git":
        moved = tmp_path / "git-data"
        (package / ".git").rename(moved)
        (package / ".git").symlink_to(moved)
    elif kind == "cache":
        root = cache._cache_root()
        root.parent.mkdir(parents=True)
        root.symlink_to(tmp_path)
    else:
        (tmp_path / "linked").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="linked directories"):
            cache.materialize_dependencies(project, tmp_path / "linked/packages")
        return
    with pytest.raises(ValueError):
        cache.ensure_dependency_entry(project)


def test_agent_workspace_cannot_publish_or_lookup(project: Path) -> None:
    (project / ".leanflow-job").mkdir()
    with pytest.raises(ValueError, match="agent workspaces"):
        cache.ensure_dependency_entry(project)


def test_invalid_manifest_names_revisions_and_duplicate_names(project: Path) -> None:
    original = json.loads((project / "lake-manifest.json").read_text())
    for field, value in (("name", "../escape"), ("rev", "main")):
        manifest = json.loads(json.dumps(original))
        manifest["packages"][0][field] = value
        (project / "lake-manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            cache.dependency_cache_key(project)
    original["packages"].append(original["packages"][0])
    (project / "lake-manifest.json").write_text(json.dumps(original))
    with pytest.raises(ValueError, match="unique"):
        cache.dependency_cache_key(project)


def test_compiler_binary_change_invalidates_cache_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = Path(os.environ["ELAN_HOME"]) / "toolchains/leanprover--lean4---v4.33.1/bin/lean"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"compiler one")
    first = cache.dependency_cache_key(project)
    executable.write_bytes(b"compiler two")
    assert cache.dependency_cache_key(project) != first


def test_restore_replaces_only_clean_source_checkouts(project: Path) -> None:
    assert cache.ensure_dependency_entry(project) is not None
    second = _second_project(project)
    copied = second / ".lake/packages"
    shutil.copytree(project / ".lake/packages", copied)
    shutil.rmtree(copied / "Lib/.lake/build")
    assert cache.validate_dependency_source(second)
    assert cache.restore_project_dependencies(second)
    assert (copied / "Lib/.lake/build/lib/lean/Lib.olean").is_file()
    assert not cache.validate_dependency_source(second)
    assert not cache.restore_project_dependencies(second)


@pytest.mark.parametrize("private", ["tracked", "untracked", "build", "extra"])
def test_restore_preserves_private_changes_and_scratch_outputs(project: Path, private: str) -> None:
    assert cache.ensure_dependency_entry(project) is not None
    second = _second_project(project)
    packages = second / ".lake/packages"
    shutil.copytree(project / ".lake/packages", packages)
    shutil.rmtree(packages / "Lib/.lake/build")
    if private == "tracked":
        protected = packages / "Lib/Lib.lean"
    elif private == "untracked":
        protected = packages / "Lib/Proof.lean"
    elif private == "build":
        protected = packages / "Lib/.lake/build/Scratch.lean"
    else:
        protected = packages / "Extra/Proof.lean"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("private proof to preserve")
    assert not cache.restore_project_dependencies(second)
    assert protected.read_text() == "private proof to preserve"


def test_restore_rolls_back_failed_publication(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cache.ensure_dependency_entry(project) is not None
    artifact = project / ".lake/packages/Lib/.lake/build/lib/lean/Lib.olean"
    artifact.unlink()
    package_directory = project / ".lake/packages"
    original = Path.rename

    def failed_publish(self, target):
        if (
            self.name == "packages"
            and ".restore-" in str(self.parent)
            and Path(target) == package_directory
        ):
            raise OSError("publication failed")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", failed_publish)
    with pytest.raises(OSError, match="publication failed"):
        cache.restore_project_dependencies(project)
    assert (package_directory / "Lib/Lib.lean").is_file()
    assert not artifact.exists()


def _repin(project: Path) -> None:
    """Commit fixture inputs and update the complete revision in its manifest."""
    package = project / ".lake/packages/Lib"
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "Additional fixture inputs")
    manifest = json.loads((project / "lake-manifest.json").read_text())
    manifest["packages"][0]["rev"] = _git(package, "rev-parse", "HEAD")
    (project / "lake-manifest.json").write_text(json.dumps(manifest))


def test_internal_relative_links_preserve_checkout_identity_and_snapshot_filtering(
    project: Path, tmp_path: Path
) -> None:
    package = project / ".lake/packages/Lib"
    (package / "Readme.lean").symlink_to("Lib.lean")
    _repin(project)
    entry = cache.ensure_dependency_entry(project)
    assert entry is not None
    assert (entry / "packages/Lib/Readme.lean").is_symlink()
    target = tmp_path / "private/packages"
    assert cache.materialize_dependencies(project, target, include_git=True)
    assert (target / "Lib/Readme.lean").is_symlink()
    assert _git(target / "Lib", "status", "--porcelain", "--untracked-files=no") == ""
    (target / "Lib/Readme.lean").write_text("private edit")
    assert (entry / "packages/Lib/Lib.lean").read_text() == (package / "Lib.lean").read_text()
    snapshot = tmp_path / "snapshot/packages"
    assert cache.materialize_dependencies(project, snapshot, include_git=False)
    assert not (snapshot / "Lib/Readme.lean").exists()


def test_clean_room_snapshot_uses_logical_canonical_paths_and_excludes_private_config(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = project / ".lake/packages/Lib"
    (package / "Problem1.lean").write_text("theorem active : True := by trivial")
    (package / "Problem2.lean").write_text("theorem sibling : True := by trivial")
    (package / ".mcp.json").write_text("private MCP configuration")
    (package / ".ruff_cache").mkdir()
    (package / ".ruff_cache/private").write_text("private cache bytes")
    _repin(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", str(package / "Problem1.lean"))
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "true")
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "true")
    snapshot = tmp_path / "snapshot/packages"
    assert cache.materialize_dependencies(project, snapshot, include_git=False)
    assert (snapshot / "Lib/Problem1.lean").is_file()
    assert not (snapshot / "Lib/Problem2.lean").exists()
    assert not (snapshot / "Lib/.mcp.json").exists()
    assert not (snapshot / "Lib/.ruff_cache").exists()
    assert (snapshot / "Lib/.lake/build/lib/lean/Lib.olean").is_file()


def test_deep_destination_symlink_is_rejected(project: Path, tmp_path: Path) -> None:
    (tmp_path / "linked").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="linked directories"):
        cache.materialize_dependencies(project, tmp_path / "linked/deeper/packages")


def test_unlocked_package_cannot_enter_global_dependency_cache(project: Path) -> None:
    extra = project / ".lake/packages/PrivateProofs"
    extra.mkdir()
    (extra / "Proof.lean").write_text("private proof")
    with pytest.raises(ValueError, match="locked closure"):
        cache.ensure_dependency_entry(project)
    assert not cache._cache_root().exists()
    assert (extra / "Proof.lean").read_text() == "private proof"


@pytest.mark.parametrize(
    "record", [[], {"schema_version": 99}, {"schema_version": 1, "kind": "other"}]
)
def test_invalid_managed_cache_metadata_fails_closed(project: Path, record) -> None:
    entry = cache.ensure_dependency_entry(project)
    assert entry is not None
    (entry / ".leanflow-storage.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="identity is inconsistent"):
        cache.materialize_dependencies(project, project.parent / "private/packages", seed=False)


def test_manifest_configuration_paths_cannot_escape_checkout(project: Path) -> None:
    manifest = json.loads((project / "lake-manifest.json").read_text())
    manifest["packages"][0]["subDir"] = "../../private"
    (project / "lake-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inside its checkout"):
        cache.dependency_cache_key(project)


def test_existing_cache_does_not_hide_locally_modified_dependency_source(project: Path) -> None:
    entry = cache.ensure_dependency_entry(project)
    assert entry is not None
    source = project / ".lake/packages/Lib/Lib.lean"
    source.write_text("theorem locally_changed : True := by trivial\n")
    with pytest.raises(ValueError, match="modified tracked"):
        cache.ensure_dependency_entry(project)
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot

    snapshot = create_snapshot(project, project.parent / "snapshot")
    assert (snapshot / ".lake/packages/Lib/Lib.lean").read_bytes() == source.read_bytes()
    assert (entry / "packages/Lib/Lib.lean").read_bytes() != source.read_bytes()


@pytest.mark.parametrize("edited", [False, True])
def test_native_snapshot_preserves_private_lake_checkout_metadata(
    project: Path, edited: bool
) -> None:
    """Lake must recognize both cached and edited private dependency checkouts."""
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot

    package = project / ".lake/packages/Lib"
    _git(package, "remote", "add", "origin", "https://example.invalid/Lib")
    if edited:
        (package / "Lib.lean").write_text("theorem local_edit : True := by trivial\n")
    (package / ".codex").mkdir()
    (package / ".codex/config.toml").write_text("private native settings")
    with (package / ".git/info/exclude").open("a") as stream:
        stream.write("\n.codex/\n")
    snapshot = create_snapshot(project, project.parent / "snapshot")
    private = snapshot / ".lake/packages/Lib"
    assert _git(private, "rev-parse", "HEAD") == _git(package, "rev-parse", "HEAD")
    assert _git(private, "config", "--get", "remote.origin.url") == "https://example.invalid/Lib"
    assert (private / "Lib.lean").read_bytes() == (package / "Lib.lean").read_bytes()
    assert not (private / ".codex").exists()
    assert not (snapshot / ".git").exists()
    metadata = private / ".git/config"
    assert metadata.stat().st_ino != (package / ".git/config").stat().st_ino
    metadata.write_text("private change")
    assert "private change" not in (package / ".git/config").read_text()
