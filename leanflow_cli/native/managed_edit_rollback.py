"""Classify, remember, and restore rejected Lean source edits."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _contains_lean_suggestion_tactic
from tools.utilities.patch_parser import preview_v4a_update

_NON_SEMANTIC_CANDIDATE_LINE_RE = re.compile(
    r"^\s*(?:trace_state|(?:all_goals\s+)?fail_if_success\s+done)(?:\s*--.*)?$"
)
_TRANSIENT_DIAGNOSTIC_COMMAND_RE = re.compile(
    r"^\s*(?:#(?:check|print|synth|eval|reduce|lint|find)\b.*|run_cmd\b.*|"
    r"set_option\s+trace\.[^\s]+\s+true\b.*)$"
)


def normalize_candidate_declaration(declaration: str) -> str:
    """Return a stable proof-candidate identity without diagnostic instrumentation."""
    normalized = str(declaration or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        line.rstrip()
        for line in normalized.splitlines()
        if not _NON_SEMANTIC_CANDIDATE_LINE_RE.match(line)
    ).strip()


def contains_transient_diagnostic(declaration: str) -> bool:
    """Return whether source contains a standalone diagnostic-only command."""
    return bool(transient_diagnostic_markers(declaration))


def transient_diagnostic_markers(source: str) -> tuple[str, ...]:
    """Return normalized standalone diagnostic commands present in Lean source."""
    normalized = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
    return tuple(
        " ".join(line.strip().split())
        for line in normalized.splitlines()
        if _NON_SEMANTIC_CANDIDATE_LINE_RE.match(line) is not None
        or _TRANSIENT_DIAGNOSTIC_COMMAND_RE.match(line) is not None
    )


def introduced_transient_diagnostics(before: str, after: str) -> tuple[str, ...]:
    """Return diagnostic commands newly introduced by one source edit."""
    before_counts = Counter(transient_diagnostic_markers(before))
    after_counts = Counter(transient_diagnostic_markers(after))
    introduced: list[str] = []
    for marker, count in after_counts.items():
        introduced.extend([marker] * max(0, count - before_counts[marker]))
    return tuple(introduced)


def contains_suggestion_tactic(declaration: str) -> bool:
    """Return whether source still contains an exploratory tactic-suggestion command."""
    return _contains_lean_suggestion_tactic(declaration)


def preview_candidate_source(
    function_name: str,
    args: Mapping[str, Any] | None,
    before_text: str,
) -> str:
    """Return the exact proposed source image when it can be reconstructed safely."""
    arguments = dict(args or {})
    if not before_text:
        return ""
    if function_name == "write_file":
        content = arguments.get("content")
        return content if isinstance(content, str) else ""
    if function_name == "patch":
        mode = str(arguments.get("mode", "replace") or "replace")
        if mode == "replace":
            old = str(arguments.get("old_string", "") or "")
            new = str(arguments.get("new_string", "") or "")
            if not old or before_text.count(old) != 1:
                return ""
            return before_text.replace(old, new, 1)
        patch_text = str(arguments.get("patch", "") or "")
    elif function_name == "apply_verified_patch":
        patch_text = str(arguments.get("patch", "") or "")
    else:
        return ""
    if not patch_text:
        return ""
    after_text, error = preview_v4a_update(patch_text, before_text)
    return "" if error or after_text is None else after_text


def matching_rejected_candidate(
    attempts: Sequence[Mapping[str, Any]],
    candidate_declaration: str,
) -> dict[str, Any] | None:
    """Return the newest rejection for one exact normalized declaration candidate."""
    normalized = normalize_candidate_declaration(candidate_declaration)
    if not normalized:
        return None
    candidate_hash = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
    for raw in reversed(list(attempts)):
        attempt = dict(raw)
        if str(attempt.get("declaration_hash", "") or "").strip() == candidate_hash:
            return attempt
    return None


def check_has_hard_errors(
    manager_check: Mapping[str, Any] | None,
    *,
    timed_out: Callable[[Mapping[str, Any] | None], bool],
) -> bool:
    """Return whether one completed gate contains concrete Lean errors."""
    checked = dict(manager_check or {})
    if not checked or timed_out(checked):
        return False
    nested = checked.get("incremental")
    payloads = [checked, dict(nested) if isinstance(nested, Mapping) else {}]
    for payload in payloads:
        if payload.get("has_errors") is True:
            return True
        for key in ("errors", "error_count"):
            value = payload.get(key)
            if isinstance(value, int) and value > 0:
                return True
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return True
        messages = payload.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for message in messages:
                if (
                    isinstance(message, Mapping)
                    and str(message.get("severity", "") or "").lower() == "error"
                ):
                    return True
    return False


def retained_edit_confirms_kernel_progress(
    active_file: str,
    *,
    before_sha256: str,
    expected_after_sha256: str,
    manager_check: Mapping[str, Any] | None,
    timed_out: Callable[[Mapping[str, Any] | None], bool],
) -> bool:
    """Return whether a changed, retained after-image received a concrete clean gate.

    A structurally accepted edit is only progress after its exact after-image has
    survived Lean.  Operational failures and restored or superseded revisions
    preserve any existing construction debt.
    """
    checked = dict(manager_check or {})
    if (
        not active_file
        or len(before_sha256) != 64
        or len(expected_after_sha256) != 64
        or before_sha256 == expected_after_sha256
        or not checked
        or checked.get("failed_edit_restored") is True
        or timed_out(checked)
        or check_has_hard_errors(checked, timed_out=timed_out)
    ):
        return False
    try:
        current_sha256 = hashlib.sha256(Path(active_file).read_bytes()).hexdigest()
    except OSError:
        return False
    if current_sha256 != expected_after_sha256:
        return False

    nested = checked.get("incremental")
    payloads = [checked, dict(nested) if isinstance(nested, Mapping) else {}]
    if any(payload.get("lean_started") is False for payload in payloads):
        return False
    return bool(
        checked.get("ok") is True
        or checked.get("lean_started") is True
        or any(payload.get("success") is True for payload in payloads)
    )


def restore_exact_after_image(
    active_file: str,
    *,
    before_text: str,
    expected_after_sha256: str,
) -> bool:
    """Atomically restore captured text only while its exact after-image is current."""
    if not active_file or not before_text or len(expected_after_sha256) != 64:
        return False
    path = Path(active_file)
    try:
        current = path.read_text(encoding="utf-8")
        if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_after_sha256:
            return False
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.managed-rollback-",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(before_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        return path.read_text(encoding="utf-8") == before_text
    except OSError:
        return False
