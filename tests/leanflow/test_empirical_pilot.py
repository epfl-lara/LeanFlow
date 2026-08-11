"""Bound the synchronous planner's process-isolated empirical probes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from leanflow_cli.workflows import empirical_pilot


def test_prompt_contract_requires_small_non_exhaustive_pilot():
    contract = empirical_pilot.prompt_contract()

    assert f"at most {empirical_pilot.PILOT_CASE_LIMIT}" in contract
    assert f"at most {empirical_pilot.PILOT_COMPUTE_CALL_LIMIT} empirical_compute calls" in contract
    assert f"at most {empirical_pilot.PILOT_COMPUTE_TIMEOUT_S} seconds" in contract
    assert "no filesystem or project-mutation authority" in contract
    assert "Never exhaustively enumerate" in contract
    assert "trial-divide a squared denominator" in contract
    assert "complete compatible residue basis" in contract
    assert "integrality or divisibility" in contract
    assert "return `inconclusive`" in contract


def test_compute_pilot_clamps_timeout():
    policy = empirical_pilot.BoundedEmpiricalPilot(timeout_s=7, max_calls=2)
    args = {"program": "print(2 + 2)", "timeout_s": 180}

    result = policy("empirical_compute", args)

    assert result is None
    assert args["timeout_s"] == 7


def test_compute_pilot_rejects_calls_after_cap_without_mutating_args():
    policy = empirical_pilot.BoundedEmpiricalPilot(timeout_s=7, max_calls=2)
    assert policy("lean_inspect", {}) is None  # Lean checks do not spend the cap
    assert policy("empirical_compute", {"program": "print(1)"}) is None
    assert (
        policy(
            "empirical_compute",
            {"program": "print(2)", "timeout_s": "bad"},
        )
        is None
    )
    third = {"program": "print(3)", "timeout_s": 99}

    result = policy("empirical_compute", third)

    assert result is not None
    assert result["status"] == "empirical_pilot_limit"
    assert result["compute_calls"] == 2
    assert third == {"program": "print(3)", "timeout_s": 99}


def test_compute_pilot_caps_a_concurrent_tool_batch():
    """A model-issued concurrent compute batch must share one hard pilot cap."""
    policy = empirical_pilot.BoundedEmpiricalPilot(timeout_s=7, max_calls=2)
    calls = [{"program": f"print({index})", "timeout_s": 180} for index in range(6)]

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        results = list(executor.map(lambda args: policy("empirical_compute", args), calls))

    assert sum(result is None for result in results) == 2
    assert (
        sum(
            isinstance(result, dict) and result.get("status") == "empirical_pilot_limit"
            for result in results
        )
        == 4
    )
    allowed = [args for args, result in zip(calls, results, strict=True) if result is None]
    assert all(args["timeout_s"] == 7 for args in allowed)
