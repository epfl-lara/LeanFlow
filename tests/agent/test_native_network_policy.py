"""Pin clean-room launch refusals and actionable native network configuration guidance."""

from __future__ import annotations

import pytest

from agent.runtime.harness.network_policy import network_policy_error


@pytest.mark.parametrize("value", ["0", "false", " no ", "off", ""])
def test_disabled_research_restrictions_allow_native_execution(value: str) -> None:
    assert not network_policy_error(
        {
            "LEANFLOW_DISABLE_SOLUTION_RESEARCH": value,
            "LEANFLOW_DISABLE_REPOSITORY_RESEARCH": value,
        }
    )


def test_every_active_restriction_remains_visible_with_its_remedy() -> None:
    environment = {
        "LEANFLOW_DISABLE_SOLUTION_RESEARCH": " TRUE ",
        "LEANFLOW_DISABLE_REPOSITORY_RESEARCH": " yes ",
        "LEANFLOW_PROVER_ALLOW_INTERNET": " OFF ",
    }
    message = network_policy_error(environment)
    for setting in environment:
        assert setting in message
    assert "turn off Clean room" in message
    assert "enable Allow internet" in message
    assert "model-only network isolation" in message
    assert environment["LEANFLOW_PROVER_ALLOW_INTERNET"] == " OFF "


def test_explicit_offline_config_cannot_be_weakened_by_an_online_environment() -> None:
    assert "enable Allow internet" in network_policy_error(
        {"LEANFLOW_PROVER_ALLOW_INTERNET": "true"}, allow_internet=False
    )
