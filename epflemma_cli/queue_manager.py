#!/usr/bin/env python3
"""Single source of truth for the per-theorem proving queue.

This module maps the spec contract in `docs/product-reference.md`
("Theorem-By-Theorem Proving Loop") onto a single typed object instead of
scattered untyped dict keys on `autonomy_state` and `live_state`. The runner
still persists the legacy dict shape for checkpoint compatibility; this class
owns the runtime mutations and renders back to that shape at boundaries.

Migration map (legacy -> here):

    autonomy_state["current_queue_assignment"]   -> TheoremQueueManager.current
    autonomy_state["failed_attempts"]            -> TheoremQueueManager._attempts
    autonomy_state["manager_feedback_retries"]   -> TheoremQueueManager._retries
    autonomy_state["theorem_outcomes"]           -> TheoremQueueManager._outcomes
    autonomy_state["incremental_prepare"]        -> QueueAssignment.prepare
    live_state["build_status"] (regex-derived)   -> TheoremQueueManager.last_verification
    live_state["declaration_queue_*"]            -> TheoremQueueManager.queue / pending_count

Function map (legacy -> method):

    _current_queue_item                  -> select_next(...)
    _prepare_queue_assignment_state      -> assign(item, prepare_fn=...)
    _queue_assignment_transition         -> detect_transition(target, file)
    _remember_failed_attempt             -> record_attempt(reason, proof_shape)
    _record_theorem_outcome              -> record_outcome(status, note)
    _clear_manager_feedback_retries      -> (handled inside transition_to)
    _manager_feedback_kind +
        _same_queue_assignment_still_blocked -> classify(check)        # one classifier
    _failed_attempt_count_for_theorem    -> attempts_for_current()
    _scoped_failed_attempt_entries       -> attempts_for(target, file)
    _extract_recent_build_status         -> (deleted; see record_verification)

The class is intentionally pure: no I/O, no logging, no MCP calls. The runner
keeps owning side effects (LeanInteract warmup, stdout, activity feed); this
class only owns the *bookkeeping* and the invariant checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Tunables (lifted from native_runner so callers can keep their env-var knobs)
# ---------------------------------------------------------------------------

DEFAULT_FAILED_ATTEMPT_HISTORY = 10
DEFAULT_REASONING_ESCALATION_THRESHOLD = 5
DEFAULT_WARNING_RETRY_LIMIT = 1
DEFAULT_HARD_RETRY_LIMIT = 2


# ---------------------------------------------------------------------------
# Identity / value types
# ---------------------------------------------------------------------------

def _normalize_path(value: str) -> str:
    """Best-effort canonicalization that never raises on bad inputs.

    The legacy code calls ``Path(...).expanduser().resolve()`` inside
    ``_manager_feedback_retry_key`` and the same logic in ``_same_active_file``;
    we centralize it here so retry-counter keys, failed-attempt keys, and
    transition detection can never disagree on what counts as "the same file".
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except Exception:
        return text


@dataclass(frozen=True)
class TheoremKey:
    """Stable identity of a (file, declaration) pair across an entire run."""

    target_symbol: str
    active_file: str  # normalized absolute path

    @classmethod
    def make(cls, target_symbol: str, active_file: str) -> "TheoremKey":
        return cls(
            target_symbol=(target_symbol or "").strip(),
            active_file=_normalize_path(active_file),
        )

    def is_valid(self) -> bool:
        return bool(self.target_symbol and self.active_file)

    def storage_key(self) -> str:
        # Stable string form for places that still need a dict key (outcomes
        # store, JSONL serialization, telemetry).
        return f"{self.active_file}::{self.target_symbol}"


@dataclass(frozen=True)
class QueueItem:
    """One pending declaration the manager could assign to the agent."""

    label: str
    kind: str = ""                     # "theorem" / "lemma" / "example" / ...
    line: int = 0
    end_line: int = 0
    reasons: tuple[str, ...] = ()      # e.g. ("contains sorry", "diagnostic near line 100")
    blocker_signature: str = ""
    search_hints: tuple[str, ...] = ()
    verification_gate: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "QueueItem":
        return cls(
            label=str(raw.get("label", "") or "").strip(),
            kind=str(raw.get("kind", "") or "").strip(),
            line=int(raw.get("line", 0) or 0),
            end_line=int(raw.get("end_line", 0) or 0),
            reasons=tuple(str(r) for r in (raw.get("reasons") or [])),
            blocker_signature=str(raw.get("blocker_signature", "") or ""),
            search_hints=tuple(str(h) for h in (raw.get("search_hints") or [])),
            verification_gate=str(raw.get("verification_gate", "") or ""),
        )

    def has_diagnostic_reason(self) -> bool:
        return any("diagnostic" in r.lower() or "error" in r.lower() for r in self.reasons)

    def has_sorry_reason(self) -> bool:
        return "contains sorry" in {r.lower() for r in self.reasons}


@dataclass(frozen=True)
class PrepareState:
    """Result of warming LeanInteract for a queue item.

    Mirrors the dict produced by ``_manager_prepare_incremental_queue_item``;
    kept opaque so we can swap implementations without churn here.
    """

    success: bool
    ok: bool = False
    elapsed_s: float = 0.0
    cache: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PrepareState":
        raw = raw or {}
        return cls(
            success=bool(raw.get("success", False)),
            ok=bool(raw.get("ok", False)),
            elapsed_s=float(raw.get("elapsed_s", 0.0) or 0.0),
            cache=dict(raw.get("cache") or {}),
            error=str(raw.get("error", "") or ""),
        )

    def is_warm(self) -> bool:
        return self.success and self.ok


