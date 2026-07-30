"""Phase 3 §5 tests: negation-goal construction, classification, budgeted pipeline."""

from __future__ import annotations

import hashlib
import threading
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


def test_spent_budget_reuses_compatible_probe_and_retires_exact_route(
    probe_env, monkeypatch, tmp_path
):
    """A concurrent/current probe cannot strand a selected negate marker."""
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.orchestrator import OrchestratorRoute

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "compatible-spent-negate")
    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "not_testable"})
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda *_args, **_kwargs: {"verdict": "inconclusive"},
    )
    assert np.run_negation_probe(path, "bad", trigger="concurrent-job")["verdict"] == "inconclusive"

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: events.append((event, details)),
    )
    state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "bad", "active_file": path},
        "_orchestrator_last_ctx": {"target_symbol": "bad", "active_file": path},
        "failed_attempts": [
            {
                "attempt": attempt,
                "target_symbol": "bad",
                "active_file": path,
                "reason": "kernel rejection",
            }
            for attempt in (1, 2)
        ],
    }
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="bad",
        active_file=path,
        trigger="event",
        reserve_inflight=True,
    )
    route = OrchestratorRoute(route="negate", reason="test the exact current declaration")

    assert runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "continue"
    assert not runner.campaign_epoch.campaign_snapshot().get("inflight_route")
    execution = state["_negation_route_execution"]
    assert execution["status"] == "completed"
    assert execution["probe_recorded"] is True
    probe_events = [details for event, details in events if event == "negation-probe"]
    assert len(probe_events) == 1
    assert probe_events[0]["recovered"] is True
    assert probe_events[0]["reused_after_budget_exhaustion"] is True


def test_spent_budget_stale_signature_retires_route_without_reusing_old_probe(
    probe_env, monkeypatch, tmp_path
):
    """Old probe evidence cannot discharge a route after statement drift."""
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.orchestrator import OrchestratorRoute

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "stale-spent-negate")
    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "not_testable"})
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda *_args, **_kwargs: {"verdict": "inconclusive"},
    )
    assert np.run_negation_probe(path, "bad", trigger="old-job")["verdict"] == "inconclusive"
    Path(path).write_text("theorem bad : False := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "bad", "active_file": path},
        "_orchestrator_last_ctx": {"target_symbol": "bad", "active_file": path},
        "failed_attempts": [
            {
                "attempt": attempt,
                "target_symbol": "bad",
                "active_file": path,
                "reason": "kernel rejection",
            }
            for attempt in (1, 2)
        ],
    }
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="bad",
        active_file=path,
        trigger="event",
        reserve_inflight=True,
    )
    marker = dict(runner.campaign_epoch.campaign_snapshot()["inflight_route"])

    assert (
        runner._apply_orchestrator_route_with_completion(
            OrchestratorRoute(route="negate", reason="statement changed"),
            [],
            state,
            {},
        )
        == "continue"
    )
    assert marker["token"]
    assert not runner.campaign_epoch.campaign_snapshot().get("inflight_route")
    assert state["_negation_route_execution"]["probe_recorded"] is False
    assert state["_negation_route_execution"]["evidence_kind"] == "negate-route-obstacle"


def test_reused_negation_proof_still_runs_authoritative_promotion(probe_env, monkeypatch, tmp_path):
    """Signature-compatible recovery never bypasses the promotion gate."""
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.negation_promotion import PromotionResult

    path = _write(tmp_path, "theorem bad : False := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "counterexample"})
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda *_args, **_kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms_ok": True,
        },
    )
    assert (
        np.run_negation_probe(path, "bad", trigger="concurrent-job")["verdict"] == "negation_proved"
    )

    promotions: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_negation_reconciliation_barrier", lambda _state: False)

    def promote(entry, **_kwargs):
        promotions.append(dict(entry))
        return PromotionResult(True, "authoritatively promoted", node_id="n-bad")

    monkeypatch.setattr(runner.negation_promotion, "promote_negation", promote)
    state: dict[str, Any] = {}
    execution = runner._maybe_negation_probe(
        state,
        target_symbol="bad",
        active_file=path,
        force=True,
        trigger="orchestrator-explicit-route",
        selected_at=datetime.now(UTC).isoformat(),
    )

    assert len(promotions) == 1
    assert execution.completed is True
    assert execution.promotion_recorded is True
    assert state["negation_promotion"]["ok"] is True


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


