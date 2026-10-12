"""Keep every research restriction effective without blocking native model connections."""

from __future__ import annotations

import pytest

from agent.runtime.harness.network_policy import restricted_research


@pytest.mark.parametrize("value", ["0", "false", " no ", "off", ""])
def test_disabled_restrictions_keep_normal_native_tools(value: str) -> None:
    assert not restricted_research(
        {
            "LEANFLOW_DISABLE_SOLUTION_RESEARCH": value,
            "LEANFLOW_DISABLE_REPOSITORY_RESEARCH": value,
            "LEANFLOW_PROVER_ALLOW_INTERNET": "true",
        }
    )


@pytest.mark.parametrize(
    "name", ["LEANFLOW_DISABLE_SOLUTION_RESEARCH", "LEANFLOW_DISABLE_REPOSITORY_RESEARCH"]
)
@pytest.mark.parametrize("value", ["1", " TRUE ", "yes", "on"])
def test_clean_room_and_repository_restrictions_require_local_tools(name: str, value: str) -> None:
    environment = {name: value, "LEANFLOW_PROVER_ALLOW_INTERNET": "true"}
    before = dict(environment)
    assert restricted_research(environment, allow_internet=True)
    assert environment == before


@pytest.mark.parametrize("value", ["0", " FALSE ", "no", "off"])
def test_online_argument_cannot_weaken_an_offline_environment(value: str) -> None:
    assert restricted_research({"LEANFLOW_PROVER_ALLOW_INTERNET": value}, allow_internet=True)


def test_explicit_offline_config_cannot_be_weakened_by_an_online_environment() -> None:
    assert restricted_research({"LEANFLOW_PROVER_ALLOW_INTERNET": "true"}, allow_internet=False)


def test_unset_policy_remains_online() -> None:
    assert not restricted_research({})
    assert not restricted_research({}, allow_internet=True)


def test_default_lookup_honors_process_restrictions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    assert restricted_research()
    assert not restricted_research({})