@dataclass(frozen=True)
class QueueAssignment:
    """The currently-assigned theorem turn.

    Spec contract (docs/product-reference.md, section "Theorem-By-Theorem
    Proving Loop", step 3): "the assignment is the worker boundary." The model
    owns the assigned proof task, may add helper declarations for it, and must
    not modify pre-existing non-assigned declarations or future queue items.

    `slice` is the declaration text captured at assignment time so the runner
    can detect out-of-scope edits and restore baseline.
    """

    key: TheoremKey
    slice: str = ""
    prepare: PrepareState = field(default_factory=lambda: PrepareState(success=False))


# ---------------------------------------------------------------------------
# Verification record (replaces _extract_recent_build_status regex)
# ---------------------------------------------------------------------------

class VerificationScope(str, Enum):
    TARGET = "target"           # lean_incremental_check(check_target)
    FILE_EXACT = "file_exact"   # lean_verify(mode=file_exact)
    MODULE = "module"           # lake build <Module>
    PROJECT = "project"         # lake build
    INSPECT = "inspect"         # lean_inspect refresh (not a real verification)


@dataclass(frozen=True)
class VerificationRecord:
    """One authoritative snapshot of what the manager actually verified.

    Replaces ``_extract_recent_build_status`` which fabricated a string by
    grepping the last 12 messages for the words "error" and "build".  Every
    place that previously read ``live_state["build_status"]`` should read
    this instead. If we have not run a verification since the last edit,
    callers must surface ``None`` rather than inventing a string.
    """

    scope: VerificationScope
    ok: bool
    tool: str = ""                # "lean_incremental_check" / "lake build" / ...
    target: str = ""              # for TARGET scope
    cache: str = ""               # "warm" / "cold" / "rebuilt"
    elapsed_s: float = 0.0
    errors: int = 0
    warnings: int = 0
    sorry_count: int = 0
    summary: str = ""             # short single-line for handoff rendering


def verification_from_mapping(raw: Mapping[str, Any] | None) -> VerificationRecord | None:
    """Parse a legacy verification dict into a typed record."""
    if not isinstance(raw, Mapping) or not raw:
        return None
    try:
        raw_scope = str(raw.get("scope", "") or "")
        if raw_scope.startswith("target:"):
            scope = VerificationScope.TARGET
            target = str(raw.get("target", "") or raw_scope.split(":", 1)[1] or "")
        elif raw_scope == "file":
            scope = VerificationScope.FILE_EXACT
            target = str(raw.get("target", "") or "")
        else:
            scope = VerificationScope(raw_scope or VerificationScope.TARGET)
            target = str(raw.get("target", "") or "")
        return VerificationRecord(
            scope=scope,
            ok=bool(raw.get("ok", False)),
            tool=str(raw.get("tool", "") or ""),
            target=target,
            cache=str(raw.get("cache", "") or ""),
            elapsed_s=float(raw.get("elapsed_s", 0.0) or 0.0),
            errors=int(raw.get("errors", 0) or 0),
            warnings=int(raw.get("warnings", 0) or 0),
            sorry_count=int(raw.get("sorry", raw.get("sorry_count", 0)) or 0),
            summary=str(raw.get("summary", "") or ""),
        )
    except Exception:
        return None


def verification_to_mapping(record: VerificationRecord | None) -> dict[str, Any]:
    """Render a typed verification record using the legacy checkpoint keys."""
    if record is None:
        return {}
    if record.scope is VerificationScope.TARGET:
        scope = f"target:{record.target or '[unknown]'}"
    elif record.scope is VerificationScope.FILE_EXACT:
        scope = "file"
    else:
        scope = record.scope.value
    return {
        "scope": scope,
        "ok": record.ok,
        "tool": record.tool,
        "target": record.target,
        "cache": record.cache,
        "elapsed_s": record.elapsed_s,
        "errors": record.errors,
        "warnings": record.warnings,
        "sorry": record.sorry_count,
        "summary": record.summary,
    }


# ---------------------------------------------------------------------------
# Manager classification (one classifier, not two)
# ---------------------------------------------------------------------------

class Classification(str, Enum):
    HARD_BLOCKER = "hard_blocker"        # error / open goals / assigned-decl sorry
    WARNING_ONCE = "warning_once"        # warning-only, opportunity not yet spent
    ACCEPT = "accept"                    # clean, OR warnings after opportunity
    FUTURE_ONLY = "future_only"          # no diagnostics on assigned decl


@dataclass(frozen=True)
class ManagerCheck:
    """Input to ``classify``. Wraps whatever evidence the runner has.

    Construct from either ``lean_incremental_check`` output, ``lean_verify``
    output, or a final-report review. The classifier must not care which.
    """

    has_assigned_sorry: bool = False
    has_assigned_error: bool = False
    has_assigned_open_goals: bool = False
    has_assigned_warning: bool = False
    has_future_evidence: bool = False
    verification_failed: bool = False     # explicit ok=False from a real check
    raw_messages: tuple[str, ...] = ()    # unstructured fallback (lake stderr etc.)


@dataclass(frozen=True)
class FailedAttempt:
    key: TheoremKey
    attempt: int                # 1-indexed within (theorem, file)
    cycle: int                  # workflow cycle number
    proof_shape: str            # short text: snippet of the body or its diff
    reason: str                 # short text: blocker summary


