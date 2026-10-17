"""Bound quiet discovery and navigate every mathematical source item without narrowing coverage."""

from __future__ import annotations

import copy
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult, IsolationPolicy, run_harness
from agent.runtime.harness.models import NativeLaunchFailure
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import preparation_discovery as discovery
from leanflow_cli.formalization.clause_obligations import record_obligation
from leanflow_cli.formalization.preparation_bounds import DISCOVERY_WALL_TIME_ENV
from leanflow_cli.formalization.preparation_inputs import preparation_documents
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.review_transport import (
    REVIEW_RESPONSE_BYTES,
    EvidenceBridge,
    _response_size,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.fixture
def paper(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    path = tmp_path / "paper.tex"
    path.write_text(
        r"\begin{definition}Double a natural number.\end{definition}"
        "\n"
        r"\begin{theorem}Doubling agrees with addition.\end{theorem}"
    )
    record = create_document(tmp_path, path)
    return tmp_path, record, default_preparation_plan(tmp_path, record)


def _discover(paper, monkeypatch, *, result, environment=None, budget=None, **extra):
    root, record, plan = paper
    observed = []

    def invoke(request):
        observed.append(request)
        request.on_started()
        return result

    monkeypatch.setattr(discovery, "run_harness", invoke)
    monkeypatch.setattr(discovery, "build_runtime_isolation", lambda *args, **kwargs: object())
    budget = budget or CampaignLimits(root / "limits.json", {"agent_calls": 5})
    outcome = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="codex",
        model="fixture",
        effort="max",
        environment=environment or {},
        budget=budget,
        directory=root / ".leanflow/discovery",
        **extra,
    )
    return outcome, observed, budget


@pytest.mark.parametrize("status", ["cancelled", "deadline_exceeded"])
def test_verified_stop_reports_the_real_reason_and_preserves_charged_draft(
    paper, monkeypatch, status
):
    root, record, _ = paper
    original = (root / "paper.tex").read_bytes()
    before = copy.deepcopy(record)
    outcome, requests, budget = _discover(
        paper,
        monkeypatch,
        result=HarnessResult(status, cleanup_verified=True),
    )
    assert not outcome["accepted"] and outcome["status"] == status
    assert "closure is unverified" not in outcome["error"]
    assert ("cancelled" if status == "cancelled" else "900-second stage limit") in outcome["error"]
    assert "preserved" in outcome["error"]
    assert outcome["discovery"]["cleanup_verified"] is True
    assert list(budget.state["invocations"].values()) == [{"status": "finished", "usage": None}]
    assert len(requests) == 1 and requests[0].mcp_config.is_file()
    assert not list((root / ".leanflow/discovery").rglob("recommendation.json"))
    assert (root / "paper.tex").read_bytes() == original and record == before
    assert record["units"] == [] and record["checks"] == {} and record["reviews"] == []
    assert not (root / "formalization").exists()


@pytest.mark.parametrize("status", ["cancelled", "deadline_exceeded"])
def test_unverified_stop_still_fails_closed(paper, monkeypatch, status):
    outcome, _, _ = _discover(
        paper,
        monkeypatch,
        result=HarnessResult(status, cleanup_verified=False),
    )
    assert outcome["status"] == "cleanup_unverified" and not outcome["accepted"]
    assert outcome["discovery"]["cleanup_verified"] is False


@pytest.mark.parametrize("status", ["cancelled", "deadline_exceeded"])
def test_verified_prelaunch_stop_keeps_reason_without_charging_a_started_call(
    paper, monkeypatch, status
):
    root, record, plan = paper

    def invoke(request):
        raise NativeLaunchFailure("Stopped during native startup", status=status)

    monkeypatch.setattr(discovery, "run_harness", invoke)
    monkeypatch.setattr(discovery, "build_runtime_isolation", lambda *args, **kwargs: object())
    budget = CampaignLimits(root / "limits.json", {"agent_calls": 1})
    outcome = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="codex",
        model="fixture",
        effort="max",
        environment={},
        budget=budget,
        directory=root / ".leanflow/discovery",
    )
    assert outcome["status"] == status and outcome["discovery"]["cleanup_verified"] is True
    assert "preserved" in outcome["error"] and "closure is unverified" not in outcome["error"]
    assert list(budget.state["invocations"].values()) == [{"status": "not_started", "usage": None}]


@pytest.mark.parametrize(
    "configured,campaign_remaining,caller_deadline,expected,stage_limited",
    [
        (None, None, None, 1900, True),
        ("120", 21600, 22600, 1120, True),
        ("120", 30, None, 1030, False),
        ("120", None, 1020, 1020, False),
        ("0.25", None, None, 1000.25, True),
    ],
)
def test_stage_deadline_never_extends_original_campaign_or_caller_time(
    paper, monkeypatch, configured, campaign_remaining, caller_deadline, expected, stage_limited
):
    root, _, _ = paper
    budget = CampaignLimits(root / "limits.json", {"agent_calls": 5})
    monkeypatch.setattr(budget, "remaining", lambda: campaign_remaining)
    monkeypatch.setattr(
        discovery, "deadline_after", lambda seconds: None if seconds is None else 1000 + seconds
    )
    monkeypatch.setattr(discovery, "remaining_seconds", lambda deadline: max(0, deadline - 1000))
    environment = {} if configured is None else {DISCOVERY_WALL_TIME_ENV: configured}
    outcome, requests, returned_budget = _discover(
        paper,
        monkeypatch,
        result=HarnessResult("deadline_exceeded", cleanup_verified=True),
        environment=environment,
        budget=budget,
        deadline=caller_deadline,
    )
    assert requests[0].deadline == expected
    assert ("stage limit" in outcome["error"]) is stage_limited
    assert returned_budget is budget and budget.limits == {
        "agent_calls": 5,
        "tokens": None,
        "wall_time_s": None,
    }


