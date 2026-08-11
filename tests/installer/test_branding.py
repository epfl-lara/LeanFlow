"""Keep installer-created command names on the current LeanFlow brand."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_internal_installer_removes_retired_epflemma_wrappers() -> None:
    """Require upgrades to remove every retired EPFLemma wrapper."""
    repo_root = Path(__file__).resolve().parents[2]
    installer = (repo_root / "scripts" / "install-internal.sh").read_text(encoding="utf-8")

    for wrapper in ("epflemma", "epflemma-prove", "epflemma-formalize"):
        assert f'"$LEANFLOW_BIN_DIR/{wrapper}"' in installer


def test_active_commands_and_bundled_skill_names_exclude_retired_epflemma_brand() -> None:
    """Prevent the retired command or skill brand from re-entering active surfaces."""
    repo_root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    scripts = project["project"]["scripts"]
    assert set(scripts) == {"leanflow", "leanflow-agent"}
    assert all("epflemma" not in name.casefold() for name in scripts)

    skill_names = []
    for skill_file in sorted((repo_root / "leanflow_skills").glob("*/SKILL.md")):
        frontmatter = skill_file.read_text(encoding="utf-8").split("---", 2)[1]
        name_line = next(
            line for line in frontmatter.splitlines() if line.strip().startswith("name:")
        )
        skill_names.append(name_line.split(":", 1)[1].strip())
    assert skill_names
    assert all("epflemma" not in name.casefold() for name in skill_names)
