"""Prepare native workflow instructions and copy formalization inputs into job authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained, digest, identity
from leanflow_cli.runtime.skill_core import load_skill
from leanflow_cli.workflows.prover.native_snapshot import remap_project_text

_CONTRACT = """You are the native agent for a LeanFlow {workflow} workflow.
Use your native coding tools in this private project copy. Your runtime owns model
settings, conversation history and compaction. LeanFlow independently verifies every
source proposal before promotion. Keep original statements and dependency settings.
Use the leanflow MCP get_assignment tool to recover this assignment after compaction.
Use start_lean_check and start_command with poll_operation for long Lean work;
LeanFlow imposes only an optional global campaign deadline. Native shell tools retain
their own behavior. Call submit_files with a short JSON report once your changes are
ready, and poll_operation for acceptance or actionable rejection. Continue in this
conversation after rejection. Stop after acceptance or an honest external blocker.
For a blocker, return exactly {\"status\":\"blocked\",\"reason\":\"concrete blocker\"}.
Implement definitions and constructions fully. Named Prop theorem proofs
may remain pending; completed proofs and exact library reuse are valid. Do not edit
canonical files or write approval prose as evidence. Proving is a separate action.
"""

_FORMALIZATION_CONTRACT = """Formalization uses the complete frozen source inventory and
structured proposal specified in the assignment. Follow the checked preparation
plan and representative-slice receipt before full expansion. Independent item and
document reviews gate publication.
"""


def _copy_source_partition(root: Path, snapshot: Path, scratch: Path, manifest: str) -> str:
    """Authenticate and copy the lossless audit partition before exposing its private path."""
    value = json.loads(manifest)
    reference = value.get("source_region_partition")
    if reference is None:
        return manifest
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError("Formalization source partition reference is invalid")
    selected = Path(reference["path"])
    selected = root / selected if not selected.is_absolute() else selected
    selected = contained(root, selected.relative_to(root).as_posix())
    raw = read_regular_bytes(selected, limit=128 * 1024**2)
    partition = json.loads(raw)
    if (
        not isinstance(partition, dict)
        or identity(partition) != reference.get("sha256")
        or len(partition.get("canonical_inventory", {}).get("regions", []))
        != reference.get("canonical_region_count")
        or len(partition.get("groups", [])) != reference.get("group_count")
    ):
        raise ValueError("Formalization source partition differs from frozen intake evidence")
    target = contained(
        snapshot, (scratch / "formalization-source-partition.json").relative_to(snapshot).as_posix()
    )
    target.write_bytes(raw)
    value["source_region_partition"] = {
        **reference,
        "path": str(target),
        "bytes_sha256": digest(raw),
    }
    return json.dumps(value, ensure_ascii=False)


def prepare_context(
    *,
    root: Path,
    snapshot: Path,
    scratch: Path,
    environment: dict[str, str],
    allowed: tuple[str, ...],
) -> tuple[dict[str, Any], str]:
    """Load selected guidance and expose only copied source inputs to the native job."""
    kind = environment.get("LEANFLOW_NATIVE_WORKFLOW_KIND", "")
    if kind != "formalize":
        raise ValueError("Native formalization context requires the formalize workflow")
    context = {
        "workflow": kind,
        "active_file": environment.get("LEANFLOW_NATIVE_ACTIVE_FILE", ""),
        "project_root": str(snapshot),
        "prompt": environment.get("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", ""),
        "requested_target": environment.get("LEANFLOW_NATIVE_REQUESTED_TARGET", ""),
        "allowed_axioms": list(allowed),
    }
    prompt = _CONTRACT.replace("{workflow}", kind) + "\n" + json.dumps(context, ensure_ascii=False)
    prompt += "\n" + _FORMALIZATION_CONTRACT
    selected = [environment.get("LEANFLOW_NATIVE_ACTIVE_SKILL", "")]
    selected.extend(environment.get("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", "").split(os.pathsep))
    for name in dict.fromkeys(selected):
        if name:
            skill = load_skill(name, cwd=root)
            if skill:
                prompt += (
                    "\nWorkflow guidance (use native tools and LeanFlow MCP equivalents):\n"
                    + str(skill["content"])
                )
    replacements: dict[str, str] = {}
    texts: dict[str, str] = {}
    for field in ("CONTEXT", "MANIFEST", "EXTRACTED_TEXT"):
        value = environment.get("LEANFLOW_FORMALIZATION_" + field, "")
        if not value:
            continue
        path = Path(value)
        content = path.read_text() if path.is_file() else value
        target = scratch / ("formalization-" + field.lower() + ".txt")
        replacements[value] = str(target)
        texts[field] = content
    if "MANIFEST" in texts:
        texts["MANIFEST"] = _copy_source_partition(root, snapshot, scratch, texts["MANIFEST"])
    for field, content in texts.items():
        for before, after in replacements.items():
            content = content.replace(before, after)
        (scratch / ("formalization-" + field.lower() + ".txt")).write_text(
            remap_project_text(content, root, snapshot)
        )
    if texts:
        if "MANIFEST" in texts:
            context["formalization_manifest"] = str(scratch / "formalization-manifest.txt")
        prompt += "\nSource formalization context:\n" + texts.get("CONTEXT", "")
        prompt += "\nRead the complete extracted source at " + str(
            scratch / "formalization-extracted_text.txt"
        )
    for before, after in replacements.items():
        prompt = prompt.replace(before, after)
    return context, remap_project_text(prompt, root, snapshot)
