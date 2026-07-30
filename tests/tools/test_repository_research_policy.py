"""Tests for the opt-in clean-room repository-research boundary."""

from __future__ import annotations

import json

import pytest

from tools.implementations import file_tools
from tools.utilities import repository_research_policy as policy


@pytest.mark.parametrize(
    "value, expected",
    [("", False), ("0", False), ("false", False), ("1", True), ("YES", True), ("on", True)],
)
def test_repository_research_flag(monkeypatch, value, expected):
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, value)
    assert policy.repository_research_disabled() is expected


def test_solution_research_blocks_active_task_labels(monkeypatch):
    monkeypatch.setenv(policy.DISABLE_SOLUTION_RESEARCH_ENV, "1")
    monkeypatch.setenv(
        policy.CLEAN_ROOM_TASK_LABELS_ENV,
        "IMO 2026 Problem 6|IMO2026 P6",
    )

    assert policy.solution_research_query_block_reason(
        "IMO 2026 problem 6 sequence periodic solution"
    )
    assert policy.solution_research_query_block_reason("proof of IMO2026/P6")
    assert policy.solution_research_url_block_reason(
        "https://example.org/imo-2026-problem-6-solution"
    )
    assert policy.solution_research_command_block_reason(
        "curl 'https://search.example/?q=IMO2026+P6'"
    )
    assert (
        policy.solution_research_query_block_reason("maximal intersecting hypergraph finite kernel")
        == ""
    )


@pytest.mark.parametrize(
    "command",
    [
        "lake env lean IMO2026/P6Scratch.lean",
        "rg 'result' /workspace/IMO2026/P6.lean",
    ],
)
def test_solution_research_allows_local_task_commands(monkeypatch, command):
    monkeypatch.setenv(policy.DISABLE_SOLUTION_RESEARCH_ENV, "1")
    monkeypatch.setenv(
        policy.CLEAN_ROOM_TASK_LABELS_ENV,
        "IMO 2026 Problem 6|IMO2026 P6",
    )

    assert policy.solution_research_command_block_reason(command) == ""


def test_solution_research_blocks_scripted_network_task_lookup(monkeypatch):
    monkeypatch.setenv(policy.DISABLE_SOLUTION_RESEARCH_ENV, "1")
    monkeypatch.setenv(policy.CLEAN_ROOM_TASK_LABELS_ENV, "IMO2026 P6")

    command = (
        "python3 -c 'import requests; " 'requests.get("https://search.example/?q=IMO2026+P6")\''
    )
    assert policy.solution_research_command_block_reason(command)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/repo",
        "https://raw.githubusercontent.com/example/repo/main/Demo.lean",
        "https://sourcegraph.com/search?q=Demo",
        "https://example.github.io/formalization/",
    ],
)
def test_repository_hosts_are_recognized(url):
    assert policy.is_repository_url(url) is True


def test_non_repository_math_source_is_allowed():
    assert policy.is_repository_url("https://artofproblemsolving.com/wiki/example") is False


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "/usr/bin/git clone https://example.com/repo",
        "curl https://github.com/example/repo",
        "lake update",
        "lake update repl",
        "leanflow project init",
    ],
)
def test_clean_room_blocks_git_and_repository_commands(monkeypatch, command):
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    assert policy.repository_command_block_reason(command)


def test_clean_room_allows_lean_commands(monkeypatch):
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    assert policy.repository_command_block_reason("lake env lean IMO2026/P6.lean") == ""


def test_clean_room_confines_paths_to_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    assert policy.clean_room_path_block_reason("IMO2026/P6.lean") == ""
    assert policy.clean_room_path_block_reason(project / "Scratch.lean") == ""
    assert policy.clean_room_path_block_reason("../formalization/IMO2026/P6.lean")


def test_clean_room_blocks_symlink_escape(monkeypatch, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    assert policy.clean_room_path_block_reason("escape/P6.lean")


@pytest.mark.parametrize(
    "invoke",
    [
        lambda escaped: file_tools.read_file_tool(escaped),
        lambda escaped: file_tools.search_tool("result", path=escaped),
        lambda escaped: file_tools.write_file_tool(escaped, "discarded"),
        lambda escaped: file_tools.patch_tool(
            mode="replace",
            path=escaped,
            old_string="sorry",
            new_string="by exact trivial",
        ),
    ],
)
def test_clean_room_file_tools_reject_out_of_project_paths(monkeypatch, tmp_path, invoke):
    project = tmp_path / "project"
    outside = tmp_path / "formalization" / "IMO2026" / "P6.lean"
    project.mkdir()
    outside.parent.mkdir(parents=True)
    outside.write_text("theorem result : True := by trivial\n", encoding="utf-8")
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda _task_id: pytest.fail("escaped path reached the file backend"),
    )

    payload = json.loads(invoke(str(outside)))

    assert payload["status"] == "clean_room_path_denied"
    assert payload["path"] == str(outside)


def test_clean_room_v4a_patch_rejects_escaped_destination(monkeypatch, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.setenv(policy.DISABLE_REPOSITORY_RESEARCH_ENV, "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda _task_id: pytest.fail("escaped patch reached the file backend"),
    )
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {outside / 'Leaked.lean'}\n"
        "+def leaked := true\n"
        "*** End Patch\n"
    )

    payload = json.loads(file_tools.patch_tool(mode="patch", patch=patch))

    assert payload["status"] == "clean_room_path_denied"
