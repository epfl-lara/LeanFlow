"""Require actual ordinary Git checkouts across installation and manifest-only reuse paths."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import libraries
from leanflow_cli.workflows.prover.installed_libraries import libraries_already_installed
from leanflow_cli.workflows.prover.library_checkout import require_checkout
from tests.leanflow.library_checkout_fixture import PIN, REQUEST, create_checkout


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a bounded project while only replacing public reachability and Lake acquisition."""
    (tmp_path / "lakefile.toml").write_text('name="demo"\n')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.30.0\n")
    (tmp_path / "lake-manifest.json").write_text('{"version":"1.2.0","packages":[]}')
    monkeypatch.delenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", raising=False)
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    )
    return tmp_path


def _resolved(root: Path) -> dict[str, object]:
    """Acquire real local Git bytes at the immutable requested revision."""
    create_checkout(root / ".lake/packages/physlib")
    manifest = root / "lake-manifest.json"
    document = json.loads(manifest.read_text())
    if not any(item["name"] == REQUEST["name"] for item in document["packages"]):
        document["packages"].append(
            {"name": REQUEST["name"], "type": "git", "url": REQUEST["git"], "rev": PIN}
        )
    manifest.write_text(json.dumps(document))
    return {"success": True, "returncode": 0}


def _tamper(root: Path, change: str) -> None:
    """Leave manifest identity intact while changing the actual local checkout authority."""
    checkout = root / ".lake/packages/physlib"
    if change == "missing":
        shutil.rmtree(checkout)
    elif change == "head":
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "commit",
                "--allow-empty",
                "-qm",
                "changed actual head",
            ],
            check=True,
            capture_output=True,
        )
    elif change in {"git_symlink", "git_pointer"}:
        metadata = checkout.with_name("borrowed-git-metadata")
        (checkout / ".git").rename(metadata)
        if change == "git_symlink":
            (checkout / ".git").symlink_to(metadata, target_is_directory=True)
        else:
            (checkout / ".git").write_text(f"gitdir: {metadata}\n")
    elif change == "alternates":
        path = checkout / ".git/objects/info/alternates"
        path.write_text(str(root / "unowned-objects") + "\n")
    elif change == "ref_symlink":
        head = checkout / ".git/HEAD"
        retained = root / "borrowed-HEAD"
        head.rename(retained)
        head.symlink_to(retained)
    else:
        head = checkout / ".git/HEAD"
        head.unlink()
        os.mkfifo(head)


