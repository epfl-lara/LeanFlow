"""Classify source edits that only adjust a timed-out proof's heartbeat budget."""

from __future__ import annotations

import re

_HEARTBEAT_WRAPPER_RE = re.compile(
    r"(?m)^[ \t]*set_option[ \t]+maxHeartbeats[ \t]+[0-9_]+[ \t]+in[ \t]*(?:\r?\n)?"
)


def is_heartbeat_only_change(before_declaration: str, after_declaration: str) -> bool:
    """Return whether an edit only adds or changes ``maxHeartbeats`` wrappers.

    The comparison intentionally ignores layout after removing line-oriented
    wrappers. A substantive tactic, term, or helper change therefore remains
    admissible even when the same edit also adjusts the heartbeat budget.
    """
    before = str(before_declaration or "")
    after = str(after_declaration or "")
    if not before or not after or before == after:
        return False
    if not (_HEARTBEAT_WRAPPER_RE.search(before) or _HEARTBEAT_WRAPPER_RE.search(after)):
        return False

    def normalize(declaration: str) -> str:
        without_wrappers = _HEARTBEAT_WRAPPER_RE.sub("", declaration)
        return " ".join(without_wrappers.split())

    return normalize(before) == normalize(after)


def is_same_tactic_with_budget_wrapper(before_tactic: str, candidate_tactic: str) -> bool:
    """Return whether a local candidate only rewraps the same tactic invocation."""

    def normalize(tactic: str) -> str:
        text = _HEARTBEAT_WRAPPER_RE.sub("", str(tactic or ""))
        text = re.sub(r"^\s*exact\s+by\s+", "", text)
        text = re.sub(r"^\s*classical(?:\s*;\s*|\s+)", "", text)
        return " ".join(text.split())

    before = normalize(before_tactic)
    candidate = normalize(candidate_tactic)
    return bool(before and candidate and before == candidate)
