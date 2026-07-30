"""Keep answer definitions provisional until their characterizing theorem verifies.

Determine-style olympiad tasks commonly expose two source holes: a definition named
``answer`` followed by a theorem whose statement characterizes that definition. A
kernel-clean definition body only proves that the proposed value elaborates; it does
not prove that the value is mathematically correct. This module recognizes that
source shape, persists the answer/result coupling, and renders the narrow permission
needed for the result turn to revise a disproved proposal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)

STATE_KEY = "provisional_determine_answers"

_DEFINITION_RE = re.compile(r"\b(?:def|abbrev)\s+([A-Za-z_][A-Za-z0-9_'.]*)\b")
_THEOREM_RE = re.compile(r"\b(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_'.]*)\b")


@dataclass(frozen=True)
class DetermineAnswerDependency:
    """Describe one provisional answer and its characterizing consumer."""

    answer_target: str
    answer_file: str
    consumer_target: str
    consumer_file: str

    def storage_key(self) -> str:
        """Return a stable state key for the provisional answer."""
        return f"{_normalized_file(self.answer_file)}::{self.answer_target}"

    def to_mapping(
        self,
        *,
        verification: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Serialize the coupling and the answer's elaboration evidence."""
        return {
            "answer_target": self.answer_target,
            "answer_file": _normalized_file(self.answer_file),
            "consumer_target": self.consumer_target,
            "consumer_file": _normalized_file(self.consumer_file),
            "answer_verification": dict(verification or {}),
        }


def _normalized_file(value: str) -> str:
    """Return a best-effort absolute file identity."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except Exception:
        return text


def _short_name(value: str) -> str:
    """Return the unqualified Lean declaration name."""
    return str(value or "").strip().removeprefix("_root_.").split(".")[-1]


def _slice_body(value: str) -> str:
    """Remove the managed queue's display header from a declaration slice."""
    text = str(value or "").strip()
    if text.startswith("Assigned declaration slice") and ":\n" in text:
        return text.partition(":\n")[2].strip()
    return text


def _references_name(source: str, name: str) -> bool:
    """Return whether Lean source contains an exact identifier reference."""
    stripped = _strip_lean_comments_and_strings(_slice_body(source))
    short = _short_name(name)
    return (
        bool(short)
        and re.search(
            rf"(?<![A-Za-z0-9_']){re.escape(short)}(?![A-Za-z0-9_'])",
            stripped,
        )
        is not None
    )


def detect_transition(
    assignment: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
) -> DetermineAnswerDependency | None:
    """Recognize an answer-hole transition into its characterizing theorem.

    Detection is deliberately narrow: the previous assigned declaration must
    be a ``def``/``abbrev`` named ``answer``; the next declaration must be a
    theorem/lemma in the same file and its source must reference that answer
    exactly. The baseline need not still contain ``sorry`` because a checkpoint
    may have refreshed it after the proposal elaborated but before handoff.
    """
    previous = dict(assignment or {})
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    answer_target = str(previous.get("target_symbol", "") or "").strip()
    answer_file = str(previous.get("active_file", "") or "").strip()
    consumer_target = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    consumer_file = str(
        current.get("active_file", "") or current.get("active_file_label", "") or ""
    ).strip()
    answer_slice = _slice_body(str(previous.get("slice", "") or ""))
    consumer_slice = _slice_body(
        str(current.get("current_queue_item_slice", "") or item.get("text", "") or "")
    )
    if (
        _short_name(answer_target) != "answer"
        or not answer_file
        or not consumer_target
        or not consumer_file
        or _normalized_file(answer_file) != _normalized_file(consumer_file)
    ):
        return None
    definition = _DEFINITION_RE.search(_strip_lean_comments_and_strings(answer_slice))
    consumer = _THEOREM_RE.search(_strip_lean_comments_and_strings(consumer_slice))
    if (
        definition is None
        or _short_name(definition.group(1)) != "answer"
        or consumer is None
        or _short_name(consumer.group(1)) != _short_name(consumer_target)
        or not _references_name(consumer_slice, answer_target)
    ):
        return None
    return DetermineAnswerDependency(
        answer_target=answer_target,
        answer_file=answer_file,
        consumer_target=consumer_target,
        consumer_file=consumer_file,
    )


