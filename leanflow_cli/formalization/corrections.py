"""Bind proposed source corrections to scoped user authority, closed facts and fresh review."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inventory_authority import complete_inventory

_KINDS = {
    "encoding_repair",
    "equivalent_reformulation",
    "source_supported_mathematical",
    "user_interpretation",
}
_AUTHORITY_KINDS = {"bounded_policy", "interpretation", "convention"}
_AUTHORITY_FIELDS = {
    "authority_id",
    "version",
    "kind",
    "source_bundle",
    "source_item_ids",
    "clause_ids",
    "statement",
    "enabled",
}
_CORRECTION_FIELDS = {
    "id",
    "kind",
    "source_item_id",
    "clause_ids",
    "original",
    "adopted",
    "rationale",
    "source_context_refs",
    "authority_refs",
    "evidence",
}


def _text(value: Any, label: str, *, explanation: bool = False) -> str:
    """Require bounded text, reserving substantive explanations for semantic judgments."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4096
        or (explanation and (len(value.strip()) < 40 or len(value.split()) < 6))
    ):
        raise ValueError("Missing or insubstantial correction " + label)
    return value.strip()


def _names(value: Any, label: str, *, empty: bool = False) -> list[str]:
    """Require bounded distinct explicit identities instead of wildcard authority."""
    if (
        not isinstance(value, list)
        or not (0 if empty else 1) <= len(value) <= 64
        or any(
            not isinstance(name, str) or re.fullmatch(r"[\w.'-]{1,256}", name) is None
            for name in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("Invalid correction " + label)
    return value


def _source_scope(record: dict[str, Any], items: list[str], clauses: list[str]) -> None:
    """Require current original items and clauses without changing their denominator."""
    from leanflow_cli.formalization.clause_obligations import obligation_registry

    known = obligation_registry(record)
    if set(items) - {item["id"] for item in complete_inventory(record)["items"]} or any(
        clause not in known or known[clause]["source_id"] not in items for clause in clauses
    ):
        raise ValueError("Correction scope names unknown original items or clauses")


def _contains(span: dict[str, Any], ref: dict[str, Any]) -> bool:
    """Keep exact byte selections and PDF text selections inside their original item scope."""
    if any(
        ref.get(key) != span.get(key) for key in ("file", "occurrence_id", "page_index")
    ) or ref.get("coordinate_space") != span.get("coordinate_space", "source_bytes"):
        return False
    coordinates = [("byte_start", "byte_end")]
    if ref.get("coordinate_space") == "pdf_page":
        coordinates.append(("text_start", "text_end"))
    return all(
        span.get(first, -1) <= ref[first] < ref[last] <= span.get(last, -1)
        for first, last in coordinates
    )


def _authorities(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate immutable user decisions and expose only the latest version of each handle."""
    rows = record.get("correction_authorities", [])
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("Invalid correction authority registry")
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _AUTHORITY_FIELDS | {"id", "actor"}:
            raise ValueError("Invalid correction authority record")
        payload = {key: value for key, value in row.items() if key != "id"}
        handle = _names([row["authority_id"]], "authority handle")[0]
        if (
            row["actor"] != "user"
            or not isinstance(row["kind"], str)
            or row["kind"] not in _AUTHORITY_KINDS
            or type(row["enabled"]) is not bool
            or type(row["version"]) is not int
            or row["version"] != latest.get(handle, {}).get("version", 0) + 1
            or not isinstance(row["source_bundle"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["source_bundle"]) is None
            or row["id"] != "authority-" + identity(payload)
        ):
            raise ValueError("Correction authority is not a versioned trusted user decision")
        _names(row["source_item_ids"], "authority item scope")
        _names(row["clause_ids"], "authority clause scope", empty=True)
        _text(row["statement"], "authority statement", explanation=True)
        latest[handle] = row
    return {row["id"]: row for row in latest.values()}


def record_correction_authority(record: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Append one explicitly scoped trusted user action; never call from author proposals."""
    if (
        not isinstance(action, dict)
        or set(action) != _AUTHORITY_FIELDS | {"type"}
        or action["type"] != "record_correction_authority"
    ):
        raise ValueError("Invalid trusted correction-authority action")
    if action["source_bundle"] != record["source_bundle"]:
        raise ValueError("Correction authority names a different frozen source bundle")
    _authorities(record)
    _source_scope(
        record,
        _names(action["source_item_ids"], "authority item scope"),
        _names(action["clause_ids"], "authority clause scope", empty=True),
    )
    payload = {key: copy.deepcopy(value) for key, value in action.items() if key != "type"}
    row = {**payload, "actor": "user"}
    row["id"] = "authority-" + identity(row)
    candidate = {
        **record,
        "correction_authorities": [*record.get("correction_authorities", []), row],
    }
    _authorities(candidate)
    record["correction_authorities"] = candidate["correction_authorities"]
    return copy.deepcopy(row)


def _applicable(record: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    """Require live, exact-scope authority while distinguishing permission from mathematical proof."""
    current = _authorities(record)
    selected = []
    source_preserving = row["kind"] in {"encoding_repair", "equivalent_reformulation"}
    for key in _names(row["authority_refs"], "authority references", empty=source_preserving):
        authority = current.get(key)
        if (
            authority is None
            or authority["enabled"] is not True
            or authority["source_bundle"] != record["source_bundle"]
            or row["source_item_id"] not in authority["source_item_ids"]
            or (authority["clause_ids"] and set(row["clause_ids"]) - set(authority["clause_ids"]))
        ):
            raise ValueError("Correction lacks current applicable user authority")
        selected.append(authority)
    if source_preserving:
        return selected
    needed = (
        {"interpretation", "convention"}
        if row["kind"] == "user_interpretation"
        else {"bounded_policy"}
    )
    if not any(authority["kind"] in needed for authority in selected):
        raise ValueError("Correction kind is not enabled by its scoped authority")
    return selected


def apply_corrections(record: dict[str, Any], proposed: Any) -> list[dict[str, Any]]:
    """Validate bounded author proposals without importing approval or modifying source coverage."""
    from leanflow_cli.formalization.clause_obligations import _refs, obligation_registry

    if (
        not isinstance(proposed, list)
        or len(proposed) > 64
        or len(json.dumps(proposed, ensure_ascii=False).encode()) > 512 * 1024
    ):
        raise ValueError("Correction proposals exceed their bounded contract")
    known = obligation_registry(record)
    items = {item["id"]: item for item in complete_inventory(record)["items"]}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    corrected_clauses: set[str] = set()
    for value in proposed:
        if not isinstance(value, dict) or set(value) != _CORRECTION_FIELDS:
            raise ValueError("Invalid authored correction record or supplied approval")
        row = copy.deepcopy(value)
        key = _names([row["id"]], "identity")[0]
        if key in seen or not isinstance(row["kind"], str) or row["kind"] not in _KINDS:
            raise ValueError("Duplicate or unsupported correction")
        seen.add(key)
        clauses = _names(row["clause_ids"], "original clauses")
        if corrected_clauses.intersection(clauses):
            raise ValueError(
                "Overlapping corrections must be merged into one exact clause proposal"
            )
        corrected_clauses.update(clauses)
        _names([row["source_item_id"]], "original item")
        _source_scope(record, [row["source_item_id"]], clauses)
        for field, fields in (
            ("original", {"source_refs", "literal_reading"}),
            ("adopted", {"reading", "declaration_names"}),
        ):
            if not isinstance(row[field], dict) or set(row[field]) != fields:
                raise ValueError("Correction requires exact literal and adopted readings")
        _refs(record, row["original"]["source_refs"])
        _refs(record, row["source_context_refs"])
        item = items[row["source_item_id"]]
        spans = [
            *item.get("statement_spans", []),
            *item.get("proof_spans", []),
            *[ref for clause in clauses for ref in known[clause]["source_refs"]],
        ]
        if any(
            not any(_contains(span, ref) for span in spans)
            for ref in row["original"]["source_refs"]
        ):
            raise ValueError("Correction original references escape their source item and clauses")
        _text(row["original"]["literal_reading"], "literal reading")
        _text(row["adopted"]["reading"], "adopted reading")
        _text(row["rationale"], "rationale", explanation=True)
        _names(row["adopted"]["declaration_names"], "adopted declarations")
        evidence = row["evidence"]
        if (
            not isinstance(evidence, list)
            or len(evidence) > 16
            or (row["kind"] != "encoding_repair" and not evidence)
        ):
            raise ValueError("Mathematical corrections require explicit closed local evidence")
        evidence_names: set[str] = set()
        for fact in evidence:
            if not isinstance(fact, dict) or set(fact) != {"goal_name", "proof_name", "purpose"}:
                raise ValueError("Correction evidence must name its exact expected goal and proof")
            names = _names([fact["goal_name"], fact["proof_name"]], "evidence declarations")
            if evidence_names.intersection(names):
                raise ValueError("Correction evidence repeats a goal or proof")
            evidence_names.update(names)
            _text(fact["purpose"], "evidence purpose", explanation=True)
        _applicable(record, row)
        result.append(
            {
                **row,
                "content_id": "correction-content-"
                + identity({"source_bundle": record["source_bundle"], **row}),
                "actor": "author_proposal",
                "status": "proposed",
            }
        )
    record["corrections"] = result
    return copy.deepcopy(result)


def correction_context_identity(record: dict[str, Any]) -> str:
    """Hash every proposed reading and scoped user decision for conservative invalidation."""
    return identity(
        {
            "source_bundle": record["source_bundle"],
            "corrections": record.get("corrections", []),
            "authorities": record.get("correction_authorities", []),
        }
    )


def correction_authority_history(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Read validated trusted history without applying old scopes to a changed inventory."""
    _authorities(record)
    return copy.deepcopy(record.get("correction_authorities", []))


def assert_current_correction_authority(root: Path, record: dict[str, Any]) -> None:
    """Reject historical correction approval after any selected authority decision changes."""
    if not record.get("corrections"):
        return
    from leanflow_cli.formalization.records import load_document

    selected = load_document(root, record["document_id"], reconcile_receipt=False)
    if correction_authority_history(record) != correction_authority_history(selected):
        raise ValueError(
            "Correction authority decisions changed; retain this historical evidence and obtain fresh acceptance"
        )


def _checked_corrections(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Revalidate stored proposals instead of trusting a retained author or evidence stamp."""
    stored = record.get("corrections", [])
    if not isinstance(stored, list):
        raise ValueError("Invalid correction registry")
    if not stored:
        return []
    replay = {**record}
    proposed = [
        {key: value for key, value in row.items() if key in _CORRECTION_FIELDS}
        for row in stored
        if isinstance(row, dict)
    ]
    if len(proposed) != len(stored) or apply_corrections(replay, proposed) != stored:
        raise ValueError("Correction content differs from its controller-derived identity")
    return stored


def bind_correction_evidence(
    record: dict[str, Any], checks: dict[str, Any]
) -> list[dict[str, Any]]:
    """Bind independently replayed closed facts without granting correction approval."""
    from leanflow_cli.formalization.correction_evidence import bind_evidence

    return bind_evidence(
        record, checks, _checked_corrections(record), correction_context_identity(record)
    )


def validate_correction_verdicts(dossier: dict[str, Any], value: dict[str, Any]) -> list[str]:
    """Require exact fresh correction verdicts in the same statement-review response."""
    corrections = {row["id"]: row for row in dossier.get("corrections", [])}
    rows = value.get("correction_verdicts", [])
    if not isinstance(rows, list) or len(rows) != len(corrections):
        raise ValueError("Reviewer must assess every assigned correction exactly once")
    evidence = {row["correction_id"]: row for row in dossier.get("correction_evidence", [])}
    if corrections and (
        value.get("revision_id") != dossier.get("revision_id") or not dossier.get("revision_id")
    ):
        raise ValueError("Correction reviewer assessed a different candidate revision")
    seen: set[str] = set()
    blocked = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "correction_id",
            "content_id",
            "evidence_id",
            "authority_refs",
            "decision",
            "rationale",
            "source_support",
            "competing_alternatives",
            "issues",
        }:
            raise ValueError("Invalid independent correction verdict")
        key = row["correction_id"]
        if (
            key not in corrections
            or key in seen
            or key not in evidence
            or row["content_id"] != corrections[key]["content_id"]
            or row["evidence_id"] != evidence[key]["evidence_id"]
            or row["authority_refs"] != corrections[key]["authority_refs"]
            or row["decision"] not in {"PASS", "BLOCK"}
        ):
            raise ValueError(
                "Correction verdict differs from its exact content, evidence or authority"
            )
        seen.add(key)
        for field in ("rationale", "source_support", "competing_alternatives"):
            _text(row[field], "review " + field, explanation=True)
        if (
            not isinstance(row["issues"], list)
            or len(row["issues"]) > 32
            or any(
                not isinstance(issue, str) or not issue.strip() or len(issue) > 4096
                for issue in row["issues"]
            )
        ):
            raise ValueError("Correction verdict requires bounded explicit issues")
        if row["decision"] == "PASS" and row["issues"]:
            raise ValueError("Unresolved correction issues cannot receive PASS")
        if row["decision"] != "PASS":
            blocked.append(key)
    return blocked


def assert_corrections_accepted(record: dict[str, Any], reviews: list[dict[str, Any]]) -> None:
    """Require current independently closed correction PASS receipts before source acceptance."""
    corrections = _checked_corrections(record)
    if not corrections:
        return
    checks = copy.deepcopy(record.get("checks", {}))
    expected = bind_correction_evidence(record, checks)
    if record.get("checks", {}).get("correction_evidence") != expected:
        raise ValueError("Correction evidence is missing or stale")
    indexed = {row["id"]: row for row in corrections}
    seen: set[str] = set()
    if not reviews:
        raise ValueError("Corrections require fresh independent statement review")
    for review in reviews:
        if (
            review.get("revision_id") != record["revision_id"]
            or review.get("accepted") is not True
            or review.get("cleanup_verified") is not True
            or review.get("status") != "completed"
        ):
            raise ValueError("Corrections require fresh independently closed reviews")
        verdicts = review.get("correction_verdicts", [])
        if not isinstance(verdicts, list) or any(
            not isinstance(row, dict) or row.get("correction_id") not in indexed for row in verdicts
        ):
            raise ValueError("Review correction membership differs from this candidate")
        selected = {row["correction_id"] for row in verdicts}
        dossier = {
            "revision_id": record["revision_id"],
            "corrections": [indexed[key] for key in selected],
            "correction_evidence": expected,
        }
        if validate_correction_verdicts(dossier, review):
            raise ValueError("Correction review requires changes")
        seen.update(selected)
    if seen != set(indexed):
        raise ValueError("Corrections lack exact fresh independent PASS verdicts")
