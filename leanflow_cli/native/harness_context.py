"""Prepare native workflow instructions and copy formalization inputs into job authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
For review, return your findings without modifying project sources. For draft and
formalize, create elaborating statement skeletons with sorry only in theorem proofs.
Implement definitions and constructions fully. Preserve the supplied mathematical
scope and source mappings in Blueprint.md. An independent fresh reviewer checks
source fidelity before promotion. Proving is a separate user-started workflow.
"""


def prepare_context(
    *,
    root: Path,
    snapshot: Path,
    scratch: Path,
    environment: dict[str, str],
    allowed: tuple[str, ...],
) -> tuple[dict[str, Any], str, str]:
    """Load selected guidance and expose only copied source inputs to the native job."""
    kind = environment.get("LEANFLOW_NATIVE_WORKFLOW_KIND", "review")
    context = {
        "workflow": kind,
        "active_file": environment.get("LEANFLOW_NATIVE_ACTIVE_FILE", ""),
        "project_root": str(snapshot),
        "prompt": environment.get("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", ""),
        "requested_target": environment.get("LEANFLOW_NATIVE_REQUESTED_TARGET", ""),
        "allowed_axioms": list(allowed),
    }
    prompt = _CONTRACT.replace("{workflow}", kind) + "\n" + json.dumps(context, ensure_ascii=False)
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
    for field, content in texts.items():
        for before, after in replacements.items():
            content = content.replace(before, after)
        (scratch / ("formalization-" + field.lower() + ".txt")).write_text(
            remap_project_text(content, root, snapshot)
        )
    if texts:
        prompt += "\nSource formalization context:\n" + texts.get("CONTEXT", "")
        prompt += "\nRead the complete extracted source at " + str(
            scratch / "formalization-extracted_text.txt"
        )
    for before, after in replacements.items():
        prompt = prompt.replace(before, after)
    source_context = "\n\n".join(f"{name}:\n{text}" for name, text in texts.items())
    return context, remap_project_text(prompt, root, snapshot), source_context
