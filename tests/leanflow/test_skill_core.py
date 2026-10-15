"""Exercise curated skill overlays and supported workflow defaults."""

from __future__ import annotations

import pytest

from leanflow_cli.runtime.skill_core import (
    CURATED_BUILTIN_SKILLS,
    default_workflow_skill,
    discover_skill_commands,
    discover_skills,
    find_skill,
    load_skill,
)


def test_discover_skills_prefers_project_then_user_then_builtin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    user_dir = home / "skills" / "lean-proof-loop"
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text(
        "---\nname: lean-proof-loop\ndescription: User override\n---\n# User\n",
        encoding="utf-8",
    )

    project_dir = tmp_path / ".leanflow" / "skills" / "lean-proof-loop"
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
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    project_dir = tmp_path / ".leanflow" / "skills" / "lean-diagnostics"
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


def test_load_skill_returns_none_for_unknown_skill(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    result = load_skill("no-such-skill-xyz", tmp_path)

    assert result is None


def test_find_skill_resolves_slash_prefixed_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    record = find_skill("/lean-proof-loop", tmp_path)

    assert record is not None
    assert record.name == "lean-proof-loop"


def test_find_skill_normalizes_underscores_to_hyphens(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    record = find_skill("lean_proof_loop", tmp_path)

    assert record is not None
    assert record.name == "lean-proof-loop"


def test_find_skill_returns_none_for_unknown_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    result = find_skill("no-such-skill", tmp_path)

    assert result is None


def test_skill_without_frontmatter_falls_back_to_directory_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    skill_dir = tmp_path / ".leanflow" / "skills" / "my-raw-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("No frontmatter here, just plain text\n", encoding="utf-8")

    record = find_skill("my-raw-skill", tmp_path)

    assert record is not None
    assert record.name == "my-raw-skill"
    assert record.source == "project"


def test_load_skill_accepts_direct_skill_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    skill = tmp_path / "external" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: external-skill\ndescription: External\n---\n# External\n", encoding="utf-8"
    )

    payload = load_skill(str(skill), tmp_path)

    assert payload is not None
    assert payload["name"] == "external-skill"
    assert payload["source"] == "path"


@pytest.mark.parametrize(
    "workflow_kind,expected_skill",
    [
        ("autoprove", "lean-proof-loop"),
        ("prove", "lean-proof-loop"),
        ("autoformalize", "lean-formalization"),
        ("formalize", "lean-formalization"),
    ],
)
def test_default_workflow_skill_covers_all_workflow_kinds(workflow_kind, expected_skill):
    assert default_workflow_skill(workflow_kind) == expected_skill


def test_default_workflow_skill_is_case_insensitive():
    assert default_workflow_skill("AUTOPROVE") == "lean-proof-loop"
    assert default_workflow_skill("AutoFormalize") == "lean-formalization"


def test_builtin_search_skill_is_loadable(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    # The merged lean-search skill must surface both former roles: local-project
    # context and Mathlib/semantic discovery.
    payload = load_skill("lean-search", tmp_path)

    assert payload is not None
    prompt = payload["content"]
    assert "Mathlib" in prompt
    assert "local Lean project" in prompt


def test_theorem_queue_worker_skill_is_loadable(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    payload = load_skill("lean-theorem-queue-worker", tmp_path)

    assert payload is not None
    prompt = payload["content"]
    assert "external workflow manager" in prompt
    assert "Focus only on solving the assigned declaration" in prompt
    assert "adding and iterating on new helper declarations" in prompt
    assert "lean_decompose_helpers" in prompt
    assert "sublemma/invariant split" in prompt
    assert "Plan-State Freshness" in prompt
    assert "read-only generated sections" in prompt
    assert "Do not edit or paginate that file" in prompt
    assert "user-owned historical Notes tail" in prompt
    assert "Structured planner state is persisted" in prompt
    assert "append below" not in prompt
    assert "append planning findings" not in prompt
    assert "current queue assignment, Lean source, or kernel diagnostics" in prompt
    assert "Do not read raw `summary.json` or `blueprint.json`" in prompt
    normalized = " ".join(prompt.lower().split())
    assert "blocker is never permission to end an unresolved theorem" in normalized
    assert "failed proof body and diagnostics in workflow state" in normalized
    assert "failed declarations are not copied into the source as comments" in normalized
    assert "comments the current failed declaration above the theorem" not in normalized
    assert "stopping with failure" not in normalized


def test_proof_loop_skill_mentions_helper_decomposition(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    payload = load_skill("lean-proof-loop", tmp_path)

    assert payload is not None
    assert "lean_decompose_helpers" in payload["content"]
    assert "checked sublemma plan" in payload["content"]


def test_all_curated_builtin_skills_are_discoverable(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    discovered = {record.name for record in discover_skills(tmp_path) if record.source == "builtin"}

    for skill_name in CURATED_BUILTIN_SKILLS:
        assert skill_name in discovered, f"Curated builtin skill {skill_name!r} not discoverable"


def test_builtin_skill_names_use_current_leanflow_brand(monkeypatch, tmp_path):
    """Keep the retired EPFLemma name out of the active skill surface."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    discovered = {
        record.name.lower() for record in discover_skills(tmp_path) if record.source == "builtin"
    }

    assert all("epflemma" not in skill_name for skill_name in CURATED_BUILTIN_SKILLS)
    assert all("epflemma" not in skill_name for skill_name in discovered)


def test_skill_with_linked_files_reports_them(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    skill_dir = tmp_path / ".leanflow" / "skills" / "lean-proof-loop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: lean-proof-loop\ndescription: With refs\n---\n# Body\n",
        encoding="utf-8",
    )
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "tactics.md").write_text("# Tactics\n", encoding="utf-8")

    payload = load_skill("lean-proof-loop", tmp_path)

    assert payload is not None
    assert payload["linked_files"] is not None
    assert any("tactics.md" in f for f in payload["linked_files"].get("references", []))


@pytest.mark.parametrize("kind", ["draft", "review", "refactor", "golf", "unknown", ""])
def test_retired_workflows_have_no_default_skill(kind):
    with pytest.raises(ValueError, match="prove and formalize"):
        default_workflow_skill(kind)


def test_retired_builtin_workflow_skills_are_not_discoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    names = {item.name for item in discover_skills(tmp_path) if item.source == "builtin"}
    assert not names & {"lean-statement-draft", "lean-refactor-golf", "lean-autonomous-swarm"}
