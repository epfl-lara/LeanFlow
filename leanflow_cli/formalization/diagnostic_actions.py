"""Record explicit user interpretations without waiving source coverage or translation review."""

from __future__ import annotations

from typing import Any


def resolve_command(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Bind verified command semantics to every frozen occurrence and reopen its coverage audit."""
    _resolve_interpretation(record, action, code="unresolved_command", subject="command")


def resolve_conditional(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Record a user interpretation without selecting branches or narrowing candidate coverage."""
    _resolve_interpretation(record, action, code="unresolved_conditional", subject="conditional")


def _resolve_interpretation(
    record: dict[str, Any], action: dict[str, Any], *, code: str, subject: str
) -> None:
    """Bind a checked interpretation to immutable occurrences and retain all review obligations."""
    matches = [
        row
        for row in record["inventory"]["diagnostics"]
        if row["id"] == action.get("diagnostic_id")
    ]
    if len(matches) != 1 or matches[0]["code"] != code:
        raise ValueError(
            f"Only an uncertain TeX {subject} can receive a manual semantic interpretation"
        )
    diagnostic = matches[0]
    if action.get("verified") is not True:
        raise ValueError(f"Verify the {subject} meaning against every frozen source occurrence")
    interpretation = action.get("interpretation")
    reason = action.get("reason")
    if (
        not isinstance(interpretation, str)
        or not interpretation.strip()
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ValueError(
            f"{subject.capitalize()} reconciliation needs its complete meaning and a reason"
        )
    spans = diagnostic.get("spans") or ([diagnostic["span"]] if "span" in diagnostic else [])
    if not spans:
        raise ValueError(
            f"{subject.capitalize()} reconciliation requires exact frozen source occurrences"
        )
    resolution = {
        "actor": "user",
        "verified": True,
        "interpretation": interpretation.strip(),
        "reason": reason.strip(),
        "source_spans": spans,
    }
    note = {"diagnostic_id": diagnostic["id"], **resolution}
    affected_items: set[str] = set()
    for span in spans:
        regions = [
            region
            for region in record["inventory"]["regions"]
            if region["file"] == span["file"]
            and region.get("occurrence_id") == span.get("occurrence_id")
            and region["byte_start"] < span["byte_end"]
            and span["byte_start"] < region["byte_end"]
        ]
        if not regions:
            raise ValueError(f"An uncertain {subject} occurrence has no frozen coverage region")
        for region in regions:
            notes = [
                old
                for old in region.get("source_interpretations", [])
                if old["diagnostic_id"] != diagnostic["id"]
            ]
            region["source_interpretations"] = [*notes, note]
            affected_items.update(region.get("item_ids", []))
            if not region.get("item_ids") and not (
                region.get("disposition") == "excluded" and region.get("actor") == "source"
            ):
                region.update(
                    disposition="unresolved",
                    reason=f"User {subject} interpretation requires independent source coverage review",
                )
    for item in record["inventory"]["items"]:
        if item["id"] in affected_items:
            notes = [
                old
                for old in item.get("source_interpretations", [])
                if old["diagnostic_id"] != diagnostic["id"]
            ]
            item["source_interpretations"] = [*notes, note]
            item.pop("proof_authorization", None)
    diagnostic.update(blocking=False, resolution=resolution)
    record["inventory"]["extraction_status"] = (
        "blocked"
        if any(row.get("blocking") for row in record["inventory"]["diagnostics"])
        else "audit_required"
    )
