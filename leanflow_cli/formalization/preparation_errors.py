"""Share fail-closed preparation ownership errors without coupling recovery state machines."""

from __future__ import annotations

from typing import Any


class PreparationConflict(ValueError):
    """Preserve infrastructure versions until an observed reconciliation is recorded."""


class PreparationAuthorityLoss(PreparationConflict):
    """Expose surviving preparation evidence without importing its selected authority."""

    def __init__(self, discovery: dict[str, Any]) -> None:
        super().__init__(
            "Preparation registry unavailable or incomplete; selected committed authority is unavailable. Inspect preparation-conflicts "
            "and acknowledge exact retained history before fresh preparation"
        )
        self.discovery = discovery
