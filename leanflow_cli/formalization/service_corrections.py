"""Project literal/adopted readings and saved correction evidence without granting authority."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from leanflow_cli.formalization.corrections import (
    _applicable,
    _authorities,
    correction_context_identity,
)
from leanflow_cli.formalization.evidence import digest

_POLICY = (
    "Allow only source-supported local corrections whose recorded original and adopted readings "
    "are justified by closed independently replayed Lean evidence and fresh independent source "
    "review. Preserve the frozen source and every source clause. A nonunique reading needs a "
    "separate recorded user interpretation before acceptance; it can remain unresolved in a delivered draft."
)


def source_bundle_binding(record: dict[str, Any]) -> str | None:
    """Expose decision scope only when an exact frozen source identity exists."""
    value = record.get("source_bundle")
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def authority_options(
    record: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Derive next-version user actions from the exact source bundle and saved decisions."""
    if source_bundle_binding(record) is None:
        return {item["id"]: [] for item in inventory["items"]}
    latest = {row["authority_id"]: row for row in _authorities(record).values()}
    included = [row["id"] for row in inventory["items"] if row.get("scope") == "included"]
    result: dict[str, list[dict[str, Any]]] = {}
    for item in inventory["items"]:
        choices = []
        for kind, handle, scope, statement in (
            ("bounded_policy", "paper-correction-policy", included, _POLICY),
            ("interpretation", "interpretation-" + digest(item["id"])[:24], [item["id"]], ""),
            ("convention", "convention-" + digest(item["id"])[:24], [item["id"]], ""),
        ):
            if not 1 <= len(scope) <= 64:
                continue
            current = latest.get(handle)
            active = bool(
                current
                and current["enabled"]
                and current["kind"] == kind
                and current["source_bundle"] == record["source_bundle"]
                and set(current["source_item_ids"]) == set(scope)
            )
            choices.append(
                {
                    "enabled": active,
                    "current": current,
                    "action": {
                        "type": "record_correction_authority",
                        "authority_id": handle,
                        "version": (current or {}).get("version", 0) + 1,
                        "kind": kind,
                        "source_bundle": record["source_bundle"],
                        "source_item_ids": list(scope),
                        "clause_ids": [],
                        "statement": (
                            statement
                            if kind == "bounded_policy"
                            else (current or {}).get("statement", "")
                        ),
                        "enabled": True,
                    },
                }
            )
        result[item["id"]] = choices
    return result


