"""Bind compiled dependency inventories to bytes without mistaking inventory for acceptance."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from leanflow_cli.formalization import dependency_inputs as inputs
from leanflow_cli.formalization import environment, review_cache
from tests.leanflow.test_formalization_admission import compiler_fixture

PINNED_REVISION = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def compiled_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a pinned package with deliberately unauthenticated compiled fixture bytes."""
    root = tmp_path / "project"
    package = root / ".lake/packages/example"
    library = package / ".lake/build/lib/lean"
    library.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "example", "type": "git", "rev": PINNED_REVISION},
                ]
            }
        )
    )
    (package / "lakefile.toml").write_text('name = "example"\n[[lean_lib]]\nname = "Example"\n')
    (package / "Example.lean").write_text(
        'macro "dependency_tactic" : tactic => `(tactic| trivial)\n'
    )
    for extension in ("olean", "ilean", "ir", "ir.sig", "olean.private", "olean.server"):
        (library / f"Example.{extension}").write_bytes(b"inventory fixture; not kernel accepted")
    (library.parent / "libexample.so").write_bytes(b"recorded native library bytes")
    (package / "resources").mkdir()
    (package / "resources/table.json").write_text('{"value": 1}\n')
    (package / ".git").mkdir()
    (package / ".git/config").write_text("local checkout metadata\n")
    compiler = compiler_fixture(tmp_path / "compiler")
    monkeypatch.setattr(environment, "_compiler_root", lambda _: compiler)
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    return root, package


