"""Bind formalization discovery and review to the managed native runtime and companion paths."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import preparation_discovery as discovery
from leanflow_cli.formalization import review
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.runtime import lemex_install as installer
from tests.leanflow.test_formalization_preparation_discovery import _response
from tests.leanflow.test_formalization_preparation_discovery import paper as paper
from tests.leanflow.test_formalization_revision_review import answer
from tests.leanflow.test_formalization_revision_review import source_record as source_record


@pytest.fixture
def managed_runtime(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Publish a complete local content pin without installation, probing or model execution."""
    state = tmp_path_factory.mktemp("formalization-managed-native")
    monkeypatch.setenv("LEANFLOW_HOME", str(state / "leanflow"))
    for backend in ("codex", "claude", "lemex"):
        key = "CLAUDE_CONFIG_DIR" if backend == "claude" else f"{backend.upper()}_HOME"
        home = state / f"native-{backend}"
        home.mkdir()
        (home / "config.toml").write_text("# Preserved native fixture configuration\n")
        monkeypatch.setenv(key, str(home))
    package = state / "package"
    (package / "bin").mkdir(parents=True)
    (package / "vendor/nested-platform").mkdir(parents=True)
    (package / "package.json").write_text('{"name":"lemex"}\n')
    executable = package / "bin/lemex"
    executable.write_text("#!/bin/sh\nexit 97\n")
    executable.chmod(0o700)
    (package / "vendor/nested-platform/companion").write_text("Complete runtime companion\n")
    identity, files = installer._tree_identity(package)
    distribution = installer._root() / "objects" / identity
    distribution.parent.mkdir(parents=True)
    package.rename(distribution)
    (installer._root() / "current.json").write_text(
        json.dumps({"schema": 1, "sha256": identity, "entrypoint": "bin/lemex", "files": files})
    )
    assert installer._read_install(verify=True)["sha256"] == identity
    return distribution / "bin/lemex", distribution


def _path_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shadow: bool) -> Path | None:
    """Give PATH either no Lemex or a conflicting executable independent of the managed pin."""
    binary_directory = tmp_path / "path-bin"
    binary_directory.mkdir()
    executable = binary_directory / "lemex"
    if shadow:
        executable.write_text("#!/bin/sh\nexit 98\n")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_directory))
    assert shutil.which("lemex") == (str(executable) if shadow else None)
    return executable if shadow else None


def _protected_sources(root: Path, record: dict[str, Any]) -> dict[str, bytes]:
    """Include original paper files alongside protected Lean and project sources."""
    return {
        **source_files(root),
        **{name: (root / name).read_bytes() for name in record["source_files"]},
    }


def _assert_request(
    request: Any,
    *,
    backend: str,
    root: Path,
    directory: Path,
    executable: Path,
    distribution: Path,
    managed: bool,
) -> None:
    """Check actual isolation grants and preserve evidence-only fresh-session restrictions."""
    assert request.backend == backend
    assert request.executable == (str(executable) if managed else "")
    assert request.tools is False and request.restricted_tools is True
    assert request.session_id == "" and request.previous_usage is None
    assert request.model == "fixture-model" and request.effort == "max"
    assert request.cwd.is_relative_to(directory) and request.mcp_config.is_file()
    policy = request.isolation
    assert policy.snapshot_root == request.cwd.resolve()
    assert root.resolve() in policy.denied_roots and directory.resolve() in policy.denied_roots
    assert request.cwd / ".mcp.json" in policy.denied_roots
    assert Path(discovery.__file__).resolve().parents[2] in policy.readonly_roots
    assert not distribution.is_relative_to(root)
    assert (distribution.resolve() in policy.readable_roots) is managed
    assert not any(distribution.resolve().is_relative_to(path) for path in policy.writable_roots)
    assert not any(path == root.resolve() for path in policy.writable_roots)
    companion = distribution / "vendor/nested-platform/companion"
    assert companion.read_text() == "Complete runtime companion\n"
    if managed:
        assert companion.is_relative_to(distribution)
        assert executable.parent not in policy.readable_roots


@pytest.mark.parametrize("shadow", [False, True], ids=["managed-only", "wrong-path-lemex"])
def test_discovery_selects_managed_lemex_and_grants_its_whole_distribution_readonly(
    paper, managed_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: bool
) -> None:
    root, record, plan = paper
    executable, distribution = managed_runtime
    wrong = _path_runtime(tmp_path, monkeypatch, shadow=shadow)
    assert installer.resolve_lemex_executable() == executable and executable != wrong
    assert installer.managed_distribution_root() == distribution
    original = _protected_sources(root, record)
    directory = root / ".leanflow/discovery"
    calls = []

    def invoke(request):
        calls.append(request)
        _assert_request(
            request,
            backend="lemex",
            root=root,
            directory=directory,
            executable=executable,
            distribution=distribution,
            managed=True,
        )
        request.on_started()
        return HarnessResult(
            "completed",
            json.dumps(_response(record, plan)),
            session_id="fresh-discovery",
            usage={"input_tokens": 3, "output_tokens": 2},
            cleanup_verified=True,
        )

    monkeypatch.setattr(discovery, "run_harness", invoke)
    budget = CampaignLimits(root / "limits.json", {"agent_calls": 1, "tokens": 5})
    result = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="lemex",
        model="fixture-model",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(root)},
        budget=budget,
        directory=directory,
    )
    assert len(calls) == 1 and result["accepted"] and result["status"] == "planned"
    assert result["semantic_approval"] is False and result["discovery"]["backend"] == "lemex"
    assert list(budget.state["invocations"].values()) == [
        {"status": "finished", "usage": {"input_tokens": 3, "output_tokens": 2}}
    ]
    assert _protected_sources(root, record) == original


