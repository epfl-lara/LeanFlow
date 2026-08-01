"""Define immutable theorem-queue values and classification helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tunables (lifted from native_runner so callers can keep their env-var knobs)
# ---------------------------------------------------------------------------

DEFAULT_FAILED_ATTEMPT_HISTORY = 10
DEFAULT_REASONING_ESCALATION_THRESHOLD = 5
DEFAULT_WARNING_RETRY_LIMIT = 1
DEFAULT_HARD_RETRY_LIMIT = 2
# Post-edit gates are cheap inner-loop checks, so they get a much longer leash
# than final-report gates (native_runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT).
DEFAULT_POST_EDIT_HARD_RETRY_LIMIT = 8


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
    def make(cls, target_symbol: str, active_file: str) -> TheoremKey:
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
    kind: str = ""  # "theorem" / "lemma" / "example" / ...
    line: int = 0
    end_line: int = 0
    reasons: tuple[str, ...] = ()  # e.g. ("contains sorry", "diagnostic near line 100")
    blocker_signature: str = ""
    search_hints: tuple[str, ...] = ()
    verification_gate: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> QueueItem:
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

    def has_golf_reason(self) -> bool:
        """Golf candidates (managed /golf, Phase 6) — their own bucket:
        prove queues never emit this reason, so prove selection is
        byte-identical."""
        return any("golf candidate" in str(reason).lower() for reason in self.reasons)

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
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PrepareState:
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
    TARGET = "target"  # lean_incremental_check(check_target)
    FILE_EXACT = "file_exact"  # lean_verify(mode=file_exact)
    MODULE = "module"  # lake build <Module>
    PROJECT = "project"  # lake build
    INSPECT = "inspect"  # lean_inspect refresh (not a real verification)


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
    tool: str = ""  # "lean_incremental_check" / "lake build" / ...
    target: str = ""  # for TARGET scope
    cache: str = ""  # "warm" / "cold" / "rebuilt"
    elapsed_s: float = 0.0
    lean_command_elapsed_s: float = 0.0
    probe_wall_elapsed_s: float = 0.0
    tool_wall_elapsed_s: float = 0.0
    errors: int = 0
    warnings: int = 0
    sorry_count: int = 0
    summary: str = ""  # short single-line for handoff rendering
    axiom_profile_checked: bool = False
    axiom_profile_axioms: tuple[str, ...] = ()
    axiom_profile_blockers: tuple[str, ...] = ()
    source_revision_sha256: str = ""


def verification_from_mapping(raw: Mapping[str, Any] | None) -> VerificationRecord | None:
    """Parse a legacy verification dict into a typed record."""
    if not isinstance(raw, Mapping) or not raw:
        return None
    try:
        raw_axiom_blockers = raw.get("axiom_profile_blockers") or []
        raw_axioms = raw.get("axiom_profile_axioms") or []
        axiom_profile_axioms: tuple[str, ...]
        if isinstance(raw_axioms, (str, bytes)):
            axiom_profile_axioms = (str(raw_axioms),)
        else:
            axiom_profile_axioms = tuple(str(item) for item in raw_axioms)
        axiom_profile_blockers: tuple[str, ...]
        if isinstance(raw_axiom_blockers, (str, bytes)):
            axiom_profile_blockers = (str(raw_axiom_blockers),)
        else:
            axiom_profile_blockers = tuple(str(item) for item in raw_axiom_blockers)
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
            lean_command_elapsed_s=float(raw.get("lean_command_elapsed_s", 0.0) or 0.0),
            probe_wall_elapsed_s=float(raw.get("probe_wall_elapsed_s", 0.0) or 0.0),
            tool_wall_elapsed_s=float(raw.get("tool_wall_elapsed_s", 0.0) or 0.0),
            errors=int(raw.get("errors", 0) or 0),
            warnings=int(raw.get("warnings", 0) or 0),
            sorry_count=int(raw.get("sorry", raw.get("sorry_count", 0)) or 0),
            summary=str(raw.get("summary", "") or ""),
            axiom_profile_checked=raw.get("axiom_profile_checked") is True,
            axiom_profile_axioms=axiom_profile_axioms,
            axiom_profile_blockers=axiom_profile_blockers,
            source_revision_sha256=str(raw.get("source_revision_sha256", "") or ""),
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
    payload = {
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
    for key, value in (
        ("lean_command_elapsed_s", record.lean_command_elapsed_s),
        ("probe_wall_elapsed_s", record.probe_wall_elapsed_s),
        ("tool_wall_elapsed_s", record.tool_wall_elapsed_s),
    ):
        if value:
            payload[key] = value
    if record.axiom_profile_checked or record.axiom_profile_axioms or record.axiom_profile_blockers:
        payload["axiom_profile_checked"] = record.axiom_profile_checked
        payload["axiom_profile_axioms"] = list(record.axiom_profile_axioms)
        payload["axiom_profile_blockers"] = list(record.axiom_profile_blockers)
    if record.source_revision_sha256:
        payload["source_revision_sha256"] = record.source_revision_sha256
    return payload


# ---------------------------------------------------------------------------
# Manager classification (one classifier, not two)
# ---------------------------------------------------------------------------


class Classification(str, Enum):
    HARD_BLOCKER = "hard_blocker"  # error / open goals / assigned-decl sorry
    WARNING_ONCE = "warning_once"  # warning-only, opportunity not yet spent
    ACCEPT = "accept"  # clean, OR warnings after opportunity
    FUTURE_ONLY = "future_only"  # no diagnostics on assigned decl


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
    verification_failed: bool = False  # explicit ok=False from a real check
    raw_messages: tuple[str, ...] = ()  # unstructured fallback (lake stderr etc.)


class DecisionSource(str, Enum):
    """Which production gate is asking for a verdict.

    The retry policy is deliberately source-dependent (final-report retries are
    full turns, post-edit retries are cheap inner-loop checks, verification-tool
    results consume nothing) — encoding the source keeps ``decide()`` able to
    reproduce every legacy branch byte-for-byte.
    """

    FINAL_REPORT = "final_report"
    POST_EDIT = "post_edit"  # patch / write_file / apply_verified_patch trigger
    VERIFICATION_RESULT = "verification_result"  # lean_verify / incremental / terminal
    LIVE_STATE = "live_state"  # synthetic evidence from the live-state probe
    BUDGET_EXHAUSTION = "budget_exhaustion"


@dataclass(frozen=True)
class DecisionContext:
    """Everything one manager gate knows, source-tagged for ``decide()``."""

    source: DecisionSource
    check: ManagerCheck
    signature: str = ""  # retry-idempotency signature ("" = consume unconditionally)
    cleanup_reason: str = ""  # local warning-cleanup reason ("" = none)
    axiom_blockers: tuple[str, ...] = ()  # forbidden-axiom dependencies (vetoes accept)
    claims_success: bool = True  # final-report success-claim regex gate result


@dataclass(frozen=True)
class FailedAttempt:
    """Record one semantically distinct kernel rejection within a prover turn."""

    key: TheoremKey
    attempt: int  # 1-indexed within (theorem, file)
    cycle: int  # workflow cycle number
    proof_shape: str  # short text: snippet of the body or its diff
    reason: str  # short text: blocker summary
    declaration_hash: str = ""  # exact declaration content at the gate
    gate_verdict: str = ""  # normalized kernel-gate rejection
    turn_key: str = ""  # restart-safe provider-turn identity


@dataclass(frozen=True)
class TheoremOutcome:
    key: TheoremKey
    status: str  # "solved" / "unresolved" / "deferred" / legacy "blocked"
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
            self.previous is not None and self.current is not None and self.previous != self.current
        )


# ---------------------------------------------------------------------------
# Pure helpers (no state, easy to unit-test)
# ---------------------------------------------------------------------------


#: Precedence rank at or above which an item is avoided while any
#: better-ranked candidate exists (graph dependency false/blocked/parked).
PRECEDENCE_AVOID = 2
#: Absolute exclusion rank for authoritatively false or human-paused nodes.
#: Unlike a blocked route, these items must not be retried merely because the
#: rest of the queue is also difficult.
PRECEDENCE_EXCLUDE = 3


def select_next_item(
    queue: Sequence[QueueItem],
    *,
    is_present_in_file: Callable[[str], bool],
    precedence: Callable[[str], int] | None = None,
    order_key: Callable[[str], Any] | None = None,
) -> QueueItem | None:
    """Spec rule (line 519 of product-reference): error diagnostics first,
    then ``sorry`` placeholders, then nothing else.

    This deliberately drops the legacy "third fallback" in
    ``_current_queue_item`` ([native_runner.py:2718](leanflow_cli/native_runner.py:2718))
    that returned the first declaration found in the file even with no
    diagnostics or sorry — that path could select a clean declaration and
    silently violate the spec. If neither bucket matches, return None and let
    the caller treat the queue as empty so the final-sweep path runs.

    Phase 4 graph-frontier option: ``precedence`` maps an item label to a
    rank — 0 = frontier-ready (dependencies proved), 1 = unknown (including
    project-scope file-path labels), >=2 = avoid (a dependency is
    blocked), 3 = exclude (authoritatively false or human-paused). Ranks order
    candidates stably WITHIN each bucket (the diagnostic-first bucket rule is
    about unblocking compilation and stays authoritative); rank-2 items are
    excluded only while a better-ranked candidate exists somewhere, so a
    queue of only blocked items still proves. Rank-3 items are never selected.
    ``None`` is the byte-identical legacy path.

    Phase 5 curriculum option: ``order_key`` breaks ties WITHIN the best
    precedence rank of a bucket (easy->hard ordering — smaller keys first);
    it can never override the bucket rule or the precedence ranks, and
    ``None`` keeps the stable file-order tie-break.
    """
    if not queue:
        return None
    if precedence is None and order_key is None:
        for item in queue:
            if item.label and is_present_in_file(item.label) and item.has_diagnostic_reason():
                return item
        for item in queue:
            if item.label and is_present_in_file(item.label) and item.has_sorry_reason():
                return item
        for item in queue:
            if item.label and is_present_in_file(item.label) and item.has_golf_reason():
                return item
        return None
    rank_fn = precedence

    def _rank(item: QueueItem) -> int:
        if rank_fn is None:
            return 1  # uniform rank; order_key decides the ties
        try:
            return int(rank_fn(item.label))
        except Exception:
            return 1

    diagnostic = [
        item
        for item in queue
        if item.label and is_present_in_file(item.label) and item.has_diagnostic_reason()
    ]
    sorry = [
        item
        for item in queue
        if item.label and is_present_in_file(item.label) and item.has_sorry_reason()
    ]
    golf = [
        item
        for item in queue
        if item.label and is_present_in_file(item.label) and item.has_golf_reason()
    ]
    ranks = {id(item): _rank(item) for item in (*diagnostic, *sorry, *golf)}

    order = {id(item): index for index, item in enumerate(queue)}

    def _curriculum_pick(contenders: list[QueueItem]) -> QueueItem:
        # All-or-nothing: if ANY key fails to compute or the keys do not
        # compare, the WHOLE pick falls back to file order — a partial
        # failure must never invert the ordering between items.
        in_file_order = min(contenders, key=lambda item: order[id(item)])
        if order_key is None:
            return in_file_order
        try:
            keyed = sorted(
                ((order_key(item.label), order[id(item)], item) for item in contenders),
                key=lambda triple: (triple[0], triple[1]),
            )
            return keyed[0][2]
        except Exception:
            return in_file_order

    def _pick(bucket: list[QueueItem]) -> QueueItem | None:
        # Avoid-exclusion is PER BUCKET: the diagnostic-first rule stays
        # authoritative, so a rank-2 diagnostic still outranks any sorry
        # item and is only skipped for a better diagnostic candidate.
        if not bucket:
            return None
        bucket = [item for item in bucket if ranks[id(item)] < PRECEDENCE_EXCLUDE]
        if not bucket:
            return None
        if any(ranks[id(item)] < PRECEDENCE_AVOID for item in bucket):
            bucket = [item for item in bucket if ranks[id(item)] < PRECEDENCE_AVOID]
        best = min(ranks[id(item)] for item in bucket)
        return _curriculum_pick([item for item in bucket if ranks[id(item)] == best])

    return _pick(diagnostic) or _pick(sorry) or _pick(golf)


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
    if check.has_assigned_sorry or check.has_assigned_error or check.has_assigned_open_goals:
        return Classification.HARD_BLOCKER
    if check.has_assigned_warning:
        return Classification.WARNING_ONCE
    if check.verification_failed:
        # Verification failed but assigned decl is clean -> errors live in
        # other declarations; spec calls this FUTURE_ONLY.
        return Classification.FUTURE_ONLY
    return Classification.ACCEPT


def _fold_cleanup_reason(check: ManagerCheck, cleanup_reason: str) -> ManagerCheck:
    """OR a local-cleanup reason into the check's evidence flags.

    Mirrors the legacy ``local_cleanup_reason`` routing inside
    ``_manager_check_for_feedback_kind`` (sorry-wording -> assigned sorry;
    error-wording -> assigned error; anything else -> assigned warning).
    OR-idempotent, so it is safe whether or not the caller already encoded
    the reason into the check.
    """
    lowered = str(cleanup_reason or "").strip().lower()
    if not lowered:
        return check
    if "sorry" in lowered:
        return replace(check, has_assigned_sorry=True)
    if any(token in lowered for token in ("error", "unsolved", "failed")):
        return replace(check, has_assigned_error=True)
    return replace(check, has_assigned_warning=True)
