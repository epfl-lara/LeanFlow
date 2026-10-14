"""Compile private preparation inputs and commit only independently checked infrastructure changes."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inspector_readiness import (
    check_inspector_runtime,
    valid_inspector_readiness,
)
from leanflow_cli.formalization.intake import preflight_dependencies
from leanflow_cli.formalization.lean_build import build_project
from leanflow_cli.formalization.preparation_delta import infrastructure_baseline, managed_delta
from leanflow_cli.formalization.preparation_lifecycle import PrivateStage
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.formalization.preparation_stage import (
    add_packages,
    build_prepared_dependencies,
    configure_layout,
    copy_stage,
)
from leanflow_cli.formalization.preparation_transaction import (
    commit_preparation,
    dispose_preparation_incoming,
    entry_identity,
    preparation_directory,
    protected_inputs,
)
from leanflow_cli.workflows.project_setup.execution import build_setup_execution_policy
from leanflow_cli.workflows.project_setup.process import (
    Progress,
    SetupCancelled,
    SetupFailure,
    run_command,
)
from leanflow_cli.workflows.project_setup.service import prepare_project
from leanflow_cli.workflows.project_setup.toolchain_install import ensure_official_toolchain
from leanflow_cli.workflows.prover.check_process import check_scratch, close_check_workers
from leanflow_cli.workflows.prover.source import project_sources


def prepare_private_stage(
    root: Path,
    record: dict[str, Any],
    plan: dict[str, Any],
    *,
    lease: PrivateStage,
    progress: Progress | None,
    cancel: threading.Event,
    deadline: float,
    cancelled: Callable[[], bool],
    network_allowed: bool,
    budget_path: Path | None,
    checkpoint: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Check and publish one isolated stage while retaining uncertain process inputs."""
    stage = lease.path
    free = os.statvfs(stage)
    initial_free = free.f_bavail * free.f_frsize
    lease.require_cleanup()
    try:
        compiler_acquisition = ensure_official_toolchain(
            stage,
            canonical_root=root,
            toolchain=plan["toolchain"],
            deadline=deadline,
            network_allowed=network_allowed,
            cancel=cancel,
            progress=progress,
        )
    except BaseException as error:
        lease.verify_acquisition_failure(error)
        raise
    lease.verify_cleanup(compiler_acquisition.get("resources") or {})
    if not lease.cleanup_verified:
        raise SetupFailure(
            "setup_cleanup_unverified",
            "Compiler acquisition cleanup could not be verified.",
            "Retain private inputs and inspect owned processes before retrying preparation.",
        )
    lease.dispose_compiler_inputs()
    marker = root / "lean-toolchain"
    pinned = marker.read_bytes() if marker.is_file() else (plan["toolchain"] + "\n").encode()
    if pinned.decode().strip() != plan["toolchain"]:
        raise ValueError("Project toolchain changed since its preparation plan")
    (stage / "lean-toolchain").write_bytes(pinned)
    policy = build_setup_execution_policy(
        stage,
        canonical_root=root,
        toolchain=plan["toolchain"],
        network_allowed=network_allowed,
        deadline=deadline,
    )
    policy.usage.initial_free_bytes = initial_free
    acquisition_resources = compiler_acquisition.get("resources") or {}
    for field in ("peak_owned_rss_bytes", "output_bytes", "commands", "peak_disk_growth_bytes"):
        setattr(policy.usage, field, acquisition_resources.get(field, 0))
    lease.require_cleanup()
    try:
        with supervise_preparation(policy, cancel=cancel):
            baseline = infrastructure_baseline(root, check=lambda: policy.check(cancel))
            original_paths = set(baseline)
            protected = protected_inputs(root, source_paths=record["source_files"])
            copy_stage(root, stage, check=lambda: policy.check(cancel))
            configure_layout(stage, root, plan, baseline)
            import_roots = {
                name: before for name, before in baseline.items() if name not in original_paths
            }
            add_packages(stage, plan)
            before_packages = {item["name"]: item for item in plan["existing_packages"]}
            added = [
                item["name"] for item in plan["packages"] if item["name"] not in before_packages
            ]
            if added:
                update_log = preparation_directory(root) / (uuid.uuid4().hex + "-acquisition.log")
                with update_log.open("x", encoding="utf-8") as log:
                    code, output = run_command(
                        ["lake", "update", *added],
                        cwd=stage,
                        log=log,
                        cancel=cancel,
                        progress=progress,
                        phase="acquiring_packages",
                        execution_policy=policy,
                    )
                    if code:
                        raise ValueError("Pinned dependency acquisition failed: " + output[-4000:])
                if (stage / "lean-toolchain").read_text().strip() != plan["toolchain"]:
                    raise ValueError("Dependency acquisition tried to change the pinned compiler")
            result = prepare_project(
                stage,
                preset=plan["preset"],
                progress=progress,
                cancel=cancel,
                execution_policy=policy,
            )
            if result["status"] != "ready":
                return {
                    "status": result["status"],
                    "accepted": False,
                    "plan": plan,
                    "setup": result,
                    "resources": policy.evidence(),
                    "error": result["message"],
                }
            build_prepared_dependencies(
                root, stage, plan, before_packages, policy, cancel=cancel, progress=progress
            )
            layout = preflight_dependencies(stage, record["document_id"])
            if layout.get("toolchain") != plan["toolchain"].removeprefix("leanprover/lean4:v"):
                raise ValueError("Installed compiler differs from the exact planned Lean release")
            checking = stage / ".leanflow/preparation-checks"
            checking.mkdir(parents=True, exist_ok=True)
            build = build_project(stage, layout, checking, deadline=deadline, cancelled=cancelled)
            if not build.get("success"):
                raise ValueError(
                    "Prepared project failed independent compilation: "
                    + str(build.get("error", build.get("stderr", "")))
                )
            imports = plan["required_imports"]
            probe = checking / "Imports.lean"
            probe.write_text(
                "".join("import " + name + "\n" for name in imports)
                + "example : True := by trivial\n"
            )
            try:
                checked = check_scratch(
                    project_root=stage,
                    workspace=checking,
                    file=probe,
                    timeout_s=max(0.1, deadline - time.monotonic()),
                    cancelled=cancelled,
                )
            finally:
                close_check_workers(checking)
            if not checked.get("ok"):
                raise ValueError(
                    "Required mathematical imports are unavailable: "
                    + str(checked.get("error", checked))
                )
            policy.check(cancel)
            if progress:
                progress(
                    {
                        "type": "progress",
                        "phase": "checking_lean_compatibility",
                        "message": "Checking Lean compatibility",
                    }
                )
            inspector_readiness = check_inspector_runtime(
                stage,
                checking / "inspector-runtime",
                deadline=deadline,
                cancelled=cancelled,
            )
            if not valid_inspector_readiness(inspector_readiness):
                policy.check(cancel)
                raise SetupFailure(
                    "setup_inspector_incompatible",
                    "The pinned compiler could not run the formalization inspector: "
                    + json.dumps(inspector_readiness, ensure_ascii=False),
                    "Check LeanFlow compatibility with the pinned compiler and prepare again before translation.",
                )
            environment = environment_identity(stage)
    finally:
        lease.verify_cleanup(policy.evidence())
    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "preparation_plan_id": plan["plan_id"],
        "source_revision_id": record["revision_id"],
        "source_bundle": record["source_bundle"],
        "plan": plan,
        "environment_id": environment,
        "layout": layout,
        "imports": imports,
        "project_build": {key: value for key, value in build.items() if key != "local_artifacts"},
        "import_check": checked,
        "inspector_readiness": inspector_readiness,
        "resources": policy.evidence(),
        "precommit_cleanup": lease.precommit_cleanup(),
        "resource_scope": "private preparation through verified descendant cleanup",
        "toolchain_acquisition": compiler_acquisition,
        "project_sources": {
            path.relative_to(stage).as_posix(): entry_identity(path)
            for path in project_sources(stage)
            if path.name != "lakefile.lean"
        },
        "resolved_packages": json.loads((stage / "lake-manifest.json").read_text())["packages"],
        "budget_path": str(budget_path) if budget_path is not None else None,
    }
    receipt["preparation_id"] = "preparation-" + identity(receipt)

    def validate() -> None:
        if environment_identity(root) != environment:
            raise ValueError("Prepared authority changed before infrastructure commitment")

    post_commit_stop: Exception | None = None

    def guarded_checkpoint(name: str) -> None:
        """Record post-commit stops without disguising selected infrastructure as rollback."""
        nonlocal post_commit_stop
        try:
            policy.check(cancel)
            policy.observe_disk()
            if checkpoint:
                checkpoint(name)
        except Exception as error:
            if name not in {"journal:COMMITTED", "committed", "settled"}:
                raise
            if post_commit_stop is None:
                post_commit_stop = error

    selected = managed_delta(root, stage, baseline, check=lambda: policy.check(cancel))
    selected.update(import_roots)
    try:
        transaction = commit_preparation(
            root,
            stage,
            managed=selected,
            protected=protected,
            receipt=receipt,
            validate=validate,
            cancelled=cancelled,
            checkpoint=guarded_checkpoint,
            check=lambda: policy.check(cancel),
        )
    finally:
        if lease.cleanup_verified:
            dispose_preparation_incoming(root, cleanup_verified=True)
    from leanflow_cli.workflows.project_setup.state import (
        fingerprint,
        save_result,
        state_directory,
    )
    from leanflow_cli.workflows.project_setup.verification import repl_binary

    state_directory(root).mkdir(parents=True, exist_ok=True)
    binary = repl_binary(root)
    save_result(
        root,
        {
            "status": "ready",
            "phase": "formalization_prepared",
            "code": "ready",
            "message": "Project dependencies are checked; source translation can begin.",
            "remediation": "",
            "root": str(root),
            "log_path": "",
            "fingerprint": fingerprint(root),
            "capabilities": {
                "verification": True,
                "mathlib": any(
                    package["name"] == "mathlib" for package in receipt["resolved_packages"]
                ),
                "repl_path": str(binary) if binary else "",
                "source_diagnostics": [],
            },
        },
    )
    stopped = post_commit_stop is not None or cancelled()
    explicit_cancel = cancel.is_set() or isinstance(
        post_commit_stop, (SetupCancelled, InterruptedError)
    )
    result = {
        "accepted": not stopped,
        "status": (
            "cancelled" if explicit_cancel else "preparation_stopped" if stopped else "prepared"
        ),
        "committed": True,
        "plan": plan,
        "receipt": receipt,
        "transaction_id": transaction["transaction_id"],
    }
    if stopped:
        reason = str(post_commit_stop or "") or (
            "Cancellation requested" if explicit_cancel else "Preparation deadline reached"
        )
        result["error"] = "Project dependencies committed; preparation stopped afterward: " + reason
    return result
