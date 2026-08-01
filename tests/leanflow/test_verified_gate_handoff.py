"""Tests for revision-authenticated queue-boundary state reuse."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.native import native_runner as runner
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


def test_final_exact_gate_builds_drained_state_without_lean(tmp_path, monkeypatch):
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(runner, "_document_formalization_requested", lambda: False)
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_last_verification_record",
        lambda *args, **kwargs: {
            "ok": True,
            "scope": "target:demo",
            "tool": "lean_incremental_check",
        },
    )
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {}),
    )

    state = runner._build_verified_gate_handoff_state(
        str(source),
        "demo",
        {
            "ok": True,
            "target": "demo",
            "axiom_profile_checked": True,
            "axiom_profile_blockers": [],
            "output": "target:demo passed",
        },
        {},
    )

    assert state["verification_ok"] is True
    assert state["proof_solved"] is True
    assert state["declaration_queue_total"] == 0
    assert state["queue_needs_final_file_sweep"] is False
    assert state["proof_state_authority"] == "authenticated_target_gate"
    assert runner._live_state_is_verified(state)