@pytest.mark.parametrize("shadow", [False, True], ids=["managed-only", "wrong-path-lemex"])
@pytest.mark.parametrize(
    "override", [False, True], ids=["lemex-author", "codex-author-lemex-review"]
)
def test_review_uses_effective_lemex_backend_and_managed_companion_grant(
    source_record,
    managed_runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shadow: bool,
    override: bool,
) -> None:
    executable, distribution = managed_runtime
    wrong = _path_runtime(tmp_path, monkeypatch, shadow=shadow)
    assert installer.resolve_lemex_executable() == executable and executable != wrong
    original = _protected_sources(tmp_path, source_record)
    directory = tmp_path / "review"
    calls = []

    def invoke(request):
        calls.append(request)
        _assert_request(
            request,
            backend="lemex",
            root=tmp_path,
            directory=directory,
            executable=executable,
            distribution=distribution,
            managed=True,
        )
        request.on_started()
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        return HarnessResult(
            "completed",
            answer(
                assignment["dossier"],
                assignment["revision_id"],
                assignment["dossier_id"],
                decision="BLOCK",
            ),
            session_id="fresh-review",
            usage={"input_tokens": 3, "output_tokens": 2},
            cleanup_verified=True,
        )

    monkeypatch.setattr(review, "run_harness", invoke)
    environment = {"LEANFLOW_PROJECT_ROOT": str(tmp_path)}
    if override:
        environment.update(
            LEANFLOW_FORMALIZATION_REVIEW_PROVIDER="lemex",
            LEANFLOW_FORMALIZATION_REVIEW_MODEL="fixture-model",
            LEANFLOW_FORMALIZATION_REVIEW_EFFORT="max",
        )
    budget = CampaignLimits(tmp_path / "limits.json", {"agent_calls": 1, "tokens": 5})
    result = review.review_revision(
        source_record,
        backend="codex" if override else "lemex",
        model="author-model" if override else "fixture-model",
        effort="high" if override else "max",
        environment=environment,
        directory=directory,
        budget=budget,
    )
    assert len(calls) == 1 and not result["accepted"] and result["status"] == "completed"
    assert result["reviews"][0]["backend"] == "lemex"
    assert list(budget.state["invocations"].values()) == [
        {"status": "finished", "usage": {"input_tokens": 3, "output_tokens": 2}}
    ]
    assert _protected_sources(tmp_path, source_record) == original


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_other_discovery_backends_do_not_resolve_or_grant_managed_lemex(
    paper, managed_runtime, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    root, record, plan = paper
    executable, distribution = managed_runtime
    calls = []
    directory = root / ".leanflow/discovery"
    original = _protected_sources(root, record)

    def forbidden():
        pytest.fail("A non-Lemex discovery must not resolve or grant the managed Lemex runtime")

    monkeypatch.setattr(installer, "resolve_lemex_executable", forbidden)
    monkeypatch.setattr(installer, "managed_distribution_root", forbidden)

    def invoke(request):
        calls.append(request)
        _assert_request(
            request,
            backend=backend,
            root=root,
            directory=directory,
            executable=executable,
            distribution=distribution,
            managed=False,
        )
        request.on_started()
        return HarnessResult(
            "completed", json.dumps(_response(record, plan)), cleanup_verified=True
        )

    monkeypatch.setattr(discovery, "run_harness", invoke)
    result = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend=backend,
        model="fixture-model",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(root)},
        budget=CampaignLimits(root / "limits.json", {}),
        directory=directory,
    )
    assert len(calls) == 1 and result["accepted"] and not result["semantic_approval"]
    assert _protected_sources(root, record) == original


@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize(
    "override", [False, True], ids=["matching-author", "lemex-author-other-review"]
)
def test_other_review_backends_do_not_resolve_or_grant_managed_lemex(
    source_record,
    managed_runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    override: bool,
) -> None:
    executable, distribution = managed_runtime
    calls = []
    directory = tmp_path / "review"
    original = _protected_sources(tmp_path, source_record)

    def forbidden():
        pytest.fail(
            "A non-Lemex reviewer must not resolve or grant the author's managed Lemex runtime"
        )

    monkeypatch.setattr(installer, "resolve_lemex_executable", forbidden)
    monkeypatch.setattr(installer, "managed_distribution_root", forbidden)

    def invoke(request):
        calls.append(request)
        _assert_request(
            request,
            backend=backend,
            root=tmp_path,
            directory=directory,
            executable=executable,
            distribution=distribution,
            managed=False,
        )
        request.on_started()
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        return HarnessResult(
            "completed",
            answer(
                assignment["dossier"],
                assignment["revision_id"],
                assignment["dossier_id"],
                decision="BLOCK",
            ),
            cleanup_verified=True,
        )

    monkeypatch.setattr(review, "run_harness", invoke)
    environment = {"LEANFLOW_PROJECT_ROOT": str(tmp_path)}
    if override:
        environment.update(
            LEANFLOW_FORMALIZATION_REVIEW_PROVIDER=backend,
            LEANFLOW_FORMALIZATION_REVIEW_MODEL="fixture-model",
            LEANFLOW_FORMALIZATION_REVIEW_EFFORT="max",
        )
    result = review.review_revision(
        source_record,
        backend="lemex" if override else backend,
        model="author-model" if override else "fixture-model",
        effort="high" if override else "max",
        environment=environment,
        directory=directory,
        budget=CampaignLimits(tmp_path / "limits.json", {}),
    )
    assert len(calls) == 1 and not result["accepted"] and result["status"] == "completed"
    assert result["reviews"][0]["backend"] == backend
    assert _protected_sources(tmp_path, source_record) == original
