"""Bind explicitly identified source clauses to authored mappings and independent review checklists."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inventory_authority import complete_inventory

_DISPOSITIONS = {"represented", "pending_proof", "omitted", "specialized", "unresolved"}
_POSITIVE = {"represented", "pending_proof"}
_MAX_OBLIGATIONS = 256
_MAX_REFS = 32
_MAX_NAMES = 64


def _text(value: Any, description: str, *, limit: int = 2048) -> str:
    """Require a bounded substantive explanation instead of an empty approval marker."""
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{description} requires nonempty text of at most {limit} characters")
    return value.strip()


def _refs(record: dict[str, Any], refs: Any) -> None:
    """Require immutable file identities and bounded exact byte or frozen PDF text selections."""
    if not isinstance(refs, list) or not 1 <= len(refs) <= _MAX_REFS:
        raise ValueError("Known clause requires bounded frozen source selections")
    inventory = complete_inventory(record)
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("Known clause source references must be structured")
        file = ref.get("file")
        if not isinstance(file, str):
            raise ValueError("Known clause source file must be a frozen relative name")
        metadata = inventory["files"].get(file, {})
        start, end = ref.get("byte_start"), ref.get("byte_end")
        if (
            file not in record["source_files"]
            or ref.get("file_hash") != "sha256:" + record["source_files"][file]
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= metadata.get("size", -1)
        ):
            raise ValueError("Known clause selection differs from frozen source bytes")
        if ref.get("coordinate_space") == "source_bytes":
            if ref.get("precision") != "exact" or not any(
                row["id"] == ref.get("occurrence_id") and row["file"] == file
                for row in inventory.get("include_occurrences", [])
            ):
                raise ValueError("Known clause requires an exact include occurrence")
        elif ref.get("coordinate_space") == "pdf_page":
            page = next(
                (
                    row
                    for row in inventory.get("pages", [])
                    if row["index"] == ref.get("page_index") and row.get("file", file) == file
                ),
                None,
            )
            first, last = ref.get("text_start"), ref.get("text_end")
            if (
                page is None
                or type(first) is not int
                or type(last) is not int
                or not 0 <= first < last <= len(page.get("text", ""))
                or page["text"][first:last] != ref.get("text")
            ):
                raise ValueError("Known clause differs from its frozen PDF text selection")
        else:
            raise ValueError("Known clause requires a declared frozen coordinate space")


def record_obligation(
    record: dict[str, Any],
    item: dict[str, Any],
    spans: list[dict[str, Any]],
    action: dict[str, Any],
) -> None:
    """Register a known clause without approving a translation, narrowing scope or resolving it."""
    _refs(record, spans)
    payload = {
        "source_id": item["id"],
        "source_bundle": record["source_bundle"],
        "description": _text(action.get("description"), "Known clause description"),
        "reason": _text(action.get("reason"), "Known clause reason"),
        "source_refs": copy.deepcopy(spans),
        "actor": "user",
    }
    row = {"id": "obligation-" + identity(payload), **payload}
    previous = item.setdefault("known_obligations", [])
    if row not in previous:
        if len(previous) >= _MAX_OBLIGATIONS:
            raise ValueError("Source item has too many registered known clauses")
        previous.append(row)


def obligation_registry(
    record: dict[str, Any], source_ids: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Read protected known clauses without treating the checklist as automatic discovery."""
    result: dict[str, dict[str, Any]] = {}
    for item in complete_inventory(record)["items"]:
        if source_ids is not None and item["id"] not in source_ids:
            continue
        rows = item.get("known_obligations", [])
        if not isinstance(rows, list) or len(rows) > _MAX_OBLIGATIONS:
            raise ValueError("Invalid known source clause registry")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "id",
                "source_id",
                "source_bundle",
                "description",
                "reason",
                "source_refs",
                "actor",
            }:
                raise ValueError("Known clause registry contains an invalid entry")
            _text(row["description"], "Known clause description")
            _text(row["reason"], "Known clause reason")
            _refs(record, row["source_refs"])
            if (
                row["source_id"] != item["id"]
                or row["source_bundle"] != record["source_bundle"]
                or row["actor"] != "user"
                or row["id"]
                != "obligation-" + identity({k: v for k, v in row.items() if k != "id"})
                or row["id"] in result
            ):
                raise ValueError("Known clause registry differs from its frozen source identity")
            result[row["id"]] = row
    return result


