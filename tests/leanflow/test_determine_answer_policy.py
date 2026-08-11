"""Regression tests for coupled answer/result determination lifecycle."""

from __future__ import annotations

from leanflow_cli.native import determine_answer_policy as policy
from leanflow_cli.native import native_runner as runner


def _assignment(path: str) -> dict:
    return {
        "target_symbol": "answer",
        "active_file": path,
        "slice": "Assigned declaration slice (answer):\ndef answer : Set ℝ := by\n  sorry",
    }


def _result_state(path: str) -> dict:
    return {
        "active_file": path,
        "current_queue_item": {"label": "result", "kind": "theorem"},
        "current_queue_item_slice": ("theorem result : {x : ℝ | x = 1} = answer := by\n  sorry"),
        "declaration_queue_summary": "- result [Main.lean] — contains sorry",
    }


def _answer_verification() -> dict:
    return {
        "scope": "target:answer",
        "target": "answer",
        "ok": True,
        "errors": 0,
        "sorry": 0,
    }


def test_detect_transition_recognizes_answer_definition_and_consumer(tmp_path):
    active = str(tmp_path / "Main.lean")

    dependency = policy.detect_transition(_assignment(active), _result_state(active))

    assert dependency is not None
    assert dependency.answer_target == "answer"
    assert dependency.consumer_target == "result"


def test_detect_transition_survives_compiled_baseline_but_rejects_unrelated_consumer(
    tmp_path,
):
    active = str(tmp_path / "Main.lean")
    compiled = _assignment(active)
    compiled["slice"] = "def answer : Set ℝ := {1}"
    unrelated = _result_state(active)
    unrelated["current_queue_item_slice"] = "theorem result : True := by\n  sorry"

    assert policy.detect_transition(compiled, _result_state(active)) is not None
    assert policy.detect_transition(_assignment(active), unrelated) is None


def test_discover_for_consumer_recovers_explicit_determine_source(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "/-- The answer to be determined. -/\n"
        "def answer : Set Nat := {2}\n"
        "theorem helper : True := by trivial\n"
        "theorem result : answer = answer := by\n  sorry\n",
        encoding="utf-8",
    )

    dependency = policy.discover_for_consumer(str(active), "result")

    assert dependency is not None
    assert dependency.answer_target == "answer"
    assert dependency.consumer_target == "result"


def test_discover_for_consumer_requires_determine_marker(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "def answer : Set Nat := {2}\n" "theorem result : answer = answer := by\n  sorry\n",
        encoding="utf-8",
    )

    assert policy.discover_for_consumer(str(active), "result") is None


def test_provisional_state_allows_only_coupled_answer_edit_and_renders_contract(tmp_path):
    active = str(tmp_path / "Main.lean")
    dependency = policy.detect_transition(_assignment(active), _result_state(active))
    assert dependency is not None
    state: dict = {}
    policy.register(state, dependency, verification=_answer_verification())

    assert policy.editable_answer_names(
        state,
        consumer_target="result",
        consumer_file=active,
    ) == {"answer"}
    protected = [
        {"kind": "def", "name": "answer"},
        {"kind": "theorem", "name": "helper"},
    ]
    assert policy.without_editable_answers(protected, {"answer"}) == [
        {"kind": "theorem", "name": "helper"}
    ]
    rendered = policy.prompt(
        state,
        consumer_target="result",
        consumer_file=active,
    )
    assert "remains provisional" in rendered
    assert "revise the coupled answer definition" in rendered
    assert "only a clean kernel gate for `result` promotes" in rendered


def test_transition_summary_keeps_compiled_answer_provisional(tmp_path):
    active = str(tmp_path / "Main.lean")
    autonomy_state = {
        "current_queue_assignment": _assignment(active),
        "last_verification": _answer_verification(),
    }

    outcome = runner._summarize_theorem_transition_outcome(
        autonomy_state,
        _result_state(active),
        [],
    )

    assert outcome["status"] == "provisional"
    assert "characterizing theorem result verifies" in outcome["note"]