@pytest.mark.parametrize("value", ["", "0", "-1", "nan", "inf", "-inf", "unlimited", "bad"])
def test_invalid_stage_limit_fails_before_reserving_or_launching(paper, monkeypatch, value):
    outcome, requests, budget = _discover(
        paper,
        monkeypatch,
        result=HarnessResult("completed", cleanup_verified=True),
        environment={DISCOVERY_WALL_TIME_ENV: value},
    )
    assert outcome["status"] == "preparation_discovery_error" and not outcome["accepted"]
    assert DISCOVERY_WALL_TIME_ENV in outcome["error"]
    assert not requests and not budget.state["invocations"]


def test_whole_mathematical_index_retains_every_item_clause_and_frozen_locator(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(
        "% Long preamble\n" * 1000
        + "\n".join(
            rf"\begin{{{'definition' if index == 0 else 'theorem'}}}\label{{item{index}}}"
            + f"Mathematics {index}: "
            + "a long statement " * 40
            + rf"\end{{{'definition' if index == 0 else 'theorem'}}}"
            for index in range(24)
        )
    )
    record = create_document(tmp_path, source)
    inventory = record["inventory"]
    for index in range(38):
        item = inventory["items"][index % 24]
        record_obligation(
            record,
            item,
            [
                {
                    **item["statement_spans"][0],
                    "coordinate_space": "source_bytes",
                    "precision": "exact",
                }
            ],
            {
                "description": f"Explicit clause {index}",
                "reason": "Keep the exact source obligation",
            },
        )
    record["full_inventory"] = copy.deepcopy(inventory)
    record["inventory"]["items"] = record["inventory"]["items"][:1]
    original = copy.deepcopy(record)
    documents = preparation_documents(tmp_path, record, default_preparation_plan(tmp_path, record))
    index = documents["mathematical-source-index"]
    assert index["source_item_count"] == 24 and index["known_clause_count"] == 38
    assert index["canonical_region_count"] == len(original["full_inventory"]["regions"])
    assert {row["source_id"] for row in index["items"]} == {
        item["id"] for item in original["full_inventory"]["items"]
    }
    for item, row in zip(original["full_inventory"]["items"], index["items"], strict=True):
        assert row["statement_spans"] == item["statement_spans"]
        assert row["clause_ids"] == [clause["id"] for clause in item["known_obligations"]]
        assert row["statement_excerpt"] == item["text"][:400]
        assert row["statement_excerpt_complete"] is False
        assert documents[row["evidence_id"]]["item"] == item
    assert documents["inventory"] == original["full_inventory"] and record == original
    tools = SessionTools(
        role="reviewer", project_root=tmp_path, workspace=tmp_path / "tools", context={}
    )
    bridge = EvidenceBridge(toolset=tools, context={}, callbacks={}, directory=tmp_path / "bridge")
    bridge.documents = documents
    offset, parts = 0, []
    while True:
        page = bridge.call("read_evidence", {"id": "mathematical-source-index", "offset": offset})
        assert _response_size(page) <= REVIEW_RESPONSE_BYTES
        parts.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(parts)) == index and len(parts) < 10
    assert (
        documents["preparation-assignment"]["mathematical_source_index_ref"]
        == "mathematical-source-index"
    )


def test_quiet_fake_native_process_stops_at_stage_deadline_with_verified_cleanup(
    paper, monkeypatch
):
    """Exercise actual supervision using a temporary fake CLI, never an installed provider."""
    from agent.runtime.harness import runner

    root, record, plan = paper
    executable = root / "quiet-fake-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        "pathlib.Path('fake.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, policy, **kwargs: argv)
    monkeypatch.setattr(
        discovery,
        "build_runtime_isolation",
        lambda backend, workspace, **kwargs: IsolationPolicy(workspace),
    )
    requests = []

    def invoke(request):
        requests.append(request)
        return run_harness(replace(request, executable=str(executable)))

    monkeypatch.setattr(discovery, "run_harness", invoke)
    budget = CampaignLimits(root / "limits.json", {"agent_calls": 2})
    started = time.monotonic()
    outcome = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="claude",
        model="fixture",
        effort="max",
        environment={DISCOVERY_WALL_TIME_ENV: "2", "CLAUDE_CONFIG_DIR": str(root / "fake-home")},
        budget=budget,
        directory=root / ".leanflow/discovery",
    )
    assert time.monotonic() - started < 15
    assert outcome["status"] == "deadline_exceeded"
    assert outcome["discovery"]["cleanup_verified"] is True
    assert (
        "2-second stage limit" in outcome["error"]
        and "closure is unverified" not in outcome["error"]
    )
    process = int((requests[0].cwd / "fake.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process, 0)
    assert list(budget.state["invocations"].values()) == [{"status": "finished", "usage": None}]
    assert not list((root / ".leanflow/discovery").rglob("recommendation.json"))
    assert record["units"] == [] and not (root / "formalization").exists()
