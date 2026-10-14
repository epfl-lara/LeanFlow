"""Read Lean module imports for project dependency checks and build scheduling."""

from __future__ import annotations

import re

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings


def _lean_imports_from_text(text: str) -> list[str]:
    """Read every module named by plain or public import commands."""
    imports: list[str] = []
    module = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
    for match in re.finditer(
        rf"^[ \t]*(?:public[ \t]+)?import[ \t]+({module}(?:[ \t]+{module})*)\b",
        _strip_lean_comments_and_strings(str(text or "")),
        flags=re.MULTILINE,
    ):
        for name in match.group(1).split():
            if name not in imports:
                imports.append(name)
    return imports
