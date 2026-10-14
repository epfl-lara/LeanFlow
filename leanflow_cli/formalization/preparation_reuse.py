"""Reuse exact checked infrastructure while rebinding a new source plan and campaign budget."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inspector_readiness import valid_inspector_readiness
from leanflow_cli.formalization.preparation_transaction import commit_preparation, protected_inputs
from leanflow_cli.formalization.stage_errors import RepreparationRequired


def reuse_preparation(
    root: Path,
    record: dict[str, Any],
    plan: dict[str, Any],
    previous: dict[str, Any],
    *,
    budget_path: Path | None,
    cancelled: Callable[[], bool],
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Select a new receipt only when the exact project and all requested imports were checked."""
    if not valid_inspector_readiness(previous.get("inspector_readiness")):
        raise RepreparationRequired(
            "Prepared Lean compatibility was not checked by the current inspector; prepare again before reuse"
        )
    if not set(plan["required_imports"]).issubset(previous["imports"]):
        return None
    if any(
        plan["layout"][key] != previous["layout"][key]
        for key in (
            "library",
            "src_dir",
            "root_module",
            "root_file",
            "module_prefix",
            "owned_prefix",
            "aggregator",
        )
    ):
        return None
    resolved = {package["name"]: package for package in previous["resolved_packages"]}
    if any(
        resolved.get(package["name"], {}).get("rev") != package["revision"]
        or resolved.get(package["name"], {}).get("url") != package["url"]
        for package in plan["packages"]
    ):
        return None
    if cancelled():
        raise InterruptedError("Preparation cancelled before reusing infrastructure")
    receipt = copy.deepcopy(previous)
    receipt.pop("preparation_id")
    # The original journal retains its closure; this metadata-only transaction owns no children.
    receipt.pop("precommit_cleanup", None)
    receipt.update(
        source_revision_id=record["revision_id"],
        source_bundle=record["source_bundle"],
        preparation_plan_id=plan["plan_id"],
        plan=plan,
        budget_path=str(budget_path) if budget_path else None,
        reused_preparation_id=previous["preparation_id"],
    )
    receipt["preparation_id"] = "preparation-" + identity(receipt)

    def validate() -> None:
        if environment_identity(root) != previous["environment_id"]:
            raise ValueError("Checked infrastructure changed before reusing preparation")

    transaction = commit_preparation(
        root,
        root,
        managed={},
        protected=protected_inputs(root),
        receipt=receipt,
        validate=validate,
        cancelled=cancelled,
        checkpoint=checkpoint,
    )
    stopped = cancelled()
    result = {
        "accepted": not stopped,
        "status": "cancelled" if stopped else "prepared",
        "committed": True,
        "reused": True,
        "plan": plan,
        "receipt": receipt,
        "transaction_id": transaction["transaction_id"],
    }
    if stopped:
        result["error"] = (
            "Project dependencies committed; preparation reuse stopped afterward: cancellation requested or deadline reached"
        )
    return result
