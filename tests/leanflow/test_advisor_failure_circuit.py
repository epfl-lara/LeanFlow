"""Tests for the crash-durable unchanged-source advisor failure circuit."""

from __future__ import annotations

import json

from leanflow_cli.workflows import advisor_failure_circuit


def _configure_state_path(monkeypatch, tmp_path):
    state_root = tmp_path / ".leanflow" / "workflow-state"
    monkeypatch.setattr(advisor_failure_circuit, "workflow_state_root", lambda: state_root)
    return state_root


def test_failures_survive_process_local_state_and_block_third_call(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "same-source",
        "campaign_id": "campaign-1",
    }

    first = advisor_failure_circuit.observe_result(
        function_name="lean_reasoning_help",
        result_text=json.dumps({"success": False, "status": "error"}),
        **common,
    )
    second = advisor_failure_circuit.observe_result(
        function_name="lean_decompose_helpers",
        result_text=json.dumps({"success": False, "status": "unavailable"}),
        **common,
    )

    assert first.consecutive_failures == 1
    assert second.consecutive_failures == 2
    assert advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **common,
    )
    assert advisor_failure_circuit.load_snapshot().consecutive_failures == 2


def test_helper_only_source_change_keeps_durable_advisor_circuit_closed(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "old-source",
        "target_revision_sha256": "same-target",
        "campaign_id": "campaign-1",
    }
    for tool in ("lean_reasoning_help", "lean_decompose_helpers"):
        advisor_failure_circuit.observe_result(
            function_name=tool,
            result_text=json.dumps({"success": False, "status": "timeout"}),
            **common,
        )

    assert advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **{**common, "source_revision_sha256": "new-source"},
    )


def test_single_provider_timeout_exhausts_advisor_retry_budget(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "same-source",
        "target_revision_sha256": "same-target",
        "campaign_id": "campaign-1",
    }

    snapshot = advisor_failure_circuit.observe_result(
        function_name="lean_decompose_helpers",
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )

    assert snapshot.consecutive_failures == advisor_failure_circuit.FAILURE_THRESHOLD
    assert advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **common,
    )


def test_campaign_timeout_quarantine_survives_target_declaration_change(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "old-source",
        "target_revision_sha256": "old-target",
        "campaign_id": "campaign-1",
    }
    for tool in ("lean_reasoning_help", "lean_decompose_helpers"):
        advisor_failure_circuit.observe_result(
            function_name=tool,
            result_text=json.dumps({"success": False, "status": "timeout"}),
            **common,
        )

    assert advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **{
            **common,
            "source_revision_sha256": "new-source",
            "target_revision_sha256": "new-target",
        },
    )


def test_new_semantic_evidence_releases_campaign_timeout_quarantine(monkeypatch, tmp_path):
    """Give a changed verified-evidence context one fresh advisor budget."""
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "old-source",
        "target_revision_sha256": "same-target",
        "evidence_revision_sha256": "old-evidence",
        "campaign_id": "campaign-1",
    }
    advisor_failure_circuit.observe_result(
        function_name="lean_decompose_helpers",
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )

    assert advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **common,
    )
    assert not advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **{**common, "evidence_revision_sha256": "new-evidence"},
    )


def test_target_declaration_change_releases_non_timeout_advisor_failures(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "old-source",
        "target_revision_sha256": "old-target",
        "campaign_id": "campaign-1",
    }
    for tool in ("lean_reasoning_help", "lean_decompose_helpers"):
        advisor_failure_circuit.observe_result(
            function_name=tool,
            result_text=json.dumps({"success": False, "status": "unavailable"}),
            **common,
        )

    assert not advisor_failure_circuit.preflight_blocked(
        function_name="lean_reasoning_help",
        **{
            **common,
            "source_revision_sha256": "new-source",
            "target_revision_sha256": "new-target",
        },
    )


def test_successful_advisor_clears_matching_durable_circuit(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "same-source",
        "campaign_id": "campaign-1",
    }
    advisor_failure_circuit.observe_result(
        function_name="lean_reasoning_help",
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )
    cleared = advisor_failure_circuit.observe_result(
        function_name="lean_decompose_helpers",
        result_text=json.dumps({"success": True, "status": "completed"}),
        **common,
    )

    assert cleared.consecutive_failures == 0
    assert advisor_failure_circuit.load_snapshot().consecutive_failures == 0


def test_preflight_rejection_does_not_increment_timeout_budget(monkeypatch, tmp_path):
    _configure_state_path(monkeypatch, tmp_path)
    common = {
        "target_symbol": "result",
        "active_file": str(tmp_path / "Main.lean"),
        "source_revision_sha256": "same-source",
        "campaign_id": "campaign-1",
    }
    advisor_failure_circuit.observe_result(
        function_name="lean_reasoning_help",
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )
    unchanged = advisor_failure_circuit.observe_result(
        function_name="lean_decompose_helpers",
        result_text=json.dumps(
            {
                "success": False,
                "status": "advisor_retry_exhausted",
                "provider_called": False,
            }
        ),
        **common,
    )

    assert unchanged.consecutive_failures == advisor_failure_circuit.FAILURE_THRESHOLD


def test_completed_call_is_charged_to_its_preflight_source_revision(tmp_path):
    state: dict = {}
    active = str(tmp_path / "Main.lean")
    advisor_failure_circuit.remember_call_source(
        state,
        function_name="lean_reasoning_help",
        target_symbol="result",
        active_file=active,
        source_revision_sha256="source-before-call",
        target_revision_sha256="target-before-call",
        evidence_revision_sha256="evidence-before-call",
    )

    identity = advisor_failure_circuit.consume_call_identity(
        state,
        function_name="lean_reasoning_help",
        target_symbol="result",
        active_file=active,
        fallback_source_revision_sha256="source-after-worker-edit",
        fallback_target_revision_sha256="target-after-worker-edit",
        fallback_evidence_revision_sha256="evidence-after-worker-edit",
    )

    assert identity.source_revision_sha256 == "source-before-call"
    assert identity.target_revision_sha256 == "target-before-call"
    assert identity.evidence_revision_sha256 == "evidence-before-call"
    assert advisor_failure_circuit.PENDING_STATE_KEY not in state