def test_exact_transition_gate_replaces_file_gate_with_future_sorry(
    monkeypatch,
    tmp_path,
):
    active = str(tmp_path / "Main.lean")
    dependency = policy.detect_transition(_assignment(active), _result_state(active))
    assert dependency is not None
    autonomy_state = {
        "last_verification": {
            "scope": "file",
            "target": "answer",
            "ok": True,
            "errors": 0,
            "sorry": 1,
        }
    }
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item",
        lambda active_file, target_symbol: (
            {
                "ok": True,
                "mode": "incremental_target",
                "target": target_symbol,
                "incremental": {
                    "success": True,
                    "ok": True,
                    "target": target_symbol,
                    "has_errors": False,
                    "has_sorry": False,
                    "sorry": 0,
                    "messages": [],
                },
            },
            "lean_incremental_check",
        ),
    )
    monkeypatch.setattr(runner, "_log_manager_verification", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    verification = runner._ensure_determine_answer_exact_gate(
        autonomy_state,
        dependency,
    )

    assert verification["scope"] == "target:answer"
    assert verification["sorry"] == 0
    assert autonomy_state["last_verification"]["target"] == "answer"


def test_resume_recovers_provisional_answer_before_result_turn(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "/-- The answer to be determined. -/\n"
        "def answer : Set Nat := {2}\n"
        "theorem result : answer = answer := by\n  sorry\n",
        encoding="utf-8",
    )
    autonomy_state: dict = {}
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item",
        lambda active_file, target_symbol: (
            {
                "ok": True,
                "mode": "file_exact",
                "target": active_file,
                "errors": 0,
                "sorry": 1,
                "messages": [
                    {
                        "severity": "warning",
                        "message": "declaration uses `sorry`",
                    }
                ],
            },
            "lean_verify",
        ),
    )
    monkeypatch.setattr(runner, "_log_manager_verification", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    assert runner._recover_provisional_determine_answer_assignment(
        autonomy_state,
        target_symbol="result",
        active_file=str(active),
    )
    assert policy.editable_answer_names(
        autonomy_state,
        consumer_target="result",
        consumer_file=str(active),
    ) == {"answer"}
    answer_outcome = next(iter(autonomy_state["theorem_outcomes"].values()))
    assert answer_outcome["status"] == "provisional"
    assert answer_outcome["last_verification"]["tool"] == "determine_answer_elaboration"


def test_solved_consumer_promotes_answer_with_its_original_exact_gate(
    monkeypatch,
    tmp_path,
):
    active = str(tmp_path / "Main.lean")
    dependency = policy.detect_transition(_assignment(active), _result_state(active))
    assert dependency is not None
    autonomy_state: dict = {}
    policy.register(
        autonomy_state,
        dependency,
        verification=_answer_verification(),
    )
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item",
        lambda active_file, target_symbol: (
            {
                "ok": True,
                "mode": "incremental_target",
                "target": target_symbol,
                "incremental": {
                    "success": True,
                    "ok": True,
                    "target": target_symbol,
                    "has_errors": False,
                    "has_sorry": False,
                    "sorry": 0,
                    "messages": [],
                },
            },
            "lean_incremental_check",
        ),
    )

    runner._reconcile_determine_answer_outcome(
        autonomy_state,
        None,
        {
            "status": "solved",
            "target_symbol": "result",
            "active_file": active,
        },
    )

    outcomes = dict(autonomy_state.get("theorem_outcomes") or {})
    answer = next(raw for raw in outcomes.values() if raw.get("target_symbol") == "answer")
    assert answer["status"] == "solved"
    assert answer["last_verification"]["target"] == "answer"
    assert policy.STATE_KEY not in autonomy_state
    assert events[0][0][0] == "determine-answer-promoted"


def test_provisional_answer_blocks_verified_final_exit():
    autonomy_state = {
        "theorem_outcomes": {
            "Main.lean::answer": {
                "target_symbol": "answer",
                "active_file": "Main.lean",
                "status": "provisional",
            }
        }
    }

    assert runner._has_unresolved_theorem_outcomes(autonomy_state)


