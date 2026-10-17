"""Bind formalization snapshots to canonical dependency bytes and explicit policy failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import dependency_snapshot as snapshot
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.workflows.prover import native_snapshot


def _dependencies(root: Path) -> dict[str, Any]:
    """Model the path-independent admitted manifest, including opaque compiled resources."""
    packages = root / ".lake/packages"
    return {
        "policy": "test-compiled-authority",
        "packages": {
            package.name: {
                "directory": package.relative_to(root).as_posix(),
                "files": {
                    path.relative_to(package).as_posix(): digest(path.read_bytes())
                    for path in sorted(package.rglob("*"))
                    if path.is_file() and ".git" not in path.relative_to(package).parts
                },
            }
            for package in sorted(packages.iterdir())
        },
    }


def _environment(root: Path) -> str:
    """Bind exact configuration and all admitted dependency bytes without a compiler."""
    return identity(
        {
            "dependencies": _dependencies(root),
            "configuration": {
                name: digest((root / name).read_bytes())
                for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json")
            },
        }
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "canonical"
    package = root / ".lake/packages/mathlib"
    package.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
    (root / "lakefile.toml").write_text('name = "example"\n')
    (root / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "mathlib", "type": "git", "rev": "a" * 40}]})
    )
    (root / "Main.lean").write_text("theorem checked : True := by trivial\n")
    for relative, data in {
        "lakefile.lean": b"import Lake\nopen Lake DSL\npackage mathlib\n",
        "Mathlib.lean": b"def libraryValue := 37\n",
        "resource.txt": b"canonical resource",
        ".lake/build/lib/lean/Mathlib.olean": b"canonical compiled artifact",
        ".lake/build/lib/lean/Mathlib.olean.private": b"private sidecar",
        ".lake/config/0/lakefile.olean": b"precompiled configuration",
        "node_modules/display.js": b"admitted supporting resource",
        ".git/HEAD": b"ignored private bookkeeping",
    }.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for relative in (
        ".leanflow/private.json",
        ".codex/auth.json",
        ".claude/settings.json",
        ".lake/build/lib/lean/Main.olean",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must not be copied")
    monkeypatch.setattr(
        snapshot,
        "inspect_layout",
        lambda root: {
            "accepted": True,
            "build_dir": ".lake/build",
            "lean_lib_dir": ".lake/build/lib/lean",
        },
    )
    monkeypatch.setattr(snapshot, "dependency_inputs", _dependencies)
    monkeypatch.setattr(snapshot, "environment_identity", _environment)
    monkeypatch.setattr(snapshot, "register_workspace", lambda path: None)
    monkeypatch.setattr(snapshot, "ensure_storage_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        native_snapshot,
        "materialize_dependencies",
        lambda *args, **kwargs: pytest.fail("A formalization checker must not use shared cache"),
    )
    monkeypatch.delenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", raising=False)
    monkeypatch.delenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", raising=False)
    return root, package


def test_snapshot_freezes_exact_canonical_artifacts_and_resources_without_cache(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, package = project
    target = snapshot.create_formalization_snapshot(root, tmp_path / "frozen")
    frozen_package = target / ".lake/packages/mathlib"
    assert _dependencies(target) == _dependencies(root)
    assert _environment(target) == _environment(root)
    assert (frozen_package / "node_modules/display.js").is_file()
    assert (frozen_package / ".lake/config/0/lakefile.olean").is_file()
    assert not (frozen_package / ".git").exists()
    assert not (target / ".leanflow").exists()
    assert not (target / ".codex").exists()
    assert not (target / ".claude").exists()
    assert not (target / ".lake/build").exists()
    original = package / ".lake/build/lib/lean/Mathlib.olean"
    copied = frozen_package / ".lake/build/lib/lean/Mathlib.olean"
    assert copied.stat().st_ino != original.stat().st_ino
    copied.write_bytes(b"independent private mutation")
    assert original.read_bytes() == b"canonical compiled artifact"


@pytest.mark.parametrize("change", ["source", "compiled", "new_file", "configuration"])
def test_concurrent_canonical_input_mutation_discards_the_entire_snapshot(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root, package = project
    target = tmp_path / "frozen"
    original_copy = snapshot.copy_file
    fired = False

    def copy_and_mutate(source: Path, destination: Path) -> str:
        nonlocal fired
        result = original_copy(source, destination)
        if not fired:
            fired = True
            changed = {
                "source": package / "Mathlib.lean",
                "compiled": package / ".lake/build/lib/lean/Mathlib.olean",
                "new_file": package / "extra.txt",
                "configuration": root / "lean-toolchain",
            }[change]
            changed.write_bytes(b"changed while copying")
        return result

    monkeypatch.setattr(snapshot, "copy_file", copy_and_mutate)
    with pytest.raises(ValueError, match="changed while freezing"):
        snapshot.create_formalization_snapshot(root, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".formalization-snapshot-*"))


def test_copy_corruption_fails_before_snapshot_publication(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = project
    target = tmp_path / "frozen"
    original_copy = snapshot.copy_file

    def corrupt_copy(source: Path, destination: Path) -> str:
        result = original_copy(source, destination)
        if source.name == "Mathlib.olean":
            destination.write_bytes(b"a substituted stale cache artifact")
        return result

    monkeypatch.setattr(snapshot, "copy_file", corrupt_copy)
    with pytest.raises(ValueError, match="dependency inputs changed"):
        snapshot.create_formalization_snapshot(root, target)
    assert not target.exists()


def test_clean_room_denial_fails_instead_of_silently_filtering_admitted_resource(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = project
    monkeypatch.setattr(
        snapshot,
        "clean_room_path_block_reason",
        lambda path, **kwargs: "denied by clean-room policy" if path.name == "resource.txt" else "",
    )
    target = tmp_path / "frozen"
    with pytest.raises(ValueError, match="cannot omit an admitted input.*clean-room"):
        snapshot.create_formalization_snapshot(root, target)
    assert not target.exists()


@pytest.mark.parametrize("location", ["package", "package_file", "destination"])
def test_aliases_never_become_mutable_dependency_views(
    project: tuple[Path, Path], tmp_path: Path, location: str
) -> None:
    root, package = project
    target = tmp_path / "frozen"
    if location == "destination":
        path = target
        real = tmp_path / "outside"
        real.mkdir()
    else:
        path = package if location == "package" else package / "Mathlib.lean"
        real = path.with_name(path.name + "-actual")
        path.rename(real)
    path.symlink_to(real, target_is_directory=real.is_dir())
    with pytest.raises(ValueError, match="symlink|linked|aliased"):
        snapshot.create_formalization_snapshot(root, target)


def test_admitted_resource_alias_is_materialized_as_independent_regular_bytes(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, package = project
    script = package / "scripts/bench/run"
    script.parent.mkdir(parents=True)
    script.write_text("print('exact resource bytes')\n")
    alias = script.with_suffix(".py")
    alias.symlink_to("run")
    frozen = snapshot.create_formalization_snapshot(root, tmp_path / "frozen")
    copied = frozen / ".lake/packages/mathlib/scripts/bench/run.py"
    assert not copied.is_symlink()
    assert copied.read_bytes() == script.read_bytes()
    assert copied.stat().st_ino != script.stat().st_ino
    assert _dependencies(root) == _dependencies(frozen)


def test_real_compiled_manifest_matches_the_private_normalized_view(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, package = project
    (package / "resource-alias.py").symlink_to("resource.txt")
    monkeypatch.setattr(snapshot, "dependency_inputs", dependency_inputs)
    before = dependency_inputs(root)
    assert before["packages"]["mathlib"]["mode"] == "compiled-library"
    frozen = snapshot.create_formalization_snapshot(root, tmp_path / "frozen")
    assert dependency_inputs(frozen) == before
    assert not (frozen / ".lake/packages/mathlib/resource-alias.py").is_symlink()


@pytest.mark.parametrize("kind", ["absolute", "escape", "directory", "code", "dangling"])
def test_resource_alias_exceptions_do_not_relax_package_boundaries(
    project: tuple[Path, Path], tmp_path: Path, kind: str
) -> None:
    root, package = project
    target = {
        "absolute": str(package / "resource.txt"),
        "escape": "../../../Main.lean",
        "directory": ".lake",
        "code": "Mathlib.lean",
        "dangling": "missing-resource",
    }[kind]
    (package / "alias.py").symlink_to(target)
    with pytest.raises(ValueError, match="aliased"):
        snapshot.create_formalization_snapshot(root, tmp_path / "frozen")


def test_environment_drift_after_dependency_copy_is_detected(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = project
    calls = 0

    def unstable_environment(path: Path) -> str:
        nonlocal calls
        calls += 1
        return "same" if calls < 3 else "different compiler"

    with pytest.raises(ValueError, match="environment changed while freezing"):
        snapshot.create_formalization_snapshot(
            root, tmp_path / "frozen", environment=unstable_environment
        )


def test_previous_candidate_environment_must_match_before_any_copy(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = project
    monkeypatch.setattr(snapshot, "copy_file", lambda *args: pytest.fail("Must reject before copy"))
    with pytest.raises(ValueError, match="Canonical.*environment changed"):
        snapshot.create_formalization_snapshot(
            root, tmp_path / "frozen", expected_environment="stale accepted environment"
        )


def test_snapshot_inside_controller_directory_does_not_recursively_copy_itself(
    project: tuple[Path, Path],
) -> None:
    root, _ = project
    target = snapshot.create_formalization_snapshot(root, root / ".leanflow/checks/project")
    assert _dependencies(target) == _dependencies(root)
    assert not (target / ".leanflow").exists()


def test_snapshot_destination_cannot_modify_an_admitted_dependency(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    target = package / "new-output/frozen"
    before = _dependencies(root)
    with pytest.raises(ValueError, match="inside an admitted dependency"):
        snapshot.create_formalization_snapshot(root, target)
    assert not target.parent.exists()
    assert _dependencies(root) == before


def test_custom_build_directory_is_not_copied(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = project
    stale = root / "outputs/custom/lib/lean/Mathlib/Shadow.olean"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"untrusted prior compiler output")
    monkeypatch.setattr(
        snapshot,
        "inspect_layout",
        lambda root: {
            "accepted": True,
            "build_dir": "outputs/custom",
            "lean_lib_dir": "outputs/custom/lib/lean",
        },
    )
    frozen = snapshot.create_formalization_snapshot(root, tmp_path / "frozen")
    assert not (frozen / "outputs/custom").exists()
    assert stale.read_bytes() == b"untrusted prior compiler output"


def test_snapshot_rejects_build_root_containing_dependencies(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, package = project
    monkeypatch.setattr(
        snapshot,
        "inspect_layout",
        lambda root: {"accepted": True, "build_dir": ".lake", "lean_lib_dir": ".lake/packages"},
    )
    with pytest.raises(ValueError, match="disjoint from dependencies"):
        snapshot.create_formalization_snapshot(root, tmp_path / "frozen")
    assert (package / "Mathlib.lean").exists()
    assert not (tmp_path / "frozen").exists()