def test_outcome_append_failure_replays_exact_persisted_probe_once(
    probe_env, monkeypatch, tmp_path
):
    """A filled summary row survives an outcome-stream crash without a stuck route."""
    import leanflow_cli.workflows.workflow_state as workflow_state
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.orchestrator import OrchestratorRoute

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "negation-outcome-race")
    path = _write(tmp_path, "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n")
    monkeypatch.setattr(np, "run_plausible_preprobe", lambda *a, **k: {"verdict": "not_testable"})
    monkeypatch.setattr(
        np,
        "run_negation_attempt",
        lambda goal, **k: {"verdict": "inconclusive"},
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    reason = "requested route: negate; n = 5 is the counterexample"
    target = {
        "target_symbol": "bad",
        "active_file": path,
        "prover_requested_route": "negate",
        "prover_request_reason": reason,
    }
    state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "bad",
            "active_file": path,
        },
        "_orchestrator_last_ctx": {
            "target_symbol": "bad",
            "active_file": path,
        },
    }
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="bad",
        active_file=path,
        trigger="event",
        route_target=target,
        reserve_inflight=True,
    )
    monkeypatch.setattr(
        workflow_state,
        "append_workflow_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outcome append failed")),
    )

    with pytest.raises(RuntimeError, match="outcome append failed"):
        np.run_negation_probe(
            path,
            "bad",
            trigger="orchestrator-explicit-route",
            route_reason=reason,
        )

    monkeypatch.setattr(workflow_state, "append_workflow_outcome", lambda *a, **k: None)
    route = OrchestratorRoute(route="negate", reason="explicit evidence", target=target)

    assert runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "continue"
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()
    assert state["_negation_route_execution"]["probe_recorded"] is True
    # With the durable selection consumed, an old spent row cannot masquerade
    # as fresh work for a second application. The exhausted action retires as a
    # route obstacle instead of remaining an in-flight replay forever.
    assert runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "continue"
    assert state["_negation_route_execution"]["probe_recorded"] is False
    assert state["_negation_route_execution"]["evidence_kind"] == "negate-route-obstacle"


def test_bare_crashed_reservation_surfaces_infrastructure_pause(probe_env, monkeypatch, tmp_path):
    """A legacy reservation without ownership time never counts as route work."""
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.queue_models import TheoremKey
    from leanflow_cli.workflows.workflow_json_io import update_json_file
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    path = _write(tmp_path, "theorem bad : True := by\n  sorry\n")
    storage_key = TheoremKey.make("bad", path).storage_key()

    def seed(summary):
        summary["negation_probes"] = [
            {"key": storage_key, "reservation": "legacy", "status": "reserved"}
        ]

    update_json_file(workflow_state_root() / "summary.json", seed)
    state: dict[str, Any] = {}
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    execution = runner._maybe_negation_probe(
        state,
        target_symbol="bad",
        active_file=path,
        force=True,
        trigger="orchestrator-explicit-route",
        selected_at=datetime.now(UTC).isoformat(),
    )

    assert execution.completed is False
    assert execution.verdict == "reservation_orphaned"
    assert state["operational_pause"] == "paused_infrastructure"


# --- runner trigger -----------------------------------------------------------


def test_runner_trigger_gates_on_flag_and_failures(monkeypatch, tmp_path):
    from leanflow_cli.native import native_runner as runner

    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
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
    disabled = runner._maybe_negation_probe(
        state,
        target_symbol="demo",
        active_file="Demo.lean",
    )
    assert disabled.status == "deferred"
    # Flag on but below the failure threshold -> inert.
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    below_threshold = runner._maybe_negation_probe(
        state,
        target_symbol="demo",
        active_file="Demo.lean",
    )
    assert below_threshold.status == "deferred"
    assert "requires 2 failed attempts; observed 1" in below_threshold.reason

    probed: list[tuple] = []
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda file, target, **k: probed.append((file, target)) or {"verdict": "inconclusive"},
    )
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    state["failed_attempts"].append(
        {"attempt": 2, "target_symbol": "demo", "active_file": "Demo.lean", "reason": "r"}
    )
    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")
    assert probed == [("Demo.lean", "demo")]


def test_explicit_negation_route_bypasses_only_failure_threshold(monkeypatch, tmp_path):
    """An exact prover request runs at attempt zero and preserves its evidence."""
    from leanflow_cli.native import native_runner as runner

    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    def run_probe(file_path, theorem_id, **kwargs):
        calls.append((file_path, theorem_id, dict(kwargs)))
        return {
            "verdict": "inconclusive",
            "probe_entry": {
                "theorem": theorem_id,
                "file": file_path,
                "negation": {"verdict": "inconclusive"},
            },
        }

    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_AFTER_FAILURES", "2")
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner.negation_probe, "run_negation_probe", run_probe)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: events.append((event, details)),
    )

    evidence = "requested route: negate; s = 3 contradicts the universal helper"
    execution = runner._maybe_negation_probe(
        {},
        target_symbol="demo",
        active_file=str(active),
        force=True,
        trigger="orchestrator-explicit-route",
        route_reason=evidence,
    )

    assert execution.completed is True
    assert execution.probe_recorded is True
    assert execution.explicit_request is True
    assert len(calls) == 1
    assert calls[0][2]["trigger"] == "orchestrator-explicit-route"
    assert calls[0][2]["route_reason"] == evidence
    assert any(event == "negation-probe" for event, _details in events)


