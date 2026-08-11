#!/usr/bin/env python3
"""Verified-patch application tool for LeanFlow.

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
reaches its collaborators directly via ``leanflow_cli.runtime.file_locks`` /
``leanflow_cli.lean.lean_services`` / ``leanflow_cli.workflows.workflow_state`` /
``tools.implementations.file_operations`` / ``tools.utilities.patch_parser``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from core import verified_edit_authority
from core.runtime_modes import scratch_only_dispatch_worker_enabled
from leanflow_cli.lean.lean_incremental import lean_incremental_check
from leanflow_cli.lean.lean_services import lean_verify
from leanflow_cli.runtime.file_locks import ensure_file_lock, release_file_lock
from leanflow_cli.workflows.workflow_state import (
    append_workflow_outcome,
    save_verified_patch_status,
    write_verified_patch_checkpoint,
)
from tools.implementations.file_operations import ShellFileOperations
from tools.utilities.patch_parser import OperationType, parse_v4a_patch
from tools.utilities.read_freshness import note_write


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
    check_mode: str = "incremental",
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
        "patch_elaborated": False,
        "target_verified": False,
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
    normalized = str(check_mode or "incremental").strip().lower().replace("-", "_")
    aliases = {
        "lean_file": "file_exact",
        "file": "file_exact",
        "fast": "incremental",
        "incremental": "incremental",
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


def _source_revision_sha256(content: bytes) -> str:
    """Return the exact source-byte digest used to bind verification evidence."""
    return hashlib.sha256(content).hexdigest()


def _restore_unverified_source(
    path: Path,
    *,
    before_exists: bool,
    before_content: bytes,
) -> tuple[bool, str]:
    """Atomically restore the exact pre-patch source revision."""
    try:
        if not before_exists:
            path.unlink(missing_ok=True)
            return True, ""
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.rollback-",
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(before_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except BaseException as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:240]}"
    return True, ""


def apply_verified_patch_tool(
    path: str,
    patch: str,
    *,
    cwd: str = "",
    check_mode: str = "incremental",
    theorem_id: str = "",
    owner_id: str = "",
    task_id: str = "default",
    timeout_s: int = 300,
    verified_edit_authority_token: str = "",
) -> str:
    """Apply a one-file Lean patch and immediately verify the touched scope."""
    raw_path = str(path or "").strip()
    raw_patch = str(patch or "")
    normalized_check = _normalize_verified_patch_check_mode(check_mode)
    if scratch_only_dispatch_worker_enabled():
        # Do not call _verified_patch_failure here: it intentionally persists
        # the latest authoritative patch status, which a research job must
        # never replace even when its write request is rejected.
        return json.dumps(
            {
                "success": False,
                "status": "scratch_only_write_denied",
                "path": raw_path,
                "cwd": cwd,
                "check_mode": normalized_check,
                "patch_applied": False,
                "check_passed": False,
                "patch_elaborated": False,
                "target_verified": False,
                "verified": False,
                "message": (
                    "Scratch-only research jobs cannot edit project files. "
                    "Use lean_incremental_check with an inline replacement."
                ),
            },
            ensure_ascii=False,
        )
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
    if normalized_check not in {"incremental", "file_exact", "module", "project"}:
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
    before_bytes = b""
    before_exists = resolved_path.exists()
    if resolved_path.exists():
        before_bytes = resolved_path.read_bytes()
        before_content = before_bytes.decode("utf-8")

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
        # Verified edits are high-risk transactions bound to the caller's exact
        # source image.  Fuzzy relocation can silently replace stale trailing
        # context (including a neighboring declaration) and then verify a
        # different edit than the model requested.  Structural freshness must
        # therefore be resolved by a reread and a new patch, never by guessing.
        patch_result = file_ops.patch_v4a(raw_patch, strict=True)
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

    try:
        verification_source_bytes = resolved_path.read_bytes()
    except OSError:
        verification_source_bytes = b""
    verification_source_revision_sha256 = _source_revision_sha256(verification_source_bytes)
    cached_authorization = verified_edit_authority.consume(
        verified_edit_authority_token,
        path=str(resolved_path),
        theorem_id=theorem_id,
        before_sha256=_source_revision_sha256(before_bytes),
        after_sha256=verification_source_revision_sha256,
    )
    if cached_authorization is not None:
        verification = {
            "success": True,
            "ok": True,
            "backend": "authenticated_parent_helper_reuse",
            "tool": "lean_incremental_check",
            "action": "check_helper",
            "file": str(resolved_path),
            "target": cached_authorization.verified_declaration,
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "timed_out": False,
            "error_code": "",
            "error": "",
            "output": (
                "reused exact parent LeanProbe verification for the authenticated "
                "helper insertion image"
            ),
            "messages": [],
            "verification_scope": "authenticated_helper_insertion",
            "axiom_profile_checked": True,
            "axiom_profile_axioms": list(cached_authorization.axiom_profile_axioms),
            "axiom_profile_blockers": [],
            "cache": {
                "cache_hit": True,
                "kind": "exact_parent_helper_insertion",
            },
        }
    elif normalized_check == "incremental":
        verification = lean_incremental_check(
            action="check_file",
            file_path=str(resolved_path),
            cwd=str(base_cwd),
            timeout_s=max(1, int(timeout_s or 300)),
        )
    else:
        verification = lean_verify(
            target=str(resolved_path), cwd=str(base_cwd), mode=normalized_check
        ).to_dict()
    try:
        post_verification_bytes = resolved_path.read_bytes()
    except OSError:
        post_verification_bytes = b""
    verification_source_unchanged = bool(
        resolved_path.exists() and post_verification_bytes == verification_source_bytes
    )
    if cached_authorization is not None:
        check_passed = True
    elif normalized_check == "incremental":
        check_passed = bool(verification.get("success")) and not bool(
            verification.get("has_errors")
        )
        check_passed = check_passed and not bool(verification.get("timed_out"))
    else:
        check_passed = bool(verification.get("ok"))
    check_passed = check_passed and verification_source_unchanged
    rolled_back = False
    rollback_error = ""
    if not check_passed and verification_source_unchanged:
        rolled_back, rollback_error = _restore_unverified_source(
            resolved_path,
            before_exists=before_exists,
            before_content=before_bytes,
        )
    status = (
        "patch_elaborated"
        if check_passed
        else ("check_failed" if not rollback_error else "rollback_failed")
    )
    payload = {
        "success": check_passed,
        "status": status,
        "path": str(resolved_path),
        "cwd": str(base_cwd),
        "theorem_id": str(theorem_id or ""),
        "check_mode": normalized_check,
        "patch_applied": bool(check_passed or not rolled_back),
        "patch_applied_before_rollback": True,
        "rolled_back": rolled_back,
        "rollback_error": rollback_error,
        "check_passed": check_passed,
        # This tool checks a broad file/module/project scope.  It does not run
        # the queue manager's declaration-identity and axiom-profile gate, so
        # a helper-only edit must never be presented as proof of the assigned
        # theorem.  The native runner performs that exact gate immediately
        # after this broad check succeeds.
        "patch_elaborated": check_passed,
        "target_verified": False,
        "verified": False,
        "checkpoint_id": checkpoint.get("checkpoint_id", ""),
        "checkpoint": checkpoint,
        "patch": patch_payload,
        "changed_ranges": _diff_hunk_headers(str(patch_payload.get("diff", "") or "")),
        "verification": verification,
        "verified_source_revision_sha256": verification_source_revision_sha256,
        "verification_source_unchanged": verification_source_unchanged,
        "authenticated_helper_insertion": cached_authorization is not None,
        "broad_verification_skipped": cached_authorization is not None,
        "message": (
            (
                "Patch applied by reusing an exact parent LeanProbe helper check; "
                "the assigned theorem remains unresolved."
            )
            if cached_authorization is not None
            else (
                "Patch applied and its broad verification check passed; the exact target gate is still required."
                if check_passed
                else (
                    "Patch applied and Lean returned successfully, but the source changed during verification. Run a fresh exact check on the current revision."
                    if not verification_source_unchanged
                    else (
                        "Patch verification failed and the exact pre-patch source revision was restored. Repair the candidate from the returned diagnostics before applying it again."
                        if rolled_back
                        else "Patch verification failed, and restoring the pre-patch source revision failed. Stop source edits until the rollback error is resolved."
                    )
                )
            )
        ),
    }
    if check_passed or rolled_back:
        # The verified-patch transaction writes outside file_tools, but it is
        # still part of the same model tool session. Refresh that session's
        # read-before-edit image after either a committed patch or an exact
        # rollback so the next edit is not rejected as stale solely because of
        # LeanFlow's own managed write.
        try:
            current_content = resolved_path.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            note_write(task_id, str(resolved_path), current_content)
    save_verified_patch_status(payload)
    append_workflow_outcome("apply-verified-patch", payload)
    return json.dumps(payload, ensure_ascii=False)
