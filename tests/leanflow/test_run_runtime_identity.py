"""Focused contracts for runtime, provider, terminal, and ambient provenance."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.cli import run_metrics, run_python_identity

_HERMETIC_ENV_KEEP = {"LEANFLOW_HOME", "LEANFLOW_QUEUE_INVARIANT_CHECKS"}
_HERMETIC_ENV_PREFIXES = ("LEANFLOW_", "TERMINAL_", "GIT_")
_REAL_SYS_PATH_IDENTITY = run_python_identity._sys_path_identity


class _FixtureDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


@pytest.fixture(autouse=True)
def _hermetic_machine_identity(tmp_path_factory, monkeypatch) -> None:
    """Give each test a private copy of every machine-global identity input.

    Python-runtime identity hashes the real sys.path entries (including this
    checkout, whose top-level import files a concurrent agent session may edit
    mid-test), the resolved openai/anthropic package trees, the
    installed-distribution inventory, and — through capture/seal — the whole
    installed runtime tree. On a developer machine those are live shared
    state: an out-of-band edit landing between two identity calls breaks
    equality assertions, a file replaced mid-scan surfaces as spurious
    unreadable issues, and hashing the real trees is slow enough under xdist
    load to risk the suite's 30s per-test watchdog. Repointing every input at
    test-owned stand-ins keeps the measured evidence immutable for the
    duration of a test. Tests that exercise one of these inputs re-enable it
    against a controlled stand-in: `_use_real_sys_path` for import-root
    hashing and `_patch_provider_specs` for provider-package trees.
    """
    fake_root = tmp_path_factory.mktemp("hermetic-runtime")
    for relative in ("leanflow_cli", "agent", "core", "tools", "leanflow_skills", "leanflow_specs"):
        module = fake_root / relative / "source.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(f"# {relative}\n", encoding="utf-8")
    (fake_root / "run_agent.py").write_text("# runner\n", encoding="utf-8")
    fake_module = fake_root / "leanflow_cli" / "cli" / "run_metrics.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# metrics\n", encoding="utf-8")
    monkeypatch.setattr(run_metrics, "__file__", str(fake_module))

    monkeypatch.setattr(run_python_identity, "_PRIMARY_PROVIDER_PACKAGES", ())
    monkeypatch.setattr(
        run_python_identity,
        "_sys_path_identity",
        lambda project_root=None: (
            {"sha256": "0" * 64, "entry_count": 0, "content_complete": True},
            [],
        ),
    )
    # openai/anthropic stay in the inventory so provider tests can hold the
    # reported distribution version fixed while the package content drifts.
    monkeypatch.setattr(
        run_metrics.importlib_metadata,
        "distributions",
        lambda: [
            _FixtureDistribution("openai", "1.0.0"),
            _FixtureDistribution("anthropic", "1.0.0"),
            _FixtureDistribution("leanflow-hermetic-fixture", "1.0.0"),
        ],
    )

    for key in list(os.environ):
        if key.startswith(_HERMETIC_ENV_PREFIXES) and key not in _HERMETIC_ENV_KEEP:
            monkeypatch.delenv(key, raising=False)


def _use_real_sys_path(monkeypatch, *entries: Path) -> None:
    """Run real import-root hashing over test-owned sys.path entries only.

    The live sys.path tail is shared machine state: a concurrent agent
    session editing this checkout between two identity calls would change
    entry hashes out from under an equality assertion.
    """
    monkeypatch.setattr(run_python_identity, "_sys_path_identity", _REAL_SYS_PATH_IDENTITY)
    monkeypatch.setattr(run_python_identity.sys, "path", [str(entry) for entry in entries])


def _project(project: Path) -> Path:
    """Create the minimum exact-metrics project and workflow-state layout."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "Main.lean").write_text("theorem runtime_identity : True := by trivial\n")
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc2\n")
    (project / "lake-manifest.json").write_text(json.dumps({"packages": []}))
    state = project / ".leanflow" / "workflow-state"
    (state / "activity" / "runs").mkdir(parents=True)
    (state / "runs").mkdir(parents=True)
    return state


