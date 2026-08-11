"""Characterize fail-closed reuse of an exact parent helper verification."""

from __future__ import annotations

import hashlib
import json

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.native import parent_helper_verification_reuse
from tools.implementations import lean_patch


def _sha256(text: str) -> str:
    """Return the exact UTF-8 source identity used by the test fixture."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ready_candidate(
    tmp_path,
    *,
    axioms: tuple[str, ...] = ("Classical.choice",),
    declaration_prefix: str = "",
    before: str = "theorem demo : True := by\n  sorry\n",
):
    """Stage one accepted parent helper check against the original source."""
    active = tmp_path / "Main.lean"
    declaration = declaration_prefix + "private lemma checked_family : True := by\n  trivial"
    active.write_text(before, encoding="utf-8")
    declaration_hash = _sha256(declaration)
    finding = {
        "job_id": "campaign.em-checked",
        "target_symbol": "demo",
        "active_file": str(active),
        "deliverable": {
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "active_file": str(active),
                    "anchor_target_symbol": "demo",
                    "declaration": declaration,
                    "declaration_sha256": declaration_hash,
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["checked_family"],
                    },
                }
            ],
        },
    }
    state = {
        "campaign_id": "campaign",
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": before.strip(),
        },
    }
    pending = runner.research_helper_candidate_priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert pending is not None
    expected = parent_helper_verification_reuse.expected_integrated_source(
        before,
        pending,
    )
    assert expected
    ready = runner.research_helper_candidate_priority.mark_parent_recheck(
        state,
        candidate_id=pending.candidate_id,
        status="accepted",
        source_revision_sha256=_sha256(before),
        expected_integrated_source_revision_sha256=_sha256(expected),
        axiom_profile_axioms=axioms,
    )
    assert ready is not None and ready.ready
    return active, before, expected, declaration, state, ready


def test_scoped_option_parent_helper_has_authenticated_insertion_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Bind a parent check to the exact source image including its option wrapper."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )

    _active, _before, expected, declaration, _state, ready = _ready_candidate(
        tmp_path,
        declaration_prefix="set_option maxRecDepth 100000 in\n",
    )

    assert expected.startswith(declaration)
    assert ready.expected_integrated_source_revision_sha256 == _sha256(expected)
    assert runner.research_helper_candidate_priority.parent_recheck_evidence_authenticated(ready)


def test_exact_parent_helper_insertion_reuses_gate_and_records_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Bank an exact insertion without compiling the identical helper twice."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    active, before, expected, _declaration, state, ready = _ready_candidate(tmp_path)
    active.write_text(expected, encoding="utf-8")

    class Agent:
        def __init__(self) -> None:
            self._managed_autonomy_state = state
            self._managed_pending_theorem_feedback = None

    agent = Agent()
    events: list[tuple[tuple, dict]] = []
    graph_syncs: list[str] = []
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda *_args, **_kwargs: pytest.fail(
            "exact cached helper triggered a second Lean compile"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_maybe_sync_plan_state",
        lambda *_args, **_kwargs: graph_syncs.append("synced") or True,
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    monkeypatch.setattr(
        runner.decomposer,
        "prover_edit_evidence_helper_names",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        runner.conditional_helper_progress,
        "deferred_helper_names",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.finite_branch_progress,
        "deferred_helper_names",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.banked_helper_inspection,
        "remember",
        lambda *_args, **_kwargs: None,
    )

    result = runner._record_helper_only_edit_progress(
        agent,
        target_symbol="demo",
        active_file=str(active),
        helper_names=(ready.helper_name,),
        verification_tool="patch",
        edit_before_source_revision_sha256=_sha256(before),
    )

    assert result.verified_any is True
    assert result.proof_progress is True
    outcome = runner._queue_manager_from_state(state).outcome_for(
        runner._queue_key(ready.helper_name, str(active))
    )
    assert outcome is not None and outcome.status == "solved"
    assert outcome.verification is not None
    assert outcome.verification.axiom_profile_checked is True
    assert outcome.verification.axiom_profile_axioms == ("Classical.choice",)
    assert graph_syncs == ["synced"]
    reused = next(
        kwargs for args, kwargs in events if args[0] == "queue-helper-parent-verification-reused"
    )
    assert reused["candidate_id"] == ready.candidate_id
    assert reused["lean_started"] is False
    assert reused["axiom_profile_axioms"] == ["Classical.choice"]


def test_helper_priority_authorizes_exact_atomic_patch_without_target_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Carry a parent helper check through the required atomic patch tool."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *_args, **_kwargs: None)
    active, _before, expected, declaration, state, ready = _ready_candidate(tmp_path)

    class Agent:
        _managed_autonomy_state = state

        @staticmethod
        def is_interrupted() -> bool:
            return False

    arguments = {
        "path": str(active),
        "theorem_id": "demo",
        "patch": (
            "*** Begin Patch\n"
            f"*** Update File: {active}\n"
            "@@\n"
            f"+{declaration.replace(chr(10), chr(10) + '+')}\n"
            "+\n"
            " theorem demo : True := by\n"
            "*** End Patch\n"
        ),
    }

    assert runner._managed_pre_tool_call(Agent(), "apply_verified_patch", arguments) is None
    token = str(arguments.get("_leanflow_verified_edit_authority", ""))
    assert token
    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **_kwargs: pytest.fail("exact parent helper insertion replayed Lean"),
    )
    payload = json.loads(
        lean_patch.apply_verified_patch_tool(
            str(active),
            str(arguments["patch"]),
            cwd=str(tmp_path),
            theorem_id="demo",
            verified_edit_authority_token=token,
        )
    )

    assert payload["success"] is True
    assert payload["authenticated_helper_insertion"] is True
    assert payload["verification"]["target"] == ready.helper_name
    assert active.read_text(encoding="utf-8") == expected


def test_exact_helper_patch_recovers_transiently_cleared_queue_assignment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Keep parent evidence when a step boundary temporarily clears queue identity."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_record_agent_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    active, _before, _expected, declaration, state, ready = _ready_candidate(tmp_path)
    state.pop("current_queue_assignment")

    class Agent:
        _managed_autonomy_state = state

        @staticmethod
        def is_interrupted() -> bool:
            return False

    arguments = {
        "path": str(active),
        "theorem_id": "demo",
        "patch": (
            "*** Begin Patch\n"
            f"*** Update File: {active}\n"
            "@@\n"
            f"+{declaration.replace(chr(10), chr(10) + '+')}\n"
            "+\n"
            " theorem demo : True := by\n"
            "*** End Patch\n"
        ),
    }

    assert runner._managed_pre_tool_call(Agent(), "apply_verified_patch", arguments) is None
    assert arguments.get("_leanflow_verified_edit_authority")
    assert state["current_queue_assignment"]["target_symbol"] == "demo"
    assert state["current_queue_assignment"]["active_file"] == str(active)
    recovered = next(
        kwargs for args, kwargs in events if args[1] == "research-helper-assignment-recovered"
    )
    assert recovered["candidate_id"] == ready.candidate_id
    assert recovered["lean_started"] is False


def test_helper_assignment_recovery_rejects_a_different_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Never infer queue identity from a patch aimed at another theorem."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    active, _before, _expected, declaration, state, _ready = _ready_candidate(tmp_path)
    state.pop("current_queue_assignment")
    arguments = {
        "path": str(active),
        "theorem_id": "other",
        "patch": declaration,
    }

    recovered = runner._recover_ready_helper_assignment_for_exact_patch(
        state,
        "apply_verified_patch",
        arguments,
    )

    assert recovered is None
    assert "current_queue_assignment" not in state


def test_helper_only_verified_patch_waits_for_authenticated_parent_recheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Never replay an unchanged parent while exact helper evidence is pending."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *_args, **_kwargs: None)
    boundary_agents: list[object] = []
    monkeypatch.setattr(
        runner,
        "_request_step_boundary_interrupt",
        lambda agent: boundary_agents.append(agent),
    )
    active, before, _expected, _declaration, state, ready = _ready_candidate(tmp_path)
    pending = runner.research_helper_candidate_priority.reset_for_source_change(
        state,
        candidate_id=ready.candidate_id,
    )
    assert pending is not None and not pending.ready
    patch = parent_helper_verification_reuse.exact_integrated_source_patch(
        before,
        pending,
        path=str(active),
    )
    assert patch

    class Agent:
        _managed_autonomy_state = state

        @staticmethod
        def is_interrupted() -> bool:
            return False

    agent = Agent()
    payload = json.loads(
        runner._managed_pre_tool_call(
            agent,
            "apply_verified_patch",
            {
                "path": str(active),
                "theorem_id": "demo",
                "patch": patch,
            },
        )
    )

    assert payload["status"] == "checked_helper_parent_recheck_pending"
    assert payload["helper_symbol"] == pending.helper_name
    assert payload["patch_applied"] is False
    assert payload["lean_started"] is False
    assert boundary_agents == [agent]
    assert agent._managed_step_boundary_closed is True
    assert active.read_text(encoding="utf-8") == before


def test_helper_priority_normalizes_model_patch_to_parent_checked_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Replace a guessed helper location with the exact checked source transition."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_record_agent_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    before = "set_option maxHeartbeats 1000000\n\ntheorem demo : True := by\n  sorry\n"
    active, _before, expected, declaration, state, ready = _ready_candidate(
        tmp_path,
        before=before,
    )
    for _ in range(runner.research_helper_candidate_priority.MAX_INTEGRATION_ATTEMPTS):
        runner.research_helper_candidate_priority.note_integration_attempt(
            state,
            candidate_id=ready.candidate_id,
        )
    assert not runner.research_helper_candidate_priority.load(state).integration_fence_active

    class Agent:
        _managed_autonomy_state = state

        @staticmethod
        def is_interrupted() -> bool:
            return False

    guessed_patch = (
        "*** Begin Patch\n"
        f"*** Update File: {active}\n"
        "@@\n"
        "-set_option maxHeartbeats 1000000\n"
        f"+{declaration.replace(chr(10), chr(10) + '+')}\n"
        "+\n"
        "+set_option maxHeartbeats 1000000\n"
        " theorem demo : True := by\n"
        "*** End Patch\n"
    )
    arguments = {
        "path": str(active),
        "theorem_id": "demo",
        "patch": guessed_patch,
    }

    assert runner._managed_pre_tool_call(Agent(), "apply_verified_patch", arguments) is None
    assert arguments["patch"] != guessed_patch
    normalized, error = runner.preview_v4a_update(str(arguments["patch"]), before)
    assert error is None
    assert normalized == expected
    token = str(arguments.get("_leanflow_verified_edit_authority", ""))
    assert token
    authorized = next(
        kwargs for args, kwargs in events if args[1] == "research-helper-verified-edit-authorized"
    )
    assert authorized["patch_normalized"] is True

    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **_kwargs: pytest.fail("normalized helper insertion replayed Lean"),
    )
    payload = json.loads(
        lean_patch.apply_verified_patch_tool(
            str(active),
            str(arguments["patch"]),
            cwd=str(tmp_path),
            theorem_id="demo",
            verified_edit_authority_token=token,
        )
    )

    assert payload["success"] is True
    assert payload["authenticated_helper_insertion"] is True
    assert payload["verification"]["target"] == ready.helper_name
    assert active.read_text(encoding="utf-8") == expected


def test_exact_helper_patch_ignores_preserved_commented_declaration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Build the authenticated insertion beside a live target, not its failed-attempt comment."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    before = (
        "-- LeanFlow failed attempt preserved.\n"
        "-- theorem demo : True := by\n"
        "--   sorry\n"
        "\n"
        "theorem demo : True := by\n"
        "  sorry\n"
    )
    active, _before, expected, _declaration, _state, ready = _ready_candidate(
        tmp_path,
        before=before,
    )

    patch = parent_helper_verification_reuse.exact_integrated_source_patch(
        before,
        ready,
        path=str(active),
    )
    preview, error = runner.preview_v4a_update(patch, before)

    assert patch
    assert error is None
    assert preview == expected


@pytest.mark.parametrize(
    ("mutation", "before_revision", "reason"),
    [
        ("exact", "stale", "pre_edit_source_changed"),
        ("reordered", "current", "integrated_source_changed"),
        ("extra", "current", "integrated_source_changed"),
    ],
)
def test_parent_helper_reuse_rejects_stale_reordered_or_extra_edits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    mutation: str,
    before_revision: str,
    reason: str,
) -> None:
    """Fall back whenever the managed edit is not the authenticated insertion image."""
    monkeypatch.setattr(
        runner.research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    active, before, expected, declaration, _state, ready = _ready_candidate(tmp_path)
    if mutation == "reordered":
        active.write_text(before + "\n" + declaration + "\n", encoding="utf-8")
    elif mutation == "extra":
        active.write_text(expected + "\n-- unrelated concurrent edit\n", encoding="utf-8")
    else:
        active.write_text(expected, encoding="utf-8")
    supplied_before_revision = _sha256(before)
    if before_revision == "stale":
        supplied_before_revision = _sha256(before + "\n-- stale snapshot")

    decision = parent_helper_verification_reuse.classify_reuse(
        ready,
        target_symbol="demo",
        active_file=str(active),
        edit_before_source_revision_sha256=supplied_before_revision,
        allowed_axioms=runner._allowed_axioms(),
    )

    assert decision.reusable is False
    assert decision.reason == reason
    assert decision.manager_check == {}