def test_explicit_negation_probe_exception_is_structured_deferred(monkeypatch, tmp_path):
    """A probe crash remains resumable instead of looking like route completion."""
    from leanflow_cli.native import native_runner as runner

    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe crashed")),
    )
    monkeypatch.setattr(runner, "_reconcile_promotion_runtime_exception", lambda *a, **k: None)
    events: list[str] = []
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, *_args, **_kwargs: events.append(event),
    )

    execution = runner._maybe_negation_probe(
        {},
        target_symbol="demo",
        active_file=str(tmp_path / "Demo.lean"),
        force=True,
        trigger="orchestrator-explicit-route",
    )

    assert execution.status == "deferred"
    assert execution.completed is False
    assert execution.reason == "probe execution raised RuntimeError"
    assert events == ["negation-probe-deferred"]


def test_ill_formed_probe_retires_route_and_releases_rollover(probe_env, monkeypatch, tmp_path):
    """A deterministic unsupported probe must not replay across every epoch."""
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.orchestrator import OrchestratorRoute

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "ill-formed-negate-route")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *_args, **_kwargs: {
            "verdict": "ill_formed",
            "detail": "section variables are unavailable in scratch",
        },
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: events.append((event, details)),
    )
    state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
        "_orchestrator_last_ctx": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="demo",
        active_file=str(active),
        trigger="event",
        reserve_inflight=True,
    )
    runner.campaign_epoch.request_rollover(
        state,
        runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
    )

    action = runner._apply_orchestrator_route_with_completion(
        OrchestratorRoute(route="negate", reason="test feasibility"),
        [],
        state,
        {},
    )

    assert action == "continue"
    assert not runner.campaign_epoch.campaign_snapshot().get("inflight_route")
    assert runner._consume_ready_campaign_rollover(state, {}) == (
        runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )
    execution = state["_negation_route_execution"]
    assert execution["status"] == "completed"
    assert execution["outcome"] == "ill_formed"
    assert execution["evidence_kind"] == "negate-route-obstacle"
    assert [event for event, _details in events] == [
        "negation-probe",
        "negation-route-obstacle",
    ]


def test_runner_negation_probe_keeps_parent_portfolio_maintenance_alive(monkeypatch, tmp_path):
    """A slow foreground probe must not starve dispatch result reaping."""
    from leanflow_cli.native import native_runner as runner

    caller = threading.get_ident()
    maintained = threading.Event()
    action_thread_ids: list[int] = []
    poll_thread_ids: list[int] = []
    state = {
        "failed_attempts": [
            {
                "attempt": attempt,
                "target_symbol": "demo",
                "active_file": "Demo.lean",
                "reason": "failed",
            }
            for attempt in (1, 2)
        ]
    }

    def run_probe(*_args, **_kwargs):
        action_thread_ids.append(threading.get_ident())
        assert maintained.wait(timeout=2)
        return {"verdict": "inconclusive"}

    def maintain(autonomy_state, live_state):
        assert autonomy_state is state
        assert live_state == {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        }
        poll_thread_ids.append(threading.get_ident())
        maintained.set()

    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.negation_probe, "run_negation_probe", run_probe)
    monkeypatch.setattr(runner, "_maintain_research_portfolio", maintain)
    monkeypatch.setattr(runner, "_research_portfolio_parent_poll_interval_s", lambda: 0.01)

    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")

    assert action_thread_ids and action_thread_ids[0] != caller
    assert poll_thread_ids == [caller]


def test_runner_main_negation_promotion_sets_terminal_outcome(monkeypatch, tmp_path):
    from leanflow_cli.native import native_runner as runner
    from leanflow_cli.workflows.negation_promotion import PromotionResult

    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_AFTER_FAILURES", "2")
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(runner.campaign_epoch, "record_status", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *a, **k: {
            "verdict": "negation_proved",
            "probe_entry": {"theorem": "demo", "file": "Demo.lean"},
        },
    )
    monkeypatch.setattr(
        runner.negation_promotion,
        "promote_negation",
        lambda *a, **k: PromotionResult(
            True,
            "promoted",
            node_id="n-demo",
            is_main_goal=True,
        ),
    )
    state = {
        "failed_attempts": [
            {
                "attempt": attempt,
                "target_symbol": "demo",
                "active_file": "Demo.lean",
                "reason": "failed",
            }
            for attempt in (1, 2)
        ]
    }

    runner._maybe_negation_probe(state, target_symbol="demo", active_file="Demo.lean")

    assert state["terminal_outcome"] == "disproved"
    assert state["negation_promotion"]["is_main_goal"] is True
    assert runner._autonomous_stop_reason([], {}, state) == "disproved"