def validate_dispositions(
    record: dict[str, Any], unit: dict[str, Any], *, compiled: bool = False
) -> dict[str, dict[str, Any]]:
    """Require every known clause and exact mapped declarations, keeping partial clauses visible."""
    required = obligation_registry(record, set(unit["required_source_ids"]))
    rows = unit.get("clause_dispositions", [])
    if not isinstance(rows, list) or len(rows) != len(required):
        raise ValueError("Translation unit must account for every known source clause")
    mapped = {row["name"] for row in unit["declarations"]}
    checked = {row["name"]: row for row in record.get("checks", {}).get("declarations", [])}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "obligation_id",
            "disposition",
            "declaration_names",
            "rationale",
        }:
            raise ValueError("Invalid authored clause disposition")
        key, names = row["obligation_id"], row["declaration_names"]
        if (
            not isinstance(key, str)
            or key not in required
            or key in result
            or not isinstance(row["disposition"], str)
            or row["disposition"] not in _DISPOSITIONS
            or not isinstance(names, list)
            or len(names) > _MAX_NAMES
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or set(names) - mapped
        ):
            raise ValueError(
                "Clause disposition names unknown or duplicate source/declaration evidence"
            )
        _text(row["rationale"], "Clause disposition rationale")
        if row["disposition"] in _POSITIVE and not names:
            raise ValueError("Represented clauses require exact mapped Lean declarations")
        if (
            compiled
            and row["disposition"] == "pending_proof"
            and not any(
                checked.get(name, {}).get("kind") == "theorem"
                and checked[name].get("is_prop") is True
                and checked[name].get("pending") is True
                and checked[name].get("data_sorry") is False
                and checked[name].get("direct_type_sorry") is False
                and checked[name].get("type_untracked_sorry") is False
                for name in names
            )
        ):
            raise ValueError(
                "Pending clause requires an actual named Prop proof slot with complete data/types"
            )
        result[key] = row
    if result.keys() != required.keys():
        raise ValueError("Translation unit omitted a known source clause")
    return result


def validate_clause_verdicts(
    unit: dict[str, Any], verdict: dict[str, Any], obligations: dict[str, dict[str, Any]]
) -> list[str]:
    """Bind independent checklist verdicts and block every known omitted, narrowed or unresolved clause."""
    authored = {row["obligation_id"]: row for row in unit.get("clause_dispositions", [])}
    required = {
        key: row
        for key, row in obligations.items()
        if row["source_id"] in unit["required_source_ids"]
    }
    rows = verdict.get("clause_verdicts", [])
    if not isinstance(rows, list) or len(rows) != len(required):
        raise ValueError("Reviewer must assess every known source clause exactly once")
    seen: set[str] = set()
    blocked: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "obligation_id",
            "decision",
            "rationale",
            "source_evidence_ids",
            "declaration_names",
        }:
            raise ValueError("Invalid independent clause verdict")
        key, names = row["obligation_id"], row["declaration_names"]
        if (
            not isinstance(key, str)
            or key not in required
            or key in seen
            or not isinstance(row["decision"], str)
            or row["decision"] not in {"PASS", "BLOCK"}
            or row["source_evidence_ids"] != ["obligation:" + key]
            or not isinstance(names, list)
            or len(names) > _MAX_NAMES
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or set(names) - {ref["name"] for ref in unit["declarations"]}
        ):
            raise ValueError("Clause verdict differs from exact frozen source/declaration evidence")
        _text(row["rationale"], "Independent clause rationale")
        seen.add(key)
        if key not in authored:
            raise ValueError("Reviewer cannot invent the author's missing clause disposition")
        if (
            row["decision"] == "PASS"
            and authored[key]["disposition"] in _POSITIVE
            and (not names or set(names) != set(authored[key]["declaration_names"]))
        ):
            raise ValueError("Clause PASS requires all exact authored declaration evidence")
        if authored[key]["disposition"] not in _POSITIVE or row["decision"] != "PASS":
            row["decision"] = "BLOCK"
            blocked.append(key)
    return blocked
