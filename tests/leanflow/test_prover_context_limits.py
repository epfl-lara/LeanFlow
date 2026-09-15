"""Prevent context failures from spending planner budgets or discarding contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.context_policy import ContextBudget
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.session_context import approximate_tokens, compact_history
from leanflow_cli.workflows.prover.session_tools import SessionTools
from tests.leanflow.test_prover_runtime import Verifier, project
from tests.leanflow.test_prover_sessions import run_fake


def test_default_context_limits_match_flags_and_preserve_output_headroom() -> None:
    from leanflow_cli.flags.prover_catalog import PROVER_FLAGS

    config = ProverConfig()
    flags = {flag.name: flag.default for flag in PROVER_FLAGS}
    assert config.search_order == flags["LEANFLOW_PROVER_SEARCH_ORDER"] == "top-down"
    assert config.wall_time_s == int(flags["LEANFLOW_PROVER_WALL_TIME_S"]) == 57600
    assert flags["LEANFLOW_PROVER_CONTEXT_TOKENS"] == "256000"
    assert flags["LEANFLOW_PROVER_ORCHESTRATOR_CONTEXT_TOKENS"] == "256000"
    for role in ("prover", "negation", "orchestrator", "review", "research"):
        budget = ContextBudget.from_config(config.to_mapping(role))
        assert budget == ContextBudget(256000, 8192, 247808, 192000)
    assert ContextBudget.from_config({}) == budget


def test_history_compaction_cannot_shrink_an_oversized_pinned_assignment(tmp_path: Path) -> None:
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "protected claim" * 1000},
    ]
    compacted, _ = compact_history(messages, context_tokens=500, workspace=tmp_path)
    assert compacted == messages
    assert approximate_tokens(compacted) > 500


def test_review_context_failure_stops_before_another_proposal_and_resumes_stage(
    tmp_path: Path,
) -> None:
    path = project(tmp_path)
    calls: list[str] = []
    fail_review = True

    def stage(**kwargs: Any) -> dict[str, Any]:
        role = kwargs["role"]
        calls.append(role)
        if role == "review" and fail_review:
            return {
                "status": "context_limit",
                "api_calls": 0,
                "error": "review context too large",
                "final_response": "",
            }
        report = {"plan": "Use trivial", "nodes": []}
        if role == "review":
            report = {"accepted": True}
        elif role == "prover":
            report = {"proof": "trivial"}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    config = ProverConfig(
        mode="research", total_api_calls=20, orchestrator_api_calls=2, job_api_calls=2
    )
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=config, session=stage, verifier=Verifier()
    )
    state = runtime.run()
    assert state["status"] == "context_limit"
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert state["metrics"]["api_calls"] == 2
    assert state["planning_request"]["steps"]["review-0"]
    assert state["proposal_status"] == "proposed"
    assert "sorry" in path.read_text()
    fail_review = False
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=config,
        session=stage,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    result = resumed.run()
    assert result["status"] == "completed"
    assert calls == ["orchestrator", "orchestrator", "review", "review", "prover"]
    assert result["metrics"]["api_calls"] == 4


def test_oversized_graph_proof_payload_is_externalized_before_first_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = "theorem protected : True"
    context = {
        "dag": {
            "roots": ["r"],
            "nodes": [
                {
                    "id": "r",
                    "statement": statement,
                    "status": "proved",
                    "dependencies": [],
                    "candidate": ["large proof " * 10000],
                }
            ],
        }
    }
    calls = []

    def request(_agent: Any, messages: Any, _timeout: float) -> Any:
        calls.append(messages)
        assert statement in messages[1]["content"]
        assert "large proof " * 100 not in messages[1]["content"]
        return {"role": "assistant", "content": '{"accepted":true}'}, {}

    result = run_fake(tmp_path, monkeypatch, request, role="review", context=context)
    assert result["status"] == "completed"
    assert len(calls) == result["api_calls"] == 1
    assert any("assignment" in artifact for artifact in result["artifacts"])
    assert context["dag"]["nodes"][0]["candidate"] == ["large proof " * 10000]


def test_compression_triggers_before_hard_context_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def request(_agent: Any, messages: Any, _timeout: float) -> Any:
        calls.append(messages)
        if len(calls) == 1:
            return {"role": "assistant", "content": "old reasoning " * 1600}, {}
        assert "old reasoning " * 100 not in json.dumps(messages)
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(
        tmp_path,
        monkeypatch,
        request,
        config={"context_tokens": 16000, "max_output_tokens": 1000, "compression_threshold": 0.5},
    )
    assert result["status"] == "completed" and len(calls) == 2
    events = [json.loads(line) for line in (tmp_path / "job.jsonl").read_text().splitlines()]
    event = next(item["details"] for item in events if item["type"] == "context-compacted")
    assert event["trigger_tokens"] == 8000
    assert event["before_tokens"] < event["input_limit"]
    assert event["after_tokens"] < event["before_tokens"]


def test_exact_model_contexts_apply_to_all_roles_and_roundtrip() -> None:
    profiles = {
        "worker": {
            "context_tokens": 32000,
            "compression_threshold": 0.6,
            "max_output_tokens": 4000,
        },
        "planner": {"context_tokens": 96000, "compression_threshold": 0.7},
    }
    config = ProverConfig.from_env(
        {
            "LEANFLOW_NATIVE_MODEL": "worker",
            "LEANFLOW_PROVER_ORCHESTRATOR_MODEL": "planner",
            "LEANFLOW_PROVER_MODEL_CONTEXTS": json.dumps(profiles),
        }
    )
    for role in ("prover", "negation", "orchestrator", "review", "research"):
        settings = config.to_mapping(role)
        expected = profiles[settings["model"]]
        assert all(settings[key] == value for key, value in expected.items())
        assert ContextBudget.from_config(settings).trigger_tokens == int(
            expected["context_tokens"] * expected["compression_threshold"]
        )
    saved = config.to_mapping()
    assert "max_output_tokens" not in saved
    assert ProverConfig(**saved).to_mapping("review")["context_tokens"] == 96000


def test_explicit_output_allowance_is_not_reduced_to_a_quarter_window() -> None:
    budget = ContextBudget.from_config({"context_tokens": 96000, "max_output_tokens": 32768})
    assert budget.output_tokens == 32768
    assert budget.input_limit == 63232
    assert budget.trigger_tokens <= budget.input_limit
    with pytest.raises(ValueError, match="smaller than context_tokens"):
        ContextBudget.from_config({"context_tokens": 32000, "max_output_tokens": 32768})


def test_role_thresholds_remain_independent_without_model_override() -> None:
    config = ProverConfig.from_env(
        {
            "LEANFLOW_PROVER_COMPRESSION_THRESHOLD": "0.5",
            "LEANFLOW_PROVER_ORCHESTRATOR_COMPRESSION_THRESHOLD": "0.8",
        }
    )
    assert config.to_mapping("prover")["compression_threshold"] == 0.5
    assert config.to_mapping("review")["compression_threshold"] == 0.8
    assert (
        ContextBudget.from_config(
            {"context_tokens": 10000, "max_output_tokens": 2500, "compression_threshold": 0.9}
        ).trigger_tokens
        == 7500
    )


@pytest.mark.parametrize("value", [0, 1, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_compression_threshold_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="compression_threshold"):
        ProverConfig(compression_threshold=value)
    with pytest.raises(ValueError, match="compression_threshold"):
        ProverConfig(model_contexts={"m": {"compression_threshold": value}})


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"m": []},
        {"m": {"typo": 100}},
        {"m": {"context_tokens": 0}},
        {"m": {"context_tokens": True}},
        {"m": {"context_tokens": 32000.5}},
    ],
)
def test_invalid_model_context_map_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        ProverConfig.from_env({"LEANFLOW_PROVER_MODEL_CONTEXTS": json.dumps(value)})


def test_oversized_essential_contract_stops_without_charging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    statement = "protected claim " * 20000
    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *_: calls.append(1),
        context={"assignment": {"statement": statement}},
    )
    assert result["status"] == "context_limit" and result["api_calls"] == 0
    assert calls == []


def test_externalized_evidence_is_complete_and_readable(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.session_assignment import compact_assignment

    context = {
        "dag": {
            "roots": ["r"],
            "nodes": [
                {
                    "id": "r",
                    "statement": "theorem r : True",
                    "dependencies": ["h"],
                    "status": "candidate",
                    "candidate": ["proof " * 10000],
                    "informal_justification": "requires h",
                }
            ],
        },
        "proposed_plan": "Use h; do not accept until h is proved",
    }
    message, artifacts = compact_assignment(
        "Review", context, workspace=tmp_path, token_budget=2000
    )
    reduced = json.loads(message["content"].split("Assignment context:\n")[1])
    node = reduced["dag"]["nodes"][0]
    assert node["statement"] == context["dag"]["nodes"][0]["statement"]
    assert node["status"] == "candidate" and node["dependencies"] == ["h"]
    assert reduced["proposed_plan"] == context["proposed_plan"]
    assert (
        json.loads(Path(artifacts[0]).read_text())["candidate"]
        == context["dag"]["nodes"][0]["candidate"]
    )
    tools = SessionTools(role="review", project_root=tmp_path, workspace=tmp_path, context=context)
    assert tools.invoke("read_file", {"path": artifacts[0]})["success"] is True


@pytest.mark.parametrize(
    "message",
    [
        "context_length_exceeded",
        "maximum context length is 32768 tokens",
        "Your input exceeds the context window of this model.",
    ],
)
def test_provider_context_rejection_is_not_a_reconnectable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def request(*_args: Any) -> Any:
        raise RuntimeError(message)

    result = run_fake(tmp_path, monkeypatch, request)
    assert result["status"] == "context_limit" and result["api_calls"] == 1


def test_durable_notes_cannot_inflate_compaction_or_duplicate_on_repeat(tmp_path: Path) -> None:
    notes = "saved evidence " * 10000
    (tmp_path / "PLAN_job.md").write_text(notes)
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "claim"},
        {"role": "assistant", "content": "old reasoning " * 1000},
    ]
    compacted, changed = compact_history(messages, context_tokens=1000, workspace=tmp_path)
    assert changed and approximate_tokens(compacted) <= 1000
    compacted.append({"role": "assistant", "content": "more reasoning " * 1000})
    compacted, changed = compact_history(compacted, context_tokens=1000, workspace=tmp_path)
    assert changed and approximate_tokens(compacted) <= 1000
    assert (
        sum(
            message.get("content", "").startswith("Current proof notes:\n") for message in compacted
        )
        == 1
    )
    assert (tmp_path / "PLAN_job.md").read_text() == notes


def test_model_profile_snapshot_resumes_without_losing_role_defaults(tmp_path: Path) -> None:
    path = project(tmp_path)
    config = ProverConfig(
        model="m",
        model_contexts={
            "m": {"context_tokens": 32000, "max_output_tokens": 4000, "compression_threshold": 0.6}
        },
    )
    runtime = ProverRuntime(root=tmp_path, targets=[path], config=config, verifier=Verifier())
    restored = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    assert restored.config == config
    assert restored.config.to_mapping("prover")["max_output_tokens"] == 4000
    runtime.progress.close()
    restored.progress.close()


def test_review_graph_deduplication_is_lossless_and_keeps_changes(tmp_path: Path) -> None:
    import copy

    from leanflow_cli.workflows.prover.session_assignment import compact_assignment

    nodes = [
        {
            "id": "a",
            "statement": "theorem a : True",
            "status": "proved",
            "dependencies": [],
            "informal_justification": "reason " * 1000,
        },
        {"id": "b", "statement": "theorem b : True", "status": "pending", "dependencies": []},
    ]
    proposed = copy.deepcopy(nodes)
    proposed[1]["dependencies"] = ["a"]
    context = {
        "dag": {"nodes": nodes, "roots": ["b"]},
        "proposed_dag": {"nodes": proposed, "roots": ["b"]},
    }
    message, _ = compact_assignment("Review", context, workspace=tmp_path, token_budget=2500)
    reduced = json.loads(message["content"].split("Assignment context:\n")[1])
    current = {node["id"]: node for node in reduced["dag"]["nodes"]}
    reconstructed = [
        current[node["same_as_current_dag_node"]] if "same_as_current_dag_node" in node else node
        for node in reduced["proposed_dag"]["nodes"]
    ]
    assert reconstructed == proposed
    assert reduced["proposed_dag"]["nodes"][1] == proposed[1]
    assert context["proposed_dag"]["nodes"] == proposed


def test_pinned_contract_above_soft_trigger_keeps_new_tool_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def request(_agent: Any, messages: Any, _timeout: float) -> Any:
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"PLAN_job.md","content":"evidence"}',
                        },
                    }
                ],
            }, {}
        assert any(
            message.get("role") == "tool" and message.get("tool_call_id") == "c1"
            for message in messages
        )
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(
        tmp_path,
        monkeypatch,
        request,
        context={"assignment": {"statement": "essential claim " * 1000}},
        config={"compression_threshold": 0.2},
    )
    assert result["status"] == "completed" and result["api_calls"] == 2
