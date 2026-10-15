"""Validate explicit source repairs while preserving unresolved residual coverage."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.inventory_authority import (
    complete_inventory,
    select_review_inventory,
)
from leanflow_cli.formalization.records import source_artifact_id, source_bytes


def _reason(action: dict[str, Any]) -> str:
    reason = str(action.get("reason", "")).strip()
    if not reason:
        raise ValueError("This coverage decision requires a reason")
    return reason


def _item(record: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in record["inventory"]["items"]:
        if item["id"] == item_id:
            return item
    raise ValueError("Source item does not belong to this revision")


def _page(record: dict[str, Any], index: Any) -> dict[str, Any]:
    pages = record["inventory"].get("pages", [])
    if type(index) is not int or not 0 <= index < len(pages):
        raise ValueError("PDF selection requires a valid physical page")
    return pages[index]


def validated_spans(record: dict[str, Any], refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve selections against immutable source bytes, with explicit PDF precision."""
    if not refs:
        raise ValueError("A source item requires at least one source selection")
    result = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("Source selections must be structured references")
        file = str(ref.get("file", ""))
        if "artifact_id" in ref:
            matches = [
                name
                for name in record["source_files"]
                if source_artifact_id(record, name) == ref["artifact_id"]
            ]
            if len(matches) != 1 or (file and file != matches[0]):
                raise ValueError("Source artifact identity does not match its file")
            file = matches[0]
        raw = source_bytes(record, file)
        common = {"file": file, "file_hash": "sha256:" + digest(raw)}
        if record["inventory"]["source_kind"] == "pdf":
            page = _page(record, ref.get("page_index"))
            text = str(ref.get("text", "")).strip()
            if not text:
                raise ValueError("PDF repair requires selected text or a verified transcription")
            original = str(page.get("text", ""))
            start, end = ref.get("text_start"), ref.get("text_end")
            if type(start) is int and type(end) is int:
                if not 0 <= start < end <= len(original) or original[start:end].strip() != text:
                    raise ValueError("PDF selection differs from the frozen page text")
            else:
                start = original.find(text)
                if start < 0 or original.find(text, start + 1) >= 0:
                    raise ValueError(
                        "PDF selection must uniquely match the frozen page text; repair its transcription or specify exact text offsets"
                    )
                end = start + len(text)
            result.append(
                {
                    **common,
                    "byte_start": 0,
                    "byte_end": len(raw),
                    "page_index": page["index"],
                    "text_start": start,
                    "text_end": end,
                    "precision": "approximate",
                    "text": original[start:end],
                    "coordinate_space": "pdf_page",
                    "mapping_method": page.get("mapping_method", "manual_transcription"),
                }
            )
            continue
        start, end = ref.get("byte_start"), ref.get("byte_end")
        if type(start) is not int or type(end) is not int:
            lines = raw.splitlines(keepends=True)
            first, last = ref.get("line_start"), ref.get("line_end")
            if (
                type(first) is not int
                or type(last) is not int
                or not 1 <= first <= last <= len(lines)
            ):
                raise ValueError("Source selection lines are outside the frozen file")
            start, end = sum(map(len, lines[: first - 1])), sum(map(len, lines[:last]))
        if not 0 <= start < end <= len(raw):
            raise ValueError("Source selection bytes are outside the frozen file")
        encoding = record["inventory"]["files"][file].get("encoding", "utf-8")
        if encoding == "binary":
            raise ValueError("A binary supporting file cannot be selected as TeX text")
        try:
            raw[:start].decode(encoding)
            raw[start:end].decode(encoding)
        except UnicodeDecodeError as error:
            raise ValueError("Selection splits a source character") from error
        occurrences = [
            entry["id"]
            for entry in record["inventory"].get("include_occurrences", [])
            if entry["file"] == file and entry["status"] != "unresolved"
        ]
        occurrence = ref.get("occurrence_id")
        if occurrence is None and len(occurrences) == 1:
            occurrence = occurrences[0]
        if occurrence not in occurrences:
            raise ValueError("Choose an exact include occurrence for this source selection")
        result.append(
            {
                **common,
                "byte_start": start,
                "byte_end": end,
                "line_start": raw[:start].count(b"\n") + 1,
                "line_end": raw[: max(start, end - 1)].count(b"\n") + 1,
                "precision": "exact",
                "coordinate_space": "source_bytes",
                "occurrence_id": occurrence,
            }
        )
    return result