def _provider_roots(root: Path) -> dict[str, Path]:
    """Create deterministic fake primary-provider package trees."""
    roots: dict[str, Path] = {}
    for package in ("openai", "anthropic"):
        package_root = root / "providers" / package
        (package_root / "resources").mkdir(parents=True)
        (package_root / "__init__.py").write_text(f'__version__ = "1.0-{package}"\n')
        (package_root / "resources" / "client.py").write_text(f"PROVIDER = {package!r}\n")
        roots[package] = package_root
    return roots


def _patch_provider_specs(monkeypatch, roots: dict[str, Path | tuple[str, Path]]) -> None:
    """Route primary-provider discovery to deterministic package or module roots."""
    original = run_python_identity.importlib.util.find_spec

    def find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        target = roots.get(name)
        if target is None:
            return original(name, *args, **kwargs)
        if isinstance(target, tuple):
            _scope, module = target
            return SimpleNamespace(origin=str(module), submodule_search_locations=None)
        return SimpleNamespace(
            origin=str(target / "__init__.py"),
            submodule_search_locations=[str(target)],
        )

    monkeypatch.setattr(run_python_identity, "_PRIMARY_PROVIDER_PACKAGES", tuple(roots))
    monkeypatch.setattr(run_python_identity.importlib.util, "find_spec", find_spec)


def _event(run_id: str, index: int, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
    """Return one canonical exact-metrics event."""
    return {
        "event_id": f"{run_id}-e{index}",
        "timestamp": f"2026-08-11T00:00:{index:02d}+00:00",
        "type": event_type,
        "run_id": run_id,
        "agent_id": "foreground",
        "message": "",
        "details": {
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "run_scope": "top-level",
            **details,
        },
    }


def _seal_success(project: Path, state: Path, run_id: str) -> None:
    """Seal a zero-provider-use successful run after provenance collection."""
    events = [
        _event(run_id, 0, "runner-start", {}),
        _event(
            run_id,
            1,
            "conversation-end",
            {
                "agent_session_id": "foreground",
                "api_calls": 0,
                "usage": {
                    "turn": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "cost": {
                        "source": "provider_reported",
                        "total_usd": 0.0,
                        "provider_reported_total_usd": 0.0,
                        "estimated_turn_usd": None,
                    },
                },
            },
        ),
        _event(run_id, 2, "runner-exit", {"exit_code": 0}),
    ]
    (state / "activity" / "runs" / f"{run_id}.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    )
    (state / "blueprint.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "node-runtime",
                        "name": "runtime_identity",
                        "file": "Main.lean",
                        "kind": "theorem",
                        "status": "proved",
                        "attempts": 1,
                        "api_steps": 0,
                    }
                ]
            }
        )
    )
    assert run_metrics.finalize_run_snapshot(
        state,
        run_id=run_id,
        project_root=project,
        outcome={
            "phase": "exited",
            "proof_solved": True,
            "sorry_count": 0,
            "project_sorry_count": 0,
            "model": "model-a",
            "provider": "provider-a",
            "exit_code": 0,
            "reason": "verified completion",
        },
    )


def test_python_runtime_binds_flags_paths_and_python_controls_without_plaintext(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project-secret-name"
    project.mkdir()
    secret_path = project / "sk-private-python-path"
    import_root = tmp_path / "import-root"
    import_root.mkdir()
    (import_root / "routing_module.py").write_text("ROUTED = 1\n")
    _use_real_sys_path(monkeypatch, import_root)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(project), str(secret_path))))
    monkeypatch.setenv("PYTHONHASHSEED", "credential-shaped-python-secret")
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")

    first = run_metrics._python_runtime_identity(project)
    serialized = json.dumps(first, sort_keys=True)
    runtime = first["python_runtime"]

    assert first["python_runtime_complete"] is True
    assert runtime["flags"]["optimize"] == sys.flags.optimize
    assert len(runtime["executable"]["content_sha256"]) == 64
    assert len(runtime["prefix"]["path_sha256"]) == 64
    assert runtime["sys_path"]["content_complete"] is True
    assert runtime["python_environment"]["PYTHONPATH"].startswith("[content-sha256:")
    for plaintext in (
        str(project),
        str(secret_path),
        "credential-shaped-python-secret",
        sys.executable,
        sys.prefix,
    ):
        assert plaintext not in serialized

    monkeypatch.setenv("PYTHONHASHSEED", "changed-python-control")
    second = run_metrics._python_runtime_identity(project)
    assert second["python_runtime_sha256"] != first["python_runtime_sha256"]

    monkeypatch.setenv("PYTHONHASHSEED", "credential-shaped-python-secret")
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    optimized = run_metrics._python_runtime_identity(project)
    assert optimized["python_runtime_sha256"] != first["python_runtime_sha256"]

    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(project), str(tmp_path / "other-path"))))
    rerouted = run_metrics._python_runtime_identity(project)
    assert rerouted["python_runtime_sha256"] != first["python_runtime_sha256"]


