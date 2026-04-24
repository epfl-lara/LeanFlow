#!/usr/bin/env python3
"""Native Lean workflow tools for EPFLemma."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from agent.auxiliary_client import call_llm
from epflemma_cli.file_locks import ensure_file_lock, release_file_lock
from epflemma_cli.lean_services import (
    LeanWorkerRequest,
    dispatch_worker,
    lean_auto_probe,
    lean_auto_search,
    lean_auto_try,
    lean_axioms,
    lean_inspect,
    lean_multi_attempt,
    lean_proof_context,
    lean_search,
    lean_sorries,
    lean_verify,
    probe_capabilities,
)
from epflemma_cli.workflow_state import (
    append_workflow_outcome,
    save_verified_patch_status,
    write_verified_patch_checkpoint,
)
from tools.file_operations import ShellFileOperations
from tools.patch_parser import OperationType, parse_v4a_patch
from tools.registry import registry


def check_lean_requirements() -> bool:
    return True


def lean_capabilities(cwd: str = "") -> str:
    return json.dumps(
        {
            "success": True,
            **probe_capabilities(cwd or None).to_dict(),
        },
        ensure_ascii=False,
    )


def lean_inspect_tool(target: str, cwd: str = "", line: int | None = None, symbol: str = "") -> str:
    return json.dumps(
        {
            "success": True,
            **lean_inspect(target, cwd=cwd or None, line=line, symbol=symbol or None).to_dict(),
        },
        ensure_ascii=False,
    )


def lean_verify_tool(target: str = "", cwd: str = "", mode: str = "project") -> str:
    return json.dumps(
        {
            "success": True,
            **lean_verify(target=target, cwd=cwd or None, mode=mode).to_dict(),
        },
        ensure_ascii=False,
    )


def lean_search_tool(query: str, cwd: str = "", mode: str = "auto", limit: int = 10, file_path: str = "") -> str:
    result = lean_search(query, cwd=cwd or None, mode=mode, limit=limit, file_path=file_path)
    payload = {
        "success": True,
        **result.to_dict(),
    }
    if (
        not result.results
        and "repeated empty search loop detected; stop searching and change tactic" in result.degraded_reasons
    ):
        payload["success"] = False
        payload["action_required"] = (
            "Stop searching in this turn and either make the strongest concrete proof/edit attempt, "
            "run verification, dispatch a worker, or report a blocker."
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def lean_sorries_tool(scope: str = "project", target: str = "", cwd: str = "") -> str:
    findings = [item.to_dict() for item in lean_sorries(scope=scope, target=target, cwd=cwd or None)]
    return json.dumps(
        {
            "success": True,
            "scope": scope,
            "target": target,
            "count": len(findings),
            "findings": findings,
        },
        ensure_ascii=False,
    )


def lean_axioms_tool(target: str, cwd: str = "", file_path: str = "") -> str:
    return json.dumps(
        {
            "success": True,
            **lean_axioms(target, cwd=cwd or None, file_path=file_path).to_dict(),
        },
        ensure_ascii=False,
    )


def lean_worker_dispatch_tool(
    worker: str,
    goal: str,
    *,
    context: str = "",
    file_path: str = "",
    line: int | None = None,
    allow_delegation: bool = False,
    use_file_lock: bool = True,
    parent_agent=None,
    owner_id: str = "",
) -> str:
    result = dispatch_worker(
        LeanWorkerRequest(
            worker=worker,
            goal=goal,
            context=context,
            file_path=file_path,
            line=line,
            use_file_lock=use_file_lock,
            allow_delegation=allow_delegation,
        ),
        parent_agent=parent_agent,
        owner_id=owner_id,
    )
    return json.dumps({"success": True, **result.to_dict()}, ensure_ascii=False)


def lean_proof_context_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    include_similar_proofs: bool = True,
    similarity_threshold: float = 0.7,
) -> str:
    return json.dumps(
        lean_proof_context(
            file_path,
            theorem_id,
            cwd=cwd or None,
            include_similar_proofs=include_similar_proofs,
            similarity_threshold=similarity_threshold,
        ),
        ensure_ascii=False,
    )


def lean_multi_attempt_tool(
    file_path: str,
    line: int,
    attempts: list[str],
    *,
    cwd: str = "",
    column: int | None = None,
) -> str:
    return json.dumps(
        lean_multi_attempt(
            file_path,
            line,
            attempts,
            cwd=cwd or None,
            column=column,
        ),
        ensure_ascii=False,
    )


def lean_auto_probe_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    methods: list[str] | None = None,
    timeout_s: int = 10,
) -> str:
    return json.dumps(
        lean_auto_probe(
            file_path,
            theorem_id,
            cwd=cwd or None,
            methods=methods,
            timeout_s=timeout_s,
        ),
        ensure_ascii=False,
    )


def lean_auto_search_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    timeout_s: int = 10,
    objective: str = "balanced",
) -> str:
    return json.dumps(
        lean_auto_search(
            file_path,
            theorem_id,
            cwd=cwd or None,
            timeout_s=timeout_s,
            objective=objective,
        ),
        ensure_ascii=False,
    )


def lean_auto_try_tool(
    file_path: str,
    theorem_id: str,
    proof_attempt: str,
    *,
    cwd: str = "",
    timeout_s: int = 10,
) -> str:
    return json.dumps(
        lean_auto_try(
            file_path,
            theorem_id,
            proof_attempt,
            cwd=cwd or None,
            timeout_s=timeout_s,
        ),
        ensure_ascii=False,
    )


class _LocalShellEnv:
    def __init__(self, cwd: Path):
        self.cwd = str(cwd)

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd or self.cwd,
            input=stdin_data,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {"output": completed.stdout, "returncode": completed.returncode}


def _resolve_tool_path(path: str, cwd: str = "") -> Path:
    raw = Path(str(path or "").strip()).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    base = Path(str(cwd or "")).expanduser() if str(cwd or "").strip() else Path.cwd()
    return (base / raw).resolve()


def _verified_patch_failure(
    status: str,
    message: str,
    *,
    path: str = "",
    cwd: str = "",
    check_mode: str = "file_exact",
    checkpoint: dict | None = None,
    patch_applied: bool = False,
    patch: dict | None = None,
    changed_ranges: list[str] | None = None,
    verification: dict | None = None,
    lock: dict | None = None,
) -> str:
    payload = {
        "success": False,
        "status": status,
        "path": path,
        "cwd": cwd,
        "check_mode": check_mode,
        "patch_applied": patch_applied,
        "check_passed": False,
        "verified": False,
        "message": message,
    }
    if checkpoint:
        payload["checkpoint_id"] = checkpoint.get("checkpoint_id", "")
        payload["checkpoint"] = checkpoint
    if patch:
        payload["patch"] = patch
    if changed_ranges is not None:
        payload["changed_ranges"] = changed_ranges
    if verification:
        payload["verification"] = verification
    if lock:
        payload["lock"] = lock
    save_verified_patch_status(payload)
    append_workflow_outcome("apply-verified-patch", payload)
    return json.dumps(payload, ensure_ascii=False)


def _normalize_verified_patch_check_mode(check_mode: str) -> str:
    normalized = str(check_mode or "file_exact").strip().lower().replace("-", "_")
    aliases = {
        "lean_file": "file_exact",
        "file": "file_exact",
        "fast": "file_exact",
        "file_exact": "file_exact",
        "module": "module",
        "medium": "module",
        "project": "project",
        "strict": "project",
    }
    return aliases.get(normalized, normalized)


def _patch_operation_paths(patch: str, *, cwd: str) -> tuple[list[Path], str]:
    operations, error = parse_v4a_patch(patch)
    if error:
        return [], error
    if len(operations) != 1:
        return [], "apply_verified_patch expects exactly one V4A file operation."

    op = operations[0]
    if op.operation not in {OperationType.ADD, OperationType.UPDATE}:
        return [], "apply_verified_patch only supports adding or updating one Lean file."

    paths = [_resolve_tool_path(op.file_path, cwd)]
    if op.new_path:
        paths.append(_resolve_tool_path(op.new_path, cwd))
    return paths, ""


def _diff_hunk_headers(diff: str) -> list[str]:
    return [line for line in str(diff or "").splitlines() if line.startswith("@@")]


def _patch_error_is_no_change(error: str) -> bool:
    lowered = str(error or "").lower()
    return bool(
        "old_string and new_string are identical" in lowered
        or "no changes" in lowered
        or "unchanged" in lowered
        or "empty patch" in lowered
    )


def apply_verified_patch_tool(
    path: str,
    patch: str,
    *,
    cwd: str = "",
    check_mode: str = "file_exact",
    theorem_id: str = "",
    owner_id: str = "",
    task_id: str = "default",
) -> str:
    """Apply a one-file Lean patch and immediately verify the touched scope."""
    del task_id
    raw_path = str(path or "").strip()
    raw_patch = str(patch or "")
    normalized_check = _normalize_verified_patch_check_mode(check_mode)
    if not raw_path:
        return _verified_patch_failure("invalid_request", "path required.", check_mode=normalized_check)
    if not raw_patch.strip():
        return _verified_patch_failure(
            "invalid_request",
            "patch content required.",
            path=raw_path,
            cwd=cwd,
            check_mode=normalized_check,
        )
    if normalized_check not in {"file_exact", "module", "project"}:
        return _verified_patch_failure(
            "invalid_request",
            f"unsupported check_mode: {check_mode}",
            path=raw_path,
            cwd=cwd,
            check_mode=normalized_check,
        )

    resolved_path = _resolve_tool_path(raw_path, cwd)
    if resolved_path.suffix != ".lean":
        return _verified_patch_failure(
            "invalid_request",
            "apply_verified_patch only edits .lean files.",
            path=str(resolved_path),
            cwd=cwd,
            check_mode=normalized_check,
        )

    patch_paths, patch_error = _patch_operation_paths(raw_patch, cwd=cwd)
    if patch_error:
        return _verified_patch_failure(
            "patch_failed",
            patch_error,
            path=str(resolved_path),
            cwd=cwd,
            check_mode=normalized_check,
        )
    if len(patch_paths) != 1 or patch_paths[0] != resolved_path:
        return _verified_patch_failure(
            "patch_failed",
            "patch must add or update exactly the requested path.",
            path=str(resolved_path),
            cwd=cwd,
            check_mode=normalized_check,
        )

    base_cwd = Path(str(cwd or "")).expanduser().resolve() if str(cwd or "").strip() else Path.cwd().resolve()
    before_content = ""
    before_exists = resolved_path.exists()
    if resolved_path.exists():
        before_content = resolved_path.read_text(encoding="utf-8")

    checkpoint = write_verified_patch_checkpoint(
        file_path=str(resolved_path),
        cwd=str(base_cwd),
        before_content=before_content,
        patch=raw_patch,
        check_mode=normalized_check,
        theorem_id=theorem_id,
    )

    lock_owner = str(owner_id or "").strip()
    temporary_lock_owner = ""
    if not lock_owner:
        temporary_lock_owner = f"apply_verified_patch:{os.getpid()}"
        lock_owner = temporary_lock_owner
    lock_result = ensure_file_lock(str(resolved_path), owner_id=lock_owner, purpose="apply_verified_patch")
    if not lock_result.get("success"):
        return _verified_patch_failure(
            "lock_conflict",
            str(lock_result.get("error", "file is locked")),
            path=str(resolved_path),
            cwd=str(base_cwd),
            check_mode=normalized_check,
            checkpoint=checkpoint,
            lock=lock_result.get("lock") if isinstance(lock_result.get("lock"), dict) else lock_result,
        )

    try:
        file_ops = ShellFileOperations(_LocalShellEnv(base_cwd), cwd=str(base_cwd))
        patch_result = file_ops.patch_v4a(raw_patch)
    finally:
        if temporary_lock_owner:
            release_file_lock(str(resolved_path), owner_id=temporary_lock_owner)

    patch_payload = patch_result.to_dict()
    if not patch_result.success:
        patch_error = str(patch_result.error or "patch did not apply")
        after_exists = resolved_path.exists()
        after_content = resolved_path.read_text(encoding="utf-8") if after_exists else ""
        if _patch_error_is_no_change(patch_error) and before_exists == after_exists and before_content == after_content:
            return _verified_patch_failure(
                "no_changes",
                "Patch did not change the file; the file content is unchanged. Submit a patch that makes a real edit before verification.",
                path=str(resolved_path),
                cwd=str(base_cwd),
                check_mode=normalized_check,
                checkpoint=checkpoint,
                patch_applied=False,
                patch=patch_payload,
                changed_ranges=[],
                verification=None,
            )
        return _verified_patch_failure(
            "patch_failed",
            patch_error,
            path=str(resolved_path),
            cwd=str(base_cwd),
            check_mode=normalized_check,
            checkpoint=checkpoint,
            verification=None,
        )

    after_exists = resolved_path.exists()
    after_content = resolved_path.read_text(encoding="utf-8") if after_exists else ""
    if before_exists == after_exists and before_content == after_content:
        return _verified_patch_failure(
            "no_changes",
            "Patch applied but the file content is unchanged. Submit a patch that makes a real edit before verification.",
            path=str(resolved_path),
            cwd=str(base_cwd),
            check_mode=normalized_check,
            checkpoint=checkpoint,
            patch_applied=False,
            patch=patch_payload,
            changed_ranges=[],
            verification=None,
        )

    verification = lean_verify(target=str(resolved_path), cwd=str(base_cwd), mode=normalized_check).to_dict()
    verified = bool(verification.get("ok"))
    status = "verified" if verified else "check_failed"
    payload = {
        "success": verified,
        "status": status,
        "path": str(resolved_path),
        "cwd": str(base_cwd),
        "theorem_id": str(theorem_id or ""),
        "check_mode": normalized_check,
        "patch_applied": True,
        "check_passed": verified,
        "verified": verified,
        "checkpoint_id": checkpoint.get("checkpoint_id", ""),
        "checkpoint": checkpoint,
        "patch": patch_payload,
        "changed_ranges": _diff_hunk_headers(str(patch_payload.get("diff", "") or "")),
        "verification": verification,
        "message": (
            "Patch applied and verification passed."
            if verified
            else "Patch applied, but verification failed. Continue repair from the returned diagnostics."
        ),
    }
    save_verified_patch_status(payload)
    append_workflow_outcome("apply-verified-patch", payload)
    return json.dumps(payload, ensure_ascii=False)


def _advisor_failure(status: str, message: str, *, theorem_id: str = "", file_path: str = "") -> str:
    return json.dumps(
        {
            "success": False,
            "status": status,
            "theorem_id": theorem_id,
            "file_path": file_path,
            "message": (
                "lean_reasoning_help is not working for this request: "
                f"{message} Continue with the main proof workflow; do not treat missing advisor "
                "advice as evidence that the theorem statement should change."
            ),
        },
        ensure_ascii=False,
    )


def lean_reasoning_help_tool(
    theorem_id: str,
    file_path: str,
    *,
    theorem_statement: str = "",
    current_diagnostics: str = "",
    current_goals: str = "",
    current_attempt: str = "",
    recent_failed_attempts: str = "",
    question: str = "",
    cwd: str = "",
    timeout_s: int = 45,
) -> str:
    """Ask the configured auxiliary theorem advisor for proof-strategy advice."""
    theorem_id = str(theorem_id or "").strip()
    file_path = str(file_path or "").strip()
    if not theorem_id:
        return _advisor_failure("invalid_request", "missing theorem_id.", file_path=file_path)
    if not file_path:
        return _advisor_failure("invalid_request", "missing file_path.", theorem_id=theorem_id)

    system_prompt = (
        "You are an auxiliary Lean proof-strategy advisor for EPFLemma. "
        "You do not edit files and you do not decide success. Give concrete proof ideas, "
        "search terms, likely lemmas, and tactic sketches for the assigned theorem only. "
        "The existing theorem/lemma/example statement must be preserved exactly; if the "
        "statement appears wrong or too hard, say that as a blocker rather than proposing "
        "a changed statement. Do not suggest replacing the proof with sorry."
    )
    user_prompt = "\n\n".join(
        part
        for part in [
            f"File: {file_path}",
            f"Theorem: {theorem_id}",
            f"Working directory: {cwd}" if cwd else "",
            f"Theorem statement:\n{theorem_statement}" if theorem_statement else "",
            f"Current diagnostics:\n{current_diagnostics}" if current_diagnostics else "",
            f"Current goals:\n{current_goals}" if current_goals else "",
            f"Current attempt:\n{current_attempt}" if current_attempt else "",
            f"Recent failed attempts:\n{recent_failed_attempts}" if recent_failed_attempts else "",
            f"Question:\n{question}" if question else "Question:\nSuggest the next strongest proof strategy.",
        ]
        if part
    )

    try:
        response = call_llm(
            task="lean_reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=3000,
            timeout=max(5, int(timeout_s or 45)),
        )
    except RuntimeError as exc:
        return _advisor_failure("unavailable", str(exc), theorem_id=theorem_id, file_path=file_path)
    except Exception as exc:
        return _advisor_failure(
            "error",
            f"{type(exc).__name__}: {exc}",
            theorem_id=theorem_id,
            file_path=file_path,
        )

    try:
        advice = str(response.choices[0].message.content or "").strip()
    except Exception:
        advice = ""
    if not advice:
        return _advisor_failure(
            "no_answer",
            "the auxiliary advisor returned no content.",
            theorem_id=theorem_id,
            file_path=file_path,
        )

    return json.dumps(
        {
            "success": True,
            "status": "answered",
            "theorem_id": theorem_id,
            "file_path": file_path,
            "model": str(getattr(response, "model", "") or ""),
            "advice": advice,
            "next_step": "Use this as advice only; apply a concrete proof edit and verify with lean_verify(mode=file_exact).",
        },
        ensure_ascii=False,
    )


LEAN_CAPABILITIES_SCHEMA = {
    "name": "lean_capabilities",
    "description": "Inspect the native EPFLemma Lean workflow capability surface: project detection, Lean/Lake/Elan binaries, MCP/LSP tool availability, search providers, helper availability, workers, and degraded-mode reasons.",
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Optional working directory to probe"},
        },
    },
}

LEAN_INSPECT_SCHEMA = {
    "name": "lean_inspect",
    "description": "Return structured Lean state for a target file: diagnostics, goals, sorry counts, blocker kind, queue candidates, and capability snapshot.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Lean file path to inspect"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "line": {"type": "integer", "description": "Optional target line for goals lookup"},
            "symbol": {"type": "string", "description": "Optional declaration name for goals lookup"},
        },
        "required": ["target"],
    },
}

LEAN_VERIFY_SCHEMA = {
    "name": "lean_verify",
    "description": "Run a canonical Lean verification step for a file, module, or the whole project. Use `mode=file_exact` for file-scoped theorem acceptance checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Optional file target for file/module verification"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "mode": {
                "type": "string",
                "description": "Verification mode: `file_exact`, `module`, or `project`",
                "default": "project",
            },
        },
    },
}

LEAN_SEARCH_SCHEMA = {
    "name": "lean_search",
    "description": "Search for Lean declarations and proof hints using MCP/LSP providers first, then native rg/mathlib fallbacks. Returns provider provenance with each result. If degraded reasons report a repeated empty search loop, stop searching and change tactic.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query or type pattern"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "mode": {
                "type": "string",
                "description": "Search mode: `auto`, `local`, `semantic`, `type-pattern`, or `natural-language`",
                "default": "auto",
            },
            "limit": {"type": "integer", "description": "Maximum number of results", "default": 10},
            "file_path": {"type": "string", "description": "Optional active file path for provider-specific search"},
        },
        "required": ["query"],
    },
}

LEAN_SORRIES_SCHEMA = {
    "name": "lean_sorries",
    "description": "List `sorry` findings across the project or a single file, including declaration names and line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "description": "`project` or `file`", "default": "project"},
            "target": {"type": "string", "description": "File path when scope=`file`"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
    },
}

LEAN_AXIOMS_SCHEMA = {
    "name": "lean_axioms",
    "description": "Run a best-effort axiom report for one declaration using a temporary Lean file that imports the target module and issues `#print axioms`.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Declaration name to inspect"},
            "file_path": {"type": "string", "description": "Lean file containing the declaration"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["target"],
    },
}

LEAN_PROOF_CONTEXT_SCHEMA = {
    "name": "lean_proof_context",
    "description": "Fetch theorem-local context from the managed Lean automation backend: theorem statement, original proof, hypotheses, in-scope names, namespace, and optional similar proofs.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_id": {"type": "string", "description": "Declaration name to inspect"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "include_similar_proofs": {"type": "boolean", "default": True},
            "similarity_threshold": {"type": "number", "default": 0.7},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_MULTI_ATTEMPT_SCHEMA = {
    "name": "lean_multi_attempt",
    "description": "Screen 2-6 short concrete tactic attempts at one proof location using the Lean MCP backend. Do not pass full proof blocks or candidates containing `sorry`.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "line": {"type": "integer", "description": "Target line number"},
            "column": {"type": "integer", "description": "Optional target column"},
            "attempts": {"type": "array", "items": {"type": "string"}, "description": "Concrete tactic candidates to test"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "line", "attempts"],
    },
}

LEAN_AUTO_PROBE_SCHEMA = {
    "name": "lean_auto_probe",
    "description": "Probe theorem-local automation methods such as `aesop`, `aesop?`, and `grind` before broader search.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to probe"},
            "methods": {"type": "array", "items": {"type": "string"}, "description": "Automation methods to probe"},
            "timeout_s": {"type": "integer", "default": 10},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_AUTO_SEARCH_SCHEMA = {
    "name": "lean_auto_search",
    "description": "Ask the managed Lean automation backend to search for one theorem-local automated proof candidate after context/probe data exists.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to search"},
            "timeout_s": {"type": "integer", "default": 10},
            "objective": {"type": "string", "default": "balanced"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_AUTO_TRY_SCHEMA = {
    "name": "lean_auto_try",
    "description": "Validate one concrete theorem-local automated proof attempt before patching it into the file.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to test"},
            "proof_attempt": {"type": "string", "description": "Concrete proof candidate to validate"},
            "timeout_s": {"type": "integer", "default": 10},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id", "proof_attempt"],
    },
}

APPLY_VERIFIED_PATCH_SCHEMA = {
    "name": "apply_verified_patch",
    "description": (
        "Apply one V4A patch to a single .lean file, persist a pre-edit checkpoint, "
        "then immediately run Lean verification. Prefer this for Lean proof/formalization edits "
        "because patched is not considered verified until the check passes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The single .lean file to add or update"},
            "patch": {"type": "string", "description": "A V4A patch with exactly one Add File or Update File operation for path"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "check_mode": {
                "type": "string",
                "description": "Verification tier: file_exact/lean-file/fast, module/medium, or project/strict",
                "default": "file_exact",
            },
            "theorem_id": {"type": "string", "description": "Optional active theorem/declaration id for workflow state"},
        },
        "required": ["path", "patch"],
    },
}

LEAN_WORKER_DISPATCH_SCHEMA = {
    "name": "lean_worker_dispatch",
    "description": "Dispatch or describe a native Lean specialist worker such as `proof-repair`, `proof-golfer`, `axiom-eliminator`, or `sorry-filler-deep`. When delegation is unavailable, returns a structured worker plan instead of failing.",
    "parameters": {
        "type": "object",
        "properties": {
            "worker": {"type": "string", "description": "Worker preset to use"},
            "goal": {"type": "string", "description": "Concrete worker objective"},
            "context": {"type": "string", "description": "Additional worker context"},
            "file_path": {"type": "string", "description": "Lean file the worker should focus on"},
            "line": {"type": "integer", "description": "Optional target line"},
            "allow_delegation": {
                "type": "boolean",
                "description": "Allow subagent delegation when parent-agent context exists",
                "default": False,
            },
            "use_file_lock": {
                "type": "boolean",
                "description": "Reserve the file before dispatching when owner context exists",
                "default": True,
            },
        },
        "required": ["worker", "goal"],
    },
}

LEAN_REASONING_HELP_SCHEMA = {
    "name": "lean_reasoning_help",
    "description": (
        "Ask the configured auxiliary theorem advisor for proof-strategy advice on a hard Lean theorem. "
        "Use after repeated focused attempts or search/automation exhaustion. The advisor only gives advice; "
        "you must still preserve the theorem statement and verify any edit with `lean_verify(mode=file_exact)`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "theorem_id": {"type": "string", "description": "Assigned theorem/lemma/example identifier"},
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_statement": {"type": "string", "description": "Exact current declaration statement, if available"},
            "current_diagnostics": {"type": "string", "description": "Current Lean diagnostics or blocker text"},
            "current_goals": {"type": "string", "description": "Current Lean goals, if available"},
            "current_attempt": {"type": "string", "description": "Most recent proof attempt or edit idea"},
            "recent_failed_attempts": {"type": "string", "description": "Summary of prior failed attempts and errors"},
            "question": {"type": "string", "description": "Specific advice request for the auxiliary model"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "timeout_s": {"type": "integer", "description": "Advisor request timeout in seconds", "default": 45},
        },
        "required": ["theorem_id", "file_path"],
    },
}


registry.register(
    name="lean_capabilities",
    toolset="lean",
    schema=LEAN_CAPABILITIES_SCHEMA,
    handler=lambda args, **kw: lean_capabilities(cwd=args.get("cwd", "")),
    check_fn=check_lean_requirements,
    emoji="🧭",
)
registry.register(
    name="lean_inspect",
    toolset="lean",
    schema=LEAN_INSPECT_SCHEMA,
    handler=lambda args, **kw: lean_inspect_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        line=args.get("line"),
        symbol=args.get("symbol", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🔬",
)
registry.register(
    name="lean_verify",
    toolset="lean",
    schema=LEAN_VERIFY_SCHEMA,
    handler=lambda args, **kw: lean_verify_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        mode=args.get("mode", "project"),
    ),
    check_fn=check_lean_requirements,
    emoji="✅",
)
registry.register(
    name="lean_search",
    toolset="lean",
    schema=LEAN_SEARCH_SCHEMA,
    handler=lambda args, **kw: lean_search_tool(
        query=args.get("query", ""),
        cwd=args.get("cwd", ""),
        mode=args.get("mode", "auto"),
        limit=args.get("limit", 10),
        file_path=args.get("file_path", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🔎",
)
registry.register(
    name="lean_sorries",
    toolset="lean",
    schema=LEAN_SORRIES_SCHEMA,
    handler=lambda args, **kw: lean_sorries_tool(
        scope=args.get("scope", "project"),
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="📍",
)
registry.register(
    name="lean_axioms",
    toolset="lean",
    schema=LEAN_AXIOMS_SCHEMA,
    handler=lambda args, **kw: lean_axioms_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        file_path=args.get("file_path", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="📐",
)
registry.register(
    name="lean_proof_context",
    toolset="lean",
    schema=LEAN_PROOF_CONTEXT_SCHEMA,
    handler=lambda args, **kw: lean_proof_context_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        include_similar_proofs=bool(args.get("include_similar_proofs", True)),
        similarity_threshold=float(args.get("similarity_threshold", 0.7)),
    ),
    check_fn=check_lean_requirements,
    emoji="🧾",
)
registry.register(
    name="lean_multi_attempt",
    toolset="lean",
    schema=LEAN_MULTI_ATTEMPT_SCHEMA,
    handler=lambda args, **kw: lean_multi_attempt_tool(
        file_path=args.get("file_path", ""),
        line=int(args.get("line", 1) or 1),
        attempts=list(args.get("attempts", []) or []),
        cwd=args.get("cwd", ""),
        column=args.get("column"),
    ),
    check_fn=check_lean_requirements,
    emoji="🎯",
)
registry.register(
    name="lean_auto_probe",
    toolset="lean",
    schema=LEAN_AUTO_PROBE_SCHEMA,
    handler=lambda args, **kw: lean_auto_probe_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        methods=list(args.get("methods", []) or []) or None,
        timeout_s=int(args.get("timeout_s", 10) or 10),
    ),
    check_fn=check_lean_requirements,
    emoji="🧪",
)
registry.register(
    name="lean_auto_search",
    toolset="lean",
    schema=LEAN_AUTO_SEARCH_SCHEMA,
    handler=lambda args, **kw: lean_auto_search_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 10) or 10),
        objective=args.get("objective", "balanced"),
    ),
    check_fn=check_lean_requirements,
    emoji="🛰️",
)
registry.register(
    name="lean_auto_try",
    toolset="lean",
    schema=LEAN_AUTO_TRY_SCHEMA,
    handler=lambda args, **kw: lean_auto_try_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        proof_attempt=args.get("proof_attempt", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 10) or 10),
    ),
    check_fn=check_lean_requirements,
    emoji="🛠️",
)
registry.register(
    name="apply_verified_patch",
    toolset="lean",
    schema=APPLY_VERIFIED_PATCH_SCHEMA,
    handler=lambda args, **kw: apply_verified_patch_tool(
        path=args.get("path", ""),
        patch=args.get("patch", ""),
        cwd=args.get("cwd", ""),
        check_mode=args.get("check_mode", "file_exact"),
        theorem_id=args.get("theorem_id", ""),
        owner_id=str(kw.get("owner_id", "") or ""),
        task_id=str(kw.get("task_id", "") or "default"),
    ),
    check_fn=check_lean_requirements,
    emoji="✅",
)
registry.register(
    name="lean_worker_dispatch",
    toolset="lean",
    schema=LEAN_WORKER_DISPATCH_SCHEMA,
    handler=lambda args, **kw: lean_worker_dispatch_tool(
        worker=args.get("worker", ""),
        goal=args.get("goal", ""),
        context=args.get("context", ""),
        file_path=args.get("file_path", ""),
        line=args.get("line"),
        allow_delegation=bool(args.get("allow_delegation", False)),
        use_file_lock=bool(args.get("use_file_lock", True)),
        parent_agent=kw.get("parent_agent"),
        owner_id=str(kw.get("owner_id", "") or ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🧠",
)
registry.register(
    name="lean_reasoning_help",
    toolset="lean",
    schema=LEAN_REASONING_HELP_SCHEMA,
    handler=lambda args, **kw: lean_reasoning_help_tool(
        theorem_id=args.get("theorem_id", ""),
        file_path=args.get("file_path", ""),
        theorem_statement=args.get("theorem_statement", ""),
        current_diagnostics=args.get("current_diagnostics", ""),
        current_goals=args.get("current_goals", ""),
        current_attempt=args.get("current_attempt", ""),
        recent_failed_attempts=args.get("recent_failed_attempts", ""),
        question=args.get("question", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 45) or 45),
    ),
    check_fn=check_lean_requirements,
    emoji="💡",
)
