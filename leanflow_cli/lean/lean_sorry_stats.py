"""Count ``sorry`` placeholders in Lean files and project trees."""

from __future__ import annotations

import re
from pathlib import Path

from leanflow_cli.lean.lean_attempt_helpers import _strip_comments_and_strings


def _count_sorries(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    return len(re.findall(r"\bsorry\b", _strip_comments_and_strings(raw)))


def _project_sorry_stats(project_root: Path | None) -> tuple[int | None, list[str]]:
    if project_root is None or not project_root.is_dir():
        return None, []
    total = 0
    files: list[str] = []
    for path in project_root.rglob("*.lean"):
        if any(part in {".git", ".lake", ".leanflow", "build"} for part in path.parts):
            continue
        count = _count_sorries(path)
        if not count:
            continue
        total += count
        try:
            files.append(str(path.relative_to(project_root)))
        except Exception:
            files.append(str(path))
    return total, files