def discover_for_consumer(
    active_file: str,
    consumer_target: str,
) -> DetermineAnswerDependency | None:
    """Recover a determine coupling after restart from authoritative source.

    Recovery requires the explicit ``The answer to be determined`` source
    comment, an earlier ``answer`` definition, and an exact reference from the
    assigned theorem/lemma. This lets a run interrupted between answer and
    result restore provisional edit permission without classifying ordinary
    pre-existing definitions as determine holes.
    """
    path = str(active_file or "").strip()
    consumer_name = str(consumer_target or "").strip()
    if not path or not consumer_name:
        return None
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    entries = _declaration_line_index_from_text(source)
    consumer_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if _short_name(str(entry.get("name", "") or "")) == _short_name(consumer_name)
            and str(entry.get("kind", "") or "") in {"theorem", "lemma"}
        ),
        -1,
    )
    if consumer_index < 0:
        return None
    consumer = entries[consumer_index]
    candidates = [
        entry
        for entry in entries[:consumer_index]
        if _short_name(str(entry.get("name", "") or "")) == "answer"
        and str(entry.get("kind", "") or "") in {"def", "abbrev"}
    ]
    if not candidates:
        return None
    answer = candidates[-1]
    answer_line = max(1, int(answer.get("line", 1) or 1))
    source_lines = source.splitlines()
    nearby_preamble = "\n".join(source_lines[max(0, answer_line - 9) : answer_line]).lower()
    if "answer to be determined" not in nearby_preamble:
        return None
    consumer_source = str(consumer.get("text", "") or "")
    if not _references_name(consumer_source, "answer"):
        return None
    return DetermineAnswerDependency(
        answer_target=str(answer.get("name", "") or "answer").strip(),
        answer_file=path,
        consumer_target=str(consumer.get("name", "") or consumer_name).strip(),
        consumer_file=path,
    )


def register(
    state: MutableMapping[str, Any],
    dependency: DetermineAnswerDependency,
    *,
    verification: Mapping[str, Any] | None = None,
) -> None:
    """Persist one answer/result coupling across turns and checkpoints."""
    entries = state.setdefault(STATE_KEY, {})
    if not isinstance(entries, dict):
        entries = {}
        state[STATE_KEY] = entries
    entries[dependency.storage_key()] = dependency.to_mapping(verification=verification)


def entries_for_consumer(
    state: Mapping[str, Any] | None,
    *,
    consumer_target: str,
    consumer_file: str,
) -> tuple[dict[str, Any], ...]:
    """Return provisional answers coupled to the current consumer."""
    raw_entries = (state or {}).get(STATE_KEY)
    if not isinstance(raw_entries, Mapping):
        return ()
    normalized_file = _normalized_file(consumer_file)
    matches: list[dict[str, Any]] = []
    for raw in raw_entries.values():
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        if (
            _short_name(str(entry.get("consumer_target", "") or "")) == _short_name(consumer_target)
            and _normalized_file(str(entry.get("consumer_file", "") or "")) == normalized_file
        ):
            matches.append(entry)
    return tuple(matches)


def editable_answer_names(
    state: Mapping[str, Any] | None,
    *,
    consumer_target: str,
    consumer_file: str,
) -> frozenset[str]:
    """Return answer declarations the coupled result turn may revise."""
    return frozenset(
        str(entry.get("answer_target", "") or "").strip()
        for entry in entries_for_consumer(
            state,
            consumer_target=consumer_target,
            consumer_file=consumer_file,
        )
        if str(entry.get("answer_target", "") or "").strip()
    )


def resolve_for_consumer(
    state: MutableMapping[str, Any],
    *,
    consumer_target: str,
    consumer_file: str,
) -> tuple[dict[str, Any], ...]:
    """Remove and return provisional answers certified by a solved consumer."""
    raw_entries = state.get(STATE_KEY)
    if not isinstance(raw_entries, dict):
        return ()
    matches = entries_for_consumer(
        state,
        consumer_target=consumer_target,
        consumer_file=consumer_file,
    )
    matched_keys = {
        f"{_normalized_file(str(entry.get('answer_file', '') or ''))}::"
        f"{str(entry.get('answer_target', '') or '').strip()}"
        for entry in matches
    }
    for key in matched_keys:
        raw_entries.pop(key, None)
    if not raw_entries:
        state.pop(STATE_KEY, None)
    return matches


def prompt(
    state: Mapping[str, Any] | None,
    *,
    consumer_target: str,
    consumer_file: str,
) -> str:
    """Render the coupled determination contract for the current theorem."""
    entries = entries_for_consumer(
        state,
        consumer_target=consumer_target,
        consumer_file=consumer_file,
    )
    if not entries:
        return ""
    names = ", ".join(f"`{str(entry.get('answer_target', '') or '').strip()}`" for entry in entries)
    return "\n".join(
        [
            "Determine-answer lifecycle:",
            f"- {names} elaborated, but remains provisional until `{consumer_target}` verifies",
            "- the dependent theorem is the mathematical acceptance gate; compilation of a set/value guess alone is not evidence that the answer is correct",
            "- if proof search, a counterexample, or verified research contradicts the proposal, revise the coupled answer definition in this turn and continue—do not keep trying to prove a false frozen statement",
            "- preserve the research ledger and record rejected candidate answers and dead branches before changing the proposal",
            f"- only a clean kernel gate for `{consumer_target}` promotes the coupled answer",
        ]
    )


def without_editable_answers(
    protected: Sequence[Mapping[str, Any]],
    editable_names: Sequence[str],
) -> list[dict[str, Any]]:
    """Exclude coupled answer definitions from the ordinary queue edit guard."""
    short_names = {_short_name(name) for name in editable_names if _short_name(name)}
    return [
        dict(entry)
        for entry in protected
        if _short_name(str(entry.get("name", "") or "")) not in short_names
    ]
