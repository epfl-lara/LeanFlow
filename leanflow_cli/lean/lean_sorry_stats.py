"""Pure ``sorry``-counting helpers extracted from lean_services (Phase 5).

This module collects the side-effect-free helpers that lean_services uses to count Lean
``sorry`` placeholders: ``_count_sorries`` reads a single ``.lean`` file and counts ``sorry``
tokens outside comments/strings, and ``_project_sorry_stats`` walks a project tree and
aggregates the per-file counts. Each function here is the fixpoint closure under "calls": the
only non-stdlib callee is ``_strip_comments_and_strings`` (already extracted to
``lean_attempt_helpers``) and ``_project_sorry_stats``'s only non-stdlib callee is the sibling
``_count_sorries``. Neither invokes a Lean backend (MCP / REPL / Lake) nor reads
module-mutable state.

Because this module imports ONLY stdlib (``re``, ``pathlib``) plus ``lean_attempt_helpers``
(itself stdlib-only) and does NOT import ``lean_services`` or ``native_runner``, re-exporting
these names back from ``lean_services`` introduces no import cycle. Existing callers keep
resolving them as ``lean_services.<name>`` unchanged, and tests that monkeypatch
``lean_services._project_sorry_stats`` still take effect because the in-module caller
(``lean_inspect``) looks the name up in the ``lean_services`` namespace populated by the shim.
"""

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
