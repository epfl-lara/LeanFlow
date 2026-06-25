#!/usr/bin/env python3
"""Verified-patch application tool for EPFLemma.

This module holds the ``apply_verified_patch`` tool that was split out of
``tools/lean_tool.py``: ``apply_verified_patch_tool`` applies a one-file V4A Lean
patch and immediately verifies the touched scope, together with the patch
path/diff/verification-gate helpers it uses (``_LocalShellEnv``,
``_resolve_tool_path``, ``_verified_patch_failure``,
``_normalize_verified_patch_check_mode``, ``_patch_operation_paths``,
``_diff_hunk_headers`` and ``_patch_error_is_no_change``).

``tools.implementations.lean_tool`` re-exports ``apply_verified_patch_tool`` so callers and the
tool registry keep resolving it as ``lean_tool.apply_verified_patch_tool``. This
module must NOT import ``tools.implementations.lean_tool`` (it would create an import cycle); it
reaches its collaborators directly via ``epflemma_cli.runtime.file_locks`` /
``epflemma_cli.lean.lean_services`` / ``epflemma_cli.workflows.workflow_state`` /
``tools.implementations.file_operations`` / ``tools.utilities.patch_parser``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from epflemma_cli.lean.lean_services import lean_verify
from epflemma_cli.runtime.file_locks import ensure_file_lock, release_file_lock
from epflemma_cli.workflows.workflow_state import (
    append_workflow_outcome,
    save_verified_patch_status,
    write_verified_patch_checkpoint,
)
from tools.implementations.file_operations import ShellFileOperations
from tools.utilities.patch_parser import OperationType, parse_v4a_patch


class _LocalShellEnv:
    def __init__(self, cwd: Path):
        self.cwd = str(cwd)

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        # shell=True is intentional: ShellFileOperations builds commands that use shell
        # features (redirects/pipes/&&) and pre-escapes all interpolated paths via
        # _escape_shell_arg, so this is the generic executor, not a raw user string.
        completed = subprocess.run(
            command,
            shell=True,  # noqa: S602
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
        return _verified_patch_failure(
            "invalid_request", "path required.", check_mode=normalized_check
        )
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

    base_cwd = (
        Path(str(cwd or "")).expanduser().resolve()
        if str(cwd or "").strip()
        else Path.cwd().resolve()
    )
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
    lock_result = ensure_file_lock(
        str(resolved_path), owner_id=lock_owner, purpose="apply_verified_patch"
    )
    if not lock_result.get("success"):
        return _verified_patch_failure(
            "lock_conflict",
            str(lock_result.get("error", "file is locked")),
            path=str(resolved_path),
            cwd=str(base_cwd),
            check_mode=normalized_check,
            checkpoint=checkpoint,
            lock=(
                lock_result.get("lock")
                if isinstance(lock_result.get("lock"), dict)
                else lock_result
            ),
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
        if (
            _patch_error_is_no_change(patch_error)
            and before_exists == after_exists
            and before_content == after_content
        ):
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

    verification = lean_verify(
        target=str(resolved_path), cwd=str(base_cwd), mode=normalized_check
    ).to_dict()
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
