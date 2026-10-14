"""Serve immutable review assignments and evidence within the complete UTF-8 MCP envelope budget."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.workflows.prover.native_bridge import NativeBridge, _validate_arguments

REVIEW_RESPONSE_BYTES = 16000
_REQUEST_ID_BYTES = 256


def _json_text(value: Any) -> str:
    """Serialize frozen evidence consistently for lossless character-offset pagination."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _response_size(value: Any) -> int:
    """Measure UTF-8 including MCP text escaping and a bounded JSON-RPC request ID."""
    return len(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "x" * _REQUEST_ID_BYTES,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "isError": False,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _assignment_manifest(assignment: dict[str, Any], key: str) -> dict[str, Any]:
    """Describe a complete paginated assignment without copying large identifiers."""
    return {
        "assignment_ref": key,
        "characters": len(_json_text(assignment)),
        "utf8_bytes": len(_json_text(assignment).encode("utf-8")),
        "sha256": identity(assignment),
        "required_read": "Read every page of assignment_ref through read_evidence, continuing until next_offset is null. Reconstruct the complete JSON assignment before reviewing; no evidence was omitted.",
    }


class EvidenceBridge(NativeBridge):
    """Remove every command/check/write operation from the native reviewer bridge."""

    documents: dict[str, dict[str, Any]]

    def schemas(self) -> list[dict[str, Any]]:
        """Advertise only assignment recovery and bounded frozen evidence retrieval."""
        return [
            {
                "name": "get_assignment",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
                "description": "Recover the immutable assignment or its small manifest. Follow assignment_ref/dossier_ref with read_evidence until next_offset is null; responses, including the MCP envelope, fit 16000 UTF-8 bytes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_evidence",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
                "description": "Read a bounded page of frozen JSON. Offsets are character offsets; continue until next_offset is null. max_characters optionally reduces the default 8000 (1..8000); the full MCP response is capped at 16000 UTF-8 bytes. No live files are readable.",
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
            if _response_size(self.context) <= REVIEW_RESPONSE_BYTES:
                return copy.deepcopy(self.context)
            key = "assignment-context"
            self.documents[key] = copy.deepcopy(self.context)
            return _assignment_manifest(self.context, key)
        key = str(arguments["id"])
        if key not in self.documents:
            raise ValueError("Evidence ID is outside the frozen review catalog")
        text = _json_text(self.documents[key])
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
        while _response_size(result) > REVIEW_RESPONSE_BYTES:
            end = offset + (end - offset) // 2
            if end == offset:
                raise ValueError("Evidence metadata exceeds the transport limit")
            result.update(text=text[offset:end], next_offset=end if end < len(text) else None)
        return result

    def dispatch(self, request: Any) -> dict[str, Any]:
        """Keep the actual transport envelope bounded, including caller request identities."""
        if isinstance(request, dict) and len(_json_text(request.get("id")).encode("utf-8")) > (
            _REQUEST_ID_BYTES
        ):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "Review request ID exceeds the response budget",
                },
            }
        response = super().dispatch(request)
        if (
            isinstance(request, dict)
            and request.get("method") == "tools/call"
            and len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            > REVIEW_RESPONSE_BYTES
        ):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Review response metadata exceeds the transport budget",
                        }
                    ],
                    "isError": True,
                },
            }
        return response
