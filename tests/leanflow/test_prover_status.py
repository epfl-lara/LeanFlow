"""Test exact-run editor snapshots and durable prover steering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from leanflow_cli.cli.prover_status import append_prover_message, read_prover_state
from leanflow_cli.cli.runs_command import handle_runs, register_runs_parser


def _state(root: Path, run_id: str = "prove-one", **fields: object) -> Path:
    """Create one persisted prover run for a CLI read or write."""
    directory = root / "prover" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "phase": "proving",
                "jobs": [{"id": "job-1", "agent_id": "prover-1"}],
                **fields,
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_snapshot_is_exact_and_reads_persisted_plan(tmp_path: Path) -> None:
    directory = _state(tmp_path)
    (directory / "PLAN.md").write_text("# Plan\nProve the base case.\n", encoding="utf-8")
    snapshot = read_prover_state(tmp_path, "prove-one")
    assert snapshot is not None
    assert snapshot["plan_markdown"] == "# Plan\nProve the base case.\n"
    assert read_prover_state(tmp_path, "missing-run") is None


def test_mismatched_snapshot_never_falls_back_to_current_run(tmp_path: Path) -> None:
    directory = _state(tmp_path)
    (directory / "state.json").write_text('{"run_id": "prove-two"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        read_prover_state(tmp_path, "prove-one")


@pytest.mark.parametrize("run_id", ["", "..", "../secrets", "/tmp/run", "run/a", "run\\a"])
def test_snapshot_rejects_non_identifiers(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid"):
        read_prover_state(tmp_path, run_id)


def test_guidance_preserves_arrival_order_and_exact_agent(tmp_path: Path) -> None:
    directory = _state(tmp_path)
    first = append_prover_message(tmp_path, "prove-one", "orchestrator", "Use induction.")
    second = append_prover_message(tmp_path, "prove-one", "prover-1", "Try the zero case.")
    records = [json.loads(line) for line in (directory / "inbox.jsonl").read_text().splitlines()]
    assert records == [first, second]
    assert records[1]["agent_id"] == "prover-1"
    assert first["id"] != second["id"]


@pytest.mark.parametrize(
    "phase",
    [
        "completed",
        "failed",
        "exhausted",
        "disproved",
        "interrupted",
        "provider_error",
        "environment_error",
        "source_conflict",
        "verification_failed",
        "budget_exhausted",
        "error",
        "blocked",
    ],
)
def test_terminal_run_rejects_guidance(tmp_path: Path, phase: str) -> None:
    directory = _state(tmp_path, phase=phase)
    with pytest.raises(ValueError, match="finished"):
        append_prover_message(tmp_path, "prove-one", "orchestrator", "Continue")
    assert not (directory / "inbox.jsonl").exists()


def test_guidance_rejects_finished_jobs_but_accepts_active_research_children(
    tmp_path: Path,
) -> None:
    directory = _state(
        tmp_path,
        jobs=[
            {"id": "parent", "status": "completed"},
            {"id": "child", "status": "running", "parent_job_id": "parent"},
        ],
    )
    with pytest.raises(ValueError, match="job has finished"):
        append_prover_message(tmp_path, "prove-one", "parent", "Continue")
    record = append_prover_message(tmp_path, "prove-one", "child", "Check the boundary case")
    assert record["agent_id"] == "child"
    assert len((directory / "inbox.jsonl").read_text().splitlines()) == 1


def test_snapshot_preserves_resume_lineage_without_reading_previous_run(tmp_path: Path) -> None:
    _state(tmp_path, resumed_from="previous", parent_run_id="previous", terminal=False)
    snapshot = read_prover_state(tmp_path, "prove-one")
    assert snapshot is not None
    assert snapshot["run_id"] == "prove-one"
    assert snapshot["resumed_from"] == "previous"
    assert snapshot["terminal"] is False


def test_unknown_agent_and_oversized_message_are_rejected(tmp_path: Path) -> None:
    _state(tmp_path)
    with pytest.raises(ValueError, match="does not belong"):
        append_prover_message(tmp_path, "prove-one", "wrong-agent", "Continue")
    for message in (" ", "x" * 8193, "a\0b"):
        with pytest.raises(ValueError, match="8192"):
            append_prover_message(tmp_path, "prove-one", "orchestrator", message)


@pytest.mark.parametrize("filename", ["state.json", "PLAN.md", "inbox.jsonl"])
def test_symlinked_state_leaf_is_not_read_or_written(tmp_path: Path, filename: str) -> None:
    directory = _state(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text('{"run_id":"prove-one"}', encoding="utf-8")
    leaf = directory / filename
    leaf.unlink(missing_ok=True)
    leaf.symlink_to(outside)
    with pytest.raises(ValueError):
        if filename == "inbox.jsonl":
            append_prover_message(tmp_path, "prove-one", "orchestrator", "Continue")
        else:
            read_prover_state(tmp_path, "prove-one")
    assert outside.read_text() == '{"run_id":"prove-one"}'


def test_symlinked_run_directory_is_rejected(tmp_path: Path) -> None:
    directory = _state(tmp_path, "real-run")
    (tmp_path / "prover" / "linked-run").symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinked"):
        read_prover_state(tmp_path, "linked-run")


def test_runs_cli_dispatches_snapshot_and_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = tmp_path / ".leanflow" / "workflow-state"
    directory = _state(state_root)
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: Test\n", encoding="utf-8")
    parser = argparse.ArgumentParser()
    register_runs_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["runs", "--project", str(tmp_path), "prover", "prove-one", "--json"])
    assert handle_runs(args) == 0
    assert json.loads(capsys.readouterr().out)["prover"]["run_id"] == "prove-one"
    args = parser.parse_args(
        [
            "runs",
            "--project",
            str(tmp_path),
            "prover-message",
            "prove-one",
            "--agent",
            "prover-1",
            "--message=--literal guidance",
        ]
    )
    assert handle_runs(args) == 0
    assert json.loads(capsys.readouterr().out)["success"] is True
    assert json.loads((directory / "inbox.jsonl").read_text())["message"] == "--literal guidance"
