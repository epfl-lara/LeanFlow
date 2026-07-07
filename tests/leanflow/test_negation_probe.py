"""Phase 3 §5 tests: negation-goal construction, classification, budgeted pipeline."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.lean import negation_probe as np
from leanflow_cli.lean.negation_probe import NegationGoal


def _write(tmp_path, text: str):
    path = tmp_path / "Demo.lean"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- goal construction -------------------------------------------------------


def test_build_negation_simple_forall(tmp_path):
    path = _write(tmp_path, "import Mathlib\n\ntheorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")

    goal = np.build_negation_goal(path, "bad")

    assert isinstance(goal, NegationGoal)
    assert goal.prop == "∀ n : Nat, n < 5"
    assert goal.lean_code == "theorem neg_bad : ¬ (∀ n : Nat, n < 5) := by\n  sorry"


def test_build_negation_binder_soup(tmp_path):
    text = (
        "@[simp] private theorem Foo.bar (x : Nat) {n : Nat} [inst : Add Nat]\n"
        "    (f : Nat → Nat := id) (h : x < n) : f x ≤ n := by\n"
        "  sorry\n"
    )
    path = _write(tmp_path, text)

    goal = np.build_negation_goal(path, "Foo.bar")

    assert isinstance(goal, NegationGoal)
    # Binder text is reused byte-for-byte, ':=' default included (top-level
    # ':' split must skip the default inside the parens).
    assert "(f : Nat → Nat := id)" in goal.binders
    assert goal.result_type == "f x ≤ n"
    assert goal.name == "neg_bar"
    assert goal.prop.startswith("∀ (x : Nat)")


def test_build_negation_handles_comments_inside_signature(tmp_path):
    text = (
        "theorem tricky -- a colon : in a comment\n"
        '    (s : String := "a : b") : s.length ≥ 0 := by\n'
        "  sorry\n"
    )
    path = _write(tmp_path, text)

    goal = np.build_negation_goal(path, "tricky")

    assert isinstance(goal, NegationGoal)
    assert goal.result_type == "s.length ≥ 0"


def test_build_negation_error_paths(tmp_path):
    path = _write(tmp_path, "def compute (n : Nat) : Nat := n + 1\n")
    outcome = np.build_negation_goal(path, "compute")
    assert isinstance(outcome, dict)
    assert outcome["error_code"] == "unsupported_kind"

    assert np.build_negation_goal(path, "missing")["error_code"] == "not_found"

    universe = _write(tmp_path, "theorem u.{v} : Type v := by\n  sorry\n")
    assert np.build_negation_goal(universe, "u")["error_code"] in {
        "ill_formed",
        "not_found",
        "parse_failure",
    }


def test_scratch_header_adds_plausible_once(tmp_path):
    path = _write(
        tmp_path, "import Mathlib\nimport Plausible\n\ntheorem t : True := by\n  trivial\n"
    )
    header = np.scratch_header(path)
    assert header.splitlines() == [
        "import Mathlib",
        "import Plausible",
        "set_option autoImplicit false",
    ]

    bare = _write(tmp_path, "theorem t : True := by\n  trivial\n")
    assert np.scratch_header(bare) == "import Plausible\nset_option autoImplicit false"


# --- classification over canned scratch payloads -----------------------------


def _goal() -> NegationGoal:
    return NegationGoal(
        name="neg_bad",
        original="theorem bad : ∀ n : Nat, n < 5",
        binders="",
        result_type="∀ n : Nat, n < 5",
        prop="∀ n : Nat, n < 5",
        lean_code="theorem neg_bad : ¬ (∀ n : Nat, n < 5) := by\n  sorry",
    )


def _canned(monkeypatch, payloads: list[dict[str, Any]]):
    calls: list[str] = []

    def fake(code, *, cwd="", timeout_s=90):
        calls.append(code)
        return payloads[min(len(calls), len(payloads)) - 1]

    monkeypatch.setattr(np, "lean_scratch_check", fake)
    return calls


def test_plausible_classification(monkeypatch, tmp_path):
    path = _write(tmp_path, "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")
    cases = [
        (
            {
                "success": True,
                "ok": False,
                "messages": [{"severity": "error", "message": "Found problems! n := 5"}],
            },
            "counterexample",
        ),
        (
            {
                "success": True,
                "ok": False,
                "messages": [{"severity": "error", "message": "Gave up after 100 tries"}],
            },
            "gave_up",
        ),
        (
            {
                "success": True,
                "ok": False,
                "messages": [
                    {"severity": "error", "message": "Failed to create a `testable` instance"}
                ],
            },
            "not_testable",
        ),
        ({"success": True, "ok": True, "messages": []}, "passed_sampling"),
    ]
    for payload, expected in cases:
        _canned(monkeypatch, [payload])
        outcome = np.run_plausible_preprobe(path, "bad")
        assert outcome["verdict"] == expected
    _canned(monkeypatch, [cases[0][0]])
    assert "n := 5" in np.run_plausible_preprobe(path, "bad")["counterexample_text"]


def test_negation_attempt_requires_standard_axioms(monkeypatch):
    shape_ok = {
        "success": True,
        "ok": False,
        "messages": [{"severity": "warning", "message": "declaration uses 'sorry'"}],
    }
    proved = {
        "success": True,
        "ok": True,
        "messages": [
            {
                "severity": "info",
                "message": "'neg_bad' depends on axioms: [propext, Classical.choice]",
            }
        ],
    }
    _canned(monkeypatch, [shape_ok, proved])
    outcome = np.run_negation_attempt(_goal())
    assert outcome["verdict"] == "negation_proved"
    assert outcome["tactic"] == "decide"
    assert outcome["axioms_ok"] is True

    tainted = {
        "success": True,
        "ok": True,
        "messages": [
            {"severity": "info", "message": "'neg_bad' depends on axioms: [propext, sorryAx]"}
        ],
    }
    _canned(monkeypatch, [shape_ok, tainted])
    outcome = np.run_negation_attempt(_goal())
    assert outcome["verdict"] == "inconclusive"
    assert outcome["axioms_ok"] is False


def test_negation_attempt_tool_failure_is_probe_error(monkeypatch):
    _canned(monkeypatch, [{"success": False, "ok": False, "messages": [], "error": "REPL down"}])
    assert np.run_negation_attempt(_goal())["verdict"] == "probe_error"


def test_negation_attempt_ill_formed_statement(monkeypatch):
    broken = {
        "success": True,
        "ok": False,
        "messages": [{"severity": "error", "message": "unknown identifier 'Frobble'"}],
    }
    _canned(monkeypatch, [broken])
    outcome = np.run_negation_attempt(_goal())
    assert outcome["verdict"] == "ill_formed"


def test_negation_attempt_all_tactics_fail(monkeypatch):
    shape_ok = {"success": True, "ok": False, "messages": []}
    failing = {
        "success": True,
        "ok": False,
        "messages": [{"severity": "error", "message": "decide failed"}],
    }
    _canned(monkeypatch, [shape_ok, failing, failing, failing])
    assert np.run_negation_attempt(_goal())["verdict"] == "inconclusive"


# --- pipeline ----------------------------------------------------------------


@pytest.fixture()
def probe_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    import leanflow_cli.workflows.workflow_state as workflow_state

    monkeypatch.setattr(workflow_state, "append_workflow_outcome", lambda *a, **k: None)
    return tmp_path


def test_pipeline_disabled_and_budget(probe_env, monkeypatch, tmp_path):
    path = _write(tmp_path, "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")
    monkeypatch.setattr(
        np,
        "run_plausible_preprobe",
        lambda *a, **k: {"verdict": "counterexample", "counterexample_text": "n := 5"},
    )
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda goal, **k: {"verdict": "negation_proved", "tactic": "decide", "axioms_ok": True},
    )

    first = np.run_negation_probe(path, "bad")
    assert first["verdict"] == "negation_proved"
    assert first["plan_delta"][0]["status"] == "false"
    assert first["plan_delta"][0]["requires_promotion"] is True

    # Budget default 1: the second probe on the same theorem is a no-op.
    second = np.run_negation_probe(path, "bad")
    assert second["verdict"] == "budget_exhausted"

    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    assert np.run_negation_probe(path, "bad")["verdict"] == "disabled"


def test_pipeline_ill_formed_does_not_consume_budget(probe_env, monkeypatch, tmp_path):
    path = _write(tmp_path, "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "not_testable"})
    monkeypatch.setattr(
        np, "run_negation_attempt", lambda goal, **k: {"verdict": "ill_formed", "detail": "x"}
    )

    assert np.run_negation_probe(path, "bad")["verdict"] == "ill_formed"

    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda goal, **k: {"verdict": "inconclusive"},
    )
    # Budget untouched by the ill-formed attempt: a real probe still runs.
    assert np.run_negation_probe(path, "bad")["verdict"] == "inconclusive"


def test_pipeline_probe_error_consumes_budget(probe_env, monkeypatch, tmp_path):
    """Tool failures are recorded and budgeted — no infinite retry loop."""
    path = _write(tmp_path, "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "error"})
    monkeypatch.setattr(
        np, "run_negation_attempt", lambda goal, **k: {"verdict": "probe_error", "detail": "down"}
    )

    assert np.run_negation_probe(path, "bad")["verdict"] == "probe_error"
    assert np.run_negation_probe(path, "bad")["verdict"] == "budget_exhausted"


# --- runner trigger -----------------------------------------------------------


def test_runner_trigger_gates_on_flag_and_failures(monkeypatch, tmp_path):
    from leanflow_cli.native import native_runner as runner

    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *a, **k: pytest.fail("must not probe"),
    )
    state = {
        "failed_attempts": [
            {"attempt": 1, "target_symbol": "demo", "active_file": "Demo.lean", "reason": "r"}
        ]
    }
    # Flag off -> inert.
    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")
    # Flag on but below the failure threshold -> inert.
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")

    probed: list[tuple] = []
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda file, target, **k: probed.append((file, target)) or {"verdict": "inconclusive"},
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    state["failed_attempts"].append(
        {"attempt": 2, "target_symbol": "demo", "active_file": "Demo.lean", "reason": "r"}
    )
    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")
    assert probed == [("Demo.lean", "demo")]
