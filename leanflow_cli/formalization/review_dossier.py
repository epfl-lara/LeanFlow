"""Expose complete frozen source evidence through a read-only reviewer capability."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.environment import LIBRARY_SEMANTICS
from leanflow_cli.formalization.evidence import digest, identity, read_blob
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.workflows.prover.native_bridge import NativeBridge, _validate_arguments


def _span_text(record: dict[str, Any], span: dict[str, Any]) -> str | None:
    """Read a frozen span in its declared coordinate space, including companion pages."""
    relative = span["file"]
    if span.get("coordinate_space") == "pdf_page":
        pages = record["inventory"].get("pages", [])
        page = next(
            (
                page
                for page in pages
                if page["index"] == span["page_index"] and page.get("file", relative) == relative
            ),
            None,
        )
        if page is None:
            raise ValueError("Frozen PDF span has no matching physical page")
        text = str(page.get("text", ""))
        return text[span.get("text_start", 0) : span.get("text_end", len(text))]
    encoding = record["inventory"]["files"][relative].get("encoding", "utf-8")
    if encoding == "binary":
        return None
    return source_bytes(record, relative)[span["byte_start"] : span["byte_end"]].decode(encoding)


def evidence_documents(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep complete sources, regions, proofs, and compiled declaration evidence available."""
    documents: dict[str, dict[str, Any]] = {
        "inventory": record["inventory"],
        "layout": record["layout"],
        "declarations": record["checks"],
        "units": {"units": record["units"]},
    }
    for row in record["checks"]["declarations"]:
        documents["declaration:" + row["name"]] = row
    for row in record["checks"].get("library_declarations", []):
        documents["library-declaration:" + row["name"]] = {
            **row,
            "authority": (
                "independently-replayed-compiled-project-context"
                if row.get("role") == "project"
                else "independently-replayed-compiled-library"
            ),
            "source_build_correspondence": "not_claimed",
        }
    original = {relative: source_bytes(record, relative) for relative in record["source_files"]}
    for relative in record["source_files"]:
        raw = original[relative]
        if (
            record["inventory"]["source_kind"] == "pdf"
            or record["inventory"].get("companion_pdf", {}).get("file") == relative
        ):
            documents["source:" + relative] = {
                "sha256": digest(raw),
                "pages": [
                    page
                    for page in record["inventory"].get("pages", [])
                    if page.get("file", relative) == relative
                ],
            }
        else:
            encoding = record["inventory"]["files"][relative].get("encoding", "utf-8")
            documents["source:" + relative] = (
                {
                    "sha256": digest(raw),
                    "binary": True,
                    "review_requirement": "If mathematically relevant, require an explicitly verified transcription or block for visual source repair.",
                }
                if encoding == "binary"
                else {"sha256": digest(raw), "text": raw.decode(encoding)}
            )
    for region in record["inventory"]["regions"]:
        relative = region.get("file")
        if relative not in original:
            continue
        text = _span_text(record, region)
        documents["region:" + region["id"]] = {
            "region": region,
            "original_text": text,
            "source_sha256": record["source_files"][relative],
            "binary_requires_visual_repair": text is None,
        }
    lean_files = {**record.get("context_lean_files", {}), **record.get("lean_files", {})}
    for relative, key in lean_files.items():
        raw = read_blob(Path(record["_directory"]) / "blobs", key)
        documents["lean:" + relative] = {"sha256": key, "text": raw.decode()}
    return documents


