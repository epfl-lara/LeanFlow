"""Tests for capacity-limited foreground verification ordering."""

from __future__ import annotations

import threading
import time

from agent.execution.tool_batch_priority import OrderedCapacityGate, foreground_tool_priority


def test_exact_target_check_outranks_diagnostic_inspection() -> None:
    """Schedule the authoritative target gate before a redundant inspection."""
    assert foreground_tool_priority(
        "lean_incremental_check",
        {"action": "check_target"},
    ) < foreground_tool_priority("lean_inspect", {"symbol": "demo"})


def test_verified_patch_precedes_same_batch_target_check() -> None:
    """Make an accepted source edit visible before checking its target."""
    assert foreground_tool_priority(
        "apply_verified_patch",
        {"patch": "..."},
    ) < foreground_tool_priority(
        "lean_incremental_check",
        {"action": "check_target"},
    )


def test_ordered_capacity_gate_waits_for_higher_priority_known_job() -> None:
    """Prevent a lower-ranked worker thread from winning a one-slot race."""
    gate = OrderedCapacityGate(1, {0: (20, 0), 1: (0, 1)})
    entered: list[int] = []

    def run(index: int) -> None:
        with gate.admit(index):
            entered.append(index)
            time.sleep(0.02)

    low = threading.Thread(target=run, args=(0,))
    high = threading.Thread(target=run, args=(1,))
    low.start()
    high.start()
    low.join(timeout=2)
    high.join(timeout=2)

    assert entered == [1, 0]
