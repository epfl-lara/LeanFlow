"""Regression tests for coupled answer/result determination lifecycle."""

from __future__ import annotations

from leanflow_cli.native import determine_answer_policy as policy


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
