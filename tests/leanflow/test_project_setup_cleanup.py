"""Require exact start-identity and group closure before deleting private preparation assets."""

import pytest

from agent.runtime.harness.processes import ProcessTree
from leanflow_cli.workflows.project_setup import cleanup


def test_separate_owned_group_surviving_leader_is_not_cleanup(monkeypatch):
    tree = ProcessTree(10, {12: "owned start"})
    monkeypatch.setattr(cleanup, "process_states", lambda: {12: (12, "S", "owned start")})
    assert cleanup.verify_owned_stopped(tree, group=10, timeout_s=0) is False


def test_reused_pid_is_unrelated_and_zombies_have_closed_descriptors(monkeypatch):
    tree = ProcessTree(10, {12: "owned start", 13: "exited start"})
    monkeypatch.setattr(
        cleanup,
        "process_states",
        lambda: {12: (12, "S", "new start"), 13: (10, "Z", "exited start")},
    )
    assert cleanup.verify_owned_stopped(tree, group=10, timeout_s=0) is True


def test_unknown_live_invocation_group_member_prevents_cleanup(monkeypatch):
    monkeypatch.setattr(cleanup, "process_states", lambda: {19: (10, "S", "missed start")})
    assert cleanup.verify_owned_stopped(ProcessTree(10), group=10, timeout_s=0) is False


def test_failed_identity_inspection_never_establishes_cleanup(monkeypatch):
    def fail():
        raise ValueError("unreadable process table")

    monkeypatch.setattr(cleanup, "process_states", fail)
    with pytest.raises(ValueError, match="unreadable"):
        cleanup.verify_owned_stopped(ProcessTree(10), timeout_s=0)
