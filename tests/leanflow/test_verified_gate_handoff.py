"""Tests for revision-authenticated queue-boundary state reuse."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.native import source_only_startup, verified_gate_handoff


def _state(path: str) -> dict:
    revision = source_only_startup.capture_source_revision(path)
    assert revision is not None
    return {
        "active_file": path,
        "source_revision": revision.to_mapping(),
        "source_revision_sha256": revision.sha256,
        "verification_ok": True,
    }


def test_take_consumes_current_revision_once(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    agent = SimpleNamespace()
    state = _state(str(source))

    assert verified_gate_handoff.remember(agent, state)
    assert verified_gate_handoff.take(agent) == state
    assert verified_gate_handoff.take(agent) == {}


def test_take_rejects_state_after_source_change(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    agent = SimpleNamespace()
    state = _state(str(source))

    assert verified_gate_handoff.remember(agent, state)
    source.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert verified_gate_handoff.take(agent) == {}


def test_mapping_handoff_consumes_current_revision_once(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    owner: dict = {}
    state = _state(str(source))

    assert verified_gate_handoff.remember_mapping(owner, state)
    assert verified_gate_handoff.take_mapping(owner) == state
    assert verified_gate_handoff.take_mapping(owner) == {}


def test_mapping_handoff_rejects_stale_revision(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    owner: dict = {}
    state = _state(str(source))

    assert verified_gate_handoff.remember_mapping(owner, state)
    source.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert verified_gate_handoff.take_mapping(owner) == {}