@pytest.mark.parametrize(
    "change", ["missing", "head", "git_symlink", "git_pointer", "alternates", "ref_symlink", "fifo"]
)
def test_matching_manifest_cannot_bless_missing_or_tampered_acquisition(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    before = {
        name: (project / name).read_bytes()
        for name in ("lakefile.toml", "lean-toolchain", "lake-manifest.json")
    }

    def update(root: Path, *_):
        result = _resolved(root)
        _tamper(root, change)
        return result

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is False and result["status"] == "rolled_back", result
    assert result["dependency_cache_may_have_changed"] is False
    assert {name: (project / name).read_bytes() for name in before} == before
    assert not (project / ".lake/packages/physlib").exists()


@pytest.mark.parametrize(
    "change", ["missing", "head", "git_symlink", "git_pointer", "alternates", "ref_symlink", "fifo"]
)
def test_already_installed_fastpath_requires_actual_git_evidence(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(libraries, "_run_update", lambda root, *_: _resolved(root))
    assert libraries.install_libraries(project, [REQUEST])["accepted"]
    _tamper(project, change)
    before = {
        name: (project / name).read_bytes()
        for name in ("lakefile.toml", "lean-toolchain", "lake-manifest.json")
    }
    monkeypatch.setattr(libraries, "_run_update", lambda *_: {"success": True, "returncode": 0})
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is False, result
    assert not libraries_already_installed(project, [REQUEST])
    assert {name: (project / name).read_bytes() for name in before} == before


def test_missing_locked_checkout_can_be_reacquired_without_configuration_rewrite(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libraries, "_run_update", lambda root, *_: _resolved(root))
    assert libraries.install_libraries(project, [REQUEST])["accepted"]
    shutil.rmtree(project / ".lake/packages/physlib")
    configuration = {
        name: (project / name).read_bytes()
        for name in ("lakefile.toml", "lean-toolchain", "lake-manifest.json")
    }
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is True and result["status"] == "installed", result
    require_checkout(project / ".lake/packages/physlib", PIN)
    assert {name: (project / name).read_bytes() for name in configuration} == configuration
    assert libraries_already_installed(project, [REQUEST])


def test_previously_installed_revision_is_checked_without_new_effects(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(libraries, "_run_update", lambda root, *_: _resolved(root))
    assert libraries.install_libraries(project, [REQUEST])["accepted"]
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("fresh Lake acquisition"))
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is True and result["status"] == "already_installed"
    assert libraries_already_installed(project, [REQUEST])


@pytest.mark.parametrize("alias", ["packages", "package", "git", "source_file"])
def test_internal_alias_cannot_rewrite_canonical_git_metadata_before_isolation(
    project: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    _resolved(project)
    package = project / ".lake/packages/physlib"
    configuration = package / ".git/config"
    original = configuration.read_bytes()
    if alias == "packages":
        retained = project / "CachePackages"
        package.parent.rename(retained)
        (project / ".lake/packages").symlink_to(retained, target_is_directory=True)
        configuration = retained / "physlib/.git/config"
    elif alias == "package":
        retained = project / "CachePackage"
        package.rename(retained)
        package.symlink_to(retained, target_is_directory=True)
        configuration = retained / ".git/config"
    elif alias == "git":
        retained = project / "CacheMetadata"
        (package / ".git").rename(retained)
        (package / ".git").symlink_to(retained, target_is_directory=True)
        configuration = retained / "config"
    else:
        target = project / "canonical-source-marker.txt"
        target.write_text("original canonical data")
        (package / "canonical-link").symlink_to(target)
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("effectful Lake reached"))
    additional = {**REQUEST, "name": "newlib", "git": "https://github.com/example/newlib"}
    result = libraries.install_libraries(project, [additional])
    assert result["accepted"] is False, result
    assert configuration.read_bytes() == original
    if alias == "source_file":
        assert target.read_text() == "original canonical data"


def test_portable_package_file_link_remains_inside_private_copy(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolved(project)
    package = project / ".lake/packages/physlib"
    (package / "source-link").symlink_to("Physlib.lean")
    monkeypatch.setattr(libraries, "_run_update", lambda root, *_: _resolved(root))
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is True, result
    assert (package / "source-link").is_symlink()
    assert (package / "source-link").read_bytes() == (package / "Physlib.lean").read_bytes()


def _observe_replaced_fifo(path: str, kind: str, channel: Connection) -> None:
    """Pause immediately before open so a competing process can substitute a named pipe."""
    from leanflow_cli.workflows.prover.library_configuration import _configuration_bytes
    from leanflow_cli.workflows.prover.source_mutation import observe

    original_open = os.open

    def opening(value, flags, *args, **kwargs):
        if Path(value) == Path(path):
            channel.send("before_open")
            assert channel.recv() == "continue"
        return original_open(value, flags, *args, **kwargs)

    os.open = opening
    try:
        (_configuration_bytes if kind == "configuration" else observe)(Path(path))
        channel.send("unexpectedly_read")
    except (OSError, ValueError, RuntimeError) as error:
        channel.send({"rejected": str(error)})
    finally:
        channel.close()


@pytest.mark.parametrize("kind", ["configuration", "source"])
def test_competing_fifo_substitution_cannot_block_bounded_source_read(
    tmp_path: Path, kind: str
) -> None:
    """Reject an actual two-process regular-file-to-FIFO race without waiting for a writer."""
    path = tmp_path / "input.toml"
    path.write_text('name="original"\n')
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_observe_replaced_fifo, args=(str(path), kind, remote))
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "before_open"
        path.unlink()
        os.mkfifo(path)
        local.send("continue")
        assert local.poll(2), "Source observation blocked on a substituted FIFO"
        result = local.recv()
        assert isinstance(result, dict) and "rejected" in result
        process.join(2)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        local.close()
