"""Pure value types and mapping/classification helpers for the theorem queue.

Extracted verbatim from ``epflemma_cli.queue_manager`` so the bookkeeping
class can stay focused on runtime state. Everything here is intentionally
pure: frozen dataclasses, enums, legacy dict<->typed mappers, and stateless
helpers with no I/O, no logging, and no module-level mutable state. The
``TheoremQueueManager`` class in ``queue_manager`` imports these names back,
and ``queue_manager`` re-exports them so existing callers and tests keep
resolving ``epflemma_cli.queue_manager.<name>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
