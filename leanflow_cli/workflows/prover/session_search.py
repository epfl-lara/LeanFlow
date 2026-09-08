"""Bound source-search output and apply read authority to each match."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MATCHES_PER_FILE = 8

#: Lean sources only: what a lemma search should see.
LEAN_GLOBS: tuple[str, ...] = ("*.lean",)
#: Lean sources plus the text documentation a project ships with -- notes,
#: papers extracted to text, blueprints, bibliographies. A project's own
#: write-ups were otherwise unreachable by any prover tool: read_file needs a
#: path, and nothing could discover one.
PROJECT_GLOBS: tuple[str, ...] = ("*.lean", "*.md", "*.txt", "*.tex", "*.bib")


def search_sources(
    query: str,
    path: Path,
    readable: Callable[[str], Path],
    *,
    globs: tuple[str, ...] = LEAN_GLOBS,
) -> dict[str, Any]:
    """Return bounded literal matches without buffering project-wide output in RAM."""
    if not query or len(query) > 1000:
        raise ValueError("Search query must contain 1 to 1000 characters")
    include = [arg for glob in globs for arg in ("--glob", glob)]
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.run(
            [
                "rg",
                "--json",
                "--hidden",
                "--no-ignore-vcs",
                "-F",
                *include,
                "--glob",
                "!.leanflow/**",
                "--glob",
                "!.git/**",
                "--max-filesize",
                "1M",
                "--max-count",
                str(_MATCHES_PER_FILE + 1),
                "--",
                query,
                str(path),
            ],
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        output.seek(0)
        matches = []
        length = 0
        matches_per_file: dict[Path, int] = {}
        truncated = False
        for raw in output:
            row = json.loads(raw)
            if row.get("type") != "match":
                continue
            data = row["data"]
            try:
                source = readable(data["path"]["text"])
            except (ValueError, KeyError):
                continue
            count = matches_per_file.get(source, 0)
            if count >= _MATCHES_PER_FILE:
                # One extra match distinguishes exactly-at-limit from omitted results.
                truncated = True
                continue
            matches_per_file[source] = count + 1
            raw_text = str(data.get("lines", {}).get("text", ""))
            text = raw_text[:2000]
            truncated = truncated or len(raw_text) > len(text)
            matches.append({"path": str(source), "line": data["line_number"], "text": text})
            length += len(text) + len(str(source))
            if length >= 12000 or len(matches) >= 40:
                truncated = True
                break
        return {
            "success": process.returncode in {0, 1},
            "results": matches,
            "truncated": truncated,
        }
