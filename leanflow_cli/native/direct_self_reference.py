"""Reject non-recursive assigned theorem proofs that directly cite themselves."""

from __future__ import annotations

import re

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings


def is_direct_self_reference(declaration: str, target_symbol: str) -> bool:
    """Return whether a proof closes through an immediate circular reference.

    Bare self-reference cannot construct a theorem.  Calls with arguments are
    deliberately allowed because structurally recursive declarations may invoke
    themselves on a smaller argument.  A local recursive binding whose body is
    only its own bare name is equally circular and is rejected as well.
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
    if any(re.search(pattern, source, flags=re.DOTALL) is not None for pattern in patterns):
        return True
    local_cycle = re.search(
        r"\blet\s+rec\s+([A-Za-z_][A-Za-z0-9_']*)\b[^\n]*:=\s*\1\s*(?:\n|$)",
        source,
    )
    return local_cycle is not None
