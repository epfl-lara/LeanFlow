"""Keep repeated requests for cached pinned dependencies from restarting planning."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import libraries
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.planning_controller import install_planned_libraries


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Create a real local checkout and a matching immutable Lake lock entry."""
    package = tmp_path / ".lake/packages/mathlib"
    package.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(package)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(package),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(package), "rev-parse", "HEAD"], text=True
    ).strip()
    request = {
        "name": "mathlib",
        "git": "https://github.com/leanprover-community/mathlib4",
        "rev": revision,
    }
    (tmp_path / "lakefile.toml").write_text(
        'name="fixture"\n[[require]]\nname="mathlib"\ngit="'
        + request["git"]
        + '"\nrev="local-tag"\n'
    )
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.30.0\n")
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "mathlib", "url": request["git"], "rev": revision, "type": "git"}
                ]
            }
        )
    )
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *a, **k: pytest.fail("network invoked")
    )
    monkeypatch.setattr(
        libraries, "install_libraries", lambda *a, **k: pytest.fail("installer invoked")
    )
    return tmp_path, request


def runtime(root: Path):
    """Build the minimal owning runtime without starting a model session."""
    return SimpleNamespace(config=ProverConfig(allow_internet=False), root=root)


def test_repeated_exact_locked_dependency_is_a_readonly_noop_offline(installed):
    root, request = installed
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for _ in range(2):
        install_planned_libraries(runtime(root), [request])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "online,repository_name,clean_room",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
def test_accepted_plan_reaches_materialization_without_another_review(
    installed, monkeypatch, online, repository_name, clean_room
):
    """Exercise accepted plans with the real default timeout and cached Mathlib identity."""
    from leanflow_cli.workflows.prover import planning_controller
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

    root, request = installed
    if repository_name:
        request = {**request, "name": "mathlib4"}
    if clean_room:
        monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    source = root / "Main.lean"
    source.write_text("theorem goal : True ∧ True := by sorry\n")
    calls, materialized = [], []

    def session(**kwargs):
        calls.append(kwargs["role"])
        if len(calls) > 3:
            pytest.fail(
                "Accepted plan restarted planning or review instead of using installed library"
            )
        if kwargs["role"] == "review":
            response = {"accepted": True}
        elif '"nodes"' in kwargs["prompt"]:
            response = {
                "plan": "Use helper",
                "libraries": [request],
                "nodes": [
                    {"id": owner.dag.roots[0], "dependencies": ["helper"]},
                    {
                        "id": "helper",
                        "name": "helper",
                        "statement": "theorem helper : True := by sorry",
                        "dependencies": [],
                    },
                ],
            }
        else:
            response = {"plan": "Use helper"}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    monkeypatch.setattr(
        planning_controller,
        "materialize",
        lambda runtime, dag, skeletons: materialized.append(skeletons),
    )
    owner = ProverRuntime(
        root=root,
        targets=[source],
        config=ProverConfig(mode="research", allow_internet=online),
        session=session,
    )
    assert owner._research_plan("Decompose locally")
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert materialized == [{"helper": "theorem helper : True := by sorry"}]
    assert owner.state["phase"] == "proving" and owner.state["proposal_status"] == "accepted"
    assert owner.dag.by_id()["helper"].status == "pending"


def test_installer_configuration_error_preserves_review_without_replanning(installed, monkeypatch):
    """A controller configuration error cannot be repaired by another mathematical plan."""
    from leanflow_cli.workflows.prover import planning_controller
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure, ProverRuntime

    root, request = installed
    request = {**request, "name": "newlib", "git": "https://github.com/example/newlib"}
    source = root / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    calls = []

    def session(**kwargs):
        calls.append(kwargs["role"])
        if len(calls) > 3:
            pytest.fail("Configuration error restarted mathematical planning")
        response = (
            {"accepted": True}
            if kwargs["role"] == "review"
            else {"plan": "Keep the argument", "nodes": [], "libraries": [request]}
        )
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    monkeypatch.setattr(
        libraries,
        "install_libraries",
        lambda *a, **k: {
            "accepted": False,
            "status": "configuration_error",
            "error_code": "invalid_timeout",
            "error": "Library installation timeout must be a positive integer",
            "changes": [],
        },
    )
    monkeypatch.setattr(
        planning_controller, "materialize", lambda *a: pytest.fail("materialized after failure")
    )
    owner = ProverRuntime(
        root=root,
        targets=[source],
        config=ProverConfig(mode="research"),
        session=session,
    )
    with pytest.raises(InfrastructureFailure, match="configuration") as error:
        owner._research_plan("Build the plan")
    assert error.value.status == "environment_error"
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert owner.state["proposal_status"] == "validating"


@pytest.mark.parametrize("mismatch", ["revision", "url", "name", "cache", "head", "manifest"])
def test_offline_dependency_noop_never_accepts_missing_or_different_package(installed, mismatch):
    from leanflow_cli.workflows.prover.installed_libraries import libraries_already_installed

    root, request = installed
    request = dict(request)
    if mismatch == "revision":
        request["rev"] = "0" * 40
    elif mismatch == "url":
        request["git"] = "https://github.com/example/another-repo"
    elif mismatch == "name":
        request["name"] = "another"
    elif mismatch == "cache":
        (root / ".lake/packages/mathlib").rename(root / ".lake/packages/hidden")
    elif mismatch == "head":
        subprocess.run(
            [
                "git",
                "-C",
                str(root / ".lake/packages/mathlib"),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "different",
            ],
            check=True,
        )
    else:
        (root / "lake-manifest.json").unlink()
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime(root), [request])
    assert not libraries_already_installed(root, [request])


def test_cached_request_does_not_hide_a_new_request_in_same_batch(installed):
    from leanflow_cli.workflows.prover.installed_libraries import libraries_already_installed

    root, request = installed
    other = {**request, "name": "new-library"}
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime(root), [request, other])
    assert not libraries_already_installed(root, [request, other])


def test_offline_rejects_package_cache_escape(installed, tmp_path):
    from leanflow_cli.workflows.prover.installed_libraries import libraries_already_installed

    root, request = installed
    package = root / ".lake/packages/mathlib"
    outside = tmp_path.parent / (tmp_path.name + "-external")
    package.rename(outside)
    package.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime(root), [request])
    assert not libraries_already_installed(root, [request])


def test_repository_alias_requires_unique_exact_locked_identity(installed):
    from leanflow_cli.workflows.prover.installed_libraries import libraries_already_installed

    root, request = installed
    alias = {**request, "name": "mathlib4"}
    assert libraries_already_installed(root, [alias])
    assert not libraries_already_installed(root, [{**alias, "rev": "b" * 40}])
    manifest = root / "lake-manifest.json"
    data = json.loads(manifest.read_text())
    data["packages"].append({**data["packages"][0], "name": "otherMathlib"})
    manifest.write_text(json.dumps(data))
    assert not libraries_already_installed(root, [alias])
