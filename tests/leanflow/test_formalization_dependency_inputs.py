"""Block unknown external elaboration inputs and fingerprint every admitted package byte."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leanflow_cli.formalization import environment
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from tests.leanflow.test_formalization_admission import compiler_fixture


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "project"
    package = root / ".lake/packages/example"
    package.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
    (root / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "example", "type": "git", "rev": "pinned"}]})
    )
    (package / "lakefile.toml").write_text('name = "example"\n[[lean_lib]]\nname = "Example"\n')
    (package / "Example.lean").write_text("def exampleValue : Nat := 1\n")
    (package / "input.txt").write_text("first value")
    compiler = compiler_fixture(tmp_path / "compiler")
    monkeypatch.setattr(environment, "_compiler_root", lambda _: compiler)
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    return root, package


def test_all_declarative_dependency_files_bind_persistent_identity(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    before = environment.environment_identity(root)
    manifest = environment.environment_manifest(root)
    assert "input.txt" in manifest["dependencies"]["packages"]["example"]["files"]
    assert "dependency_inputs.py" in manifest["policy"]
    (package / "input.txt").write_text("second value")
    assert environment.environment_identity(root) != before


def test_same_size_dependency_rewrite_cannot_reuse_a_persistent_identity(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    path = package / "input.txt"
    before = environment.environment_identity(root)
    metadata = path.stat()
    path.write_text("other value")
    assert path.stat().st_size == metadata.st_size
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert environment.environment_identity(root) != before


def test_dependency_revision_and_configuration_changes_invalidate_identity(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    first = environment.environment_identity(root)
    manifest = root / "lake-manifest.json"
    data = json.loads(manifest.read_text())
    data["packages"][0]["rev"] = "different-pinned-revision"
    manifest.write_text(json.dumps(data))
    second = environment.environment_identity(root)
    assert second != first
    (package / "lakefile.toml").write_text(
        'name = "example"\n[[lean_lib]]\nname = "Example"\nroots = ["Other"]\n'
    )
    assert environment.environment_identity(root) != second


@pytest.mark.parametrize("location", ["manifest", "packages_directory", "package_directory"])
def test_dependency_manifest_and_directory_aliases_are_rejected(
    project: tuple[Path, Path], location: str
) -> None:
    root, package = project
    path = {
        "manifest": root / "lake-manifest.json",
        "packages_directory": package.parent,
        "package_directory": package,
    }[location]
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(ValueError, match="symlink"):
        dependency_inputs(root)


@pytest.mark.parametrize("alias_directory", [False, True])
def test_dependency_content_aliases_are_rejected_even_when_targets_are_inside_package(
    project: tuple[Path, Path], alias_directory: bool
) -> None:
    root, package = project
    if alias_directory:
        target = package / "resources"
        target.mkdir()
        (target / "input.txt").write_text("same project-owned input")
    else:
        target = package / "input.txt"
    (package / "alias").symlink_to(target, target_is_directory=alias_directory)
    with pytest.raises(ValueError, match="aliased or nonregular"):
        dependency_inputs(root)


@pytest.mark.parametrize(
    "source",
    [
        'elab "external" : term => do\n let text ← IO.FS.readFile "input.txt"\n pure (Lean.mkNatLit text.length)\n',
        "initialize x : Nat ← pure 1\n",
        'def hidden := s!"{(by run_tac pure ())}"\n',
    ],
)
def test_pinned_package_custom_elaborators_never_gain_implicit_trust(
    project: tuple[Path, Path], source: str
) -> None:
    root, package = project
    (package / "Example.lean").write_text(source)
    with pytest.raises(ValueError, match="unsupported_dependency_inputs.*example"):
        environment.environment_identity(root)


@pytest.mark.parametrize(
    "relative", [".lake/build/lib/lean/Example.olean", "plugin.so", "Example.olean.private"]
)
def test_source_admission_does_not_authenticate_prebuilt_package_code(
    project: tuple[Path, Path], relative: str
) -> None:
    root, package = project
    output = package / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"unknown compiled initializer")
    with pytest.raises(ValueError, match="unauthenticated precompiled input"):
        dependency_inputs(root)


def test_package_dynamic_configuration_and_escaping_aliases_block(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    config = package / "lakefile.toml"
    original = config.read_text()
    config.write_text(original + 'plugins = ["hidden"]\n')
    with pytest.raises(ValueError, match="unsupported_dependency_inputs"):
        dependency_inputs(root)
    config.write_text(original)
    (package / "hidden.txt").symlink_to(root / "lean-toolchain")
    with pytest.raises(ValueError, match="aliased or nonregular"):
        dependency_inputs(root)


def test_mathlib_name_and_url_do_not_self_authorize_effectful_inputs(
    project: tuple[Path, Path],
) -> None:
    root, package = project
    (package / "Example.lean").write_text('macro "new_tactic" : tactic => `(tactic| trivial)\n')
    package.rename(package.with_name("mathlib"))
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "mathlib",
                        "type": "git",
                        "rev": "pinned",
                        "url": "https://github.com/leanprover-community/mathlib4",
                        "trusted": True,
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="unsupported_dependency_inputs: mathlib"):
        dependency_inputs(root)
