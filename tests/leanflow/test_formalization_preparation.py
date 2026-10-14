"""Verify private preparation failure, exact selection, and durable readiness independently of models."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.cli.formalization_command import (
    handle_formalization,
    register_formalization_parser,
)
from leanflow_cli.formalization import preparation, preparation_run
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.preparation_stage import add_packages, copy_stage
from leanflow_cli.formalization.records import create_document


def _paper(root: Path) -> dict[str, Any]:
    source = root / "paper.tex"
    source.write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
    return create_document(root, source)


def test_plan_cli_accepts_source_only_workspace_and_does_not_setup(tmp_path, capsys):
    record = _paper(tmp_path)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers()
    register_formalization_parser(commands)
    args = parser.parse_args(
        [
            "formalization",
            "--project",
            str(tmp_path),
            "plan",
            record["document_id"],
            "--expected-revision",
            record["revision_id"],
            "--json",
        ]
    )
    assert handle_formalization(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["semantic_approval"] is False
    assert {p["name"] for p in result["plan"]["packages"]} == {"mathlib", "repl"}
    assert not (tmp_path / "lakefile.toml").exists()
    assert not (tmp_path / "Main.lean").exists()
    args.expected_revision = "rev-obsolete"
    assert handle_formalization(args) == 1
    assert "Source selection changed" in capsys.readouterr().out


def test_private_copy_excludes_runtime_and_git_credential_configuration(tmp_path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    (root / "Main.lean").write_text("import Init\n")
    git = root / ".lake/packages/repl/.git"
    git.mkdir(parents=True)
    (git / "config").write_text("private credential URL")
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".codex").mkdir()
    (root / ".codex/auth.json").write_text("private native state")
    copy_stage(root, stage)
    assert (stage / "Main.lean").read_text() == "import Init\n"
    assert (stage / ".lake/packages/repl/.git/HEAD").is_file()
    assert not (stage / ".lake/packages/repl/.git/config").exists()
    assert not (stage / ".codex").exists()


def test_private_copy_matches_complete_local_lean_source_census(tmp_path):
    from leanflow_cli.workflows.prover.source import project_sources

    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    for name in (
        "Main.lean",
        ".hidden/Hidden.lean",
        "build/Generated.lean",
        "node_modules/pkg/Data.lean",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def value : Nat := 3\n")
    copy_stage(root, stage)
    assert {path.relative_to(root) for path in project_sources(root)} == {
        path.relative_to(stage) for path in project_sources(stage)
    }


def test_catalog_append_does_not_duplicate_scaffolded_mathlib(tmp_path):
    record = _paper(tmp_path)
    plan = default_preparation_plan(tmp_path, record)
    (tmp_path / "lakefile.toml").write_text(
        'name = "test"\n[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4"\nrev = "existing"\n'
    )
    add_packages(tmp_path, plan)
    text = (tmp_path / "lakefile.toml").read_text()
    assert text.count('name = "mathlib"') == 1
    assert text.count('name = "repl"') == 1
    assert 'rev = "existing"' in text


def test_failed_setup_never_changes_source_only_canonical_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path.parent / (tmp_path.name + "-home")))
    record = _paper(tmp_path)
    original = (tmp_path / "paper.tex").read_bytes()
    calls = []

    class Policy:
        usage = SimpleNamespace()

        def check(self, cancel):
            assert not cancel.is_set()

        def evidence(self):
            return {"cleanup_verified": True}

    monkeypatch.setattr(preparation_run, "build_setup_execution_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(
        preparation_run,
        "ensure_official_toolchain",
        lambda *a, **k: {"status": "reused", "resources": {"cleanup_verified": True}},
    )
    monkeypatch.setattr(preparation_run, "supervise_preparation", lambda *a, **k: nullcontext())
    monkeypatch.setattr(preparation, "ensure_storage_capacity", lambda root: None)
    monkeypatch.setattr(preparation_run, "run_command", lambda *a, **k: (0, ""))

    def failed(stage, **kwargs):
        calls.append(stage)
        assert stage != tmp_path and not stage.is_relative_to(tmp_path)
        assert (stage / "Main.lean").read_text() == "import Init\n"
        assert "sorry" not in (stage / "Main.lean").read_text()
        return {"status": "failed", "message": "Required package build failed"}

    monkeypatch.setattr(preparation_run, "prepare_project", failed)
    result = preparation.prepare_document_project(tmp_path, record, preset="lean")
    assert not result["accepted"] and result["status"] == "failed"
    assert len(calls) == 1 and (tmp_path / "paper.tex").read_bytes() == original
    assert not (tmp_path / "Main.lean").exists()
    assert not (tmp_path / "lakefile.toml").exists()
    assert not (tmp_path / ".lake").exists()
    assert not calls[0].exists(), "Verified stopped private dependency stages must be disposed"


def test_preparation_receipt_binds_infrastructure_not_published_aggregator_bytes(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "environment_id": "checked-env",
        "project_sources": {"Main.lean": "old-imports"},
    }
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    (state / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(preparation, "preparation_directory", lambda root: state)
    monkeypatch.setattr(preparation, "assert_no_pending_preparation", lambda root: None)
    monkeypatch.setattr(preparation, "environment_identity", lambda root: "checked-env")
    (tmp_path / "Main.lean").write_text("import Main.Formalization.DocumentChecked\n")
    with pytest.raises(ValueError, match="Prepared project source changed"):
        preparation.assert_preparation_current(tmp_path)
    assert (
        preparation.assert_preparation_current(tmp_path, check_project_sources=False)[
            "preparation_id"
        ]
        == receipt["preparation_id"]
    )
    monkeypatch.setattr(preparation, "environment_identity", lambda root: "new-compiler")
    with pytest.raises(ValueError, match="compiler, dependencies or checker changed"):
        preparation.assert_preparation_current(tmp_path)
    monkeypatch.setattr(preparation, "environment_identity", lambda root: "checked-env")
    receipt["environment_id"] = "forged"
    (state / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="corrupt"):
        preparation.assert_preparation_current(tmp_path)


def test_reuse_rebinds_only_checked_imports_and_creates_no_canonical_writes(tmp_path, monkeypatch):
    from leanflow_cli.formalization import preparation_reuse as reuse

    record = _paper(tmp_path)
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    plan["packages"] = []
    previous = {
        "source_bundle": "bundle-from-an-earlier-source-revision",
        "imports": ["Init"],
        "layout": plan["layout"],
        "resolved_packages": [],
        "environment_id": "env",
        "preparation_id": "previous",
        "project_sources": {},
    }
    captured = []
    monkeypatch.setattr(reuse, "environment_identity", lambda root: "env")

    def commit(root, staged, **kwargs):
        assert root == staged == tmp_path
        assert kwargs["managed"] == {}
        assert "paper.tex" in kwargs["protected"]
        kwargs["validate"]()
        captured.append(kwargs["receipt"])
        return {"transaction_id": "rebound"}

    monkeypatch.setattr(reuse, "commit_preparation", commit)
    path = tmp_path / ".leanflow/formalization-campaigns/new/budget.json"
    result = reuse.reuse_preparation(
        tmp_path, record, plan, previous, budget_path=path, cancelled=lambda: False
    )
    assert result["reused"] and captured[0]["budget_path"] == str(path)
    assert captured[0]["reused_preparation_id"] == "previous"
    assert captured[0]["source_bundle"] == record["source_bundle"]
    assert captured[0]["source_revision_id"] == record["revision_id"]
    assert previous["source_bundle"] == "bundle-from-an-earlier-source-revision"
    assert not (tmp_path / "Main.lean").exists()
    plan["required_imports"] = ["UnavailableNewLibrary"]
    assert (
        reuse.reuse_preparation(
            tmp_path, record, plan, previous, budget_path=path, cancelled=lambda: False
        )
        is None
    )
    assert len(captured) == 1


def test_second_controller_reuses_infrastructure_without_claiming_original_cleanup(
    tmp_path, monkeypatch
):
    from leanflow_cli.formalization import preparation_reuse as reuse
    from leanflow_cli.formalization.preparation_journal import (
        load_journal,
        load_registry,
        preparation_directory,
    )

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path.parent / (tmp_path.name + "-state")))
    record = _paper(tmp_path)
    source = (tmp_path / "paper.tex").read_bytes()
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    plan["packages"] = []
    initial = {
        "schema_version": 1,
        "status": "prepared",
        "source_bundle": record["source_bundle"],
        "imports": ["Init"],
        "layout": plan["layout"],
        "resolved_packages": [],
        "environment_id": "env",
        "project_sources": {},
    }
    script = """
