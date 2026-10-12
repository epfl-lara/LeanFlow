"""Invalidate local proof and warm-worker reuse when imported inputs change."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an independent local compiler installation and project environment."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.setenv("PATH", "")
    (root / "lean-toolchain").write_text("local-test-toolchain")
    return root


def test_generation_tracks_alias_targets_removals_and_toolchain_contents(
    project: Path, tmp_path: Path
) -> None:
    artifact = tmp_path / "actual.olean"
    artifact.write_bytes(b"before")
    (project / "Alias.olean").symlink_to(artifact)
    first = lean_environment_generation(project)
    info = artifact.stat()
    artifact.write_bytes(b"after!")
    os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns))
    second = lean_environment_generation(project)
    assert second != first
    (project / "Alias.olean").unlink()
    assert lean_environment_generation(project) not in {first, second}
    toolchain = tmp_path / "elan/toolchains/local-test-toolchain/lib/lean/Init.olean"
    toolchain.parent.mkdir(parents=True)
    before = lean_environment_generation(project)
    toolchain.write_bytes(b"new compiler environment")
    assert lean_environment_generation(project) != before


def test_generation_follows_external_lake_path_dependencies(project: Path, tmp_path: Path) -> None:
    (project / "lake-manifest.json").write_text(
        '{"packages": [{"type":"path", "dir":"../library"}]}'
    )
    dependency = tmp_path / "library/.lake/build/lib/lean/Library.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"first")
    before = lean_environment_generation(project)
    dependency.write_bytes(b"later")
    assert lean_environment_generation(project) != before


def test_explicit_import_root_inside_skipped_directory_still_invalidates(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = project / ".venv/lean/Library.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"first")
    monkeypatch.setenv("LEAN_PATH", str(dependency.parent))
    before = lean_environment_generation(project)
    dependency.write_bytes(b"later")
    assert lean_environment_generation(project) != before


def test_generation_ignores_private_receipts_and_excluded_managed_source(project: Path) -> None:
    managed = project / "Main.lean"
    managed.write_text("theorem goal : True := by sorry")
    private = project / ".leanflow/receipt.json"
    private.parent.mkdir()
    before = lean_environment_generation(project, excluded_sources=(managed,))
    managed.write_text("theorem goal : True := by trivial")
    private.write_text('{"accepted": true}')
    assert lean_environment_generation(project, excluded_sources=(managed,)) == before
    assert lean_environment_generation(project) != before


def test_generation_never_reads_large_compiled_library_bytes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = project / "Big.olean"
    artifact.write_bytes(b"library" * (128 * 1024))
    original = Path.read_bytes

    def read(path: Path) -> bytes:
        assert path.suffix != ".olean", "generation scans must not reread compiled libraries"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    before = lean_environment_generation(project)
    assert lean_environment_generation(project) == before


def test_small_same_size_library_changes_invalidate_even_with_coarse_stat(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detect fast library writes whose size and filesystem timestamps coincide."""
    artifact = project / "Small.olean"
    artifact.write_bytes(b"before")
    previous = artifact.lstat()
    original = Path.lstat

    def coalesced(path: Path, *args, **kwargs):
        return previous if path == artifact else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", coalesced)
    before = lean_environment_generation(project)
    artifact.write_bytes(b"after!")
    assert lean_environment_generation(project) != before


@pytest.mark.parametrize("changed", ["artifact", "source", "config"])
def test_warm_checker_restarts_on_changed_import_environment(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    target = workspace / "Proof.lean"
    target.write_text("import Library\ntheorem p : True := by trivial")
    dependency = project / "Library.olean"
    dependency.write_text("old imported environment")
    (project / "Library.lean").write_text("theorem library : True := by trivial")
    script = (
        "import json,sys,os,pathlib\n"
        f"dependency=pathlib.Path({str(dependency)!r})\n"
        "imported=dependency.read_text()\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line);print(json.dumps({'id':r['id'],'result':"
        "{'success':True,'ok':True,'pid':os.getpid(),'imported':imported}}),flush=True)\n"
    )
    monkeypatch.setattr(
        check_process,
        "_sandbox_command",
        lambda *a, **kw: [sys.executable, "-I", "-u", "-c", script],
    )
    try:
        first = check_process.check_scratch(project_root=project, workspace=workspace, file=target)
        warm = check_process.check_scratch(project_root=project, workspace=workspace, file=target)
        assert first["pid"] == warm["pid"]
        if changed == "artifact":
            original = dependency.stat()
            dependency.write_text("new imported environment")
            os.utime(dependency, ns=(original.st_atime_ns, original.st_mtime_ns))
        elif changed == "source":
            (project / "Library.lean").write_text("theorem library : True := by exact True.intro")
        else:
            (project / "lakefile.toml").write_text('name = "Changed"')
        refreshed = check_process.check_scratch(
            project_root=project, workspace=workspace, file=target
        )
        assert refreshed["pid"] != first["pid"]
        assert refreshed["cold_start"]
        if changed == "artifact":
            assert refreshed["imported"] == "new imported environment"
    finally:
        check_process.close_check_workers(workspace)


def test_warm_checker_discards_feedback_if_dependency_changes_during_check(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    target = workspace / "Proof.lean"
    target.write_text("theorem p : True := by trivial")
    dependency = project / "Library.olean"
    dependency.write_bytes(b"old")
    original = check_process._CheckWorker.request

    def request(worker: check_process._CheckWorker, *args: object, **kwargs: object) -> dict:
        dependency.write_bytes(b"new")
        return {"success": True, "ok": True}

    script = "import sys\nfor line in sys.stdin: pass\n"
    monkeypatch.setattr(check_process._CheckWorker, "request", request)
    monkeypatch.setattr(
        check_process,
        "_sandbox_command",
        lambda *a, **kw: [sys.executable, "-I", "-u", "-c", script],
    )
    try:
        result = check_process.check_scratch(project_root=project, workspace=workspace, file=target)
        assert result["ok"] is False
        assert result["error_code"] == "check_environment_changed"
        assert check_process._WORKERS[(project.resolve(), workspace.resolve())].closed
    finally:
        monkeypatch.setattr(check_process._CheckWorker, "request", original)
        check_process.close_check_workers(workspace)
