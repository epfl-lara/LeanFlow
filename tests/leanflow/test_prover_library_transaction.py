"""Exercise private dependency effects and no-clobber library publication across processes."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import libraries, library_transaction, source_mutation
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.planning_controller import install_planned_libraries
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.source import SourceConflictError
from leanflow_cli.workflows.prover.source_transaction import (
    begin_materialization,
    commit_materialization,
    materialized_write,
)
from tests.leanflow.library_checkout_fixture import REQUEST, create_checkout

EDIT = b'name = "demo"\n# independent editor version\n'


def _project(root: Path) -> dict[str, bytes]:
    """Create bounded canonical compiler inputs without a native process."""
    inputs = {
        "lakefile.toml": b'name = "demo"\n',
        "lake-manifest.json": b'{"version":"1.2.0","packages":[]}',
        "lean-toolchain": b"leanprover/lean4:v4.30.0\n",
        "Main.lean": b"theorem goal : True := by sorry\n",
    }
    for name, content in inputs.items():
        (root / name).write_bytes(content)
    return inputs


def _resolved(stage: Path) -> dict[str, Any]:
    """Return a deterministic resolved private checkout and immutable manifest pin."""
    path = stage / ".lake/packages/physlib"
    create_checkout(path)
    manifest = stage / "lake-manifest.json"
    document = json.loads(manifest.read_text())
    document["packages"].append({"type": "git", "url": REQUEST["git"], **REQUEST})
    manifest.write_text(json.dumps(document))
    return {"success": True, "returncode": 0}


def _install_worker(root: str, mode: str, channel: Connection) -> None:
    """Coordinate an actual library owner with a noncooperating parent editor."""
    project = Path(root)
    libraries._public_connection = lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    os.environ.pop("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", None)
    original_install = source_mutation.install
    count = 0

    def update(stage: Path, paths: list[Path], names: list[str], timeout: float | None):
        assert stage != project and stage.is_relative_to(project / ".leanflow")
        result = _resolved(stage)
        if mode in {"resolution_failure", "resolution_success"}:
            channel.send({"phase": "private_resolution"})
            assert channel.recv() == "continue"
        if mode == "resolution_failure":
            (stage / "lakefile.toml").write_text("private hook changed config")
            (stage / "lean-toolchain").write_text("private hook changed toolchain")
            result["success"] = False
        return result

    def install(project: Path, identifier: str, boundary=None) -> None:
        nonlocal count
        count += 1
        if count == 2 and mode == "promotion_failure":
            raise ValueError("deterministic later configuration failure")
        original_install(project, identifier, boundary)
        if count == 1 and mode in {"promotion_failure", "promotion_crash"}:
            channel.send({"phase": "first_configuration_installed"})
            if mode == "promotion_crash":
                os._exit(61)
            assert channel.recv() == "continue"

    libraries._run_update = update
    source_mutation.install = install
    try:
        channel.send(libraries.install_libraries(project, [REQUEST]))
    finally:
        channel.close()


def _spawn(root: Path, mode: str):
    """Require the library owner to reach its exact publication boundary."""
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_install_worker, args=(str(root), mode, remote))
    process.start()
    remote.close()
    assert local.poll(15), "library owner did not reach the deterministic barrier"
    phase = local.recv()
    assert "phase" in phase, phase
    return process, local


def _finish(process, channel: Connection) -> dict[str, Any]:
    """Require closure of the real owner process after releasing its barrier."""
    channel.send("continue")
    assert channel.poll(15), "library owner did not finish"
    result = channel.recv()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("library owner remained alive")
    assert process.exitcode == 0
    channel.close()
    return result


def _edit(target: Path, style: str, descriptor: int | None) -> None:
    """Save through a distinct inode, an in-place update, or an already open descriptor."""
    if style == "atomic":
        replacement = target.with_suffix(".editor")
        replacement.write_bytes(EDIT)
        replacement.replace(target)
    elif style == "inplace":
        target.write_bytes(EDIT)
    else:
        assert descriptor is not None
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, EDIT)
        os.ftruncate(descriptor, len(EDIT))
        os.fsync(descriptor)


@pytest.mark.parametrize("mode", ["resolution_failure", "resolution_success", "promotion_failure"])
@pytest.mark.parametrize("style", ["atomic", "inplace", "descriptor"])
def test_library_effects_and_rollback_preserve_concurrent_editor(
    tmp_path: Path, mode: str, style: str
) -> None:
    before = _project(tmp_path)
    target = tmp_path / "lakefile.toml"
    descriptor = os.open(target, os.O_RDWR) if style == "descriptor" else None
    process, channel = _spawn(tmp_path, mode)
    try:
        _edit(target, style, descriptor)
        result = _finish(process, channel)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert result["accepted"] is False
    expected = "rolled_back" if mode == "resolution_failure" else "source_conflict"
    assert result["status"] == expected, result
    retained = list((tmp_path / source_mutation.BASE).glob("*/*"))
    assert any(path.is_file() and path.read_bytes() == EDIT for path in [target, *retained])
    assert (tmp_path / "lean-toolchain").read_bytes() == before["lean-toolchain"]
    assert (tmp_path / "lake-manifest.json").read_bytes() == before["lake-manifest.json"]
    if mode != "promotion_failure" or style != "descriptor":
        assert target.read_bytes() == EDIT
    if mode == "resolution_failure":
        assert not (tmp_path / ".lake").exists()
        assert result["dependency_cache_may_have_changed"] is False


def test_real_crashed_library_promotion_recovers_before_fresh_discovery(tmp_path: Path) -> None:
    before = _project(tmp_path)
    process, channel = _spawn(tmp_path, "promotion_crash")
    process.join(15)
    assert process.exitcode == 61
    channel.close()
    assert library_transaction.pending_path(tmp_path).exists()
    assert (tmp_path / "lakefile.toml").read_bytes() != before["lakefile.toml"]
    owner = ProverRuntime(root=tmp_path, targets=[tmp_path / "Main.lean"], config=ProverConfig())
    assert not library_transaction.pending_path(tmp_path).exists()
    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content
    assert len(owner.state["retained_source_mutations"]) == 2
    owner._assert_sources()
    owner._assert_sources()
    # Installed package directories are kept rather than removed beneath another compiler.
    assert (tmp_path / ".lake/packages/physlib/Physlib.lean").is_file()


def test_replan_library_install_preserves_prior_materialization_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    before = _project(tmp_path)
    owner = ProverRuntime(root=tmp_path, targets=[tmp_path / "Main.lean"], config=ProverConfig())
    journal = begin_materialization(owner)
    helper_configuration = before["lakefile.toml"] + b'\n[[lean_lib]]\nname="LeanFlowProofs"\n'
    materialized_write(owner, journal, tmp_path / "lakefile.toml", helper_configuration)
    commit_materialization(owner)
    first_id = owner.state["retained_source_mutations"][0]
    candidate = source_mutation.directory(tmp_path, first_id) / "candidate"
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    )
    monkeypatch.delenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", raising=False)
    monkeypatch.setattr(libraries, "_run_update", lambda root, *_: _resolved(root))
    install_planned_libraries(owner, [REQUEST])
    assert candidate.read_bytes() == helper_configuration
    assert len(owner.state["retained_source_mutations"]) == 3
    assert owner.state["libraries"][-1]["accepted"] is True
    owner._assert_sources()


def test_conflicted_library_journal_blocks_fresh_work_with_retained_path(tmp_path: Path) -> None:
    _project(tmp_path)
    process, channel = _spawn(tmp_path, "promotion_failure")
    _edit(tmp_path / "lakefile.toml", "atomic", None)
    result = _finish(process, channel)
    assert result["status"] == "source_conflict"
    with pytest.raises(SourceConflictError, match="retained versions:"):
        ProverRuntime(root=tmp_path, targets=[tmp_path / "Main.lean"], config=ProverConfig())


def test_private_dependency_git_metadata_preserves_locked_remote_without_hooks(tmp_path: Path):
    """Let Lake reuse copied checkouts while excluding the original private Git configuration."""
    _project(tmp_path)
    package = tmp_path / ".lake/packages/physlib"
    package.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(package)], check=True)
    original_config = package / ".git/config"
    original_config.write_text(
        "[core]\nrepositoryformatversion=0\nhooksPath=/private-hook\n"
        '[remote "origin"]\nurl=https://example.invalid/private-credential-marker\n'
    )
    hooks = package / ".git/hooks"
    (hooks / "post-checkout").write_text("private hook marker")
    manifest = {
        "physlib": {"name": "physlib", "type": "git", "url": REQUEST["git"], "rev": REQUEST["rev"]}
    }
    stage = tmp_path / "private-copy"
    before = original_config.read_bytes()
    library_transaction.copy_inputs(tmp_path, stage, manifest, check=lambda: None)
    copied = stage / ".lake/packages/physlib/.git"
    config = (copied / "config").read_text()
    assert "private-credential-marker" not in config and "hooksPath" not in config
    assert not (copied / "hooks").exists()
    remote = subprocess.run(
        ["git", "-C", str(copied.parent), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remote.stdout.strip() == REQUEST["git"]
    assert original_config.read_bytes() == before