import json, sys
from pathlib import Path
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_lifecycle import private_stage
from leanflow_cli.formalization.preparation_transaction import commit_preparation, protected_inputs
root, receipt = Path(sys.argv[1]), json.loads(sys.argv[2])
with private_stage(root) as lease:
    lease.require_cleanup()
    lease.verify_cleanup({"cleanup_verified": True})
    receipt["precommit_cleanup"] = lease.precommit_cleanup()
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    commit_preparation(root, root, managed={}, protected=protected_inputs(root),
                       receipt=receipt, validate=lambda: None, cancelled=lambda: False)
print(json.dumps(receipt))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(initial)],
        capture_output=True,
        text=True,
        check=True,
    )
    previous = json.loads(child.stdout)
    assert previous["precommit_cleanup"]["owner"]["pid"] != os.getpid()
    original_id = load_registry(tmp_path)["current"]
    original_path = preparation_directory(tmp_path) / "transactions" / (original_id + ".json")
    original_bytes = original_path.read_bytes()
    monkeypatch.setattr(reuse, "environment_identity", lambda root: "env")

    result = reuse.reuse_preparation(
        tmp_path, record, plan, previous, budget_path=None, cancelled=lambda: False
    )

    assert result["accepted"] and result["reused"]
    assert "precommit_cleanup" not in result["receipt"]
    assert result["receipt"]["reused_preparation_id"] == previous["preparation_id"]
    assert load_journal(tmp_path, result["transaction_id"])["writes"] == {}
    assert load_journal(tmp_path, original_id)["plan"]["receipt"] == previous
    assert original_path.read_bytes() == original_bytes
    assert (tmp_path / "paper.tex").read_bytes() == source
    preparation.assert_no_pending_preparation(tmp_path)


