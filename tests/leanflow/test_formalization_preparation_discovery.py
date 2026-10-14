"""Exercise fresh read-only setup discovery without granting model acquisition or acceptance authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import BackendError, HarnessResult
from core.campaign_limits import CampaignLimits
from core.deadlines import deadline_after
from leanflow_cli.formalization import preparation_discovery as discovery
from leanflow_cli.formalization.preparation_inputs import preparation_documents
from leanflow_cli.formalization.preparation_plan import CATALOG, default_preparation_plan
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
        "\n"
    )
    (tmp_path / "Existing.lean").write_text(
        "namespace Existing\ndef double (n : Nat) : Nat := n + n\nend Existing\n"
    )
    record = create_document(tmp_path, path)
    return tmp_path, record, default_preparation_plan(tmp_path, record, preset="lean")


def _response(record: dict[str, Any], plan: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "source_revision_id": record["revision_id"],
        "base_plan_id": plan["plan_id"],
        "required_imports": ["Init.Data.Nat.Basic"],
        "mathematical_prerequisites": [
            {
                "source_ids": [record["inventory"]["items"][0]["id"]],
                "description": "Reuse Existing.double if its compiled representation matches the source",
                "strategy": "search_existing_then_define",
                "candidate_declarations": ["Existing.double"],
            }
        ],
        "representation_notes": [
            "Use natural numbers and addition; the candidate requires independent checking."
        ],
        "slice_source_ids": plan["slice_source_ids"],
        "package_requests": [],
        **changes,
    }


def _run(root, record, plan, monkeypatch, response, *, budget=None, **extra):
    calls = []

    def invoke(request):
        calls.append(request)
        assert not request.tools and request.restricted_tools and not request.session_id
        assert request.mcp_config and request.isolation
        request.on_started()
        return HarnessResult(
            status="completed",
            session_id="fresh-discovery",
            final_response=json.dumps(response),
            usage={"input_tokens": 50, "output_tokens": 20},
        )

    monkeypatch.setattr(discovery, "run_harness", invoke)
    monkeypatch.setattr(discovery, "build_runtime_isolation", lambda *args, **kwargs: object())
    result = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="claude",
        model="fixture",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(root)},
        budget=budget or CampaignLimits(root / "limits.json", {}),
        directory=root / ".leanflow/discovery",
        **extra,
    )
    return result, calls


def test_fresh_discovery_only_estimates_existing_reuse_and_keeps_authority(paper, monkeypatch):
    root, record, plan = paper
    before = copy.deepcopy(plan)
    result, calls = _run(root, record, plan, monkeypatch, _response(record, plan))
    assert result["accepted"] and result["status"] == "planned" and not result["semantic_approval"]
    assert result["plan"]["mathematical_prerequisites"][0]["candidate_declarations"] == [
        "Existing.double"
    ]
    assert result["plan"]["network_policy"] == plan["network_policy"] and before == plan
    assert result["plan"]["preset"] == "lean"
    assert {row["name"] for row in result["plan"]["packages"]} == {"repl"}
    assert len(calls) == 1 and len(calls[0].prompt.encode()) < 6000
    assert (root / "Existing.lean").read_text().startswith("namespace Existing\n")


def test_model_unknown_source_is_retained_as_explicit_blocker_without_authorization(
    paper, monkeypatch
):
    root, record, plan = paper
    request = {
        "name": "unknown",
        "url": "https://example.org/unknown",
        "revision": "f" * 40,
        "imports": ["Unknown.Defs"],
        "reason": "Potential geometric definitions",
    }
    result, _ = _run(
        root, record, plan, monkeypatch, _response(record, plan, package_requests=[request])
    )
    assert not result["accepted"] and result["status"] == "unauthorized_acquisition"
    assert result["unauthorized_packages"] == [request]
    assert all(row["name"] != "unknown" for row in result["plan"]["packages"])
    assert result["plan"]["network_policy"] == plan["network_policy"]


def test_trusted_user_goal_reaches_discovery_without_authorizing_dependency_sources(
    paper, monkeypatch
):
    root, record, plan = paper
    goal = "Use natural numbers, reuse Existing.double, and select a proper slice."
    request = {
        "name": "unknown",
        "url": "https://example.org/unknown",
        "revision": "f" * 40,
        "imports": ["Unknown.Defs"],
        "reason": "A recommendation is still not acquisition authorization",
    }
    result, calls = _run(
        root,
        record,
        plan,
        monkeypatch,
        _response(record, plan, package_requests=[request]),
        user_goal=goal,
    )
    assert len(calls) == 1
    assert goal in calls[0].prompt
    assert result["status"] == "unauthorized_acquisition" and not result["accepted"]
    assert result["unauthorized_packages"] == [request]
    assert result["plan"]["network_policy"] == plan["network_policy"]


def test_known_mathematical_import_can_select_approved_compatible_catalog_pin(paper, monkeypatch):
    root, record, plan = paper
    result, _ = _run(
        root,
        record,
        plan,
        monkeypatch,
        _response(record, plan, required_imports=["Mathlib.Data.Nat.Basic"]),
    )
    assert result["accepted"]
    mathlib = next(row for row in result["plan"]["packages"] if row["name"] == "mathlib")
    assert (
        mathlib["revision"] == CATALOG["mathlib"]["revision"]
        and mathlib["authorization"] == "catalog"
    )
    assert not result["semantic_approval"]
    assert result["plan"]["preset"] == "mathlib"


def test_existing_mathlib_keeps_its_pin_and_enables_cache_preparation(paper, monkeypatch):
    root, record, _ = paper
    (root / "lakefile.toml").write_text(
        'name = "existing"\n[[require]]\nname = "mathlib"\n'
        'git = "https://example.org/existing-mathlib"\nrev = "' + "a" * 40 + '"\n'
    )
    plan = default_preparation_plan(root, record, preset="lean")
    assert plan["preset"] == "lean"
    result, _ = _run(root, record, plan, monkeypatch, _response(record, plan))
    assert result["accepted"] and result["plan"]["preset"] == "mathlib"
    assert result["plan"]["existing_packages"] == plan["existing_packages"]
    assert {row["name"] for row in result["plan"]["packages"]} == {"repl"}


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision_id": "other"},
        {"base_plan_id": "other"},
        {"network_policy": {"allow_unlisted_sources": True}},
        {"required_imports": ["REPL"]},
        {"slice_source_ids": []},
    ],
)
def test_native_output_cannot_change_revision_authority_or_skip_slice(paper, monkeypatch, change):
    root, record, plan = paper
    result, _ = _run(root, record, plan, monkeypatch, _response(record, plan, **change))
    assert not result["accepted"] and result["status"] == "preparation_discovery_error"


@pytest.mark.parametrize("mode", ["cancelled", "deadline", "budget"])
def test_cancel_deadline_and_budget_stop_before_launch(paper, monkeypatch, mode):
    root, record, plan = paper
    extra, budget = {}, None
    if mode == "cancelled":
        extra["cancelled"] = lambda: True
    elif mode == "deadline":
        extra["deadline"] = deadline_after(0)
    else:
        budget = CampaignLimits(root / "limits.json", {"agent_calls": 0})
    result, calls = _run(
        root, record, plan, monkeypatch, _response(record, plan), budget=budget, **extra
    )
    assert not result["accepted"] and not calls


def test_native_startup_error_does_not_consume_started_call(paper, monkeypatch):
    root, record, plan = paper
    budget = CampaignLimits(root / "limits.json", {})

    def unavailable(request):
        raise BackendError("provider unavailable")

    monkeypatch.setattr(discovery, "run_harness", unavailable)
    monkeypatch.setattr(discovery, "build_runtime_isolation", lambda *args, **kwargs: object())
    result = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="claude",
        model="fixture",
        effort="max",
        environment={},
        budget=budget,
        directory=root / ".leanflow/discovery",
    )
    assert result["status"] == "preparation_discovery_error"
    assert all(call["status"] == "not_started" for call in budget.state["invocations"].values())


def test_frozen_census_and_complete_paginated_sources_remain_readonly(paper):
    root, record, plan = paper
    documents = preparation_documents(root, record, plan)
    assignment = documents["preparation-assignment"]
    candidate = assignment["existing_declaration_candidates"][0]
    assert candidate["declarations"] == [{"name": "Existing.double", "line_start": 2}]
    (root / "Existing.lean").write_text("def replaced : Nat := 0\n")
    key = candidate["evidence_id"]
    assert "Existing" in documents[key]["text"] and "replaced" not in documents[key]["text"]
    documents["large"] = {"text": "数学🧠" * 20_000}
    tools = SessionTools(
        role="reviewer",
        project_root=root,
        workspace=root / "tools",
        context={"assignment_ref": "preparation-assignment"},
        allow_internet=False,
    )
    bridge = EvidenceBridge(
        toolset=tools, context=tools.context, callbacks={}, directory=root / "bridge"
    )
    bridge.documents = documents
    with pytest.raises(ValueError, match="no command"):
        bridge.call("write_file", {"path": "Main.lean", "content": "axiom x : False"})
    offset, parts = 0, []
    while True:
        result = bridge.call("read_evidence", {"id": "large", "offset": offset})
        assert _response_size(result) <= REVIEW_RESPONSE_BYTES
        parts.append(result["text"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert json.loads("".join(parts)) == documents["large"]
