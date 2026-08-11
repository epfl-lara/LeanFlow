"""Tests for agent/command_safety.py and its re-export from run_agent (Phase 4 extraction)."""

import run_agent
from agent.execution import command_safety


def test_run_agent_reexports_match_module():
    # Backwards-compat: run_agent.* must be the same objects as the new module's.
    for name in command_safety.__all__:
        assert getattr(run_agent, name) is getattr(command_safety, name), name


def test_is_destructive_command_flags_file_mutators():
    assert command_safety._is_destructive_command("rm -rf /tmp/x") is True
    assert command_safety._is_destructive_command("mv a b") is True
    assert command_safety._is_destructive_command("sed -i 's/a/b/' f") is True
    assert command_safety._is_destructive_command("git reset --hard") is True
    assert command_safety._is_destructive_command("git restore -- Main.lean") is True
    assert command_safety._is_destructive_command("git switch other-branch") is True
    # Chained after && / ; / || is still detected.
    assert command_safety._is_destructive_command("echo hi && rm file") is True


def test_is_destructive_command_flags_overwrite_redirect_not_append():
    # Single > overwrites; >> appends and must not be flagged on that alone.
    assert command_safety._is_destructive_command("echo x > out.txt") is True
    assert command_safety._is_destructive_command("echo x >> out.txt") is False


def test_is_destructive_command_allows_read_only_and_empty():
    assert command_safety._is_destructive_command("") is False
    assert command_safety._is_destructive_command("ls -la") is False
    assert command_safety._is_destructive_command("cat file.txt") is False
    assert command_safety._is_destructive_command("grep foo bar.py") is False


def test_reexported_callable_matches_behavior():
    # The name reachable via run_agent behaves identically.
    assert run_agent._is_destructive_command("rm file") is True
    assert run_agent._is_destructive_command("ls") is False
