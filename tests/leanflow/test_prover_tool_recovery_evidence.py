"""Verify that exact tool failures reach the next orchestrator decision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.recovery_reports import recent_tool_evidence
from tests.leanflow.test_prover_response_guard import make_runtime, stalled_result


def tool_event(error_code: str, error: str, hint: str) -> dict[str, Any]:
    """Reproduce the saved IMO job's checker argument failures."""
    return {
        "type": "tool-result",
        "evidence_id": error_code,
        "details": {
            "tool": "lean_check",
            "arguments": json.dumps(
                {"file": "Scratch.lean", "declaration": "theorem test", "replacement": "by ..."}
            ),
            "result": {
                "success": False,
                "ok": False,
                "error_code": error_code,
                "error": error,
                "hint": hint,
            },
        },
    }


def test_exact_checker_errors_reach_the_orchestrator_request(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    events = [
        tool_event(
            "replacement_not_a_declaration",
            "replacement is not a complete declaration",
            "replacement must be a COMPLETE declaration (full signature AND body)",
        ),
        tool_event(
            "target_not_found",
            "target declaration not found",
            "Use the exact declaration name as written in the file",
        ),
        {"type": "api-response", "details": {"assistant": {"reasoning": "PRIVATE_THOUGHTS"}}},
    ]
    Path(job["log_path"]).write_text("\n".join(map(json.dumps, events)) + "\n")
    requests: list[dict[str, Any]] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        requests.append(kwargs)
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps(
                {
                    "action": "continue",
                    "instructions": "Use target name test and a complete declaration.",
                }
            ),
        }

    runtime.session = session
    runtime._handle_result(job, stalled_result())
    assert len(requests) == 1 and requests[0]["role"] == "orchestrator"
    prompt = requests[0]["prompt"]
    for text in (
        "replacement_not_a_declaration",
        "target_not_found",
        "theorem test",
        "Use the exact declaration name as written in the file",
    ):
        assert text in prompt
    assert "PRIVATE_THOUGHTS" not in prompt
    assert node.status == "retry"
    assert runtime.consumed == 3


def test_evidence_is_bounded_and_ignores_damaged_and_non_tool_events(tmp_path: Path) -> None:
    entries = [tool_event(str(i), "error" * 2000, "hint" * 2000) for i in range(10)]
    (tmp_path / "events.jsonl").write_text(
        "x" * (1024 * 1024 + 100)
        + "\nnot json\n[]\n"
        + "\n".join(map(json.dumps, entries))
        + '\n{"type":"tool-result","details":null}\n'
        + '\n{"type":"api-response","details":{"reasoning":"PRIVATE"}}\n'
    )
    result = recent_tool_evidence(tmp_path)
    assert [item["error_code"] for item in result] == [str(i) for i in range(4, 10)]
    assert len(json.dumps(result)) < 12000
    assert "PRIVATE" not in json.dumps(result)


def test_evidence_preserves_real_search_result_count_without_transcript(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool-result",
                "details": {
                    "tool": "lean_search",
                    "arguments": {"query": "Nat.factorization"},
                    "result": {"query": "Nat.factorization", "results": [{"text": "LARGE"}] * 5},
                },
            }
        )
    )
    result = recent_tool_evidence(tmp_path)
    assert result[0]["result_count"] == 5
    assert "LARGE" not in json.dumps(result)


def test_missing_or_symlinked_event_log_does_not_block_recovery(tmp_path: Path) -> None:
    assert recent_tool_evidence(tmp_path) == []
    source = tmp_path / "other"
    source.write_text(json.dumps(tool_event("target_not_found", "missing", "read the file")))
    (tmp_path / "events.jsonl").symlink_to(source)
    assert recent_tool_evidence(tmp_path) == []
