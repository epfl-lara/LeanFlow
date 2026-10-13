"""Verify fresh-only project artifact reuse, exact invalidation, and managed reclamation."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.runtime import project_build_cache as cache_module
from leanflow_cli.runtime.project_build_cache import ProjectBuildCache, identity
from leanflow_cli.runtime.storage import (
    StoragePolicy,
    StoragePressure,
    cleanup_storage,
    lease_entry,
)
from leanflow_cli.runtime.storage.entries import MARKER, read_entry
from leanflow_cli.workflows.prover import check_process, session_build
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.session_build import build_session_imports


@pytest.fixture
def canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create pure, relocatable TOML sources with an isolated opt-in cache home."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_BUILD_CACHE", "1")
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.setattr(
        identity,
        "_compiler_identity",
        lambda project, environment, **kwargs: {
            "toolchain": (project / "lean-toolchain").read_text()
        },
    )
    project = tmp_path / "canonical"
    (project / "Local").mkdir(parents=True)
    (project / "Local/Support.lean").write_text("def cachedValue : Nat := 7\n")
    (project / "lakefile.toml").write_text('name = "Example"\n[[lean_lib]]\nname = "Local"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    (project / "lake-manifest.json").write_text('{"packages": []}')
    return project


def _snapshot(canonical: Path, path: Path) -> tuple[Path, Path, Path]:
    """Create a controller snapshot and its scratch source before native admission."""
    project = create_snapshot(canonical, path)
    workspace = project / ".leanflow-job"
    workspace.mkdir()
    source = workspace / "Scratch.lean"
    source.write_text("import Local.Support\ntheorem target : True := by sorry\n")
    return project, workspace, source


def _artifacts(build: Path) -> None:
    """Write representative Lake outputs with unmodified generated trace metadata."""
    leaf = build / "lib/lean/Local"
    leaf.mkdir(parents=True, exist_ok=True)
    for suffix, content in (
        ("olean", b"private compiled bytes"),
        ("olean.private", b"private sidecar"),
        ("ilean", b"editor information"),
        ("trace", b'{"depHash": "honest Lake trace"}'),
    ):
        (leaf / f"Support.{suffix}").write_bytes(content)


def _build(paths: tuple[Path, Path, Path], *, fresh: bool = True) -> dict[str, Any]:
    project, workspace, source = paths
    return build_session_imports(
        project_root=project,
        workspace=workspace,
        source_file=source,
        deadline=None,
        cancelled=None,
        cache_fresh_snapshot=fresh,
    )


def test_fresh_cache_reuses_all_outputs_and_still_runs_lake(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_artifact = canonical / ".lake/build/lib/lean/Local/Support.olean"
    canonical_artifact.parent.mkdir(parents=True)
    canonical_artifact.write_bytes(b"canonical target artifacts must never be seeded")
    calls: list[dict[str, Any]] = []

    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        build = kwargs["project_root"] / ".lake/build"
        artifact = build / "lib/lean/Local/Support.olean"
        if len(calls) == 1:
            assert not artifact.exists()
            _artifacts(build)
        else:
            assert artifact.read_bytes() == b"private compiled bytes"
        return {"success": True, "returncode": 0}

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    first_paths = _snapshot(canonical, tmp_path / "first")
    first = _build(first_paths)
    second_paths = _snapshot(canonical, tmp_path / "second")
    second = _build(second_paths)
    assert first["artifact_cache"]["published"] and not first["artifact_cache"]["hit"]
    assert second["artifact_cache"]["hit"] and not second["artifact_cache"]["published"]
    assert first["artifact_cache"]["key"] == second["artifact_cache"]["key"]
    assert len(calls) == 2 and calls[0]["argv"] == calls[1]["argv"]
    assert canonical_artifact.read_bytes() == b"canonical target artifacts must never be seeded"
    entry = (
        Path(os.environ["LEANFLOW_HOME"])
        / "cache/project-builds/v1"
        / first["artifact_cache"]["key"]
    )
    for suffix in ("olean", "olean.private", "ilean", "trace"):
        relative = Path(f"lib/lean/Local/Support.{suffix}")
        paths = [
            entry / "build" / relative,
            *(path[0] / ".lake/build" / relative for path in (first_paths, second_paths)),
        ]
        assert len({path.stat().st_ino for path in paths}) == 3
        assert not paths[0].stat().st_mode & 0o222
        assert paths[1].stat().st_mode & 0o200 and paths[2].stat().st_mode & 0o200
    (second_paths[0] / ".lake/build/lib/lean/Local/Support.olean").write_bytes(
        b"agent local mutation"
    )
    assert (entry / "build/lib/lean/Local/Support.olean").read_bytes() == b"private compiled bytes"


@pytest.mark.parametrize(
    "change",
    ["source", "resource", "configuration", "toolchain", "compiler", "environment", "module"],
)
def test_exact_inputs_invalidate_project_cache(
    canonical: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = identity.identity_key(identity.build_identity(canonical, ["Local.Support"], {}))
    modules, environment = ["Local.Support"], {}
    if change == "source":
        (canonical / "Local/Support.lean").write_text("def cachedValue : Nat := 8\n")
    elif change == "resource":
        (canonical / "resource.txt").write_text("compile-time resource")
    elif change == "configuration":
        (canonical / "lakefile.toml").write_text(
            'name = "Example"\nmoreLeanArgs = ["-DmaxRecDepth=6000"]\n'
        )
    elif change == "toolchain":
        (canonical / "lean-toolchain").write_text("leanprover/lean4:v4.34.0")
    elif change == "compiler":
        monkeypatch.setattr(
            identity, "_compiler_identity", lambda *args, **kwargs: {"lean_sha256": "different"}
        )
    elif change == "environment":
        environment = {"ELAN_TOOLCHAIN": "alternate-selection"}
    else:
        modules = ["Local.Other"]
    assert (
        identity.identity_key(identity.build_identity(canonical, modules, environment)) != original
    )


@pytest.mark.parametrize("corruption", ["bytes", "manifest", "link", "missing", "extra"])
def test_corrupt_output_is_a_miss_and_successful_build_repairs_it(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    builds = []

    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        builds.append(kwargs["project_root"])
        _artifacts(kwargs["project_root"] / ".lake/build")
        return {"success": True, "returncode": 0}

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    first = _build(_snapshot(canonical, tmp_path / "first"))
    entry = (
        Path(os.environ["LEANFLOW_HOME"])
        / "cache/project-builds/v1"
        / first["artifact_cache"]["key"]
    )
    artifact = entry / "build/lib/lean/Local/Support.olean"
    if corruption == "manifest":
        file = entry / "manifest.json"
        file.chmod(0o600)
        file.write_text("{}")
    elif corruption == "extra":
        parent = artifact.parent
        parent.chmod(0o700)
        (parent / "injected.olean").write_bytes(b"unmanifested output")
    elif corruption in {"link", "missing"}:
        artifact.parent.chmod(0o700)
        artifact.unlink()
        if corruption == "link":
            artifact.symlink_to(canonical / "Local/Support.lean")
    else:
        artifact.chmod(0o600)
        artifact.write_bytes(b"corrupted output")
    second = _build(_snapshot(canonical, tmp_path / "second"))
    assert not second["artifact_cache"]["hit"]
    assert second["artifact_cache"]["published"]
    assert len(builds) == 2
    assert read_entry(entry)["identity_sha256"] == first["artifact_cache"]["key"]


def test_resume_default_never_reuses_or_publishes_agent_outputs(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _snapshot(canonical, tmp_path / "resumed")
    _artifacts(paths[0] / ".lake/build")
    monkeypatch.setattr(check_process, "isolated_command", lambda **kwargs: {"success": True})
    result = _build(paths, fresh=False)
    assert not result["artifact_cache"]["eligible"]
    assert not (Path(os.environ["LEANFLOW_HOME"]) / "cache/project-builds").exists()
    result = _build(paths, fresh=True)
    assert not result["artifact_cache"]["eligible"]


def test_disabled_and_custom_lean_config_projects_use_existing_build(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_process, "isolated_command", lambda **kwargs: {"success": True})
    monkeypatch.delenv("LEANFLOW_PROJECT_BUILD_CACHE")
    disabled = _build(_snapshot(canonical, tmp_path / "disabled"))
    assert not disabled["artifact_cache"]["eligible"]
    monkeypatch.setenv("LEANFLOW_PROJECT_BUILD_CACHE", "1")
    (canonical / "lakefile.lean").write_text("import Lake\n")
    unsupported = _build(_snapshot(canonical, tmp_path / "unsupported"))
    assert not unsupported["artifact_cache"]["eligible"]


def test_input_mutation_or_failed_build_cannot_publish(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate(**kwargs: Any) -> dict[str, Any]:
        _artifacts(kwargs["project_root"] / ".lake/build")
        (kwargs["project_root"] / "Local/Support.lean").write_text("changed during build")
        return {"success": True}

    monkeypatch.setattr(check_process, "isolated_command", mutate)
    changed = _build(_snapshot(canonical, tmp_path / "changed"))
    assert changed["artifact_cache"]["eligible"] and not changed["artifact_cache"]["published"]
    monkeypatch.setattr(check_process, "isolated_command", lambda **kwargs: {"success": False})
    failed = _build(_snapshot(canonical, tmp_path / "failed"))
    assert not failed["artifact_cache"]["published"]
    assert not (Path(os.environ["LEANFLOW_HOME"]) / "cache/project-builds").exists()


def test_project_entries_and_staging_are_collected_with_active_lease_protection(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        _artifacts(kwargs["project_root"] / ".lake/build")
        return {"success": True}

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    result = _build(_snapshot(canonical, tmp_path / "first"))
    entry = (
        Path(os.environ["LEANFLOW_HOME"])
        / "cache/project-builds/v1"
        / result["artifact_cache"]["key"]
    )
    assert read_entry(entry)["kind"] == "project-build"
    policy = StoragePolicy(0, 0, True)
    with lease_entry(entry):
        cleanup_storage(policy=policy)
        assert entry.exists()
    cleanup_storage(policy=policy)
    assert not entry.exists()


def test_dependency_artifact_bytes_and_clean_provenance_affect_identity(
    canonical: Path,
) -> None:
    package = canonical / ".lake/packages/Lib"
    package.mkdir(parents=True)
    (package / "Lib.lean").write_text("def libraryValue : Nat := 1\n")
    (package / ".gitignore").write_text(".lake/\n")
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Cache Test",
        "GIT_COMMITTER_NAME": "Cache Test",
        "GIT_AUTHOR_EMAIL": "cache@example.invalid",
        "GIT_COMMITTER_EMAIL": "cache@example.invalid",
    }
    for arguments in (["init", "-q"], ["add", "."], ["commit", "-qm", "Pinned"]):
        subprocess.run(["git", "-C", str(package), *arguments], check=True, env=environment)
    revision = subprocess.check_output(
        ["git", "-C", str(package), "rev-parse", "HEAD"], text=True
    ).strip()
    (canonical / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "Lib",
                        "type": "git",
                        "url": "https://example.invalid/Lib",
                        "rev": revision,
                    }
                ]
            }
        )
    )
    artifact = package / ".lake/build/lib/lean/Lib.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"first dependency output")
    before = identity.identity_key(identity.build_identity(canonical, ["Local.Support"], {}))
    artifact.write_bytes(b"updated dependency output")
    assert (
        identity.identity_key(identity.build_identity(canonical, ["Local.Support"], {})) != before
    )
    (package / "Lib.lean").write_text("agent edited dependency")
    with pytest.raises(ValueError, match="Edited or unlocked"):
        identity.build_identity(canonical, ["Local.Support"], {})


def test_concurrent_fresh_builds_publish_one_complete_entry(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _snapshot(canonical, tmp_path / "first")
    second = _snapshot(canonical, tmp_path / "second")

    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        _artifacts(kwargs["project_root"] / ".lake/build")
        return {"success": True}

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_build, (first, second)))
    assert all(result["success"] for result in results)
    assert sum(result["artifact_cache"]["published"] for result in results) == 1
    parent = Path(os.environ["LEANFLOW_HOME"]) / "cache/project-builds/v1"
    assert len(list(parent.iterdir())) == 1
    assert not list(parent.glob(".staging-*"))
    assert (next(parent.iterdir()) / MARKER).is_file()


@pytest.mark.parametrize("sidecar_suffix", ["ir", "ir.sig", "a", "dll", "o", "o.export"])
def test_compiler_resolution_uses_exact_actual_sandbox_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar_suffix: str
) -> None:
    """Respect actual Elan directory overrides rather than forcing the marker's toolchain."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("marker-toolchain\n")
    toolchain = tmp_path / "actual-override-toolchain"
    binaries = toolchain / "bin"
    binaries.mkdir(parents=True)
    for name in ("lean", "lake", "elan"):
        (binaries / name).write_bytes(("actual-" + name).encode())
        (binaries / name).chmod(0o700)
    core = toolchain / "lib/lean/Init.olean"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"actual core runtime")
    sidecar = core.parent / f"Init.{sidecar_suffix}"
    sidecar.write_bytes(b"actual native or IR sidecar")
    environment = {"PATH": str(binaries), "HOME": str(tmp_path / "home")}
    calls: list[dict[str, Any]] = []

    def resolve(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        assert argv == [str(binaries / "elan"), "which", argv[-1]]
        return subprocess.CompletedProcess(argv, 0, str(binaries / argv[-1]) + "\n", "")

    monkeypatch.setattr(identity.subprocess, "run", resolve)
    result = identity._compiler_identity(root, environment)
    assert calls and all(call["env"] == environment and call["cwd"] == root for call in calls)
    assert result["toolchain"] == "marker-toolchain"
    assert result["lean_path"] == str(binaries / "lean")
    assert result["core_runtime"]["lean/Init.olean"] == identity._toolchain_file_digest(core)
    before = result["core_runtime"]["lean/Init.olean"]
    core.write_bytes(b"changed core runtime")
    assert (
        identity._compiler_identity(root, environment)["core_runtime"]["lean/Init.olean"] != before
    )


@pytest.mark.parametrize("failure_source", ["compiler", "git"])
def test_optional_toolchain_or_git_timeout_falls_back_to_normal_build(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_source: str
) -> None:
    """Retain successful setup evidence when cache-only provenance commands time out."""

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("cache identity command", 10)

    if failure_source == "compiler":
        monkeypatch.setattr(identity, "_compiler_identity", timeout)
    else:
        package = canonical / ".lake/packages/Lib"
        (package / ".git").mkdir(parents=True)
        (canonical / "lake-manifest.json").write_text(
            json.dumps(
                {
                    "packages": [
                        {
                            "name": "Lib",
                            "type": "git",
                            "url": "https://example.invalid/Lib",
                            "rev": "a" * 40,
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(identity, "_git", timeout)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        check_process,
        "isolated_command",
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )
    result = _build(_snapshot(canonical, tmp_path / "first"))
    assert result["success"] is True and len(calls) == 1
    assert "timed out" in result["artifact_cache"]["reason"]
    assert not result["artifact_cache"]["published"]


@pytest.mark.parametrize("stage", ["restore", "publish"])
def test_optional_cache_storage_pressure_preserves_successful_build(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Skip extra cache work under pressure while keeping ordinary build evidence."""

    def pressure(*args: Any, **kwargs: Any) -> None:
        raise StoragePressure(0, 1)

    monkeypatch.setattr(ProjectBuildCache, stage, pressure)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        check_process,
        "isolated_command",
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )
    result = _build(_snapshot(canonical, tmp_path / "first"))
    assert result["success"] is True and len(calls) == 1
    assert result["artifact_cache"]["reason"] == str(StoragePressure(0, 1))
    assert not result["artifact_cache"]["published"]


@pytest.mark.parametrize("fresh", [False, True])
def test_normal_build_capacity_admission_still_blocks_under_pressure(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh: bool
) -> None:
    """Keep required storage admission authoritative even when optional caching is disabled."""
    paths = _snapshot(canonical, tmp_path / "first")

    def pressure(*args: Any, **kwargs: Any) -> None:
        raise StoragePressure(0, 1)

    monkeypatch.setattr(session_build, "ensure_storage_capacity", pressure)
    monkeypatch.setattr(
        check_process, "isolated_command", lambda **kwargs: pytest.fail("build launch")
    )
    result = _build(paths, fresh=fresh)
    assert result["success"] is False
    assert result["error"] == str(StoragePressure(0, 1))


def test_failed_registration_after_rename_rolls_back_and_allows_retry(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove a renamed owned entry if its final ownership registration fails."""

    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        _artifacts(kwargs["project_root"] / ".lake/build")
        return {"success": True}

    register = cache_module.register_entry

    def crash(entry: Path, **kwargs: Any) -> None:
        if kwargs.get("kind") == "project-build":
            raise OSError("registration disk error")
        register(entry, **kwargs)

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    monkeypatch.setattr(cache_module, "register_entry", crash)
    first = _build(_snapshot(canonical, tmp_path / "first"))
    assert first["success"] is True and not first["artifact_cache"]["published"]
    parent = Path(os.environ["LEANFLOW_HOME"]) / "cache/project-builds/v1"
    assert not list(parent.iterdir())
    monkeypatch.setattr(cache_module, "register_entry", register)
    second = _build(_snapshot(canonical, tmp_path / "second"))
    assert second["artifact_cache"]["published"] and not second["artifact_cache"]["hit"]
    assert read_entry(next(parent.iterdir()))["ready"] is True


def test_large_identity_is_sealed_separately_and_gc_marker_remains_compact(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep cache inventory and last-use updates independent of verbose input provenance."""
    paths = _snapshot(canonical, tmp_path / "first")
    cache = ProjectBuildCache(paths[0], ["Local.Support"], {})
    cache.identity["large_provenance"] = {str(index): "x" * 100 for index in range(10000)}
    cache.entry = cache.entry.parent / identity.identity_key(cache.identity)
    _artifacts(paths[0] / ".lake/build")
    assert cache.publish(paths[0] / ".lake/build")
    marker = cache.entry / MARKER
    assert marker.stat().st_size < 1024
    assert (cache.entry / "identity.json").stat().st_size > 1000000
    assert "identity" not in json.loads(marker.read_bytes())
    assert not (cache.entry / "identity.json").stat().st_mode & 0o222


def test_cache_hit_skips_publication_and_second_input_hash_pass(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limit each setup to one complete input fingerprint and skip publish after a hit."""

    def compile_imports(**kwargs: Any) -> dict[str, Any]:
        if not (kwargs["project_root"] / ".lake/build/lib/lean/Local/Support.olean").exists():
            _artifacts(kwargs["project_root"] / ".lake/build")
        return {"success": True}

    fingerprints: list[Path] = []
    original = cache_module.build_identity

    def fingerprint(project: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        fingerprints.append(project)
        return original(project, *args, **kwargs)

    monkeypatch.setattr(check_process, "isolated_command", compile_imports)
    monkeypatch.setattr(cache_module, "build_identity", fingerprint)
    first = _build(_snapshot(canonical, tmp_path / "first"))
    assert first["artifact_cache"]["published"] and len(fingerprints) == 1
    monkeypatch.setattr(
        ProjectBuildCache, "publish", lambda *args, **kwargs: pytest.fail("hit publication")
    )
    second = _build(_snapshot(canonical, tmp_path / "second"))
    assert second["artifact_cache"]["hit"] and len(fingerprints) == 2


@pytest.mark.parametrize("value", ["1", "true", " YES ", "On"])
def test_cache_opt_in_accepts_standard_boolean_flag_values(
    canonical: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_BUILD_CACHE", value)
    assert identity.build_identity(canonical, ["Local.Support"], {})["relocatable_build_contract"]


@pytest.mark.parametrize(
    "mutation",
    [
        "resource_added",
        "resource_changed",
        "dependency_artifact",
        "dependency_added",
        "override_added",
    ],
)
def test_publication_metadata_guard_detects_input_changes_without_dependency_rehash(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Retain exact hashed bytes and dependency closure across the isolated compile boundary."""
    package = canonical / ".lake/packages/Lib"
    (package / ".git").mkdir(parents=True)
    artifact = package / ".lake/build/lib/lean/Lib.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"first output")
    (canonical / "resource.txt").write_bytes(b"first input")
    revision = "a" * 40
    (canonical / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "Lib",
                        "type": "git",
                        "url": "https://example.invalid/Lib",
                        "rev": revision,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        identity,
        "_git",
        lambda checkout, *args: (
            f"{checkout.resolve()}\n{revision}" if args[0] == "rev-parse" else ""
        ),
    )
    paths = _snapshot(canonical, tmp_path / "first")
    cache = ProjectBuildCache(paths[0], ["Local.Support"], {})
    _artifacts(paths[0] / ".lake/build")
    if mutation == "resource_added":
        (paths[0] / "added.txt").write_bytes(b"new input")
    elif mutation == "resource_changed":
        path = paths[0] / "resource.txt"
        before = path.stat()
        path.write_bytes(b"other input")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif mutation == "dependency_artifact":
        path = paths[0] / ".lake/packages/Lib/.lake/build/lib/lean/Lib.olean"
        before = path.stat()
        path.write_bytes(b"other output")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif mutation == "dependency_added":
        (paths[0] / ".lake/packages/Other").mkdir()
    else:
        (paths[0] / ".lake/package-overrides.json").write_text("{}")
    monkeypatch.setattr(
        cache_module, "build_identity", lambda *args, **kwargs: pytest.fail("identity rehash")
    )
    assert not cache.publish(paths[0] / ".lake/build")
    assert "changed during" in cache.metadata["reason"]