def test_result_turn_queue_guard_allows_coupled_answer_revision(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "def answer : Set Nat := {1}\n"
        "theorem helper : True := by trivial\n"
        "theorem result : answer = answer := by\n  sorry\n",
        encoding="utf-8",
    )
    dependency = policy.DetermineAnswerDependency(
        answer_target="answer",
        answer_file=str(active),
        consumer_target="result",
        consumer_file=str(active),
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "result",
            "active_file": str(active),
        }
    }
    policy.register(
        autonomy_state,
        dependency,
        verification=_answer_verification(),
    )

    class Agent:
        _managed_autonomy_state = autonomy_state

        def is_interrupted(self):
            return False

    agent = Agent()
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert (
        runner._managed_pre_tool_call(
            agent,
            "patch",
            {"path": str(active)},
        )
        is None
    )
    active.write_text(
        "def answer : Set Nat := {2}\n"
        "theorem helper : True := by trivial\n"
        "theorem result : answer = answer := by\n  rfl\n",
        encoding="utf-8",
    )

    assert runner._restore_out_of_scope_queue_edit(agent, "patch") == ""
    assert "def answer : Set Nat := {2}" in active.read_text(encoding="utf-8")


def test_trivializing_answer_revision_is_detected(tmp_path):
    active = str(tmp_path / "Main.lean")
    dependency = policy.DetermineAnswerDependency(
        answer_target="answer",
        answer_file=active,
        consumer_target="result",
        consumer_file=active,
    )
    state: dict = {}
    policy.register(state, dependency, verification=_answer_verification())
    before = (
        "def answer : Set ℝ := {θ | θ = 1}\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = answer := by\n  sorry\n"
    )
    after = (
        "def answer : Set ℝ := {θ : ℝ | 0 < θ ∧ θ < Real.pi ∧ Wins θ}\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = answer := by\n  rfl\n"
    )

    assert policy.trivializing_answer_revisions(
        state,
        consumer_target="result",
        consumer_file=active,
        before_source=before,
        after_source=after,
    ) == ("answer",)


def test_coupled_algebraic_restatement_revision_is_detected(tmp_path):
    active = str(tmp_path / "Main.lean")
    dependency = policy.DetermineAnswerDependency(
        answer_target="answer",
        answer_file=active,
        consumer_target="result",
        consumer_file=active,
    )
    state: dict = {}
    policy.register(state, dependency, verification=_answer_verification())
    before = (
        "def answer : Set (ℝ → ℝ) := {f | ∃ c, ∀ x, f x = x + c}\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, "
        "P (f x) (f y) x y ∧ Q (f x) (f y) x y} = answer := by\n  sorry\n"
    )
    after = (
        "def answer : Set (ℝ → ℝ) := {f | ∀ x y : ℝ, "
        "P' (f x) (f y) x y ∧ Q' (f x) (f y) x y}\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, "
        "P (f x) (f y) x y ∧ Q (f x) (f y) x y} = answer := by\n  sorry\n"
    )

    assert policy.trivializing_answer_revisions(
        state,
        consumer_target="result",
        consumer_file=active,
        before_source=before,
        after_source=after,
    ) == ("answer",)


def test_initial_definition_target_copy_is_detected_without_registered_state():
    before = (
        "def expected : Set ℝ := sorry\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = expected := by\n  sorry\n"
    )
    after = (
        "def expected : Set ℝ := {θ : ℝ | 0 < θ ∧ θ < Real.pi ∧ Wins θ}\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = expected := by\n  sorry\n"
    )

    assert policy.target_copying_definition_consumers(
        "expected",
        before_source=before,
        after_source=after,
    ) == ("result",)


def test_initial_definition_allows_independent_characterization():
    before = (
        "def expected : Set ℝ := sorry\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = expected := by\n  sorry\n"
    )
    after = (
        "def expected : Set ℝ := {θ : ℝ | θ = π / 3}\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = expected := by\n  sorry\n"
    )

    assert not policy.target_copying_definition_consumers(
        "expected",
        before_source=before,
        after_source=after,
    )