def test_clone_roots_normalize_but_import_content_changes_python_identity(
    tmp_path: Path, monkeypatch
) -> None:
    clone_a = tmp_path / "clone-a"
    clone_b = tmp_path / "clone-b"
    for clone in (clone_a, clone_b):
        package = clone / "sample_package"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 1\n")
        (clone / "top_level.py").write_text("TOP = 1\n")
        (clone / "README.md").write_text("non-import documentation\n")
        (clone / "Proof.lean").write_text("theorem p : True := by trivial\n")
    stable_tail = tmp_path / "stable-import-tail"
    stable_tail.mkdir()
    (stable_tail / "tail_module.py").write_text("TAIL = 1\n")

    _use_real_sys_path(monkeypatch, clone_a, stable_tail)
    first = run_metrics._python_runtime_identity(clone_a)
    _use_real_sys_path(monkeypatch, clone_b, stable_tail)
    equivalent = run_metrics._python_runtime_identity(clone_b)
    assert equivalent["python_runtime_sha256"] == first["python_runtime_sha256"]

    (clone_b / "README.md").write_text("changed but still not import routing\n")
    unrelated = run_metrics._python_runtime_identity(clone_b)
    assert unrelated["python_runtime_sha256"] == equivalent["python_runtime_sha256"]

    (clone_b / "sample_package" / "__init__.py").write_text("VALUE = 2\n")
    changed = run_metrics._python_runtime_identity(clone_b)
    assert changed["python_runtime_sha256"] != equivalent["python_runtime_sha256"]


