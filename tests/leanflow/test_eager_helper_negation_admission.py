"""Characterize eager negation admission after parent-verified helper edits."""

from __future__ import annotations

import pytest

from leanflow_cli.native import native_runner as runner


def _accepted_helper_check(active_file: str, helper_name: str) -> tuple[dict, str]:
    """Return one manager result that proves the exact helper without axioms."""
    return (
        {
            "ok": True,
            "target": helper_name,
            "axiom_profile_checked": True,
            "axiom_profile_blockers": [],
            "incremental": {
                "success": True,
                "ok": True,
                "target": helper_name,
                "file": active_file,
                "has_errors": False,
                "has_sorry": False,
            },
        },
        "lean_incremental_check",
    )


def test_positive_coverage_helper_ignores_stale_negate_route_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Do not compile a main-target negation after positive helper progress."""
    active = tmp_path / "242.lean"
    helper = "erdos_242_schinzel_residue_ray"
    active.write_text(
        f"lemma {helper} : True := by simp\n" "theorem erdos_242 : True := by\n  sorry\n",
        encoding="utf-8",
    )
    exact_route = {
        "route": "negate",
        "target_symbol": "erdos_242",
        "active_file": str(active),
    }

    class Agent:
        def __init__(self) -> None:
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "erdos_242",
                    "active_file": str(active),
                },
                "orchestrator_current_route": "negate",
                "campaign_inflight_route": dict(exact_route),
                "campaign_epoch_route_selection": dict(exact_route),
                "prover_requested_route": dict(exact_route),
            }

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda _file, name, **_kwargs: _accepted_helper_check(str(active), name),
    )
    monkeypatch.setattr(runner, "_record_theorem_outcome", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: False)
    monkeypatch.setattr(runner.banked_helper_inspection, "remember", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_verified_counterexample_evidence_for_assignment",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        runner,
        "_promote_source_negation_candidate",
        lambda *_args, **_kwargs: pytest.fail(
            "stale negate metadata admitted an eager exact-source compile"
        ),
    )

    result = runner._record_helper_only_edit_progress(
        Agent(),
        target_symbol="erdos_242",
        active_file=str(active),
        helper_names=(helper,),
        verification_tool="patch",
        evidence_helper_names=(helper,),
    )

    assert result.verified_any is True
    assert result.proof_progress is False
    assert result.step_boundary_closed is False
    admission = next(
        kwargs for args, kwargs in events if args[0] == "queue-helper-negation-promotion-admission"
    )
    assert admission["admitted"] is False
    assert admission["provenance"] == "no-authenticated-exact-counterexample"
    assert admission["observed_current_route"] == "negate"
    assert set(admission["observed_negate_route_keys"]) == {
        "campaign_epoch_route_selection",
        "campaign_inflight_route",
        "orchestrator_current_route",
        "prover_requested_route",
    }


def test_authenticated_exact_counterexample_keeps_eager_helper_promotion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Keep immediate promotion for exact parent-verified counterexample evidence."""
    active = tmp_path / "Main.lean"
    helper = "not_demo"
    active.write_text(
        f"lemma {helper} : ¬ True := by simp\n" "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )

    class Agent:
        def __init__(self) -> None:
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                }
            }

    events: list[tuple[tuple, dict]] = []
    promotions: list[str] = []
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda _file, name, **_kwargs: _accepted_helper_check(str(active), name),
    )
    monkeypatch.setattr(runner, "_record_theorem_outcome", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner,
        "_verified_counterexample_evidence_for_assignment",
        lambda **_kwargs: ({"name": helper, "node_id": "counterexample-node"},),
    )
    monkeypatch.setattr(
        runner,
        "_promote_source_negation_candidate",
        lambda _agent, **kwargs: promotions.append(kwargs["proof_declaration"]) or True,
    )

    result = runner._record_helper_only_edit_progress(
        Agent(),
        target_symbol="demo",
        active_file=str(active),
        helper_names=(helper,),
        verification_tool="patch",
        evidence_helper_names=(helper,),
    )

    assert result.step_boundary_closed is True
    assert promotions == [helper]
    admission = next(
        kwargs for args, kwargs in events if args[0] == "queue-helper-negation-promotion-admission"
    )
    assert admission["admitted"] is True
    assert admission["provenance"] == "verified-counterexample-evidence"
