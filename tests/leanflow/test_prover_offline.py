"""Pin the no-internet campaign boundary independently of clean-room filtering."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.planning_controller import install_planned_libraries
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.mark.parametrize(
    "role", ["prover", "negation", "research", "planner", "orchestrator", "review"]
)
def test_offline_removes_and_rejects_network_tools(tmp_path: Path, role: str) -> None:
    tools = SessionTools(
        role=role,
        project_root=tmp_path,
        workspace=tmp_path / "job",
        context={},
        allow_internet=False,
    )
    names = {item["function"]["name"] for item in tools.schemas()}
    assert not names & {"web_search", "fetch_resource"}
    for name in ("web_search", "fetch_resource"):
        assert not tools.invoke(name, {"query": "test", "url": "https://example.org"})["success"]
        with pytest.raises(ValueError, match="disabled"):
            tools._invoke(name, {})
    if role in {"planner", "orchestrator", "research", "negation"}:
        assert "compute" in names


def test_offline_lean_search_never_enters_remote_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.lean import lean_services

    monkeypatch.setattr(
        lean_services, "lean_search", lambda **_: pytest.fail("remote-capable search invoked")
    )
    source = tmp_path / ".lake/packages/mathlib/Mathlib/Test.lean"
    source.parent.mkdir(parents=True)
    source.write_text("theorem local_witness : True := by trivial\n")
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        context={},
        allow_internet=False,
    )
    result = tools.invoke("lean_search", {"query": "local_witness"})
    assert result["success"] and len(result["results"]) == 1
    assert result["results"][0]["path"] == str(source)


def test_offline_blocks_planned_dependencies_before_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import libraries

    monkeypatch.setattr(
        libraries, "install_libraries", lambda *_, **__: pytest.fail("installer invoked")
    )
    runtime = SimpleNamespace(config=ProverConfig(allow_internet=False), root=tmp_path)
    install_planned_libraries(runtime, [])
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime, [{"name": "remote"}])


def test_offline_config_survives_role_projection() -> None:
    config = ProverConfig.from_env({"LEANFLOW_PROVER_ALLOW_INTERNET": "0"})
    for role in ("prover", "negation", "orchestrator", "review", "research"):
        assert config.to_mapping(role)["allow_internet"] is False


def test_codex_rotation_reads_local_store_without_a_model_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanflow_cli.runtime import auth
    from leanflow_cli.workflows.prover.session_transport import refresh_stored_credentials

    refreshed = []
    agent = SimpleNamespace(
        provider="openai-codex",
        api_key="old",
        base_url="https://example.org",
        _try_refresh_codex_client_credentials=lambda **kwargs: refreshed.append(kwargs) or True,
    )
    monkeypatch.setattr(
        auth,
        "resolve_codex_runtime_credentials",
        lambda **_: {"api_key": "new", "base_url": "https://example.org"},
    )
    refresh_stored_credentials(agent)
    assert refreshed == [{"force": False}]
    monkeypatch.setattr(
        auth,
        "resolve_codex_runtime_credentials",
        lambda **_: {"api_key": "old", "base_url": "https://example.org"},
    )
    refresh_stored_credentials(agent)
    assert len(refreshed) == 1
