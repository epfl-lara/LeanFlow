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

from leanflow_cli.lean.lean_parsing import (
    LEAN_DECLARATION_PREAMBLE_RE,
    _declaration_line_index_from_text,
    _lean_suggestion_tactic_markers,
    _strip_lean_comments_and_strings,
)
from tools.utilities.patch_parser import preview_v4a_update

_NON_SEMANTIC_CANDIDATE_LINE_RE = re.compile(
    r"^\s*(?:trace_state|(?:all_goals\s+)?fail_if_success\s+done)(?:\s*--.*)?$"
)
_TRANSIENT_DIAGNOSTIC_COMMAND_RE = re.compile(
    r"^\s*(?:#(?:check|print|synth|eval|reduce|lint|find)\b.*|run_cmd\b.*|"
    r"set_option\s+trace\.[^\s]+\s+true\b.*)$"
)
_UNRESOLVED_TERM_METAVARIABLE_RE = re.compile(
    r"(?m)^\s*(?:·\s*)?(?:exact\b[^\n]*|[^\n]*\bfrom\s+|[^\n]*:=\s*)\?_(?=\s|\)|,|$)"
)
_SOURCE_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b", re.IGNORECASE)
REJECTED_HELPER_REPLAY_STATE_KEY = "rejected_helper_candidates"
_REJECTED_HELPER_REPLAY_LIMIT = 48
_DECLARATION_PREAMBLE_PATTERN = re.compile(LEAN_DECLARATION_PREAMBLE_RE)


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


def unresolved_term_metavariable_markers(source: str) -> tuple[str, ...]:
    """Return term holes that tactics cannot promote to ordinary focused goals."""
    sanitized = _strip_lean_comments_and_strings(str(source or ""))
    return tuple("?_" for _match in _UNRESOLVED_TERM_METAVARIABLE_RE.finditer(sanitized))


def introduced_unresolved_term_metavariables(before: str, after: str) -> tuple[str, ...]:
    """Return unresolved term holes newly introduced by one source edit."""
    before_count = len(unresolved_term_metavariable_markers(before))
    after_count = len(unresolved_term_metavariable_markers(after))
    return tuple("?_" for _index in range(max(0, after_count - before_count)))


def source_placeholder_markers(source: str) -> tuple[str, ...]:
    """Return unresolved proof placeholders outside comments and strings."""
    sanitized = _strip_lean_comments_and_strings(str(source or ""))
    return tuple(match.group(0).lower() for match in _SOURCE_PLACEHOLDER_RE.finditer(sanitized))


def introduced_source_placeholders(before: str, after: str) -> tuple[str, ...]:
    """Return proof placeholders newly introduced by one source edit."""
    before_counts = Counter(source_placeholder_markers(before))
    after_counts = Counter(source_placeholder_markers(after))
    introduced: list[str] = []
    for marker, count in after_counts.items():
        introduced.extend([marker] * max(0, count - before_counts[marker]))
    return tuple(introduced)


def contains_suggestion_tactic(declaration: str) -> bool:
    """Return whether source still contains an exploratory tactic-suggestion command."""
    return bool(_lean_suggestion_tactic_markers(declaration))


def introduced_suggestion_tactics(before: str, after: str) -> tuple[str, ...]:
    """Return suggestion tactics newly introduced by one source edit."""
    before_counts = Counter(_lean_suggestion_tactic_markers(before))
    after_counts = Counter(_lean_suggestion_tactic_markers(after))
    introduced: list[str] = []
    for marker, count in after_counts.items():
        introduced.extend([marker] * max(0, count - before_counts[marker]))
    return tuple(introduced)


def introduced_duplicate_declarations(before: str, after: str) -> tuple[str, ...]:
    """Return declaration names whose multiplicity an edit raises above one."""
    before_counts = Counter(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(str(before or ""))
    )
    after_counts = Counter(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(str(after or ""))
    )
    return tuple(
        name
        for name, count in after_counts.items()
        if name and count > 1 and count > before_counts[name]
    )


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


def _name_insensitive_declaration(declaration: str) -> str:
    """Return one declaration identity with only its declared name erased."""
    normalized = normalize_candidate_declaration(declaration)
    match = _DECLARATION_PREAMBLE_PATTERN.match(normalized)
    if not normalized or match is None or match.start(2) < 0:
        return ""
    anonymous = normalized[: match.start(2)] + "<helper>" + normalized[match.end(2) :]
    return " ".join(anonymous.split())


