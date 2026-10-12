"""Phase 3 §5 tests: negation-goal construction, classification, budgeted pipeline."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def test_build_negation_preserves_type_level_let_bindings(tmp_path):
    text = (
        "theorem bad (t : Nat) :\n"
        "    let n := 840 * t + 361\n"
        "    let x : Nat := n + 1\n"
        "    0 < x := by\n"
        "  let proofLocal := t + 1\n"
        "  omega\n"
    )
    path = _write(tmp_path, text)

    goal = np.build_negation_goal(path, "bad")

    assert isinstance(goal, NegationGoal)
    assert goal.result_type == ("let n := 840 * t + 361\n" "    let x : Nat := n + 1\n" "    0 < x")
    assert goal.prop.startswith("∀ (t : Nat), let n := 840 * t + 361")
    assert "proofLocal" not in goal.prop


def test_build_negation_preserves_type_level_lets_before_term_proof(tmp_path):
    signature = (
        "theorem bad (t : Nat) :\n"
        "    let n := 840 * t + 361\n"
        "    let x : Nat := n + 1\n"
        "    0 < x"
    )
    path = _write(tmp_path, f"{signature} := dependentTermProof\n")

    goal = np.build_negation_goal(path, "bad")

    assert isinstance(goal, NegationGoal)
    assert goal.original == signature
    assert goal.result_type == ("let n := 840 * t + 361\n" "    let x : Nat := n + 1\n" "    0 < x")


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


def _persist_probe_rows(rows: list[dict[str, Any]]) -> None:
    from leanflow_cli.workflows.workflow_json_io import update_json_file
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    def seed(summary: dict[str, Any]) -> None:
        summary["negation_probes"] = [dict(row) for row in rows]

    update_json_file(workflow_state_root() / "summary.json", seed)


def _completed_probe_row(
    path: str,
    theorem: str,
    signature: str,
    reservation: str,
) -> dict[str, Any]:
    from leanflow_cli.workflows.queue_models import TheoremKey

    canonical = str(Path(path).resolve())
    return {
        "key": TheoremKey.make(theorem, canonical).storage_key(),
        "reservation": reservation,
        "job_id": "unrelated-old-trigger",
        "route_reason": "unrelated old route reason",
        "theorem": theorem,
        "file": canonical,
        "plausible": {"verdict": "not_testable"},
        "negation": {"verdict": "inconclusive"},
        "promotion_evidence": {
            "declaration_signature_sha256": signature,
        },
        "timestamp": "malformed-and-irrelevant",
    }


def test_recover_latest_compatible_probe_uses_current_declaration_signature(probe_env, tmp_path):
    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    goal = np.build_negation_goal(path, "bad")
    assert isinstance(goal, NegationGoal)
    signature = hashlib.sha256(goal.original.encode("utf-8")).hexdigest()
    older = _completed_probe_row(path, "bad", signature, "older")
    latest = _completed_probe_row(path, "bad", signature, "latest")
    latest["negation"] = {
        "verdict": "negation_proved",
        "tactic": "decide",
        "axioms_ok": True,
    }
    _persist_probe_rows([older, latest])

    recovered = np.recover_latest_compatible_probe(path, "bad")

    assert recovered is not None
    assert recovered["recovered"] is True
    assert recovered["reservation"] == "latest"
    assert recovered["verdict"] == "negation_proved"
    assert recovered["probe_entry"]["job_id"] == "unrelated-old-trigger"
    assert recovered["plan_delta"][0]["requires_promotion"] is True


def test_recover_latest_compatible_probe_rejects_stale_signature(probe_env, tmp_path):
    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    goal = np.build_negation_goal(path, "bad")
    assert isinstance(goal, NegationGoal)
    signature = hashlib.sha256(goal.original.encode("utf-8")).hexdigest()
    _persist_probe_rows([_completed_probe_row(path, "bad", signature, "stale")])
    Path(path).write_text("theorem bad : False := by\n  sorry\n", encoding="utf-8")

    assert np.recover_latest_compatible_probe(path, "bad") is None


def test_recover_latest_compatible_probe_rejects_bare_reservation(probe_env, tmp_path):
    from leanflow_cli.workflows.queue_models import TheoremKey

    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    goal = np.build_negation_goal(path, "bad")
    assert isinstance(goal, NegationGoal)
    signature = hashlib.sha256(goal.original.encode("utf-8")).hexdigest()
    canonical = str(Path(path).resolve())
    _persist_probe_rows(
        [
            {
                "key": TheoremKey.make("bad", canonical).storage_key(),
                "reservation": "bare",
                "status": "reserved",
                "theorem": "bad",
                "file": canonical,
                "promotion_evidence": {
                    "declaration_signature_sha256": signature,
                },
            }
        ]
    )

    assert np.recover_latest_compatible_probe(path, "bad") is None


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

    route_reason = "requested route: negate; n = 5 is the concrete counterexample"
    first = np.run_negation_probe(path, "bad", route_reason=route_reason)
    assert first["verdict"] == "negation_proved"
    assert first["plan_delta"][0]["status"] == "false"
    assert first["plan_delta"][0]["requires_promotion"] is True
    evidence = first["probe_entry"]["promotion_evidence"]
    assert evidence["declaration_signature_sha256"]
    assert evidence["source_revision_sha256"]
    assert evidence["negation_prop"] == "∀ n : Nat, n < 5"
    assert evidence["proof_tactic"] == "decide"
    assert first["probe_entry"]["route_reason"] == route_reason

    # Budget default 1: the second probe on the same theorem is a no-op.
    second = np.run_negation_probe(path, "bad")
    assert second["verdict"] == "budget_exhausted"

    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    assert np.run_negation_probe(path, "bad")["verdict"] == "disabled"


def test_remaining_probe_budget_counts_completed_and_reserved_rows(monkeypatch):
    """Route preflight must count exactly the rows the locked gate reserves."""
    now = datetime(2026, 1, 2, tzinfo=UTC)
    stale_age = np.probe_reservation_stale_s()
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "5")
    probes = [
        {"key": "Demo.lean::demo", "negation": {"verdict": "inconclusive"}},
        {
            "key": "Demo.lean::demo",
            "status": "reserved",
            "reserved_at": now.isoformat(),
        },
        {
            "key": "Demo.lean::demo",
            "status": "reserved",
            "reserved_at": (now - timedelta(seconds=stale_age + 1)).isoformat(),
        },
        {"key": "Demo.lean::demo", "status": "reserved"},
        {
            "key": "Demo.lean::demo",
            "status": "reserved",
            "reserved_at": "malformed",
        },
        {"key": "Demo.lean::other", "negation": {"verdict": "inconclusive"}},
        "malformed",
    ]

    assert np.remaining_probe_budget(probes, "Demo.lean::demo", now=now) == 1
    assert np.remaining_probe_budget(probes, "Demo.lean::demo", budget=4, now=now) == 0
    assert np.remaining_probe_budget(None, "Demo.lean::demo", budget=1, now=now) == 1


def test_probe_reservation_stale_age_never_undercuts_full_ladder(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_TIMEOUT_S", "120")
    monkeypatch.setenv("LEANFLOW_NEGATION_RESERVATION_STALE_S", "10")

    assert np.probe_reservation_stale_s() == 960

    monkeypatch.setenv("LEANFLOW_NEGATION_RESERVATION_STALE_S", "1200")
    assert np.probe_reservation_stale_s() == 1200


def test_pipeline_reclaims_the_same_stale_reservation_budget_ignores(
    probe_env, monkeypatch, tmp_path
):
    from leanflow_cli.workflows.queue_models import TheoremKey
    from leanflow_cli.workflows.workflow_json_io import read_json_file
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    storage_key = TheoremKey.make("bad", path).storage_key()
    stale_at = datetime.now(UTC) - timedelta(seconds=np.probe_reservation_stale_s() + 1)
    stale = {
        "key": storage_key,
        "reservation": "stale",
        "status": "reserved",
        "reserved_at": stale_at.isoformat(),
    }
    _persist_probe_rows([stale])
    monkeypatch.setattr(
        np,
        "run_plausible_preprobe",
        lambda *_args, **_kwargs: {"verdict": "not_testable"},
    )
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda *_args, **_kwargs: {"verdict": "inconclusive"},
    )

    assert np.remaining_probe_budget([stale], storage_key, budget=1) == 1
    assert np.run_negation_probe(path, "bad")["verdict"] == "inconclusive"
    rows = read_json_file(workflow_state_root() / "summary.json")["negation_probes"]
    assert all(row.get("reservation") != "stale" for row in rows)


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


def test_pipeline_prefill_exception_releases_own_reservation_for_immediate_retry(
    probe_env, monkeypatch, tmp_path
):
    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    attempts = 0

    def plausible(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("plausible crashed")
        return {"verdict": "not_testable"}

    monkeypatch.setattr(np, "run_plausible_preprobe", plausible)
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda *_args, **_kwargs: {"verdict": "inconclusive"},
    )

    with pytest.raises(RuntimeError, match="plausible crashed"):
        np.run_negation_probe(path, "bad")

    assert np.run_negation_probe(path, "bad")["verdict"] == "inconclusive"
    assert attempts == 2


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
