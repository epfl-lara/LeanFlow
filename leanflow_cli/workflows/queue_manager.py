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

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# Pure value types, tunables, and mapping/classification helpers now live in
# ``queue_models``; re-exported here so existing callers and tests keep
# resolving ``leanflow_cli.workflows.queue_manager.<name>``.
# ---------------------------------------------------------------------------
from leanflow_cli.workflows.queue_models import (  # noqa: E402
    DEFAULT_FAILED_ATTEMPT_HISTORY,
    DEFAULT_HARD_RETRY_LIMIT,
    DEFAULT_POST_EDIT_HARD_RETRY_LIMIT,
    DEFAULT_REASONING_ESCALATION_THRESHOLD,
    DEFAULT_WARNING_RETRY_LIMIT,
    Classification,
    DecisionContext,
    DecisionSource,
    FailedAttempt,
    ManagerCheck,
    PrepareState,
    QueueAssignment,
    QueueItem,
    TheoremKey,
    TheoremOutcome,
    Transition,
    VerificationRecord,
    VerificationScope,  # noqa: F401
    _fold_cleanup_reason,
    _normalize_path,
    classify_check,
    select_next_item,
    verification_from_mapping,
    verification_to_mapping,
)

# ---------------------------------------------------------------------------
# The class
# ---------------------------------------------------------------------------


class QueueInvariantError(AssertionError):
    """Raised by ``check_invariants`` when a spec invariant is violated.

    Callers can run with ``LEANFLOW_QUEUE_INVARIANT_CHECKS=1`` to turn these
    on. Off by default so production never crashes on a paranoid check.
    """


def _normalize_attempt_gate_verdict(verdict: str) -> str:
    """Return a bounded canonical gate verdict without losing tail differences."""
    normalized = " ".join((verdict or "").split()).casefold()
    if len(normalized) <= 500:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
    return f"{normalized[:420]}... [sha256:{digest}]"


