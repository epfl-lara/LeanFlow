"""Authenticate selected preparation receipts against a live committed transaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.preparation_errors import PreparationConflict
from leanflow_cli.formalization.preparation_journal import load_journal, load_registry


def require_selected_preparation(root: Path, receipt: dict[str, Any]) -> None:
    """Require exact receipt membership after the caller's locked retained-history audit."""
    registry = load_registry(root)
    selected = registry.get("selected")
    if selected is not None and selected not in registry.get("journal_losses", {}):
        journal = load_journal(root, selected)
        if (
            journal["state"] == "COMMITTED"
            and not journal["conflicts"]
            and not journal["resolutions"]
            and journal["plan"]["receipt"] == receipt
        ):
            return
    raise PreparationConflict(
        "Preparation receipt has no matching selected committed transaction; prepare again"
    )
