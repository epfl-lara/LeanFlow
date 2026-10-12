"""Guard the MCP v1 API used by managed Lean servers and native job bridges."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

MCP_REQUIREMENT = "mcp>=1.2.0,<2"


def mcp_protocol_ready(venv: Path) -> bool:
    """Verify the required server API without connecting or starting a model."""
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        result = subprocess.run(
            [str(python), "-c", "from mcp.server.fastmcp import FastMCP"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
