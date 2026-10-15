"""Reject incompatible inspectors before setup publication or a translation author can run."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import preparation, preparation_run, preparation_stage
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inspector_readiness import valid_inspector_readiness
from leanflow_cli.formalization.preparation_plan import DEFAULT_TOOLCHAIN, default_preparation_plan
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.workflows.project_setup import state
from leanflow_cli.workflows.project_setup.execution import (
    SetupExecutionPolicy,
    build_setup_execution_policy,
)
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure
from tests.leanflow.preparation_helpers import inspector_readiness_record


@pytest.fixture
def staged_preparation(tmp_path, monkeypatch):
    """Mock compiler work while retaining the real preparation supervisor and transaction."""
    root = tmp_path / "project"
    root.mkdir()
    source = root / "paper.tex"
    source.write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LEANFLOW_SETUP_MAX_RSS_GIB", raising=False)
    elan = tmp_path / "elan"
    compiler = elan / "toolchains" / DEFAULT_TOOLCHAIN.replace("/", "--").replace(":", "---")
    (compiler / "bin").mkdir(parents=True)
    for name in ("lean", "lake"):
        binary = compiler / "bin" / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    monkeypatch.setenv("ELAN_HOME", str(elan))
    record = create_document(root, source)
    plan = default_preparation_plan(root, record, preset="lean")
    context: dict[str, Any] = {
        "root": root,
        "record": record,
        "plan": plan,
        "events": [],
        "policies": [],
        "commits": [],
        "cancel": threading.Event(),
        "inside_supervisor": False,
    }

    def policy(stage, **options):
        value = build_setup_execution_policy(stage, **options)
        context["policies"].append(value)
        return value

    @contextmanager
    def supervised(selected, **options):
        with supervise_preparation(selected, **options):
            context["inside_supervisor"] = True
            try:
                yield
            finally:
                context["inside_supervisor"] = False

    def ready(stage, **options):
        binary = stage / ".lake/packages/repl/.lake/build/bin/repl"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"unit fixture tooling; not a Lean execution")
        binary.chmod(0o755)
        (stage / "lake-manifest.json").write_text(json.dumps({"packages": []}))
        return {"status": "ready"}

    def imports(**options):
        context["events"].append("imports")
        context["cancelled"] = options["cancelled"]
        return {"ok": True}

    actual_commit = preparation_run.commit_preparation

    def commit(*args, **options):
        context["commits"].append(options["receipt"])
        return actual_commit(*args, **options)

    monkeypatch.setattr(preparation_run, "build_setup_execution_policy", policy)
    monkeypatch.setattr(preparation_run, "supervise_preparation", supervised)
    monkeypatch.setattr(
        preparation_run,
        "ensure_official_toolchain",
        lambda *a, **k: {"resources": {"cleanup_verified": True}},
    )
    monkeypatch.setattr(preparation_run, "prepare_project", ready)
    monkeypatch.setattr(preparation_stage, "run_command", lambda *a, **k: (0, ""))
    monkeypatch.setattr(preparation_run, "build_prepared_dependencies", lambda *a, **k: None)
    # This fixture deliberately mocks acquisition; package authority has separate
    # real-checkout and sandboxed-build regressions.
    monkeypatch.setattr(
        preparation_run.PreparationAuthorization, "acquire", lambda self, *a, **k: self
    )
    monkeypatch.setattr(
        preparation_run.PreparationAuthorization, "validate", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        preparation_run,
        "preflight_dependencies",
        lambda *a, **k: {
            **plan["layout"],
            "toolchain": DEFAULT_TOOLCHAIN.removeprefix("leanprover/lean4:v"),
        },
    )
    monkeypatch.setattr(preparation_run, "build_project", lambda *a, **k: {"success": True})

    # Acquisition and compiler work above deliberately provide no Git authority
    # or generated Lake caches; hash derivation is exercised by its real fixtures.
    def verify_hash_caches(
        stage: Path, _authority: Any, policy: SetupExecutionPolicy, lease: Any, **_options: Any
    ) -> Any:
        """Substitute only the fully mocked stage's absent hash-cache derivation."""
        assert policy.usage.cleanup_verified and lease.cleanup_verified
        assert not list(stage.rglob("*.hash"))
        return _authority

    monkeypatch.setattr(preparation_run, "verify_generated_hash_caches", verify_hash_caches)
    monkeypatch.setattr(preparation_run, "check_scratch", imports)
    monkeypatch.setattr(
        preparation_run,
        "close_check_workers",
        lambda *a, **k: context["events"].append("workers_closed"),
    )
    monkeypatch.setattr(preparation_run, "commit_preparation", commit)
    monkeypatch.setattr(preparation_run, "environment_identity", lambda root: "checked-env")
    monkeypatch.setattr(preparation, "environment_identity", lambda root: "checked-env")
    monkeypatch.setattr(preparation, "ensure_storage_capacity", lambda root: None)
    monkeypatch.setattr(state, "fingerprint", lambda root: "checked-state")
    return context


