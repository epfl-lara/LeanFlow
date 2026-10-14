"""Verify bounded dependency-cache lifetime without losing proof or resume artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.runtime.storage.process_views import ProcessViews
from leanflow_cli.workflows.prover.native_snapshot import (
    create_snapshot,
    prepare_snapshot,
    release_snapshot_dependencies,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a small pinned library and a filtered private proof workspace."""
    # These fixtures own no native child processes. A concurrently incomplete
    # host ps/lsof inventory correctly defers cleanup, but is not a pin test.
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.native_snapshot.process_views",
        lambda: ProcessViews((), (), True),
    )
    root = tmp_path / "canonical"
    root.mkdir()
    (root / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "lib", "rev": "pinned"}]})
    )
    (root / "Main.lean").write_text("theorem target : True := by sorry")
    library = root / ".lake/packages/lib"
    (library / ".lake/build").mkdir(parents=True)
    (library / ".lake/build/Lib.olean").write_bytes(b"compiled library")
    (library / "Lib.lean").write_text("theorem helper : True := by trivial")
    (library / ".git").mkdir()
    (library / ".git/config").write_text("private checkout metadata")
    snapshot = create_snapshot(root, tmp_path / "snapshot")
    return root, snapshot


def test_release_retains_proofs_scratch_and_local_build_sources(project) -> None:
    root, snapshot = project
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir()
    (scratch / "candidate.txt").write_text("by trivial")
    build = snapshot / ".lake/build"
    build.mkdir()
    (build / "Scratch.lean").write_text("theorem checked : True := by trivial")
    release_snapshot_dependencies(snapshot)
    assert not (snapshot / ".lake/packages").exists()
    assert (root / ".lake/packages/lib/Lib.lean").exists()
    assert (snapshot / "Main.lean").read_text().endswith("sorry")
    assert (scratch / "candidate.txt").read_text() == "by trivial"
    assert (build / "Scratch.lean").is_file()


def test_resume_restores_pinned_libraries_with_private_checkout_metadata(project) -> None:
    root, snapshot = project
    (snapshot / "Main.lean").write_text("private proof progress")
    release_snapshot_dependencies(snapshot)
    prepare_snapshot(root, snapshot)
    cached = snapshot / ".lake/packages/lib/.lake/build/Lib.olean"
    assert cached.read_bytes() == b"compiled library"
    assert cached.stat().st_ino != (root / ".lake/packages/lib/.lake/build/Lib.olean").stat().st_ino
    assert (snapshot / ".lake/packages/lib/.git/config").read_text() == "private checkout metadata"
    assert (snapshot / "Main.lean").read_text() == "private proof progress"
    assert not (snapshot / ".leanflow-dependencies-released").exists()


def test_changed_pins_never_overwrite_saved_sources(project) -> None:
    root, snapshot = project
    release_snapshot_dependencies(snapshot)
    (root / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "lib", "rev": "changed"}]})
    )
    with pytest.raises(ValueError, match="revisions changed"):
        prepare_snapshot(root, snapshot)
    assert not (snapshot / ".lake/packages").exists()
    assert (snapshot / ".leanflow-dependencies-released").is_file()
    assert (snapshot / "Main.lean").is_file()


def test_interrupted_cleanup_restores_a_complete_cache(project) -> None:
    root, snapshot = project
    release_snapshot_dependencies(snapshot)
    partial = snapshot / ".lake/packages/lib/.lake/build"
    partial.mkdir(parents=True)
    (partial / "Lib.olean").write_bytes(b"partial")
    prepare_snapshot(root, snapshot)
    assert (partial / "Lib.olean").read_bytes() == b"compiled library"
    assert not list((snapshot / ".lake").glob(".dependencies-*"))


def test_cleanup_never_follows_a_link_to_canonical_dependencies(project) -> None:
    root, snapshot = project
    release_snapshot_dependencies(snapshot)
    import shutil

    shutil.rmtree(snapshot / ".lake")
    (snapshot / ".lake").symlink_to(root / ".lake")
    release_snapshot_dependencies(snapshot)
    assert (root / ".lake/packages/lib/.lake/build/Lib.olean").read_bytes() == b"compiled library"
    assert not (snapshot / ".lake").exists()


def test_snapshot_rejects_linked_parent_before_any_external_write(tmp_path) -> None:
    root = tmp_path / "canonical"
    root.mkdir()
    (root / "Main.lean").write_text("theorem t : True := by sorry")
    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="linked destination"):
        create_snapshot(root, linked / "private")
    with pytest.raises(ValueError, match="linked destination"):
        prepare_snapshot(root, linked / "private")
    assert list(external.iterdir()) == []
    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="linked destination"):
        create_snapshot(root, broken)


