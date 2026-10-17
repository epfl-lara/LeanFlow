"""Compare optional structural extractors without granting them source-span authority."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .source_provenance import digest
from .source_structure import ENVIRONMENT_KINDS

_PLASTEX_SCRIPT = r"""
import contextlib
import importlib.metadata
import json
import sys
from pathlib import Path
try:
    from plasTeX import Environment
    from plasTeX.TeX import TeX
except ImportError:
    print(json.dumps({"status":"unavailable","error":"plasTeX is not installed in this interpreter"}))
    raise SystemExit(0)
source = Path(sys.argv[1])
kinds = set(json.loads(sys.argv[2]))
with contextlib.redirect_stdout(sys.stderr):
    tex = TeX()
    tex.input(source.read_text(encoding="utf-8"))
    document = tex.parse()
    nodes = []
    pending = list(getattr(document, "childNodes", []))
    while pending:
        node = pending.pop(0)
        pending[0:0] = list(getattr(node, "childNodes", []))
        # amsthm uses generic nodeName='thmenv'; its dynamically generated
        # Python class retains the actual declared environment name.
        kind = type(node).__name__
        if kind not in kinds:
            kind = getattr(node, "nodeName", "")
        if kind in kinds and isinstance(node, Environment):
            nodes.append({"environment":kind,"label":getattr(node, "id", None),
                          "text":str(getattr(node, "textContent", ""))})
print(json.dumps({"status":"parsed","adapter":"plasTeX",
                  "version":importlib.metadata.version("plasTeX"),"items":nodes}))
"""


def compare_plastex(
    root: Path, inventory: dict[str, Any], *, python_executable: str | None = None
) -> dict[str, Any]:
    """Run an opt-in adapter spike on copied, hash-checked inputs using an explicit interpreter.

    This does not install plasTeX, mutate the inventory, establish exact boundaries,
    or turn parser agreement into approval. Corpus adjudication remains separate.
    """
    if inventory["source_kind"] != "latex":
        raise ValueError("The plasTeX structural adapter only accepts LaTeX bundles")
    with tempfile.TemporaryDirectory(prefix="leanflow-plastex-") as temporary:
        frozen = Path(temporary)
        for relative, record in inventory["files"].items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("Source bundle references must be project-relative")
            path = root / relative
            if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
                raise ValueError("Source bundle reference escapes its original root")
            raw = path.read_bytes()
            if digest(raw) != record["sha256"]:
                raise ValueError("Source changed before structural adapter comparison")
            destination = frozen / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        if inventory["source_relative"] not in inventory["files"]:
            raise ValueError("Selected source must be part of the frozen bundle")
        source = frozen / inventory["source_relative"]
        environments = sorted(
            {*ENVIRONMENT_KINDS, *[item["environment"] for item in inventory["items"]]}
        )
        try:
            result = subprocess.run(
                [
                    python_executable or sys.executable,
                    "-I",
                    "-c",
                    _PLASTEX_SCRIPT,
                    str(source),
                    json.dumps(environments),
                ],
                cwd=source.parent,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode:
                return {
                    "status": "failed",
                    "adapter": "plasTeX",
                    "error": result.stderr,
                    "source_inventory_hash": inventory["inventory_hash"],
                    "grants_coverage": False,
                    "external_package_inputs_frozen": False,
                }
            proposal = json.loads(result.stdout)
            proposal["diagnostics"] = result.stderr
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            return {
                "status": "failed",
                "adapter": "plasTeX",
                "error": str(error),
                "source_inventory_hash": inventory["inventory_hash"],
                "grants_coverage": False,
                "external_package_inputs_frozen": False,
            }
    proposal["source_inventory_hash"] = inventory["inventory_hash"]
    proposal["grants_coverage"] = False
    proposal["external_package_inputs_frozen"] = False
    if proposal.get("status") == "parsed":
        expected = Counter(item["environment"] for item in inventory["items"])
        observed = Counter(item["environment"] for item in proposal["items"])
        proposal["environment_counts"] = {"scanner": dict(expected), "adapter": dict(observed)}
        proposal["count_agreement"] = expected == observed
        proposal["boundary_status"] = "unadjudicated"
        proposal["disagreements"] = [
            {"environment": env, "scanner_count": expected[env], "adapter_count": observed[env]}
            for env in sorted(set(expected) | set(observed))
            if expected[env] != observed[env]
        ]
    return proposal
