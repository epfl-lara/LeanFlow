"""Compile private preparation inputs and commit only independently checked infrastructure changes."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inspector_readiness import (
    check_inspector_runtime,
    valid_inspector_readiness,
)
from leanflow_cli.formalization.intake import preflight_dependencies
from leanflow_cli.formalization.lean_build import build_project
from leanflow_cli.formalization.preparation_authorization import (
    PreparationAuthorization,
    assert_original_configuration,
)
from leanflow_cli.formalization.preparation_delta import infrastructure_baseline, managed_delta
from leanflow_cli.formalization.preparation_hash_cache import verify_generated_hash_caches
from leanflow_cli.formalization.preparation_lifecycle import PrivateStage
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.formalization.preparation_stage import (
    acquire_authorized_inputs,
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
    safe_text,
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

    def step_log(phase: str, message: str) -> TextIO:
        """Create diagnostic-only evidence before announcing a controller check."""
        path = preparation_directory(root) / (uuid.uuid4().hex + "-" + phase + ".log")
        log = path.open("x", encoding="utf-8")
        try:
            path.chmod(0o600)
            log.write(message + "\n")
            log.flush()
            if progress:
                progress(
                    {
                        "type": "progress",
                        "phase": phase,
                        "message": message,
                        "log_path": str(path),
                    }
                )
        except BaseException:
            log.close()
            raise
        return log

    def step_result(log: TextIO, result: dict[str, Any]) -> None:
        """Append a bounded redacted outcome without creating readiness authority."""
        log.write(
            "Diagnostic outcome only; not an acceptance receipt:\n"
            + safe_text(json.dumps(result, ensure_ascii=False, default=str))
            + "\n"
        )

    free = os.statvfs(stage)
    initial_free = free.f_bavail * free.f_frsize
    lease.require_cleanup()
    with step_log("checking_toolchain", "Checking the exact pinned Lean compiler") as compiler_log:
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
            step_result(compiler_log, {"error": str(error)})
            lease.verify_acquisition_failure(error)
            raise
        step_result(compiler_log, compiler_acquisition)
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
            checking = stage / ".leanflow/preparation-checks"
            checking.mkdir(parents=True, exist_ok=True)
            policy.check(cancel)
            with step_log(
                "checking_lean_compatibility", "Checking Lean compatibility"
            ) as inspector_log:
                inspector_readiness = check_inspector_runtime(
                    stage,
                    checking / "inspector-runtime",
                    deadline=deadline,
                    cancelled=cancelled,
                )
                step_result(inspector_log, inspector_readiness)
            if not valid_inspector_readiness(inspector_readiness):
                policy.check(cancel)
                raise SetupFailure(
                    "setup_inspector_incompatible",
                    "The pinned compiler could not run the formalization inspector: "
                    + json.dumps(inspector_readiness, ensure_ascii=False),
                    "Check LeanFlow compatibility with the pinned compiler and prepare again before translation.",
                )
            assert_original_configuration(root, plan)
            baseline = infrastructure_baseline(root, check=lambda: policy.check(cancel))
            original_paths = set(baseline)
            protected = protected_inputs(root, source_paths=record["source_files"])
            copy_stage(root, stage, check=lambda: policy.check(cancel))
            assert_original_configuration(stage, plan, private=True)
            configure_layout(stage, root, plan, baseline)
            import_roots = {
                name: before for name, before in baseline.items() if name not in original_paths
            }
            add_packages(stage, plan)
            before_packages = {item["name"]: item for item in plan["existing_packages"]}

            authorization: PreparationAuthorization
            authorization, git_read = acquire_authorized_inputs(
                root, stage, plan, policy, cancel=cancel, progress=progress
            )
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
            with step_log(
                "checking_project", "Independently checking prepared local modules"
            ) as build_log:
                build = build_project(
                    stage, layout, checking, deadline=deadline, cancelled=cancelled
                )
                step_result(build_log, build)
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
            with step_log(
                "checking_imports", "Checking all required mathematical imports"
            ) as import_log:
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
                step_result(import_log, checked)
            if not checked.get("ok"):
                raise ValueError(
                    "Required mathematical imports are unavailable: "
                    + str(checked.get("error", checked))
                )

    finally:
        lease.verify_cleanup(policy.evidence())
    with step_log(
        "checking_dependency_hash_cache", "Verifying generated dependency hash caches"
    ) as hash_log:
        authorization = verify_generated_hash_caches(
            stage,
            authorization,
            policy,
            lease,
            git=git_read,
            log=hash_log,
            cancel=cancel,
            progress=progress,
        )
        step_result(
            hash_log, {"retained_verified_hash_caches": len(authorization.verified_hash_caches)}
        )
    # A matching final fingerprint cannot authorize changes made by an effectful
    # build. Repeat the original authority comparison after its descendants stop.
    lease.require_cleanup()
    try:
        with supervise_preparation(policy, cancel=cancel):
            with step_log(
                "checking_dependency_sources", "Checking pinned dependency sources after builds"
            ) as source_log:
                authorization.validate(stage, git=git_read, check=lambda: policy.check(cancel))
                step_result(source_log, {"source_authority_unchanged": True})
            with step_log(
                "fingerprinting_environment", "Fingerprinting compiler and dependency inputs"
            ) as environment_log:
                environment = environment_identity(stage)
                step_result(environment_log, {"environment_id": environment})
    finally:
        lease.verify_cleanup(policy.evidence())
    with step_log(
        "rechecking_dependency_sources", "Rechecking dependency sources after process cleanup"
    ) as source_log:
        authorization.validate(stage, check=lambda: policy.check(cancel))
        step_result(source_log, {"source_authority_unchanged": True})
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
        "verified_hash_caches": [cache.to_dict() for cache in authorization.verified_hash_caches],
        "budget_path": str(budget_path) if budget_path is not None else None,
    }
    receipt["preparation_id"] = "preparation-" + identity(receipt)

    def validate() -> None:
        if environment_identity(root) != environment:
            raise ValueError("Prepared authority changed before infrastructure commitment")

    def authorize(selected_root: Path) -> None:
        authorization.validate(selected_root, check=lambda: policy.check(cancel))

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

    with step_log(
        "selecting_preparation_changes", "Selecting changed project infrastructure"
    ) as delta_log:
        selected = managed_delta(
            root, stage, baseline, check=lambda: policy.check(cancel), authorize=authorize
        )
        selected.update(import_roots)
        step_result(delta_log, {"changed_infrastructure_entries": len(selected)})
    try:
        with step_log(
            "publishing_preparation",
            "Publishing checked infrastructure and rechecking installed inputs",
        ):
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
                authorize=authorize,
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
