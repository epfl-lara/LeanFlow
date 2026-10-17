"""Authenticate proof-preservation evidence for display without granting new proof scope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, identity
from leanflow_cli.formalization.freshness import AcceptanceFreshness, published_acceptance_freshness
from leanflow_cli.formalization.inventory_authority import assert_complete_coverage
from leanflow_cli.formalization.records import document_directory
from leanflow_cli.native.harness_artifacts import source_files


def current_proof_receipt(
    root: Path,
    record: dict[str, Any],
    receipt: dict[str, Any] | None,
    *,
    freshness: AcceptanceFreshness | None = None,
) -> dict[str, Any] | None:
    """Return only complete controller evidence matching the selected review and live source."""
    if not receipt or receipt.get("status") != "published" or not receipt.get("handoff"):
        return None
    assert_complete_coverage(record)
    freshness = freshness or published_acceptance_freshness(root, record, receipt)
    if not freshness["current"]:
        raise ValueError(freshness["reason"])
    directory = document_directory(root, record["document_id"], published=True)
    parent = contained(directory, "proof-revisions")
    if not parent.is_dir():
        return None
    from leanflow_cli.formalization.handoff import validate_handoff

    handoff = validate_handoff(
        root,
        directory / "handoff.json",
        record["revision_id"],
        check_sources=False,
        checked_environment_id=record["environment_id"],
    )
    if handoff != receipt["handoff"]:
        raise ValueError("Proof preservation belongs to a different source review")
    live = {
        file: digest(data) for file, data in source_files(root).items() if file.endswith(".lean")
    }
    matched = []
    for path in sorted(parent.glob("proof-rev-*.json")):
        value = json.loads(contained(parent, path.name).read_text())
        key = "proof-rev-" + identity(
            {name: item for name, item in value.items() if name != "proof_revision_id"}
        )
        if path.stem != key or value.get("proof_revision_id") != key:
            raise ValueError("Proof preservation certificate identity changed")
        if value.get("reviewed_revision_id") != record["revision_id"]:
            continue
        if value.get("after") != live:
            continue
        policy = handoff["acceptance_policy"]
        checks = value.get("checks", {})
        if (
            value.get("schema_version") != 1
            or value.get("kind") != "proof_preservation"
            or value.get("handoff_id") != handoff["handoff_id"]
            or value.get("before") != handoff["files"]
            or set(value["after"]) != set(handoff["files"])
            or value.get("environment_id") != handoff["environment_id"]
            or value.get("acceptance_policy") != policy
            or checks.get("accepted") is not True
            or any(checks.get(name) != content for name, content in policy.items())
        ):
            raise ValueError("Proof preservation differs from reviewed acceptance")
        profiles = checks.get("profiles", {})
        for name, before in handoff["protected_declarations"].items():
            if before["declaration_origin"] != "generated":
                continue
            current = profiles.get(name, {})
            if current.get("type_hash") != before["type_hash"] or (
                before["kind"] != "theorem" and current.get("body_hash") != before["body_hash"]
            ):
                raise ValueError("Proof preservation changed a reviewed declaration")
        for obligation in handoff["obligations"]:
            current = profiles.get(obligation["declaration"], {})
            if (
                not current
                or current.get("pending")
                or "sorryAx" in current.get("axioms", ["sorryAx"])
            ):
                raise ValueError("Proof preservation has an incomplete reviewed obligation")
        matched.append(value)
    return matched[-1] if matched else None
