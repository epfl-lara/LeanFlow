"""Report published mathematical authority freshness without looking up proof certificates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal, TypedDict

from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import contained, digest, identity
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.formalization.lean_policy import POLICY_VERSION
from leanflow_cli.formalization.publication_journal import load_journal, load_registry
from leanflow_cli.formalization.publication_membership import require_committed_publication
from leanflow_cli.formalization.records import document_directory
from leanflow_cli.formalization.source_reconciliation import assert_current_extraction


class AcceptanceFreshness(TypedDict):
    """Retain historical review while exposing the exact reason it grants no current authority."""

    current: bool
    status: Literal[
        "historical",
        "not_published",
        "current",
        "environment_stale",
        "checker_stale",
        "acceptance_stale",
    ]
    reason: str


def assert_revision_not_revoked(root: Path, revision_id: str) -> None:
    """Reject resolved publications even if their formerly accepted bytes were restored."""
    registry = load_registry(root)
    if any(
        item.get("revision_id") == revision_id for item in registry.get("revoked_revisions", [])
    ):
        raise ValueError(
            "Publication conflict resolved; a new mathematical acceptance revision is required; prepare and run formalization again"
        )
    loss = registry.get("registry_loss_acknowledgment")
    if loss:
        historical = loss["discovered_transactions"]
        fresh_commit = False
        for key, entry in registry["transactions"].items():
            if key in historical or entry.get("loss"):
                continue
            journal = load_journal(contained(root, entry["journal"]))
            if (
                journal["revision_id"] == revision_id
                and journal["state"] == "COMMITTED"
                and not journal.get("conflicts")
            ):
                fresh_commit = True
                break
        if not fresh_commit:
            # Unavailable history cannot enumerate every formerly accepted ID.
            # Only a newly checked publication after acknowledgment can proceed.
            raise ValueError(
                "Publication registry unavailable; a new mathematical acceptance revision is required; prepare and run formalization again"
            )
    # The durable journal precedes registry settlement. It remains authoritative
    # after a process exits before the redundant registry revocation is written.
    for entry in registry["transactions"].values():
        if entry.get("loss"):
            if entry["loss"]["state"] != "RESOLVED":
                raise ValueError(
                    "Publication evidence unavailable; inspect publication recovery before proceeding"
                )
            if revision_id in entry["loss"]["revision_ids"]:
                raise ValueError(
                    "Publication evidence unavailable; a new mathematical acceptance revision is required; prepare and run formalization again"
                )
            continue
        journal = load_journal(contained(root, entry["journal"]))
        if journal["revision_id"] == revision_id and journal.get("resolutions"):
            raise ValueError(
                "Publication conflict resolved; a new mathematical acceptance revision is required; prepare and run formalization again"
            )


def published_acceptance_freshness(
    root: Path, record: dict[str, Any], receipt: dict[str, Any] | None, *, verify_bytes: bool = True
) -> AcceptanceFreshness:
    """Read current checker and dependency identities for every published draft, including no-hole drafts.

    This operation writes no evidence and grants no proof scope. Source selection,
    canonical hashes, and exact obligation validation remain separate gates.
    """
    if not receipt or receipt.get("status") != "published" or not receipt.get("handoff"):
        return {
            "current": False,
            "status": "not_published",
            "reason": "No published acceptance receipt",
        }
    try:
        assert_current_extraction(complete_inventory(record))
        assert_revision_not_revoked(root, record["revision_id"])
        require_committed_publication(root, record["document_id"], record["revision_id"])
        handoff = receipt["handoff"]
        directory = document_directory(root, record["document_id"], published=True)
        stored = json.loads(contained(directory, "handoff.json").read_text(encoding="utf-8"))
        if (
            stored != handoff
            or handoff.get("revision_id") != record["revision_id"]
            or handoff.get("document_id") != record["document_id"]
            or handoff.get("handoff_id")
            != "handoff-"
            + identity({key: value for key, value in handoff.items() if key != "handoff_id"})
            or handoff.get("environment_id") != record["environment_id"]
        ):
            raise ValueError("Published acceptance differs from its immutable source review")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {"current": False, "status": "acceptance_stale", "reason": str(error)}
    policy = {
        "policy_id": POLICY_VERSION,
        "checker_sha256": digest(INSPECTOR),
        "allowed_axioms": sorted(set(record["allowed_axioms"]) - {"sorryAx"}),
    }
    checks = record.get("checks", {})
    if handoff.get("acceptance_policy") != policy or (
        checks.get("declarations")
        and any(checks.get(name) != value for name, value in policy.items())
    ):
        return {
            "current": False,
            "status": "checker_stale",
            "reason": "Published acceptance policy or Lean checker changed; prepare and run formalization again with the current checker",
        }
    try:
        if verify_bytes:
            current_environment = environment_identity(root)
        else:
            from leanflow_cli.formalization.environment_display import display_environment_identity

            current_environment = display_environment_identity(root, checker=environment_identity)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return {
            "current": False,
            "status": "environment_stale",
            "reason": f"Published compiler/dependency environment could not be validated: {error}",
        }
    if current_environment != handoff["environment_id"]:
        return {
            "current": False,
            "status": "environment_stale",
            "reason": "Published compiler/dependency or controller environment changed; prepare and run formalization again with the current checker",
        }
    return {"current": True, "status": "current", "reason": ""}


def require_current_published_acceptance(
    root: Path, record: dict[str, Any], receipt: dict[str, Any] | None
) -> None:
    """Require current environment authority without depending on proof receipt storage."""
    result = published_acceptance_freshness(root, record, receipt)
    if not result["current"]:
        raise ValueError(result["reason"])
