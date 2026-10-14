"""Propose TeX items and account for every source region without asserting coverage."""

from __future__ import annotations

import re
from typing import Any

from .source_provenance import COMMAND, digest, group, identity
from .source_tex import TexBundle

ENVIRONMENT_KINDS = {
    "theorem": "theorem",
    "thm": "theorem",
    "lemma": "lemma",
    "lem": "lemma",
    "proposition": "proposition",
    "prop": "proposition",
    "corollary": "corollary",
    "cor": "corollary",
    "definition": "definition",
    "defn": "definition",
    "def": "definition",
    "assumption": "assumption",
    "conjecture": "conjecture",
    "conj": "conjecture",
    "claim": "claim",
    "construction": "construction",
    "notation": "notation",
    "example": "example",
    "exercise": "exercise",
    "fact": "fact",
    "observation": "observation",
    "problem": "problem",
    "question": "question",
    "remark": "remark",
    "rem": "remark",
}
ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")
LABEL = re.compile(r"\\label\s*\{([^{}]+)\}")


def _environments(bundle: TexBundle) -> dict[str, str]:
    """Reconcile custom theorem declarations while leaving unknown wrappers visible."""
    kinds = dict(ENVIRONMENT_KINDS)
    for scan in bundle.scans.values():
        for definition in scan.definitions:
            if definition["command"] not in {
                "newtheorem",
                "declaretheorem",
                "newmdtheoremenv",
                "mdtheorem",
                "newtcbtheorem",
                "NewTcbTheorem",
                "spnewtheorem",
                "newenvironment",
                "renewenvironment",
            }:
                continue
            title = definition["body"].lower()
            normalized = next(
                (
                    kind
                    for kind in dict.fromkeys(ENVIRONMENT_KINDS.values())
                    if re.search(r"\b" + kind + r"\b", title)
                ),
                "statement",
            )
            if definition["command"] in {"newenvironment", "renewenvironment"}:
                underlying = ENVIRONMENT.search(definition["body"])
                normalized = kinds.get(underlying[2], "statement") if underlying else "statement"
                if normalized == "statement":
                    continue
            kinds[definition["name"]] = normalized
    return kinds


def _blocks(bundle: TexBundle, expanded: str) -> list[dict[str, Any]]:
    """Pair nested environment delimiters instead of matching nonnested regex bodies."""
    stack: list[tuple[str, int, int]] = []
    blocks: list[dict[str, Any]] = []
    for match in ENVIRONMENT.finditer(expanded):
        if match[1] == "begin":
            stack.append((match[2], match.start(), match.end()))
            continue
        if not stack or stack[-1][0] != match[2]:
            spans = bundle.spans(match.start(), match.end())
            bundle.diagnostic(
                "unmatched_environment",
                f"Unmatched or misnested end of {match[2]}",
                spans[0]["file"] if spans else "",
                spans=spans,
            )
            continue
        name, start, body_start = stack.pop()
        blocks.append(
            {
                "environment": name,
                "start": start,
                "end": match.end(),
                "body_start": body_start,
                "body_end": match.start(),
            }
        )
    for name, start, _ in stack:
        spans = bundle.spans(start, len(expanded))
        bundle.diagnostic(
            "unclosed_environment",
            f"Unclosed {name} environment",
            spans[0]["file"] if spans else "",
            spans=spans,
        )
    for macro, closer, environment in (
        ("profess", "endprofess", "profess"),
        ("proof", "endproof", "proof"),
    ):
        openings = list(re.finditer(r"\\" + macro + r"\b", expanded))
        for opening in openings:
            closing = re.search(r"\\" + closer + r"\b", expanded[opening.end() :])
            if closing is None:
                spans = bundle.spans(opening.start(), opening.end())
                bundle.diagnostic(
                    "unclosed_environment",
                    f"Unclosed plain TeX {macro} block",
                    spans[0]["file"] if spans else "",
                    spans=spans,
                )
                continue
            ending_start, ending_end = (
                opening.end() + closing.start(),
                opening.end() + closing.end(),
            )
            block: dict[str, Any] = {
                "environment": environment,
                "start": opening.start(),
                "end": ending_end,
                "body_start": opening.end(),
                "body_end": ending_start,
            }
            if macro == "profess":
                title = group(expanded, opening.end())
                if title:
                    block["kind"] = next(
                        (
                            kind
                            for kind in dict.fromkeys(ENVIRONMENT_KINDS.values())
                            if kind in title[0].lower()
                        ),
                        "statement",
                    )
                    block["body_start"] = title[1]
            blocks.append(block)
    return sorted(blocks, key=lambda block: block["start"])


