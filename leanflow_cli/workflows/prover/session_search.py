"""Bound source-search output and apply read authority to each match."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


def search_sources(query: str, path: Path, readable: Callable[[str], Path]) -> dict[str, Any]:
    """Return bounded literal matches without buffering project-wide output in RAM."""
    if not query or len(query) > 1000:
        raise ValueError("Search query must contain 1 to 1000 characters")
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.run(
            [
                "rg",
                "--json",
                "--hidden",
                "--no-ignore-vcs",
                "-F",
                "--glob",
                "*.lean",
                "--glob",
                "!.leanflow/**",
                "--glob",
                "!.git/**",
                "--max-filesize",
                "1M",
                "--max-count",
                "8",
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
        for raw in output:
            row = json.loads(raw)
            if row.get("type") != "match":
                continue
            data = row["data"]
            try:
                source = readable(data["path"]["text"])
            except (ValueError, KeyError):
                continue
            text = str(data.get("lines", {}).get("text", ""))[:2000]
            matches.append({"path": str(source), "line": data["line_number"], "text": text})
            length += len(text) + len(str(source))
            if length >= 12000 or len(matches) >= 40:
                break
        return {
            "success": process.returncode in {0, 1},
            "results": matches,
            "truncated": length >= 12000 or len(matches) >= 40,
        }
