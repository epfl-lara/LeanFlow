"""Announce real long setup steps and durable diagnostic paths before execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.formalization import preparation, preparation_run, preparation_stage
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup import toolchain_install
from leanflow_cli.workflows.project_setup.process import SetupFailure
from tests.leanflow.preparation_helpers import inspector_readiness_record
from tests.leanflow.test_formalization_preparation_readiness import (
    staged_preparation as _staged_preparation,
)
from tests.leanflow.test_project_setup_toolchain_install import (
    _ensure,
    _installer,
)
from tests.leanflow.test_project_setup_toolchain_install import (
    acquisition as _acquisition,
)

staged_preparation = _staged_preparation
acquisition = _acquisition


def test_independent_checks_announce_existing_logs_and_keep_bounded_results_after_disposal(
    staged_preparation, monkeypatch
):
    context = staged_preparation
    root = context["root"]
    events = []
    observed = []

    def started(phase):
        event = next(row for row in reversed(events) if row.get("phase") == phase)
        path = Path(event["log_path"])
        assert event["type"] == "progress" and path.is_file()
        assert path.is_relative_to(preparation_directory(root))
        assert not path.is_relative_to(root)
        observed.append((phase, path))

    def compiler(*args, **kwargs):
        started("checking_toolchain")
        return {"resources": {"cleanup_verified": True}}

    def inspector(*args, **kwargs):
        started("checking_lean_compatibility")
        return inspector_readiness_record()

    def build(*args, **kwargs):
        started("checking_project")
        return {"success": True, "stdout": "API_KEY=private-value\n" + "x" * 100000}

    def imports(**kwargs):
        started("checking_imports")
        return {"ok": True}

    def hash_cache_check(stage, authority, policy, lease, **kwargs):
        started("checking_dependency_hash_cache")
        assert lease.path == stage and lease.cleanup_verified and lease.cleanup_observed
        assert policy.usage.cleanup_verified
        assert not list((stage / ".lake/packages").rglob("*.hash"))
        return authority

    def authorize(self, stage, *, check, git=None):
        phase = events[-1]["phase"]
        assert phase in {
            "checking_dependency_sources",
            "rechecking_dependency_sources",
            "selecting_preparation_changes",
            "publishing_preparation",
        }
        assert (git is not None) is (phase == "checking_dependency_sources")
        started(phase)
        check()

    def environment(stage):
        phase = events[-1]["phase"]
        assert phase == (
            "publishing_preparation" if stage == root else "fingerprinting_environment"
        )
        started(phase)
        return "checked-env"

    monkeypatch.setattr(preparation_run, "ensure_official_toolchain", compiler)
    monkeypatch.setattr(preparation_run, "check_inspector_runtime", inspector)
    monkeypatch.setattr(preparation_run, "build_project", build)
    monkeypatch.setattr(preparation_run, "check_scratch", imports)
    monkeypatch.setattr(preparation_run, "verify_generated_hash_caches", hash_cache_check)
    monkeypatch.setattr(preparation_run.PreparationAuthorization, "validate", authorize)
    monkeypatch.setattr(preparation_run, "environment_identity", environment)
    result = preparation.prepare_document_project(
        root,
        context["record"],
        plan=context["plan"],
        timeout_s=30,
        cancel=context["cancel"],
        progress=events.append,
    )
    assert result["accepted"]
    assert [phase for phase, _ in observed] == [
        "checking_toolchain",
        "checking_lean_compatibility",
        "checking_project",
        "checking_imports",
        "checking_dependency_hash_cache",
        "checking_dependency_sources",
        "fingerprinting_environment",
        "rechecking_dependency_sources",
        "selecting_preparation_changes",
        "publishing_preparation",
        "publishing_preparation",
        "publishing_preparation",
    ]
    assert not context["policies"][0].isolation.snapshot_root.parent.exists()
    for phase, path in observed:
        content = path.read_text()
        if phase != "publishing_preparation":
            assert "Diagnostic outcome only; not an acceptance receipt:" in content
        else:
            assert "Publishing checked infrastructure and rechecking installed inputs" in content
        assert "private-value" not in content
        assert len(content) < 64500


def test_acquisition_dependency_and_project_build_events_precede_the_commands(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root, stage = tmp_path / "canonical", tmp_path / "stage"
    root.mkdir()
    stage.mkdir()
    (stage / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    preparation_directory(root).mkdir(parents=True)
    events = []
    commands = []
    authorization = SimpleNamespace(acquire=lambda *args, **kwargs: "authorized")
    monkeypatch.setattr(
        preparation_stage.PreparationAuthorization,
        "capture",
        lambda *args, **kwargs: authorization,
    )
    monkeypatch.setattr(preparation_stage, "initialize_leanflow_project", lambda *a, **k: None)
    checkout = stage / ".lake/packages/sample"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(preparation_stage, "check_checkout", lambda *a, **k: checkout)

    def run(argv, **options):
        event = events[-1]
        assert event["type"] == "progress" and event["phase"] == options["phase"]
        assert Path(event["log_path"]).is_file()
        assert Path(event["log_path"]) == Path(options["log"].name)
        commands.append((argv, event["phase"]))
        return 0, ""

    monkeypatch.setattr(preparation_stage, "run_command", run)
    plan = {
        "toolchain": "leanprover/lean4:v4.33.1",
        "catalog_requirements": [],
        "existing_packages": [],
        "packages": [{"name": "sample", "revision": "a" * 40}],
    }
    selected, _ = preparation_stage.acquire_authorized_inputs(
        root,
        stage,
        plan,
        SimpleNamespace(check=lambda *a: None),
        cancel=None,
        progress=events.append,
    )
    assert selected == "authorized"
    (stage / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "sample", "type": "git", "rev": "a" * 40}]})
    )
    preparation_stage.build_prepared_dependencies(
        root, stage, plan, {}, SimpleNamespace(), cancel=None, progress=events.append
    )
    assert commands == [
        (["lake", "update", "sample"], "acquiring_packages"),
        (["lake", "build", "@sample"], "building_dependencies"),
        (["lake", "build"], "building_project"),
    ]


def test_missing_compiler_announces_actual_install_log_before_command(acquisition, monkeypatch):
    _installer(acquisition[2], "exit 0\n")
    events = []
    admitted = []

    def stop_before_effect(argv, **options):
        event = events[-1]
        assert event["phase"] == "acquiring_toolchain" and event["type"] == "progress"
        assert Path(event["log_path"]).is_file()
        assert Path(event["log_path"]) == Path(options["log"].name)
        admitted.append(argv)
        raise SetupFailure("fixture_before_install", "No installer was executed", "Fixture")

    monkeypatch.setattr(toolchain_install, "run_command", stop_before_effect)
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition, progress=events.append)
    assert error.value.code == "fixture_before_install"
    assert len(admitted) == 1
