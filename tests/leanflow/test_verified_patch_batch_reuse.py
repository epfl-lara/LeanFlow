"""Tests for source-bound verified-patch declaration evidence reuse."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.native import verified_patch_batch_reuse as reuse


def _sha256(text: str) -> str:
    """Return the exact UTF-8 source digest used by fixtures."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch_result(active, source: str, *, theorem_id: str = "target") -> dict:
    """Return one authenticated successful file-exact tool result."""
    return {
        "success": True,
        "status": "patch_elaborated",
        "path": str(active),
        "theorem_id": theorem_id,
        "check_mode": "file_exact",
        "check_passed": True,
        "verified_source_revision_sha256": _sha256(source),
        "verification_source_unchanged": True,
        "verification": {
            "ok": True,
            "mode": "file_exact",
            "target": str(active),
            "output": f"{active}:2:3: warning: helper cleanup\n",
        },
    }


def _report(active, target: str, axioms=("Classical.choice",)):
    """Return one complete axiom profile for a requested declaration."""
    return SimpleNamespace(
        target=target,
        file_path=str(active),
        inspection_succeeded=True,
        axioms=list(axioms),
    )


def test_exact_patch_and_complete_batch_build_target_and_helper_checks(tmp_path) -> None:
    """Reuse one full-file compile plus one all-target axiom harness."""
    active = tmp_path / "Main.lean"
    source = (
        "private lemma helper : True := by\n"
        "  trivial\n\n"
        "theorem target : True := by\n"
        "  exact helper\n"
    )
    active.write_text(source, encoding="utf-8")
    calls: list[tuple[tuple[str, ...], str]] = []

    def inspect(targets, path):
        calls.append((tuple(targets), path))
        return {target: _report(active, target) for target in targets}

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("helper", "target"),
        allowed_axioms={"Classical.choice", "propext", "Quot.sound"},
        inspect_axioms_many=inspect,
    )

    assert decision.reusable is True
    assert decision.reason == "exact_verified_patch_batch"
    assert calls == [(("helper", "target"), str(active))]
    assert set(decision.checks) == {"helper", "target"}
    helper = decision.checks["helper"]
    assert helper["ok"] is True
    assert helper["mode"] == "incremental_target"
    assert helper["source_sha256"] == _sha256(source)
    assert helper["axiom_profile_checked"] is True
    assert helper["axiom_profile_axioms"] == ["Classical.choice"]
    assert helper["axiom_profile_blockers"] == []
    assert helper["warnings"] == 1
    assert len(str(helper["declaration_sha256"])) == 64


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("source", "verified_source_changed"),
        ("path", "active_file_changed"),
        ("theorem", "assignment_target_changed"),
        ("mode", "broad_scope_not_file_exact"),
    ],
)
def test_stale_or_tampered_patch_identity_falls_back_without_batch(
    tmp_path,
    mutation: str,
    reason: str,
) -> None:
    """Never profile declarations when broad evidence is not the current edit."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  trivial\n"
    active.write_text(source, encoding="utf-8")
    payload = _patch_result(active, source)
    active_file = str(active)
    assignment_target = "target"
    if mutation == "source":
        active.write_text(source + "\n-- changed\n", encoding="utf-8")
    elif mutation == "path":
        payload["path"] = str(tmp_path / "Other.lean")
    elif mutation == "theorem":
        payload["theorem_id"] = "other"
    elif mutation == "mode":
        payload["check_mode"] = "module"
    calls: list[object] = []

    decision = reuse.build_reusable_checks(
        payload,
        active_file=active_file,
        assignment_target=assignment_target,
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda *_args: calls.append(object()) or {},
    )

    assert decision.reusable is False
    assert decision.reason == reason
    assert decision.checks == {}
    assert decision.axiom_batch_started is False
    assert calls == []


def test_placeholder_declaration_cannot_borrow_broad_file_success(tmp_path) -> None:
    """Reject a broad compile that succeeded only because Lean admits a placeholder."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  admit\n"
    active.write_text(source, encoding="utf-8")
    called = False

    def inspect(_targets, _path):
        nonlocal called
        called = True
        return {}

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=inspect,
    )

    assert decision.reusable is False
    assert decision.reason == "declaration_has_placeholder:target"
    assert called is False


def test_partial_axiom_batch_falls_back_for_every_declaration(tmp_path) -> None:
    """Do not reuse any check from a partial all-or-nothing profile batch."""
    active = tmp_path / "Main.lean"
    source = (
        "private lemma helper : True := by trivial\n" "theorem target : True := by exact helper\n"
    )
    active.write_text(source, encoding="utf-8")

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("helper", "target"),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda _targets, _path: {"helper": _report(active, "helper")},
    )

    assert decision.reusable is False
    assert decision.reason == "axiom_batch_incomplete"
    assert decision.checks == {}
    assert decision.axiom_batch_started is True


