"""Verify bounded dependency-cache lifetime without losing proof or resume artifacts."""

from __future__ import annotations

import json
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
