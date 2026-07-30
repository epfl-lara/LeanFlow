"""Verify that non-editable installs retain LeanFlow's runtime resources."""

import tomllib
from pathlib import Path, PurePosixPath


def test_wheel_configuration_includes_all_builtin_skills():
    """Require package discovery and data rules for every curated skill document."""
    repo_root = Path(__file__).resolve().parents[2]
    skill_root = repo_root / "leanflow_skills"
    skill_documents = {
        path.relative_to(skill_root).as_posix()
        for path in (repo_root / "leanflow_skills").glob("*/SKILL.md")
    }
    assert skill_documents
    assert (skill_root / "__init__.py").is_file()

    with (repo_root / "pyproject.toml").open("rb") as stream:
        setuptools_config = tomllib.load(stream)["tool"]["setuptools"]

    assert "leanflow_skills" in setuptools_config["packages"]["find"]["include"]
    patterns = setuptools_config["package-data"]["leanflow_skills"]
    missing = {
        document
        for document in skill_documents
        if not any(PurePosixPath(document).match(pattern) for pattern in patterns)
    }

    assert not missing
