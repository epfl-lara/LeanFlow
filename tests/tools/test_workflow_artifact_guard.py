"""Characterize model-facing managed-workflow artifact guards."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.implementations.file_operations import (
    PatchResult,
    ReadResult,
    ShellFileOperations,
    WriteResult,
)
from tools.implementations.file_tools import (
    patch_tool,
    read_file_tool,
    search_tool,
    write_file_tool,
)
from tools.utilities.workflow_artifact_guard import (
    WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV,
    generated_plan_view,
    is_live_workflow_log_path,
    is_managed_machine_snapshot_path,
    is_managed_plan_path,
    is_workflow_state_path,
    workflow_log_read_error,
    workflow_machine_snapshot_read_error,
    workflow_plan_pagination_error,
    workflow_state_search_error,
)


@pytest.mark.parametrize(
    "path",
    [
        ".leanflow/workflow-state/latest-run.log",
        "/project/.leanflow/workflow-state/activity/runs/prove-1.jsonl",
        r"C:\project\.leanflow\workflow-state\runs\prove-1.log",
        ".leanflow/workflow-state/journal.jsonl",
    ],
)
def test_live_workflow_transcripts_are_recognized(path):
    assert is_workflow_state_path(path)
    assert is_live_workflow_log_path(path)


def test_non_transcript_workflow_snapshot_is_not_a_log():
    path = ".leanflow/workflow-state/summary.json"

    assert is_workflow_state_path(path)
    assert not is_live_workflow_log_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".leanflow/workflow-state/summary.json",
        "/project/.leanflow/workflow-state/blueprint.json",
    ],
)
def test_large_machine_snapshots_are_recognized(path):
    assert is_managed_machine_snapshot_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".leanflow/workflow-state/plan.md",
        "/project/.leanflow/workflow-state/plan.md",
        "/home/user/.leanflow/workflow-state/plan.md",
    ],
)
def test_managed_plan_paths_are_recognized(path):
    assert is_managed_plan_path(path)


def test_plan_state_override_path_is_recognized(monkeypatch, tmp_path):
    state_dir = tmp_path / "custom-plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))

    assert is_managed_plan_path(str(state_dir / "plan.md"))
    assert not is_managed_plan_path(str(state_dir / "other-plan.md"))


@pytest.mark.parametrize(
    "text",
    [
        "# Proving Plan\n\n## Strategy\n\n- current\n\n## Notes\n\nSTALE INVENTORY",
        "     1|# Proving Plan\n     2|## Strategy\n     3|- current\n     4|## Notes\n     5|STALE INVENTORY",
    ],
)
def test_generated_plan_view_excludes_raw_and_numbered_notes(text):
    view = generated_plan_view(text)

    assert "Strategy" in view
    assert "STALE INVENTORY" not in view
    assert "## Notes" not in view


def test_generated_plan_view_is_bounded_while_preserving_both_ends():
    text = "GOAL\n" + ("x" * 20_000) + "\nFINAL REPORT\n## Notes\nSTALE"

    view = generated_plan_view(text, max_chars=1_000)

    assert len(view) == 1_000
    assert view.startswith("GOAL")
    assert view.endswith("FINAL REPORT")
    assert "generated plan projection" in view
    assert "source_chars=20018" in view
    assert "returned_chars=1000" in view
    assert "omitted_source_chars=" in view
    assert "sha256=" in view
    assert "historical_notes_excluded=true" in view
    assert "STALE" not in view


def test_generated_plan_view_prioritizes_current_truth_before_large_grounding():
    text = (
        "# Proving Plan\n\n"
        "## Goal\n\nprove current_target\n\n"
        "## Current state\n\nstated: 1\n\n"
        "## Strategy\n\n- current orchestrator route: `decompose` for `current_target`\n\n"
        "## Frontier\n\n- `current_target` (Demo.lean)\n\n"
        "## Grounding\n\n"
        + ("- historical finding " + ("x" * 400) + "\n") * 100
        + "\n## Decision log\n\n- latest route event\n\n"
        "## Final report\n\n- status: in-progress\n\n"
        "## Notes\n\nSTALE INVENTORY"
    )

    view = generated_plan_view(text)

    assert len(view) == 8_000
    assert "prove current_target" in view
    assert "current orchestrator route: `decompose` for `current_target`" in view
    assert "## Frontier" in view
    assert "`current_target` (Demo.lean)" in view
    assert "## Decision log" in view
    assert "## Final report" in view
    assert "omitted_source_chars=" in view
    assert "STALE INVENTORY" not in view


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_note_replace_is_denied_without_touching_user_history(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    path = ".leanflow/workflow-state/plan.md"

    class StatefulPlanOps:
        def __init__(self):
            self.raw = (
                "# Proving Plan\n\n## Final report\n\n- status: in-progress\n\n"
                "## Notes\n\nKEEP THIS HISTORICAL USER NOTE\n"
            )

        def read_raw(self, _path):
            return self.raw

        def patch_replace(self, _path, old, new, _replace_all):
            self.raw = self.raw.replace(old, new, 1)
            return PatchResult(success=True, files_modified=[path])

        def write_file(self, _path, content):
            self.raw = content
            return WriteResult(bytes_written=len(content))

    ops = StatefulPlanOps()
    mock_get.return_value = ops

    payloads = [
        json.loads(
            patch_tool(
                mode="replace",
                path=path,
                old_string="- status: in-progress\n",
                new_string=("- status: in-progress\n\n## Notes\n\n- newly appended agent note\n"),
                task_id=f"managed-plan-note-patch-{attempt}",
            )
        )
        for attempt in range(2)
    ]

    assert all(payload["success"] is False for payload in payloads)
    assert all(payload["status"] == "managed_plan_patch_denied" for payload in payloads)
    assert all("read-only" in payload["error"] for payload in payloads)
    assert ops.raw.endswith("## Notes\n\nKEEP THIS HISTORICAL USER NOTE\n")
    assert "newly appended agent note" not in ops.raw
    mock_get.assert_not_called()
    assert "KEEP THIS HISTORICAL USER NOTE" in ops.raw


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_note_v4a_is_denied_without_touching_user_history(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    path = ".leanflow/workflow-state/plan.md"

    class StatefulPlanOps:
        def __init__(self):
            self.raw = (
                "# Proving Plan\n\n## Final report\n\n- status: in-progress\n\n"
                "## Notes\n\nKEEP THIS HISTORICAL USER NOTE\n"
            )

        def read_raw(self, _path):
            return self.raw

        def patch_v4a(self, _patch):
            self.raw = self.raw.replace(
                "- status: in-progress\n",
                "- status: in-progress\n\n## Notes\n\n- newly appended agent note\n",
                1,
            )
            return PatchResult(success=True, files_modified=[path])

        def write_file(self, _path, content):
            self.raw = content
            return WriteResult(bytes_written=len(content))

    ops = StatefulPlanOps()
    mock_get.return_value = ops
    v4a = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        " - status: in-progress\n"
        "+\n"
        "+## Notes\n"
        "+\n"
        "+- newly appended agent note\n"
        "*** End Patch"
    )

    payload = json.loads(
        patch_tool(
            mode="patch",
            patch=v4a,
            task_id="managed-plan-v4a-note-patch",
        )
    )

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_operation_denied"
    assert "read-only" in payload["error"]
    assert ops.raw.endswith("## Notes\n\nKEEP THIS HISTORICAL USER NOTE\n")
    assert "newly appended agent note" not in ops.raw
    mock_get.assert_not_called()
    assert "KEEP THIS HISTORICAL USER NOTE" in ops.raw


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_generated_section_patch_is_denied_preflight(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    path = ".leanflow/workflow-state/plan.md"

    class StatefulPlanOps:
        def __init__(self):
            self.raw = "# Proving Plan\n\n## Strategy\n\n- generated\n\n## Notes\n\nKEEP ME\n"

        def read_raw(self, _path):
            return self.raw

        def patch_replace(self, _path, old, new, _replace_all):
            self.raw = self.raw.replace(old, new, 1)
            return PatchResult(success=True, files_modified=[path])

        def write_file(self, _path, content):
            self.raw = content
            return WriteResult(bytes_written=len(content))

    ops = StatefulPlanOps()
    before = ops.raw
    mock_get.return_value = ops

    payload = json.loads(
        patch_tool(
            mode="replace",
            path=path,
            old_string="- generated",
            new_string="- model rewrite",
            task_id="managed-plan-generated-edit",
        )
    )

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_patch_denied"
    assert "read-only" in payload["error"]
    assert ops.raw == before
    mock_get.assert_not_called()


def test_managed_plan_patch_denial_preserves_exact_bytes_during_interrupt_state(
    monkeypatch, tmp_path
):
    """Deny before backend mutation even when an interrupt is already possible."""
    from tools.environments.local import LocalEnvironment
    from tools.utilities.interrupt import is_interrupted, set_interrupt

    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    state_dir = tmp_path / ".leanflow" / "workflow-state"
    state_dir.mkdir(parents=True)
    path = state_dir / "plan.md"
    before = b"# Proving Plan\n\n## Strategy\n\n- generated\n\n## Notes\n\nKEEP ME\n"
    path.write_bytes(before)
    env = LocalEnvironment(cwd=str(tmp_path))

    class InterruptAfterPatchOps(ShellFileOperations):
        def read_raw(self, requested_path):
            if is_interrupted():
                return None
            return super().read_raw(requested_path)

        def patch_replace(self, *args, **kwargs):
            result = super().patch_replace(*args, **kwargs)
            set_interrupt(True)
            return result

    ops = InterruptAfterPatchOps(env, cwd=str(tmp_path))
    set_interrupt(False)
    try:
        with patch("tools.implementations.file_tools._get_file_ops", return_value=ops):
            payload = json.loads(
                patch_tool(
                    mode="replace",
                    path=str(path),
                    old_string="- generated",
                    new_string="- interrupted model rewrite",
                    task_id="interrupted-managed-plan-reconciliation",
                )
            )
    finally:
        set_interrupt(False)
        env.cleanup()

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_patch_denied"
    assert "read-only" in payload["error"]
    assert "__LEANFLOW_FENCE" not in payload["error"]
    assert "logout" not in payload["error"].lower()
    assert path.read_bytes() == before


@patch("tools.implementations.file_tools._get_file_ops")
def test_v4a_with_managed_plan_is_denied_before_any_partial_failure(mock_get, monkeypatch):
    """Deny the aggregate patch before an earlier operation can mutate state."""
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    path = ".leanflow/workflow-state/plan.md"

    class PartialFailurePlanOps:
        def __init__(self):
            self.raw = "# Proving Plan\n\n## Strategy\n\n- generated\n\n## Notes\n\nKEEP ME\n"

        def read_raw(self, _path):
            return self.raw

        def patch_v4a(self, _patch):
            self.raw = self.raw.replace("- generated", "- model rewrite")
            return PatchResult(
                success=False,
                files_modified=[path],
                error="Failed to update Later.lean: synthetic later failure",
            )

        def write_file(self, _path, content):
            self.raw = content
            return WriteResult(bytes_written=len(content))

    ops = PartialFailurePlanOps()
    before = ops.raw
    mock_get.return_value = ops
    v4a = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        "-- generated\n"
        "+- model rewrite\n"
        "*** Update File: Later.lean\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch"
    )

    payload = json.loads(patch_tool(mode="patch", patch=v4a, task_id="partial-plan-failure"))

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_operation_denied"
    assert "read-only" in payload["error"]
    assert ops.raw == before
    mock_get.assert_not_called()


@patch("tools.implementations.file_tools._get_file_ops")
def test_v4a_managed_plan_denial_leaves_backend_untouched(mock_get, monkeypatch):
    """Reject a managed-plan update before invoking the aggregate backend."""
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    path = ".leanflow/workflow-state/plan.md"

    class UntouchedFailurePlanOps:
        def __init__(self):
            self.raw = "# Proving Plan\n\n## Notes\n\nKEEP ME\n"
            self.write_calls = 0

        def read_raw(self, _path):
            return self.raw

        def patch_v4a(self, _patch):
            return PatchResult(success=False, error="synthetic early failure")

        def write_file(self, _path, content):
            self.write_calls += 1
            self.raw = content
            return WriteResult(bytes_written=len(content))

    ops = UntouchedFailurePlanOps()
    before = ops.raw
    mock_get.return_value = ops
    v4a = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        "-missing\n"
        "+replacement\n"
        "*** End Patch"
    )

    payload = json.loads(patch_tool(mode="patch", patch=v4a, task_id="untouched-plan-failure"))

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_operation_denied"
    assert "read-only" in payload["error"]
    assert ops.raw == before
    assert ops.write_calls == 0
    mock_get.assert_not_called()


@pytest.mark.parametrize(
    "forbidden_operation",
    [
        "*** Add File: .leanflow/workflow-state/plan.md\n+replacement",
        "*** Delete File: .leanflow/workflow-state/plan.md",
        (
            "*** Move File: .leanflow/workflow-state/plan.md -> "
            ".leanflow/workflow-state/old-plan.md"
        ),
        "*** Move File: scratch.md -> .leanflow/workflow-state/plan.md",
    ],
)
@patch("tools.implementations.file_tools._get_file_ops")
def test_destructive_managed_plan_v4a_operation_is_denied_preflight(
    mock_get, forbidden_operation, monkeypatch
):
    """Scan the complete patch before an earlier ordinary edit can execute."""
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    ordinary_path = "ordinary.txt"
    v4a = (
        "*** Begin Patch\n"
        f"*** Update File: {ordinary_path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        f"{forbidden_operation}\n"
        "*** End Patch"
    )

    payload = json.loads(patch_tool(mode="patch", patch=v4a, task_id="plan-preflight"))

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_operation_denied"
    assert "read-only" in payload["error"]
    mock_get.assert_not_called()


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_full_write_is_rejected_before_backend_access(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    payload = json.loads(
        write_file_tool(
            ".leanflow/workflow-state/plan.md",
            "# replacement that would erase historical notes\n",
            task_id="managed-plan-write",
        )
    )

    assert payload["success"] is False
    assert payload["status"] == "managed_plan_write_denied"
    assert "read-only" in payload["error"]
    mock_get.assert_not_called()


@patch("tools.implementations.file_tools._get_file_ops")
def test_explicit_diagnostic_mode_can_patch_managed_plan_for_operator_audit(mock_get, monkeypatch):
    """Keep the model guard distinguishable from explicit operator diagnostics."""
    monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "1")
    file_ops = mock_get.return_value
    file_ops.read_raw.return_value = "# Proving Plan\n\n## Notes\n\noperator note\n"
    file_ops.patch_replace.return_value = PatchResult(
        success=True,
        files_modified=[".leanflow/workflow-state/plan.md"],
    )

    payload = json.loads(
        patch_tool(
            mode="replace",
            path=".leanflow/workflow-state/plan.md",
            old_string="operator note",
            new_string="audited operator note",
            task_id="diagnostic-plan-patch",
        )
    )

    assert payload["success"] is True
    file_ops.patch_replace.assert_called_once()


def test_latest_run_basename_is_blocked_defensively():
    assert is_live_workflow_log_path("logs/latest-run.log")


@patch("tools.implementations.file_tools._get_file_ops")
def test_read_file_rejects_live_self_log_before_opening_environment(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    payload = json.loads(
        read_file_tool(".leanflow/workflow-state/latest-run.log", task_id="recursion-read")
    )

    assert payload["workflow_log_blocked"] is True
    assert "own prior output" in payload["error"]
    mock_get.assert_not_called()


@patch("tools.implementations.file_tools._get_file_ops")
def test_search_rejects_explicit_workflow_state_before_opening_environment(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    payload = json.loads(
        search_tool(
            "prior proof attempt",
            path=".leanflow/workflow-state",
            task_id="recursion-search",
        )
    )

    assert payload["workflow_state_blocked"] is True
    assert "structured campaign context" in payload["error"]
    mock_get.assert_not_called()


@pytest.mark.parametrize("name", ["summary.json", "blueprint.json"])
@patch("tools.implementations.file_tools._get_file_ops")
def test_machine_snapshot_read_is_rejected_before_opening_environment(mock_get, monkeypatch, name):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    payload = json.loads(
        read_file_tool(
            f".leanflow/workflow-state/{name}",
            task_id="blocked-machine-snapshot-read",
        )
    )

    assert payload["workflow_snapshot_blocked"] is True
    assert "machine snapshots" in payload["error"]
    assert "Lean source/kernel diagnostics" in payload["error"]
    mock_get.assert_not_called()


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_read_returns_only_read_only_generated_sections(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    content = (
        "     1|# Proving Plan\n"
        "     2|## Strategy\n"
        "     3|- current route\n"
        "     4|## Notes\n"
        "     5|STALE INVENTORY: solve old_helper"
    )
    mock_get.return_value.read_file.return_value = ReadResult(
        content=content,
        total_lines=5,
        file_size=len(content),
    )

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/plan.md",
            limit=240,
            task_id="generated-plan-read",
        )
    )

    assert "current route" in payload["content"]
    assert "STALE INVENTORY" not in payload["content"]
    assert "## Notes" not in payload["content"]
    assert payload["managed_plan_view"] is True
    assert payload["historical_notes_excluded"] is True
    assert payload["truncated"] is False
    assert "Read-only managed plan view" in payload["hint"]
    assert "model writes to plan.md are blocked" in payload["hint"]
    assert "historical user Notes are excluded" in payload["hint"]


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_read_caps_large_grounding_at_eight_thousand_chars(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
    content = (
        "# Proving Plan\n\n## Goal\n\nprove target\n\n"
        "## Strategy\n\n- current route\n\n"
        "## Frontier\n\n- `target` (Demo.lean)\n\n"
        "## Grounding\n\n" + ("x" * 20_000) + "\n\n## Final report\n\n- status: in-progress\n\n"
        "## Notes\n\nHISTORICAL"
    )
    mock_get.return_value.read_file.return_value = ReadResult(
        content=content,
        total_lines=25,
        file_size=len(content),
    )

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/plan.md",
            limit=240,
            task_id="bounded-generated-plan-read",
        )
    )

    assert len(payload["content"]) == 8_000
    assert "prove target" in payload["content"]
    assert "current route" in payload["content"]
    assert "`target` (Demo.lean)" in payload["content"]
    assert "## Final report" in payload["content"]
    assert "returned_chars=8000" in payload["content"]
    assert "omitted_source_chars=" in payload["content"]
    assert "HISTORICAL" not in payload["content"]


@patch("tools.implementations.file_tools._get_file_ops")
def test_managed_plan_pagination_is_rejected_before_opening_environment(mock_get, monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/plan.md",
            offset=241,
            limit=240,
            task_id="notes-pagination",
        )
    )

    assert payload["workflow_plan_blocked"] is True
    assert "historical user-owned context" in payload["error"]
    mock_get.assert_not_called()


def test_plan_pagination_error_names_current_authorities(monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    error = workflow_plan_pagination_error(".leanflow/workflow-state/plan.md", 2)

    assert "queue assignment" in str(error)
    assert "Lean source/kernel diagnostics" in str(error)


def test_snapshot_error_names_bounded_structured_alternatives(monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    error = workflow_machine_snapshot_read_error(".leanflow/workflow-state/summary.json")

    assert "dependency-graph digest" in str(error)
    assert "completed-finding handoff" in str(error)


@patch("tools.implementations.file_tools._get_file_ops")
def test_explicit_diagnostic_mode_allows_operator_log_read(mock_get, monkeypatch):
    monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "1")
    result = MagicMock()
    result.content = "operator diagnostic"
    result.error = None
    result.to_dict.return_value = {"content": result.content}
    mock_get.return_value.read_file.return_value = result

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/latest-run.log",
            task_id="diagnostic-log-read",
        )
    )

    assert payload["content"] == "operator diagnostic"


@patch("tools.implementations.file_tools._get_file_ops")
def test_explicit_diagnostic_mode_allows_raw_plan_pagination(mock_get, monkeypatch):
    monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "1")
    content = "   241|## Notes\n   242|operator diagnostic"
    mock_get.return_value.read_file.return_value = ReadResult(
        content=content,
        total_lines=242,
        file_size=len(content),
    )

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/plan.md",
            offset=241,
            limit=20,
            task_id="diagnostic-plan-read",
        )
    )

    assert "operator diagnostic" in payload["content"]
    assert "managed_plan_view" not in payload


@patch("tools.implementations.file_tools._get_file_ops")
def test_explicit_diagnostic_mode_allows_raw_machine_snapshot(mock_get, monkeypatch):
    monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "1")
    content = '{"historical_jobs": [1, 2, 3]}'
    mock_get.return_value.read_file.return_value = ReadResult(
        content=content,
        total_lines=1,
        file_size=len(content),
    )

    payload = json.loads(
        read_file_tool(
            ".leanflow/workflow-state/summary.json",
            task_id="diagnostic-summary-read",
        )
    )

    assert "historical_jobs" in payload["content"]
    assert "workflow_snapshot_blocked" not in payload


def test_guard_errors_name_the_explicit_diagnostic_escape_hatch(monkeypatch):
    monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)

    read_error = workflow_log_read_error(".leanflow/workflow-state/latest-run.log")
    search_error = workflow_state_search_error(".leanflow/workflow-state")

    assert WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV in str(read_error)
    assert WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV in str(search_error)
