"""Verify that non-editable installs retain LeanFlow's runtime resources."""

import tomllib
from pathlib import Path, PurePosixPath

import pytest


@pytest.mark.parametrize("package", ["leanflow_skills", "leanflow_specs"])
def test_wheel_configuration_includes_all_runtime_documents(package: str) -> None:
    """Require package discovery and data rules for every bundled runtime document."""
    repo_root = Path(__file__).resolve().parents[2]
    package_root = repo_root / package
    documents = {path.relative_to(package_root).as_posix() for path in package_root.rglob("*.md")}
    assert documents
    assert (package_root / "__init__.py").is_file()

    with (repo_root / "pyproject.toml").open("rb") as stream:
        setuptools_config = tomllib.load(stream)["tool"]["setuptools"]

    assert package in setuptools_config["packages"]["find"]["include"]
    patterns = setuptools_config["package-data"][package]
    missing = {
        document
        for document in documents
        if not any(PurePosixPath(document).match(pattern) for pattern in patterns)
    }

    assert not missing
