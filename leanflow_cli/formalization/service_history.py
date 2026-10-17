"""Page saved revision identities without loading historical receipts or environments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, identifier
from leanflow_cli.formalization.publication_case_paths import (
    assert_receipt_control_path,
    exact_control_path,
)
from leanflow_cli.formalization.records import document_directory

MAX_HISTORY_ENTRIES = 20000


def revision_history(
    root: Path, document_id: str, selected_revision: str, *, offset: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Return a bounded non-authorizing index with the selected identity first."""
    if (
        type(offset) is not int
        or not 0 <= offset <= MAX_HISTORY_ENTRIES
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Revision history requires offset0..20000 and limit1..100")
    document_id, selected_revision = identifier(document_id), identifier(selected_revision)
    names = {selected_revision}
    visited = 0
    for published in (False, True):
        if published and not exact_control_path(root, "formalization"):
            # A case-folded ordinary Lean library is not portable controller history.
            continue
        directory = document_directory(root, document_id, published=published)
        parent = contained(directory, "revisions")
        assert_receipt_control_path(directory, "revisions")
        if not parent.exists():
            continue
        for path in parent.iterdir():
            visited += 1
            if visited > MAX_HISTORY_ENTRIES:
                raise ValueError("Saved revision namespace exceeds the bounded history inventory")
            if path.suffix == ".json":
                names.add(identifier(path.stem))
    ordered = [selected_revision, *sorted(names - {selected_revision})]
    end = min(offset + limit, len(ordered))
    return {
        "schema_version": 1,
        "document_id": document_id,
        "revision_id": selected_revision,
        "currentness": "historical",
        "revisions": [
            {"revision_id": value, "status": "historical"} for value in ordered[offset:end]
        ],
        "offset": offset,
        "next_offset": end if end < len(ordered) else None,
        "total": len(ordered),
    }
