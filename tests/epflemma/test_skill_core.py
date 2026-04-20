from __future__ import annotations

from epflemma_cli.skill_core import discover_skill_commands, discover_skills, load_skill


def test_discover_skills_prefers_project_then_user_then_builtin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("EPFLEMMA_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    user_dir = home / "skills" / "lean-proof-loop"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text(
        "---\nname: lean-proof-loop\ndescription: User override\n---\n# User\n",
        encoding="utf-8",
    )

    project_dir = tmp_path / ".epflemma" / "skills" / "lean-proof-loop"
    project_dir.mkdir(parents=True)
    (project_dir / "SKILL.md").write_text(
        "---\nname: lean-proof-loop\ndescription: Project override\n---\n# Project\n",
        encoding="utf-8",
    )

    distinct_user = home / "skills" / "custom-provider-overlay"
    distinct_user.mkdir(parents=True)
    (distinct_user / "SKILL.md").write_text(
        "---\nname: custom-provider-overlay\ndescription: Extra user overlay\n---\n",
        encoding="utf-8",
    )

    skills = {skill.name: skill for skill in discover_skills(tmp_path)}

    assert skills["lean-proof-loop"].source == "project"
    assert skills["custom-provider-overlay"].source == "user"
    assert skills["lean-diagnostics"].source == "builtin"


def test_load_skill_and_commands_use_curated_overlay_model(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("EPFLEMMA_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    project_dir = tmp_path / ".epflemma" / "skills" / "lean-diagnostics"
    project_dir.mkdir(parents=True)
    (project_dir / "SKILL.md").write_text(
        "---\nname: lean-diagnostics\ndescription: Project diagnostics helper\n---\n# Diagnostics\n",
        encoding="utf-8",
    )

    payload = load_skill("lean-diagnostics", tmp_path)
    commands = discover_skill_commands(tmp_path)

    assert payload is not None
    assert payload["source"] == "project"
    assert "Project diagnostics helper" in payload["content"]
    assert "/lean-diagnostics" in commands
    assert commands["/lean-diagnostics"]["source"] == "project"