def _lean_reference(
    name: str,
    references: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    artifacts: dict[str, str],
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach saved compiled identities while using only controller-allocated artifact links."""
    reference = next((dict(row) for row in references if row["name"] == name), {"name": name})
    profile = profiles.get(name, {})
    file = reference.get("file") or profile.get("file")
    if file:
        reference["file"] = file
        if file in artifacts:
            reference["artifact_id"] = artifacts[file]
    if type(profile.get("line_start")) is int:
        reference["line_start"] = profile["line_start"]
    pretty = profile.get("pretty_type") or profile.get("type")
    if isinstance(pretty, str):
        reference["type"] = pretty
    for field in ("type_hash", "body_hash", "definition_context_hash"):
        value = (binding or {}).get(field)
        if isinstance(value, str):
            reference[field] = value
    return reference


def item_corrections(
    record: dict[str, Any],
    item_id: str,
    *,
    references: list[dict[str, Any]],
    checks: dict[str, Any],
    receipt: dict[str, Any] | None,
    artifacts: dict[str, str],
    source_ref: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Show source-backed proposals, exact saved bindings and reviews separately from acceptance."""
    corrections = record.get("corrections", [])
    if not isinstance(corrections, list) or any(not isinstance(row, dict) for row in corrections):
        raise ValueError("Invalid saved correction projection")
    corrections = [row for row in corrections if row.get("source_item_id") == item_id]
    if not corrections:
        return []
    if source_bundle_binding(record) is None:
        raise ValueError("Invalid saved correction source bundle")
    authorities = {row["id"]: row for row in record.get("correction_authorities", [])}
    latest = _authorities(record)
    profiles = {row["name"]: row for row in checks.get("declarations", [])}
    context = correction_context_identity(record)
    units = {unit["id"]: unit for unit in record.get("units", [])}
    result = []
    for row in corrections:
        bindings = [
            value
            for value in checks.get("correction_evidence", [])
            if value.get("correction_id") == row["id"]
        ]
        stored: dict[str, Any] = next(
            (value for value in bindings if value.get("content_id") == row["content_id"]),
            {},
        )
        bound = stored if stored.get("context_id") == context else {}
        declaration_bindings = {value["name"]: value for value in stored.get("declarations", [])}
        facts = {value["goal_name"]: value for value in stored.get("evidence", [])}
        reviews = [
            {
                **verdict,
                "binding_matches": bool(
                    review.get("revision_id") == record["revision_id"]
                    and verdict.get("content_id") == row["content_id"]
                    and bound
                    and verdict.get("evidence_id") == bound.get("evidence_id")
                    and verdict.get("authority_refs") == row["authority_refs"]
                ),
            }
            for review in (receipt or {}).get("reviews", [])
            for verdict in review.get("correction_verdicts", [])
            if verdict.get("correction_id") == row["id"]
        ]
        affected = {key for key, unit in units.items() if item_id in unit["required_source_ids"]}
        while True:
            expanded = affected | {
                key
                for key, unit in units.items()
                if set(unit.get("depends_on_units", [])) & affected
            }
            if expanded == affected:
                break
            affected = expanded
        try:
            _applicable(record, row)
            current_authority = True
        except ValueError:
            current_authority = False
        result.append(
            {
                "id": row["id"],
                "content_id": row["content_id"],
                "kind": row["kind"],
                "source_item_id": item_id,
                "clause_ids": row["clause_ids"],
                "original": {
                    "literal_reading": row["original"]["literal_reading"],
                    "source_refs": [source_ref(ref) for ref in row["original"]["source_refs"]],
                },
                "adopted": {
                    "reading": row["adopted"]["reading"],
                    "lean_refs": [
                        _lean_reference(
                            name, references, profiles, artifacts, declaration_bindings.get(name)
                        )
                        for name in row["adopted"]["declaration_names"]
                    ],
                },
                "rationale": row["rationale"],
                "source_context_refs": [source_ref(ref) for ref in row["source_context_refs"]],
                "authorities": [
                    {**authorities[key], "current": key in latest and current_authority}
                    for key in row["authority_refs"]
                    if key in authorities
                ],
                "authority_current": current_authority,
                "evidence_status": "saved_checked" if bound else "stale" if bindings else "pending",
                "evidence_id": stored.get("evidence_id"),
                "evidence": [
                    {
                        **fact,
                        **{
                            label: _lean_reference(
                                fact[label + "_name"],
                                references,
                                profiles,
                                artifacts,
                                facts.get(fact["goal_name"], {}).get(label),
                            )
                            for label in ("goal", "proof")
                        },
                    }
                    for fact in row["evidence"]
                ],
                "reviews": reviews,
                "dependents": [
                    {
                        "unit_id": key,
                        "source_item_ids": units[key]["required_source_ids"],
                        "declaration_names": [ref["name"] for ref in units[key]["declarations"]],
                    }
                    for key in sorted(affected)
                ],
                "acceptance_status": "saved" if reviews else "pending",
            }
        )
    return result


def mark_current_corrections(items: list[dict[str, Any]], *, accepted_handoff: bool) -> None:
    """Label current corrections only after the existing whole-revision acceptance gate succeeds."""
    if not accepted_handoff:
        return
    for item in items:
        for row in item.get("corrections", []):
            if (
                row["authority_current"]
                and row["evidence_status"] == "saved_checked"
                and row["reviews"]
                and all(
                    review["binding_matches"] and review["decision"] == "PASS"
                    for review in row["reviews"]
                )
            ):
                row["acceptance_status"] = "current"
