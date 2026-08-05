"""Pure local proof-context assembly for the Lean proof-context fallback path.

``_local_proof_context_payload`` reconstructs a proof-context payload from an
on-disk declaration slice (no MCP backend, no run state) and is used by
``lean_services.lean_proof_context`` whenever the managed proof-context backend
is unavailable, fails, or returns an empty declaration context.

It depends only on the stateless path-based declaration helpers in
``lean_declarations`` (plus stdlib), so it imports nothing from ``lean_services``
and introduces no import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_declarations import (
    _declaration_index,
    _declaration_text_from_location,
    _find_declaration_entry,
    _split_declaration_statement_and_proof,
    _surrounding_declarations,
)
from leanflow_cli.lean.lean_parsing import LEAN_DECLARATION_PREAMBLE_RE

_BINDER_OPENERS = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}


def _skip_lean_space_and_comments(text: str, start: int) -> int:
    """Return the next source position outside whitespace and Lean comments."""
    index = start
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/-", index):
            depth = 1
            index += 2
            while index < len(text) and depth:
                if text.startswith("/-", index):
                    depth += 1
                    index += 2
                elif text.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        break
    return index


def _balanced_binder_group(text: str, start: int) -> tuple[str, int] | None:
    """Return one balanced declaration binder and the position after its closer."""
    opener = text[start] if start < len(text) else ""
    expected = _BINDER_OPENERS.get(opener)
    if not expected:
        return None
    stack = [expected]
    index = start + 1
    while index < len(text):
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/-", index):
            depth = 1
            index += 2
            while index < len(text) and depth:
                if text.startswith("/-", index):
                    depth += 1
                    index += 2
                elif text.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        if text[index] == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            continue
        nested_closer = _BINDER_OPENERS.get(text[index])
        if nested_closer:
            stack.append(nested_closer)
        elif text[index] == stack[-1]:
            stack.pop()
            if not stack:
                return text[start + 1 : index].strip(), index + 1
        index += 1
    return None


def _local_hypotheses_from_statement(statement: str) -> list[str]:
    """Return explicit source binders as proof-context hypothesis strings."""
    match = re.match(LEAN_DECLARATION_PREAMBLE_RE, str(statement or ""))
    if not match:
        return []
    index = match.end()
    hypotheses: list[str] = []
    possible_universe_group = str(match.group(2) or "").endswith(".")
    while True:
        index = _skip_lean_space_and_comments(statement, index)
        if index >= len(statement) or statement[index] not in _BINDER_OPENERS:
            break
        group = _balanced_binder_group(statement, index)
        if group is None:
            break
        binder, index = group
        # ``foo.{u}`` is split by the shared declaration preamble regex as
        # ``foo.`` plus ``{u}``; universe parameters are not proof hypotheses.
        if possible_universe_group and ":" not in binder:
            possible_universe_group = False
            continue
        possible_universe_group = False
        compact = " ".join(binder.split())
        if compact:
            hypotheses.append(compact)
    return hypotheses


def _enrich_backend_proof_context(
    payload: dict[str, Any], local_payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge source-authoritative declaration data into backend context."""
    if not isinstance(local_payload, Mapping):
        return payload
    backend_has_declaration = any(
        str(payload.get(key, "") or "").strip() for key in ("theorem_statement", "original_proof")
    )
    if not backend_has_declaration:
        # Keep the existing all-empty response path intact: lean_proof_context
        # replaces that payload wholesale with its local fallback, including
        # local proof text and degraded-reason provenance.
        return payload
    enrichment: dict[str, Any] = {}
    source_fields: list[str] = []
    for key in ("theorem_statement", "original_proof"):
        local_value = str(local_payload.get(key, "") or "").strip()
        backend_value = str(payload.get(key, "") or "").strip()
        if local_value and " ".join(local_value.split()) != " ".join(backend_value.split()):
            payload[key] = local_value
            source_fields.append(key)
    if source_fields:
        enrichment["source_authoritative_fields"] = source_fields
    backend_scope = payload.get("in_scope")
    local_scope = local_payload.get("in_scope")
    if isinstance(local_scope, list) and local_scope:
        merged_scope = list(backend_scope) if isinstance(backend_scope, list) else []
        seen_scope = {str(item).strip() for item in merged_scope if str(item).strip()}
        added_scope = []
        for item in local_scope:
            name = str(item or "").strip()
            if name and name not in seen_scope:
                merged_scope.append(name)
                seen_scope.add(name)
                added_scope.append(name)
        if added_scope:
            payload["in_scope"] = merged_scope
            enrichment["preceding_local_declarations"] = len(added_scope)
    local_hypotheses = local_payload.get("hypotheses")
    if not payload.get("hypotheses") and isinstance(local_hypotheses, list) and local_hypotheses:
        local_statement = str(local_payload.get("theorem_statement", "") or "").strip()
        payload["hypotheses"] = list(local_hypotheses)
        statement_enriched = bool(local_statement) and "theorem_statement" not in source_fields
        if statement_enriched:
            payload["theorem_statement"] = local_statement
        enrichment.update(
            {
                "theorem_statement": statement_enriched,
                "hypotheses": True,
                "reason": "backend omitted explicit declaration binders",
            }
        )
    if not enrichment:
        return payload
    metadata = (
        dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}
    )
    metadata["local_context_enrichment"] = enrichment
    payload["metadata"] = metadata
    return payload


