"""Research-grade contracts for immutable run metrics and provenance."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.cli import run_metrics, run_python_identity
from leanflow_cli.cli.run_metrics import (
    capture_run_launch_snapshot,
    collect_provenance,
    collect_run_metrics,
    finalize_run_snapshot,
    run_launch_snapshot_path,
    run_result_snapshot_path,
    validate_run_id,
)
from leanflow_cli.workflows import workflow_state

_HERMETIC_ENV_KEEP = {"LEANFLOW_HOME", "LEANFLOW_QUEUE_INVARIANT_CHECKS"}
_HERMETIC_ENV_PREFIXES = ("LEANFLOW_", "TERMINAL_", "GIT_")


class _FixtureDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


@pytest.fixture(autouse=True)
def _hermetic_machine_identity(tmp_path_factory, monkeypatch) -> None:
    """Give each test a private copy of every machine-global identity input.

    Provenance hashes the installed runtime tree, the resolved provider
    packages, sys.path listings, the installed-distribution inventory, and the
    ambient LEANFLOW_*/TERMINAL_*/GIT_* environment. On a developer machine
    those are live shared state: a concurrent agent session editing this
    checkout mid-test makes capture and seal disagree (spurious
    runtime-source drift), and hashing the real openai/anthropic package
    trees is slow enough under xdist load to risk the suite's 30s per-test
    watchdog. Repointing every input at test-owned stand-ins keeps the
    measured evidence immutable for the duration of a test.
    """
    fake_root = tmp_path_factory.mktemp("hermetic-runtime")
    for relative in ("leanflow_cli", "agent", "core", "tools", "leanflow_skills", "leanflow_specs"):
        module = fake_root / relative / "source.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(f"# {relative}\n", encoding="utf-8")
    (fake_root / "run_agent.py").write_text("# runner\n", encoding="utf-8")
    fake_module = fake_root / "leanflow_cli" / "cli" / "run_metrics.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# metrics\n", encoding="utf-8")
    monkeypatch.setattr(run_metrics, "__file__", str(fake_module))

    monkeypatch.setattr(run_python_identity, "_PRIMARY_PROVIDER_PACKAGES", ())
    monkeypatch.setattr(
        run_python_identity,
        "_sys_path_identity",
        lambda project_root=None: (
            {"sha256": "0" * 64, "entry_count": 0, "content_complete": True},
            [],
        ),
    )
    monkeypatch.setattr(
        run_metrics.importlib_metadata,
        "distributions",
        lambda: [_FixtureDistribution("leanflow-hermetic-fixture", "1.0.0")],
    )

    for key in list(os.environ):
        if key.startswith(_HERMETIC_ENV_PREFIXES) and key not in _HERMETIC_ENV_KEEP:
            monkeypatch.delenv(key, raising=False)


def _state(project: Path) -> Path:
    toolchain = project / "lean-toolchain"
    if not toolchain.exists():
        toolchain.write_text("leanprover/lean4:v4.30.0-rc2\n", encoding="utf-8")
    manifest = project / "lake-manifest.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"packages": []}), encoding="utf-8")
    root = project / ".leanflow" / "workflow-state"
    (root / "activity" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    return root


def _event(
    run_id: str,
    index: int,
    event_type: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    common = {
        "workflow_kind": "prove",
        "workflow_command": "/prove Main.lean",
        "run_scope": "top-level",
    }
    common.update(details or {})
    return {
        "event_id": f"{run_id}-e{index}",
        "timestamp": f"2026-08-01T00:00:{index:02d}+00:00",
        "type": event_type,
        "run_id": run_id,
        "agent_id": str(common.get("agent_session_id", "") or ""),
        "message": "",
        "details": common,
    }


def _usage(
    *,
    prompt: int,
    completion: int,
    provider_cost: float | None = None,
    estimated_turn_cost: float | None = None,
) -> dict[str, Any]:
    source = "provider_reported" if provider_cost is not None else "estimated"
    total = provider_cost if provider_cost is not None else estimated_turn_cost
    return {
        "turn": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "cost": {
            "source": source,
            "total_usd": total,
            "provider_reported_total_usd": provider_cost,
            "estimated_turn_usd": estimated_turn_cost,
        },
    }


def _complete_zero_usage_event(run_id: str, index: int) -> dict[str, Any]:
    """Return an exact conversation envelope proving zero provider usage."""
    return _event(
        run_id,
        index,
        "conversation-end",
        details={
            "agent_session_id": "foreground",
            "api_calls": 0,
            "usage": _usage(prompt=0, completion=0, provider_cost=0.0),
        },
    )


def _events(root: Path, run_id: str, events: list[dict[str, Any]]) -> None:
    (root / "activity" / "runs" / f"{run_id}.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _blueprint(root: Path, name: str, status: str = "proved") -> None:
    (root / "blueprint.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": f"node-{name}",
                        "name": name,
                        "file": "Main.lean",
                        "kind": "theorem",
                        "status": status,
                        "attempts": 2,
                        "api_steps": 9,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _seal(
    project: Path,
    run_id: str,
    events: list[dict[str, Any]],
    *,
    solved: bool,
    phase: str = "exited",
) -> None:
    root = _state(project)
    exit_code = 0 if solved else 1
    sealed_events = [dict(event) for event in events]
    if sealed_events and sealed_events[-1].get("type") == "runner-exit":
        terminal_details = dict(sealed_events[-1].get("details") or {})
        terminal_details.setdefault("exit_code", exit_code)
        sealed_events[-1]["details"] = terminal_details
        exit_code = int(terminal_details["exit_code"])
    _events(root, run_id, sealed_events)
    assert finalize_run_snapshot(
        root,
        run_id=run_id,
        project_root=project,
        outcome={
            "phase": phase,
            "proof_solved": solved,
            "sorry_count": 0 if solved else 1,
            "project_sorry_count": 0 if solved else 1,
            "model": "model-a",
            "provider": "provider-a",
            "exit_code": exit_code,
            "reason": "verified completion" if exit_code == 0 else "runtime failure",
        },
    )


def _multi_agent_events(run_id: str) -> list[dict[str, Any]]:
    return [
        _event(run_id, 0, "runner-start"),
        _event(run_id, 1, "tool-call"),
        _event(run_id, 2, "api-request"),
        _event(
            run_id,
            3,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "model": "model-a",
                "provider": "provider-a",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event(run_id, 4, "api-request"),
        _event(run_id, 5, "api-request"),
        _event(
            run_id,
            6,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "model": "model-a",
                "provider": "provider-a",
                "api_calls": 2,
                "usage": _usage(prompt=20, completion=3, provider_cost=0.9),
            },
        ),
        _event(run_id, 7, "api-request"),
        _event(
            run_id,
            8,
            "conversation-end",
            details={
                "agent_session_id": "child",
                "model": "model-b",
                "provider": "provider-b",
                "api_calls": 1,
                "usage": _usage(prompt=7, completion=1, estimated_turn_cost=0.2),
            },
        ),
        _event(run_id, 9, "verification-failed"),
        _event(run_id, 10, "runner-exit", details={"exit_code": 0}),
    ]


def test_selected_run_never_reads_later_project_global_state(tmp_path: Path) -> None:
    project = tmp_path
    root = _state(project)
    (project / "Main.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")

    assert capture_run_launch_snapshot(root, run_id="run-a", project_root=project)
    _blueprint(root, "declaration_a")
    _seal(project, "run-a", _multi_agent_events("run-a"), solved=True)

    (project / "Main.lean").write_text("theorem b : False := by sorry\n", encoding="utf-8")
    assert capture_run_launch_snapshot(root, run_id="run-b", project_root=project)
    _blueprint(root, "declaration_b", "blocked")
    run_b_events = [
        _event("run-b", 0, "runner-start"),
        _event("run-b", 1, "runner-exit", details={"exit_code": 1}),
    ]
    _seal(project, "run-b", run_b_events, solved=False)
    (root / "latest-run.log").write_text(
        "Tokens this conversation: input 999 · output 999\nTotal cost: $999 (provider reported)\n",
        encoding="utf-8",
    )
    _blueprint(root, "mutable_latest", "blocked")

    metrics = collect_run_metrics(root, run_id="run-a", project_root=project)

    assert metrics["scope"]["exact"] is True, metrics["scope"]
    assert metrics["run"]["run_id"] == "run-a"
    assert metrics["events"]["total"] == 11
    assert metrics["events"]["complete"] is True
    assert [row["name"] for row in metrics["declarations"]] == ["declaration_a"]
    assert metrics["outcome"]["proof_solved"] is True
    assert metrics["usage"]["input_tokens"] == 37
    assert metrics["usage"]["output_tokens"] == 6
    assert metrics["usage"]["api_calls"] == 4
    assert metrics["usage"]["cost_usd"] == pytest.approx(1.1)
    assert metrics["usage"]["cost_source"] == "mixed"
    assert metrics["usage"]["cost_complete"] is True
    assert metrics["usage"]["agent_sessions"] == 2
    assert metrics["failures"] == {"verification-rejected": 1}
    assert metrics["outcome"]["exit_code"] == 0
    assert metrics["outcome"]["terminal_status"] == "succeeded"


def test_exact_scope_describes_complete_evidence_not_proof_success(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="exact-failure", project_root=tmp_path)
    _blueprint(root, "blocked", "blocked")
    _seal(
        tmp_path,
        "exact-failure",
        [
            _event("exact-failure", 0, "runner-start"),
            _complete_zero_usage_event("exact-failure", 1),
            _event("exact-failure", 2, "runner-exit", details={"exit_code": 1}),
        ],
        solved=False,
    )

    metrics = collect_run_metrics(root, run_id="exact-failure")

    assert metrics["scope"]["exact"] is True
    assert metrics["scope"]["missing"] == []
    assert metrics["usage"]["complete"] is True
    assert metrics["usage"]["api_calls"] == 0
    assert metrics["outcome"]["proof_solved"] is False
    assert metrics["outcome"]["exit_code"] == 1


def test_provider_reported_cost_is_not_double_counted_across_turns(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="cost-run", project_root=tmp_path)
    _blueprint(root, "a")
    _seal(tmp_path, "cost-run", _multi_agent_events("cost-run"), solved=True)

    usage = collect_run_metrics(root, run_id="cost-run")["usage"]

    # The foreground provider total is cumulative (0.4 then 0.9), while the
    # child agent contributes an independent estimated 0.2.
    assert usage["cost_usd"] == pytest.approx(1.1)
    assert usage["conversation_end_events"] == 3


def test_stale_earlier_provider_cost_is_not_reported_as_complete(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="stale-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("stale-cost", 0, "runner-start"),
        _event(
            "stale-cost",
            1,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event(
            "stale-cost",
            2,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=20, completion=3),
            },
        ),
        _event("stale-cost", 3, "runner-exit"),
    ]
    _seal(tmp_path, "stale-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="stale-cost")["usage"]

    assert usage["cost_usd"] is None
    assert usage["cost_complete"] is False
    assert usage["complete"] is False


def test_decreasing_provider_total_cannot_report_cost_as_complete(tmp_path: Path) -> None:
    """A session total that resets must not silently drop the earlier spend.

    Cost is the only aggregate that replaces rather than accumulates, so a
    provider reporting a non-cumulative total is the one path that could
    understate it. Tokens and API calls stay complete because they are summed
    per turn and remain fully covered.
    """
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="reset-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("reset-cost", 0, "runner-start"),
        _event("reset-cost", 1, "api-request"),
        _event(
            "reset-cost",
            2,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.9),
            },
        ),
        _event("reset-cost", 3, "api-request"),
        _event(
            "reset-cost",
            4,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=20, completion=3, provider_cost=0.4),
            },
        ),
        _event("reset-cost", 5, "runner-exit"),
    ]
    _seal(tmp_path, "reset-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="reset-cost")["usage"]

    assert usage["cost_usd"] is None
    assert usage["cost_complete"] is False
    assert usage["complete"] is False
    # Only cost is affected; the token and call evidence is still exact.
    assert usage["tokens_complete"] is True
    assert usage["api_calls_complete"] is True
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 5
    assert usage["api_calls"] == 2


def test_a_later_higher_total_cannot_repair_a_reset_session(tmp_path: Path) -> None:
    """Once a session total has reset, no subsequent total restores completeness.

    The spend between the first total and the reset is unrecoverable, so a
    larger later total is still missing it. The regression flag is sticky.
    """
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="resumed-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [_event("resumed-cost", 0, "runner-start")]
    for index, cost in enumerate((0.9, 0.4, 1.2), start=1):
        events.append(_event("resumed-cost", index * 2 - 1, "api-request"))
        events.append(
            _event(
                "resumed-cost",
                index * 2,
                "conversation-end",
                details={
                    "agent_session_id": "foreground",
                    "api_calls": 1,
                    "usage": _usage(prompt=10, completion=2, provider_cost=cost),
                },
            )
        )
    events.append(_event("resumed-cost", 7, "runner-exit"))
    _seal(tmp_path, "resumed-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="resumed-cost")["usage"]

    assert usage["cost_usd"] is None
    assert usage["cost_complete"] is False


def test_repeated_equal_provider_total_remains_complete(tmp_path: Path) -> None:
    """An unchanged cumulative total is not a regression."""
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="flat-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("flat-cost", 0, "runner-start"),
        _event("flat-cost", 1, "api-request"),
        _event(
            "flat-cost",
            2,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.5),
            },
        ),
        _event("flat-cost", 3, "api-request"),
        _event(
            "flat-cost",
            4,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=20, completion=3, provider_cost=0.5),
            },
        ),
        _event("flat-cost", 5, "runner-exit"),
    ]
    _seal(tmp_path, "flat-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="flat-cost")["usage"]

    assert usage["cost_usd"] == pytest.approx(0.5)
    assert usage["cost_complete"] is True
    assert usage["complete"] is True


def test_independent_sessions_may_report_unrelated_provider_totals(tmp_path: Path) -> None:
    """Cumulative totals are per session, so a lower child total is not a reset."""
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="per-session-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("per-session-cost", 0, "runner-start"),
        _event("per-session-cost", 1, "api-request"),
        _event(
            "per-session-cost",
            2,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.9),
            },
        ),
        _event("per-session-cost", 3, "api-request"),
        _event(
            "per-session-cost",
            4,
            "conversation-end",
            details={
                "agent_session_id": "child",
                "api_calls": 1,
                "usage": _usage(prompt=7, completion=1, provider_cost=0.2),
            },
        ),
        _event("per-session-cost", 5, "runner-exit"),
    ]
    _seal(tmp_path, "per-session-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="per-session-cost")["usage"]

    assert usage["cost_usd"] == pytest.approx(1.1)
    assert usage["cost_complete"] is True


def test_estimates_after_a_cumulative_provider_total_preserve_full_coverage(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="mixed-cost", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("mixed-cost", 0, "runner-start"),
        _event("mixed-cost", 1, "api-request"),
        _event(
            "mixed-cost",
            2,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("mixed-cost", 3, "api-request"),
        _event(
            "mixed-cost",
            4,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=20, completion=3, estimated_turn_cost=0.2),
            },
        ),
        _event("mixed-cost", 5, "runner-exit"),
    ]
    _seal(tmp_path, "mixed-cost", events, solved=True)

    usage = collect_run_metrics(root, run_id="mixed-cost")["usage"]

    assert usage["cost_usd"] == pytest.approx(0.6)
    assert usage["cost_source"] == "mixed"
    assert usage["cost_complete"] is True


def test_api_request_without_conversation_end_invalidates_usage_coverage(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="missing-usage", project_root=tmp_path)
    _blueprint(root, "a")
    events = [
        _event("missing-usage", 0, "runner-start"),
        _event("missing-usage", 1, "api-request"),
        _event("missing-usage", 2, "api-request"),
        _event(
            "missing-usage",
            3,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("missing-usage", 4, "runner-exit"),
    ]
    _seal(tmp_path, "missing-usage", events, solved=True)

    usage = collect_run_metrics(root, run_id="missing-usage")["usage"]

    assert usage["recorded_api_requests"] == 2
    assert usage["api_request_coverage_complete"] is False
    assert usage["api_calls"] is None
    assert usage["input_tokens"] is None
    assert usage["cost_usd"] is None
    assert usage["complete"] is False


def test_positive_conversation_api_count_without_request_evidence_fails_closed(
    tmp_path: Path,
) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="missing-request", project_root=tmp_path)
    _blueprint(root, "missing-request")
    events = [
        _event("missing-request", 0, "runner-start"),
        _event(
            "missing-request",
            1,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("missing-request", 2, "runner-exit"),
    ]
    _seal(tmp_path, "missing-request", events, solved=True)

    metrics = collect_run_metrics(root, run_id="missing-request")
    usage = metrics["usage"]

    assert usage["recorded_api_requests"] == 0
    assert usage["api_request_coverage_complete"] is False
    assert usage["api_calls"] is None
    assert usage["complete"] is False
    assert metrics["scope"]["exact"] is False
    assert "complete-usage-evidence" in metrics["scope"]["missing"]


def test_compatibility_api_call_events_do_not_double_count_requests(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="paired-api", project_root=tmp_path)
    _blueprint(root, "paired")
    events = [
        _event("paired-api", 0, "runner-start"),
        _event("paired-api", 1, "api-request", details={"iteration": 1}),
        _event("paired-api", 2, "api-call", details={"iteration": 1}),
        _event("paired-api", 3, "api-request", details={"iteration": 2}),
        _event("paired-api", 4, "api-call", details={"iteration": 2}),
        _event(
            "paired-api",
            5,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 2,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("paired-api", 6, "runner-exit"),
    ]
    _seal(tmp_path, "paired-api", events, solved=True)

    metrics = collect_run_metrics(root, run_id="paired-api")
    usage = metrics["usage"]

    assert usage["recorded_api_requests"] == 2
    assert usage["recorded_api_request_events"] == 2
    assert usage["recorded_api_call_events"] == 2
    assert usage["api_calls"] == 2
    assert usage["api_request_coverage_complete"] is True


def test_repeated_canonical_requests_in_one_iteration_are_not_deduplicated(
    tmp_path: Path,
) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="retry-api", project_root=tmp_path)
    _blueprint(root, "retry")
    events = [
        _event("retry-api", 0, "runner-start"),
        _event("retry-api", 1, "api-request", details={"iteration": 1}),
        _event("retry-api", 2, "api-request", details={"iteration": 1}),
        _event("retry-api", 3, "api-call", details={"iteration": 1}),
        _event(
            "retry-api",
            4,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 2,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("retry-api", 5, "runner-exit"),
    ]
    _seal(tmp_path, "retry-api", events, solved=True)

    usage = collect_run_metrics(root, run_id="retry-api")["usage"]

    assert usage["recorded_api_requests"] == 2
    assert usage["recorded_api_request_events"] == 2
    assert usage["recorded_api_call_events"] == 1
    assert usage["api_request_coverage_complete"] is True


def test_unmetered_provider_attempt_invalidates_all_usage_completeness(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="unmetered-api", project_root=tmp_path)
    _blueprint(root, "unmetered")
    events = [
        _event("unmetered-api", 0, "runner-start"),
        _event("unmetered-api", 1, "api-request"),
        _event("unmetered-api", 2, "api-usage-unmetered"),
        _event(
            "unmetered-api",
            3,
            "conversation-end",
            details={
                "agent_session_id": "foreground",
                "api_calls": 1,
                "usage": _usage(prompt=10, completion=2, provider_cost=0.4),
            },
        ),
        _event("unmetered-api", 4, "runner-exit"),
    ]
    _seal(tmp_path, "unmetered-api", events, solved=True)

    metrics = collect_run_metrics(root, run_id="unmetered-api")
    usage = metrics["usage"]

    assert metrics["scope"]["exact"] is False
    assert "complete-usage-evidence" in metrics["scope"]["missing"]
    assert usage["unmetered_api_attempts"] == 1
    assert usage["api_request_coverage_complete"] is False
    assert usage["api_calls"] is None
    assert usage["input_tokens"] is None
    assert usage["cost_usd"] is None
    assert usage["complete"] is False


def test_dispatched_descendant_run_usage_fails_scoring_closed(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="dispatch-parent", project_root=tmp_path)
    _blueprint(root, "dispatch")
    events = [
        _event("dispatch-parent", 0, "runner-start"),
        _event(
            "dispatch-parent",
            1,
            "dispatch-job",
            details={"job_id": "child-1", "state": "succeeded"},
        ),
        _event("dispatch-parent", 2, "runner-exit"),
    ]
    _seal(tmp_path, "dispatch-parent", events, solved=True)

    metrics = collect_run_metrics(root, run_id="dispatch-parent")

    assert metrics["scope"]["exact"] is False
    assert "complete-usage-evidence" in metrics["scope"]["missing"]
    assert "descendant-run-usage-unmetered" in metrics["scope"]["missing"]
    assert metrics["usage"]["descendant_dispatch_events"] == 1
    assert metrics["usage"]["descendant_usage_complete"] is False


def test_command_expert_attempt_fails_reproducibility_closed(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="command-expert", project_root=tmp_path)
    _blueprint(root, "expert")
    events = [
        _event("command-expert", 0, "runner-start"),
        _event(
            "command-expert",
            1,
            "api-usage-unmetered",
            details={
                "reason": "command-expert-provider-attempt",
                "provider": "codex",
                "mode": "command",
            },
        ),
        _event("command-expert", 2, "runner-exit"),
    ]
    _seal(tmp_path, "command-expert", events, solved=True)

    metrics = collect_run_metrics(root, run_id="command-expert")

    assert metrics["scope"]["exact"] is False
    assert "complete-usage-evidence" in metrics["scope"]["missing"]
    assert "external-command-runtime-unpinned" in metrics["scope"]["missing"]
    assert metrics["usage"]["command_expert_attempts"] == 1
    assert metrics["usage"]["unmetered_reason_counts"] == {"command-expert-provider-attempt": 1}


def test_journal_rejections_require_explicit_matching_run_id(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="run-a", project_root=tmp_path)
    _blueprint(root, "a")
    (root / "journal.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"event": "unscoped-rejected"},
                {"run_id": "run-a", "event": "attempt-rejected"},
                {"run_id": "run-b", "event": "verification-rejected"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seal(
        tmp_path,
        "run-a",
        [
            _event("run-a", 0, "runner-start"),
            _complete_zero_usage_event("run-a", 1),
            _event("run-a", 2, "runner-exit"),
        ],
        solved=True,
    )

    metrics = collect_run_metrics(root, run_id="run-a")

    assert metrics["journal_rejections"] == {"attempt-rejected": 1}
    assert metrics["journal_rejections_scope"] == "explicit-run-id-only"


def test_launch_environment_is_complete_sorted_and_secret_redacted(
    tmp_path: Path, monkeypatch
) -> None:
    root = _state(tmp_path)
    monkeypatch.setenv("LEANFLOW_ALPHA_FLAG", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "model-z")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "provider-z")
    monkeypatch.setenv("LEANFLOW_NATIVE_REQUESTED_TARGET", "Main.lean")
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_BASE_URL",
        "https://alice:user-pass@models.example.test/v1"
        "?api-version=1&accessToken=query-secret#route?signature=fragment-secret",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_REASONING_EFFORT", "high")
    monkeypatch.setenv("LEANFLOW_NATIVE_AGENT_MAX_TURNS", "123")
    monkeypatch.setenv("LEANFLOW_NATIVE_CONTEXT_COMPRESSION_MODEL", "model-c")
    monkeypatch.setenv("LEANFLOW_NATIVE_TOOLSET", "lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    monkeypatch.setenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", "")
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_EXPLICIT_GOAL",
        "prove this with pasted token sk-test-secret-material",
    )
    monkeypatch.setenv(
        "LEANFLOW_RESEARCH_PROMPT",
        "unpublished prompt with token sk-second-secret-material",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "never-persist-api-key")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "never-persist-process-token")

    assert capture_run_launch_snapshot(
        root,
        run_id="env-run",
        project_root=tmp_path,
        context={"startup_prompt": "context token sk-launch-context-secret"},
    )
    raw = run_launch_snapshot_path(root, "env-run").read_text(encoding="utf-8")
    launch = json.loads(raw)
    environment = launch["environment"]
    values = environment["values"]

    assert list(values) == sorted(values)
    assert values["LEANFLOW_ALPHA_FLAG"] == "1"
    assert values["LEANFLOW_NATIVE_API_KEY"] == "[redacted]"
    assert values["LEANFLOW_NATIVE_PROCESS_TOKEN"] == "[redacted]"
    assert "never-persist-api-key" not in raw
    assert "never-persist-process-token" not in raw
    assert "alice" not in raw
    assert "user-pass" not in raw
    assert "query-secret" not in raw
    assert "fragment-secret" not in raw
    assert "sk-test-secret-material" not in raw
    assert "sk-second-secret-material" not in raw
    assert "sk-launch-context-secret" not in raw
    assert launch["context"]["startup_prompt"].startswith("[content-sha256:")
    assert values["LEANFLOW_NATIVE_EXPLICIT_GOAL"].startswith("[content-sha256:")
    assert values["LEANFLOW_RESEARCH_PROMPT"].startswith("[content-sha256:")
    assert "models.example.test" in values["LEANFLOW_NATIVE_BASE_URL"]
    assert "api-version=1" in values["LEANFLOW_NATIVE_BASE_URL"]
    assert len(environment["sha256"]) == 64
    assert environment["runtime"] == {
        "agent_max_turns": "123",
        "base_url": values["LEANFLOW_NATIVE_BASE_URL"],
        "context_compression_model": "model-c",
        "model": "model-z",
        "provider": "provider-z",
        "requested_target": "Main.lean",
        "reasoning_effort": "high",
        "toolset": "lean",
        "active_skill": "lean-proof-loop",
        "additional_skills": "",
    }

    _blueprint(root, "env")
    _seal(
        tmp_path,
        "env-run",
        [
            _event("env-run", 0, "runner-start"),
            _complete_zero_usage_event("env-run", 1),
            _event("env-run", 2, "runner-exit"),
        ],
        solved=True,
    )
    metrics = collect_run_metrics(root, run_id="env-run", project_root=tmp_path)
    assert metrics["scope"]["missing"] == []
    assert metrics["scope"]["exact"] is True, metrics["scope"]
    assert metrics["launch"]["environment"]["sha256"] == environment["sha256"]
    assert metrics["launch"]["environment"]["runtime"]["model"] == "model-z"


def test_tampered_launch_environment_fails_closed(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="tampered-env", project_root=tmp_path)
    _blueprint(root, "env")
    _seal(
        tmp_path,
        "tampered-env",
        [
            _event("tampered-env", 0, "runner-start"),
            _event("tampered-env", 1, "runner-exit"),
        ],
        solved=True,
    )
    result_path = run_result_snapshot_path(root, "tampered-env")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["launch"]["environment"]["values"]["LEANFLOW_FAKE_KNOB"] = "changed"
    result_path.chmod(0o644)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    metrics = collect_run_metrics(root, run_id="tampered-env", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert "final-snapshot-integrity-mismatch" in metrics["scope"]["missing"]
    assert metrics["launch"] is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("outcome", {"phase": "exited", "exit_code": 0, "proof_solved": False}),
        ("usage", {"api_calls": 999, "complete": True}),
        ("declarations", []),
    ],
)
def test_final_snapshot_integrity_detects_evidence_edits(
    tmp_path: Path, field: str, replacement: Any
) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="tampered-final", project_root=tmp_path)
    _blueprint(root, "final")
    _seal(
        tmp_path,
        "tampered-final",
        [
            _event("tampered-final", 0, "runner-start"),
            _event("tampered-final", 1, "runner-exit"),
        ],
        solved=True,
    )
    result_path = run_result_snapshot_path(root, "tampered-final")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result[field] = replacement
    result_path.chmod(0o644)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    metrics = collect_run_metrics(root, run_id="tampered-final", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert "final-snapshot-integrity-mismatch" in metrics["scope"]["missing"]
    assert metrics["declarations"] == []


@pytest.mark.parametrize(
    "nodes",
    [
        [
            {
                "id": "bad-status",
                "file": "Main.lean",
                "name": "a",
                "status": "invented",
                "attempts": 0,
                "api_steps": 0,
            }
        ],
        [
            {
                "id": "",
                "file": "Main.lean",
                "name": "a",
                "status": "proved",
                "attempts": 0,
                "api_steps": 0,
            }
        ],
        [
            {
                "id": "duplicate",
                "file": "Main.lean",
                "name": "a",
                "status": "proved",
                "attempts": 0,
                "api_steps": 0,
            },
            {
                "id": "duplicate",
                "file": "Other.lean",
                "name": "b",
                "status": "blocked",
                "attempts": 0,
                "api_steps": 0,
            },
        ],
        [
            {
                "id": "negative",
                "file": "Main.lean",
                "name": "a",
                "status": "proved",
                "attempts": -1,
                "api_steps": 0,
            }
        ],
    ],
)
def test_malformed_declaration_graph_cannot_be_sealed(
    tmp_path: Path, nodes: list[dict[str, Any]]
) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="bad-graph", project_root=tmp_path)
    (root / "blueprint.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    _events(
        root,
        "bad-graph",
        [
            _event("bad-graph", 0, "runner-start"),
            _event("bad-graph", 1, "runner-exit", details={"exit_code": 0}),
        ],
    )

    with pytest.raises(RuntimeError, match="cannot seal"):
        finalize_run_snapshot(
            root,
            run_id="bad-graph",
            project_root=tmp_path,
            outcome={"phase": "exited", "exit_code": 0},
        )

    assert not run_result_snapshot_path(root, "bad-graph").exists()


def test_final_snapshot_rejects_disagreeing_exit_evidence(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="exit-mismatch", project_root=tmp_path)
    _blueprint(root, "a")
    _events(
        root,
        "exit-mismatch",
        [
            _event("exit-mismatch", 0, "runner-start"),
            _event("exit-mismatch", 1, "runner-exit", details={"exit_code": 1}),
        ],
    )

    with pytest.raises(RuntimeError, match="disagree"):
        finalize_run_snapshot(
            root,
            run_id="exit-mismatch",
            project_root=tmp_path,
            outcome={"phase": "exited", "exit_code": 0},
        )

    assert not run_result_snapshot_path(root, "exit-mismatch").exists()


def test_reused_run_id_lifecycle_cannot_be_sealed_as_one_run(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="reused-run", project_root=tmp_path)
    _blueprint(root, "a")
    _events(
        root,
        "reused-run",
        [
            _event("reused-run", 0, "runner-start"),
            _event("reused-run", 1, "runner-exit", details={"exit_code": 0}),
            _event("reused-run", 2, "runner-start"),
            _event("reused-run", 3, "runner-exit", details={"exit_code": 0}),
        ],
    )

    with pytest.raises(RuntimeError, match="multiple run lifecycles"):
        finalize_run_snapshot(
            root,
            run_id="reused-run",
            project_root=tmp_path,
            outcome={"phase": "exited", "exit_code": 0},
        )


def test_launch_snapshot_collision_marks_final_metrics_non_exact(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="collision-run", project_root=tmp_path)
    assert not capture_run_launch_snapshot(root, run_id="collision-run", project_root=tmp_path)
    _blueprint(root, "a")
    _seal(
        tmp_path,
        "collision-run",
        [
            _event("collision-run", 0, "runner-start"),
            _event("collision-run", 1, "runner-exit"),
        ],
        solved=True,
    )

    metrics = collect_run_metrics(root, run_id="collision-run", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert "launch-snapshot-collision" in metrics["scope"]["missing"]


def test_launch_and_final_provenance_do_not_drift_after_the_run(tmp_path: Path) -> None:
    root = _state(tmp_path)
    source = tmp_path / "Main.lean"
    source.write_text("theorem before : True := by trivial\n", encoding="utf-8")
    assert capture_run_launch_snapshot(root, run_id="drift-run", project_root=tmp_path)
    launch_identity = collect_provenance(tmp_path)["source_identity_sha256"]

    source.write_text("theorem during : True := by trivial\n", encoding="utf-8")
    final_identity = collect_provenance(tmp_path)["source_identity_sha256"]
    _blueprint(root, "during")
    _seal(
        tmp_path,
        "drift-run",
        [
            _event("drift-run", 0, "runner-start"),
            _complete_zero_usage_event("drift-run", 1),
            _event("drift-run", 2, "runner-exit"),
        ],
        solved=True,
    )

    source.write_text("theorem later : True := by trivial\n", encoding="utf-8")
    later_identity = collect_provenance(tmp_path)["source_identity_sha256"]
    metrics = collect_run_metrics(root, run_id="drift-run", project_root=tmp_path)

    assert len({launch_identity, final_identity, later_identity}) == 3
    assert metrics["provenance"]["source_identity_sha256"] == launch_identity
    assert metrics["provenance_final"]["source_identity_sha256"] == final_identity
    assert metrics["source_changed_during_run"] is True
    assert metrics["scope"]["exact"] is True
    assert metrics["runtime_source_changed"] is False
    assert metrics["python_runtime_changed"] is False
    assert metrics["selected_skills_changed"] is False
    assert metrics["behavior_config_changed"] is False
    assert metrics["project_configuration_changed"] is False
    assert metrics["lean_toolchain_changed"] is False
    assert metrics["dependency_manifest_changed"] is False
    assert metrics["build_configuration_changed"] is False


@pytest.mark.parametrize(
    ("input_kind", "expected_field", "expected_reason"),
    [
        ("toolchain", "lean_toolchain_changed", "lean-toolchain-drift"),
        (
            "manifest",
            "dependency_manifest_changed",
            "dependency-manifest-drift",
        ),
        ("lakefile", "build_configuration_changed", "build-configuration-drift"),
    ],
)
def test_build_configuration_drift_during_run_fails_exactness(
    tmp_path: Path,
    input_kind: str,
    expected_field: str,
    expected_reason: str,
) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="build-drift", project_root=tmp_path)
    if input_kind == "toolchain":
        (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
    elif input_kind == "manifest":
        (tmp_path / "lake-manifest.json").write_text(
            json.dumps({"packages": [{"name": "mathlib", "rev": "new-revision"}]}),
            encoding="utf-8",
        )
    else:
        (tmp_path / "lakefile.toml").write_text(
            'name = "changed-build"\nversion = "0.1.0"\n', encoding="utf-8"
        )
    _blueprint(root, "build-drift")
    _seal(
        tmp_path,
        "build-drift",
        [
            _event("build-drift", 0, "runner-start"),
            _complete_zero_usage_event("build-drift", 1),
            _event("build-drift", 2, "runner-exit"),
        ],
        solved=True,
    )

    metrics = collect_run_metrics(root, run_id="build-drift", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert metrics[expected_field] is True
    assert metrics["build_configuration_changed"] is True
    assert expected_reason in metrics["scope"]["missing"]
    assert "build-configuration-drift" in metrics["scope"]["missing"]


def test_selected_skill_drift_during_run_fails_exactness(tmp_path: Path, monkeypatch) -> None:
    root = _state(tmp_path)
    skill = tmp_path / ".leanflow" / "skills" / "custom-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: custom-skill\ndescription: test\n---\nFirst route.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "custom-skill")
    assert capture_run_launch_snapshot(root, run_id="skill-drift", project_root=tmp_path)
    skill.write_text(
        "---\nname: custom-skill\ndescription: test\n---\nChanged route.\n",
        encoding="utf-8",
    )
    _blueprint(root, "skill")
    _seal(
        tmp_path,
        "skill-drift",
        [
            _event("skill-drift", 0, "runner-start"),
            _event("skill-drift", 1, "runner-exit"),
        ],
        solved=True,
    )

    metrics = collect_run_metrics(root, run_id="skill-drift", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert metrics["selected_skills_changed"] is True
    assert "selected-skills-drift" in metrics["scope"]["missing"]


def test_archived_retained_stream_remains_exact(monkeypatch, tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="archived-run", project_root=tmp_path)
    _blueprint(root, "archived")
    _seal(
        tmp_path,
        "archived-run",
        [
            _event("archived-run", 0, "runner-start"),
            _complete_zero_usage_event("archived-run", 1),
            _event("archived-run", 2, "runner-exit"),
        ],
        solved=True,
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        lambda payload, require_verified=False: False,
    )

    result = workflow_state.compact_closed_workflow_activity()
    assert result.archived_runs == ("archived-run",)
    assert not (root / "activity" / "runs" / "archived-run.jsonl").exists()

    metrics = collect_run_metrics(root, run_id="archived-run", project_root=tmp_path)
    assert metrics["scope"]["stream_source"] == "retained"
    assert metrics["scope"]["archive_audit"]["complete"] is True
    assert metrics["scope"]["exact"] is True
    assert metrics["events"]["total"] == 3


def test_unknown_run_fails_closed_without_project_globals(tmp_path: Path) -> None:
    root = _state(tmp_path)
    _blueprint(root, "unrelated")
    (root / "live_status.json").write_text(
        json.dumps({"proof_solved": True, "model": "wrong"}), encoding="utf-8"
    )

    metrics = collect_run_metrics(root, run_id="does-not-exist", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert metrics["scope"]["run_found"] is False
    assert metrics["events"]["complete"] is False
    assert metrics["declarations"] == []
    assert metrics["outcome"]["proof_solved"] is None
    assert metrics["provenance"] is None


def test_invalid_run_id_is_rejected_before_path_resolution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run id"):
        collect_run_metrics(_state(tmp_path), run_id="../../latest-run")
    with pytest.raises(ValueError, match="run id"):
        validate_run_id("run.jsonl")


def test_run_specific_log_is_the_only_legacy_usage_fallback(tmp_path: Path) -> None:
    root = _state(tmp_path)
    run_id = "legacy-run"
    _events(
        root,
        run_id,
        [_event(run_id, 0, "runner-start"), _event(run_id, 1, "runner-exit")],
    )
    (root / "runs" / f"{run_id}.log").write_text(
        "API calls: 3 this conversation\n"
        "Tokens this conversation: input 100 · output 20\n"
        "Total cost: $1.25 (provider reported)\n",
        encoding="utf-8",
    )
    (root / "latest-run.log").write_text(
        "API calls: 999 this conversation\n"
        "Tokens this conversation: input 999 · output 999\n"
        "Total cost: $999 (provider reported)\n",
        encoding="utf-8",
    )

    metrics = collect_run_metrics(root, run_id=run_id)

    assert metrics["scope"]["exact"] is False
    assert metrics["usage"]["source"] == "run-log"
    assert metrics["usage"]["api_calls"] == 3
    assert metrics["usage"]["input_tokens"] == 100
    assert metrics["usage"]["cost_usd"] == pytest.approx(1.25)
    assert metrics["usage"]["aggregation_scope"] == "last-run-log-summary-only"
    assert metrics["usage"]["complete"] is False
    assert metrics["usage"]["cost_complete"] is False


def test_partial_append_invalidates_snapshot_completeness(tmp_path: Path) -> None:
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="partial-run", project_root=tmp_path)
    _blueprint(root, "a")
    _seal(
        tmp_path,
        "partial-run",
        [_event("partial-run", 0, "runner-start"), _event("partial-run", 1, "runner-exit")],
        solved=True,
    )
    with (root / "activity" / "runs" / "partial-run.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event_id": "partial"')

    metrics = collect_run_metrics(root, run_id="partial-run")

    assert metrics["events"]["complete"] is False
    assert metrics["scope"]["exact"] is False
    assert metrics["declarations"] == []


def test_provenance_reads_toolchain_and_dependency_revisions(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc2\n", encoding="utf-8")
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "mathlib",
                        "rev": "5450b53e",
                        "url": "https://git-user:git-password@example.test/mathlib.git"
                        "?accessToken=dependency-query-secret"
                        "#access_token=dependency-fragment-secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    provenance = collect_provenance(tmp_path)

    assert provenance["lean_toolchain"] == "leanprover/lean4:v4.30.0-rc2"
    assert provenance["dependencies"]["mathlib"] == "5450b53e"
    serialized = json.dumps(provenance, sort_keys=True)
    assert "git-user" not in serialized
    assert "git-password" not in serialized
    assert "dependency-query-secret" not in serialized
    assert "dependency-fragment-secret" not in serialized
    assert "example.test/mathlib.git" in provenance["dependency_sources"]["mathlib"]["url"]
    assert provenance["source_identity_scope"] == "lean-project-files"
    assert provenance["git_evidence_status"] == "not-a-git-worktree"
    assert provenance["provenance_complete"] is True
    assert len(provenance["source_identity_sha256"]) == 64


def test_project_manifest_and_guidance_participate_in_source_identity(tmp_path: Path) -> None:
    _state(tmp_path)
    guidance = tmp_path / "PROOF_HANDOFF.md"
    guidance.write_text("Use the first reduction.\n", encoding="utf-8")
    manifest = tmp_path / ".leanflow" / "project.yaml"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_guidance": [{"path": "PROOF_HANDOFF.md", "targets": ["result"]}],
            }
        ),
        encoding="utf-8",
    )

    first = collect_provenance(tmp_path)
    guidance.write_text("Use the corrected reduction.\n", encoding="utf-8")
    second = collect_provenance(tmp_path)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workflow_guidance": [{"path": "PROOF_HANDOFF.md", "targets": ["result"]}],
            }
        ),
        encoding="utf-8",
    )
    third = collect_provenance(tmp_path)

    assert first["project_configuration_complete"] is True
    assert first["workflow_guidance_status"] == "complete"
    assert first["workflow_guidance_sha256"] != second["workflow_guidance_sha256"]
    assert second["project_manifest_sha256"] != third["project_manifest_sha256"]
    assert (
        len(
            {
                first["source_identity_sha256"],
                second["source_identity_sha256"],
                third["source_identity_sha256"],
            }
        )
        == 3
    )


def test_out_of_root_project_guidance_makes_provenance_incomplete(tmp_path: Path) -> None:
    _state(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-guidance.md"
    outside.write_text("outside", encoding="utf-8")
    manifest = tmp_path / ".leanflow" / "project.yaml"
    manifest.write_text(
        json.dumps({"workflow_guidance": [{"path": str(outside)}]}),
        encoding="utf-8",
    )

    provenance = collect_provenance(tmp_path)

    assert provenance["project_configuration_complete"] is False
    assert provenance["workflow_guidance_status"] == "incomplete"
    assert provenance["workflow_guidance_issues"] == ["entry-0-out-of-root"]
    assert provenance["provenance_complete"] is False


def test_installed_runtime_content_changes_runtime_identity(tmp_path: Path, monkeypatch) -> None:
    fake_root = tmp_path / "installed"
    for relative in ("leanflow_cli", "agent", "core", "tools", "leanflow_skills", "leanflow_specs"):
        source = fake_root / relative / "source.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"# {relative}\n", encoding="utf-8")
    (fake_root / "run_agent.py").write_text("# runner\n", encoding="utf-8")
    fake_module = fake_root / "leanflow_cli" / "cli" / "run_metrics.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# metrics\n", encoding="utf-8")
    monkeypatch.setattr(run_metrics, "__file__", str(fake_module))

    first = run_metrics._runtime_source_identity()
    (fake_root / "agent" / "source.py").write_text("# changed agent\n", encoding="utf-8")
    second = run_metrics._runtime_source_identity()

    assert first["runtime_source_complete"] is True
    assert second["runtime_source_complete"] is True
    assert first["runtime_source_sha256"] != second["runtime_source_sha256"]


def test_installed_distribution_version_changes_python_runtime_identity(
    tmp_path: Path, monkeypatch
) -> None:
    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

    installed = [Distribution("openai", "9.0.0"), Distribution("pydantic", "3.0.0")]
    monkeypatch.setattr(
        run_metrics.importlib_metadata,
        "distributions",
        lambda: list(installed),
    )

    _state(tmp_path)
    first = run_metrics._python_runtime_identity()
    first_provenance = collect_provenance(tmp_path)
    installed[0] = Distribution("openai", "9.1.0")
    second = run_metrics._python_runtime_identity()
    second_provenance = collect_provenance(tmp_path)

    assert first["python_runtime_complete"] is True
    assert first["installed_distributions"]["openai"] == "9.0.0"
    assert first["python_runtime_sha256"] != second["python_runtime_sha256"]
    assert first_provenance["runtime_source_sha256"] != second_provenance["runtime_source_sha256"]
    assert first_provenance["source_identity_sha256"] != second_provenance["source_identity_sha256"]


def test_behavior_config_and_native_runtime_knobs_change_launch_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from leanflow_cli.config import invalidate_config_cache, save_config

    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.setenv("LEANFLOW_NATIVE_AGENT_MAX_TURNS", "20")
    monkeypatch.setenv("LEANFLOW_NATIVE_CONTEXT_COMPRESSION_MODEL", "model-a")
    monkeypatch.setenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "80000")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUXILIARY_LEAN_REASONING_PROVIDER", "codex")
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE",
        "codex exec --token sk-native-command-secret",
    )
    save_config(
        {
            "model": {
                "prover_light": "light-a",
                "context_lengths": {"model-a": 131072},
                "api_key": "config-api-key-must-never-serialize",
            },
            "agent": {"temperature": 0.1},
            "auxiliary": {
                "orchestration": {
                    "provider": "codex",
                    "model": "model-orchestrator-a",
                    "command_template": "codex exec sk-config-command-secret",
                    "api_key": "config-auxiliary-api-key-secret",
                }
            },
        }
    )
    first = run_metrics.collect_launch_environment()
    first_serialized = json.dumps(first, sort_keys=True)

    save_config(
        {
            "model": {
                "prover_light": "light-b",
                "context_lengths": {"model-a": 262144},
                "api_key": "second-config-secret-must-never-serialize",
            },
            "agent": {"temperature": 0.2},
            "auxiliary": {
                "orchestration": {
                    "provider": "claude-code",
                    "model": "model-orchestrator-b",
                    "command_template": "claude --print sk-second-command-secret",
                    "api_key": "second-config-auxiliary-secret",
                }
            },
        }
    )
    invalidate_config_cache()
    monkeypatch.setenv("LEANFLOW_NATIVE_AGENT_MAX_TURNS", "21")
    monkeypatch.setenv("LEANFLOW_NATIVE_CONTEXT_COMPRESSION_MODEL", "model-b")
    monkeypatch.setenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "81000")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUXILIARY_LEAN_REASONING_PROVIDER", "claude-code")
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE",
        "claude --print sk-second-native-command-secret",
    )
    second = run_metrics.collect_launch_environment()
    second_serialized = json.dumps(second, sort_keys=True)

    assert first["runtime"]["agent_max_turns"] == "20"
    assert first["runtime"]["context_compression_model"] == "model-a"
    assert first["behavior_config"]["model"]["prover_light"] == "light-a"
    assert first["behavior_config"]["model"]["context_lengths"] == {"model-a": 131072}
    assert (
        first["behavior_config"]["environment_overrides"][
            "LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS"
        ]
        == "80000"
    )
    assert len(first["behavior_config"]["effective_static_config_sha256"]) == 64
    assert first["behavior_config"]["auxiliary"]["orchestration"]["provider"] == "codex"
    assert first["behavior_config"]["auxiliary"]["orchestration"]["command_template"].startswith(
        "[content-sha256:"
    )
    assert first["values"]["LEANFLOW_NATIVE_AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE"].startswith(
        "[content-sha256:"
    )
    assert first["sha256"] != second["sha256"]
    assert first["behavior_config_sha256"] != second["behavior_config_sha256"]
    assert "config-api-key-must-never-serialize" not in first_serialized
    assert "second-config-secret-must-never-serialize" not in second_serialized
    for secret in (
        "sk-native-command-secret",
        "sk-config-command-secret",
        "config-auxiliary-api-key-secret",
        "sk-second-native-command-secret",
        "sk-second-command-secret",
        "second-config-auxiliary-secret",
    ):
        assert secret not in first_serialized
        assert secret not in second_serialized


def test_persistent_prompt_inputs_are_digest_bound_and_drift_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    from leanflow_cli.config import save_config

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    save_config(
        {
            "memory": {
                "memory_enabled": True,
                "user_profile_enabled": True,
                "memory_char_limit": 2200,
                "user_char_limit": 1375,
            }
        }
    )
    root = _state(project)
    memory_dir = home / "memories"
    memory_dir.mkdir(parents=True, exist_ok=True)
    cursor_dir = project / ".cursor" / "rules"
    cursor_dir.mkdir(parents=True)
    inputs = {
        home / "SOUL.md": "Persistent soul guidance.",
        memory_dir / "MEMORY.md": "Persistent memory note.",
        memory_dir / "USER.md": "Persistent user preference.",
        project / "AGENTS.md": "Project agent guidance.",
        project / ".cursorrules": "Project cursor guidance.",
        cursor_dir / "style.mdc": "Nested cursor guidance.",
    }
    for path, content in inputs.items():
        path.write_text(content, encoding="utf-8")

    baseline = collect_provenance(project)
    prompt_inputs = baseline["behavior_config"]["persistent_prompt_inputs"]
    serialized = json.dumps(prompt_inputs, sort_keys=True)

    assert baseline["behavior_config_complete"] is True
    assert baseline["behavior_config"]["persistent_prompt_inputs_complete"] is True
    assert len(baseline["behavior_config"]["persistent_prompt_inputs_sha256"]) == 64
    assert prompt_inputs["context_files"]["input_file_count"] == 4
    assert prompt_inputs["memory"]["enabled"] is True
    assert prompt_inputs["user"]["enabled"] is True
    for content in inputs.values():
        assert content not in serialized

    for index, (path, content) in enumerate(inputs.items()):
        path.write_text(f"Changed persistent input {index}.", encoding="utf-8")
        changed = collect_provenance(project)
        assert changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]
        assert changed["source_identity_sha256"] != baseline["source_identity_sha256"]
        path.write_text(content, encoding="utf-8")

    assert capture_run_launch_snapshot(root, run_id="prompt-drift", project_root=project)
    (memory_dir / "MEMORY.md").write_text("Changed during the run.", encoding="utf-8")
    _blueprint(root, "prompt-drift")
    _seal(
        project,
        "prompt-drift",
        [
            _event("prompt-drift", 0, "runner-start"),
            _complete_zero_usage_event("prompt-drift", 1),
            _event("prompt-drift", 2, "runner-exit"),
        ],
        solved=True,
    )

    metrics = collect_run_metrics(root, run_id="prompt-drift", project_root=project)

    assert metrics["scope"]["exact"] is False
    assert metrics["behavior_config_changed"] is True
    assert "behavior-config-drift" in metrics["scope"]["missing"]


def test_context_length_and_advisor_reserve_each_change_behavior_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from leanflow_cli.config import save_config

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.setenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "80000")
    save_config({"model": {"context_lengths": {"model-a": 131072}}})
    _state(project)
    baseline = run_metrics.collect_launch_environment(project_root=project)

    monkeypatch.setenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "81000")
    reserve_changed = run_metrics.collect_launch_environment(project_root=project)
    assert reserve_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]

    monkeypatch.setenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "80000")
    save_config({"model": {"context_lengths": {"model-a": 262144}}})
    context_changed = run_metrics.collect_launch_environment(project_root=project)
    assert context_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]


def test_selected_project_skill_content_changes_provenance_identity(
    tmp_path: Path, monkeypatch
) -> None:
    _state(tmp_path)
    skill = tmp_path / ".leanflow" / "skills" / "custom-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: custom-skill\ndescription: test\n---\nFirst route.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "custom-skill")

    first = collect_provenance(tmp_path)
    skill.write_text(
        "---\nname: custom-skill\ndescription: test\n---\nCorrected route.\n",
        encoding="utf-8",
    )
    second = collect_provenance(tmp_path)

    assert first["selected_skills_complete"] is True
    assert first["selected_skills"][0]["source"] == "project"
    assert first["selected_skills_sha256"] != second["selected_skills_sha256"]
    assert first["source_identity_sha256"] != second["source_identity_sha256"]


def test_submodule_revision_evidence_participates_in_source_identity(
    tmp_path: Path, monkeypatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "Main.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "Main.lean"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=LeanFlow Test",
            "-c",
            "user.email=leanflow@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    _state(tmp_path)
    original_git_bytes = run_metrics._git_bytes
    submodule_status = b" 1111111111111111111111111111111111111111 deps/example\n"

    def fake_submodules(project_root: Path, *args: str) -> bytes | None:
        if args[:2] == ("submodule", "status"):
            return submodule_status
        return original_git_bytes(project_root, *args)

    monkeypatch.setattr(run_metrics, "_git_bytes", fake_submodules)
    first = collect_provenance(tmp_path)
    submodule_status = b"+2222222222222222222222222222222222222222 deps/example\n"
    second = collect_provenance(tmp_path)

    assert first["git_submodules_sha256"] != second["git_submodules_sha256"]
    assert first["source_identity_sha256"] != second["source_identity_sha256"]


def test_failed_git_evidence_cannot_masquerade_as_a_clean_exact_run(
    tmp_path: Path, monkeypatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "Main.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "Main.lean"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=LeanFlow Test",
            "-c",
            "user.email=leanflow@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    root = _state(tmp_path)
    assert capture_run_launch_snapshot(root, run_id="git-failure", project_root=tmp_path)
    original_git_bytes = run_metrics._git_bytes

    def fail_status(project_root: Path, *args: str) -> bytes | None:
        if args and args[0] == "status":
            return None
        return original_git_bytes(project_root, *args)

    monkeypatch.setattr(run_metrics, "_git_bytes", fail_status)
    incomplete = collect_provenance(tmp_path)
    assert incomplete["source_identity_scope"] == "git-index-plus-untracked"
    assert incomplete["git_evidence_status"] == "incomplete"
    assert incomplete["git_evidence_complete"] is False
    assert incomplete["provenance_complete"] is False

    _blueprint(root, "a")
    _seal(
        tmp_path,
        "git-failure",
        [
            _event("git-failure", 0, "runner-start"),
            _event("git-failure", 1, "runner-exit"),
        ],
        solved=True,
    )
    metrics = collect_run_metrics(root, run_id="git-failure", project_root=tmp_path)

    assert metrics["scope"]["exact"] is False
    assert "final-provenance" in metrics["scope"]["missing"]
    assert metrics["source_changed_during_run"] is None


def test_metrics_payload_is_json_serializable(tmp_path: Path) -> None:
    root = _state(tmp_path)
    payload = collect_run_metrics(root, run_id="missing", project_root=tmp_path)
    assert json.loads(json.dumps(payload))["version"] == 2