@pytest.mark.parametrize("outcome", ["success", "incompatible", "nonempty", "cancelled"])
def test_private_readiness_shares_budget_and_closes_failure_children(
    staged_preparation, monkeypatch, outcome
):
    context = staged_preparation
    root = context["root"]
    before = (root / "paper.tex").read_bytes()
    progress = []
    children = []

    def check(stage, workspace, *, deadline, cancelled):
        context["events"].append("inspector")
        assert context["events"] == ["inspector"]
        assert context["inside_supervisor"]
        selected = context["policies"][0]
        assert deadline == selected.deadline
        context["inspector_cancelled"] = cancelled
        assert selected.max_owned_rss_bytes == 16 * 1024**3
        assert selected.max_disk_growth_bytes == 12 * 1024**3
        assert workspace.is_relative_to(stage) and stage != root
        assert not cancelled()
        if outcome == "cancelled":
            context["cancel"].set()
            assert cancelled()
            return {"success": False, "details": {"error_code": "check_cancelled"}}
        if outcome == "incompatible":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            children.append(child)
            time.sleep(0.4)
            return {
                "success": False,
                "details": {"stdout": "unknown constant in trusted inspector"},
            }
        result = inspector_readiness_record()
        if outcome == "nonempty":
            result["declaration_rows"] = 1
        return result

    monkeypatch.setattr(preparation_run, "check_inspector_runtime", check)
    if outcome == "success":
        result = preparation.prepare_document_project(
            root,
            context["record"],
            plan=context["plan"],
            cancel=context["cancel"],
            timeout_s=30,
            progress=progress.append,
        )
        assert result["accepted"] and len(context["commits"]) == 1
        readiness = result["receipt"]["inspector_readiness"]
        assert valid_inspector_readiness(readiness) and readiness["proof_acceptance"] is False
        assert preparation.assert_preparation_current(root)["inspector_readiness"] == readiness
        assert context["events"] == ["inspector", "imports", "workers_closed"]
        assert context["inspector_cancelled"] is context["cancelled"]
    else:
        with pytest.raises(SetupCancelled if outcome == "cancelled" else SetupFailure) as raised:
            preparation.prepare_document_project(
                root,
                context["record"],
                plan=context["plan"],
                cancel=context["cancel"],
                timeout_s=30,
                progress=progress.append,
            )
        if outcome != "cancelled":
            assert raised.value.code == "setup_inspector_incompatible"
        assert context["commits"] == [] and preparation.load_preparation_receipt(root) is None
        assert not (root / "Main.lean").exists() and not (root / ".lake").exists()
        assert context["events"] == ["inspector"]
    assert any(row["message"] == "Checking Lean compatibility" for row in progress)
    assert (root / "paper.tex").read_bytes() == before
    assert context["policies"][0].usage.cleanup_verified
    assert not context["policies"][0].isolation.snapshot_root.parent.exists()
    for child in children:
        child.wait(timeout=3)
        assert child.poll() is not None


