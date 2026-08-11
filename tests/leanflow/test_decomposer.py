"""Phase 4 (3/6) tests: the mechanical decomposer — guards, placement, graph."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows import campaign_epoch, decomposition_provenance, plan_state
from leanflow_cli.workflows.decomposer import (
    DecomposeOutcome,
    _target_proof_dependency_names,
    backfill_known_prover_helpers,
    editable_dependency_helper_names,
    migrate_legacy_prover_helper_edges,
    place_helpers,
    prover_edit_evidence_helper_names,
    record_prover_helpers_from_edit,
    refresh_queue_edit_guard,
    rollback_decomposition_graph,
    run_decomposer,
    sorry_offloading_suspect,
    stub_shape_ok,
    unsupported_novel_bound_suspect,
)
from leanflow_cli.workflows.workflow_json_io import update_json_file

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

    def test_unsupported_novel_bound_rejects_guessed_threshold(self):
        parent = (
            "theorem eventual (a : ℕ) : ∀ᶠ n in Filter.atTop, " "∃ x : ℕ, a / n = 1 / x := by sorry"
        )
        guessed = "lemma guessed (a n : ℕ) (hn : n ≥ a * 6) : " "∃ x : ℕ, a / n = 1 / x := by sorry"
        assert unsupported_novel_bound_suspect(parent, guessed)

        inherited = "lemma inherited (a n : ℕ) (hn : n ≥ 6) : n ≥ 6 := by sorry"
        parent_with_bound = "theorem bounded (n : ℕ) (hn : n ≥ 6) : n ≥ 1 := by sorry"
        assert not unsupported_novel_bound_suspect(parent_with_bound, inherited)

        generic = "lemma generic (a n : ℕ) : ∃ x : ℕ, a / n = 1 / x := by sorry"
        assert not unsupported_novel_bound_suspect(parent, generic)

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

    def test_exact_existing_helper_is_reused_without_duplicate_insertion(
        self, monkeypatch, tmp_path
    ):
        active = _file(tmp_path, f"{GOOD_STUB}\n\n{PARENT}\n")
        before = active.read_text(encoding="utf-8")
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: pytest.fail("existing helper was redundantly revalidated"),
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        assert outcome.placed == ("abs_step",)
        assert outcome.skipped == ("abs_step",)
        assert "reinsertion skipped" in outcome.reason
        assert active.read_text(encoding="utf-8") == before
        assert active.read_text(encoding="utf-8").count("lemma abs_step") == 1

    def test_existing_helper_name_with_different_statement_fails_closed(
        self, monkeypatch, tmp_path
    ):
        active = _file(
            tmp_path,
            "lemma abs_step (a : ℝ) : |a| = |a| := by sorry\n\n" + PARENT + "\n",
        )
        before = active.read_text(encoding="utf-8")

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "different statement" in outcome.reason
        assert active.read_text(encoding="utf-8") == before

    def test_mixed_existing_and_new_helpers_validates_only_new_tail(self, monkeypatch, tmp_path):
        active = _file(tmp_path, f"{GOOD_STUB}\n\n{PARENT}\n")
        new_stub = "private lemma second_step : True := by sorry"
        calls: list[str] = []

        def check_tail(**kwargs):
            calls.append(kwargs["theorem_id"])
            content = active.read_text(encoding="utf-8")
            assert content.count("lemma abs_step") == 1
            assert content.count("lemma second_step") == 1
            return {"success": True, "has_errors": False, "has_sorry": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            check_tail,
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB, new_stub],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        assert outcome.placed == ("abs_step", "second_step")
        assert outcome.skipped == ("abs_step",)
        assert calls == ["second_step"]

    def test_batch_validation_checks_only_the_tail_after_every_stub_is_written(
        self, monkeypatch, tmp_path
    ):
        active = _file(tmp_path)
        names = (
            "erdos_242_residual_mod_seven_eq_one_k_mod_455_eq_1",
            "erdos_242_residual_mod_seven_eq_one_k_mod_455_eq_106",
            "erdos_242_residual_mod_seven_eq_one_k_mod_455_eq_421",
        )
        stubs = [f"private lemma {name} : True := by sorry" for name in names]
        calls: list[str] = []

        def check_tail(**kwargs):
            calls.append(kwargs["theorem_id"])
            content = active.read_text(encoding="utf-8")
            assert all(stub in content for stub in stubs)
            assert [content.index(stub) for stub in stubs] == sorted(
                content.index(stub) for stub in stubs
            )
            return {"success": True, "has_errors": False, "has_sorry": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            check_tail,
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=stubs,
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        assert outcome.placed == names
        assert calls == [names[-1]]

    def test_batch_prefix_environment_failure_uses_canonical_source_fallback(
        self, monkeypatch, tmp_path
    ):
        active = _file(tmp_path)
        stubs = [
            "private lemma prefix_step : True := by sorry",
            "private lemma tail_step : True := by sorry",
        ]
        exact_sources: list[str] = []

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {
                "success": False,
                "has_errors": True,
                "error_code": "prior_decl_failed",
                "error": (
                    "failed to build env before target at Move.half: "
                    "line 66:0 error: unexpected end of input"
                ),
            },
        )

        def exact_source_check(source, **_kwargs):
            exact_sources.append(source)
            return {
                "success": True,
                "ok": True,
                "output": "warning: declaration uses `sorry`",
            }

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_ephemeral.lean_ephemeral_source_check",
            exact_source_check,
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=stubs,
            allowed_axioms=("propext",),
            cwd=str(tmp_path),
        )

        assert outcome.ok
        assert outcome.placed == ("prefix_step", "tail_step")
        assert len(exact_sources) == 1
        assert exact_sources[0] == active.read_text(encoding="utf-8")

    def test_batch_tail_failure_reports_the_prior_declaration_and_reverts(
        self, monkeypatch, tmp_path
    ):
        active = _file(tmp_path)
        before = active.read_bytes()
        first = "residue_k_mod_455_eq_1"
        tail = "residue_k_mod_455_eq_106"

        def fail_on_prior(**kwargs):
            assert kwargs["theorem_id"] == tail
            return {
                "success": False,
                "has_errors": True,
                "error_code": "prior_declaration_failed",
                "error": f"failed to build env before target at {first}: type mismatch",
            }

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            fail_on_prior,
        )
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_ephemeral.lean_ephemeral_source_check",
            lambda *_args, **_kwargs: {
                "success": False,
                "ok": False,
                "error": "type mismatch in inserted helper batch",
            },
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[
                f"private lemma {first} : True := by sorry",
                f"private lemma {tail} : True := by sorry",
            ],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "type mismatch" in outcome.reason
        assert "write reverted" in outcome.reason
        assert active.read_bytes() == before

    def test_interrupt_during_validation_reverts_exact_inserted_revision(
        self, monkeypatch, tmp_path
    ):
        from tools.utilities.interrupt import CooperativeInterrupt, set_interrupt

        active = _file(tmp_path)
        before = active.read_bytes()

        def interrupt_after_check(**_kwargs):
            assert GOOD_STUB in active.read_text(encoding="utf-8")
            set_interrupt(True)
            return {"success": True, "has_errors": False, "has_sorry": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            interrupt_after_check,
        )
        set_interrupt(False)
        try:
            with pytest.raises(CooperativeInterrupt, match="during Lean validation"):
                place_helpers(
                    active_file=str(active),
                    target_symbol="demo",
                    skeletons=[GOOD_STUB],
                    allowed_axioms=("propext",),
                )
        finally:
            set_interrupt(False)

        assert active.read_bytes() == before

    def test_exact_decomposer_materialization_owns_forecast_planner_node(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        active_file = str(active.resolve())
        helper_id = plan_state.node_id_for("abs_step", active_file)
        plan_state.save_blueprint(
            plan_state.Blueprint(
                nodes=(
                    plan_state.GraphNode(
                        id=helper_id,
                        kind="lemma",
                        name="abs_step",
                        file=active_file,
                        statement="forecast statement",
                        status="conjectured",
                        generated_by="planner",
                        notes="planner forecast",
                    ),
                )
            )
        )
        _ok_check(monkeypatch)

        outcome = place_helpers(
            active_file=active_file,
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        helper = plan_state.load_blueprint().node_by_id(helper_id)
        assert helper is not None
        assert helper.status == "stated"
        assert helper.generated_by == "decomposer"
        assert helper.notes == "planner forecast"

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

    def test_reverted_source_pauses_when_terminal_ledger_write_fails(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        before = active.read_bytes()
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {"success": True, "has_errors": True},
        )
        original_finish = decomposition_provenance.finish_decomposition

        def fail_reverted(transaction_id, *, state, reason=""):
            if state == "reverted":
                raise OSError("ledger unavailable")
            return original_finish(transaction_id, state=state, reason=reason)

        monkeypatch.setattr(decomposition_provenance, "finish_decomposition", fail_reverted)

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert outcome.requires_pause
        assert "ledger unavailable" in outcome.reason
        assert active.read_bytes() == before
        assert plan_state.load_summary()["decomposition_provenance"][-1]["state"] == "pending"

    def test_validated_source_pauses_when_commit_ledger_write_fails(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        _ok_check(monkeypatch)
        original_finish = decomposition_provenance.finish_decomposition

        def fail_committed(transaction_id, *, state, reason=""):
            if state == "committed":
                raise OSError("ledger unavailable")
            return original_finish(transaction_id, state=state, reason=reason)

        monkeypatch.setattr(decomposition_provenance, "finish_decomposition", fail_committed)

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert outcome.requires_pause
        assert "ledger unavailable" in outcome.reason
        assert b"lemma abs_step" in active.read_bytes()
        assert plan_state.load_summary()["decomposition_provenance"][-1]["state"] == "pending"

    def test_graph_revision_conflict_reverts_source_before_commit(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        before = active.read_bytes()
        _ok_check(monkeypatch)
        monkeypatch.setattr(
            plan_state,
            "save_blueprint",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                plan_state.PlanStateRevisionConflict("raced")
            ),
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert not outcome.requires_pause
        assert "graph persistence failed" in outcome.reason
        assert active.read_bytes() == before
        provenance = plan_state.load_summary()["decomposition_provenance"][-1]
        assert provenance["state"] == "reverted"

    def test_concurrent_edit_before_insertion_is_not_overwritten(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        before = active.read_bytes()
        concurrent = before + b"\n-- concurrent edit\n"

        def mutate_before_cas(**_kwargs):
            active.write_bytes(concurrent)
            return {}

        monkeypatch.setattr(
            decomposition_provenance,
            "begin_decomposition",
            mutate_before_cas,
        )
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: pytest.fail("validation must not run after a failed source CAS"),
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "changed concurrently" in outcome.reason
        assert active.read_bytes() == concurrent

    def test_concurrent_edit_during_validation_blocks_rollback(self, monkeypatch, tmp_path):
        active = _file(tmp_path)

        def failing_check(**_kwargs):
            active.write_bytes(active.read_bytes() + b"\n-- concurrent edit survives\n")
            return {"success": True, "has_errors": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            failing_check,
        )

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert not outcome.ok
        assert "rollback was safely refused" in outcome.reason
        content = active.read_bytes()
        assert b"abs_step" in content
        assert content.endswith(b"-- concurrent edit survives\n")

    def test_crlf_source_bytes_and_provenance_hashes_are_preserved(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = tmp_path / "Demo.lean"
        before = (
            "-- café λ\r\n"
            "theorem other : True := by\r\n"
            "  trivial\r\n\r\n"
            "theorem demo (a b : ℝ) : |a| - |b| ≤ |a - b| := by\r\n"
            "  sorry\r\n"
        ).encode()
        active.write_bytes(before)
        _ok_check(monkeypatch)

        outcome = place_helpers(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        after = active.read_bytes()
        assert b"-- caf\xc3\xa9 \xce\xbb\r\n" in after
        assert b"lemma abs_step" in after
        assert b"\r\n\r\ntheorem demo" in after
        assert b"\n" not in after.replace(b"\r\n", b"")
        provenance = plan_state.load_summary()["decomposition_provenance"][-1]
        assert provenance["source_hash_kind"] == "sha256-raw-utf8-bytes"
        assert provenance["before_source_sha256"] == hashlib.sha256(before).hexdigest()
        assert provenance["after_source_sha256"] == hashlib.sha256(after).hexdigest()

    def test_pending_recovery_uses_the_same_exact_crlf_hashes(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = tmp_path / "Demo.lean"
        before = ("-- café\r\n" + PARENT.replace("\n", "\r\n") + "\r\n").encode("utf-8")
        block = (GOOD_STUB + "\r\n\r\n").encode("utf-8")
        after = block + before
        active.write_bytes(before)
        with decomposition_provenance.source_operation(active) as operation:
            record = decomposition_provenance.begin_decomposition(
                active_file=str(active),
                target_symbol="demo",
                skeletons=[GOOD_STUB],
                before_text=before.decode("utf-8"),
                after_text=after.decode("utf-8"),
                before_bytes=before,
                after_bytes=after,
                operation=operation,
            )

        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        calls = _ok_check(monkeypatch)
        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 1, "reverted": 0, "quarantined": 0}
        assert calls == ["abs_step"]
        stored = plan_state.load_summary()["decomposition_provenance"][-1]
        assert stored["transaction_id"] == record["transaction_id"]
        assert stored["state"] == "committed"
        blueprint = plan_state.load_blueprint()
        helper_id = plan_state.node_id_for("abs_step", str(active.resolve()))
        parent_id = plan_state.node_id_for("demo", str(active.resolve()))
        assert blueprint.node_by_id(helper_id).generated_by == "decomposer"
        assert any(
            edge.source == helper_id and edge.target == parent_id and edge.kind == "split_of"
            for edge in blueprint.edges
        )

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


def _legacy_negation_helper_graph(active):
    """Persist one pre-fix prover helper split for migration tests."""
    active_file = str(active.resolve())
    parent_id = plan_state.node_id_for("demo", active_file)
    helper_id = plan_state.node_id_for("terminal_conditions_consistent", active_file)
    blueprint = plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="demo",
                    file=active_file,
                    status="proving",
                    generated_by="queue-sync",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    kind="lemma",
                    name="terminal_conditions_consistent",
                    file=active_file,
                    status="proved",
                    generated_by="prover-edit",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
            ),
        )
    )
    return blueprint, active_file, parent_id, helper_id


def test_generated_dependency_revision_invalidates_prior_gate(tmp_path, plan_enabled):
    active = _file(
        tmp_path,
        body=(
            "private lemma derived_helper (j : Fin 5) : True := by\n"
            "  trivial\n\n"
            "theorem demo (j : Nat) : True := by\n"
            "  sorry\n"
        ),
    )
    active_file = str(active.resolve())
    target_id = plan_state.node_id_for("demo", active_file)
    helper_id = plan_state.node_id_for("derived_helper", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=target_id,
                    name="demo",
                    file=active_file,
                    status="proving",
                    generated_by="queue-sync",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    kind="lemma",
                    name="derived_helper",
                    file=active_file,
                    statement=(
                        "private lemma derived_helper (j : Fin 5) : True := by\n" "  trivial"
                    ),
                    source_sha256="old-gate",
                    status="proved",
                    generated_by="decomposer",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=target_id, kind="split_of"),
                plan_state.GraphEdge(source=target_id, target=helper_id, kind="depends_on"),
            ),
        )
    )
    before = active.read_text(encoding="utf-8")

    assert editable_dependency_helper_names(
        target_symbol="demo",
        active_file=active_file,
    ) == frozenset({"derived_helper"})

    active.write_text(
        before.replace("(j : Fin 5)", "(j : Nat)"),
        encoding="utf-8",
    )
    update = record_prover_helpers_from_edit(
        target_symbol="demo",
        active_file=active_file,
        before_text=before,
    )

    assert update.introduced == ()
    assert update.updated == ("derived_helper",)
    helper = plan_state.load_blueprint().node_by_id(helper_id)
    assert helper is not None
    assert "(j : Nat)" in helper.statement
    assert helper.status == "proving"
    assert helper.source_sha256 == ""
    assert helper.generated_by == "decomposer"


def _journal_legacy_helper_split(
    *,
    active_file: str,
    helper_id: str,
    route: str,
) -> None:
    """Journal the exact route and pre-fix edge event consumed by migration."""
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "route": route,
            "name": "demo",
            "file": active_file,
        }
    )
    plan_state.append_journal_event(
        {
            "event": "helper-split-recorded",
            "node_id": helper_id,
            "name": "terminal_conditions_consistent",
            "target": "demo",
            "via": "prover-edit",
        }
    )


class TestProverEditEvidenceEdgeMigration:
    def test_reclassifies_event_proven_negate_helper_split(self, tmp_path, plan_enabled):
        active = _file(
            tmp_path,
            body=(
                "lemma terminal_conditions_consistent : True := by\n"
                "  trivial\n\n"
                "theorem demo : True := by\n"
                "  sorry\n"
            ),
        )
        before, active_file, parent_id, helper_id = _legacy_negation_helper_graph(active)
        _journal_legacy_helper_split(
            active_file=active_file,
            helper_id=helper_id,
            route="negate",
        )

        migrated = migrate_legacy_prover_helper_edges()

        assert migrated == ("terminal_conditions_consistent",)
        after = plan_state.load_blueprint()
        assert after.nodes == before.nodes
        assert after.edges == (
            plan_state.GraphEdge(source=helper_id, target=parent_id, kind="evidence"),
        )
        journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
        assert '"event": "prover-helper-evidence-migrated"' in journal

    def test_reclassifies_unused_prover_helper_under_decompose_route(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        before, active_file, _parent_id, helper_id = _legacy_negation_helper_graph(active)
        plan_state.append_journal_event(
            {
                "event": "orchestrator-route",
                "route": "negate",
                "name": "demo",
                "file": active_file,
            }
        )
        _journal_legacy_helper_split(
            active_file=active_file,
            helper_id=helper_id,
            route="decompose",
        )

        assert migrate_legacy_prover_helper_edges() == ("terminal_conditions_consistent",)
        after = plan_state.load_blueprint()
        assert after.nodes == before.nodes
        assert after.edges == (
            plan_state.GraphEdge(
                source=helper_id,
                target=plan_state.node_id_for("demo", active_file),
                kind="evidence",
            ),
        )
        assert after.revision > before.revision

    def test_preserves_managed_decomposer_placement(self, tmp_path, plan_enabled):
        """Route identity cannot erase the decomposer's structural provenance."""
        active = _file(tmp_path)
        before, active_file, _parent_id, helper_id = _legacy_negation_helper_graph(active)
        helper = before.node_by_id(helper_id)
        assert helper is not None
        before = plan_state.save_blueprint(
            before.replace_node(replace(helper, generated_by="decomposer"))
        )
        plan_state.append_journal_event(
            {
                "event": "helper-split-recorded",
                "node_id": helper_id,
                "name": helper.name,
                "target": "demo",
                "via": "decomposer",
            }
        )

        assert migrate_legacy_prover_helper_edges() == ()
        after = plan_state.load_blueprint()
        assert after.nodes == before.nodes
        assert after.edges == before.edges
        assert after.revision == before.revision

    def test_is_idempotent_after_one_exact_edge_conversion(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        _before, active_file, _parent_id, helper_id = _legacy_negation_helper_graph(active)
        _journal_legacy_helper_split(
            active_file=active_file,
            helper_id=helper_id,
            route="negate",
        )
        assert migrate_legacy_prover_helper_edges() == ("terminal_conditions_consistent",)
        once = plan_state.load_blueprint()
        journal_path = plan_state.plan_state_paths().journal_jsonl
        migration_events = journal_path.read_text(encoding="utf-8").count(
            '"event": "prover-helper-evidence-migrated"'
        )

        assert migrate_legacy_prover_helper_edges() == ()
        assert plan_state.load_blueprint() == once
        assert (
            journal_path.read_text(encoding="utf-8").count(
                '"event": "prover-helper-evidence-migrated"'
            )
            == migration_events
            == 1
        )

    def test_preserves_node_statuses_and_source_bytes(self, tmp_path, plan_enabled):
        active = _file(
            tmp_path,
            body=(
                "lemma terminal_conditions_consistent : True := by\n"
                "  trivial\n\n"
                "theorem demo : True := by\n"
                "  sorry\n"
            ),
        )
        before_source = active.read_bytes()
        before_mtime = active.stat().st_mtime_ns
        before, active_file, _parent_id, helper_id = _legacy_negation_helper_graph(active)
        _journal_legacy_helper_split(
            active_file=active_file,
            helper_id=helper_id,
            route="negate",
        )

        migrate_legacy_prover_helper_edges()

        after = plan_state.load_blueprint()
        assert [(node.id, node.status) for node in after.nodes] == [
            (node.id, node.status) for node in before.nodes
        ]
        assert active.read_bytes() == before_source
        assert active.stat().st_mtime_ns == before_mtime

    def test_mixed_negate_migration_preserves_integrated_support_and_repairs_campaign(
        self, tmp_path, plan_enabled
    ):
        """Only the unused obstruction loses structural/progress authority."""
        active = _file(
            tmp_path,
            body=(
                "lemma used_helper : True := by\n"
                "  trivial\n\n"
                "lemma terminal_conditions_consistent : True := by\n"
                "  trivial\n\n"
                "theorem demo : True ∧ True := by\n"
                "  constructor\n"
                "  · exact used_helper\n"
                "  · sorry\n"
            ),
        )
        active_file = str(active.resolve())
        parent_id = plan_state.node_id_for("demo", active_file)
        used_id = plan_state.node_id_for("used_helper", active_file)
        obstruction_id = plan_state.node_id_for("terminal_conditions_consistent", active_file)
        nodes = (
            plan_state.GraphNode(
                id=parent_id,
                name="demo",
                file=active_file,
                status="proving",
                generated_by="queue-sync",
            ),
            plan_state.GraphNode(
                id=used_id,
                kind="lemma",
                name="used_helper",
                file=active_file,
                status="proved",
                generated_by="prover-edit",
            ),
            plan_state.GraphNode(
                id=obstruction_id,
                kind="lemma",
                name="terminal_conditions_consistent",
                file=active_file,
                status="proved",
                generated_by="prover-edit",
            ),
        )
        plan_state.save_blueprint(
            plan_state.Blueprint(
                nodes=nodes,
                edges=(
                    plan_state.GraphEdge(source=used_id, target=parent_id, kind="split_of"),
                    plan_state.GraphEdge(source=parent_id, target=used_id, kind="depends_on"),
                    plan_state.GraphEdge(
                        source=obstruction_id,
                        target=parent_id,
                        kind="split_of",
                    ),
                    plan_state.GraphEdge(
                        source=parent_id,
                        target=obstruction_id,
                        kind="depends_on",
                    ),
                ),
            )
        )
        plan_state.append_journal_event(
            {
                "event": "orchestrator-route",
                "route": "negate",
                "name": "demo",
                "file": active_file,
            }
        )
        for helper_id, helper_name in (
            (used_id, "used_helper"),
            (obstruction_id, "terminal_conditions_consistent"),
        ):
            plan_state.append_journal_event(
                {
                    "event": "helper-split-recorded",
                    "node_id": helper_id,
                    "name": helper_name,
                    "target": "demo",
                    "via": "prover-edit",
                }
            )

        def seed_campaign(summary: dict[str, Any]) -> None:
            summary["campaign"] = {
                "campaign_id": "mixed-negate-migration",
                "no_progress_route_streak": 3,
                "last_verified_graph_progress": {
                    "node_ids": [obstruction_id],
                    "accounting": "parent-scoped-proof-mechanism",
                },
                "verified_mechanisms": {
                    "version": 1,
                    "entries": {
                        "parent:used": {
                            "first_node_id": used_id,
                            "last_node_id": used_id,
                            "seen_node_ids": [used_id],
                            "seen_count": 1,
                        },
                        "parent:obstruction": {
                            "first_node_id": obstruction_id,
                            "last_node_id": obstruction_id,
                            "seen_node_ids": [obstruction_id],
                            "seen_count": 1,
                        },
                    },
                },
            }

        update_json_file(
            plan_state.plan_state_paths().summary_json,
            seed_campaign,
        )

        assert migrate_legacy_prover_helper_edges() == ("terminal_conditions_consistent",)

        blueprint = plan_state.load_blueprint()
        edges = {(edge.source, edge.target, edge.kind) for edge in blueprint.edges}
        assert (used_id, parent_id, "split_of") in edges
        assert (parent_id, used_id, "depends_on") in edges
        assert (used_id, parent_id, "evidence") not in edges
        assert {edge for edge in edges if obstruction_id in {edge[0], edge[1]}} == {
            (obstruction_id, parent_id, "evidence")
        }
        campaign = plan_state.load_summary()["campaign"]
        assert campaign["no_progress_route_streak"] == 3
        assert "last_verified_graph_progress" not in campaign
        ledger_entries = campaign["verified_mechanisms"]["entries"]
        assert set(ledger_entries) == {"parent:used"}
        assert ledger_entries["parent:used"]["seen_node_ids"] == [used_id]
        assert obstruction_id not in str(campaign["verified_mechanisms"])
        reconciliation = campaign["prover_edit_evidence_accounting_reconciliation"]
        assert reconciliation["node_ids"] == [obstruction_id]
        assert reconciliation["last_verified_graph_progress_cleared"] is True
        assert reconciliation["previous_streak"] == 3
        assert reconciliation["repaired_streak"] == 3


def test_unused_periodic_helper_resume_restores_route_streak_floor(monkeypatch, tmp_path):
    """Resume cannot retain the false reset observed in the live decompose campaign."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "periodic-helper-resume")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.delenv("LEANFLOW_PLAN_STATE_DIR", raising=False)
    active = _file(
        tmp_path,
        body=(
            "lemma erdos_242_periodic_countermodel : True := by\n"
            "  trivial\n\n"
            "theorem erdos_242 : True := by\n"
            "  sorry\n"
        ),
    )
    active_file = str(active.resolve())
    parent_id = plan_state.node_id_for("erdos_242", active_file)
    helper_id = plan_state.node_id_for("erdos_242_periodic_countermodel", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="erdos_242",
                    file=active_file,
                    status="proving",
                    generated_by="queue-sync",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    kind="lemma",
                    name="erdos_242_periodic_countermodel",
                    file=active_file,
                    status="proved",
                    generated_by="prover-edit",
                ),
            ),
            edges=(
                plan_state.GraphEdge(helper_id, parent_id, "split_of"),
                plan_state.GraphEdge(parent_id, helper_id, "depends_on"),
            ),
        )
    )
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "route": "decompose",
            "name": "erdos_242",
            "file": active_file,
        }
    )
    plan_state.append_journal_event(
        {
            "event": "helper-split-recorded",
            "node_id": helper_id,
            "name": "erdos_242_periodic_countermodel",
            "target": "erdos_242",
            "via": "prover-edit",
        }
    )
    state: dict[str, Any] = {}
    campaign_epoch.ensure_campaign(state)

    def seed_false_reset(summary: dict[str, Any]) -> None:
        campaign = dict(summary["campaign"])
        campaign["epoch"] = 25
        campaign["no_progress_route_streak"] = 0
        campaign["no_progress_route_limit"] = 4
        campaign["epoch_routes"] = [
            {"route": route, "decided_at": f"2026-07-17T20:0{index}:00+00:00"}
            for index, route in enumerate(
                ("decompose", "plan", "plan", "plan", "decompose"),
                start=1,
            )
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": [helper_id],
            "recorded_at": "2026-07-17T20:06:00+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "erdos_242:periodic": {
                    "first_node_id": helper_id,
                    "last_node_id": helper_id,
                    "seen_node_ids": [helper_id],
                    "seen_count": 1,
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(plan_state.plan_state_paths().summary_json, seed_false_reset)

    assert migrate_legacy_prover_helper_edges() == ("erdos_242_periodic_countermodel",)
    campaign = plan_state.load_summary()["campaign"]
    assert campaign["no_progress_route_streak"] == 4
    assert "last_verified_graph_progress" not in campaign
    assert "verified_mechanisms" not in campaign
    reconciliation = campaign["prover_edit_evidence_accounting_reconciliation"]
    assert reconciliation["route_streak_floor"] == 5
    assert reconciliation["repaired_streak"] == 4
    assert reconciliation["rollover_required"] is True

    hydrated = campaign_epoch.rehydrate_campaign(state)
    assert hydrated["no_progress_route_streak"] == 4
    assert state["orchestrator_routes_used"] == 4
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"
    reason = campaign_epoch.consume_rollover_request(state)
    campaign_epoch.roll_epoch(
        state,
        reason=reason,
        cycle=0,
        target_symbol="erdos_242",
        active_file=active_file,
    )
    assert campaign_epoch.campaign_snapshot()["epoch"] == 26


def test_target_dependency_short_names_fail_closed_when_ambiguous():
    content = "theorem demo : True := by\n  exact shared\n"

    assert (
        _target_proof_dependency_names(
            content,
            target_symbol="demo",
            helper_names=("Left.shared", "Right.shared"),
        )
        == set()
    )
    assert _target_proof_dependency_names(
        content.replace("exact shared", "exact Left.shared"),
        target_symbol="demo",
        helper_names=("Left.shared", "Right.shared"),
    ) == {"Left.shared"}


def test_periodic_countermodel_name_spoof_is_not_exact_target_use(tmp_path):
    """A progress-shaped name and comment cannot promote a spontaneous helper."""
    active = _file(
        tmp_path,
        body=(
            "lemma erdos_242_periodic_countermodel_iff_positive_exceptional_families : True := by\n"
            "  trivial\n\n"
            "theorem erdos_242 : True := by\n"
            "  -- erdos_242_periodic_countermodel_iff_positive_exceptional_families\n"
            "  have erdos_242_periodic_countermodel_iff_positive_exceptional_families_note : "
            "True := True.intro\n"
            "  sorry\n"
        ),
    )
    helper = "erdos_242_periodic_countermodel_iff_positive_exceptional_families"

    assert prover_edit_evidence_helper_names(
        target_symbol="erdos_242",
        active_file=str(active),
        helper_names=(helper,),
        assigned_changed=True,
    ) == (helper,)

    active.write_text(
        active.read_text(encoding="utf-8").replace(
            "  sorry\n",
            f"  exact {helper}\n",
            1,
        ),
        encoding="utf-8",
    )
    assert (
        prover_edit_evidence_helper_names(
            target_symbol="erdos_242",
            active_file=str(active),
            helper_names=(helper,),
            assigned_changed=True,
        )
        == ()
    )


def _rollback_graph(
    active,
    *,
    helper_status="stated",
    helper_generated_by="decomposer",
    extra_edges=(),
):
    """Persist one exact decomposer split for graph rollback tests."""
    active_file = str(active.resolve())
    parent_id = plan_state.node_id_for("demo", active_file)
    helper_id = plan_state.node_id_for("abs_step", active_file)
    other_id = plan_state.node_id_for("other", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="demo",
                    file=active_file,
                    status="proving",
                    generated_by="queue-sync",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    kind="lemma",
                    name="abs_step",
                    file=active_file,
                    status=helper_status,
                    generated_by=helper_generated_by,
                ),
                plan_state.GraphNode(
                    id=other_id,
                    name="other",
                    file=active_file,
                    status="proved",
                    generated_by="human",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                plan_state.GraphEdge(source=parent_id, target=other_id, kind="depends_on"),
                *extra_edges,
            ),
        )
    )
    return active_file, parent_id, helper_id, other_id


class TestGraphRollback:
    def test_removes_only_exact_owned_helper_and_structural_edges(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        active_file, parent_id, helper_id, other_id = _rollback_graph(active)

        outcome = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert outcome.ok
        assert outcome.removed == ("abs_step",)
        blueprint = plan_state.load_blueprint()
        assert blueprint.node_by_id(helper_id) is None
        assert blueprint.node_by_id(parent_id) is not None
        assert blueprint.node_by_id(other_id) is not None
        assert all(helper_id not in {edge.source, edge.target} for edge in blueprint.edges)
        assert any(
            edge.source == parent_id and edge.target == other_id and edge.kind == "depends_on"
            for edge in blueprint.edges
        )

    def test_is_idempotent_after_exact_helper_is_absent(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        active_file, _parent_id, _helper_id, _other_id = _rollback_graph(active)
        first = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )
        revision = plan_state.load_blueprint().revision

        second = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert first.ok
        assert second.ok
        assert second.removed == ()
        assert second.already_absent == ("abs_step",)
        assert plan_state.load_blueprint().revision == revision

    @pytest.mark.parametrize("protected_status", ["proved", "false"])
    def test_rejects_protected_kernel_status(self, tmp_path, plan_enabled, protected_status):
        active = _file(tmp_path)
        active_file, _parent_id, helper_id, _other_id = _rollback_graph(
            active,
            helper_status=protected_status,
        )

        outcome = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert not outcome.ok
        assert "protected kernel status" in outcome.reason
        assert plan_state.load_blueprint().node_by_id(helper_id) is not None

    @pytest.mark.parametrize("unsafe_kind", ["evidence", "depends_on"])
    def test_rejects_evidence_and_unrelated_incident_edges(
        self, tmp_path, plan_enabled, unsafe_kind
    ):
        active = _file(tmp_path)
        active_file = str(active.resolve())
        helper_id = plan_state.node_id_for("abs_step", active_file)
        other_id = plan_state.node_id_for("other", active_file)
        unsafe_edge = plan_state.GraphEdge(
            source=other_id,
            target=helper_id,
            kind=unsafe_kind,
        )
        _rollback_graph(active, extra_edges=(unsafe_edge,))

        outcome = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert not outcome.ok
        assert "evidence or unrelated" in outcome.reason
        blueprint = plan_state.load_blueprint()
        assert blueprint.node_by_id(helper_id) is not None
        assert unsafe_edge in blueprint.edges

    def test_rejects_reassigned_helper_identity(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        active_file = str(active.resolve())
        parent_id = plan_state.node_id_for("demo", active_file)
        helper_id = plan_state.node_id_for("abs_step", active_file)
        alias_id = plan_state.node_id_for("alias", active_file)
        plan_state.save_blueprint(
            plan_state.Blueprint(
                nodes=(
                    plan_state.GraphNode(id=parent_id, name="demo", file=active_file),
                    plan_state.GraphNode(
                        id=helper_id,
                        name="alias",
                        file=active_file,
                        generated_by="decomposer",
                    ),
                    plan_state.GraphNode(
                        id=alias_id,
                        name="abs_step",
                        file=active_file,
                        generated_by="decomposer",
                    ),
                )
            )
        )

        outcome = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert not outcome.ok
        assert "reassigned" in outcome.reason
        assert len(plan_state.load_blueprint().nodes) == 3

    def test_revision_conflict_fails_without_claiming_success(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        active_file, _parent_id, helper_id, _other_id = _rollback_graph(active)
        monkeypatch.setattr(
            plan_state,
            "save_blueprint",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                plan_state.PlanStateRevisionConflict("raced")
            ),
        )

        outcome = rollback_decomposition_graph(
            target_symbol="demo",
            active_file=active_file,
            helper_names=("abs_step",),
        )

        assert not outcome.ok
        assert "changed while" in outcome.reason
        assert plan_state.load_blueprint().node_by_id(helper_id) is not None


def _begin_test_transaction(active):
    """Prepare one exact pending decomposition transaction for state tests."""
    before = active.read_bytes()
    before_text = before.decode("utf-8")
    offset = before_text.index(PARENT)
    after_text = before_text[:offset] + GOOD_STUB + "\n\n" + before_text[offset:]
    after = after_text.encode("utf-8")
    with decomposition_provenance.source_operation(active) as operation:
        record = decomposition_provenance.begin_decomposition(
            active_file=str(active),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            before_text=before.decode("utf-8"),
            after_text=after.decode("utf-8"),
            before_bytes=before,
            after_bytes=after,
            operation=operation,
        )
    return record, before, after


class TestSourceTransactions:
    def test_live_provenance_is_preserved_beyond_terminal_history_cap(self):
        history = [
            {"transaction_id": f"history-{index}", "state": "committed"}
            for index in range(decomposition_provenance._PROVENANCE_CAP + 7)
        ]
        pending = {"transaction_id": "pending", "state": "pending"}
        quarantined = {"transaction_id": "quarantined", "state": "quarantined"}

        retained = decomposition_provenance._retained_provenance_records(
            [*history, pending, quarantined]
        )

        retained_ids = {item["transaction_id"] for item in retained}
        assert len(retained) == decomposition_provenance._PROVENANCE_CAP + 2
        assert "pending" in retained_ids
        assert "quarantined" in retained_ids
        assert "history-0" not in retained_ids
        assert f"history-{len(history) - 1}" in retained_ids

    def test_live_provenance_overflow_fails_closed(self):
        live = [
            {
                "transaction_id": f"live-{index}",
                "state": "pending" if index % 2 else "quarantined",
            }
            for index in range(decomposition_provenance._PROVENANCE_CAP + 1)
        ]

        with pytest.raises(ValueError, match="too many live"):
            decomposition_provenance._retained_provenance_records(live)

    def test_symlink_retarget_during_begin_never_writes_retarget(self, monkeypatch, tmp_path):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = _file(first_dir)
        second = _file(second_dir)
        second_before = second.read_bytes()
        alias = tmp_path / "Alias.lean"
        alias.symlink_to(first)
        original_begin = decomposition_provenance.begin_decomposition

        def retarget_alias(**kwargs):
            alias.unlink()
            alias.symlink_to(second)
            return original_begin(**kwargs)

        monkeypatch.setattr(
            decomposition_provenance,
            "begin_decomposition",
            retarget_alias,
        )
        _ok_check(monkeypatch)

        outcome = place_helpers(
            active_file=str(alias),
            target_symbol="demo",
            skeletons=[GOOD_STUB],
            allowed_axioms=("propext",),
        )

        assert outcome.ok
        assert b"lemma abs_step" in first.read_bytes()
        assert second.read_bytes() == second_before
        assert alias.resolve() == second.resolve()

    def test_identical_begins_have_distinct_attempts_and_independent_finishes(
        self, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        winner, before, after = _begin_test_transaction(active)
        with decomposition_provenance.source_operation(active) as operation:
            loser = decomposition_provenance.begin_decomposition(
                active_file=str(active),
                target_symbol="demo",
                skeletons=[GOOD_STUB],
                before_text=before.decode("utf-8"),
                after_text=after.decode("utf-8"),
                before_bytes=before,
                after_bytes=after,
                operation=operation,
            )

        assert winner["insertion_fingerprint"] == loser["insertion_fingerprint"]
        assert winner["transaction_id"] != loser["transaction_id"]
        assert decomposition_provenance.finish_decomposition(
            winner["transaction_id"], state="committed"
        )
        assert decomposition_provenance.finish_decomposition(
            loser["transaction_id"], state="reverted"
        )
        states = {
            item["transaction_id"]: item["state"]
            for item in plan_state.load_summary()["decomposition_provenance"]
        }
        assert states[winner["transaction_id"]] == "committed"
        assert states[loser["transaction_id"]] == "reverted"

    def test_terminal_finish_is_monotonic(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        record, _before, _after = _begin_test_transaction(active)
        transaction_id = record["transaction_id"]

        assert decomposition_provenance.finish_decomposition(transaction_id, state="committed")
        assert not decomposition_provenance.finish_decomposition(
            transaction_id, state="reverted", reason="late loser"
        )
        assert not decomposition_provenance.finish_decomposition(
            transaction_id, state="quarantined", reason="late recovery"
        )
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == transaction_id
        )
        assert stored["state"] == "committed"
        assert "reason" not in stored

    def test_recovery_skips_live_owned_pending_attempt(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        before = active.read_bytes()
        after = (GOOD_STUB + "\n\n").encode("utf-8") + before
        with decomposition_provenance.source_operation(active) as operation:
            record = decomposition_provenance.begin_decomposition(
                active_file=str(active),
                target_symbol="demo",
                skeletons=[GOOD_STUB],
                before_text=before.decode("utf-8"),
                after_text=after.decode("utf-8"),
                before_bytes=before,
                after_bytes=after,
                operation=operation,
            )
            recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 0}
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "pending"

    def test_recovery_removes_exact_ghost_graph_before_marking_source_reverted(
        self, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, _before, _after = _begin_test_transaction(active)
        decomposition_provenance._ensure_pending_decomposition_graph(record)
        helper_id = plan_state.node_id_for("abs_step", str(active.resolve()))
        assert plan_state.load_blueprint().node_by_id(helper_id) is not None

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 1, "quarantined": 0}
        blueprint = plan_state.load_blueprint()
        assert blueprint.node_by_id(helper_id) is None
        assert all(helper_id not in {edge.source, edge.target} for edge in blueprint.edges)
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "reverted"

    def test_recovery_quarantines_restored_source_when_ghost_graph_is_protected(
        self, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, _before, _after = _begin_test_transaction(active)
        decomposition_provenance._ensure_pending_decomposition_graph(record)
        helper_id = plan_state.node_id_for("abs_step", str(active.resolve()))
        blueprint = plan_state.load_blueprint()
        helper = blueprint.node_by_id(helper_id)
        assert helper is not None
        plan_state.save_blueprint(blueprint.replace_node(replace(helper, status="proved")))

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert plan_state.load_blueprint().node_by_id(helper_id) is not None
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"
        reconciliation = decomposition_provenance.reconcile_quarantined_decompositions(
            cwd=str(tmp_path)
        )
        assert reconciliation["active"] == 1
        assert reconciliation["resolved"] == 0

    def test_pending_recovery_validates_every_exact_helper(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        calls = _ok_check(monkeypatch)

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 1, "reverted": 0, "quarantined": 0}
        assert calls == ["abs_step"]
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "committed"

    def test_pending_recovery_uses_canonical_fallback_for_prefix_failure(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {
                "success": False,
                "has_errors": True,
                "error_code": "prior_decl_failed",
                "error": "failed to build env before target at Move.half",
            },
        )
        checked_sources: list[str] = []

        def exact_source_check(source, **_kwargs):
            checked_sources.append(source)
            return {"success": True, "ok": True, "output": ""}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_ephemeral.lean_ephemeral_source_check",
            exact_source_check,
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 1, "reverted": 0, "quarantined": 0}
        assert checked_sources == [after.decode("utf-8")]
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "committed"

    @pytest.mark.parametrize("validation_mode", ["failure", "crash"])
    def test_failed_pending_recovery_validation_rolls_back_exact_source(
        self,
        monkeypatch,
        tmp_path,
        plan_enabled,
        validation_mode,
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )

        def validate(**_kwargs):
            if validation_mode == "crash":
                raise RuntimeError("validator crashed")
            return {"success": True, "has_errors": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            validate,
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 1, "quarantined": 0}
        assert active.read_bytes() == before
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "reverted"
        assert "pending helper validation failed" in stored["reason"]

    def test_failed_pending_recovery_restores_exact_crlf_bytes(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = tmp_path / "Demo.lean"
        before_text = "-- café\r\n" + PARENT.replace("\n", "\r\n") + "\r\n"
        offset = before_text.index("theorem demo")
        after_text = before_text[:offset] + GOOD_STUB + "\r\n\r\n" + before_text[offset:]
        before = before_text.encode("utf-8")
        after = after_text.encode("utf-8")
        active.write_bytes(before)
        with decomposition_provenance.source_operation(active) as operation:
            decomposition_provenance.begin_decomposition(
                active_file=str(active),
                target_symbol="demo",
                skeletons=[GOOD_STUB],
                before_text=before_text,
                after_text=after_text,
                before_bytes=before,
                after_bytes=after,
                operation=operation,
            )
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {"success": False, "has_errors": True},
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 1, "quarantined": 0}
        assert active.read_bytes() == before

    def test_concurrent_edit_during_recovery_validation_is_quarantined(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )

        def edit_during_validation(**_kwargs):
            active.write_bytes(active.read_bytes() + b"\n-- external edit\n")
            return {"success": True, "has_errors": True}

        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            edit_during_validation,
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert active.read_bytes().endswith(b"-- external edit\n")
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"

    def test_failed_recovery_validation_with_existing_graph_is_quarantined(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        decomposition_provenance._ensure_pending_decomposition_graph(record)
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {"success": False, "has_errors": True},
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert active.read_bytes() == after

    def test_failed_recovery_validation_quarantines_reassigned_graph_identity(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        active_file = str(active.resolve())
        blueprint = plan_state.load_blueprint().replace_node(
            plan_state.GraphNode(
                id="n-reassigned",
                name="abs_step",
                file=active_file,
                status="stated",
                generated_by="decomposer",
            )
        )
        plan_state.save_blueprint(blueprint)
        monkeypatch.setattr(
            "leanflow_cli.lean.lean_incremental.lean_incremental_check",
            lambda **_kwargs: {"success": False, "has_errors": True},
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert active.read_bytes() == before
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"
        assert plan_state.load_blueprint().node_by_id("n-reassigned") is not None

    def test_recovery_never_resolves_durable_source_path(self, monkeypatch, tmp_path, plan_enabled):
        active = _file(tmp_path)
        _record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        _ok_check(monkeypatch)
        monkeypatch.setattr(
            decomposition_provenance,
            "canonical_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("durable source identity was resolved")
            ),
        )

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 1, "reverted": 0, "quarantined": 0}

    def test_recovery_rejects_final_symlink_retarget(self, tmp_path, plan_enabled):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        original = tmp_path / "Original.lean"
        active.rename(original)
        attacker = tmp_path / "Attacker.lean"
        attacker.write_bytes(after)
        active.symlink_to(attacker)

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert attacker.read_bytes() == after
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"

    def test_recovery_rejects_ancestor_symlink_retarget(self, tmp_path, plan_enabled):
        owned = tmp_path / "owned"
        owned.mkdir()
        active = _file(owned)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        original = tmp_path / "original"
        owned.rename(original)
        attacker_root = tmp_path / "attacker"
        attacker_root.mkdir()
        attacker = attacker_root / active.name
        attacker.write_bytes(after)
        owned.symlink_to(attacker_root, target_is_directory=True)

        recovered = decomposition_provenance.recover_pending_decompositions(cwd=str(tmp_path))

        assert recovered == {"committed": 0, "reverted": 0, "quarantined": 1}
        assert attacker.read_bytes() == after
        assert (original / active.name).read_bytes() == after
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"

    def test_multiline_crlf_signature_hash_matches_lf_equivalent(self):
        source_lf = (
            "theorem multiline\n"
            "    (a : Nat)\n"
            "    (b : Nat)\n"
            "    : a + b = b + a := by\n"
            "  omega\n"
        )
        source_crlf = source_lf.replace("\n", "\r\n")

        lf = decomposition_provenance.declaration_slice(source_lf, "multiline")
        crlf = decomposition_provenance.declaration_slice(source_crlf, "multiline")

        assert lf is not None
        assert crlf is not None
        assert lf.signature != crlf.signature
        assert lf.signature_sha256 == crlf.signature_sha256

    def test_external_edit_before_final_revalidation_is_preserved(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        before = active.read_bytes()
        replacement = before + b"\n-- LeanFlow replacement\n"
        external = before + b"\n-- external editor write\n"

        def inject_external_edit(stage):
            if stage == "before-final-revalidation":
                active.write_bytes(external)

        monkeypatch.setattr(
            decomposition_provenance,
            "_source_cas_hook",
            inject_external_edit,
        )

        swapped = decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=replacement,
        )

        assert not swapped
        assert active.read_bytes() == external

    def test_source_lock_does_not_create_artifact_beside_source(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
        active = _file(tmp_path)
        before = active.read_bytes()
        replacement = before + b"\n-- replacement\n"

        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=replacement,
        )
        assert not list(tmp_path.glob(".*leanflow-source.lock"))

    def test_hardlink_aliases_share_the_same_source_lifecycle_lease(self, tmp_path):
        active = _file(tmp_path)
        alias = tmp_path / "Alias.lean"
        alias.hardlink_to(active)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def hold_first():
            with decomposition_provenance.source_operation(active):
                first_entered.set()
                assert release_first.wait(3)

        def enter_second():
            assert first_entered.wait(3)
            with decomposition_provenance.source_operation(alias):
                second_entered.set()

        first = threading.Thread(target=hold_first)
        second = threading.Thread(target=enter_second)
        first.start()
        second.start()
        assert first_entered.wait(3)
        assert not second_entered.wait(0.1)
        release_first.set()
        first.join(3)
        second.join(3)
        assert not first.is_alive()
        assert not second.is_alive()
        assert second_entered.is_set()

    def test_quarantined_insert_pauses_until_source_is_safely_restored(
        self, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        decomposition_provenance._ensure_pending_decomposition_graph(record)
        helper_id = plan_state.node_id_for("abs_step", str(active.resolve()))
        assert plan_state.load_blueprint().node_by_id(helper_id) is not None
        assert decomposition_provenance.finish_decomposition(
            record["transaction_id"],
            state="quarantined",
            reason="validation could not roll back",
        )

        unresolved = decomposition_provenance.reconcile_quarantined_decompositions(
            cwd=str(tmp_path)
        )
        assert unresolved["active"] == 1
        assert unresolved["resolved"] == 0

        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=after,
            replacement_bytes=before,
        )
        restored = decomposition_provenance.reconcile_quarantined_decompositions(cwd=str(tmp_path))
        assert restored["active"] == 0
        assert restored["resolved"] == 1
        blueprint = plan_state.load_blueprint()
        assert blueprint.node_by_id(helper_id) is None
        assert all(helper_id not in {edge.source, edge.target} for edge in blueprint.edges)
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "reverted"
        assert stored["quarantine_reconciled"] is True

    def test_quarantine_reconciliation_rechecks_source_after_graph_rollback(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, before, after = _begin_test_transaction(active)
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=before,
            replacement_bytes=after,
        )
        decomposition_provenance._ensure_pending_decomposition_graph(record)
        assert decomposition_provenance.finish_decomposition(
            record["transaction_id"],
            state="quarantined",
            reason="test quarantine",
        )
        assert decomposition_provenance.compare_and_swap_source(
            active,
            expected_bytes=after,
            replacement_bytes=before,
        )
        real_rollback = decomposition_provenance._rollback_restored_decomposition_graph

        def rollback_then_retarget(raw_record):
            outcome = real_rollback(raw_record)
            active.write_bytes(after)
            return outcome

        monkeypatch.setattr(
            decomposition_provenance,
            "_rollback_restored_decomposition_graph",
            rollback_then_retarget,
        )

        reconciliation = decomposition_provenance.reconcile_quarantined_decompositions(
            cwd=str(tmp_path)
        )

        assert reconciliation["active"] == 1
        assert reconciliation["resolved"] == 0
        stored = next(
            item
            for item in plan_state.load_summary()["decomposition_provenance"]
            if item["transaction_id"] == record["transaction_id"]
        )
        assert stored["state"] == "quarantined"

    def test_malformed_quarantine_path_reports_active_without_unbound_error(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        record, _before, _after = _begin_test_transaction(active)
        assert decomposition_provenance.finish_decomposition(
            record["transaction_id"],
            state="quarantined",
            reason="test malformed path",
        )
        monkeypatch.setattr(
            decomposition_provenance,
            "_stored_canonical_source_path",
            lambda _record: (_ for _ in ()).throw(OSError("malformed durable path")),
        )

        reconciliation = decomposition_provenance.reconcile_quarantined_decompositions(
            cwd=str(tmp_path)
        )

        assert reconciliation["active"] == 1
        assert reconciliation["resolved"] == 0
        assert reconciliation["reasons"]


def _backend(
    monkeypatch,
    helpers: list[dict[str, Any]],
    success: bool = True,
    **extra: Any,
):
    import json as _json

    def fake(theorem_id, file_path, **kwargs):
        return _json.dumps({"success": success, "helpers": helpers, **extra})

    monkeypatch.setattr("tools.implementations.lean_experts.lean_decompose_helpers_tool", fake)


class TestRunDecomposer:
    def test_live_advisor_stub_reaches_managed_placement_and_graph(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        from tools.implementations import lean_experts

        active = _file(tmp_path)
        _ok_check(monkeypatch)
        monkeypatch.setattr(lean_experts, "resolve_expert_provider", lambda _task: "test-model")
        monkeypatch.setattr(lean_experts, "is_command_expert_provider", lambda _provider: False)
        monkeypatch.setattr(
            lean_experts,
            "call_llm",
            lambda **kwargs: SimpleNamespace(
                model="test-model",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "helpers": [
                                        {
                                            "name": "abs_step",
                                            "purpose": "Isolate the absolute-value nonnegativity step.",
                                            "lean_skeleton": GOOD_STUB,
                                            "dependencies": [],
                                            "proof_hints": ["exact abs_nonneg a"],
                                        }
                                    ]
                                }
                            )
                        )
                    )
                ],
            ),
        )
        monkeypatch.setattr(
            lean_experts,
            "lean_incremental_check",
            lambda **kwargs: {
                "success": True,
                "ok": False,
                "errors": 0,
                "has_errors": False,
                "sorry": 2,
                "tool": "lean_probe",
                "action": "check_target",
                "file": str(active.resolve()),
                "target": "demo",
                "replacement_matches_target": True,
                "verification_scope": "target_candidate",
                "output": "warning: declarations use `sorry`",
            },
        )

        outcome = run_decomposer(
            target_symbol="demo",
            active_file=str(active),
            statement=PARENT,
            cwd=str(tmp_path),
        )

        assert outcome.ok
        assert outcome.placed == ("abs_step",)
        assert GOOD_STUB in active.read_text(encoding="utf-8")
        helper_id = plan_state.node_id_for("abs_step", str(active))
        target_id = plan_state.node_id_for("demo", str(active))
        blueprint = plan_state.load_blueprint()
        assert blueprint.node_by_id(helper_id).status == "stated"
        assert any(
            edge.source == helper_id and edge.target == target_id and edge.kind == "split_of"
            for edge in blueprint.edges
        )

    def test_new_managed_placement_flag_overrides_legacy_ready_flag(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        _backend(
            monkeypatch,
            helpers=[
                {
                    "name": "abs_step",
                    "lean_skeleton": GOOD_STUB,
                    "ready_for_managed_placement": False,
                    "ready_to_insert": True,
                    "validation_order": 1,
                }
            ],
        )

        outcome = run_decomposer(
            target_symbol="demo",
            active_file=str(active),
            statement=PARENT,
        )

        assert not outcome.ok
        assert outcome.skipped == ("abs_step",)
        assert GOOD_STUB not in active.read_text(encoding="utf-8")

    def test_backend_helper_with_unsupported_bound_is_not_inserted(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = tmp_path / "Demo.lean"
        parent = (
            "theorem eventual (a : ℕ) : ∀ᶠ n in Filter.atTop, "
            "∃ x : ℕ, a / n = 1 / x := by\n  sorry\n"
        )
        active.write_text("import Mathlib\n\n" + parent, encoding="utf-8")
        _ok_check(monkeypatch)
        _backend(
            monkeypatch,
            helpers=[
                {
                    "name": "guessed",
                    "lean_skeleton": (
                        "lemma guessed (a n : ℕ) (hn : n ≥ a * 6) : "
                        "∃ x : ℕ, a / n = 1 / x := by sorry"
                    ),
                    "ready_to_insert": True,
                    "validation_order": 1,
                }
            ],
        )

        outcome = run_decomposer(
            target_symbol="eventual",
            active_file=str(active),
            statement=parent,
        )

        assert not outcome.ok
        assert outcome.skipped == ("guessed",)
        assert "lemma guessed" not in active.read_text(encoding="utf-8")

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
        assert bp.node_by_id(helper_id).statement == GOOD_STUB
        kinds = {(e.source, e.target, e.kind) for e in bp.edges}
        assert (helper_id, target_id, "split_of") in kinds
        assert (target_id, helper_id, "depends_on") in kinds
        # Guard caches refreshed so the prover will not restore the stubs.
        assert agent._managed_queue_edit_guard_state == {}
        assert agent._managed_initial_declaration_keys_by_file == {}
        provenance = plan_state.load_summary()["decomposition_provenance"][-1]
        assert provenance["state"] == "committed"
        assert provenance["parent"] == "demo"
        assert [helper["name"] for helper in provenance["helpers"]] == ["abs_step"]

    def test_managed_placement_preserves_helper_dependency_order(
        self, monkeypatch, tmp_path, plan_enabled
    ):
        active = _file(tmp_path)
        _ok_check(monkeypatch)
        first = "private lemma abs_nonneg_step (a : ℝ) : 0 ≤ |a| := by sorry"
        second = "private lemma abs_self_step (a : ℝ) : |a| = |a| := by sorry"
        _backend(
            monkeypatch,
            helpers=[
                {
                    "name": "abs_nonneg_step",
                    "lean_skeleton": first,
                    "dependencies": [],
                    "ready_to_insert": True,
                    "validation_order": 1,
                },
                {
                    "name": "abs_self_step",
                    "lean_skeleton": second,
                    "dependencies": ["abs_nonneg_step"],
                    "ready_to_insert": True,
                    "validation_order": 2,
                },
            ],
        )

        outcome = run_decomposer(
            target_symbol="demo",
            active_file=str(active),
            statement=PARENT,
            cwd=str(tmp_path),
        )

        assert outcome.ok
        first_id = plan_state.node_id_for("abs_nonneg_step", str(active))
        second_id = plan_state.node_id_for("abs_self_step", str(active))
        blueprint = plan_state.load_blueprint()
        assert plan_state.GraphEdge(second_id, first_id, "depends_on") in blueprint.edges
        assert [node.name for node in blueprint.frontier()] == ["abs_nonneg_step"]
        provenance = plan_state.load_summary()["decomposition_provenance"][-1]
        by_name = {helper["name"]: helper for helper in provenance["helpers"]}
        assert by_name["abs_self_step"]["dependencies"] == ["abs_nonneg_step"]

    def test_backfill_links_only_explicit_known_helpers(self, tmp_path, plan_enabled):
        active = _file(
            tmp_path,
            body=(
                "lemma historical : True := by\n"
                "  trivial\n\n"
                "lemma known_child : True := by\n"
                "  trivial\n\n"
                "theorem demo : True := by\n"
                "  sorry\n"
            ),
        )

        linked = backfill_known_prover_helpers(
            target_symbol="demo",
            active_file=str(active),
            helper_names=("known_child", "missing_child"),
        )

        assert linked == ("known_child",)
        bp = plan_state.load_blueprint()
        target_id = plan_state.node_id_for("demo", str(active))
        helper_id = plan_state.node_id_for("known_child", str(active))
        historical_id = plan_state.node_id_for("historical", str(active))
        helper = bp.node_by_id(helper_id)
        assert helper is not None
        assert helper.status == "proving"
        assert helper.generated_by == "prover-edit-backfill"
        assert bp.node_by_id(historical_id) is None
        edges = {(edge.source, edge.target, edge.kind) for edge in bp.edges}
        assert (helper_id, target_id, "split_of") in edges
        assert (target_id, helper_id, "depends_on") in edges

    def test_backend_failure_is_a_clean_fallback(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        _backend(
            monkeypatch,
            helpers=[],
            success=False,
            status="timeout",
            provider_called=True,
        )

        outcome = run_decomposer(target_symbol="demo", active_file=str(active))

        assert not outcome.ok
        assert isinstance(outcome, DecomposeOutcome)
        assert outcome.advisor_success is False
        assert outcome.advisor_status == "timeout"
        assert outcome.advisor_provider_called is True

    def test_no_guarded_helpers_reports_reason(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        _backend(
            monkeypatch,
            helpers=[{"name": "bad", "lean_skeleton": "def f : Nat := 0", "ready_to_insert": True}],
            obstacle_summary="the proposed declaration is not a lemma",
            recommended_split="state a guarded arithmetic helper instead",
            first_concrete_next_edit=(
                "  Prove the arithmetic helper in scratch first,\n"
                "then rerun its exact Lean check.  "
            ),
        )

        outcome = run_decomposer(target_symbol="demo", active_file=str(active))

        assert not outcome.ok
        assert "no ready, guarded helpers" in outcome.reason
        assert "bad" in outcome.skipped
        assert outcome.obstacle_summary == "the proposed declaration is not a lemma"
        assert outcome.recommended_split == "state a guarded arithmetic helper instead"
        assert outcome.first_concrete_next_edit == (
            "Prove the arithmetic helper in scratch first, then rerun its exact Lean check."
        )
        assert outcome.to_payload()["first_concrete_next_edit"] == (
            outcome.first_concrete_next_edit
        )
        assert outcome.advisor_success is True

    def test_no_guarded_helpers_bounds_first_concrete_next_edit(self, monkeypatch, tmp_path):
        active = _file(tmp_path)
        _backend(
            monkeypatch,
            helpers=[],
            first_concrete_next_edit="check this exact helper " * 200,
        )

        outcome = run_decomposer(target_symbol="demo", active_file=str(active))

        assert len(outcome.first_concrete_next_edit) == 1600
        assert outcome.first_concrete_next_edit.endswith("...")
        assert outcome.to_payload()["first_concrete_next_edit"] == (
            outcome.first_concrete_next_edit
        )
