"""Drive warm LeanProbe feedback under the controller's sole execution deadline."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def check(module: Any, project: Path, file: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Check exact source without legacy cold-start floors or per-command ceilings.

    The isolated parent worker enforces cancellation and the campaign deadline
    over startup, import replay and elaboration together. LeanInteract accepts
    None as an unlimited wait, so no large synthetic timeout is necessary.
    """
    source = file.read_text(encoding="utf-8")
    target = str(request.get("declaration") or "")
    replacement = str(request.get("replacement") or "")
    action = "check_target" if target else "check_file"
    if not target:
        _, segments = module._segment_file(source)
        target = str(getattr(segments[-1], "name", "") or "") if segments else ""
    query = None
    if request.get("include_axiom_profile") is True:
        query = module._inline_axiom_query_for_target(
            source, theorem_id=target, replacement=replacement
        )
        if query is None:
            return {
                "success": False,
                "ok": False,
                "error_code": "inline_axiom_query_unavailable",
                "error": "Cannot identify the exact declaration for axiom inspection",
            }
        replacement = query.source
    probe = module._probe()
    if target:
        payload = probe.check_target(
            str(file),
            theorem_id=target,
            cwd=str(project),
            replacement=replacement,
            include_tactics=True,
            timeout_s=None,
        )
    else:
        payload = probe.prepare_file(str(file), theorem_id="", cwd=str(project), timeout_s=None)
    result = dict(module._normalize_payload(payload, action))
    if result.get("error_code") == "header_failed":
        # LeanProbe 0.3 drops the Lean messages from failed header warmup.
        # A standalone diagnostic replay stays inside this same isolated worker
        # and global deadline; it cannot turn the failed target check into success.
        diagnostics = probe.check_code(source, cwd=str(project), timeout_s=None)
        result["header_diagnostics"] = dict(module._normalize_payload(diagnostics, "check_code"))
    if query is not None:
        result = dict(module._attach_inline_axiom_profile(result, query))
    return result