def _repair_regions(
    record: dict[str, Any], item: dict[str, Any], spans: list[dict[str, Any]]
) -> None:
    """Split partial selections and release old boundaries instead of covering whole pages."""
    regions = record["inventory"]["regions"]
    for region in regions:
        if item["id"] in region.get("item_ids", []):
            region["item_ids"] = [value for value in region["item_ids"] if value != item["id"]]
            if region["disposition"] in {"item", "represented_item"} and not region["item_ids"]:
                region.update(
                    disposition="unresolved", reason="Previous source-item boundary was replaced"
                )
    for span in spans:
        repaired = []
        pdf = span.get("coordinate_space") == "pdf_page"
        start_key, end_key = ("text_start", "text_end") if pdf else ("byte_start", "byte_end")
        for region in regions:
            same = region["file"] == span["file"] and (
                region.get("page_index") == span.get("page_index")
                if pdf
                else region.get("occurrence_id") == span.get("occurrence_id")
            )
            if not same or region.get("repair_required"):
                repaired.append(region)
                continue
            first = region.get(start_key, 0)
            last = region.get(end_key, len(_page(record, span["page_index"])["text"]) if pdf else 0)
            begin, end = max(first, span[start_key]), min(last, span[end_key])
            if begin >= end:
                repaired.append(region)
                continue
            for left, right in ((first, begin), (begin, end), (end, last)):
                if left >= right:
                    continue
                part = {
                    **region,
                    start_key: left,
                    end_key: right,
                    "id": "region-"
                    + identity({"parent": region["id"], "start": left, "end": right})[:24],
                }
                if left == begin and right == end:
                    part.update(
                        disposition="item",
                        item_ids=sorted({*region.get("item_ids", []), item["id"]}),
                        reason="Explicit user source selection",
                        actor="user",
                    )
                repaired.append(part)
        regions = repaired
    record["inventory"]["regions"] = regions


def _classify(record: dict[str, Any], action: dict[str, Any]) -> None:
    classification = action.get("classification")
    if classification not in {"non_mathematical", "supporting_context", "excluded"}:
        raise ValueError("A represented region needs an actual source item")
    regions = [
        region
        for region in record["inventory"]["regions"]
        if region["id"] == action.get("region_id")
    ]
    if len(regions) != 1:
        raise ValueError("Unknown source region")
    region = regions[0]
    if region.get("repair_required") and classification != "excluded":
        raise ValueError(
            "Unextractable material requires a verified transcription or an explicit scope exclusion"
        )
    if region.get("item_ids"):
        if classification != "excluded" or any(
            _item(record, item_id)["scope"] != "excluded" for item_id in region["item_ids"]
        ):
            raise ValueError(
                "Represented source items must be explicitly excluded before excluding their region"
            )
    region.update(disposition=classification, reason=_reason(action), actor="user")
    if classification == "excluded" and region.get("repair_required"):
        region["repair_required"] = False
        for diagnostic in record["inventory"]["diagnostics"]:
            if diagnostic.get("page_index") == region.get("page_index") and diagnostic["code"] in {
                "pdf_text_missing",
                "pdf_fidelity_unverified",
            }:
                diagnostic.update(
                    blocking=False,
                    resolution="explicit_scope_exclusion",
                    resolution_reason=region["reason"],
                )
    from leanflow_cli.formalization.pdf_actions import reconcile_extraction_diagnostics

    reconcile_extraction_diagnostics(record["inventory"])


