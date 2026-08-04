"""Reject non-recursive assigned theorem proofs that directly cite themselves."""

from __future__ import annotations

import re

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings


def is_direct_self_reference(declaration: str, target_symbol: str) -> bool:
    """Return whether a complete proof body is only a bare reference to its target.

    Bare self-reference cannot construct a theorem.  Calls with arguments are
    deliberately allowed because structurally recursive declarations may invoke
    themselves on a smaller argument.
    """
    short = str(target_symbol or "").strip().rsplit(".", 1)[-1]
    if not declaration.strip() or not short:
        return False
    source = _strip_lean_comments_and_strings(declaration).strip()
    qualified = rf"(?:[A-Za-z_][A-Za-z0-9_']*\.)*{re.escape(short)}"
    bare = rf"\(?\s*{qualified}\s*\)?"
    patterns = (
        rf":=\s*{bare}\s*$",
        rf":=\s*by\s+exact\s+{bare}\s*$",
        rf":=\s*by\s+simpa(?:\s+only\s*\[[^\]]*\])?\s+using\s+{bare}\s*$",
    )
    return any(re.search(pattern, source, flags=re.DOTALL) is not None for pattern in patterns)