def test_provider_package_content_and_single_module_shadowing_are_exact(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    roots = _provider_roots(project)
    _patch_provider_specs(monkeypatch, roots)

    first = run_metrics._python_runtime_identity(project)
    provider_version = first["installed_distributions"]["openai"]
    (roots["openai"] / "resources" / "client.py").write_text("PROVIDER = 'modified'\n")
    modified = run_metrics._python_runtime_identity(project)

    assert modified["installed_distributions"]["openai"] == provider_version
    assert (
        modified["python_runtime"]["primary_provider_packages"]["openai"]["sha256"]
        != first["python_runtime"]["primary_provider_packages"]["openai"]["sha256"]
    )
    assert modified["python_runtime_sha256"] != first["python_runtime_sha256"]

    shadow = project / "shadow" / "openai.py"
    shadow.parent.mkdir()
    shadow.write_text("SHADOW = 1\n")
    sibling = shadow.parent / "unrelated.py"
    sibling.write_text("UNRELATED = 1\n")
    _patch_provider_specs(
        monkeypatch, {"openai": ("single-module", shadow), "anthropic": roots["anthropic"]}
    )
    shadowed = run_metrics._python_runtime_identity(project)
    openai_identity = shadowed["python_runtime"]["primary_provider_packages"]["openai"]
    assert openai_identity["resolution_scope"] == "single-module"
    assert openai_identity["file_count"] == 1

    sibling.write_text("UNRELATED = 2\n")
    sibling_changed = run_metrics._python_runtime_identity(project)
    assert (
        sibling_changed["python_runtime"]["primary_provider_packages"]["openai"]["sha256"]
        == openai_identity["sha256"]
    )
    shadow.write_text("SHADOW = 2\n")
    shadow_changed = run_metrics._python_runtime_identity(project)
    assert (
        shadow_changed["python_runtime"]["primary_provider_packages"]["openai"]["sha256"]
        != openai_identity["sha256"]
    )


def test_provider_content_drift_makes_a_sealed_run_inexact(tmp_path: Path, monkeypatch) -> None:
    from leanflow_cli.config import invalidate_config_cache

    project = tmp_path / "project"
    state = _project(project)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    invalidate_config_cache()
    roots = _provider_roots(project)
    _patch_provider_specs(monkeypatch, roots)

    assert run_metrics.capture_run_launch_snapshot(
        state, run_id="provider-drift", project_root=project
    )
    (roots["openai"] / "resources" / "client.py").write_text("PROVIDER = 'drifted'\n")
    _seal_success(project, state, "provider-drift")

    metrics = run_metrics.collect_run_metrics(state, run_id="provider-drift", project_root=project)
    assert metrics["scope"]["exact"] is False
    assert metrics["python_runtime_changed"] is True
    assert "python-runtime-drift" in metrics["scope"]["missing"]
    invalidate_config_cache()


def test_terminal_and_ambient_controls_are_bound_redacted_and_clone_stable(
    tmp_path: Path, monkeypatch
) -> None:
    from leanflow_cli.config import invalidate_config_cache

    home = tmp_path / "home"
    clone_a = tmp_path / "clone-a"
    clone_b = tmp_path / "clone-b"
    clone_a.mkdir()
    clone_b.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    monkeypatch.setenv("TERMINAL_CWD", str(clone_a))
    monkeypatch.setenv("PATH", os.pathsep.join((str(clone_a / "bin"), "/usr/bin")))
    monkeypatch.setenv("TERMINAL_SSH_HOST", "alice:secret@host.example.test")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "registry.example.test/private/image:token")
    invalidate_config_cache()

    baseline = run_metrics.collect_launch_environment(project_root=clone_a)
    baseline_raw = json.dumps(baseline["behavior_config"], sort_keys=True)
    assert baseline["behavior_config_complete"] is True
    assert baseline["behavior_config"]["terminal_runtime"]["controls"]["TERMINAL_TIMEOUT"] == 180
    assert baseline["behavior_config"]["ambient_runtime"]["complete"] is True
    assert baseline["behavior_config"]["ambient_runtime"]["configured_count"] > 0
    for plaintext in (
        str(clone_a),
        "alice:secret@host.example.test",
        "registry.example.test/private/image:token",
    ):
        assert plaintext not in baseline_raw

    monkeypatch.setenv("TERMINAL_TIMEOUT", "181")
    timeout_changed = run_metrics.collect_launch_environment(project_root=clone_a)
    assert timeout_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    backend_changed = run_metrics.collect_launch_environment(project_root=clone_a)
    assert backend_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]
    monkeypatch.setenv("TERMINAL_ENV", "local")

    monkeypatch.setenv("TERMINAL_RESEARCH_TOKEN", "terminal-arbitrary-secret-one")
    arbitrary_changed = run_metrics.collect_launch_environment(project_root=clone_a)
    assert arbitrary_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]
    assert run_metrics._launch_environment_is_valid(arbitrary_changed) is True
    assert "terminal-arbitrary-secret-one" not in json.dumps(arbitrary_changed, sort_keys=True)
    monkeypatch.delenv("TERMINAL_RESEARCH_TOKEN")

    monkeypatch.setenv("LEAN_RUNTIME_API_KEY", "credential-shaped-ambient-secret")
    ambient_changed = run_metrics.collect_launch_environment(project_root=clone_a)
    assert ambient_changed["behavior_config_sha256"] != baseline["behavior_config_sha256"]
    assert run_metrics._launch_environment_is_valid(ambient_changed) is True
    assert "credential-shaped-ambient-secret" not in json.dumps(ambient_changed, sort_keys=True)
    monkeypatch.delenv("LEAN_RUNTIME_API_KEY")

    monkeypatch.setenv("TERMINAL_CWD", str(clone_b))
    monkeypatch.setenv("PATH", os.pathsep.join((str(clone_b / "bin"), "/usr/bin")))
    equivalent_clone = run_metrics.collect_launch_environment(project_root=clone_b)
    assert equivalent_clone["behavior_config_sha256"] == baseline["behavior_config_sha256"]

    monkeypatch.setenv("HOME", str(tmp_path / "different-home"))
    home_changed = run_metrics.collect_launch_environment(project_root=clone_b)
    assert home_changed["behavior_config_sha256"] != equivalent_clone["behavior_config_sha256"]
    invalidate_config_cache()
