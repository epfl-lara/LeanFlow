"""Reject scratch-style names for model-generated production Lean helpers."""

from __future__ import annotations

import re

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _declaration_names_from_text,
)

_NONPRODUCTION_NAME_RE = re.compile(
    r"(?:^|_)(?:scratch|temp|test|tmp|counterexample|probe|obstruction|not_universal|"
    r"without_universal|false_of)(?:_|$)|(?:^|_)(?:do|does)_not(?:_|$)",
    flags=re.IGNORECASE,
)


def nonproduction_generated_helpers(
    before_text: str,
    after_text: str,
    *,
    assigned_target: str,
) -> tuple[str, ...]:
    """Return newly introduced declarations whose names still describe scratch work.

    Existing declarations are outside this policy: the managed prover must not
    retroactively reinterpret user source. The assigned declaration is excluded
    because this gate governs only helpers introduced around the active proof.
    """
    before_names = set(_declaration_names_from_text(str(before_text or "")))
    target_aliases = {
        name
        for name in (
            str(assigned_target or "").strip(),
            str(assigned_target or "").strip().split(".")[-1],
        )
        if name
    }
    introduced = (
        name
        for entry in _declaration_line_index_from_text(str(after_text or ""))
        if (name := str(entry.get("name", "") or "").strip())
        and name not in before_names
        and name not in target_aliases
    )
    return tuple(name for name in introduced if _NONPRODUCTION_NAME_RE.search(name))