def mandatory_dossier(
    record: dict[str, Any], units: list[dict[str, Any]], *, coherence: bool
) -> dict[str, Any]:
    """Supply unabridged required items and proofs, with full frozen retrieval as backup."""
    items = {item["id"]: copy.deepcopy(item) for item in record["inventory"]["items"]}
    required = {key for unit in units for key in unit["required_source_ids"]}
    for key in required:
        item = items[key]
        item["proof_text"] = [
            _span_text(record, span) for span in item.get("proof_spans", []) if "byte_start" in span
        ]
    documents = evidence_documents(record)
    referenced_files = {
        ref["file"] for unit in units for ref in unit["declarations"] if ref.get("file")
    }
    return {
        "schema_version": 1,
        "phase": "coherence" if coherence else "translation",
        "units": units,
        "source_items": [items[key] for key in sorted(required)],
        "regions": (
            [
                {
                    **region,
                    "evidence_id": "region:" + region["id"],
                    "text_delivery": (
                        "visual_repair"
                        if documents.get("region:" + region["id"], {}).get("original_text") is None
                        else (
                            "paged"
                            if len(
                                json.dumps(documents["region:" + region["id"]], ensure_ascii=False)
                            )
                            > 16000
                            else "inline"
                        )
                    ),
                    "original_text": (
                        documents.get("region:" + region["id"], {}).get("original_text")
                        if len(
                            json.dumps(
                                documents.get("region:" + region["id"], {}), ensure_ascii=False
                            )
                        )
                        <= 16000
                        else None
                    ),
                }
                for region in record["inventory"]["regions"]
            ]
            if coherence
            else []
        ),
        "excluded_items": [item for item in items.values() if item.get("scope") == "excluded"],
        "setup_notes": record.get("setup_notes", ""),
        "layout": record["layout"],
        "compiled_declarations": [
            {
                **{key: value for key, value in row.items() if key != "body"},
                "evidence_id": "declaration:" + row["name"],
                "body_delivery": "read_evidence",
            }
            for row in record["checks"]["declarations"]
            if row["name"] in {ref["name"] for unit in units for ref in unit["declarations"]}
        ],
        "library_evidence": {
            "authority": "Exact kernel-checked compiled declarations, not presumed source/build correspondence",
            "semantics": copy.deepcopy(LIBRARY_SEMANTICS),
            "review_requirement": (
                "Check the mathematical meaning of every referenced library or existing project "
                "definition and type through library-declaration evidence; names, Git pins and "
                "source files alone do not establish it. Project-role entries expose actual compiled "
                "local context, not approval inferred from its source text. "
                "The listed semantic surface follows statement types and data definitions, excluding "
                "proof-only dependencies. The complete audit closure remains in the evidence catalog. "
                "Block when the compiled meaning cannot be assessed; do not substitute trust in names."
            ),
            "audit_declaration_count": len(record["checks"].get("library_declarations", [])),
            "declarations": [
                {
                    "name": row["name"],
                    "module": row["module"],
                    "role": row.get("role", "dependency"),
                    "evidence_id": "library-declaration:" + row["name"],
                }
                for row in record["checks"].get("library_declarations", [])
                if row.get("semantic_surface", True)
            ],
        },
        "lean_files": {name: documents["lean:" + name] for name in sorted(referenced_files)},
        "evidence_catalog": {
            key: {
                "sha256": identity(value),
                "characters": len(json.dumps(value, ensure_ascii=False)),
            }
            for key, value in documents.items()
        },
        "source_bundle": record["source_bundle"],
        "environment_id": record["environment_id"],
        "policy_id": record["policy_id"],
    }


def review_assignment(
    record: dict[str, Any], dossier: dict[str, Any], dossier_id: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Page large assignments instead of silently truncating them or overflowing native prompts."""
    documents = evidence_documents(record)
    assignment = {
        "revision_id": record["revision_id"],
        "dossier_id": dossier_id,
        "dossier": dossier,
    }
    if len(json.dumps(assignment, ensure_ascii=False)) <= 200000:
        return assignment, documents
    documents["assignment-dossier"] = dossier
    return {
        "revision_id": record["revision_id"],
        "dossier_id": dossier_id,
        "dossier_ref": "assignment-dossier",
        "required_read": "Read every page of assignment-dossier through read_evidence before reviewing; no content was omitted. Its evidence catalog provides full surrounding sources.",
        "unit_count": len(dossier["units"]),
        "region_count": len(dossier["regions"]),
        "characters": len(json.dumps(dossier, ensure_ascii=False, sort_keys=True)),
        "sha256": identity(dossier),
    }, documents


class EvidenceBridge(NativeBridge):
    """Remove every command/check/write operation from the native reviewer bridge."""

    documents: dict[str, dict[str, Any]]

    def schemas(self) -> list[dict[str, Any]]:
        """Advertise only assignment recovery and bounded frozen evidence retrieval."""
        return [
            {
                "name": "get_assignment",
                "description": "Read the complete immutable review assignment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_evidence",
                "description": "Read a bounded page of frozen JSON. Offsets are character offsets; continue until next_offset is null. max_characters optionally reduces the default8000 (1..8000); output is also capped at16000 UTF-8 bytes. No live files are readable.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "offset": {"type": "integer"},
                        "max_characters": {"type": "integer"},
                    },
                    "required": ["id", "offset"],
                    "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve opaque catalog keys rather than accepting reviewer filesystem paths."""
        schema = next((row for row in self.schemas() if row["name"] == name), None)
        if schema is None:
            raise ValueError("Reviewers have no command, edit, or publication capability")
        _validate_arguments(arguments, schema["inputSchema"])
        if name == "get_assignment":
            return copy.deepcopy(self.context)
        key = str(arguments["id"])
        if key not in self.documents:
            raise ValueError("Evidence ID is outside the frozen review catalog")
        text = json.dumps(self.documents[key], ensure_ascii=False, sort_keys=True)
        offset = arguments["offset"]
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise ValueError("Invalid evidence offset")
        limit = arguments.get("max_characters", 8000)
        if type(limit) is not int or not 1 <= limit <= 8000:
            raise ValueError("Evidence page size must be between1 and8000 characters")
        end = min(offset + limit, len(text))
        result = {
            "id": key,
            "sha256": identity(self.documents[key]),
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "complete_length": len(text),
        }
        while len(json.dumps(result, ensure_ascii=False).encode()) > 16000:
            end = offset + (end - offset) // 2
            if end == offset:
                raise ValueError("Evidence metadata exceeds the transport limit")
            result.update(text=text[offset:end], next_offset=end if end < len(text) else None)
        return result