@dataclass(frozen=True)
class TheoremOutcome:
    key: TheoremKey
    status: str                 # "solved" / "unresolved" / "skipped"
    note: str = ""
    build_status: str = ""
    verification: VerificationRecord | None = None


@dataclass(frozen=True)
class Transition:
    """Boundary event between two assignments."""

    previous: TheoremKey | None
    current: TheoremKey | None

    def is_new_theorem(self) -> bool:
        return (
            self.previous is not None
            and self.current is not None
            and self.previous != self.current
        )


# ---------------------------------------------------------------------------
# Pure helpers (no state, easy to unit-test)
# ---------------------------------------------------------------------------

def select_next_item(
    queue: Sequence[QueueItem],
    *,
    is_present_in_file: Callable[[str], bool],
) -> QueueItem | None:
    """Spec rule (line 519 of product-reference): error diagnostics first,
    then ``sorry`` placeholders, then nothing else.

    This deliberately drops the legacy "third fallback" in
    ``_current_queue_item`` ([native_runner.py:2718](epflemma_cli/native_runner.py:2718))
    that returned the first declaration found in the file even with no
    diagnostics or sorry — that path could select a clean declaration and
    silently violate the spec. If neither bucket matches, return None and let
    the caller treat the queue as empty so the final-sweep path runs.
    """
    if not queue:
        return None
    for item in queue:
        if item.label and is_present_in_file(item.label) and item.has_diagnostic_reason():
            return item
    for item in queue:
        if item.label and is_present_in_file(item.label) and item.has_sorry_reason():
            return item
    return None


def classify_check(check: ManagerCheck) -> Classification:
    """The single classifier.

    Replaces both ``_manager_feedback_kind`` and the parallel string-matching
    inside ``_same_queue_assignment_still_blocked``. The two had diverged in
    edge cases (the duplicate ``has_sorry`` short-circuit at lines 974 and
    1016, and the ``blocker_summary`` regex at 3477).

    Order matters: hard blockers always dominate; warnings only matter when
    nothing harder is present; FUTURE_ONLY means the assigned declaration is
    clean but other parts of the file still need work.
    """
    assigned_evidence = (
        check.has_assigned_sorry
        or check.has_assigned_error
        or check.has_assigned_open_goals
        or check.has_assigned_warning
    )
    if check.verification_failed and check.has_future_evidence and not assigned_evidence:
        return Classification.FUTURE_ONLY
    if check.verification_failed and not assigned_evidence:
        # Verification said "no" but we don't have decl-scoped evidence — treat
        # as a hard blocker so we don't silently advance.
        return Classification.HARD_BLOCKER
    if (
        check.has_assigned_sorry
        or check.has_assigned_error
        or check.has_assigned_open_goals
    ):
        return Classification.HARD_BLOCKER
    if check.has_assigned_warning:
        return Classification.WARNING_ONCE
    if check.verification_failed:
        # Verification failed but assigned decl is clean -> errors live in
        # other declarations; spec calls this FUTURE_ONLY.
        return Classification.FUTURE_ONLY
    return Classification.ACCEPT


# ---------------------------------------------------------------------------
# The class
# ---------------------------------------------------------------------------

class QueueInvariantError(AssertionError):
    """Raised by ``check_invariants`` when a spec invariant is violated.

    Callers can run with ``EPFLEMMA_QUEUE_INVARIANT_CHECKS=1`` to turn these
    on. Off by default so production never crashes on a paranoid check.
    """