@pytest.mark.parametrize("kind", ["missing", "stale", "nonempty"])
def test_current_or_reused_receipt_requires_exact_inspector_readiness(tmp_path, monkeypatch, kind):
    from leanflow_cli.formalization import preparation_reuse

    receipt = {"schema_version": 1, "status": "prepared"}
    if kind != "missing":
        readiness = inspector_readiness_record()
        readiness["checker_sha256" if kind == "stale" else "declaration_rows"] = (
            "old" if kind == "stale" else 1
        )
        receipt["inspector_readiness"] = readiness
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    directory = tmp_path / "preparation"
    directory.mkdir()
    (directory / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(preparation, "preparation_directory", lambda root: directory)
    monkeypatch.setattr(preparation, "assert_no_pending_preparation", lambda root: None)
    # Keep this fabricated receipt focused on inspector staleness, independently of membership tests.
    monkeypatch.setattr(preparation, "require_selected_preparation", lambda root, receipt: None)
    monkeypatch.setattr(
        preparation_reuse,
        "commit_preparation",
        lambda *a, **k: pytest.fail("Unverified readiness obtained metadata-only reuse"),
    )
    with pytest.raises(RepreparationRequired, match="Lean compatibility") as current:
        preparation.assert_preparation_current(tmp_path)
    assert current.value.error_code == "needs_reprepare"
    with pytest.raises(RepreparationRequired, match="Lean compatibility"):
        preparation_reuse.reuse_preparation(
            tmp_path, {}, {}, receipt, budget_path=None, cancelled=lambda: False
        )


def test_failed_readiness_blocks_formalization_intake_and_paid_retry(
    staged_preparation, monkeypatch
):
    from leanflow_cli import workflow
    from leanflow_cli.formalization import preparation_launch

    context = staged_preparation
    root = context["root"]
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DISCOVERY", "false")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_PRESET", "lean")
    monkeypatch.setattr(
        workflow, "resolve_runtime_provider", lambda **k: {"provider": "codex", "model": "unit"}
    )
    monkeypatch.setattr(
        preparation_launch,
        "load_config",
        lambda: {"limits": {"agent_calls": 3, "tokens": None, "wall_time_s": 30}},
    )
    # This case tests a later private inspector failure, after manual baseline
    # readiness; the fixture already substitutes all compiler/tool acquisition.
    monkeypatch.setattr(
        preparation_launch,
        "inspect_project",
        lambda root: {"root": str(root), "ready": True, "configured": True},
    )
    monkeypatch.setattr(
        preparation_launch,
        "discover_preparation_plan",
        lambda *a, **k: pytest.fail("Disabled discovery spent a model call"),
    )
    monkeypatch.setattr(
        workflow,
        "prepare_formalization_document_context",
        lambda *a, **k: pytest.fail("Incompatible inspector reached translation intake"),
    )
    monkeypatch.setattr(
        preparation_run,
        "check_inspector_runtime",
        lambda *a, **k: {"success": False, "details": {"stdout": "incompatible compiler API"}},
    )
    before = (root / "paper.tex").read_bytes()
    for _ in range(2):
        with pytest.raises(SetupFailure) as error:
            workflow.resolve_workflow_request("/formalize paper.tex", active_cwd=root)
        assert error.value.code == "setup_inspector_incompatible"
    budgets = list((root / ".leanflow/formalization-campaigns").glob("*/budget.json"))
    assert len(budgets) == 2
    assert all(json.loads(path.read_text())["invocations"] == {} for path in budgets)
    assert context["commits"] == [] and preparation.load_preparation_receipt(root) is None
    assert all(policy.usage.cleanup_verified for policy in context["policies"])
    assert (root / "paper.tex").read_bytes() == before
    assert not (root / "Main.lean").exists() and not (root / ".lake").exists()