def _without(start: int, end: int, removed: list[dict[str, Any]]) -> list[tuple[int, int]]:
    result = []
    cursor = start
    for block in sorted(removed, key=lambda value: value["start"]):
        if block["start"] > cursor:
            result.append((cursor, block["start"]))
        cursor = max(cursor, block["end"])
    if cursor < end:
        result.append((cursor, end))
    return result


def extract_items(bundle: TexBundle, expanded: str) -> list[dict[str, Any]]:
    """Build full source-item proposals with stable labels or occurrence-local ordinals."""
    kinds = _environments(bundle)
    blocks = _blocks(bundle, expanded)
    statements = [
        block for block in blocks if block["environment"].rstrip("*") in kinds or block.get("kind")
    ]
    proofs = [block for block in blocks if block["environment"] == "proof"]
    items: list[dict[str, Any]] = []
    ordinals: dict[tuple[str, str], int] = {}
    labels: dict[str, list[str]] = {}
    for block in statements:
        attached = [
            proof
            for proof in proofs
            if block["start"] < proof["start"] < proof["end"] < block["end"]
        ]
        following = next(
            (
                proof
                for proof in proofs
                if proof["start"] >= block["end"]
                and not expanded[block["end"] : proof["start"]].strip()
            ),
            None,
        )
        if following:
            attached.append(following)
        inside = [proof for proof in attached if proof["end"] <= block["end"]]
        spans = [
            span
            for start, end in _without(block["start"], block["end"], inside)
            for span in bundle.spans(start, end)
        ]
        proof_spans = [
            span for proof in attached for span in bundle.spans(proof["start"], proof["end"])
        ]
        if not spans:
            continue
        direct = expanded[block["body_start"] : block["body_end"]]
        label_match = LABEL.search(direct)
        label = label_match[1].strip() if label_match else None
        title = group(expanded, block["body_start"], "[")
        kind = block.get("kind") or kinds[block["environment"].rstrip("*")]
        key = (spans[0]["occurrence_id"], label or kind)
        ordinals[key] = ordinals.get(key, 0) + 1
        item_id = identity(
            "src", bundle.document_id + ":" + key[0] + ":" + key[1] + ":" + str(ordinals[key])
        )
        if label:
            labels.setdefault(label, []).append(item_id)
        text = bundle.original_text(spans)
        proof_text = bundle.original_text(proof_spans)
        used = {match[1].rstrip("*") for match in COMMAND.finditer(text + proof_text)}
        macro_refs = [
            definition["id"]
            for scan in bundle.scans.values()
            for definition in scan.definitions
            if definition["name"] in used or definition["name"] == block["environment"].rstrip("*")
        ]
        uncertain = any(
            mark[2] == "unresolved"
            and scan.source.offsets[mark[0]] < span["byte_end"]
            and span["byte_start"] < scan.source.offsets[mark[1]]
            for span in spans
            for scan in [bundle.scans[span["file"]]]
            for mark in scan.marks
        )
        status = (
            "proved_in_source"
            if attached
            else (
                "conjectured"
                if kind in {"conjecture", "problem", "question"}
                else (
                    "assumed"
                    if kind == "assumption"
                    else (
                        "defined"
                        if kind in {"definition", "notation", "construction"}
                        else "statement_only"
                    )
                )
            )
        )
        items.append(
            {
                "id": item_id,
                "kind": kind,
                "claim_status": status,
                "label": label,
                "title": title[0] if title else label or kind,
                "statement_spans": spans,
                "proof_spans": proof_spans,
                "context_refs": macro_refs,
                "content_hash": digest(text + "\0" + proof_text),
                "text": text,
                "proof_text": proof_text,
                "scope": "candidate" if uncertain else "included",
                "occurrence_id": key[0],
                "environment": block["environment"],
                "identity_basis": "label_occurrence" if label else "kind_ordinal_occurrence",
            }
        )
    for label, item_ids in labels.items():
        if len(item_ids) > 1:
            bundle.diagnostic(
                "duplicate_label",
                f"Label {label!r} identifies multiple source occurrences",
                "",
                item_ids=item_ids,
                blocking=False,
            )
    for scan in bundle.scans.values():
        for definition in scan.definitions:
            if definition["command"] in {
                "newcommand",
                "renewcommand",
                "providecommand",
                "def",
                "gdef",
                "edef",
                "xdef",
                "DeclareRobustCommand",
            } and re.search(r"\\(?:begin|end|input|include)\b", definition["body"]):
                if re.search(r"\\" + re.escape(definition["name"]) + r"\b", expanded):
                    bundle.diagnostic(
                        "macro_generated_structure",
                        f"Macro \\{definition['name']} generates document structure and needs reconciliation",
                        scan.source.file,
                        macro_id=definition["id"],
                    )
    return items


