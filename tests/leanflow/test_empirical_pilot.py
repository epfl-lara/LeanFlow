"""Bound the synchronous planner's empirical terminal probes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from leanflow_cli.workflows import empirical_pilot


def test_prompt_contract_requires_small_non_exhaustive_pilot():
    contract = empirical_pilot.prompt_contract()

    assert f"at most {empirical_pilot.PILOT_CASE_LIMIT}" in contract
    assert f"at most {empirical_pilot.PILOT_TERMINAL_CALL_LIMIT} terminal calls" in contract
    assert f"at most {empirical_pilot.PILOT_TERMINAL_TIMEOUT_S} seconds" in contract
    assert "Never exhaustively enumerate" in contract
    assert "trial-divide a squared denominator" in contract
    assert "complete compatible residue basis" in contract
    assert "integrality or divisibility" in contract
    assert "return `inconclusive`" in contract


def test_terminal_pilot_clamps_timeout_and_disables_background():
    policy = empirical_pilot.BoundedTerminalPilot(timeout_s=7, max_calls=2)
    args = {"command": "python pilot.py", "timeout": 180, "background": True}

    result = policy("terminal", args)

    assert result is None
    assert args["timeout"] == 7
    assert args["background"] is False


def test_terminal_pilot_rejects_calls_after_cap_without_mutating_args():
    policy = empirical_pilot.BoundedTerminalPilot(timeout_s=7, max_calls=2)
    assert policy("lean_inspect", {}) is None  # non-terminal tools do not spend the cap
    assert policy("terminal", {"command": "first"}) is None
    assert policy("terminal", {"command": "second", "timeout": "bad"}) is None
    third = {"command": "third", "timeout": 99, "background": True}

    result = policy("terminal", third)

    assert result is not None
    assert result["status"] == "empirical_pilot_limit"
    assert result["terminal_calls"] == 2
    assert third == {"command": "third", "timeout": 99, "background": True}


def test_terminal_pilot_caps_a_concurrent_tool_batch():
    """A model-issued concurrent terminal batch must share one hard pilot cap."""
    policy = empirical_pilot.BoundedTerminalPilot(timeout_s=7, max_calls=2)
    calls = [
        {"command": f"python pilot_{index}.py", "timeout": 180, "background": True}
        for index in range(6)
    ]

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        results = list(executor.map(lambda args: policy("terminal", args), calls))

    assert sum(result is None for result in results) == 2
    assert (
        sum(
            isinstance(result, dict) and result.get("status") == "empirical_pilot_limit"
            for result in results
        )
        == 4
    )
    allowed = [args for args, result in zip(calls, results, strict=True) if result is None]
    assert all(args["timeout"] == 7 for args in allowed)
    assert all(args["background"] is False for args in allowed)
