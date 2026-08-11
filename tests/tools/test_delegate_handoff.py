"""Characterize bounded delegated-research handoff extraction."""

from __future__ import annotations

from core.constants import WORKFLOW_STEP_BOUNDARY_INTERRUPT
from tools.utilities.delegate_handoff import (
    MAX_EVIDENCE_ITEMS,
    MAX_RESULT_CHARS,
    build_managed_interrupt_handoff,
)


def test_managed_handoff_is_deterministic_and_preserves_early_and_recent_evidence():
    calls = [
        {
            "id": f"call-{index}",
            "function": {
                "name": "web_fetch",
                "arguments": f'{{"url":"https://example.test/{index}"}}',
            },
        }
        for index in range(MAX_EVIDENCE_ITEMS + 4)
    ]
    messages: list[dict] = [{"role": "assistant", "tool_calls": calls}]
    messages.extend(
        {
            "role": "tool",
            "tool_call_id": f"call-{index}",
            "content": f"evidence-{index:02d} {'x' * (MAX_RESULT_CHARS + 100)}",
        }
        for index in range(len(calls))
    )
    result = {
        "interrupted": True,
        "interrupt_message": WORKFLOW_STEP_BOUNDARY_INTERRUPT,
        "messages": messages,
    }

    first = build_managed_interrupt_handoff(result)
    repeated = build_managed_interrupt_handoff(result)

    assert repeated == first
    evidence = first["evidence"]
    assert len(evidence) == MAX_EVIDENCE_ITEMS
    assert "evidence-00" in evidence[0]["result_excerpt"]
    assert "evidence-13" in evidence[-1]["result_excerpt"]
    assert all(len(item["result_excerpt"]) <= MAX_RESULT_CHARS for item in evidence)