def helper_candidate_fingerprint(declaration: str) -> str:
    """Return a proof-sensitive fingerprint that ignores the helper's name."""
    anonymous = _name_insensitive_declaration(declaration)
    if not anonymous:
        return ""
    return hashlib.sha256(anonymous.encode("utf-8", "replace")).hexdigest()


def _declaration_fingerprints(source: str) -> list[dict[str, str]]:
    """Return named declaration fingerprints from one in-memory Lean source image."""
    records: list[dict[str, str]] = []
    for entry in _declaration_line_index_from_text(str(source or "")):
        declaration = str(entry.get("text", "") or "")
        fingerprint = helper_candidate_fingerprint(declaration)
        name = str(entry.get("name", "") or "").strip()
        if fingerprint and name:
            records.append(
                {
                    "name": name,
                    "fingerprint": fingerprint,
                    "declaration": normalize_candidate_declaration(declaration),
                }
            )
    return records


def remember_rejected_helper_check(
    state: dict[str, Any],
    *,
    args: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
) -> list[dict[str, Any]]:
    """Remember concrete failed ``check_helper`` candidates for pre-edit replay fencing."""
    arguments = dict(args or {})
    payload = dict(result or {})
    if str(arguments.get("action", "") or "") != "check_helper":
        return []
    status = str(payload.get("status", "") or "").lower()
    if (
        payload.get("ok") is True
        or payload.get("valid_without_sorry") is True
        or payload.get("lean_started") is False
        or payload.get("timed_out") is True
        or "timeout" in status
        or not target_symbol
        or not active_file
        or len(source_revision_sha256) != 64
    ):
        return []
    messages = payload.get("messages")
    concrete_error = payload.get("has_errors") is True or payload.get("error_count", 0) not in {
        0,
        "0",
        None,
    }
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        concrete_error = concrete_error or any(
            isinstance(message, Mapping)
            and str(message.get("severity", "") or "").lower() == "error"
            for message in messages
        )
    if not concrete_error:
        return []
    reason = str(payload.get("error", "") or "").strip()
    if not reason and isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        reason = next(
            (
                str(message.get("message", "") or "").strip()
                for message in messages
                if isinstance(message, Mapping)
                and str(message.get("severity", "") or "").lower() == "error"
            ),
            "",
        )
    remembered: list[dict[str, Any]] = []
    records = list(state.get(REJECTED_HELPER_REPLAY_STATE_KEY) or [])
    existing = {
        (
            str(record.get("fingerprint", "") or ""),
            str(record.get("target_symbol", "") or ""),
            str(record.get("active_file", "") or ""),
            str(record.get("source_revision_sha256", "") or ""),
        )
        for record in records
        if isinstance(record, Mapping)
    }
    for candidate in _declaration_fingerprints(str(arguments.get("replacement", "") or "")):
        identity = (
            candidate["fingerprint"],
            target_symbol,
            active_file,
            source_revision_sha256,
        )
        if identity in existing:
            continue
        record: dict[str, Any] = {
            **candidate,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "source_revision_sha256": source_revision_sha256,
            "reason": " ".join(reason.split())[:500],
        }
        records.append(record)
        remembered.append(record)
        existing.add(identity)
    state[REJECTED_HELPER_REPLAY_STATE_KEY] = records[-_REJECTED_HELPER_REPLAY_LIMIT:]
    return remembered


def matching_new_rejected_helper(
    state: Mapping[str, Any],
    *,
    before_source: str,
    after_source: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
) -> dict[str, Any] | None:
    """Return a renamed failed helper newly introduced at the same source revision."""
    before_counts = Counter(
        record["fingerprint"]
        for record in _declaration_fingerprints(before_source)
        if record["name"] != target_symbol
    )
    added: list[dict[str, str]] = []
    for candidate in _declaration_fingerprints(after_source):
        if candidate["name"] == target_symbol:
            continue
        fingerprint = candidate["fingerprint"]
        if before_counts[fingerprint]:
            before_counts[fingerprint] -= 1
        else:
            added.append(candidate)
    if not added:
        return None
    rejected = [
        dict(record)
        for record in state.get(REJECTED_HELPER_REPLAY_STATE_KEY, [])
        if isinstance(record, Mapping)
        and str(record.get("target_symbol", "") or "") == target_symbol
        and str(record.get("active_file", "") or "") == active_file
        and str(record.get("source_revision_sha256", "") or "") == source_revision_sha256
    ]
    for candidate in added:
        for record in reversed(rejected):
            if record.get("fingerprint") == candidate["fingerprint"]:
                return {**record, "replayed_name": candidate["name"]}
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
