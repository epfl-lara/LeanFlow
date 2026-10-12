"""Check native kernel isolation without starting a model or reading credentials."""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Any


def kernel_sandbox_status() -> dict[str, Any]:
    """Run a harmless sandbox probe and report missing tools or host restrictions."""
    system = platform.system()
    name = "bwrap" if system == "Linux" else "sandbox-exec"
    binary = shutil.which(name) if system in {"Linux", "Darwin"} else None
    if not binary:
        return {
            "available": False,
            "error": f"Native isolation requires {'Bubblewrap and user namespaces' if system == 'Linux' else 'macOS sandbox-exec or Linux Bubblewrap'}.",
        }
    if system == "Linux":
        command = [
            binary,
            "--ro-bind",
            "/",
            "/",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "/bin/true",
        ]
    else:
        command = [binary, "-p", "(version 1)(allow default)(deny file-write*)", "/usr/bin/true"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": f"Native isolation probe failed: {exc}"}
    if result.returncode:
        return {
            "available": False,
            "error": "Native isolation is blocked by this host: " + result.stderr.strip()[-1000:],
            "remediation": "Ask the host administrator to enable the required sandbox. On restricted Ubuntu hosts, installing Bubblewrap alone may not suffice: load an AppArmor profile permitting user namespaces for /usr/bin/bwrap. See docs/product-reference.md.",
        }
    return {"available": True, "executable": binary}
