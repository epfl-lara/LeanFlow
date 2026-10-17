"""Project source-inventory provenance into document-tool display fields without new extraction."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Mapping
from typing import Any

from leanflow_cli.formalization.source_provenance import SourceText, group

MAX_CONTEXT_EXCERPT_CHARS = 24_000


def _arguments(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read balanced arguments only at command locations identified by the source inventory."""
    values = []
    sources: dict[str, SourceText] = {}
    for command in inventory.get("command_references", []):
        if command["name"] not in {
            "title",
            "centerline",
            "chapter",
            "section",
            "subsection",
            "subsubsection",
            "label",
            "ref",
            "eqref",
            "pageref",
            "cref",
            "Cref",
            "autoref",
            "cite",
            "citep",
            "citet",
            "nocite",
            "bibliography",
            "addbibresource",
            "uses",
            "lean",
        }:
            continue
        spans = command["spans"]
        if len(spans) != 1:
            continue
        span = spans[0]
        file = span["file"]
        if file not in sources:
            record = inventory["files"][file]
            sources[file] = SourceText(file, record["text"].encode(record["encoding"]))
        source = sources[file]
        text = source.text
        offset = bisect_left(source.offsets, span["byte_end"])
        option = group(text, offset, "[")
        value = group(text, option[1] if option else offset)
        if value:
            values.append({"name": command["name"], "value": value[0], "span": span})
    return values


def _references(values: list[dict[str, Any]], names: set[str]) -> list[str]:
    """Deduplicate display reference labels without bounding source authority."""
    return list(
        dict.fromkeys(
            part.strip()
            for value in values
            if value["name"] in names
            for part in value["value"].split(",")
            if part.strip()
        )
    )


def _inside(command: Mapping[str, Any], spans: list[dict[str, Any]]) -> bool:
    """Associate commands with the exact original include occurrence of an item."""
    point = command["span"]
    return any(
        point["file"] == span["file"]
        and point.get("occurrence_id") == span.get("occurrence_id")
        and span["byte_start"] <= point["byte_start"] < span["byte_end"]
        for span in spans
    )


def display_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Expose complete statements and one explicitly bounded context excerpt from frozen bytes."""
    values = _arguments(inventory)
    titles = [value["value"].strip() for value in values if value["name"] == "title"]
    if not titles:
        titles = [
            re.sub(r"^\s*\\titlefont\s+", "", value["value"]).strip()
            for value in values
            if value["name"] == "centerline" and value["value"].lstrip().startswith("\\titlefont")
        ]
    text = (
        "\n\n".join(str(record.get("text", "")) for record in inventory["files"].values())
        if inventory["source_kind"] == "latex"
        else "\n\n".join(page["text"] for page in inventory["pages"])
    )
    blocks = []
    for item in inventory["items"]:
        span = item["statement_spans"][0]
        proof_span = item["proof_spans"][0] if item["proof_spans"] else {}
        commands = [value for value in values if _inside(value, item["statement_spans"])]
        blocks.append(
            {
                **item,
                "label": item.get("label") or f"line-{span.get('line_start', 0)}-{item['kind']}",
                "statement": item["text"],
                "proof": item.get("proof_text", ""),
                "line": span.get("line_start", 0),
                "end_line": item["statement_spans"][-1].get("line_end", 0),
                "proof_line": proof_span.get("line_start", 0),
                "uses": _references(commands, {"uses"}),
                "lean": _references(commands, {"lean"}),
            }
        )
    return {
        "title": " ".join(titles),
        "bytes": sum(record["size"] for record in inventory["files"].values()),
        "sections": [
            {
                "level": value["name"],
                "title": value["value"].strip(),
                "line": value["span"]["line_start"],
                **value["span"],
            }
            for value in values
            if value["name"] in {"chapter", "section", "subsection", "subsubsection"}
        ],
        "theorem_blocks": blocks,
        "labels": _references(values, {"label"}),
        "refs": _references(values, {"ref", "eqref", "pageref", "cref", "Cref", "autoref"}),
        "citations": _references(values, {"cite", "citep", "citet", "nocite"}),
        "bibliography_files": _references(values, {"bibliography", "addbibresource"}),
        "pages": len(inventory["pages"]),
        "extracted_text": text,
        "text_excerpt": text[:MAX_CONTEXT_EXCERPT_CHARS],
        "text_excerpt_truncated": len(text) > MAX_CONTEXT_EXCERPT_CHARS,
        "title_and_sections_are_display_only": True,
    }
