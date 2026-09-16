"""Check real request headroom and recoverable evidence after compaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.context_policy import ContextBudget
from leanflow_cli.workflows.prover.session_context import approximate_tokens, compact_history
from tests.leanflow.test_prover_sessions import run_fake


def test_large_history_shrinks_to_distinct_target_and_remains_recoverable(tmp_path: Path) -> None:
    budget = ContextBudget.from_config({})
    pinned = [
        {"role": "system", "content": "immutable contract"},
        {"role": "user", "content": "theorem exact_helper : True := by sorry"},
    ]
    messages = pinned + [
        {"role": "assistant", "content": str(index) + "x" * 60000} for index in range(12)
    ]
    (tmp_path / "PLAN_job.md").write_text(
        "Verified helper h; current blocker is the final rewrite."
    )
    compacted, changed = compact_history(
        messages,
        context_tokens=budget.trigger_tokens,
        target_tokens=budget.target_tokens,
        hard_limit=budget.input_limit,
        workspace=tmp_path,
    )
    assert changed
    assert compacted[:2] == pinned
    assert approximate_tokens(compacted) <= budget.target_tokens
    assert budget.input_limit - approximate_tokens(compacted) > 100000
    assert "current blocker" in json.dumps(compacted)
    archive = next((tmp_path / "context-history").glob("*.json"))
    assert json.loads(archive.read_text())["messages"] == messages
    assert str(archive) in json.dumps(compacted)


def test_latest_tool_batch_and_signatures_survive_target_floor(tmp_path: Path) -> None:
    pinned = [{"role": "system", "content": "contract"}, {"role": "user", "content": "claim"}]
    batch = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [{"signature": "opaque"}],
            "tool_calls": [{"id": "c1"}, {"id": "c2"}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "feedback " * 300},
        {"role": "tool", "tool_call_id": "c2", "content": "other feedback " * 300},
    ]
    compacted, changed = compact_history(
        pinned + [{"role": "assistant", "content": "old " * 4000}] + batch,
        context_tokens=3000,
        target_tokens=300,
        hard_limit=4000,
        workspace=tmp_path,
    )
    assert changed and compacted[:2] == pinned and compacted[-3:] == batch
    assert 300 < approximate_tokens(compacted) < 4000


def test_unseen_tool_result_too_large_is_not_silently_discarded(tmp_path: Path) -> None:
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "claim"},
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "feedback " * 2000},
    ]
    compacted, _ = compact_history(
        messages,
        context_tokens=1000,
        target_tokens=500,
        hard_limit=2000,
        workspace=tmp_path,
    )
    assert compacted[-2:] == messages[-2:]
    assert approximate_tokens(compacted) > 2000  # caller must stop, not dispatch it


@pytest.mark.parametrize("suffix", ["TOKEN=private-value", "Authorization: Bearer private-value"])
def test_archive_redaction_preserves_json_structure(tmp_path: Path, suffix: str) -> None:
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "claim"},
        {"role": "assistant", "content": "old evidence " * 2000 + suffix},
    ]
    compacted, changed = compact_history(
        messages,
        context_tokens=1000,
        target_tokens=500,
        hard_limit=2000,
        workspace=tmp_path,
    )
    assert changed and compacted[:2] == messages[:2]
    archive = next((tmp_path / "context-history").glob("*.json"))
    saved = json.loads(archive.read_text())
    assert "private-value" not in archive.read_text()
    assert saved["messages"][-1]["content"].endswith("***")
    assert messages[-1]["content"].endswith(suffix)


def test_optional_notes_cannot_crowd_out_newest_tool_pair(tmp_path: Path) -> None:
    (tmp_path / "PLAN_job.md").write_text("n" * 14000)
    pinned = [{"role": "system", "content": "contract"}, {"role": "user", "content": "claim"}]
    pair = [
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "t" * 25000},
    ]
    compacted, changed = compact_history(
        pinned + [{"role": "assistant", "content": "old" * 30000}] + pair,
        context_tokens=10000,
        target_tokens=5000,
        hard_limit=12000,
        workspace=tmp_path,
    )
    assert changed and compacted[:2] == pinned and compacted[-2:] == pair
    assert approximate_tokens(compacted) <= 12000


def test_optional_handoff_cannot_crowd_out_newest_tool_pair(tmp_path: Path) -> None:
    pinned = [{"role": "system", "content": "contract"}, {"role": "user", "content": "claim"}]
    pair = [
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "feedback " * 300},
    ]
    mandatory = pinned + pair
    compacted, changed = compact_history(
        pinned + [{"role": "assistant", "content": "old" * 10000}] + pair,
        context_tokens=500,
        target_tokens=300,
        hard_limit=approximate_tokens(mandatory),
        workspace=tmp_path,
    )
    assert changed and compacted == mandatory
    assert len(list((tmp_path / "context-history").glob("*.json"))) == 1


def test_noop_compaction_does_not_create_archives(tmp_path: Path) -> None:
    messages = [{"role": "system", "content": "s" * 4000}, {"role": "user", "content": "claim"}]
    for _ in range(2):
        compacted, changed = compact_history(
            messages,
            context_tokens=1000,
            target_tokens=500,
            hard_limit=2000,
            workspace=tmp_path,
        )
        assert not changed and compacted == messages
    assert not list((tmp_path / "context-history").glob("*.json"))


def test_identical_accepted_compaction_reuses_archive(tmp_path: Path) -> None:
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "claim"},
        {"role": "assistant", "content": "old evidence " * 2000},
    ]
    replacements = []
    for _ in range(2):
        compacted, changed = compact_history(
            messages,
            context_tokens=1000,
            target_tokens=500,
            hard_limit=2000,
            workspace=tmp_path,
        )
        assert changed
        replacements.append(compacted)
    assert replacements[0] == replacements[1]
    assert len(list((tmp_path / "context-history").glob("*.json"))) == 1


def test_next_provider_request_has_headroom_and_current_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    def request(_agent: Any, messages: Any, _timeout: float) -> Any:
        requests.append(messages)
        if len(requests) < 5:
            return {"role": "assistant", "content": str(len(requests)) + "x" * 170000}, {}
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(
        tmp_path,
        monkeypatch,
        request,
        api_budget=5,
        config={"context_tokens": 256000},
        context={"assignment": {"statement": "theorem exact_helper : True := by sorry"}},
    )
    assert result["status"] == "completed" and result["api_calls"] == 5
    events = [json.loads(line) for line in (tmp_path / "job.jsonl").read_text().splitlines()]
    compactions = [e["details"] for e in events if e["type"] == "context-compacted"]
    assert compactions
    for event in compactions:
        assert event["target_met"] and event["after_tokens"] <= event["target_tokens"]
        assert event["headroom_tokens"] > 100000
        assert event["token_count_method"] == "utf8_estimate"
        actual_next = requests[event["api_calls"]]
        assert approximate_tokens(actual_next) <= event["target_tokens"]
        assert "theorem exact_helper : True := by sorry" in actual_next[1]["content"]


def test_target_config_is_role_specific_and_model_overridable() -> None:
    config = ProverConfig.from_env(
        {
            "LEANFLOW_PROVER_COMPRESSION_TARGET": "0.4",
            "LEANFLOW_PROVER_ORCHESTRATOR_COMPRESSION_TARGET": "0.6",
        }
    )
    assert config.to_mapping("prover")["compression_target"] == 0.4
    assert config.to_mapping("review")["compression_target"] == 0.6
    assert ProverConfig(**config.to_mapping()) == config
    config = ProverConfig(model="glm", model_contexts={"glm": {"compression_target": 0.3}})
    for role in ("prover", "negation", "orchestrator", "research", "review"):
        budget = ContextBudget.from_config(config.to_mapping(role))
        assert budget.target_tokens == int(budget.trigger_tokens * 0.3)


@pytest.mark.parametrize("value", [0, 1, -1, True, "0.5", float("nan")])
def test_invalid_target_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="compression_target"):
        ProverConfig(compression_target=value)
    with pytest.raises(ValueError, match="compression_target"):
        ProverConfig(model_contexts={"m": {"compression_target": value}})
