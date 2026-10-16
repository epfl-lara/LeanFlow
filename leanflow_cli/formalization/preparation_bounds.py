"""Share native preparation field bounds between validation and its advertised contract."""

from __future__ import annotations

import json
from typing import Any

MAX_IMPORTS = 256
MAX_PACKAGES = 32
MAX_PACKAGE_FIELD_CHARS = 4000
MAX_PREREQUISITES = 128
MAX_PREREQUISITE_DESCRIPTION_CHARS = 4000
MAX_CANDIDATE_DECLARATIONS = 64
MAX_DECLARATION_HINT_CHARS = 512
MAX_SLICE_SOURCE_IDS = 8
MAX_REPRESENTATION_NOTES = 128
MAX_REPRESENTATION_NOTE_CHARS = 4000
MAX_REPRESENTATION_NOTES_BYTES = 512 * 1024
MAX_BLOCKERS = 32
MAX_BLOCKER_CHARS = 4000


def recommendation_field_bounds() -> dict[str, Any]:
    """Describe every bounded field the native preparation response may supply."""
    return {
        "source_revision_id": "exact assignment source_revision_id",
        "base_plan_id": "exact assignment base_plan_id",
        "required_imports": {"max_items": MAX_IMPORTS, "distinct_portable_modules": True},
        "mathematical_prerequisites": {
            "max_items": MAX_PREREQUISITES,
            "source_ids": "exact included source IDs; at most the included source count",
            "description_characters": [1, MAX_PREREQUISITE_DESCRIPTION_CHARS],
            "strategy": "search_existing_then_define",
            "candidate_declarations": {
                "max_items": MAX_CANDIDATE_DECLARATIONS,
                "name_characters": [1, MAX_DECLARATION_HINT_CHARS],
                "format": "qualified Unicode identifier components; primes, ? or ! may follow the first character",
                "verified_matches": False,
            },
        },
        "representation_notes": {
            "max_items": MAX_REPRESENTATION_NOTES,
            "note_characters": [1, MAX_REPRESENTATION_NOTE_CHARS],
            "total_utf8_bytes": MAX_REPRESENTATION_NOTES_BYTES,
        },
        "slice_source_ids": {
            "items": [1, MAX_SLICE_SOURCE_IDS],
            "distinct_exact_included_source_ids": True,
            "retain_definition_and_result_when_available": True,
        },
        "package_requests": {
            "max_items": MAX_PACKAGES,
            "name_url_revision_reason_max_characters": MAX_PACKAGE_FIELD_CHARS,
            "reason_nonempty": True,
            "imports_max_items": MAX_IMPORTS,
            "imports_portable_modules": True,
            "acquisition_authorized": False,
        },
    }


def recommendation_bounds_contract() -> str:
    """Render the same limits used by validation without omitting whole-paper notes."""
    return (
        "\nNative recommendation field bounds (characters are Unicode characters; "
        "the notes aggregate is UTF-8 bytes):\n"
        + json.dumps(recommendation_field_bounds(), sort_keys=True, separators=(",", ":"))
        + "\nRetain every substantive source ambiguity within these bounds. "
        "These estimates confer no source, semantic or acquisition approval.\n"
    )