class TheoremQueueManager:
    """Owns all per-theorem queue state for one autonomous workflow run.

    Lifetime: created when the runner starts a managed prove/formalize loop,
    persists across API steps, discarded when the workflow ends. Resume from
    checkpoint: serialize via :meth:`to_state` / :meth:`from_state`.

    Concurrency: not thread-safe. The runner is single-threaded; the swarm
    workflow uses one manager per agent.
    """

    OWNED_AUTONOMY_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "current_queue_assignment",
            "failed_attempts",
            "manager_feedback_retries",
            "manager_feedback_retry_consumed_signatures",
            "theorem_outcomes",
            "last_verification",
            "disabled_tools_this_run",
            "reasoning_effort_by_theorem",
        }
    )

    # ----- construction / serialization ---------------------------------

    def __init__(
        self,
        *,
        warning_retry_limit: int = DEFAULT_WARNING_RETRY_LIMIT,
        hard_retry_limit: int = DEFAULT_HARD_RETRY_LIMIT,
        failed_attempt_history: int = DEFAULT_FAILED_ATTEMPT_HISTORY,
        reasoning_escalation_threshold: int = DEFAULT_REASONING_ESCALATION_THRESHOLD,
    ) -> None:
        self._queue: list[QueueItem] = []
        self._current: QueueAssignment | None = None
        self._attempts: list[FailedAttempt] = []
        self._display_files: dict[TheoremKey, str] = {}
        self._warning_retries: dict[TheoremKey, int] = {}
        self._hard_retries: dict[TheoremKey, int] = {}
        self._retry_signatures: dict[tuple[TheoremKey, str], list[str]] = {}
        self._outcomes: dict[TheoremKey, TheoremOutcome] = {}
        self._last_verification: VerificationRecord | None = None
        self._disabled_tool_reasons: dict[str, str] = {}
        self._reasoning_effort_by_key: dict[TheoremKey, str] = {}
        self._pending_active_file = ""

        self._warning_retry_limit = warning_retry_limit
        self._hard_retry_limit = hard_retry_limit
        self._failed_attempt_history = failed_attempt_history
        self._reasoning_escalation_threshold = reasoning_escalation_threshold

    # ----- queue state --------------------------------------------------

    def replace_queue(self, items: Iterable[Mapping[str, Any] | QueueItem]) -> None:
        """Refresh the queue from a freshly-built list.

        Called after every ``lean_inspect``. The current assignment, if it
        still appears in the new queue, is kept; if it no longer appears we
        consider the theorem solved (the runner will detect this at the next
        ``detect_transition`` call and emit a boundary event).
        """
        self._queue = [
            it if isinstance(it, QueueItem) else QueueItem.from_mapping(it)
            for it in (items or [])
        ]

    @property
    def queue(self) -> tuple[QueueItem, ...]:
        return tuple(self._queue)

    def _remember_display_file(self, key: TheoremKey, active_file: str) -> None:
        raw = str(active_file or "").strip()
        if key.is_valid() and raw:
            self._display_files[key] = raw

    def _display_file_for(self, key: TheoremKey) -> str:
        return self._display_files.get(key) or key.active_file

    @property
    def pending_count(self) -> int:
        """How many queue items remain *besides* the currently-assigned one."""
        if self._current is None:
            return len(self._queue)
        current_label = self._current.key.target_symbol
        return sum(1 for it in self._queue if it.label != current_label)

    @property
    def current(self) -> QueueAssignment | None:
        return self._current

    # ----- assignment / transition --------------------------------------

    def select_next(self, *, is_present_in_file: Callable[[str], bool]) -> QueueItem | None:
        return select_next_item(self._queue, is_present_in_file=is_present_in_file)

    def assign(
        self,
        item: QueueItem,
        *,
        active_file: str = "",
        slice_text: str = "",
        prepare: PrepareState | None = None,
    ) -> Transition:
        """Make `item` the current assignment, atomically.

        This is the only entry point that mutates ``current``. It also clears
        the outgoing theorem's retry counters so warning-cleanup state can
        never leak across boundaries — that bug is what drove duplicate
        warning-cleanup increments in the old code (``_review_agent_final_report``
        at native_runner.py:1056 vs ``_manager_gate_for_queue_verification``
        at native_runner.py:1432).
        """
        if active_file:
            self.set_active_file(active_file)
        new_key = TheoremKey.make(item.label, self._pending_active_file)
        if not new_key.is_valid():
            raise QueueInvariantError(f"cannot assign: invalid key from item={item!r}")
        self._remember_display_file(new_key, self._pending_active_file)

        previous_key = self._current.key if self._current else None
        if previous_key is not None and previous_key != new_key:
            # Transition: drop retry counters belonging to the outgoing theorem.
            self._warning_retries.pop(previous_key, None)
            self._hard_retries.pop(previous_key, None)
            self._last_verification = None

        self._current = QueueAssignment(
            key=new_key,
            slice=slice_text or "",
            prepare=prepare or PrepareState(success=False),
        )
        return Transition(previous=previous_key, current=new_key)

    def peek_assignment(
        self,
        item: QueueItem,
        *,
        active_file: str = "",
        slice_text: str = "",
        prepare: PrepareState | None = None,
    ) -> "TheoremQueueManager":
        """Return a view copy with `item` assigned, without mutating self."""
        copy = TheoremQueueManager(
            warning_retry_limit=self._warning_retry_limit,
            hard_retry_limit=self._hard_retry_limit,
            failed_attempt_history=self._failed_attempt_history,
            reasoning_escalation_threshold=self._reasoning_escalation_threshold,
        )
        copy._queue = list(self._queue)
        copy._current = self._current
        copy._attempts = list(self._attempts)
        copy._display_files = dict(self._display_files)
        copy._warning_retries = dict(self._warning_retries)
        copy._hard_retries = dict(self._hard_retries)
        copy._retry_signatures = {key: list(value) for key, value in self._retry_signatures.items()}
        copy._outcomes = dict(self._outcomes)
        copy._last_verification = self._last_verification
        copy._disabled_tool_reasons = dict(self._disabled_tool_reasons)
        copy._reasoning_effort_by_key = dict(self._reasoning_effort_by_key)
        copy._pending_active_file = self._pending_active_file
        copy.assign(item, active_file=active_file, slice_text=slice_text, prepare=prepare)
        return copy

    def clear_assignment(self) -> Transition | None:
        """Clear the current assignment and theorem-local retry state."""
        previous_key = self._current.key if self._current else None
        if previous_key is None:
            return None
        self.clear_retries_for(previous_key)
        self._current = None
        return Transition(previous=previous_key, current=None)

    def set_active_file(self, active_file: str) -> None:
        self._pending_active_file = _normalize_path(active_file)

    def detect_transition(
        self,
        *,
        candidate_target: str,
        candidate_file: str,
    ) -> Transition | None:
        """Return a transition record iff the candidate differs from current.

        Replaces ``_queue_assignment_transition`` ([native_runner.py:2815](epflemma_cli/native_runner.py:2815))
        but the comparison goes through ``TheoremKey.make`` so file-path
        normalization can never drift between this check and the retry-key
        lookups (today they share the same intent but different code paths).
        """
        candidate = TheoremKey.make(candidate_target, candidate_file)
        if not candidate.is_valid():
            return None
        previous = self._current.key if self._current else None
        if previous is None or previous == candidate:
            return None
        return Transition(previous=previous, current=candidate)

    # ----- failed attempts ---------------------------------------------

    def record_attempt(self, *, cycle: int, proof_shape: str, reason: str) -> FailedAttempt | None:
        """Append a failed attempt scoped to the current assignment.

        Replaces ``_remember_failed_attempt`` and fixes the budget-exhaustion
        race at [native_runner.py:3670](epflemma_cli/native_runner.py:3670):
        the legacy code recorded the attempt against ``live_state`` *before*
        restoring ``current_queue_assignment`` to the original snapshot, so
        the recorded snapshot could describe a different theorem than the one
        being restored. Here we always record against ``self._current``;
        callers cannot mismatch.
        """
        if self._current is None:
            return None
        return self.record_attempt_for(
            self._current.key,
            cycle=cycle,
            proof_shape=proof_shape,
            reason=reason,
        )

    def record_attempt_for(
        self,
        key: TheoremKey,
        *,
        cycle: int,
        proof_shape: str,
        reason: str,
    ) -> FailedAttempt | None:
        """Append a failed attempt for an explicit theorem key."""
        if not key.is_valid():
            return None
        self._remember_display_file(key, self._display_file_for(key))
        attempt = FailedAttempt(
            key=key,
            attempt=self.attempt_count_for(key) + 1,
            cycle=cycle,
            proof_shape=(proof_shape or "").strip(),
            reason=(reason or "").strip(),
        )
        if not attempt.reason:
            return None
        self._attempts.append(attempt)
        self._prune_attempts()
        return attempt

    def attempts_for(self, key: TheoremKey) -> tuple[FailedAttempt, ...]:
        return tuple(a for a in self._attempts if a.key == key)

    def attempt_entries_for(self, key: TheoremKey) -> tuple[dict[str, Any], ...]:
        return tuple(self._attempt_to_mapping(a) for a in self.attempts_for(key))

    def attempt_count_for(self, key: TheoremKey) -> int:
        attempts = self.attempts_for(key)
        numbered = [a.attempt for a in attempts if a.attempt > 0]
        if numbered:
            return max(numbered)
        return len(attempts)

    def attempts_for_current(self) -> int:
        if self._current is None:
            return 0
        return len(self.attempts_for(self._current.key))

    def clear_attempts_for(self, key: TheoremKey) -> None:
        if not key.is_valid():
            return
        self._attempts = [attempt for attempt in self._attempts if attempt.key != key]

    def _prune_attempts(self) -> None:
        """Keep at most `failed_attempt_history` attempts per (theorem, file)."""
        if not self._attempts:
            return
        per_key: dict[TheoremKey, list[FailedAttempt]] = {}
        kept: list[FailedAttempt] = []
        # Walk newest -> oldest, keeping up to N per key, then re-sort.
        for attempt in reversed(self._attempts):
            bucket = per_key.setdefault(attempt.key, [])
            if len(bucket) < self._failed_attempt_history:
                bucket.append(attempt)
                kept.append(attempt)
        self._attempts = list(reversed(kept))

    # ----- manager retry counters --------------------------------------

    def warning_retries_for_current(self) -> int:
        if self._current is None:
            return 0
        return self._warning_retries.get(self._current.key, 0)

    def warning_retries_for(self, key: TheoremKey) -> int:
        return self._warning_retries.get(key, 0) if key.is_valid() else 0

    def hard_retries_for_current(self) -> int:
        if self._current is None:
            return 0
        return self._hard_retries.get(self._current.key, 0)

    def hard_retries_for(self, key: TheoremKey) -> int:
        return self._hard_retries.get(key, 0) if key.is_valid() else 0

    def retry_count_for(self, key: TheoremKey, kind: str) -> int:
        normalized = self._retry_bucket(kind)
        if normalized == "warning":
            return self.warning_retries_for(key)
        if normalized == "hard":
            return self.hard_retries_for(key)
        return 0

    def consume_warning_retry(self) -> int:
        """Increment the warning-cleanup counter for the current assignment.

        Spec ("focused warning-cleanup opportunity"): one shot per theorem.
        The legacy code incremented this in two places without a single
        source of truth (``_review_agent_final_report`` at native_runner.py:1104
        and ``_manager_gate_for_queue_verification`` at native_runner.py:1432);
        callers in the new world only ever call *this* method.
        """
        if self._current is None:
            return 0
        key = self._current.key
        new_value = self._warning_retries.get(key, 0) + 1
        self._warning_retries[key] = new_value
        return new_value

    def consume_hard_retry(self) -> int:
        if self._current is None:
            return 0
        key = self._current.key
        new_value = self._hard_retries.get(key, 0) + 1
        self._hard_retries[key] = new_value
        return new_value

    @staticmethod
    def _retry_bucket(kind: str) -> str:
        normalized = str(kind or "").strip().lower()
        if normalized == "warning":
            return "warning"
        if normalized in {"hard", "error", "sorry"}:
            return "hard"
        return ""

    def consume_retry_once_for(self, key: TheoremKey, *, kind: str, signature: str = "") -> int:
        """Consume a retry for an explicit key, idempotently by signature."""
        bucket = self._retry_bucket(kind)
        if not key.is_valid() or not bucket:
            return 0
        if signature:
            consumed_key = (key, bucket)
            seen = list(self._retry_signatures.get(consumed_key, []))
            if signature in seen:
                return self.retry_count_for(key, bucket)
            seen.append(signature)
            self._retry_signatures[consumed_key] = seen[-20:]
        if bucket == "warning":
            new_value = self._warning_retries.get(key, 0) + 1
            self._warning_retries[key] = new_value
            return new_value
        new_value = self._hard_retries.get(key, 0) + 1
        self._hard_retries[key] = new_value
        return new_value

    def consume_retry_once(self, *, kind: str, signature: str = "") -> int:
        if self._current is None:
            return 0
        return self.consume_retry_once_for(self._current.key, kind=kind, signature=signature)

    def clear_retries_for(self, key: TheoremKey) -> None:
        if not key.is_valid():
            return
        self._warning_retries.pop(key, None)
        self._hard_retries.pop(key, None)
        for retry_key in list(self._retry_signatures):
            if retry_key[0] == key:
                self._retry_signatures.pop(retry_key, None)

    def clear_all_retries_except(self, key: TheoremKey) -> None:
        if not key.is_valid():
            self._warning_retries.clear()
            self._hard_retries.clear()
            self._retry_signatures.clear()
            return
        self._warning_retries = {k: v for k, v in self._warning_retries.items() if k == key}
        self._hard_retries = {k: v for k, v in self._hard_retries.items() if k == key}
        self._retry_signatures = {
            retry_key: signatures
            for retry_key, signatures in self._retry_signatures.items()
            if retry_key[0] == key
        }

    def warning_retry_exhausted(self) -> bool:
        return self.warning_retries_for_current() >= self._warning_retry_limit

    def hard_retry_exhausted(self) -> bool:
        return self.hard_retries_for_current() >= self._hard_retry_limit

    # ----- classification & decision -----------------------------------

    def classify(self, check: ManagerCheck) -> Classification:
        return classify_check(check)

    def decide(self, check: ManagerCheck) -> "Decision":
        """High-level branch policy for one manager gate.

        This is where the spec's step 7 ("Branch on the classification")
        lives in one place. The runner just calls ``decide(...)`` and follows
        the returned action; today this logic is open-coded across
        ``_review_agent_final_report``, ``_manager_gate_for_queue_verification``,
        and the budget-exhaustion path, which is why they can disagree.
        """
        cls = self.classify(check)
        if cls is Classification.HARD_BLOCKER:
            count = self.consume_hard_retry()
            if count >= self._hard_retry_limit:
                return Decision(
                    action="restore_baseline",
                    classification=cls,
                    reason="hard retry limit reached; restore baseline sorry and continue",
                )
            return Decision(
                action="continue_same_theorem",
                classification=cls,
                reason="hard blocker; record failed attempt and feed manager note",
            )
        if cls is Classification.WARNING_ONCE:
            if self.warning_retry_exhausted():
                return Decision(
                    action="advance_queue",
                    classification=Classification.ACCEPT,
                    reason="warning-cleanup opportunity already spent; accept",
                )
            self.consume_warning_retry()
            return Decision(
                action="continue_same_theorem",
                classification=cls,
                reason="grant the one focused warning-cleanup opportunity",
            )
        if cls is Classification.FUTURE_ONLY:
            return Decision(
                action="advance_queue",
                classification=cls,
                reason="assigned declaration clean; remaining work is future queue items",
            )
        # ACCEPT
        return Decision(
            action="advance_queue",
            classification=cls,
            reason="assigned declaration clean and no warnings",
        )

    # ----- verification record -----------------------------------------

    def record_verification(self, record: VerificationRecord) -> None:
        """Replace the regex-derived ``build_status`` with a typed record.

        Every place that previously read ``live_state["build_status"]``
        should read ``self.last_verification`` instead. If we have not run
        an authoritative check since the last edit, ``last_verification``
        may be ``None`` and the handoff renderer should say "no recent
        verification" rather than fabricating a string.
        """
        self._last_verification = record

    @property
    def last_verification(self) -> VerificationRecord | None:
        return self._last_verification

    def invalidate_verification(self) -> None:
        """Drop the cached verification record (e.g. after the agent edits)."""
        self._last_verification = None

    # ----- outcomes -----------------------------------------------------

    def record_outcome(
        self,
        *,
        status: str,
        note: str = "",
        build_status: str = "",
        verification: VerificationRecord | None = None,
    ) -> TheoremOutcome | None:
        if self._current is None:
            return None
        outcome = TheoremOutcome(
            key=self._current.key,
            status=(status or "unknown").strip(),
            note=(note or "").strip(),
            build_status=(build_status or "").strip(),
            verification=verification or self._last_verification,
        )
        self._outcomes[self._current.key] = outcome
        return outcome

    def outcome_for(self, key: TheoremKey) -> TheoremOutcome | None:
        return self._outcomes.get(key)

    def record_outcome_for(
        self,
        key: TheoremKey,
        *,
        status: str,
        note: str = "",
        build_status: str = "",
        verification: VerificationRecord | None = None,
    ) -> TheoremOutcome | None:
        if not key.is_valid():
            return None
        outcome = TheoremOutcome(
            key=key,
            status=(status or "unknown").strip(),
            note=(note or "").strip(),
            build_status=(build_status or "").strip(),
            verification=verification or self._last_verification,
        )
        self._outcomes[key] = outcome
        return outcome

    @property
    def outcomes(self) -> Mapping[TheoremKey, TheoremOutcome]:
        return dict(self._outcomes)

    # ----- disabled-tool tracking (P0.4 in the plan) -------------------

    def disable_tool(self, name: str, reason: str = "") -> None:
        """Record that a tool was disabled for the rest of the run.

        The runner reads ``disabled_tools`` to (a) drop the tool from the
        next API call's schema and (b) annotate the prompt. Today the disable
        is invisible to the model and wastes one API step per re-attempt
        (lean_auto_try in the GaussTest log: 6 wasted steps).
        """
        if name:
            self._disabled_tool_reasons[str(name)] = str(reason or "")

    @property
    def disabled_tools(self) -> frozenset[str]:
        return frozenset(self._disabled_tool_reasons)

    def disabled_tool_entries(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"name": name, "reason": reason}
            for name, reason in sorted(self._disabled_tool_reasons.items())
        )

    # ----- reasoning effort (replaces _resolve_managed_reasoning_config core) -

    def reasoning_effort_for_current(self) -> str:
        """Return the managed theorem-queue default reasoning effort."""
        return "high"

    def remembered_reasoning_effort_for(self, key: TheoremKey) -> str:
        return self._reasoning_effort_by_key.get(key, "high") if key.is_valid() else "high"

    def remember_reasoning_effort_for(self, key: TheoremKey, effort: str) -> str:
        previous = self.remembered_reasoning_effort_for(key)
        if key.is_valid() and effort:
            self._reasoning_effort_by_key[key] = str(effort)
        return previous

    # ----- invariant checks (debug; opt-in) ----------------------------

    def check_invariants(self) -> None:
        """Cheap invariants pinned to spec lines.

        The runner can call this after every state mutation when
        ``EPFLEMMA_QUEUE_INVARIANT_CHECKS=1`` is set. Cheap to run, loud
        when broken, silent in production.
        """
        if self._current is not None:
            key = self._current.key
            if not key.is_valid():
                raise QueueInvariantError("current assignment has empty key")
            # Retry counters must belong to the current key only.
            stale_warn = [k for k in self._warning_retries if k != key]
            if stale_warn:
                raise QueueInvariantError(
                    f"warning-retry counters survived a transition: {stale_warn!r}"
                )
            stale_hard = [k for k in self._hard_retries if k != key]
            if stale_hard:
                raise QueueInvariantError(
                    f"hard-retry counters survived a transition: {stale_hard!r}"
                )
            stale_signatures = [retry_key for retry_key in self._retry_signatures if retry_key[0] != key]
            if stale_signatures:
                raise QueueInvariantError(
                    f"retry signatures survived a transition: {stale_signatures!r}"
                )

    # ----- (de)serialization -------------------------------------------
    #
    # Resume / checkpoint compatibility: the legacy autonomy_state dict is
    # the persisted format, so we read/write it directly. Once the manager
    # is in place we can migrate to a typed JSON schema.

    @classmethod
    def from_autonomy_state(cls, autonomy_state: Mapping[str, Any]) -> "TheoremQueueManager":
        mgr = cls()

        assignment = dict(autonomy_state.get("current_queue_assignment") or {})
        if assignment:
            key = TheoremKey.make(
                str(assignment.get("target_symbol", "") or ""),
                str(assignment.get("active_file", "") or ""),
            )
            if key.is_valid():
                mgr._remember_display_file(key, str(assignment.get("active_file", "") or ""))
                mgr._current = QueueAssignment(
                    key=key,
                    slice=str(assignment.get("slice", "") or ""),
                    prepare=PrepareState.from_mapping(assignment.get("incremental_prepare")),
                )

        for raw in autonomy_state.get("failed_attempts", []) or []:
            if not isinstance(raw, Mapping):
                continue
            key = TheoremKey.make(
                str(raw.get("target_symbol", "") or ""),
                str(raw.get("active_file", "") or ""),
            )
            if not key.is_valid():
                continue
            mgr._remember_display_file(key, str(raw.get("active_file", "") or ""))
            mgr._attempts.append(
                FailedAttempt(
                    key=key,
                    attempt=int(raw.get("attempt", 0) or 0),
                    cycle=int(raw.get("cycle", 0) or 0),
                    proof_shape=str(raw.get("proof_shape", "") or ""),
                    reason=str(raw.get("reason", "") or ""),
                )
            )

        # Legacy store keyed retries by f"{file}::{target}" string with kind
        # buckets {"warning": N, "hard": M}.
        retries = autonomy_state.get("manager_feedback_retries") or {}
        if isinstance(retries, Mapping):
            for storage_key, entry in retries.items():
                if not isinstance(entry, Mapping):
                    continue
                file_part, _, target_part = str(storage_key).partition("::")
                key = TheoremKey.make(target_part, file_part)
                if not key.is_valid():
                    continue
                mgr._remember_display_file(key, file_part)
                w = int(entry.get("warning", 0) or 0)
                h = int(entry.get("hard", entry.get("error", entry.get("sorry", 0))) or 0)
                if w:
                    mgr._warning_retries[key] = w
                if h:
                    mgr._hard_retries[key] = h

        consumed = autonomy_state.get("manager_feedback_retry_consumed_signatures") or {}
        if isinstance(consumed, Mapping):
            for raw_key, raw_signatures in consumed.items():
                parts = str(raw_key).rsplit("::", 2)
                if len(parts) != 3:
                    continue
                storage_key, raw_kind = f"{parts[0]}::{parts[1]}", parts[2]
                file_part, _, target_part = storage_key.partition("::")
                key = TheoremKey.make(target_part, file_part)
                bucket = cls._retry_bucket(raw_kind)
                if not key.is_valid() or not bucket:
                    continue
                mgr._remember_display_file(key, file_part)
                signatures = [str(value) for value in list(raw_signatures or []) if str(value).strip()]
                if signatures:
                    mgr._retry_signatures[(key, bucket)] = signatures[-20:]

        outcomes = autonomy_state.get("theorem_outcomes") or {}
        if isinstance(outcomes, Mapping):
            for storage_key, raw in outcomes.items():
                if not isinstance(raw, Mapping):
                    continue
                file_part, _, target_part = str(storage_key).partition("::")
                key = TheoremKey.make(target_part, file_part)
                if not key.is_valid():
                    continue
                mgr._remember_display_file(key, str(raw.get("active_file", "") or file_part))
                mgr._outcomes[key] = TheoremOutcome(
                    key=key,
                    status=str(raw.get("status", "") or "unknown"),
                    note=str(raw.get("note", "") or ""),
                    build_status=str(raw.get("build_status", "") or ""),
                    verification=verification_from_mapping(raw.get("last_verification")),
                )

        mgr._last_verification = verification_from_mapping(autonomy_state.get("last_verification"))

        disabled_tools = autonomy_state.get("disabled_tools_this_run")
        if isinstance(disabled_tools, Iterable) and not isinstance(disabled_tools, (str, bytes)):
            for raw in disabled_tools:
                if isinstance(raw, Mapping):
                    mgr.disable_tool(str(raw.get("name", "") or ""), str(raw.get("reason", "") or ""))
                else:
                    mgr.disable_tool(str(raw or ""))

        reasoning_efforts = autonomy_state.get("reasoning_effort_by_theorem") or {}
        if isinstance(reasoning_efforts, Mapping):
            for storage_key, effort in reasoning_efforts.items():
                file_part, _, target_part = str(storage_key).partition("::")
                key = TheoremKey.make(target_part, file_part)
                if key.is_valid():
                    mgr._reasoning_effort_by_key[key] = str(effort or "high")

        return mgr

    def _attempt_to_mapping(self, attempt: FailedAttempt) -> dict[str, Any]:
        return {
            "target_symbol": attempt.key.target_symbol,
            "active_file": self._display_file_for(attempt.key),
            "attempt": attempt.attempt,
            "cycle": attempt.cycle,
            "proof_shape": attempt.proof_shape,
            "reason": attempt.reason,
        }

    def _outcome_to_mapping(self, outcome: TheoremOutcome) -> dict[str, Any]:
        payload = {
            "target_symbol": outcome.key.target_symbol,
            "active_file": self._display_file_for(outcome.key),
            "status": outcome.status,
            "note": outcome.note,
            "build_status": outcome.build_status,
        }
        verification = verification_to_mapping(outcome.verification)
        if verification:
            payload["last_verification"] = verification
        return payload

    def to_autonomy_state(self) -> dict[str, Any]:
        """Render back into the legacy dict format for backward-compatible
        on-disk checkpoints. Once the runner uses the manager natively, the
        on-disk schema can switch to a typed payload."""
        out: dict[str, Any] = {}

        if self._current is not None:
            out["current_queue_assignment"] = {
                "target_symbol": self._current.key.target_symbol,
                "active_file": self._display_file_for(self._current.key),
                "slice": self._current.slice,
                "incremental_prepare": {
                    "success": self._current.prepare.success,
                    "ok": self._current.prepare.ok,
                    "elapsed_s": self._current.prepare.elapsed_s,
                    "cache": dict(self._current.prepare.cache),
                    "error": self._current.prepare.error,
                },
            }

        if self._attempts:
            out["failed_attempts"] = [
                self._attempt_to_mapping(a) for a in self._attempts
            ]

        if self._warning_retries or self._hard_retries:
            retries: dict[str, dict[str, int]] = {}
            for key, value in self._warning_retries.items():
                retries.setdefault(key.storage_key(), {})["warning"] = value
            for key, value in self._hard_retries.items():
                retries.setdefault(key.storage_key(), {})["hard"] = value
            out["manager_feedback_retries"] = retries
        if self._retry_signatures:
            out["manager_feedback_retry_consumed_signatures"] = {
                f"{key.storage_key()}::{kind}": list(signatures[-20:])
                for (key, kind), signatures in self._retry_signatures.items()
                if key.is_valid() and signatures
            }

        if self._outcomes:
            out["theorem_outcomes"] = {
                f"{self._display_file_for(key)}::{key.target_symbol}": self._outcome_to_mapping(outcome)
                for key, outcome in self._outcomes.items()
            }

        if self._last_verification is not None:
            out["last_verification"] = verification_to_mapping(self._last_verification)

        if self._disabled_tool_reasons:
            out["disabled_tools_this_run"] = list(self.disabled_tool_entries())

        if self._reasoning_effort_by_key:
            out["reasoning_effort_by_theorem"] = {
                key.storage_key(): effort
                for key, effort in self._reasoning_effort_by_key.items()
                if key.is_valid() and effort
            }

        return out


@dataclass(frozen=True)
class Decision:
    """Result of :meth:`TheoremQueueManager.decide`."""

    action: str            # "continue_same_theorem" | "advance_queue" | "restore_baseline"
    classification: Classification
    reason: str

    def advances_queue(self) -> bool:
        return self.action == "advance_queue"

    def keeps_theorem(self) -> bool:
        return self.action in ("continue_same_theorem", "restore_baseline")