def _support_marks(text: str) -> list[tuple[int, int, str, str]]:
    """Classify explicit document scaffolding without classifying surrounding prose."""
    result = []
    for command in COMMAND.finditer(text):
        name = command[1].rstrip("*")
        if name in {
            "title",
            "author",
            "date",
            "chapter",
            "section",
            "subsection",
            "subsubsection",
            "paragraph",
            "includeonly",
            "bibliographystyle",
            "label",
            "pagestyle",
        }:
            argument = group(text, command.end())
            if argument:
                result.append(
                    (
                        command.start(),
                        argument[1],
                        "supporting_context",
                        "Document metadata or section context",
                    )
                )
        elif name in {"maketitle", "tableofcontents", "clearpage", "newpage", "appendix"}:
            result.append(
                (command.start(), command.end(), "non_mathematical", "Document layout command")
            )
        elif name in {"begin", "end"}:
            argument = group(text, command.end())
            if argument and argument[0] == "document":
                result.append(
                    (command.start(), argument[1], "supporting_context", "Document delimiter")
                )
    return result


def inventory_regions(bundle: TexBundle, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Partition each included original file occurrence, retaining unresolved prose."""
    regions: list[dict[str, Any]] = []
    for occurrence in bundle.occurrences:
        if occurrence["file"] not in bundle.scans or occurrence["status"] == "unresolved":
            continue
        scan = bundle.scans[occurrence["file"]]
        source = scan.source
        marks = [
            (source.offsets[a], source.offsets[b], d, r)
            for a, b, d, r in [
                *scan.marks,
                *_support_marks(scan.visible),
                *bundle.occurrence_marks.get(occurrence["id"], []),
            ]
        ]
        linked = [
            (span["byte_start"], span["byte_end"], item["id"], key == "proof_spans")
            for item in items
            for key in ("statement_spans", "proof_spans")
            for span in item[key]
            if span["file"] == source.file and span["occurrence_id"] == occurrence["id"]
        ]
        boundaries = sorted(
            {
                0,
                len(source.raw),
                *[x for a, b, _, _ in marks for x in (a, b)],
                *[x for a, b, _, _ in linked for x in (a, b)],
            }
        )
        for start, end in zip(boundaries, boundaries[1:]):
            if start == end:
                continue
            item_ids = sorted({item_id for a, b, item_id, _ in linked if a <= start and end <= b})
            covering = [
                (disposition, reason)
                for a, b, disposition, reason in marks
                if a <= start and end <= b
            ]
            if occurrence["status"] == "excluded":
                disposition, reason = "excluded", "Excluded by includeonly"
            elif any(d == "excluded" for d, _ in covering):
                disposition, reason = next(value for value in covering if value[0] == "excluded")
            elif any(d == "unresolved" for d, _ in covering):
                disposition, reason = next(value for value in covering if value[0] == "unresolved")
            elif item_ids:
                disposition, reason = "item", "Represented statement or source proof"
            elif covering:
                disposition, reason = covering[-1]
            elif not source.raw[start:end].decode(source.encoding).strip():
                disposition, reason = "non_mathematical", "Whitespace"
            else:
                disposition, reason = (
                    "unresolved",
                    "Unclassified source material requires coverage audit",
                )
            regions.append(
                {
                    "id": identity("region", occurrence["id"] + ":" + str(start) + ":" + str(end)),
                    "file": source.file,
                    "byte_start": start,
                    "byte_end": end,
                    "disposition": disposition,
                    "item_ids": item_ids,
                    "reason": reason,
                    "occurrence_id": occurrence["id"],
                    "file_hash": source.sha256,
                    "coordinate_space": "source_bytes",
                }
            )
            if disposition == "excluded":
                regions[-1].update(
                    actor="source",
                    exclusion_kind=(
                        "includeonly"
                        if occurrence["status"] == "excluded"
                        else "post_document" if "after" in reason else "inactive"
                    ),
                )
    for file, record in bundle.files.items():
        if file not in bundle.scans:
            regions.append(
                {
                    "id": identity("region", file + ":support"),
                    "file": file,
                    "byte_start": 0,
                    "byte_end": record["size"],
                    "disposition": "supporting_context",
                    "item_ids": [],
                    "reason": "Referenced original supporting artifact",
                    "coordinate_space": "source_bytes",
                    "file_hash": record["sha256"],
                }
            )
    return regions
