"""Join one compact activity row to the full model output recorded behind it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from leanflow_cli.cli import run_event_detail
from leanflow_cli.cli.run_event_detail import read_event_detail
from leanflow_cli.cli.runs_command import handle_runs

RUN = "prove-run-a"
JOB = "orchestrator_00001"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), "utf-8")


def _row(event_id: str, kind: str, timestamp: str, **details: object) -> dict:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "type": kind,
        "run_id": RUN,
        "agent_id": JOB,
        "message": kind,
        "details": {"job_id": JOB, **details},
    }


def _job_record(kind: str, timestamp: str, evidence_id: str, details: dict) -> dict:
    return {
        "type": kind,
        "timestamp": timestamp,
        "agent_id": JOB,
        "run_id": RUN,
        "evidence_id": evidence_id,
        "details": details,
    }


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / ".leanflow" / "workflow-state"
    _write_jsonl(
        root / "activity" / "runs" / f"{RUN}.jsonl",
        [
            _row("e-req", "api-request", "2026-09-06T13:23:26+00:00", api_calls=10, api_budget=50),
            _row(
                "e-resp",
                "api-response",
                "2026-09-06T13:23:30+00:00",
                api_calls=10,
                evidence_id="ev-response-10",
                content_preview="Let me read the paper first…",
            ),
            # Recorded before evidence ids existed: only kind, tool, and time remain.
            _row("e-tool", "tool-result", "2026-09-06T13:23:32+00:00", tool="read_file"),
            _row("e-tool-late", "tool-result", "2026-09-06T13:24:40+00:00", tool="read_file"),
            _row(
                "e-ctl",
                "candidate_checked",
                "2026-09-06T13:30:00+00:00",
                node_id="n1",
                accepted=True,
            ),
            _row("e-orphan", "api-response", "2026-09-06T13:40:00+00:00", api_calls=99),
        ],
    )
    _write_jsonl(
        root / "prover" / RUN / "jobs" / JOB / "events.jsonl",
        [
            _job_record(
                "api-response",
                "2026-09-06T13:23:30.123456+00:00",
                "ev-response-10",
                {
                    "api_calls": 10,
                    "assistant": {
                        "role": "assistant",
                        "content": "Let me read the paper first, then draft the invariant.",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"resources/bukh.md"}',
                                },
                            }
                        ],
                    },
                },
            ),
            _job_record(
                "tool-result",
                "2026-09-06T13:23:32.500000+00:00",
                "",
                {
                    "tool": "read_file",
                    "arguments": '{"path":"resources/bukh.md"}',
                    "result": {"success": True, "content": "# Paper"},
                },
            ),
            _job_record(
                "tool-result",
                "2026-09-06T13:24:40.900000+00:00",
                "",
                {
                    "tool": "read_file",
                    "arguments": '{"path":"PLAN_job.md"}',
                    "result": {"success": True, "content": "x" * 250_000},
                },
            ),
        ],
    )
    _write_jsonl(
        root / "prover" / RUN / "events.jsonl",
        [
            {
                "time": "2026-09-06T13:30:00.250000+00:00",
                "event": "candidate_checked",
                "node_id": "n1",
                "accepted": True,
                "result": {"accepted": True, "axioms": ["propext"]},
            }
        ],
    )
    return root


def test_shared_evidence_id_returns_the_exact_job_log_record(state_root: Path) -> None:
    payload = read_event_detail(state_root, RUN, "e-resp")
    assert payload["found"] is True
    assert payload["event"]["event_id"] == "e-resp"
    evidence = payload["evidence"]
    assert evidence["source"] == "job-log" and evidence["match"] == "evidence_id"
    assert evidence["path"].endswith(f"jobs/{JOB}/events.jsonl")
    record = evidence["record"]
    assert record["kind"] == "api-response" and record["evidence_id"] == "ev-response-10"
    assert record["details"]["assistant"]["content"].startswith("Let me read the paper first,")
    assert record["details"]["assistant"]["tool_calls"][0]["function"]["name"] == "read_file"


def test_legacy_rows_match_by_kind_tool_and_nearest_time(state_root: Path) -> None:
    first = read_event_detail(state_root, RUN, "e-tool")["evidence"]
    late = read_event_detail(state_root, RUN, "e-tool-late")["evidence"]
    assert first["match"] == late["match"] == "heuristic"
    assert first["record"]["details"]["arguments"] == '{"path":"resources/bukh.md"}'
    assert late["record"]["details"]["arguments"] == '{"path":"PLAN_job.md"}'


def test_controller_events_resolve_from_the_run_event_log(state_root: Path) -> None:
    evidence = read_event_detail(state_root, RUN, "e-ctl")["evidence"]
    assert evidence["source"] == "run-events" and evidence["match"] == "heuristic"
    assert evidence["record"]["details"]["result"] == {"accepted": True, "axioms": ["propext"]}
    assert "time" not in evidence["record"]["details"]


def test_long_strings_are_bounded_with_their_original_length(state_root: Path) -> None:
    record = read_event_detail(state_root, RUN, "e-tool-late")["evidence"]["record"]
    content = record["details"]["result"]["content"]
    assert len(content) < 250_000
    assert content.endswith("<truncated chars=250000>")
    assert content.startswith("x" * run_event_detail.MAX_EVIDENCE_STRING_CHARS)


def test_unmatched_and_unknown_rows_report_no_evidence_without_failing(state_root: Path) -> None:
    orphan = read_event_detail(state_root, RUN, "e-orphan")
    assert orphan["found"] is True and orphan["evidence"]["source"] == "none"
    missing = read_event_detail(state_root, RUN, "nope")
    assert missing["found"] is False and missing["event"] is None
    assert read_event_detail(state_root, "other-run", "e-resp")["found"] is False


def test_api_rows_prefer_the_matching_call_count(state_root: Path) -> None:
    path = state_root / "prover" / RUN / "jobs" / JOB / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records.insert(
        0,
        _job_record(
            "api-response",
            "2026-09-06T13:23:29.000000+00:00",
            "",
            {"api_calls": 9, "assistant": {"content": "earlier turn"}},
        ),
    )
    _write_jsonl(path, records)
    rows = state_root / "activity" / "runs" / f"{RUN}.jsonl"
    stripped = []
    for line in rows.read_text().splitlines():
        row = json.loads(line)
        row["details"].pop("evidence_id", None)
        stripped.append(row)
    _write_jsonl(rows, stripped)
    record = read_event_detail(state_root, RUN, "e-resp")["evidence"]["record"]
    assert record["details"]["api_calls"] == 10


@pytest.mark.parametrize(
    "run_id,event_id", [("../x", "e-resp"), (RUN, "../e"), (RUN, ""), ("", "e")]
)
def test_identifiers_are_validated_before_any_read(
    state_root: Path, run_id: str, event_id: str
) -> None:
    with pytest.raises(ValueError):
        read_event_detail(state_root, run_id, event_id)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlinked_prover_state_is_not_followed(state_root: Path, tmp_path: Path) -> None:
    real = state_root / "prover" / RUN
    moved = tmp_path / "elsewhere"
    real.rename(moved)
    os.symlink(moved, real)
    payload = read_event_detail(state_root, RUN, "e-resp")
    assert payload["found"] is True and payload["evidence"]["source"] == "none"


def test_cli_event_command_prints_the_join(state_root: Path, tmp_path: Path, capsys) -> None:
    args = argparse.Namespace(
        project=str(tmp_path), runs_command="event", run_id=RUN, event_id="e-resp"
    )
    assert handle_runs(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1 and payload["found"] is True
    assert payload["evidence"]["match"] == "evidence_id"

    bad = argparse.Namespace(
        project=str(tmp_path), runs_command="event", run_id="../x", event_id="e"
    )
    assert handle_runs(bad) == 2
    assert json.loads(capsys.readouterr().out)["success"] is False
