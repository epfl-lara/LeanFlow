"""Keep structural limits and research selection consistent across launch surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli import workflow
from leanflow_cli.flags import lookup_flag, resolve_profile
from leanflow_cli.flags.resolve import effective_flag_values
from leanflow_cli.flags.spec import FlagProfile
from leanflow_cli.workflows.prover.config import ProverConfig


@pytest.mark.parametrize("name", ["max_restarts", "plan_refinements", "max_decompositions"])
@pytest.mark.parametrize("raw, expected", [("", None), ("0", 0), ("7", 7)])
def test_structural_limits_are_optional_public_launch_settings(
    name: str, raw: str, expected: int | None
) -> None:
    env_name = "LEANFLOW_PROVER_" + name.upper()
    config = ProverConfig.from_env({env_name: raw})
    assert getattr(config, name) == expected
    assert getattr(ProverConfig(**config.to_mapping()), name) == expected
    spec = lookup_flag(env_name)
    assert spec is not None and spec.extension_editable
    assert spec.default == "" and spec.minimum == 0


@pytest.mark.parametrize("name", ["max_restarts", "plan_refinements", "max_decompositions"])
@pytest.mark.parametrize("raw", ["-1", "1.5", "nan", "true"])
def test_structural_limits_reject_invalid_counts(name: str, raw: str) -> None:
    with pytest.raises(ValueError):
        ProverConfig.from_env({"LEANFLOW_PROVER_" + name.upper(): raw})


def test_graph_limit_is_visible_and_preserves_its_existing_default() -> None:
    spec = lookup_flag("LEANFLOW_PROVER_MAX_NODES")
    assert spec is not None and spec.extension_editable
    assert spec.default == "128" and spec.minimum == 1
    assert ProverConfig.from_env({}).max_nodes == 128
    assert ProverConfig.from_env({spec.name: "19"}).max_nodes == 19
    with pytest.raises(ValueError):
        ProverConfig.from_env({spec.name: "0"})


@pytest.mark.parametrize("name", ["prover_sessions", "orchestrator_sessions", "negation_sessions"])
@pytest.mark.parametrize("raw, expected", [("", None), ("0", 0), ("9", 9)])
def test_role_invocation_limits_are_serialized_and_editable(
    name: str, raw: str, expected: int | None
) -> None:
    env_name = "LEANFLOW_PROVER_" + name.upper()
    config = ProverConfig.from_env({env_name: raw})
    assert getattr(config, name) == expected
    assert config.to_mapping()[name] == expected
    assert getattr(ProverConfig(**config.to_mapping()), name) == expected
    spec = lookup_flag(env_name)
    assert spec is not None and spec.extension_editable
    assert spec.default == "" and spec.minimum == 0


@pytest.mark.parametrize("name", ["prover_sessions", "orchestrator_sessions", "negation_sessions"])
@pytest.mark.parametrize("raw", ["-1", "2.5", "nan", "false"])
def test_role_invocation_limits_reject_invalid_counts(name: str, raw: str) -> None:
    with pytest.raises(ValueError):
        ProverConfig.from_env({"LEANFLOW_PROVER_" + name.upper(): raw})


def test_explicit_empty_structural_override_removes_a_profile_limit() -> None:
    name = "LEANFLOW_PROVER_PLAN_REFINEMENTS"
    profile = FlagProfile(name="limited", summary="", overrides={name: "5"})
    rows = {row["name"]: row for row in effective_flag_values(env={name: ""}, profile=profile)}
    assert rows[name]["source"] == "environment"
    assert rows[name]["value"] is None
    assert ProverConfig.from_env({**profile.overrides, name: ""}).plan_refinements is None


@pytest.fixture()
def native_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (
        "LEANFLOW_PROVER_MODE",
        "LEANFLOW_PROVER_PARALLELISM",
        "LEANFLOW_RESEARCH_MODE",
        "LEANFLOW_RESEARCH_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Test", "root": tmp_path})(),
    )
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda requested=None: {"provider": "codex"}
    )
    return tmp_path


@pytest.mark.parametrize("mode", ["standard", ""])
def test_explicit_standard_selection_beats_legacy_research_identity(
    native_launch: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_PROVER_MODE", mode)
    plan = workflow.resolve_workflow_request(
        "/prove Main.lean", active_cwd=native_launch, preview=True
    )
    assert not plan.workflow.research_mode
    assert plan.child_env["LEANFLOW_RESEARCH_MODE"] == "0"
    assert ProverConfig.from_env(plan.child_env).mode == "standard"


def test_builtin_research_profile_activates_native_controller_without_legacy_features(
    native_launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = resolve_profile("research")
    assert profile.overrides == {
        "LEANFLOW_PROVER_MODE": "research",
        "LEANFLOW_PROVER_PARALLELISM": "2",
    }
    for name, value in profile.overrides.items():
        monkeypatch.setenv(name, value)
    plan = workflow.resolve_workflow_request(
        "/prove Main.lean", active_cwd=native_launch, preview=True
    )
    assert plan.workflow.research_mode
    assert plan.child_env["LEANFLOW_RESEARCH_MODE"] == "1"
    config = ProverConfig.from_env(plan.child_env)
    assert (config.mode, config.parallelism) == ("research", 2)


@pytest.mark.parametrize(
    "options, expected",
    [("", 4), ("--no-parallel", 1), ("--research-workers 3", 3), ("--agents 5", 5)],
)
def test_research_launch_preserves_or_explicitly_overrides_native_parallelism(
    native_launch: Path, monkeypatch: pytest.MonkeyPatch, options: str, expected: int
) -> None:
    monkeypatch.setenv("LEANFLOW_PROVER_MODE", "research")
    monkeypatch.setenv("LEANFLOW_PROVER_PARALLELISM", "4")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "9")
    plan = workflow.resolve_workflow_request(
        f"/prove Main.lean {options}", active_cwd=native_launch, preview=True
    )
    assert plan.workflow.research_mode
    assert ProverConfig.from_env(plan.child_env).parallelism == expected


def test_explicit_research_cli_still_beats_standard_environment(
    native_launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_PROVER_MODE", "standard")
    plan = workflow.resolve_workflow_request(
        "/prove Main.lean --research", active_cwd=native_launch, preview=True
    )
    assert plan.workflow.research_mode
    assert ProverConfig.from_env(plan.child_env).mode == "research"