def test_preparation_display_requires_current_local_sources_without_rehashing_dependencies(
    tmp_path, monkeypatch
):
    from leanflow_cli.formalization.preparation_transaction import entry_identity

    source = tmp_path / "Main.lean"
    source.write_text("import Init\n")
    receipt = {"project_sources": {"Main.lean": entry_identity(source)}}
    calls = []

    def checked(root, *, check_project_sources):
        calls.append(check_project_sources)
        assert root == tmp_path and check_project_sources is False
        return receipt

    monkeypatch.setattr(preparation, "assert_preparation_current", checked)
    assert preparation.preparation_status(tmp_path)["current"] is True
    source.write_text("import Main.Formalization.DocumentChecked\n")
    stale = preparation.preparation_status(tmp_path)
    assert stale["current"] is False and stale["status"] == "preparation_required"
    assert "before the next translation" in stale["reason"]
    assert calls == [False, False]


def test_ready_private_stage_publishes_receipt_and_disposes_inputs(tmp_path, monkeypatch):
    from leanflow_cli.formalization.preparation_plan import DEFAULT_TOOLCHAIN
    from leanflow_cli.workflows.project_setup import state

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path.parent / (tmp_path.name + "-home")))
    record = _paper(tmp_path)
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    stages = []

    class Policy:
        usage = SimpleNamespace()

        def __init__(self, stage):
            self.stage = stage
            stages.append(stage)

        def check(self, cancel):
            assert not cancel.is_set()
            assert (self.stage / "lean-toolchain").read_text().strip() == DEFAULT_TOOLCHAIN

        def observe_disk(self):
            return None

        def evidence(self):
            return {"cleanup_verified": True}

    def ready(stage, **kwargs):
        binary = stage / ".lake/packages/repl/.lake/build/bin/repl"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"unit fixture compiled tooling")
        binary.chmod(0o755)
        (stage / "lake-manifest.json").write_text(
            json.dumps({"packages": [{"name": "repl", "rev": plan["packages"][0]["revision"]}]})
        )
        return {"status": "ready"}

    monkeypatch.setattr(
        preparation_run, "build_setup_execution_policy", lambda stage, **k: Policy(stage)
    )
    monkeypatch.setattr(
        preparation_run,
        "ensure_official_toolchain",
        lambda *a, **k: {"resources": {"cleanup_verified": True}},
    )
    monkeypatch.setattr(preparation_run, "supervise_preparation", lambda *a, **k: nullcontext())
    monkeypatch.setattr(preparation_run, "prepare_project", ready)
    monkeypatch.setattr(preparation_run, "run_command", lambda *a, **k: (0, ""))
    monkeypatch.setattr(preparation_run, "build_prepared_dependencies", lambda *a, **k: None)
    monkeypatch.setattr(
        preparation_run,
        "preflight_dependencies",
        lambda *a, **k: {
            **plan["layout"],
            "toolchain": DEFAULT_TOOLCHAIN.removeprefix("leanprover/lean4:v"),
        },
    )
    monkeypatch.setattr(preparation_run, "build_project", lambda *a, **k: {"success": True})
    monkeypatch.setattr(preparation_run, "check_scratch", lambda **k: {"ok": True})
    monkeypatch.setattr(preparation_run, "close_check_workers", lambda *a, **k: None)
    monkeypatch.setattr(preparation_run, "environment_identity", lambda root: "checked-env")
    monkeypatch.setattr(preparation, "ensure_storage_capacity", lambda root: None)
    monkeypatch.setattr(state, "fingerprint", lambda root: "checked-state")
    saved = []
    monkeypatch.setattr(state, "save_result", lambda root, value: saved.append(value))
    result = preparation.prepare_document_project(tmp_path, record, plan=plan)
    assert result["accepted"] and result["receipt"]["environment_id"] == "checked-env"
    assert (tmp_path / "Main.lean").read_text() == "import Init\n"
    assert (tmp_path / ".lake/packages/repl/.lake/build/bin/repl").is_file()
    assert (
        preparation.load_preparation_receipt(tmp_path)["preparation_id"]
        == result["receipt"]["preparation_id"]
    )
    assert saved[0]["status"] == "ready" and saved[0]["fingerprint"] == "checked-state"
    assert stages and not stages[0].parent.exists()