def _has_failed_attempt_evidence(reason: str) -> bool:
    """Return whether a reason represents failure rather than a successful tool result.

    Older checkpoints can contain the JSON payload of a successful non-verification
    tool call because the runner once treated every incremental-check action as
    theorem feedback.  Plain-text reasons remain valid failure evidence; only an
    unambiguously successful structured result is rejected.
    """
    normalized = (reason or "").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered.startswith("target:") and " passed | tool:" in lowered:
        return False
    try:
        payload = json.loads(normalized)
    except (TypeError, ValueError, json.JSONDecodeError):
        if normalized.startswith("{"):
            explicit_failure_markers = (
                '"success": false',
                '"ok": false',
                '"status": "blocked"',
                '"status": "error"',
                '"status": "fail"',
                '"status": "failed"',
                '"status": "timeout"',
                '"has_errors": true',
                '"has_sorry": true',
            )
            return any(marker in lowered for marker in explicit_failure_markers)
        return True
    if not isinstance(payload, Mapping):
        return True

    status = str(payload.get("status", "") or "").strip().lower()
    has_error = bool(str(payload.get("error", "") or "").strip())
    has_degraded_reason = bool(payload.get("degraded_reasons"))
    explicitly_failed = (
        payload.get("success") is False
        or payload.get("ok") is False
        or status in {"blocked", "error", "fail", "failed", "timeout"}
        or has_error
        or has_degraded_reason
    )
    if explicitly_failed:
        return True
    return False


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
            "theorem_api_steps",
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
        post_edit_hard_retry_limit: int = DEFAULT_POST_EDIT_HARD_RETRY_LIMIT,
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
        self._api_steps: dict[TheoremKey, int] = {}  # cumulative, never pruned
        self._outcomes: dict[TheoremKey, TheoremOutcome] = {}
        self._last_verification: VerificationRecord | None = None
        self._disabled_tool_reasons: dict[str, str] = {}
        self._reasoning_effort_by_key: dict[TheoremKey, str] = {}
        self._pending_active_file = ""

        self._warning_retry_limit = warning_retry_limit
        self._hard_retry_limit = hard_retry_limit
        self._post_edit_hard_retry_limit = post_edit_hard_retry_limit
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
            it if isinstance(it, QueueItem) else QueueItem.from_mapping(it) for it in (items or [])
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

    def select_next(
        self,
        *,
        is_present_in_file: Callable[[str], bool],
        precedence: Callable[[str], int] | None = None,
        order_key: Callable[[str], Any] | None = None,
    ) -> QueueItem | None:
        return select_next_item(
            self._queue,
            is_present_in_file=is_present_in_file,
            precedence=precedence,
            order_key=order_key,
        )

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
    ) -> TheoremQueueManager:
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

        Replaces ``_queue_assignment_transition`` ([native_runner.py:2815](leanflow_cli/native_runner.py:2815))
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

    def record_attempt(
        self,
        *,
        cycle: int,
        proof_shape: str,
        reason: str,
        declaration_hash: str = "",
        gate_verdict: str = "",
        turn_key: str = "",
    ) -> FailedAttempt | None:
        """Append a failed attempt scoped to the current assignment.

        Replaces ``_remember_failed_attempt`` and fixes the budget-exhaustion
        race at [native_runner.py:3670](leanflow_cli/native_runner.py:3670):
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
            declaration_hash=declaration_hash,
            gate_verdict=gate_verdict,
            turn_key=turn_key,
        )

    def record_attempt_for(
        self,
        key: TheoremKey,
        *,
        cycle: int,
        proof_shape: str,
        reason: str,
        declaration_hash: str = "",
        gate_verdict: str = "",
        turn_key: str = "",
    ) -> FailedAttempt | None:
        """Append one semantically distinct failure for an explicit theorem.

        A single provider turn can expose the same unchanged rejection through
        both a patch/diff result and a subsequent full-declaration check. The
        presentation is useful history, but it is not a second proof attempt.
        Suppress that duplicate when the theorem, exact declaration, normalized
        gate verdict, and provider-turn identity all match. Legacy callers and
        checkpoints omit the new identity fields and retain append-only behavior.
        """
        if not key.is_valid():
            return None
        self._remember_display_file(key, self._display_file_for(key))
        normalized_hash = (declaration_hash or "").strip().lower()
        normalized_verdict = _normalize_attempt_gate_verdict(gate_verdict)
        normalized_turn_key = (turn_key or "").strip()
        if normalized_hash and normalized_verdict and normalized_turn_key:
            duplicate = next(
                (
                    previous
                    for previous in reversed(self._attempts)
                    if previous.key == key
                    and previous.turn_key == normalized_turn_key
                    and previous.declaration_hash == normalized_hash
                    and previous.gate_verdict == normalized_verdict
                ),
                None,
            )
            if duplicate is not None:
                return None
        attempt = FailedAttempt(
            key=key,
            attempt=self.attempt_count_for(key) + 1,
            cycle=cycle,
            proof_shape=(proof_shape or "").strip(),
            reason=(reason or "").strip(),
            declaration_hash=normalized_hash,
            gate_verdict=normalized_verdict,
            turn_key=normalized_turn_key,
        )
        if not _has_failed_attempt_evidence(attempt.reason):
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

    def add_api_steps_for(self, key: TheoremKey, steps: int) -> int:
        """Accumulate spent API steps for a theorem across turns; return the total.

        Cumulative and never ring-pruned — the failed-attempt history caps at
        10 entries per key, which makes it unusable as a budget; this counter
        is the Phase 1 budget-breakpoint accounting.
        """
        if not key.is_valid() or steps <= 0:
            return self.api_steps_for(key)
        total = self._api_steps.get(key, 0) + int(steps)
        self._api_steps[key] = total
        return total

    def api_steps_for(self, key: TheoremKey) -> int:
        return self._api_steps.get(key, 0) if key.is_valid() else 0

    def reset_api_steps_for(self, key: TheoremKey) -> None:
        """Grant a fresh budget tranche (orchestrator route resumed the theorem)."""
        if key.is_valid():
            self._api_steps.pop(key, None)

    def retry_signatures_for(self, key: TheoremKey) -> dict[str, list[str]]:
        """Return the consumed retry signatures per bucket (decision-packet input)."""
        if not key.is_valid():
            return {}
        return {
            bucket: list(signatures)
            for (stored_key, bucket), signatures in self._retry_signatures.items()
            if stored_key == key and signatures
        }

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

    def _hard_retry_limit_for(self, source: DecisionSource) -> int:
        """Source-dependent hard-retry limits — legacy drift D2, kept as data.

        Final-report retries are full turns (limit 2); post-edit retries are
        cheap inner-loop checks (limit 8); verification-tool results consume
        nothing (0 = no consumption, no exhaustion). Harmonizing these is an
        owner-approved follow-up, not Phase 0.
        """
        if source is DecisionSource.FINAL_REPORT:
            return self._hard_retry_limit
        if source is DecisionSource.POST_EDIT:
            return self._post_edit_hard_retry_limit
        return 0

    def _signature_already_consumed(self, key: TheoremKey, bucket: str, signature: str) -> bool:
        return bool(signature) and signature in self._retry_signatures.get((key, bucket), [])

    def decide(self, ctx: DecisionContext) -> Decision:
        """Pure verdict policy for one manager gate — reads, never mutates.

        This is where the spec's step 7 ("Branch on the classification")
        lives in one place. The runner calls ``decide(...)``, renders the
        returned plan (messages, prints, activity), and commits its retry
        side effects via :meth:`apply_decision`; file restores and attempt
        recording stay runner-owned I/O, driven by the Decision fields.
        Purity makes shadow-compare safe: evaluating a legacy gate in shadow
        must never corrupt production retry counters.
        """
        check = _fold_cleanup_reason(ctx.check, ctx.cleanup_reason)
        cls = self.classify(check)
        if ctx.axiom_blockers:
            # Axiom-dependency veto (legacy Path A): a kernel-accepted proof
            # leaning on forbidden axioms is a hard blocker, never an accept.
            # (In production the veto only fires on otherwise-clean checks.)
            cls = Classification.HARD_BLOCKER
        key = self._current.key if self._current is not None else None

        if cls is Classification.HARD_BLOCKER:
            feedback_kind = "sorry" if check.has_assigned_sorry else "error"
            if ctx.source is DecisionSource.BUDGET_EXHAUSTION:
                return Decision(
                    action="restore_baseline",
                    classification=cls,
                    reason="api step budget exhausted while blocked; restore baseline sorry",
                    feedback_kind=feedback_kind,
                    record_failed_attempt=True,
                    restore_baseline=True,
                )
            if ctx.source is DecisionSource.LIVE_STATE:
                return Decision(
                    action="continue_same_theorem",
                    classification=cls,
                    reason="assignment still blocked per live state",
                    feedback_kind=feedback_kind,
                )
            limit = self._hard_retry_limit_for(ctx.source)
            count = self.retry_count_for(key, "hard") if key is not None else 0
            if limit and count >= limit:
                # Exhaustion is judged on the PRE-consumption count (pinned by
                # the boundary characterization tests).
                return Decision(
                    action="restore_baseline",
                    classification=cls,
                    reason=(
                        "local feedback window complete; restore baseline sorry and "
                        "continue on a new route"
                    ),
                    feedback_kind=feedback_kind,
                    retry_count=count,
                    retry_limit=limit,
                    restore_baseline=True,
                )
            consume = "hard" if limit else ""
            after = count
            if consume and key is not None:
                if not self._signature_already_consumed(key, "hard", ctx.signature):
                    after = count + 1
            return Decision(
                action="continue_same_theorem",
                classification=cls,
                reason="hard blocker; record failed attempt and feed manager note",
                feedback_kind=feedback_kind,
                consume_retry=consume,
                retry_count=after,
                retry_limit=limit,
                record_failed_attempt=ctx.source
                in (DecisionSource.POST_EDIT, DecisionSource.VERIFICATION_RESULT),
            )
        if cls is Classification.WARNING_ONCE:
            if ctx.source in (DecisionSource.LIVE_STATE, DecisionSource.BUDGET_EXHAUSTION):
                # Predicate-only sources: warning evidence never blocked the
                # legacy live-state probe and never triggers a budget restore;
                # they neither own nor consume the warning-cleanup window.
                return Decision(
                    action="advance_queue",
                    classification=cls,
                    reason="warning-only evidence; assignment not blocked",
                    feedback_kind="warning",
                )
            limit = self._warning_retry_limit
            count = self.retry_count_for(key, "warning") if key is not None else 0
            if count >= limit:
                # Opportunity already spent: accept (exhaustion is judged
                # BEFORE consuming — the inverse of the hard-blocker order).
                return Decision(
                    action="advance_queue",
                    classification=Classification.ACCEPT,
                    reason="warning-cleanup opportunity already spent; accept",
                    retry_count=count,
                    retry_limit=limit,
                    accepted_after_warning_limit=True,
                )
            after = count
            if key is not None and not self._signature_already_consumed(
                key, "warning", ctx.signature
            ):
                after = count + 1
            return Decision(
                action="continue_same_theorem",
                classification=cls,
                reason="grant the one focused warning-cleanup opportunity",
                feedback_kind="warning",
                consume_retry="warning",
                retry_count=after,
                retry_limit=limit,
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

    def apply_decision(self, ctx: DecisionContext, decision: Decision) -> Decision:
        """Commit a decision's retry side effects to this manager.

        Consumes the planned retry idempotently by ``ctx.signature`` and
        clears retry bookkeeping when the queue advances (accept). File
        restores and failed-attempt recording stay runner-owned (I/O).
        Returns the decision with ``retry_count`` reflecting the committed
        counter value.
        """
        if self._current is None:
            return decision
        key = self._current.key
        if decision.consume_retry:
            count = self.consume_retry_once_for(
                key, kind=decision.consume_retry, signature=ctx.signature
            )
            decision = replace(decision, retry_count=count)
        if decision.action == "advance_queue":
            self.clear_retries_for(key)
        return decision

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

    def discard_outcome_for(self, key: TheoremKey) -> TheoremOutcome | None:
        """Remove one obsolete theorem verdict without changing other knowledge."""
        return self._outcomes.pop(key, None) if key.is_valid() else None

    def retire_theorem_state(self, key: TheoremKey) -> bool:
        """Remove all scheduler state for a declaration deleted by the campaign.

        Authoritative false-decomposition cleanup preserves its mathematical
        negation in plan state, so queue-local proof attempts and verdicts for
        the now-absent helper are stale rather than useful campaign knowledge.
        """
        if not key.is_valid():
            return False
        changed = False
        filtered_queue = [item for item in self._queue if item.label != key.target_symbol]
        if len(filtered_queue) != len(self._queue):
            self._queue = filtered_queue
            changed = True
        if self._current is not None and self._current.key == key:
            self._current = None
            self._last_verification = None
            changed = True
        filtered_attempts = [attempt for attempt in self._attempts if attempt.key != key]
        if len(filtered_attempts) != len(self._attempts):
            self._attempts = filtered_attempts
            changed = True
        for storage in (
            self._display_files,
            self._warning_retries,
            self._hard_retries,
            self._api_steps,
            self._outcomes,
            self._reasoning_effort_by_key,
        ):
            if storage.pop(key, None) is not None:
                changed = True
        for retry_key in tuple(self._retry_signatures):
            if retry_key[0] == key:
                self._retry_signatures.pop(retry_key, None)
                changed = True
        return changed

    def reopen_blocked_outcomes(self, *, trigger: str) -> tuple[TheoremOutcome, ...]:
        """Return temporary route deferrals to unresolved queue work.

        ``deferred`` is the current non-terminal scheduler vocabulary. Legacy
        checkpoints may still carry ``blocked`` from before route exhaustion
        was separated from mathematical verdicts, so both are reopened. A
        campaign epoch or verified-knowledge refresh clears the temporary
        cooldown; solved, disproved, and operational campaign states remain
        untouched. Repeated calls are idempotent until a later proof turn
        records another deferred outcome.
        """
        refresh = (trigger or "strategy refresh").strip()
        reopened: list[TheoremOutcome] = []
        for key, outcome in tuple(self._outcomes.items()):
            if str(outcome.status or "").strip().lower() not in {"blocked", "deferred"}:
                continue
            prior_note = str(outcome.note or "").strip()
            note = f"{refresh}: reopened for a distinct proof route"
            if prior_note:
                note = f"{note}; prior blocker: {prior_note}"
            updated = replace(outcome, status="unresolved", note=note)
            self._outcomes[key] = updated
            reopened.append(updated)
        return tuple(reopened)

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

    # ----- disabled-tool tracking --------------------------------------

    def disable_tool(self, name: str, reason: str = "") -> None:
        """Record that a tool was disabled for the rest of the run.

        The runner reads ``disabled_tools`` to (a) drop the tool from the
        next API call's schema and (b) annotate the prompt. Today the disable
        is invisible to the model and wastes one API step per re-attempt
        (lean_auto_try in the test log: 6 wasted steps).
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
        ``LEANFLOW_QUEUE_INVARIANT_CHECKS=1`` is set. Cheap to run, loud
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
            stale_signatures = [
                retry_key for retry_key in self._retry_signatures if retry_key[0] != key
            ]
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
    def from_autonomy_state(cls, autonomy_state: Mapping[str, Any]) -> TheoremQueueManager:
        """Reconstruct queue state from a legacy autonomy_state dict checkpoint. Deserializes all internal state (current assignment, failed attempts, retry counters, outcomes, verification, disabled tools, reasoning efforts) with validation and path normalization for checkpoint resume compatibility."""
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

        dropped_attempt_numbers: dict[TheoremKey, list[int]] = {}
        for raw in autonomy_state.get("failed_attempts", []) or []:
            if not isinstance(raw, Mapping):
                continue
            key = TheoremKey.make(
                str(raw.get("target_symbol", "") or ""),
                str(raw.get("active_file", "") or ""),
            )
            if not key.is_valid():
                continue
            raw_attempt = int(raw.get("attempt", 0) or 0)
            reason = str(raw.get("reason", "") or "")
            if not _has_failed_attempt_evidence(reason):
                if raw_attempt > 0:
                    dropped_attempt_numbers.setdefault(key, []).append(raw_attempt)
                continue
            dropped_before = sum(
                1 for dropped in dropped_attempt_numbers.get(key, ()) if dropped <= raw_attempt
            )
            attempt_number = (
                max(1, raw_attempt - dropped_before) if raw_attempt > 0 else raw_attempt
            )
            mgr._remember_display_file(key, str(raw.get("active_file", "") or ""))
            mgr._attempts.append(
                FailedAttempt(
                    key=key,
                    attempt=attempt_number,
                    cycle=int(raw.get("cycle", 0) or 0),
                    proof_shape=str(raw.get("proof_shape", "") or ""),
                    reason=reason,
                    declaration_hash=str(raw.get("declaration_hash", "") or "").strip().lower(),
                    gate_verdict=_normalize_attempt_gate_verdict(
                        str(raw.get("gate_verdict", "") or "")
                    ),
                    turn_key=str(raw.get("turn_key", "") or "").strip(),
                )
            )

        # Cumulative per-theorem API-step totals (Phase 1 budget breakpoint):
        # keyed f"{file}::{target}" -> int, never ring-pruned.
        api_steps = autonomy_state.get("theorem_api_steps") or {}
        if isinstance(api_steps, Mapping):
            for storage_key, raw_total in api_steps.items():
                file_part, _, target_part = str(storage_key).partition("::")
                key = TheoremKey.make(target_part, file_part)
                total = int(raw_total or 0)
                if key.is_valid() and total > 0:
                    mgr._remember_display_file(key, file_part)
                    mgr._api_steps[key] = total

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
                signatures = [
                    str(value) for value in list(raw_signatures or []) if str(value).strip()
                ]
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
                    mgr.disable_tool(
                        str(raw.get("name", "") or ""), str(raw.get("reason", "") or "")
                    )
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
        payload = {
            "target_symbol": attempt.key.target_symbol,
            "active_file": self._display_file_for(attempt.key),
            "attempt": attempt.attempt,
            "cycle": attempt.cycle,
            "proof_shape": attempt.proof_shape,
            "reason": attempt.reason,
        }
        if attempt.declaration_hash:
            payload["declaration_hash"] = attempt.declaration_hash
        if attempt.gate_verdict:
            payload["gate_verdict"] = attempt.gate_verdict
        if attempt.turn_key:
            payload["turn_key"] = attempt.turn_key
        return payload

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
            out["failed_attempts"] = [self._attempt_to_mapping(a) for a in self._attempts]

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
        if self._api_steps:
            out["theorem_api_steps"] = {
                key.storage_key(): total
                for key, total in self._api_steps.items()
                if key.is_valid() and total > 0
            }

        if self._outcomes:
            out["theorem_outcomes"] = {
                f"{self._display_file_for(key)}::{key.target_symbol}": self._outcome_to_mapping(
                    outcome
                )
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

    def to_checkpoint_state(self) -> dict[str, Any]:
        """Return durable queue knowledge safe to hydrate in a new process.

        Process-local verifier state must not cross a runner restart: a new
        LeanInteract server needs its own warmup, the last verification may
        describe an older source revision, and disabled tools are scoped to
        the process that observed their failure.  The assignment identity,
        failed proof shapes, retry signatures, outcomes, and accounting are
        durable campaign knowledge and are preserved.
        """
        state = self.to_autonomy_state()
        state.pop("last_verification", None)
        state.pop("disabled_tools_this_run", None)
        assignment = state.get("current_queue_assignment")
        if isinstance(assignment, dict):
            assignment["incremental_prepare"] = {
                "success": False,
                "ok": False,
                "elapsed_s": 0.0,
                "cache": {},
                "error": "runner restart requires fresh warmup",
            }
        return state


@dataclass(frozen=True)
class Decision:
    """Result of :meth:`TheoremQueueManager.decide` — the action plus the
    side-effect plan (retry to consume, attempt to record, restore to run).

    ``decide()`` is pure; :meth:`TheoremQueueManager.apply_decision` commits
    the retry plan, and the runner performs the I/O the flags call for.
    """

    # "continue_same_theorem" | "advance_queue" | "restore_baseline" |
    # "budget_breakpoint" (Phase 1 flag-gated hook; unused while flags are off)
    action: str
    classification: Classification
    reason: str
    feedback_kind: str = ""  # legacy adapter string: "sorry" | "error" | "warning" | ""
    consume_retry: str = ""  # "" | "warning" | "hard" — what apply_decision() consumes
    retry_count: int = 0  # count AFTER the pending consumption (for prompt rendering)
    retry_limit: int = 0  # source-dependent limit (drift D2 encoded as data)
    record_failed_attempt: bool = False  # runner records via _remember_failed_attempt
    accepted_after_warning_limit: bool = False  # legacy wording preserved
    restore_baseline: bool = False  # runner restores the baseline `sorry` slice

    def advances_queue(self) -> bool:
        return self.action == "advance_queue"

    def keeps_theorem(self) -> bool:
        return self.action in ("continue_same_theorem", "restore_baseline")