def test_restored_snapshot_refreshes_workspace_storage_size(project) -> None:
    from leanflow_cli.runtime.storage import storage_status

    root, snapshot = project
    release_snapshot_dependencies(snapshot)
    prepare_snapshot(root, snapshot)
    status = storage_status()
    assert status["workspace_bytes"] > 0
    assert status["workspace_pinned_bytes"] == status["workspace_bytes"]


def test_session_release_defers_dependency_pruning_for_live_background_child(
    project, monkeypatch
) -> None:
    from leanflow_cli.runtime.storage.process_views import ProcessViews

    _, snapshot = project
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.native_snapshot.process_views",
        lambda: ProcessViews((str(snapshot),), (), True),
    )
    release_snapshot_dependencies(snapshot)
    assert (snapshot / ".lake/packages/lib/.lake/build/Lib.olean").is_file()
    assert not (snapshot / ".leanflow-dependencies-released").exists()
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.native_snapshot.process_views",
        lambda: ProcessViews((), (), True),
    )
    release_snapshot_dependencies(snapshot)
    assert not (snapshot / ".lake/packages").exists()


@pytest.mark.parametrize("checkpoint", ["provenance_flushed", "snapshot_published"])
def test_real_process_exit_keeps_scoped_snapshot_resume_reachable(tmp_path, checkpoint) -> None:
    """Resume after either side of exclusive publication without losing old evidence."""
    from leanflow_cli.workflows.prover import native_snapshot

    root = tmp_path / "canonical"
    root.mkdir()
    original = b"theorem target : True := by sorry\n"
    (root / "Main.lean").write_bytes(original)
    cached = root / "artifacts/lib/lean/Hidden.olean"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"canonical cache must be excluded")
    destination = tmp_path / "snapshot"
    code = """
import os, sys
from pathlib import Path
from leanflow_cli.workflows.prover import native_snapshot
from leanflow_cli.formalization import publication_io
root, destination, checkpoint = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
publish = native_snapshot._publish_exclusion
move = publication_io.rename_exclusive
def published(*args):
    publish(*args)
    if checkpoint == 'provenance_flushed':
        os._exit(91)
def moved(source, target):
    move(source, target)
    if target == destination and checkpoint == 'snapshot_published':
        os._exit(91)
native_snapshot._publish_exclusion = published
publication_io.rename_exclusive = moved
native_snapshot.prepare_snapshot(root, destination, excluded_build_dir='artifacts')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), str(destination), checkpoint],
        env={**os.environ, "LEANFLOW_HOME": str(tmp_path / "state")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 91, result.stderr
    record = native_snapshot._exclusion_record(destination)
    prior = record.read_bytes()
    published = destination.exists()
    inode = destination.stat().st_ino if published else None
    resumed = prepare_snapshot(root, destination, excluded_build_dir="artifacts")
    assert resumed == destination and not (resumed / "artifacts").exists()
    assert (resumed / "Main.lean").read_bytes() == original
    assert (root / "Main.lean").read_bytes() == original and cached.read_bytes()
    if published:
        assert destination.stat().st_ino == inode and record.read_bytes() == prior
    else:
        retained = list(record.parent.glob(record.name + ".unpublished-*"))
        assert len(retained) == 1 and retained[0].read_bytes() == prior


def test_partial_unpublished_provenance_is_preserved_before_a_fresh_copy(tmp_path) -> None:
    """Keep incomplete creation evidence while granting only a newly excluded view."""
    from leanflow_cli.workflows.prover import native_snapshot

    root = tmp_path / "canonical"
    root.mkdir()
    (root / "Main.lean").write_text("theorem target : True := by sorry\n")
    destination = tmp_path / "snapshot"
    record = native_snapshot._exclusion_record(destination)
    record.write_bytes(b"partial original provenance")
    prepare_snapshot(root, destination, excluded_build_dir="artifacts")
    retained = list(record.parent.glob(record.name + ".unpublished-*"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"partial original provenance"
    native_snapshot._assert_exclusion(destination, "artifacts")


def test_competing_snapshot_directory_is_never_replaced(tmp_path, monkeypatch) -> None:
    """Preserve an independent view created after scoped provenance was registered."""
    from leanflow_cli.workflows.prover import native_snapshot

    root = tmp_path / "canonical"
    root.mkdir()
    (root / "Main.lean").write_text("theorem target : True := by sorry\n")
    destination = tmp_path / "snapshot"
    publish = native_snapshot._publish_exclusion

    def competing(*args):
        publish(*args)
        destination.mkdir()
        (destination / "independent.lean").write_bytes(b"independent version")

    monkeypatch.setattr(native_snapshot, "_publish_exclusion", competing)
    with pytest.raises(FileExistsError):
        create_snapshot(root, destination, excluded_build_dir="artifacts")
    assert (destination / "independent.lean").read_bytes() == b"independent version"
    with pytest.raises(ValueError, match="exact build-exclusion provenance"):
        prepare_snapshot(root, destination, excluded_build_dir="artifacts")