def test_disallowed_axiom_preserves_negative_exact_verdict(tmp_path) -> None:
    """Reuse elaboration while rejecting a declaration outside current policy."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  trivial\n"
    active.write_text(source, encoding="utf-8")

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda _targets, _path: {
            "target": _report(active, "target", ("Classical.choice", "Unsafe.custom"))
        },
    )

    assert decision.reusable is True
    check = decision.checks["target"]
    assert check["ok"] is False
    assert check["axiom_profile_checked"] is True
    assert check["axiom_profile_blockers"] == ["Unsafe.custom"]
    assert check["errors"] == 1


def test_managed_apply_uses_one_batch_for_helper_and_target(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feed both post-edit gates from one batch without individual Lean calls."""
    active = tmp_path / "Main.lean"
    source = (
        "private lemma helper : True := by trivial\n" "theorem target : True := by exact helper\n"
    )
    active.write_text(source, encoding="utf-8")
    payload = _patch_result(active, source)
    batch_calls: list[tuple[str, ...]] = []
    helper_checks: list[dict[str, object]] = []
    target_checks: list[dict[str, object]] = []

    class Agent:
        def __init__(self) -> None:
            self._session_messages = []
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "target",
                    "active_file": str(active),
                    "slice": "theorem target : True := by sorry",
                }
            }
            self._managed_pending_theorem_feedback = None

        def is_interrupted(self) -> bool:
            return False

    def inspect_many(targets, *, file_path):
        batch_calls.append(tuple(targets))
        return {target: _report(active, target) for target in targets}

    def record_helpers(_agent, **kwargs):
        helper_checks.append(dict(kwargs["verified_patch_checks"]))
        return runner._ManagedHelperEditResult(verified_any=True, proof_progress=True)

    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_poll_research_portfolio_after_tool_result", lambda *_: None)
    monkeypatch.setattr(runner, "_refresh_live_queue_source_after_managed_edit", lambda *_: True)
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_reset_search_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "lean_axioms_many", inspect_many)
    monkeypatch.setattr(runner, "_record_helper_only_edit_progress", record_helpers)
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda *_args, **_kwargs: pytest.fail("helper triggered an individual Lean gate"),
    )
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda *_args, **_kwargs: pytest.fail("target triggered an individual Lean gate"),
    )
    monkeypatch.setattr(
        runner,
        "_finish_queue_step_boundary",
        lambda _agent, **kwargs: target_checks.append(dict(kwargs["manager_verification"])),
    )

    runner._handle_managed_tool_result(
        Agent(),
        "apply_verified_patch",
        {"path": str(active), "theorem_id": "target", "check_mode": "file_exact"},
        __import__("json").dumps(payload),
        queue_edit_accepted=True,
        queue_assignment_changed=True,
        queue_helper_candidates=("helper",),
    )

    assert batch_calls == [("helper", "target")]
    assert len(helper_checks) == 1
    assert set(helper_checks[0]) == {"helper", "target"}
    assert len(target_checks) == 1
    assert target_checks[0]["target"] == "target"
    assert target_checks[0]["axiom_profile_source"] == "verified_patch_batch"


def test_helper_progress_consumes_batched_check_without_new_lean(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bank a helper directly from the shared post-patch evidence map."""
    active = tmp_path / "Main.lean"
    source = "private lemma helper : True := by trivial\n" "theorem target : True := by sorry\n"
    active.write_text(source, encoding="utf-8")
    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("helper",),
        allowed_axioms={"Classical.choice", "propext", "Quot.sound"},
        inspect_axioms_many=lambda targets, _path: {
            target: _report(active, target) for target in targets
        },
    )
    assert decision.reusable is True

    class Agent:
        def __init__(self) -> None:
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "target",
                    "active_file": str(active),
                }
            }
            self._managed_pending_theorem_feedback = None

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda *_args, **_kwargs: pytest.fail("batched helper started another Lean gate"),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "prover_edit_evidence_helper_names",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: False)
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(runner.banked_helper_inspection, "remember", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        runner,
        "_verified_counterexample_evidence_for_assignment",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        runner.source_negation_candidates,
        "observed_negate_route_keys",
        lambda *_args, **_kwargs: (),
    )

    result = runner._record_helper_only_edit_progress(
        Agent(),
        target_symbol="target",
        active_file=str(active),
        helper_names=("helper",),
        verification_tool="apply_verified_patch",
        assigned_changed=False,
        verified_patch_checks=decision.checks,
    )

    assert result.verified_any is True
    assert result.proof_progress is True
    reused_event = next(
        kwargs for args, kwargs in events if args[0] == "queue-helper-verified-patch-batch-reused"
    )
    assert reused_event["helper_symbol"] == "helper"
    assert reused_event["lean_started"] is False