def test_algebraic_predicate_restatement_is_detected():
    before = (
        "def expected : Set (ℝ → ℝ) := sorry\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, "
        "(f x + y) / 2 ≤ √((x ^ 2 + f y ^ 2) / 2) ∧ "
        "√(x * f y) ≤ (f x + y) / 2} = expected := by\n  sorry\n"
    )
    after = (
        "def expected : Set (ℝ → ℝ) := {f | ∀ x y : ℝ, "
        "4 * x * f y ≤ (f x + y) ^ 2 ∧ "
        "(f x + y) ^ 2 ≤ 2 * x ^ 2 + 2 * f y ^ 2}\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, "
        "(f x + y) / 2 ≤ √((x ^ 2 + f y ^ 2) / 2) ∧ "
        "√(x * f y) ≤ (f x + y) / 2} = expected := by\n  sorry\n"
    )

    assert policy.restating_definition_consumers(
        "expected",
        before_source=before,
        after_source=after,
    ) == ("result",)


def test_explicit_family_is_not_a_predicate_restatement():
    before = (
        "def expected : Set (ℝ → ℝ) := sorry\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, P (f x) (f y) x y ∧ "
        "Q (f x) (f y) x y} = expected := by\n  sorry\n"
    )
    after = (
        "def expected : Set (ℝ → ℝ) := "
        "{f | ∃ c : ℝ, 0 ≤ c ∧ ∀ x, f x = x + c}\n"
        "theorem result : {f : ℝ → ℝ | ∀ x y : ℝ, P (f x) (f y) x y ∧ "
        "Q (f x) (f y) x y} = expected := by\n  sorry\n"
    )

    assert not policy.restating_definition_consumers(
        "expected",
        before_source=before,
        after_source=after,
    )


def test_initial_definition_target_copy_is_blocked_before_edit(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "def expected : Set ℝ := sorry\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = expected := by\n  sorry\n",
        encoding="utf-8",
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "expected",
            "active_file": str(active),
        }
    }

    class Agent:
        _managed_autonomy_state = autonomy_state

    patch = """*** Begin Patch
*** Update File: Main.lean
@@
-def expected : Set ℝ := sorry
+def expected : Set ℝ := {θ : ℝ | 0 < θ ∧ θ < Real.pi ∧ Wins θ}
*** End Patch"""
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))

    result = runner._determine_answer_trivialization_pre_tool_guard(
        Agent(),
        "apply_verified_patch",
        {"path": str(active), "patch": patch},
        autonomy_state,
    )

    assert result is not None
    assert "determine_answer_trivialization_rejected" in result
    assert '"consumer_targets": ["result"]' in result
    assert active.read_text(encoding="utf-8").startswith("def expected : Set ℝ := sorry")


def test_trivializing_answer_revision_is_blocked_before_edit(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "def answer : Set ℝ := {θ | θ = 1}\n"
        "theorem result : {θ : ℝ | 0 < θ ∧ θ < π ∧ Wins θ} = answer := by\n  sorry\n",
        encoding="utf-8",
    )
    dependency = policy.DetermineAnswerDependency(
        answer_target="answer",
        answer_file=str(active),
        consumer_target="result",
        consumer_file=str(active),
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "result",
            "active_file": str(active),
        }
    }
    policy.register(autonomy_state, dependency, verification=_answer_verification())

    class Agent:
        _managed_autonomy_state = autonomy_state

    patch = """*** Begin Patch
*** Update File: Main.lean
@@
-def answer : Set ℝ := {θ | θ = 1}
+def answer : Set ℝ := {θ : ℝ | 0 < θ ∧ θ < Real.pi ∧ Wins θ}
@@
-  sorry
+  rfl
*** End Patch"""
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))

    result = runner._determine_answer_trivialization_pre_tool_guard(
        Agent(),
        "apply_verified_patch",
        {"path": str(active), "patch": patch},
        autonomy_state,
    )

    assert result is not None
    assert "determine_answer_trivialization_rejected" in result
    assert '"lean_started": false' in result
    assert active.read_text(encoding="utf-8").startswith("def answer : Set ℝ := {θ | θ = 1}")


def test_provisional_outcome_is_not_recorded_as_failed_attempt():
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "answer",
            "active_file": "Main.lean",
            "slice": "def answer := by\n  sorry",
        }
    }

    runner._remember_transition_failed_attempt(
        autonomy_state,
        {
            "status": "provisional",
            "target_symbol": "answer",
            "active_file": "Main.lean",
        },
    )

    assert not autonomy_state.get("failed_attempts")
