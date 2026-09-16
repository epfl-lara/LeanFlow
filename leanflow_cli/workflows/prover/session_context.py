"""Compact job history deterministically while retaining its complete contract."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.utils import atomic_json_write

_HANDOFF_PREFIX = "Context history checkpoint:\n"


def _redact_archive_value(value: Any) -> Any:
    """Redact string leaves without letting a regex corrupt serialized JSON."""
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: _redact_archive_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_archive_value(item) for item in value]
    return value


def _prepare_archive(
    messages: list[dict[str, Any]], workspace: Path
) -> tuple[Path, dict[str, Any]]:
    """Prepare a stable checkpoint without writing rejected compaction attempts."""
    saved = {"messages": _redact_archive_value(messages)}
    encoded = json.dumps(saved, ensure_ascii=False, sort_keys=True).encode("utf-8")
    artifact = workspace / "context-history" / (hashlib.sha256(encoded).hexdigest() + ".json")
    return artifact, saved


def _archive_handoff(artifact: Path) -> str:
    """Describe where to recover discarded evidence without asserting proof claims."""
    return (
        _HANDOFF_PREFIX + f"Earlier exchanges are saved at {artifact}. "
        "Use read_file for exact older evidence if needed. Earlier checkpoints remain linked "
        "from that history. The current assignment remains authoritative. Proof notes are "
        "working hypotheses, not verified results. Current workspace files retain proof edits."
    )


def _accept_compaction(
    messages: list[dict[str, Any]],
    compacted: list[dict[str, Any]],
    archive: tuple[Path, dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Persist a checkpoint only when a smaller replacement will be installed."""
    if compacted == messages or approximate_tokens(compacted) >= approximate_tokens(messages):
        return messages, False
    if archive is not None:
        artifact, saved = archive
        if not artifact.exists():
            artifact.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(artifact, saved)
    return compacted, True


def approximate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate input tokens conservatively without calling a model."""
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) // 3 + 1


def tool_result_message(result: dict[str, Any], workspace: Path) -> tuple[str, Path | None]:
    """Keep feedback valid JSON and retain omitted evidence as a readable artifact."""
    content = json.dumps(result, ensure_ascii=False, default=str)
    if len(content) <= 16000:
        return content, None
    artifact = workspace / "tool-results" / (uuid.uuid4().hex + ".json")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(artifact, json.loads(content))
    # Slice the serialized preview by its encoded length so escaping quotes and
    # control characters cannot push the wrapper beyond the message ceiling.
    preview = content[:12000]
    while len(json.dumps(preview, ensure_ascii=False)) > 12000:
        preview = preview[: len(preview) * 3 // 4]
    bounded = {
        "success": result.get("success"),
        "truncated": True,
        "artifact_path": str(artifact),
        "preview": preview,
        "note": "Full structured evidence is saved at artifact_path; use read_file to inspect more.",
    }
    return json.dumps(bounded, ensure_ascii=False), artifact


def compact_history(
    messages: list[dict[str, Any]],
    *,
    context_tokens: int,
    workspace: Path,
    target_tokens: int | None = None,
    hard_limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Retain pinned instructions, durable notes, and complete recent exchanges.

    Never retain an orphan tool result. The original prompt is immutable and
    includes the assignment, plan, DAG and budget contract on every request.
    The optional lower target leaves room for future work; exact instructions
    and the newest tool exchange may exceed it, but never authorize an oversized
    provider request. Save an archive before removing evidence in this mode.
    """
    if approximate_tokens(messages) <= context_tokens:
        return messages, False
    notes_path = workspace / "PLAN_job.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
    if len(notes) > 16000:
        notes = notes[:8000] + "\n[Middle omitted; read full notes file.]\n" + notes[-8000:]
    pinned = messages[:2]
    groups: list[list[dict[str, Any]]] = []
    for message in messages[2:]:
        if message.get("role") == "user" and str(message.get("content", "")).startswith(
            ("Current proof notes:\n", _HANDOFF_PREFIX)
        ):
            continue
        if message.get("role") != "tool" or not groups:
            groups.append([])
        groups[-1].append(message)
    latest = (
        groups[-1]
        if target_tokens is not None
        and groups
        and any(message.get("role") == "tool" for message in groups[-1])
        else []
    )
    limit = context_tokens if target_tokens is None else min(context_tokens, target_tokens)
    ceiling = context_tokens if hard_limit is None else hard_limit
    archive = _prepare_archive(messages, workspace) if target_tokens is not None else None
    if latest and approximate_tokens(pinned + latest) > ceiling:
        # Only the exact contract and unseen feedback may force a hard failure.
        # Optional notes or checkpoint text must never make a fitting pair fail.
        return _accept_compaction(messages, pinned + latest, archive)
    if archive is not None:
        handoff = {"role": "user", "content": _archive_handoff(archive[0])}
        if approximate_tokens(pinned + [handoff] + latest) <= ceiling:
            pinned = pinned + [handoff]
    # Pinned declarations are an explicit floor, not something a summary may
    # paraphrase. The caller reports whether the requested target was attainable.
    limit = max(limit, approximate_tokens(pinned + latest))
    if notes:
        note_message = {"role": "user", "content": "Current proof notes:\n" + notes}
        # Durable notes must not turn an otherwise fitting contract into an
        # oversized request. Their complete file remains available to read_file.
        while notes and approximate_tokens(pinned + [note_message] + latest) > limit:
            notes = notes[: len(notes) // 2]
            note_message["content"] = (
                "Current proof notes:\n" + notes + f"\nFull notes: {notes_path}"
            )
        if notes:
            pinned = pinned + [note_message]
    retained: list[dict[str, Any]] = []
    for group in reversed(groups):
        candidate = pinned + group + retained
        if approximate_tokens(candidate) > limit:
            break
        retained = group + retained
    compacted = pinned + retained
    return _accept_compaction(messages, compacted, archive)
