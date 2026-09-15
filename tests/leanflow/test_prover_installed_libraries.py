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


def test_offline_accepted_plan_reaches_materialization_without_another_review(
    installed, monkeypatch
):
    """Reproduce PB026's accepted plan being discarded for its installed mathlib request."""
    from leanflow_cli.workflows.prover import planning_controller
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

    root, request = installed
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
        config=ProverConfig(mode="research", allow_internet=False),
        session=session,
    )
    assert owner._research_plan("Decompose locally")
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert materialized == [{"helper": "theorem helper : True := by sorry"}]
    assert owner.state["phase"] == "proving" and owner.state["proposal_status"] == "accepted"
    assert owner.dag.by_id()["helper"].status == "pending"


@pytest.mark.parametrize("mismatch", ["revision", "url", "name", "cache", "head", "manifest"])
def test_offline_dependency_noop_never_accepts_missing_or_different_package(installed, mismatch):
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


def test_cached_request_does_not_hide_a_new_request_in_same_batch(installed):
    root, request = installed
    other = {**request, "name": "new-library"}
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime(root), [request, other])


def test_offline_rejects_package_cache_escape(installed, tmp_path):
    root, request = installed
    package = root / ".lake/packages/mathlib"
    outside = tmp_path.parent / (tmp_path.name + "-external")
    package.rename(outside)
    package.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="disabled"):
        install_planned_libraries(runtime(root), [request])
