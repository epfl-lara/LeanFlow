"""Exercise durable repetition bounds against actual scratch tool observations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.session_progress import SessionProgress
from leanflow_cli.workflows.prover.session_tools import SessionTools


def make_guard(tmp_path: Path, role: str = "research") -> SessionProgress:
    """Create or reopen the guard for the same private job workspace."""
    return SessionProgress(tmp_path / ".runtime/job", role=role, workspace=tmp_path / "job")


def test_repeated_same_passage_warns_then_hands_off(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    source = workspace / "paper.md"
    source.write_text("Read this same passage again.")
    tools = SessionTools(role="research", project_root=tmp_path, workspace=workspace, context={})
    guard = make_guard(tmp_path)
    notices = []
    for _ in range(6):
        args = {"path": "paper.md"}
        result = tools.invoke("read_file", args)
        assert result["success"]
        notice = guard.observe("read_file", args, result)
        if notice:
            notices.append((notice.action, notice.count))
    assert notices == [("warn", 3), ("report", 6)]
    assert guard.report_only
    assert "helpers are invalid" in guard.report_reason


def test_semantic_read_defaults_and_equivalent_paths_share_history(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    result = {"success": True, "content": "the same", "next_offset": 8}
    requests: list[dict[str, Any]] = [
        {"path": "paper.md"},
        {"limit": 8000, "offset": 0, "path": str(tmp_path / "job/paper.md")},
        {"offset": -1, "path": "./paper.md", "limit": "8000"},
    ]
    for args in requests:
        notice = guard.observe("read_file", args, result)
    assert notice is not None and notice.action == "warn"


def test_changed_read_result_resets_only_its_own_count(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    args = {"path": "paper.md"}
    for _ in range(5):
        guard.observe("read_file", args, {"success": True, "content": "before"})
    assert guard.observe("read_file", args, {"success": True, "content": "after"}) is None
    assert not guard.report_only
    assert guard.observe("read_file", args, {"success": True, "content": "after"}) is None
    notice = guard.observe("read_file", args, {"success": True, "content": "after"})
    assert notice is not None and notice.count == 3


def test_file_change_allows_a_fresh_read_even_when_window_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("same prefix\nold ending")
    tools = SessionTools(
        role="research", project_root=tmp_path, workspace=tmp_path / "job", context={}
    )
    guard = make_guard(tmp_path)
    args = {"path": str(source), "limit": 4}
    for _ in range(5):
        result = tools.invoke("read_file", args)
        guard.observe("read_file", args, result)
    previous_mtime = source.stat().st_mtime_ns
    source.write_text("same prefix\nnew ending")
    os.utime(source, ns=(previous_mtime + 10_000, previous_mtime + 10_000))
    assert tools.invoke("read_file", args) == result
    assert guard.observe("read_file", args, result) is None
    assert not guard.report_only


def test_noop_write_to_read_target_does_not_reset_its_history(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    source = workspace / "notes.md"
    source.write_text("Existing findings")
    tools = SessionTools(role="research", project_root=tmp_path, workspace=workspace, context={})
    guard = make_guard(tmp_path)
    for _ in range(6):
        read_args = {"path": "notes.md"}
        notice = guard.observe("read_file", read_args, tools.invoke("read_file", read_args))
        write_args = {"path": "notes.md", "content": "Existing findings"}
        guard.observe("write_file", write_args, tools.invoke("write_file", write_args))
    assert notice is not None and notice.action == "report"


def test_meaningful_scratch_write_allows_a_fresh_read(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    source = workspace / "notes.md"
    source.write_text("same prefix\nold ending")
    tools = SessionTools(role="research", project_root=tmp_path, workspace=workspace, context={})
    guard = make_guard(tmp_path)
    args = {"path": "notes.md", "limit": 4}
    for _ in range(5):
        result = tools.invoke("read_file", args)
        guard.observe("read_file", args, result)
    write_args = {"path": "notes.md", "old": "old ending", "new": "new ending"}
    changed = tools.invoke("replace_text", write_args)
    assert changed["success"]
    guard.observe("replace_text", write_args, changed)
    assert tools.invoke("read_file", args) == result
    assert guard.observe("read_file", args, result) is None
    assert not guard.report_only


def test_interleaved_and_batched_observations_do_not_reset_repetition(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    notices = []
    # Two batches of three repeated reads, interleaved with other useful tools and
    # no-op writes, still provide no new evidence for this particular request.
    for batch in range(2):
        for index in range(3):
            notice = guard.observe("read_file", {"path": "paper.md"}, {"content": "same"})
            if notice:
                notices.append(notice.action)
            guard.observe("write_file", {"path": "notes.md", "content": "same"}, {"success": True})
            guard.observe("compute", {"program": f"print({batch * 3 + index})"}, {"output": index})
    assert notices == ["warn", "report"]


def test_pagination_and_distinct_results_are_not_a_loop(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for index in range(20):
        assert (
            guard.observe("read_file", {"path": "paper.md", "offset": index}, {"content": "x"})
            is None
        )
        assert guard.observe("compute", {"program": "print(1)"}, {"output": str(index)}) is None
    assert not guard.report_only


@pytest.mark.parametrize("role", ["orchestrator", "planner", "review", "reviewer", "research"])
def test_research_roles_resume_counts_and_report_handoff(tmp_path: Path, role: str) -> None:
    guard = make_guard(tmp_path, role)
    for _ in range(2):
        guard.observe("read_file", {"path": "paper.md"}, {"content": "same"})
    guard = make_guard(tmp_path, role)
    warning = guard.observe("read_file", {"path": "paper.md"}, {"content": "same"})
    assert warning is not None and warning.action == "warn"
    for _ in range(3):
        guard.observe("read_file", {"path": "paper.md"}, {"content": "same"})
    resumed = make_guard(tmp_path, role)
    assert resumed.report_only
    assert resumed.observe("read_file", {"path": "paper.md"}, {"content": "changed"}) is None
    assert resumed.report_only
    fresh = SessionProgress(
        tmp_path / ".runtime/another", role=role, workspace=tmp_path / "another"
    )
    assert not fresh.report_only


@pytest.mark.parametrize("role", ["prover", "negation", "unknown"])
def test_proving_and_unrecognized_roles_are_unaffected(tmp_path: Path, role: str) -> None:
    guard = make_guard(tmp_path, role)
    for _ in range(20):
        assert guard.observe("read_file", {"path": "x.lean"}, {"content": "sorry"}) is None
    assert not guard.report_only
    assert not guard.path.exists()


def test_computation_syntax_and_timing_noise_cannot_hide_repetition(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for index in range(6):
        program = "print(1)" if index % 2 else "# explain\nprint( 1 )\n"
        notice = guard.observe(
            "compute", {"program": program}, {"success": True, "output": "1", "elapsed_s": index}
        )
    assert notice is not None and notice.action == "report"


def test_fetch_bookkeeping_is_not_new_evidence_but_changed_hash_is(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for index in range(5):
        guard.observe(
            "fetch_resource",
            {"url": " https://example.org/paper ", "filename": f"copy{index}.md"},
            {
                "success": True,
                "sha256": "same",
                "path": f"copy{index}.md",
                "retrieved_at": str(index),
            },
        )
    assert not guard.report_only
    assert (
        guard.observe(
            "fetch_resource",
            {"url": "https://example.org/paper"},
            {"success": True, "sha256": "new"},
        )
        is None
    )
    assert not guard.report_only


def test_identical_tool_failures_lead_to_report_without_a_math_verdict(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for _ in range(6):
        notice = guard.observe(
            "compute", {"program": "bad syntax !"}, {"success": False, "error": "denied"}
        )
    assert notice is not None and notice.action == "report"
    assert "does not establish" in notice.message


def test_state_is_bounded_and_contains_only_hashes_and_counters(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for index in range(300):
        guard.observe(
            "web_search", {"query": f"private-query-token-{index}"}, {"result": "private evidence"}
        )
    raw = guard.path.read_text()
    saved = json.loads(raw)
    assert len(saved["observations"]) == 256
    assert len(raw) < 256_000
    assert "private-query" not in raw
    assert "private evidence" not in raw
    assert str(tmp_path) not in raw


@pytest.mark.parametrize("corruption", ["{truncated", "[]", '{"version": 999}', "x" * 256_001])
def test_invalid_saved_guard_cannot_refund_repetition_allowance(
    tmp_path: Path, corruption: str
) -> None:
    guard = make_guard(tmp_path)
    guard.path.parent.mkdir(parents=True)
    guard.path.write_text(corruption)
    resumed = make_guard(tmp_path)
    assert resumed.report_only
    assert "could not be validated" in resumed.report_reason