def test_pinned_compiled_library_inventory_is_content_bound_but_not_acceptance(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    manifest = inputs.dependency_inputs(root)
    record = manifest["packages"]["example"]
    assert record["mode"] == "compiled-library"
    assert record["revision"] == PINNED_REVISION
    assert "accepted" not in manifest and "accepted" not in record
    for relative in (
        "Example.lean",
        "lakefile.toml",
        "resources/table.json",
        ".lake/build/lib/lean/Example.olean",
        ".lake/build/lib/lean/Example.olean.private",
        ".lake/build/lib/lean/Example.olean.server",
        ".lake/build/lib/lean/Example.ilean",
        ".lake/build/lib/lean/Example.ir",
        ".lake/build/lib/lean/Example.ir.sig",
        ".lake/build/lib/libexample.so",
    ):
        assert (
            record["files"][relative]
            == hashlib.sha256((package / relative).read_bytes()).hexdigest()
        )
    assert not any(name.startswith(".git/") for name in record["files"])
    assert inputs.compiled_library_paths(root) == [package / ".lake/build/lib/lean"]


@pytest.mark.parametrize(
    "relative",
    [
        "Example.lean",
        "lakefile.toml",
        "resources/table.json",
        ".lake/build/lib/lean/Example.olean",
        ".lake/build/lib/lean/Example.olean.private",
        ".lake/build/lib/lean/Example.olean.server",
        ".lake/build/lib/lean/Example.ilean",
        ".lake/build/lib/lean/Example.ir",
        ".lake/build/lib/lean/Example.ir.sig",
        ".lake/build/lib/libexample.so",
    ],
)
def test_every_compiled_library_input_change_invalidates_persistent_environment(
    compiled_project: tuple[Path, Path], relative: str
) -> None:
    root, package = compiled_project
    before = environment.environment_identity(root)
    path = package / relative
    original = path.read_bytes()
    metadata = path.stat()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert path.stat().st_size == metadata.st_size
    assert environment.environment_identity(root) != before


def test_dependency_checkout_metadata_is_not_a_mathematical_input(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    before = environment.environment_identity(root)
    (package / ".git/config").write_text("a different local remote and checkout path\n")
    assert environment.environment_identity(root) == before


def test_source_only_pinned_package_does_not_supply_a_compiled_search_path(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    library = package / ".lake/build/lib/lean"
    for path in library.iterdir():
        path.unlink()
    assert "Example.lean" in inputs.dependency_inputs(root)["packages"]["example"]["files"]
    assert inputs.compiled_library_paths(root) == []


def test_compiled_sidecar_additions_and_deletions_invalidate_the_environment(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    before = environment.environment_identity(root)
    path = package / ".lake/build/lib/lean/Example.olean.private"
    original = path.read_bytes()
    path.unlink()
    assert environment.environment_identity(root) != before
    path.write_bytes(original)
    assert environment.environment_identity(root) == before
    (path.parent / "Extra.olean").write_bytes(b"new compiled declarations")
    assert environment.environment_identity(root) != before


@pytest.mark.parametrize(
    "relative",
    ["Example.lean", "resources", ".lake/build/lib/lean", ".lake/build/lib/lean/Example.olean"],
)
def test_compiled_inventory_and_loader_paths_refuse_aliased_inputs(
    compiled_project: tuple[Path, Path], relative: str
) -> None:
    root, package = compiled_project
    path = package / relative
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(ValueError, match="aliased|symlink|nonregular"):
        inputs.dependency_inputs(root)
    with pytest.raises(ValueError, match="aliased|symlink|nonregular"):
        inputs.compiled_library_paths(root)


def test_actual_dependency_change_invalidates_cached_review_evidence(
    compiled_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, package = compiled_project
    monkeypatch.setattr(review_cache, "evidence_documents", lambda record: {})
    record = dict(
        inventory={},
        source_bundle={},
        environment_id=environment.environment_identity(root),
        policy_id="policy",
        layout={},
        units=[],
        checks={"declarations": []},
    )
    dossier = {"phase": "translation", "units": [], "regions": []}
    before = review_cache.rejection_key(record, dossier, {})
    (package / ".lake/build/lib/lean/Example.olean").write_bytes(b"changed compiled library")
    record["environment_id"] = environment.environment_identity(root)
    assert review_cache.rejection_key(record, dossier, {}) != before


def test_manifest_revision_change_invalidates_compiled_library_identity(
    compiled_project: tuple[Path, Path],
) -> None:
    root, _ = compiled_project
    before = environment.environment_identity(root)
    path = root / "lake-manifest.json"
    data = json.loads(path.read_text())
    data["packages"][0]["rev"] = "f" * 40
    path.write_text(json.dumps(data))
    assert environment.environment_identity(root) != before


def add_repl_tooling(root: Path) -> Path:
    """Install a clearly scoped tooling package next to the mathematical library."""
    package = root / ".lake/packages/repl"
    library = package / ".lake/build/lib/lean/REPL"
    library.mkdir(parents=True)
    (library.parent / "REPL.olean").write_bytes(b"tooling root")
    (library / "Main.olean").write_bytes(b"tooling executable module")
    (package / "REPL.lean").write_text("import REPL.Main\n")
    (package / "REPL").mkdir()
    (package / "REPL/Main.lean").write_text("def main : IO Unit := pure ()\n")
    (package / "lakefile.toml").write_text(
        'name = "REPL"\n[[lean_lib]]\nname = "REPL"\n'
        '[[lean_exe]]\nname = "repl"\nroot = "REPL.Main"\n'
    )
    path = root / "lake-manifest.json"
    data = json.loads(path.read_text())
    data["packages"].append({"name": "repl", "type": "git", "rev": "a" * 40})
    path.write_text(json.dumps(data))
    return package


def test_repl_tooling_remains_fingerprinted_but_is_not_a_library_search_path(
    compiled_project: tuple[Path, Path],
) -> None:
    root, library = compiled_project
    repl = add_repl_tooling(root)
    record = inputs.dependency_inputs(root)["packages"]["repl"]
    assert record["mode"] == "tooling"
    assert ".lake/build/lib/lean/REPL/Main.olean" in record["files"]
    assert inputs.compiled_library_paths(root) == [library / ".lake/build/lib/lean"]
    before = environment.environment_identity(root)
    (repl / ".lake/build/lib/lean/REPL/Main.olean").write_bytes(b"modified tooling")
    assert environment.environment_identity(root) != before


@pytest.mark.parametrize(
    "old,new",
    [
        ('name = "REPL"\n[[lean_lib]]', 'name = "Other"\n[[lean_lib]]'),
        ('[[lean_lib]]\nname = "REPL"', '[[lean_lib]]\nname = "Other"'),
        ('[[lean_lib]]\nname = "REPL"', '[[lean_lib]]\nname = "REPL"\nroots = ["REPL", "Other"]'),
        ('root = "REPL.Main"', 'root = "Other.Main"'),
        ('[[lean_exe]]\nname = "repl"', '[[lean_exe]]\nname = "other"'),
    ],
)
def test_repl_name_cannot_hide_other_libraries_or_executables(
    compiled_project: tuple[Path, Path], old: str, new: str
) -> None:
    root, library = compiled_project
    repl = add_repl_tooling(root)
    config = repl / "lakefile.toml"
    content = config.read_text()
    assert old in content
    config.write_text(content.replace(old, new))
    record = inputs.dependency_inputs(root)["packages"]["repl"]
    assert record["mode"] == "compiled-library"
    assert set(inputs.compiled_library_paths(root)) == {
        library / ".lake/build/lib/lean",
        repl / ".lake/build/lib/lean",
    }


def test_extra_repl_executable_requires_full_compiled_library_checks(
    compiled_project: tuple[Path, Path],
) -> None:
    root, _ = compiled_project
    repl = add_repl_tooling(root)
    config = repl / "lakefile.toml"
    config.write_text(config.read_text() + '\n[[lean_exe]]\nname = "other"\nroot = "Other.Main"\n')
    assert inputs.dependency_inputs(root)["packages"]["repl"]["mode"] == "compiled-library"
    assert repl / ".lake/build/lib/lean" in inputs.compiled_library_paths(root)


def test_conventional_repl_test_driver_remains_excluded_tooling(
    compiled_project: tuple[Path, Path],
) -> None:
    root, library = compiled_project
    repl = add_repl_tooling(root)
    config = repl / "lakefile.toml"
    config.write_text(config.read_text() + '\n[[lean_exe]]\nname = "test"\nroot = "Test"\n')
    assert inputs.dependency_inputs(root)["packages"]["repl"]["mode"] == "tooling"
    assert inputs.compiled_library_paths(root) == [library / ".lake/build/lib/lean"]
    config.write_text(config.read_text().replace('root = "Test"', 'root = "Other.Test"'))
    assert inputs.dependency_inputs(root)["packages"]["repl"]["mode"] == "compiled-library"


def test_mathlib_named_pinned_inventory_has_no_extra_trust_or_verdict(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    package.rename(package.with_name("mathlib"))
    path = root / "lake-manifest.json"
    data = json.loads(path.read_text())
    data["packages"][0].update(
        name="mathlib", url="https://github.com/leanprover-community/mathlib4", trusted=True
    )
    path.write_text(json.dumps(data))
    record = inputs.dependency_inputs(root)["packages"]["mathlib"]
    assert record["mode"] == "compiled-library"
    assert "accepted" not in record and "trusted" not in record


def test_relative_resource_alias_has_the_same_inventory_as_materialized_bytes(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    scripts = package / "scripts/bench/size"
    scripts.mkdir(parents=True)
    content = b"#!/usr/bin/env python3\nprint('resource script')\n"
    (scripts / "run").write_bytes(content)
    alias = scripts / "run.py"
    alias.symlink_to("run")
    inventory = inputs.dependency_inputs(root)
    files = inventory["packages"]["example"]["files"]
    assert files["scripts/bench/size/run.py"] == files["scripts/bench/size/run"]
    before = environment.environment_identity(root)
    alias.unlink()
    alias.write_bytes(content)
    assert inputs.dependency_inputs(root) == inventory
    assert environment.environment_identity(root) == before


@pytest.mark.parametrize(
    "kind", ["escape", "absolute", "directory", "missing", "cyclic", "code", "compiled", "config"]
)
def test_resource_alias_cannot_escape_or_disguise_executable_dependency_inputs(
    compiled_project: tuple[Path, Path], kind: str
) -> None:
    root, package = compiled_project
    alias = package / "resource.py"
    if kind == "escape":
        (root / ".lake/packages/outside.py").write_text("outside = True\n")
        alias.symlink_to("../outside.py")
    elif kind == "absolute":
        alias.symlink_to(package / "resources/table.json")
    elif kind == "directory":
        alias.symlink_to("resources", target_is_directory=True)
    elif kind == "missing":
        alias.symlink_to("missing.py")
    elif kind == "cyclic":
        alias.symlink_to("other.py")
        (package / "other.py").symlink_to("resource.py")
    else:
        alias.symlink_to(
            {
                "code": "Example.lean",
                "compiled": ".lake/build/lib/lean/Example.olean",
                "config": "lakefile.toml",
            }[kind]
        )
    with pytest.raises(ValueError):
        inputs.dependency_inputs(root)


@pytest.mark.parametrize("hop", ["absolute", "git", "directory_parent"])
def test_resource_alias_validates_each_hop_before_normalizing_its_target(
    compiled_project: tuple[Path, Path], hop: str
) -> None:
    from leanflow_cli.formalization.dependency_files import resolve_dependency_file

    _, package = compiled_project
    alias = package / "alias.py"
    target = package / "resources/table.json"
    if hop == "absolute":
        (package / "middle.py").symlink_to(target)
        alias.symlink_to("middle.py")
    elif hop == "git":
        (package / ".git/middle.py").symlink_to(target)
        alias.symlink_to(".git/middle.py")
    else:
        (package / "shortcut").symlink_to("resources", target_is_directory=True)
        alias.symlink_to("shortcut/../resources/table.json")
    with pytest.raises(ValueError, match="aliased"):
        resolve_dependency_file(alias, package)


def test_confined_relative_resource_chain_can_be_materialized(
    compiled_project: tuple[Path, Path],
) -> None:
    root, package = compiled_project
    (package / "middle.py").symlink_to("resources/table.json")
    (package / "alias.py").symlink_to("middle.py")
    files = inputs.dependency_inputs(root)["packages"]["example"]["files"]
    assert files["alias.py"] == files["middle.py"] == files["resources/table.json"]
