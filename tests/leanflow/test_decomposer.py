"""Phase 4 (3/6) tests: the mechanical decomposer — guards, placement, graph."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.decomposer import (
    DecomposeOutcome,
    place_helpers,
    refresh_queue_edit_guard,
    run_decomposer,
    sorry_offloading_suspect,
    stub_shape_ok,
)

# Genuinely easier than PARENT — the anti-offloading guard must let it pass.
GOOD_STUB = "lemma abs_step (a : ℝ) : 0 ≤ |a| := by sorry"
PARENT = "theorem demo (a b : ℝ) : |a| - |b| ≤ |a - b| := by\n  sorry"


def _file(tmp_path, body: str = ""):
    active = tmp_path / "Demo.lean"
    active.write_text(
        (body or "theorem other : True := by\n  trivial\n\n" + PARENT + "\n"),
        encoding="utf-8",
    )
    return active


def _ok_check(monkeypatch):
    calls: list[str] = []

    def fake(**kwargs):
        calls.append(kwargs.get("theorem_id", ""))
        return {"success": True, "has_errors": False, "has_sorry": True}

    monkeypatch.setattr("leanflow_cli.lean.lean_incremental.lean_incremental_check", fake)
    return calls


class TestGuards:
    def test_stub_shape_accepts_only_sorry_stubs(self):
        assert stub_shape_ok(GOOD_STUB)
        assert stub_shape_ok("private theorem t : True := by sorry")
        assert stub_shape_ok("@[simp] lemma s : 1 = 1 := by sorry")
        # Anything beyond a single sorry-bodied theorem/lemma is rejected.
        assert not stub_shape_ok("def f : Nat := 0")
        assert not stub_shape_ok("axiom evil : False")
        assert not stub_shape_ok("lemma t : True := by trivial")
        assert not stub_shape_ok(GOOD_STUB + "\naxiom evil : False")

    def test_stub_shape_rejects_multi_declaration_smuggling(self):
        # A lone regex can anchor on the FINAL ':= by sorry' across smuggled
        # declarations; the declaration count must kill these.
        assert not stub_shape_ok("lemma a : True := by sorry\n\nlemma b : False := by sorry")
        assert not stub_shape_ok(
            "lemma a : True := by sorry\n\naxiom evil : False\n\nlemma b : True := by sorry"
        )
        assert not stub_shape_ok("lemma a : True := by sorry; axiom evil : False")
        # Same-line smuggling — Lean accepts adjacent declarations on one line.
        assert not stub_shape_ok("theorem a : True := by sorry theorem b : True := by sorry")
        # A keyword inside a comment must NOT trip the declaration count
        # (leading comment LINES are rejected by the strict structural shape,
        # so the inline form is the probe here).
        assert stub_shape_ok("lemma c /- helper for theorem demo -/ : True := by sorry")

    def test_offloading_suspect_flags_parent_restatement(self):
        restated = "lemma demo_helper (a b : ℝ) : |a| - |b| ≤ |a - b| := by sorry"
        assert sorry_offloading_suspect(PARENT, restated) is True
        easier = "lemma abs_nonneg_step (a : ℝ) : 0 ≤ |a| := by sorry"
        assert sorry_offloading_suspect(PARENT, easier) is False
        # The queue-slice display header must not defeat the similarity check.
        prefixed = "Assigned declaration slice (7-9):\n" + PARENT
        assert sorry_offloading_suspect(prefixed, restated) is True

    def test_guard_refresh_resets_agent_caches(self):
        class _Agent:
            _managed_queue_edit_guard_state = {"demo": "stale"}
            _managed_initial_declaration_keys_by_file = {"f": ["stale"]}

        agent = _Agent()
        refresh_queue_edit_guard(agent)
        assert agent._managed_queue_edit_guard_state == {}
        assert agent._managed_initial_declaration_keys_by_file == {}
        refresh_queue_edit_guard(None)  # tolerated


class TestPlacement:
    def test_places_stubs_before_target_and_validates(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        calls = _ok_check(monkeypatch)

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        assert outcome.placed == ("abs_step",)
        content = active.read_text(encoding="utf-8")
        assert content.index("abs_step") < content.index("theorem demo")
        assert calls == ["abs_step"]

    def test_validation_error_reverts_the_whole_write(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        before = active.read_text(encoding="utf-8")
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **kwargs: {"success": True, "has_errors": True},
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "reverted" in outcome.reason
        assert active.read_text(encoding="utf-8") == before

    def test_shape_violation_rejected_before_any_write(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        before = active.read_text(encoding="utf-8")

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=["axiom evil : False"],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "stub-shape" in outcome.reason
        assert active.read_text(encoding="utf-8") == before

    def test_insertion_stays_above_doc_and_attribute_block(self, monkeypatch, tmp_path):
        active = tmp_path / "Demo.lean"
        active.write_text(
            "theorem other : True := by\n  trivial\n\n"
            "/-- The main demo statement. -/\n"
            "@[simp]\n" + PARENT + "\n",
            encoding="utf-8",
        )
        _ok_check(monkeypatch)

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        content = active.read_text(encoding="utf-8")
        # The stub lands ABOVE the doc comment, which stays glued to its
        # declaration together with the attribute.
        assert content.index("abs_step") < content.index("/-- The main demo")
        assert "/-- The main demo statement. -/\n@[simp]\ntheorem demo" in content

    def test_missing_target_is_an_error(self, tmp_path):
        active = _file(tmp_path)
        outcome = place_helpers(
            active_file=str(active),
            target_symbol="nonexistent",
            skeletons=[GOOD_STUB],
            allowed_axioms=(),
        )
        assert not outcome.ok


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _backend(monkeypatch, helpers: list[dict[str, Any]], success: bool = True):
    import json as _json

    def fake(theorem_id, file_path, **kwargs):
        return _json.dumps({"success": success, "helpers": helpers})

    monkeypatch.setattr("tools.implementations.lean_experts.lean_decompose_helpers_tool", fake)


class TestRunDecomposer:
    def test_full_pipeline_places_guards_and_records_graph(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        _ok_check(monkeypatch)
        _backend(
            monkeypatch,
            helpers=[
                {
                    "name": "abs_step",
                    "lean_skeleton": GOOD_STUB,
                    "ready_to_insert": True,
                    "validation_order": 1,
                },
                {
                    "name": "not_ready",
                    "lean_skeleton": "lemma nr : True := by sorry",
                    "ready_to_insert": False,
                    "validation_order": 2,
                },
                {
                    "name": "demo_restated",
                    "lean_skeleton": (
                        "lemma demo_restated (a b : ℝ) : |a| - |b| ≤ |a - b| := by sorry"
                    ),
                    "ready_to_insert": True,
                    "validation_order": 3,
                },
            ],
        )

        class _Agent:
            _managed_queue_edit_guard_state = {"stale": True}
            _managed_initial_declaration_keys_by_file = {"stale": True}

        agent = _Agent()
        outcome = run_decomposer(
            target_symbol="demo",
            active_file=str(active),
            statement=PARENT,
            agent=agent,
        )

        assert outcome.ok
        assert outcome.placed == ("abs_step",)
        assert "not_ready" in outcome.skipped
        assert "demo_restated" in outcome.skipped  # anti-sorry-offloading
        # Graph: stated helper node + both edges.
        bp = plan_state.load_blueprint()
        helper_id = plan_state.node_id_for("abs_step", str(active))
        target_id = plan_state.node_id_for("demo", str(active))
        assert bp.node_by_id(helper_id).status == "stated"
        assert bp.node_by_id(helper_id).generated_by == "decomposer"
        kinds = {(e.source, e.target, e.kind) for e in bp.edges}
        assert (helper_id, target_id, "split_of") in kinds
        assert (target_id, helper_id, "depends_on") in kinds
        # Guard caches refreshed so the prover will not restore the stubs.
        assert agent._managed_queue_edit_guard_state == {}
        assert agent._managed_initial_declaration_keys_by_file == {}

    def test_backend_failure_is_a_clean_fallback(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        _backend(monkeypatch, helpers=[], success=False)

        outcome = run_decomposer(target_symbol="demo", active_file=str(active))

        assert not outcome.ok
        assert isinstance(outcome, DecomposeOutcome)

    def test_no_guarded_helpers_reports_reason(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        _backend(
            monkeypatch,
            helpers=[{"name": "bad", "lean_skeleton": "def f : Nat := 0", "ready_to_insert": True}],
        )

        outcome = run_decomposer(target_symbol="demo", active_file=str(active))

        assert not outcome.ok
        assert "no ready, guarded helpers" in outcome.reason
        assert "bad" in outcome.skipped
