"""Run research computations with actionable child-process failure reports."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.utilities import empirical_compute_runtime


def run_computation(program: str, workspace: Path) -> dict[str, Any]:
    """Run a bounded experiment and preserve resource failures at the agent boundary.

    A CPU-killed child has no JSON output. Inspect its exit before decoding so
    the agent can reduce the computation instead of retrying a parsing error.
    """
    if len(program.encode()) > empirical_compute_runtime.MAX_PROGRAM_BYTES:
        raise ValueError("Experiment exceeds the bounded program size")
    guidance = "Use a smaller computation, fewer iterations or smaller rational operands."
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(Path(empirical_compute_runtime.__file__).resolve()),
                "--timeout-s",
                "4",
            ],
            input=program,
            capture_output=True,
            text=True,
            cwd=workspace,
            env={"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
            timeout=6,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps its child before raising.
        return {
            "success": False,
            "status": "empirical_compute_timeout",
            "output": "",
            "error": f"Computation exceeded the 6-second wall limit. {guidance}",
        }
    killed = {-int(signal.SIGKILL), -int(signal.SIGXCPU)}
    if process.returncode in killed:
        return {
            "success": False,
            "status": "empirical_compute_resource_limit",
            "output": "",
            "returncode": process.returncode,
            "error": (
                f"Computation was killed by signal {-process.returncode}; "
                "the child enforces CPU and memory limits. " + guidance
            ),
        }
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        error = "Computation returned no valid structured result."
    else:
        if isinstance(result, dict) and isinstance(result.get("success"), bool):
            # main() exits 1 for a valid negative verdict, including capability
            # denials. Preserve that feedback; only exit 0 may claim success.
            if process.returncode == 0 or (process.returncode == 1 and result["success"] is False):
                return result
        error = "Computation returned an invalid result object."
    if process.returncode:
        error = f"Computation exited with code {process.returncode}. " + error
    return {
        "success": False,
        "status": "empirical_compute_error",
        "output": "",
        "returncode": process.returncode,
        "error": error + (" " + process.stderr[:1000].strip() if process.stderr else ""),
    }
