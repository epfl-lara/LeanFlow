"""Reject module-root capture even when exact compiled declaration filenames differ."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import dependency_inputs, environment, lean_layout
from leanflow_cli.formalization.dependency_modules import module_roots, validate_library_roots
from tests.leanflow.test_formalization_admission import compiler_fixture


def _library(root: Path, package: str, module: str) -> Path:
    """Create a package whose compiled bytes are inventory inputs, never kernel evidence."""
    directory = root / ".lake/packages" / package
    library = directory / ".lake/build/lib/lean"
    artifact = library.joinpath(*module.split(".")).with_suffix(".olean")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"synthetic artifact")
    (directory / "lakefile.toml").write_text(f'name = "{package}"\n')
    return library


@pytest.mark.parametrize("empty", [False, True])
def test_partial_or_empty_compiler_root_cannot_capture_pinned_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    root = tmp_path / "project"
    compiler = compiler_fixture(tmp_path / "compiler")
    monkeypatch.setattr(environment, "_compiler_root", lambda root: compiler)
    library = _library(root, "example", "Example")
    (library / "Init").mkdir()
    if not empty:
        (library / "Init/Captured.olean").write_bytes(b"partial shadow")
    (root / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "example", "type": "git", "rev": "a" * 40}]})
    )
    with pytest.raises(ValueError, match="Ambiguous module root Init.*pinned compiler"):
        dependency_inputs.compiled_library_paths(root)


def test_distinct_packages_cannot_split_one_module_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    compiler = compiler_fixture(tmp_path / "compiler")
    monkeypatch.setattr(environment, "_compiler_root", lambda root: compiler)
    _library(root, "first", "Shared.One")
    _library(root, "second", "Shared.Two")
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": name, "type": "git", "rev": "a" * 40} for name in ("first", "second")
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="Ambiguous module root Shared"):
        dependency_inputs.compiled_library_paths(root)


def test_disjoint_libraries_and_compiler_roots_are_supported(tmp_path: Path) -> None:
    compiler = compiler_fixture(tmp_path / "compiler")
    first = _library(tmp_path / "project", "first", "First.Nested")
    second = _library(tmp_path / "project", "second", "Second")
    assert module_roots(first) == {"First"}
    assert module_roots(second) == {"Second"}
    validate_library_roots([first, second], compiler / "lib/lean")


@pytest.mark.parametrize("roots,flags", [(["Main"], [False]), ([], [])])
def test_selective_and_rootless_globs_are_explicitly_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roots: list[str], flags: list[bool]
) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
    (tmp_path / "lakefile.toml").write_text('name = "paper"\n[[lean_lib]]\nname = "Main"\n')
    monkeypatch.setattr(lean_layout, "compiler_executable", lambda *args: "/pinned/lean")

    def inspect(**kwargs: object) -> dict:
        args = kwargs["argv"]
        assert isinstance(args, list)
        Path(args[-1]).write_text(
            json.dumps(
                {
                    "build_dir": ".lake/build",
                    "lean_lib_dir": ".lake/build/lib/lean",
                    "libraries": [
                        {
                            "name": "Main",
                            "src_dir": ".",
                            "roots": roots,
                            "roots_buildable": flags,
                            "lean_args": [],
                        }
                    ],
                }
            )
        )
        return {"success": True}

    monkeypatch.setattr(lean_layout, "isolated_command", inspect)
    result = lean_layout.inspect_layout(tmp_path)
    assert not result["accepted"]
    assert "Selective or rootless library globs" in result["error"]