def _filter_backend_in_scope_source_order(
    payload: dict[str, Any], file_path: Path, theorem_id: str
) -> dict[str, Any]:
    """Remove target and later same-file names from backend proof context."""
    in_scope = payload.get("in_scope")
    if not isinstance(in_scope, list) or not in_scope:
        return payload
    entries = _declaration_index(file_path)
    wanted = str(theorem_id or "").strip()
    short_name = wanted.split(".")[-1]
    target_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if str(entry.get("name", "") or "").strip() in {wanted, short_name}
        ),
        None,
    )
    if target_index is None:
        return payload
    inaccessible = {
        str(entry.get("name", "") or "").strip()
        for entry in entries[target_index:]
        if str(entry.get("name", "") or "").strip()
    }
    filtered = [
        item for item in in_scope if not isinstance(item, str) or item.strip() not in inaccessible
    ]
    removed_count = len(in_scope) - len(filtered)
    if not removed_count:
        return payload
    payload["in_scope"] = filtered
    metadata = (
        dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}
    )
    metadata["source_order_filter"] = {
        "removed_same_file_names": removed_count,
        "reason": "target and later same-file declarations are unavailable",
    }
    payload["metadata"] = metadata
    return payload


def _local_proof_context_payload(
    file_path: Path,
    theorem_id: str,
    *,
    degraded_reasons: list[str],
    scan_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    entry = _find_declaration_entry(file_path, theorem_id)
    if not entry:
        return None
    theorem_name = str(entry.get("name", "") or theorem_id).strip()
    theorem = dict(scan_payload.get("theorem") or {}) if isinstance(scan_payload, Mapping) else {}
    location = (
        dict(theorem.get("location") or {}) if isinstance(theorem.get("location"), Mapping) else {}
    )
    local_text = _declaration_text_from_location(file_path, location) if location else ""
    if local_text:
        indexed_name = str(entry.get("name", "") or "").strip()
        indexed_kind = str(entry.get("kind", "") or "").strip()
        source_declarations = [
            match
            for line in local_text.splitlines()
            if (match := re.match(LEAN_DECLARATION_PREAMBLE_RE, line))
        ]
        location_matches_entry = any(
            str(match.group(1) or "").strip() == indexed_kind
            and str(match.group(2) or "").strip() == indexed_name
            for match in source_declarations
        )
        if not location_matches_entry:
            # Upstream range scans occasionally report a syntactically valid
            # location for the first line of the file when asked about a local
            # ``def`` or ``abbrev``. Never let that stale location override the
            # exact source declaration already found by the local index.
            local_text = ""
    if not local_text:
        local_text = str(entry.get("text", "") or "")
    statement, proof = _split_declaration_statement_and_proof(local_text)
    metadata = {
        "fallback_source": "local-declaration-slice",
        "declaration_kind": str(entry.get("kind", "") or theorem.get("kind", "")),
        "line": int(entry.get("line", 0) or 0),
        "end_line": int(entry.get("end_line", 0) or 0),
        "scan_theorem": (
            dict(scan_payload or {}) if isinstance(scan_payload, Mapping) and scan_payload else {}
        ),
    }
    if location:
        metadata["location"] = location
    return {
        "success": True,
        "status": "local-fallback",
        "backend_tool": "local-declaration-slice",
        "degraded_reasons": list(dict.fromkeys(degraded_reasons)),
        "file_path": str(file_path),
        "theorem_id": theorem_name,
        "theorem_statement": statement,
        "original_proof": proof,
        "hypotheses": _local_hypotheses_from_statement(statement),
        "in_scope": _surrounding_declarations(file_path, theorem_name),
        "namespace": theorem_name.rsplit(".", 1)[0] if "." in theorem_name else "",
        "similar_proofs": [],
        "metadata": metadata,
        "timing": {},
    }
