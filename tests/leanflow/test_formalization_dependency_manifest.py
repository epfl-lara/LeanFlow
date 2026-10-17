"""Reject malformed or ambiguous dependency roots before inventoring or loading packages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import dependency_inputs as inputs


@pytest.fixture
def project(tmp_path: Path) -> Path:
    package = tmp_path / ".lake/packages/example"
    package.mkdir(parents=True)
    (package / "lakefile.toml").write_text('name = "example"\n[[lean_lib]]\nname = "Example"\n')
    (package / "Example.lean").write_text("def value : Nat := 1\n")
    return tmp_path


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        None,
        "manifest",
        {"packages": {}},
        {"packagesDir": None, "packages": []},
        {"packagesDir": 3, "packages": []},
        {"packagesDir": "/tmp", "packages": []},
        {"packages": [{"name": "example", "type": "path"}]},
        {"packages": [{"name": "example", "type": "path", "dir": []}]},
        {"packages": [{"name": "example", "type": "path", "dir": "/tmp"}]},
        {"packages": [{"name": "example", "type": "git", "rev": {"nested": "pin"}}]},
        {"packages": [{"name": "example", "type": "git", "rev": ["pin"]}]},
        {"packages": [{"name": "example", "type": "git", "rev": 42}]},
        {"packages": [{"name": "example", "type": "git", "rev": " "}]},
    ],
)
def test_malformed_manifest_always_reports_a_domain_error(project: Path, manifest: object) -> None:
    (project / "lake-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        inputs.dependency_inputs(project)


@pytest.mark.parametrize("subdirectory", ["nested", "../outside", "/tmp", [], {}, 42])
def test_subdirectory_checkout_cannot_silently_load_the_repository_root(
    project: Path, subdirectory: object
) -> None:
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "example", "type": "git", "rev": "a" * 40, "subDir": subdirectory}
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="subDir"):
        inputs.dependency_inputs(project)


@pytest.mark.parametrize("kind", ["same", "nested", "private", "case_collision"])
def test_ambiguous_or_private_dependency_roots_block_before_snapshot(
    project: Path, kind: str
) -> None:
    package = project / ".lake/packages/example"
    packages = [{"name": "example", "type": "git", "rev": "a" * 40}]
    if kind == "case_collision":
        packages.append({"name": "EXAMPLE", "type": "git", "rev": "b" * 40})
    else:
        relative = {
            "same": ".lake/packages/example",
            "nested": ".lake/packages/example/nested",
            "private": ".leanflow/private-package",
        }[kind]
        target = project / relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "lakefile.toml").write_text((package / "lakefile.toml").read_text())
        packages.append({"name": "other", "type": "path", "dir": relative})
    (project / "lake-manifest.json").write_text(json.dumps({"packages": packages}))
    with pytest.raises(ValueError, match="overlap|duplicate"):
        inputs.dependency_inputs(project)


def test_loader_uses_the_same_manifest_root_that_was_inventoried(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = project / ".lake/packages/example"
    library = package / ".lake/build/lib/lean"
    library.mkdir(parents=True)
    (library / "Example.olean").write_bytes(b"frozen library")
    manifest = project / "lake-manifest.json"
    value = {"packages": [{"name": "example", "type": "git", "rev": "a" * 40}]}
    manifest.write_text(json.dumps(value))
    read = inputs.dependency_inputs

    def changed_after_inventory(root: Path) -> dict:
        result = read(root)
        manifest.write_text(json.dumps({**value, "packagesDir": "unrecorded"}))
        return result

    monkeypatch.setattr(inputs, "dependency_inputs", changed_after_inventory)
    assert inputs.compiled_library_paths(project) == [library]
