"""Associate explicitly selected PDF evidence with TeX items without claiming exact alignment."""

from __future__ import annotations

import re
from typing import Any


def _words(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"[^\W\d_]+", text, re.UNICODE))


def _prose(text: str) -> str:
    """Extract only lexical matching hints; preserve the original TeX as claim authority."""
    text = re.sub(r"\\(?:begin|end|label)\s*\{[^{}]*\}", " ", text)
    text = re.sub(r"\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", " ", text, flags=re.DOTALL)
    return re.sub(r"\\[A-Za-z@]+\*?", " ", text)


def _matches(item: dict[str, Any], page: dict[str, Any]) -> tuple[int, int, int] | None:
    """Find conservative shared prose phrases and retain ambiguous matches as candidates."""
    words = [match[0].casefold() for match in _words(_prose(item["text"]))]
    phrases = {tuple(words[index : index + 3]) for index in range(len(words) - 2)}
    page_words = _words(page["text"])
    positions = [
        index
        for index in range(len(page_words) - 2)
        if tuple(match[0].casefold() for match in page_words[index : index + 3]) in phrases
    ]
    matched = {tuple(match[0].casefold() for match in page_words[i : i + 3]) for i in positions}
    if len(matched) < 2 or len(matched) < len(phrases) * 0.25:
        return None
    return len(matched), page_words[min(positions)].start(), page_words[max(positions) + 2].end()


def attach_companion(inventory: dict[str, Any], pdf: dict[str, Any]) -> None:
    """Add frozen companion pages as unaudited evidence and approximate navigation hints."""
    if inventory["source_kind"] != "latex" or pdf["source_kind"] != "pdf":
        raise ValueError("A companion PDF requires one TeX entrypoint and one PDF")
    file = pdf["source_relative"]
    metadata = pdf["files"][file]
    inventory["files"].update(pdf["files"])
    inventory["companion_pdf"] = {
        "file": file,
        "sha256": metadata["sha256"],
        "selection": "explicit",
        "correspondence_verified": False,
    }
    inventory["pages"] = [{**page, "file": file} for page in pdf["pages"]]
    inventory["regions"].extend(
        {
            **region,
            "item_ids": [],
            "reason": "Companion PDF correspondence and additional mathematical content require source audit",
        }
        for region in pdf["regions"]
    )
    inventory["diagnostics"].extend(pdf["diagnostics"])
    refresh_companion_refs(inventory)


def refresh_companion_refs(inventory: dict[str, Any]) -> None:
    """Recompute navigational hints after a user changes a canonical source boundary."""
    pairing = inventory.get("companion_pdf")
    if not pairing:
        return
    file = pairing["file"]
    metadata = inventory["files"][file]
    for item in inventory["items"]:
        base = {
            "file": file,
            "file_hash": metadata["sha256"],
            "byte_start": 0,
            "byte_end": metadata["size"],
            "coordinate_space": "pdf_page",
        }
        candidates: list[tuple[dict[str, Any], tuple[int, int, int]]] = []
        for page in inventory["pages"]:
            match = _matches(item, page)
            if match is not None:
                candidates.append((page, match))
        best = max((match[0] for _, match in candidates), default=0)
        item["pdf_refs"] = [
            {
                **base,
                "page_index": page["index"],
                "text_start": match[1],
                "text_end": match[2],
                "text": page["text"][match[1] : match[2]],
                "mapping_precision": "approximate",
                "mapping_method": "text_match",
            }
            for page, match in candidates
            if match[0] >= best * 0.8
        ] or [{**base, "mapping_precision": "unavailable", "mapping_method": "unavailable"}]