def _transcribe(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Replace verified page evidence and reopen affected claims without reusing offsets."""
    inventory = record["inventory"]
    if (inventory["source_kind"] != "pdf" and not inventory.get("companion_pdf")) or action.get(
        "verified"
    ) is not True:
        raise ValueError(
            "Page transcription requires an explicit verification against the frozen PDF"
        )
    page = _page(record, action.get("page_index"))
    text, reason = str(action.get("text", "")).strip(), _reason(action)
    if not text:
        raise ValueError("A verified transcription must contain the complete page text")
    from leanflow_cli.formalization.pdf_actions import replace_transcription

    replace_transcription(record, page, text, reason)


def apply_action(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Apply one typed repair to a new revision; never accept review claims from clients."""
    result = copy.deepcopy(record)
    result["inventory"] = copy.deepcopy(complete_inventory(record))
    _apply_action(result, action)
    if "full_inventory" in record:
        result["full_inventory"] = copy.deepcopy(result["inventory"])
        select_review_inventory(result)
    record.clear()
    record.update(result)


def _apply_action(record: dict[str, Any], action: dict[str, Any]) -> None:
    """Mutate complete source authority only after the caller creates a private repair copy."""
    item: dict[str, Any]
    kind = action.get("type")
    if kind == "set_scope":
        scope = action.get("scope")
        if scope not in {"included", "excluded"}:
            raise ValueError("Scope must be included or excluded")
        _item(record, str(action.get("item_id", ""))).update(
            scope=scope, scope_reason=_reason(action), scope_actor="user"
        )
        for diagnostic in record["inventory"]["diagnostics"]:
            if diagnostic["code"] == "pdf_item_boundary_missing" and diagnostic.get(
                "item_id"
            ) == action.get("item_id"):
                diagnostic.update(
                    blocking=scope == "included"
                    and not _item(record, str(action["item_id"]))["statement_spans"],
                    resolution="explicit_scope_exclusion" if scope == "excluded" else None,
                    resolution_reason=_reason(action),
                )
    elif kind == "authorize_proof":
        if type(action.get("authorized")) is not bool:
            raise ValueError("Proof authorization requires an explicit boolean decision")
        _item(record, str(action.get("item_id", "")))["proof_authorization"] = {
            "actor": "user",
            "authorized": action["authorized"],
            "reason": _reason(action),
        }
    elif kind == "record_obligation":
        from leanflow_cli.formalization.clause_obligations import record_obligation

        refs = action.get("source_refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
            raise ValueError("Known clause requires bounded frozen source selections")
        record_obligation(
            record,
            _item(record, str(action.get("item_id", ""))),
            validated_spans(record, refs),
            action,
        )
    elif kind == "classify_region":
        _classify(record, action)
    elif kind == "resolve_command":
        from leanflow_cli.formalization.diagnostic_actions import resolve_command

        resolve_command(record, action)
    elif kind == "resolve_conditional":
        from leanflow_cli.formalization.diagnostic_actions import resolve_conditional

        resolve_conditional(record, action)
    elif kind in {"transcribe_page", "replace_page_transcription"}:
        _transcribe(record, action)
    elif kind == "retry_extraction":
        from leanflow_cli.formalization.extraction_actions import retry_extraction

        retry_extraction(record, _reason(action))
    elif kind in {"add_item", "update_boundary"}:
        refs = action.get("source_refs")
        if not isinstance(refs, list):
            raise ValueError("Source selections must be a list")
        spans = validated_spans(record, refs)
        text = "\n".join(
            (
                str(span["text"])
                if "page_index" in span
                else source_bytes(record, span["file"])[
                    span["byte_start"] : span["byte_end"]
                ].decode(record["inventory"]["files"][span["file"]].get("encoding", "utf-8"))
            )
            for span in spans
        )
        if kind == "add_item":
            title = str(action.get("title", "")).strip()
            if not title:
                raise ValueError("A new source item requires a title")
            item = {
                "id": "src-user-"
                + identity(
                    {
                        "document_id": record["document_id"],
                        "ordinal": len(record["inventory"]["items"]),
                        "parent_revision_id": record.get("parent_revision_id"),
                    }
                )[:24],
                "kind": str(action.get("kind", "statement")),
                "claim_status": "unclassified",
                "title": title,
                "scope": "included",
                "scope_actor": "user",
                "proof_spans": [],
                "context_refs": [],
            }
            record["inventory"]["items"].append(item)
        else:
            item = _item(record, str(action.get("item_id", "")))
            # Consent concerns the selected claim, so a changed boundary needs
            # a fresh decision even when the item keeps its structural ID.
            item.pop("proof_authorization", None)
        item.update(
            statement_spans=spans,
            text=text,
            content_hash="sha256:" + identity({"text": text, "spans": spans}),
            scope="included",
            boundary_review_required=False,
        )
        for diagnostic in record["inventory"]["diagnostics"]:
            if (
                diagnostic["code"] == "pdf_item_boundary_missing"
                and diagnostic.get("item_id") == item["id"]
            ):
                diagnostic.update(blocking=False, resolution="explicit_source_boundary")
        _repair_regions(record, item, spans)
        from leanflow_cli.formalization.source_companion import refresh_companion_refs

        refresh_companion_refs(record["inventory"])
    elif kind == "set_mapping":
        from leanflow_cli.formalization.mapping_actions import set_mapping

        set_mapping(record, action)
    else:
        raise ValueError("Unknown formalization action; review approval cannot be set by an action")
    record["actions"].append({**action, "actor": "user"})
    record["inventory"]["audit_complete"] = False
