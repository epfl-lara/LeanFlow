"""Distinguish external source/setup drift from author-correctable translation errors."""

from __future__ import annotations


class RepreparationRequired(ValueError):
    """Terminate the current author campaign until source and infrastructure are prepared again."""

    error_code = "needs_reprepare"
