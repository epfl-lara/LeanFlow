"""Tracked, lineage-addressed synchronous and process-isolated job dispatch.

One shared lifecycle for every dispatch level: propose → deploy → running →
{done | failed | stuck | killed}, persisted in the ``dispatch_ledger`` key of
``summary.json`` and mirrored as one ``dispatch-job`` activity event per state
transition. Metadata-only ledger mutations remain silent. Every ledger
mutation runs inside a single read-validate-write TRANSACTION (an
in-process lock plus a checked-and-retried cross-process file lock with a
per-process/thread owner id), and every state change is applied against the
PERSISTED entry — a deploy that lost a race to ``kill``/``reconcile`` keeps
the terminal verdict instead of resurrecting the job.

Jobs carry independent budgets and dotted lineage ids. ``deploy`` preserves
the synchronous compatibility path; ``deploy_async`` runs the same backend in
a dedicated subprocess and persists a bounded result artifact for the parent
to harvest through ``poll`` or ``join``.

Correctness invariants: deliverables are consumed once and never as raw
transcripts; prover-job disk edits are re-verified by the PARENT's
deterministic checker before any graph effect; ``reconcile`` favors agent
evidence over ledger optimism — a live pid is live evidence, and a job with
no evidence is only ``stuck`` after the two-clause patience test — so a job
can never be silently lost (N1: ``open_jobs()`` is the loud audit).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.providers.isolated_auxiliary import sanitize_auxiliary_error
from core.constants import WORKFLOW_STEP_BOUNDARY_INTERRUPT
from core.process_identity import (
    PROCESS_TOKEN_ENV,
    ProcessIdentity,
    process_identity_from_mapping,
    process_identity_matches,
    process_token_sha256,
)
from core.provider_availability import normalize_provider_retry_after
from core.provider_capacity import (
    BACKGROUND_PROVIDER_CAPACITY_ENV,
    background_provider_capacity,
)
from core.utils import atomic_json_write
from leanflow_cli.runtime.file_locks import acquire_file_lock, release_file_lock
from leanflow_cli.workflows import (
    dispatch_incremental_evidence,
    dispatch_ledger_compaction,
    research_route_context,
)
from leanflow_cli.workflows.dispatch_models import (
    MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY,
    TERMINAL_STATES,
    JobSpec,
    LedgerEntry,
    descendants,
    is_ancestor,
    next_job_id,
)
from leanflow_cli.workflows.workflow_json_io import json_write_lock, read_json_file
from leanflow_cli.workflows.workflow_state import (
    _process_seems_alive,  # noqa: F401 - historical monkeypatch surface
    append_workflow_activity,
    summarize_workflow_agents,
    terminate_workflow_agent,
    terminate_workflow_agent_descendants,
)
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root
from tools.utilities.delegate_handoff import HANDOFF_KIND

logger = logging.getLogger(__name__)

try:  # POSIX launch locks; in-process locking remains the non-POSIX fallback.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

_LEDGER_KEY = "dispatch_ledger"


class MathematicalDeltaReservationConflict(ValueError):
    """Report the open job that won an exact-assignment delta reservation."""

    def __init__(self, *, winning_job_id: str, delta_signature: str):
        self.winning_job_id = str(winning_job_id)
        self.delta_signature = str(delta_signature)
        super().__init__(
            "mathematical delta already reserved by open job "
            f"{self.winning_job_id!r}: {self.delta_signature}"
        )


class DispatchLaunchAdmissionDeferred(RuntimeError):
    """Report that a policy-neutral launch guard rejected process creation."""


# A job is stuck only when both the wall clock is well past
# the declared budget AND the activity stream has gone quiet — the second
# clause protects a long Lake build whose events keep the stream fresh.
PATIENCE_WALL_CLOCK_FACTOR = 1.5
PATIENCE_MIN_QUIET_S = 600
PATIENCE_QUIET_FACTOR = 0.25
# A worker can finish its backend and briefly fail an exact process-identity
# probe while its finally-block closes subprocess trees before publishing the
# atomic result artifact.  Keep this short: it is paid only at an apparent
# process exit and prevents a false failure/replacement from outracing output.
ASYNC_RESULT_PUBLICATION_GRACE_S = 3.0
ASYNC_RESULT_RECHECK_INTERVAL_S = 0.1
# ``deployed`` is the durable async-launch transaction state. A worker
# publishes its nonce-bound exact identity immediately on process entry; only
# a missing identity beyond this short handshake window can be retried.
ASYNC_LAUNCH_HANDSHAKE_GRACE_S = 5.0
ASYNC_LAUNCH_MAX_ATTEMPTS = 3
ASYNC_LAUNCH_TERMINATION_GRACE_S = 2.0
ASYNC_LAUNCH_TERMINATION_POLL_S = 0.05
WALL_CLOCK_TERMINATION_PENDING_NOTE = (
    "wall-clock budget exhausted; worker termination pending exact process exit"
)
WALL_CLOCK_EXIT_CONFIRMED_NOTE = "wall-clock budget exhausted; worker process exit confirmed"
PROCESS_EXIT_UNCONFIRMED_NOTE = "worker identity unavailable; awaiting exact process exit evidence"
DELIVERABLE_STRING_CAP = 4000
DELIVERABLE_JSON_CAP = 32000
DELEGATE_ERROR_DETAIL_CAP = 1000
CHECKED_REPLACEMENT_TOOL = "lean_incremental_check"
CHECKED_HELPERS_KEY = "checked_helpers"
CHECKED_HELPER_STATUS = "worker_checked_parent_recheck_required"
DISPATCH_WORKER_MODULE = "leanflow_cli.native.dispatch_worker"
PROCESS_ARGV_MAX_BYTES = 1024 * 1024
SAFE_LEGACY_PROCESS_RELEASE_REASONS = frozenset(
    {
        "legacy-process-exited",
        "legacy-process-command-mismatch",
        "legacy-dispatch-worker-spec-mismatch",
        "process-command-mismatch",
        "dispatch-worker-spec-mismatch",
    }
)
DARWIN_UNAMBIGUOUS_PROCESS_ARG_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,\-]+$")
MAX_CHECKED_HELPERS = 8
CHECKED_REPLACEMENT_CONTRACT = (
    '"checked_replacements":[{"target_symbol":"exact Lean declaration",'
    '"replacement":"full exact target declaration checked inline",'
    '"worker_check":{"tool":"lean_incremental_check",'
    '"valid_without_sorry":true,"has_errors":false,"has_sorry":false,'
    '"replacement_matches_target":true}}]'
)
DECOMPOSITION_REPORT_SCHEMA_VERSION = 1
DECOMPOSITION_DEPENDENCY_KINDS = frozenset({"depends_on", "split_of"})
DECOMPOSITION_SOURCE_KINDS = frozenset(
    {"local", "mathlib", "web", "proof_state", "research_finding"}
)
DECOMPOSITION_MAX_SOURCES = 4
DECOMPOSITION_MAX_SUBGOALS = 5
DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS = 8
DECOMPOSITION_MAX_REFERENCES = 4
DECOMPOSITION_IDENTIFIER_CAP = 80
DECOMPOSITION_REFERENCE_CAP = 400
DECOMPOSITION_SOURCE_SUMMARY_CAP = 250
DECOMPOSITION_STATEMENT_CAP = 700
DECOMPOSITION_PURPOSE_CAP = 250
DECOMPOSITION_DIFFICULTY_CAP = 300
DECOMPOSITION_RATIONALE_CAP = 400
DECOMPOSITION_CONTRACT_ISSUES_CAP = 24
SCRATCH_ARCHETYPE_TOOLSET_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "prover": ("lean-research",),
    "empirical": ("lean-research", "empirical-compute"),
    "deep_search": ("web-research", "lean-research"),
    "negation_probe": ("lean-research",),
    "decomposition": ("web-research", "lean-research"),
}
SCRATCH_TOOLSET_ALIASES = {
    "lean": "lean-research",
    "web": "web-research",
}
DECOMPOSITION_REPORT_CONTRACT = (
    '"decomposition_report":{"source_basis":[{"id":"src-1","kind":"local|mathlib|web|'
    'proof_state|research_finding","reference":"exact declaration, URL, or finding id",'
    '"summary":"what this source supports"}],"subgoals":[{"id":"sg-1",'
    '"statement":"exact Lean proposition or precise candidate statement",'
    '"purpose":"why it helps","source_refs":["src-1"],"dependencies":[],'
    '"difficulty_reduction":"why this is strictly easier"}],"dependency_proposals":'
    '[{"source":"sg-1","target":"target","kind":"split_of",'
    '"rationale":"how the implication is assembled","source_refs":["src-1"]}]}'
)
DECOMPOSITION_REPORT_DURABLE_CAPS = (
    "Durable numeric caps (hard limits): "
    f"source_basis: at most {DECOMPOSITION_MAX_SOURCES} records; "
    f"subgoals: at most {DECOMPOSITION_MAX_SUBGOALS} records; "
    "dependency_proposals: at most "
    f"{DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS} records; source_refs or dependencies: "
    f"at most {DECOMPOSITION_MAX_REFERENCES} ids. Field character caps: identifiers "
    f"{DECOMPOSITION_IDENTIFIER_CAP}, source references {DECOMPOSITION_REFERENCE_CAP}, "
    f"source summaries {DECOMPOSITION_SOURCE_SUMMARY_CAP}, statements "
    f"{DECOMPOSITION_STATEMENT_CAP}, purposes {DECOMPOSITION_PURPOSE_CAP}, difficulty "
    f"reductions {DECOMPOSITION_DIFFICULTY_CAP}, rationales {DECOMPOSITION_RATIONALE_CAP}."
)
DECOMPOSITION_TARGET_SENTINEL_PROMPT = (
    "In dependency_proposals, represent the requested theorem only with the literal JSON "
    'string `target`; use `"target"` as the target value and never the theorem name.'
)
_INTERRUPTED_RESULT_STATUSES = frozenset({"canceled", "cancelled", "interrupted", "killed"})
_INTERRUPTED_ERROR_TYPES = frozenset(
    {"InterruptedError", "KeyboardInterrupt", "NativeTerminationSignal"}
)
_ASYNC_LAUNCH_LOCKS_GUARD = threading.Lock()
_ASYNC_LAUNCH_LOCKS: dict[str, threading.RLock] = {}


def dispatch_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_DISPATCH_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def dispatch_max_concurrent() -> int:
    try:
        value = int(str(os.getenv("LEANFLOW_DISPATCH_MAX_CONCURRENT", "") or "").strip() or 3)
    except ValueError:
        value = 3
    return max(1, value)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _worker_interruption_reason(*, worker_ok: bool, status: str, error: str) -> str:
    """Return a stable reason when a worker artifact records cancellation."""
    normalized_status = status.strip().lower()
    if worker_ok and normalized_status in _INTERRUPTED_RESULT_STATUSES:
        return f"result status {normalized_status}"
    error_type = error.partition(":")[0].strip().rsplit(".", 1)[-1]
    if not worker_ok and error_type in _INTERRUPTED_ERROR_TYPES:
        return error_type
    return ""


def _bounded_delegate_error(value: Any) -> str:
    """Return a compact provider/backend error suitable for durable state."""
    if isinstance(value, Mapping):
        text = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    elif isinstance(value, (list, tuple)):
        text = json.dumps(list(value), ensure_ascii=False, default=str)
    else:
        text = str(value or "")
    return sanitize_auxiliary_error(text, limit=DELEGATE_ERROR_DETAIL_CAP)


def _result_error_detail(result: Mapping[str, Any]) -> str:
    """Return the bounded operational cause retained by one worker result."""
    for key in ("error", "error_detail"):
        detail = _bounded_delegate_error(result.get(key))
        if detail:
            return detail
    return ""


def _worker_result_failure_note(status: str, result: Mapping[str, Any]) -> str:
    """Build an activity-visible failure note without copying unbounded output."""
    safe_status = sanitize_auxiliary_error(status, limit=100) or "[missing]"
    prefix = f"worker result status: {safe_status}"
    detail = _result_error_detail(result)
    return (f"{prefix}: {detail}" if detail else prefix)[:300]


def patience_exceeded(
    *, started_at: str, wall_clock_s: int, now: datetime, last_event_age_s: float | None
) -> bool:
    """The two-clause stuck test over a running entry's declared budget."""
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    over_wall = (now - started).total_seconds() > PATIENCE_WALL_CLOCK_FACTOR * wall_clock_s
    quiet_floor = max(PATIENCE_MIN_QUIET_S, PATIENCE_QUIET_FACTOR * wall_clock_s)
    gone_quiet = last_event_age_s is None or last_event_age_s > quiet_floor
    return over_wall and gone_quiet


def _delegate_toolsets(spec: JobSpec) -> list[str] | None:
    """Return an explicit archetype-bounded tool surface for scratch jobs.

    A delegated child treats both ``None`` and ``[]`` as permission to inherit a
    broader parent/default surface. Scratch jobs therefore always receive a
    non-empty explicit allowlist, even when an older persisted spec omitted its
    toolsets. A non-empty but entirely disallowed request is rejected instead of
    silently expanding to another surface.
    """
    configured = list(spec.toolsets)
    if not spec.scope.get("scratch_only"):
        return configured or None
    allowed = SCRATCH_ARCHETYPE_TOOLSET_ALLOWLISTS.get(spec.archetype)
    if not allowed:
        raise RuntimeError(
            f"scratch dispatch archetype {spec.archetype!r} has no delegated toolset allowlist"
        )
    if not configured:
        return list(allowed)
    restricted: list[str] = []
    for name in configured:
        mapped = SCRATCH_TOOLSET_ALIASES.get(name, name)
        if mapped not in allowed:
            continue
        if mapped not in restricted:
            restricted.append(mapped)
    if not restricted:
        raise RuntimeError(
            f"scratch dispatch job {spec.job_id!r} requested no toolsets permitted for "
            f"archetype {spec.archetype!r}"
        )
    return restricted


def wall_clock_exceeded(*, started_at: str, wall_clock_s: int, now: datetime) -> bool:
    """Return whether a process-isolated worker exhausted its hard budget."""
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    return (now - started).total_seconds() > max(0, wall_clock_s)


def _descendant_process_ids(process_id: int) -> list[int]:
    """Return descendant PIDs deepest-first, including new-session children."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    discovered: list[tuple[int, int]] = []
    stack = [(process_id, 0)]
    seen = {process_id}
    while stack:
        parent, depth = stack.pop()
        for child in children.get(parent, []):
            if child in seen:
                continue
            seen.add(child)
            discovered.append((depth + 1, child))
            stack.append((child, depth + 1))
    return [pid for _depth, pid in sorted(discovered, reverse=True)]


def _signal_process_group(process_id: int, signal_number: int) -> bool:
    """Signal a worker and descendants that opened their own process groups."""
    descendants = _descendant_process_ids(process_id)
    signaled = False
    try:
        os.killpg(process_id, signal_number)
    except (OSError, ProcessLookupError):
        pass
    else:
        signaled = True
    for child in descendants:
        try:
            os.kill(child, signal_number)
        except (OSError, ProcessLookupError):
            continue
        signaled = True
    return signaled


def _terminate_process_group(process_id: int) -> bool:
    """Terminate a worker and descendants that opened their own process groups."""
    return _signal_process_group(process_id, signal.SIGTERM)


def _dispatch_process_identity_is_live(entry: LedgerEntry) -> bool:
    """Return whether a running dispatch worker still owns its persisted PID."""
    return process_identity_matches(entry.process_identity())


def _terminate_dispatch_process(entry: LedgerEntry) -> bool:
    """Terminate one dispatch worker only after exact identity revalidation."""
    if not _dispatch_process_identity_is_live(entry):
        return False
    return _terminate_process_group(entry.process_id)


def _dispatch_process_identity_has_exited(entry: LedgerEntry) -> bool:
    """Return whether the persisted process boundary is provably gone.

    Token lookup deliberately fails closed for signaling, so a false identity
    match alone is not exit evidence. Only a missing PID or changed POSIX
    process/session boundary proves the old worker can no longer be running.
    """
    if entry.process_id <= 1:
        return True
    try:
        process_group_id = os.getpgid(entry.process_id)
        session_id = os.getsid(entry.process_id)
    except ProcessLookupError:
        return True
    except (AttributeError, PermissionError):
        return False
    except OSError as exc:
        return exc.errno == errno.ESRCH
    if entry.process_group_id > 0 and process_group_id != entry.process_group_id:
        return True
    return bool(entry.process_session_id > 0 and session_id != entry.process_session_id)


def _process_started_at_utc(process_id: int) -> datetime | None:
    """Return one live POSIX process start time for diagnostics only.

    ``ps lstart`` is available on the supported macOS and Linux hosts. Force
    the C locale so parsing cannot vary with the operator environment; a
    failed or ambiguous lookup remains absent. This timestamp never authorizes
    capacity release because wall-clock changes and DST ambiguity make it
    unsuitable as process-ownership evidence.
    """
    if process_id <= 1:
        return None
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        completed = subprocess.run(
            ["ps", "-p", str(process_id), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        rendered = completed.stdout.strip()
        if completed.returncode != 0 or not rendered:
            return None
        local_struct = time.strptime(rendered, "%a %b %d %H:%M:%S %Y")
        return datetime.fromtimestamp(time.mktime(local_struct), tz=UTC)
    except (OSError, OverflowError, subprocess.SubprocessError, ValueError):
        return None


def _read_linux_process_argv(
    process_id: int,
    *,
    proc_root: Path = Path("/proc"),
) -> tuple[str, ...] | None:
    """Return exact NUL-delimited Linux argv or fail closed.

    A missing, oversized, unterminated, empty, or otherwise ambiguous cmdline
    is not mismatch evidence. The caller separately rechecks process absence.
    """
    if process_id <= 1:
        return None
    path = proc_root / str(process_id) / "cmdline"
    try:
        with path.open("rb") as handle:
            raw = handle.read(PROCESS_ARGV_MAX_BYTES + 1)
    except OSError:
        return None
    if not raw or len(raw) > PROCESS_ARGV_MAX_BYTES or not raw.endswith(b"\0"):
        return None
    fields = raw[:-1].split(b"\0")
    if not fields or any(not field for field in fields):
        return None
    return tuple(os.fsdecode(field) for field in fields)


def _read_darwin_process_argv(process_id: int) -> tuple[str, ...] | None:
    """Return full-width Darwin process argv when its rendering is unambiguous."""
    if process_id <= 1:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(process_id), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rendered = completed.stdout.strip()
    if (
        completed.returncode != 0
        or not rendered
        or len(rendered.encode("utf-8", errors="replace")) > PROCESS_ARGV_MAX_BYTES
        or len(rendered.splitlines()) != 1
        or "\x00" in rendered
        or (rendered.startswith("(") and rendered.endswith(")"))
    ):
        return None
    try:
        argv = tuple(shlex.split(rendered, posix=True))
    except ValueError:
        return None
    return argv if argv and all(argv) else None


def _read_process_argv(
    process_id: int,
    *,
    expected_spec_path: str,
) -> tuple[str, ...] | None:
    """Return exact-enough argv on supported hosts or fail closed."""
    if sys.platform.startswith("linux"):
        return _read_linux_process_argv(process_id)
    if sys.platform == "darwin":
        # BSD ``ps`` renders a string rather than NUL-delimited argv. A spec
        # path containing shell-sensitive characters cannot be reconstructed
        # unambiguously from that presentation.
        if DARWIN_UNAMBIGUOUS_PROCESS_ARG_RE.fullmatch(expected_spec_path) is None:
            return None
        return _read_darwin_process_argv(process_id)
    return None


def _dispatch_artifact_stem(job_id: str) -> str:
    """Return the filesystem-safe stem shared by dispatch artifacts."""
    return "".join(ch if ch.isalnum() or ch in {"-", "."} else "_" for ch in job_id)


def _dispatch_job_spec_path(job_id: str) -> str:
    """Return the exact job-global spec path passed to a dispatch worker."""
    path = workflow_state_root() / "dispatch-jobs" / f"{_dispatch_artifact_stem(job_id)}.spec.json"
    return str(path.expanduser().resolve(strict=False))


def _canonical_absolute_process_path(value: str) -> str:
    """Return one canonical absolute argv path or empty when ambiguous."""
    raw = str(value or "")
    if not raw or "\x00" in raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return ""
    return str(path.resolve(strict=False))


def _dispatch_worker_spec_from_argv(argv: Sequence[str]) -> tuple[bool, str]:
    """Return whether argv is a dispatch worker and its unambiguous spec path."""
    module_indices = [
        index
        for index in range(max(0, len(argv) - 1))
        if argv[index] == "-m" and argv[index + 1] == DISPATCH_WORKER_MODULE
    ]
    if not module_indices:
        return False, ""
    if len(module_indices) != 1:
        return True, ""
    candidates: list[tuple[int, str]] = []
    for index, argument in enumerate(argv):
        if argument == "--spec-file":
            if index + 1 >= len(argv):
                return True, ""
            candidates.append((index, str(argv[index + 1])))
        elif argument.startswith("--spec-file="):
            candidates.append((index, argument.partition("=")[2]))
    if len(candidates) != 1:
        return True, ""
    spec_index, spec_path = candidates[0]
    if module_indices[0] >= spec_index:
        return True, ""
    return True, _canonical_absolute_process_path(spec_path)


def _argv_sha256(argv: Sequence[str]) -> str:
    """Return a non-secret digest of exact process arguments."""
    encoded = b"\0".join(os.fsencode(str(argument)) for argument in argv)
    return hashlib.sha256(encoded).hexdigest()


def _legacy_process_exit_evidence(entry: LedgerEntry) -> dict[str, str]:
    """Return canonical durable evidence for a missing legacy process."""
    reason = "legacy-process-exited"
    evidence = {
        "version": "2",
        "job_id": entry.spec.job_id,
        "process_id": str(entry.process_id),
        "reason": reason,
    }
    return {
        "reason": reason,
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "observed_started_at": "",
    }


def _legacy_terminal_process_release_evidence(entry: LedgerEntry) -> dict[str, str]:
    """Return fail-closed evidence that one killed worker no longer owns its PID.

    Process absence is authoritative. For a reused live PID, only an exact
    command-line mismatch from this job's known dispatch-worker invocation is
    authoritative. Modern token-bound rows reach this fallback only after the
    token no longer matches, so an unrelated process reusing the same PID,
    process group, and session cannot pin a killed campaign forever.
    """
    if entry.state != "killed":
        return {}
    if _dispatch_process_identity_has_exited(entry):
        return _legacy_process_exit_evidence(entry)
    modern_identity = bool(entry.launch_nonce or entry.process_identity().verifiable)
    if modern_identity and _dispatch_process_identity_is_live(entry):
        return {}

    expected_spec_path = _dispatch_job_spec_path(entry.spec.job_id)
    argv = _read_process_argv(
        entry.process_id,
        expected_spec_path=expected_spec_path,
    )
    if argv is None:
        # Close the observation race without interpreting lookup failure as
        # mismatch evidence.
        if _dispatch_process_identity_has_exited(entry):
            return _legacy_process_exit_evidence(entry)
        return {}
    dispatch_worker, observed_spec_path = _dispatch_worker_spec_from_argv(argv)
    if dispatch_worker and not observed_spec_path:
        return {}
    if dispatch_worker and observed_spec_path == expected_spec_path:
        return {}

    if modern_identity:
        reason = "dispatch-worker-spec-mismatch" if dispatch_worker else "process-command-mismatch"
    else:
        reason = (
            "legacy-dispatch-worker-spec-mismatch"
            if dispatch_worker
            else "legacy-process-command-mismatch"
        )
    observed_started = _process_started_at_utc(entry.process_id)
    observed_started_at = observed_started.isoformat() if observed_started is not None else ""
    evidence = {
        "version": "2",
        "job_id": entry.spec.job_id,
        "process_id": str(entry.process_id),
        "reason": reason,
        "expected_spec_path_sha256": hashlib.sha256(expected_spec_path.encode("utf-8")).hexdigest(),
        "observed_argv_sha256": _argv_sha256(argv),
        "observed_spec_path_sha256": (
            hashlib.sha256(observed_spec_path.encode("utf-8")).hexdigest()
            if observed_spec_path
            else ""
        ),
    }
    return {
        "reason": reason,
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "observed_started_at": observed_started_at,
    }


def _process_release_report_key(
    entry: LedgerEntry,
    *,
    reason: str,
    evidence_sha256: str,
) -> str:
    """Return one deterministic activity key for a durable release verdict."""
    payload = "\0".join(
        (
            entry.spec.job_id,
            str(entry.process_id),
            entry.finished_at,
            str(reason),
            str(evidence_sha256),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"research-portfolio-capacity-released:{digest}"


def _wait_for_dispatch_process_exit(entry: LedgerEntry, *, timeout_s: float) -> bool:
    """Wait until one exact worker's POSIX boundary is provably gone."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while not _dispatch_process_identity_has_exited(entry):
        # Reap when this recovery process happens to own the child. A restarted
        # runner gets ChildProcessError and waits for the orphan's real parent.
        _reap_process(entry.process_id, block=False)
        if _dispatch_process_identity_has_exited(entry):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(ASYNC_LAUNCH_TERMINATION_POLL_S, max(0.0, deadline - time.monotonic())))
    return True


def _terminate_dispatch_process_and_wait(entry: LedgerEntry) -> bool:
    """Synchronously retire one exact stale launch before replacement.

    Revalidate ownership before TERM and again before KILL so PID reuse can
    never redirect escalation. Returning false means the exact identity could
    not be proven gone; callers must leave the durable launch in place rather
    than overlap it with a replacement worker.
    """
    if _dispatch_process_identity_has_exited(entry):
        return True
    if not _dispatch_process_identity_is_live(entry):
        return False
    _terminate_process_group(entry.process_id)
    if _wait_for_dispatch_process_exit(
        entry,
        timeout_s=ASYNC_LAUNCH_TERMINATION_GRACE_S,
    ):
        return True
    if not _dispatch_process_identity_is_live(entry):
        return False
    _signal_process_group(entry.process_id, signal.SIGKILL)
    return _wait_for_dispatch_process_exit(
        entry,
        timeout_s=ASYNC_LAUNCH_TERMINATION_GRACE_S,
    )


def _reap_process(process_id: int, *, block: bool) -> bool:
    """Reap one child process, waiting briefly after its result is published."""
    if process_id <= 0:
        return False
    deadline = time.monotonic() + 5.0 if block else time.monotonic()
    while True:
        try:
            waited, _status = os.waitpid(process_id, os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        if waited == process_id:
            return True
        if not block or time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _bounded_deliverable_value(value: Any, *, depth: int = 0) -> Any:
    """Bound a model-produced JSON value while preserving its structure."""
    if depth >= 6:
        return str(value)[:DELIVERABLE_STRING_CAP]
    if isinstance(value, Mapping):
        return {
            str(key)[:200]: _bounded_deliverable_value(item, depth=depth + 1)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, list):
        return [_bounded_deliverable_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, tuple):
        return [_bounded_deliverable_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:DELIVERABLE_STRING_CAP]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:DELIVERABLE_STRING_CAP]


def _normalized_lean_symbol(value: Any) -> str:
    """Return a comparison form for one fully qualified Lean declaration name."""
    return str(value or "").strip().removeprefix("_root_.")


def _normalized_dispatch_path(value: Any) -> str:
    """Return an absolute comparison path using the workflow project root."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        project_root = str(os.environ.get("LEANFLOW_PROJECT_ROOT", "") or "").strip()
        if project_root:
            path = Path(project_root).expanduser() / path
    return str(path.resolve(strict=False))


def _checked_helper_artifact(
    function_name: str,
    arguments: Mapping[str, Any],
    raw_result: Any,
    *,
    expected_target_symbol: str = "",
    expected_active_file: str = "",
) -> dict[str, Any] | None:
    """Build canonical evidence from one successful inline helper check.

    The model's final report is not trusted to reproduce proof text. This
    validator instead joins the exact tool arguments with the corresponding
    checker result observed by the parent process. The artifact remains
    advisory until a foreground parent re-runs Lean against current source.
    """
    if function_name != CHECKED_REPLACEMENT_TOOL:
        return None
    if str(arguments.get("action", "") or "").strip() != "check_helper":
        return None
    declaration = arguments.get("replacement")
    if not isinstance(declaration, str) or not declaration.strip():
        return None
    target_symbol = str(arguments.get("theorem_id", "") or "").strip()
    active_file = str(arguments.get("file_path", "") or "").strip()
    if not target_symbol or not active_file:
        return None
    expected_target = _normalized_lean_symbol(expected_target_symbol)
    if expected_target and _normalized_lean_symbol(target_symbol) != expected_target:
        return None
    expected_path = _normalized_dispatch_path(expected_active_file)
    if expected_path and _normalized_dispatch_path(active_file) != expected_path:
        return None

    if isinstance(raw_result, Mapping):
        result = dict(raw_result)
    else:
        try:
            parsed = json.loads(str(raw_result or ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        result = dict(parsed)
    if (
        result.get("success") is not True
        or result.get("ok") is not True
        or str(result.get("action", "") or "").strip() != "check_helper"
        or result.get("valid_without_sorry") is not True
        or result.get("has_errors") is not False
        or result.get("has_sorry") is not False
        or str(result.get("verification_scope", "") or "").strip() != "helper_candidate"
        or result.get("replacement_matches_target") is not False
        or _normalized_lean_symbol(result.get("target")) != _normalized_lean_symbol(target_symbol)
        or _normalized_dispatch_path(result.get("file")) != _normalized_dispatch_path(active_file)
    ):
        return None
    raw_declarations = result.get("replacement_declarations")
    if not isinstance(raw_declarations, Sequence) or isinstance(
        raw_declarations, (str, bytes, bytearray)
    ):
        return None
    replacement_declarations = [
        str(value).strip() for value in raw_declarations if str(value).strip()
    ]
    if not replacement_declarations:
        return None
    worker_check: dict[str, Any] = {
        "tool": CHECKED_REPLACEMENT_TOOL,
        "action": "check_helper",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "verification_scope": "helper_candidate",
        "replacement_matches_target": False,
        "replacement_declarations": replacement_declarations[:20],
    }
    elapsed_s = result.get("elapsed_s")
    if isinstance(elapsed_s, (int, float)) and not isinstance(elapsed_s, bool):
        worker_check["elapsed_s"] = max(0.0, float(elapsed_s))
    return {
        "anchor_target_symbol": target_symbol,
        "active_file": active_file,
        # Exact checked source is correctness data; generic string caps must
        # never turn it into syntactically plausible but corrupted Lean code.
        "declaration": declaration,
        "declaration_sha256": hashlib.sha256(declaration.encode("utf-8")).hexdigest(),
        "worker_check": worker_check,
        "parent_recheck_required": True,
    }


def _attach_checked_helpers(
    deliverable: Mapping[str, Any], checked_helpers: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Replace model-authored helper claims with parent-observed artifacts."""
    payload = dict(deliverable)
    payload.pop(CHECKED_HELPERS_KEY, None)
    payload.pop("checked_helper_status", None)
    canonical = [dict(item) for item in checked_helpers if isinstance(item, Mapping)]
    if canonical:
        payload[CHECKED_HELPERS_KEY] = canonical[-MAX_CHECKED_HELPERS:]
        payload["checked_helper_status"] = CHECKED_HELPER_STATUS
        payload["parent_recheck_required"] = True
    return payload


_CHECK_CLAIM_KEYS = frozenset(
    {
        "check",
        "check_status",
        "evidence",
        "kernel_check",
        "lean_check",
        "lean_verification",
        "result",
        "status",
        "summary",
        "verification",
        "worker_check",
    }
)


def _positive_checked_claim_text(value: Any) -> bool:
    """Return whether one report field positively claims a checked candidate."""
    text = str(value or "").strip().lower()
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "unverified",
            "not verified",
            "verification failed",
            "check failed",
            "has_errors=true",
            "valid_without_sorry=false",
        )
    ):
        return False
    return bool(
        re.search(r"\bkernel[-_ ]checked\b", text)
        or re.search(r"\b(?:candidate|proof|replacement)[-_ ]verified\b", text)
        or re.search(r"(?:^|[_ -])verified(?:$|[_ -])", text)
        or "valid_without_sorry=true" in text
        or (
            "lean_incremental_check" in text
            and any(marker in text for marker in ("accepted", "passed", "valid", "verified"))
        )
    )


def _is_auxiliary_helper_check_claim(value: Any) -> bool:
    """Return whether one check record explicitly concerns an auxiliary helper.

    Helper checks are useful parent-recheckable evidence, but they are not a
    claim that the dispatched target was replaced.  Keep the distinction
    structural so unrelated target-verification claims elsewhere in the same
    report still trigger the exact replacement contract.
    """
    if not isinstance(value, Mapping):
        return False
    action = str(value.get("action", "") or "").strip().casefold()
    kind = str(value.get("kind", "") or "").strip().casefold()
    scope = str(value.get("verification_scope", "") or "").strip().casefold()
    replacement_matches_target = value.get("replacement_matches_target")
    return bool(
        action == "check_helper"
        or kind in {"auxiliary_helper", "helper", "helper_candidate"}
        or scope == "helper_candidate"
        or replacement_matches_target is False
    )


def _claims_checked_candidate(value: Any, *, depth: int = 0) -> bool:
    """Return whether structured worker prose claims kernel-checked proof code."""
    if depth >= 5 or not isinstance(value, Mapping):
        return False
    for raw_key, item in value.items():
        key = str(raw_key or "").strip().lower()
        if key == CHECKED_HELPERS_KEY:
            # This key is populated from parent-observed tool traffic. It is
            # helper evidence, not a claim that the assigned target was
            # replaced, and therefore does not trigger the target contract.
            continue
        if key in {"valid_without_sorry", "kernel_checked", "verified"} and item is True:
            return True
        if key in _CHECK_CLAIM_KEYS:
            if isinstance(item, Mapping):
                if _is_auxiliary_helper_check_claim(item):
                    continue
                if _claims_checked_candidate(item, depth=depth + 1):
                    return True
            elif _positive_checked_claim_text(item):
                return True
    return False


def _normalize_checked_replacements(
    raw: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Validate exact checked replacements, preserving invalid exact code separately."""
    if raw is None:
        return [], [], []
    if isinstance(raw, Mapping):
        entries: list[Any] = [raw]
    elif isinstance(raw, list):
        entries = list(raw)
    else:
        return [], [], ["checked_replacements must be a JSON list of objects"]

    checked: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    all_issues: list[str] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping):
            all_issues.append(f"checked_replacements[{index}] is not an object")
            continue
        entry = dict(raw_entry)
        target_symbol = str(entry.get("target_symbol", "") or entry.get("target", "") or "").strip()
        replacement_value = entry.get("replacement")
        replacement = replacement_value if isinstance(replacement_value, str) else ""
        worker_check_raw = entry.get("worker_check")
        worker_check = dict(worker_check_raw) if isinstance(worker_check_raw, Mapping) else {}
        issues: list[str] = []
        if not target_symbol:
            issues.append("missing target_symbol")
        if not replacement.strip():
            issues.append("missing full exact replacement text")
        tool = str(worker_check.get("tool", "") or "").strip()
        if tool != CHECKED_REPLACEMENT_TOOL:
            issues.append(f"worker_check.tool must be {CHECKED_REPLACEMENT_TOOL}")
        if worker_check.get("valid_without_sorry") is not True:
            issues.append("worker_check.valid_without_sorry must be true")
        if worker_check.get("has_errors") is not False:
            issues.append("worker_check.has_errors must be false")
        if worker_check.get("has_sorry") is not False:
            issues.append("worker_check.has_sorry must be false")
        # Lean's inline-replacement checker compares the candidate declaration
        # signature with the assigned source declaration. A matching name is
        # insufficient: scratch research can deliberately reuse that name for
        # a different proposition, which is checked Lean code but is not a
        # checked replacement for the assignment.
        if worker_check.get("replacement_matches_target") is not True:
            issues.append("worker_check.replacement_matches_target must be true")

        normalized_check = dict(_bounded_deliverable_value(worker_check))
        normalized = {
            "target_symbol": target_symbol,
            # Exact proof text is correctness data. Never pass it through the
            # generic string cap; reject/downgrade rather than silently cutting it.
            "replacement": replacement,
            "worker_check": normalized_check,
        }
        if issues:
            normalized["contract_issues"] = issues
            unchecked.append(normalized)
            all_issues.extend(f"checked_replacements[{index}]: {issue}" for issue in issues)
            continue
        checked.append(normalized)
    return checked, unchecked, all_issues


def enforce_checked_replacement_contract(
    deliverable: Mapping[str, Any],
    *,
    expected_target_symbol: str = "",
) -> dict[str, Any]:
    """Downgrade unverifiable check claims and preserve exact checked proof text."""
    payload = dict(deliverable or {})
    raw_replacements = payload.get("checked_replacements")
    checked, unchecked, issues = _normalize_checked_replacements(raw_replacements)
    expected_target = str(expected_target_symbol or "").strip().removeprefix("_root_.")
    if expected_target:
        target_matched: list[dict[str, Any]] = []
        for entry in checked:
            reported_target = (
                str(entry.get("target_symbol", "") or "").strip().removeprefix("_root_.")
            )
            if reported_target == expected_target:
                target_matched.append(entry)
                continue
            issue = (
                f"target_symbol {reported_target!r} does not match dispatched target "
                f"{expected_target!r}"
            )
            invalid = dict(entry)
            invalid["contract_issues"] = [issue]
            unchecked.append(invalid)
            issues.append(issue)
        checked = target_matched
    claimed = raw_replacements is not None or _claims_checked_candidate(payload)

    if checked:
        payload["checked_replacements"] = checked
        payload["checked_replacement_status"] = (
            "partially_worker_checked_parent_recheck_required"
            if unchecked
            else "worker_checked_parent_recheck_required"
        )
        payload["parent_recheck_required"] = True
    elif claimed:
        reported_status = payload.get("status")
        if reported_status not in (None, "", "incomplete_unverified"):
            payload["reported_status"] = reported_status
        payload["status"] = "incomplete_unverified"
        payload["checked_replacements"] = []
        payload["checked_replacement_status"] = "incomplete_unverified"
        payload["parent_recheck_required"] = True
        if raw_replacements is None:
            issues.append(
                "kernel-check claim omitted the required exact checked_replacements entry"
            )
    if unchecked:
        payload["unchecked_replacements"] = unchecked
    if issues:
        payload["checked_replacement_contract_issues"] = issues
    return payload


def _bound_deliverable_preserving_exact_code(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply generic caps while exempting exact checked Lean source text."""
    generic = dict(payload)
    checked = generic.pop("checked_replacements", None)
    unchecked = generic.pop("unchecked_replacements", None)
    checked_helpers = generic.pop(CHECKED_HELPERS_KEY, None)
    bounded = _bounded_deliverable_value(generic)
    result = dict(bounded) if isinstance(bounded, Mapping) else {"summary": str(bounded)}
    if checked is not None:
        result["checked_replacements"] = checked
    if unchecked is not None:
        result["unchecked_replacements"] = unchecked
    if checked_helpers is not None:
        result[CHECKED_HELPERS_KEY] = checked_helpers
    return result


def _is_normalized_decomposition_report(payload: Mapping[str, Any]) -> bool:
    """Return whether a deliverable carries the normalized decomposition boundary."""
    return bool(
        payload.get("schema_version") == DECOMPOSITION_REPORT_SCHEMA_VERSION
        and isinstance(payload.get("source_basis"), list)
        and isinstance(payload.get("subgoals"), list)
        and isinstance(payload.get("dependency_proposals"), list)
        and payload.get("parent_state_write_required") is True
        and payload.get("child_state_mutated") is False
    )


def _cap_normalized_decomposition_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve bounded decomposition structure through final deliverable assembly."""
    result = dict(payload)
    structural_keys = (
        "schema_version",
        "status",
        "source_basis",
        "subgoals",
        "dependency_proposals",
        "parent_state_write_required",
        "child_state_mutated",
        "contract_issues",
        "reported_status",
        "checked_helper_status",
        "parent_recheck_required",
        "checked_replacement_contract_issues",
    )
    priority = {key: result[key] for key in structural_keys if key in result}
    for key in (
        "checked_replacements",
        "unchecked_replacements",
        CHECKED_HELPERS_KEY,
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY,
    ):
        if key in result:
            priority[key] = result[key]

    generic = {key: value for key, value in result.items() if key not in priority}
    if generic:
        candidate = {
            **priority,
            "summary": json.dumps(generic, ensure_ascii=False, sort_keys=True),
            "truncated": True,
        }
        if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) <= DELIVERABLE_JSON_CAP:
            return candidate

    if len(json.dumps(priority, ensure_ascii=False, sort_keys=True)) > DELIVERABLE_JSON_CAP:
        # Normalization bounds the graph and parent context. Only exact checked
        # Lean source may legitimately exceed the generic artifact budget; keep
        # both trust-boundary records intact and make the exception explicit.
        priority["structured_decomposition_exceeds_deliverable_cap"] = True
    return priority


def _cap_deliverable_preserving_exact_code(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fit prose to the artifact budget without ever truncating exact proof code."""
    result = dict(payload)
    if _is_normalized_decomposition_report(result):
        return _cap_normalized_decomposition_report(result)
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(serialized) <= DELIVERABLE_JSON_CAP:
        return result

    checked = result.get("checked_replacements")
    unchecked = result.get("unchecked_replacements")
    checked_helpers = result.get(CHECKED_HELPERS_KEY)
    parent_route_context = result.get(research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY)
    priority: dict[str, Any] = {
        key: result[key]
        for key in (
            "status",
            "reported_status",
            "checked_replacement_status",
            "checked_helper_status",
            "parent_recheck_required",
            "checked_replacement_contract_issues",
        )
        if key in result
    }
    if checked is not None:
        priority["checked_replacements"] = checked
    if unchecked is not None:
        priority["unchecked_replacements"] = unchecked
    if checked_helpers is not None:
        priority[CHECKED_HELPERS_KEY] = checked_helpers
    if parent_route_context is not None:
        priority[research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY] = parent_route_context
    generic = dict(result)
    generic.pop("checked_replacements", None)
    generic.pop("unchecked_replacements", None)
    generic.pop(CHECKED_HELPERS_KEY, None)
    generic.pop(research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY, None)
    priority["summary"] = json.dumps(generic, ensure_ascii=False, sort_keys=True)
    priority["truncated"] = True

    priority_serialized = json.dumps(priority, ensure_ascii=False, sort_keys=True)
    overflow = len(priority_serialized) - DELIVERABLE_JSON_CAP
    if overflow > 0 and len(priority["summary"]) > overflow:
        priority["summary"] = priority["summary"][: len(priority["summary"]) - overflow]
    if len(json.dumps(priority, ensure_ascii=False, sort_keys=True)) > DELIVERABLE_JSON_CAP:
        # The exact code alone exceeds the generic cap. Preserve it and make
        # the exceptional size explicit instead of silently corrupting it.
        priority.pop("summary", None)
        priority["exact_code_exceeds_deliverable_cap"] = True
    return priority


def _decomposition_identifier(value: Any) -> str:
    """Return one bounded proposal-local identifier or an empty rejection."""
    text = str(value or "").strip()
    if (
        not text
        or len(text) > DECOMPOSITION_IDENTIFIER_CAP
        or re.fullmatch(r"[A-Za-z0-9_.-]+", text) is None
    ):
        return ""
    return text


def _decomposition_mapping_items(value: Any) -> list[Mapping[str, Any]]:
    """Return only mapping records from one untrusted report collection."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _decomposition_reference_ids(value: Any) -> list[str]:
    """Return deduplicated bounded identifiers from one untrusted list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(
        dict.fromkeys(
            identifier for item in value if (identifier := _decomposition_identifier(item))
        )
    )[:DECOMPOSITION_MAX_REFERENCES]


def _decomposition_edge_creates_cycle(
    adjacency: Mapping[str, set[str]],
    *,
    source: str,
    target: str,
) -> bool:
    """Return whether adding one directed proposal edge would create a cycle."""
    pending = [target]
    visited: set[str] = set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node in visited:
            continue
        visited.add(node)
        pending.extend(adjacency.get(node, ()))
    return False


def _decomposition_connected_to_target(
    adjacency: Mapping[str, set[str]],
) -> set[str]:
    """Return proposal nodes connected to target under kind-neutral graph semantics."""
    pending = ["target"]
    visited: set[str] = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        pending.extend(adjacency.get(node, ()))
    return visited


def _incomplete_decomposition_report(issue: str) -> dict[str, Any]:
    """Return the canonical non-usable report for malformed delegate output."""
    report = _normalize_decomposition_report({})
    existing = list(report.get("contract_issues") or [])
    report["contract_issues"] = list(dict.fromkeys([issue, *existing]))[
        :DECOMPOSITION_CONTRACT_ISSUES_CAP
    ]
    return report


def _normalize_decomposition_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a source-backed proposal schema with no child-owned state edits."""
    issues: list[str] = []
    sources: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    raw_sources = _decomposition_mapping_items(payload.get("source_basis"))
    if len(raw_sources) > DECOMPOSITION_MAX_SOURCES:
        issues.append(f"source_basis exceeds the {DECOMPOSITION_MAX_SOURCES}-record durable limit")
    for index, raw_source in enumerate(raw_sources[:DECOMPOSITION_MAX_SOURCES]):
        source_id = _decomposition_identifier(raw_source.get("id"))
        reference = str(raw_source.get("reference", "") or "").strip()
        kind = str(raw_source.get("kind", "") or "").strip()
        if (
            not source_id
            or not reference
            or source_id in source_ids
            or kind not in DECOMPOSITION_SOURCE_KINDS
        ):
            issues.append(
                f"source_basis[{index}] lacks a unique id, exact reference, or allowed kind"
            )
            continue
        if len(reference) > DECOMPOSITION_REFERENCE_CAP:
            issues.append(f"source_basis[{index}].reference exceeds the durable field limit")
        source_summary = str(raw_source.get("summary", "") or "")
        if len(source_summary) > DECOMPOSITION_SOURCE_SUMMARY_CAP:
            issues.append(f"source_basis[{index}].summary exceeds the durable field limit")
        source_ids.add(source_id)
        sources.append(
            {
                "id": source_id,
                "kind": kind,
                "reference": reference[:DECOMPOSITION_REFERENCE_CAP],
                "summary": source_summary[:DECOMPOSITION_SOURCE_SUMMARY_CAP],
            }
        )

    subgoals: list[dict[str, Any]] = []
    subgoal_ids: set[str] = set()
    raw_dependencies_by_id: dict[str, list[str]] = {}
    raw_subgoals = _decomposition_mapping_items(payload.get("subgoals"))
    if len(raw_subgoals) > DECOMPOSITION_MAX_SUBGOALS:
        issues.append(f"subgoals exceeds the {DECOMPOSITION_MAX_SUBGOALS}-record durable limit")
    for index, raw_subgoal in enumerate(raw_subgoals[:DECOMPOSITION_MAX_SUBGOALS]):
        subgoal_id = _decomposition_identifier(raw_subgoal.get("id"))
        statement = str(raw_subgoal.get("statement", "") or "").strip()
        difficulty_reduction = str(raw_subgoal.get("difficulty_reduction", "") or "").strip()
        source_refs = _decomposition_reference_ids(raw_subgoal.get("source_refs"))
        valid_source_refs = [source_id for source_id in source_refs if source_id in source_ids]
        if not subgoal_id or not statement or subgoal_id in subgoal_ids or not valid_source_refs:
            issues.append(f"subgoals[{index}] lacks a unique id, statement, or valid source_refs")
            continue
        if len(valid_source_refs) != len(source_refs):
            issues.append(f"subgoals[{index}] references an unknown source")
        if len(statement) > DECOMPOSITION_STATEMENT_CAP:
            issues.append(f"subgoals[{index}].statement exceeds the durable field limit")
        if not difficulty_reduction:
            issues.append(f"subgoal {subgoal_id!r} lacks a nonempty difficulty_reduction")
        elif len(difficulty_reduction) > DECOMPOSITION_DIFFICULTY_CAP:
            issues.append(f"subgoals[{index}].difficulty_reduction exceeds the durable field limit")
        purpose = str(raw_subgoal.get("purpose", "") or "")
        if len(purpose) > DECOMPOSITION_PURPOSE_CAP:
            issues.append(f"subgoals[{index}].purpose exceeds the durable field limit")
        subgoal_ids.add(subgoal_id)
        raw_dependencies_by_id[subgoal_id] = _decomposition_reference_ids(
            raw_subgoal.get("dependencies")
        )
        bounded_statement = statement[:DECOMPOSITION_STATEMENT_CAP]
        subgoals.append(
            {
                "id": subgoal_id,
                "statement": bounded_statement,
                # The duplicate shape field gives the parent semantic classifier
                # a provenance-insensitive identity for proposal deduplication.
                "proof_shape": bounded_statement,
                "purpose": purpose[:DECOMPOSITION_PURPOSE_CAP],
                "source_refs": valid_source_refs,
                "dependencies": [],
                "difficulty_reduction": difficulty_reduction[:DECOMPOSITION_DIFFICULTY_CAP],
                "verification_status": "proposal_parent_review_required",
            }
        )

    dependency_adjacency: dict[str, set[str]] = {subgoal_id: set() for subgoal_id in subgoal_ids}
    dependency_adjacency["target"] = set()
    for subgoal in subgoals:
        subgoal_id = str(subgoal["id"])
        dependencies = raw_dependencies_by_id.get(subgoal_id, [])
        valid_dependencies: list[str] = []
        for dependency in dependencies:
            if dependency not in subgoal_ids or dependency == subgoal_id:
                issues.append(f"subgoal {subgoal_id!r} has an unknown or self dependency")
                continue
            if _decomposition_edge_creates_cycle(
                dependency_adjacency,
                source=subgoal_id,
                target=dependency,
            ):
                issues.append(f"subgoal {subgoal_id!r} dependency {dependency!r} creates a cycle")
                continue
            dependency_adjacency[subgoal_id].add(dependency)
            valid_dependencies.append(dependency)
        subgoal["dependencies"] = valid_dependencies

    dependency_proposals: list[dict[str, Any]] = []
    dependency_keys: set[tuple[str, str, str]] = set()
    valid_nodes = {*subgoal_ids, "target"}
    raw_dependency_proposals = _decomposition_mapping_items(payload.get("dependency_proposals"))
    if len(raw_dependency_proposals) > DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS:
        issues.append(
            "dependency_proposals exceeds the "
            f"{DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS}-record durable limit"
        )
    for index, raw_dependency in enumerate(
        raw_dependency_proposals[:DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS]
    ):
        source = _decomposition_identifier(raw_dependency.get("source"))
        target = _decomposition_identifier(raw_dependency.get("target"))
        kind = str(raw_dependency.get("kind", "") or "").strip()
        rationale = str(raw_dependency.get("rationale", "") or "").strip()
        source_refs = _decomposition_reference_ids(raw_dependency.get("source_refs"))
        valid_source_refs = [source_id for source_id in source_refs if source_id in source_ids]
        key = (source, target, kind)
        if (
            source not in valid_nodes
            or target not in valid_nodes
            or source == target
            or kind not in DECOMPOSITION_DEPENDENCY_KINDS
            or not rationale
            or not valid_source_refs
            or key in dependency_keys
        ):
            issues.append(
                f"dependency_proposals[{index}] is not a unique source-backed graph proposal"
            )
            continue
        if len(valid_source_refs) != len(source_refs):
            issues.append(f"dependency_proposals[{index}] references an unknown source")
        if len(rationale) > DECOMPOSITION_RATIONALE_CAP:
            issues.append(
                f"dependency_proposals[{index}].rationale exceeds the durable field limit"
            )
        if _decomposition_edge_creates_cycle(
            dependency_adjacency,
            source=source,
            target=target,
        ):
            issues.append(f"dependency_proposals[{index}] creates a dependency cycle")
            continue
        dependency_keys.add(key)
        dependency_adjacency[source].add(target)
        dependency_proposals.append(
            {
                "source": source,
                "target": target,
                "kind": kind,
                "rationale": rationale[:DECOMPOSITION_RATIONALE_CAP],
                "source_refs": valid_source_refs,
            }
        )

    connectivity: dict[str, set[str]] = {node: set() for node in valid_nodes}
    for subgoal in subgoals:
        source = str(subgoal["id"])
        for target in subgoal["dependencies"]:
            connectivity[source].add(target)
            connectivity[target].add(source)
    for proposal in dependency_proposals:
        source = str(proposal["source"])
        target = str(proposal["target"])
        # ``depends_on`` uses parent -> dependency while ``split_of`` uses
        # helper -> parent. Connectivity is deliberately kind-neutral: both
        # express a dependency path between the candidate and requested target.
        connectivity[source].add(target)
        connectivity[target].add(source)
    connected_subgoals = subgoal_ids.intersection(_decomposition_connected_to_target(connectivity))
    all_subgoals_connected = bool(subgoal_ids) and connected_subgoals == subgoal_ids
    all_subgoals_strictly_easier = bool(subgoals) and all(
        str(subgoal.get("difficulty_reduction", "") or "").strip() for subgoal in subgoals
    )
    if not sources:
        issues.append("source_basis must contain at least one exact reference")
    if not subgoals:
        issues.append("subgoals must contain at least one source-backed proposal")
    if not dependency_proposals:
        issues.append("dependency_proposals must connect a subgoal to the target")
    if subgoals and not connected_subgoals:
        issues.append("no accepted subgoal has a dependency path to target")
    elif not all_subgoals_connected:
        disconnected = sorted(subgoal_ids - connected_subgoals)
        issues.append(
            "accepted subgoals lack a dependency path to target: " + ", ".join(disconnected)
        )
    structurally_usable = bool(sources and subgoals and dependency_proposals and connected_subgoals)
    complete = structurally_usable and all_subgoals_connected and all_subgoals_strictly_easier
    if not structurally_usable:
        status = "incomplete_unverified"
    elif not complete or issues:
        status = "partial_proposal_parent_review_required"
    else:
        status = "proposal_parent_review_required"
    result: dict[str, Any] = {
        "schema_version": DECOMPOSITION_REPORT_SCHEMA_VERSION,
        "status": status,
        "source_basis": sources,
        "subgoals": subgoals,
        "dependency_proposals": dependency_proposals,
        "parent_state_write_required": True,
        "child_state_mutated": False,
    }
    if issues:
        result["contract_issues"] = list(dict.fromkeys(issues))[:DECOMPOSITION_CONTRACT_ISSUES_CAP]
    return result


def _delegate_deliverable(
    summary: str,
    schema: str,
    *,
    expected_target_symbol: str = "",
) -> dict[str, Any]:
    """Parse a compact JSON delegate report without truncating it into invalid text."""
    text = str(summary or "").strip()
    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        first_newline = candidate.find("\n")
        if first_newline >= 0:
            candidate = candidate[first_newline + 1 : -3].strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        if schema == "decomposition_report":
            return _incomplete_decomposition_report(
                "decomposition_report must be a valid JSON object"
            )
        return {"summary": text[:DELIVERABLE_STRING_CAP]}
    if not isinstance(parsed, Mapping):
        if schema == "decomposition_report":
            return _incomplete_decomposition_report(
                "decomposition_report must decode to a JSON object"
            )
        return {"summary": text[:DELIVERABLE_STRING_CAP]}
    selected: Any = parsed
    if schema in parsed:
        selected = parsed.get(schema)
    elif isinstance(parsed.get("deliverable"), Mapping):
        selected = parsed.get("deliverable")
    elif str(parsed.get("deliverable", "") or "").strip() == schema:
        # Some providers render the requested schema name as a discriminator
        # and place its fields alongside it.  Preserve those fields rather
        # than collapsing a valid report to {"summary": "experiment_result"}.
        selected = {key: value for key, value in parsed.items() if key != "deliverable"}
    if not isinstance(selected, Mapping):
        if schema == "decomposition_report":
            return _incomplete_decomposition_report(
                "decomposition_report payload must be a JSON object"
            )
        return {"summary": str(selected)[:DELIVERABLE_STRING_CAP]}
    payload = dict(selected)
    if schema == "findings_report":
        payload = enforce_checked_replacement_contract(
            payload,
            expected_target_symbol=expected_target_symbol,
        )
    elif schema == "decomposition_report":
        payload = _normalize_decomposition_report(payload)
    bounded = _bound_deliverable_preserving_exact_code(payload)
    return _cap_deliverable_preserving_exact_code(bounded)


def _managed_boundary_deliverable(
    spec: JobSpec,
    raw_handoff: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one consumable finding from a bounded managed-boundary handoff.

    Provenance is copied from the authoritative JobSpec, never from model text.
    The caller may promote the operationally interrupted worker only when this
    function returns a non-empty deliverable.
    """
    handoff = dict(raw_handoff or {})
    if (
        str(handoff.get("kind", "") or "") != HANDOFF_KIND
        or str(handoff.get("boundary_marker", "") or "") != WORKFLOW_STEP_BOUNDARY_INTERRUPT
    ):
        return {}
    raw_evidence = handoff.get("evidence")
    if not isinstance(raw_evidence, list):
        return {}
    evidence = [dict(item) for item in raw_evidence if isinstance(item, Mapping)]
    evidence = [item for item in evidence if str(item.get("result_excerpt", "") or "").strip()]
    try:
        completed_tool_calls = int(handoff.get("completed_tool_calls", 0) or 0)
    except (TypeError, ValueError):
        return {}
    if not evidence or completed_tool_calls <= 0:
        return {}
    inputs = dict(spec.inputs or {})
    provenance = {
        "job_id": spec.job_id,
        "target_symbol": str(inputs.get("target_symbol", "") or ""),
        "active_file": str(inputs.get("active_file", "") or ""),
        "route_key": str(inputs.get("route_key", "") or ""),
        "route_signature": str(inputs.get("route_signature", "") or ""),
    }
    raw_reasoning = handoff.get("reasoning")
    reasoning_values = raw_reasoning if isinstance(raw_reasoning, list) else []
    reasoning = [str(item).strip() for item in reasoning_values if str(item).strip()]
    payload = {
        "status": "interrupted_with_evidence",
        "summary": (
            "Managed search boundary preserved "
            f"{len(evidence)} evidence item(s) from {completed_tool_calls} completed "
            "grounding tool call(s); synthesize this handoff before further broad search."
        ),
        "route_boundary": {
            "kind": HANDOFF_KIND,
            "provenance": provenance,
            "completed_tool_calls": completed_tool_calls,
            "evidence": evidence,
            "reasoning": reasoning,
        },
        "next_route": {
            "kind": "synthesize_preserved_evidence",
            "search_policy": "no_broad_search_before_concrete_candidate_check",
            "objective": (
                "Synthesize the preserved evidence into one concrete formula, helper lemma, "
                "or proof-shape candidate and check that candidate before any new broad search."
            ),
        },
    }
    return _cap_deliverable_preserving_exact_code(_bound_deliverable_preserving_exact_code(payload))


class DispatchService:
    """The single dispatch authority for one runner process."""

    def __init__(
        self,
        *,
        parent_agent: Any = None,
        root_job_id: str = "",
        cap: int = 0,
        incremental_evidence_sink: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
        async_launch_admission: Callable[[Mapping[str, Any]], bool] | None = None,
    ):
        self._parent_agent = parent_agent
        self.root_job_id = root_job_id or "run"
        self._cap = cap or dispatch_max_concurrent()
        self._incremental_evidence_sink = incremental_evidence_sink
        self._async_launch_admission = async_launch_admission

    # ----- ledger transactions -------------------------------------------

    def _summary_path(self):
        return workflow_state_root() / "summary.json"

    def _transaction(
        self,
        mutate: Callable[[list[dict[str, Any]]], tuple[Any, list[LedgerEntry]]],
        *,
        summary_admission: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> Any:
        """Run one read-validate-write over the whole ledger atomically.

        ``mutate`` edits the raw ledger list in place and returns
        ``(outcome, entries_to_announce)``; validation raising inside the
        transaction aborts the write. Activity events are emitted after the
        commit, outside the lock.
        """
        path = self._summary_path()
        with json_write_lock(path):
            summary = read_json_file(path)
            if summary_admission is not None and not summary_admission(summary):
                raise DispatchLaunchAdmissionDeferred(
                    "dispatch process launch deferred by current campaign admission"
                )
            ledger = [
                dict(raw) for raw in (summary.get(_LEDGER_KEY) or []) if isinstance(raw, Mapping)
            ]
            outcome, announcements = mutate(ledger)
            removed_context_fields = dispatch_ledger_compaction.compact_terminal_dispatch_records(
                ledger
            )
            archived_records = dispatch_ledger_compaction.compact_consumed_dispatch_records(
                ledger,
                state_root=workflow_state_root(),
            )
            if removed_context_fields or archived_records:
                raw_compaction = summary.get("dispatch_ledger_compaction")
                compaction = dict(raw_compaction) if isinstance(raw_compaction, Mapping) else {}
                try:
                    prior_fields_removed = int(compaction.get("fields_removed", 0) or 0)
                except (TypeError, ValueError):
                    prior_fields_removed = 0
                try:
                    prior_records_archived = int(compaction.get("records_archived", 0) or 0)
                except (TypeError, ValueError):
                    prior_records_archived = 0
                compaction["version"] = 2
                compaction["fields_removed"] = max(0, prior_fields_removed) + removed_context_fields
                compaction["records_archived"] = max(0, prior_records_archived) + archived_records
                compaction["last_compacted_at"] = _now_iso()
                summary["dispatch_ledger_compaction"] = compaction
            summary[_LEDGER_KEY] = ledger
            summary["updated_at"] = _now_iso()
            atomic_json_write(path, summary, sort_keys=True)
        for entry in announcements:
            self._announce(entry)
        return outcome

    @staticmethod
    def _find(ledger: list[dict[str, Any]], job_id: str) -> int:
        for index, raw in enumerate(ledger):
            if dict(raw.get("spec") or {}).get("job_id") == job_id:
                return index
        return -1

    def _announce(self, entry: LedgerEntry) -> None:
        provider_retry_after = normalize_provider_retry_after(
            entry.result.get("provider_retry_after")
        )
        append_workflow_activity(
            "dispatch-job",
            f"Dispatch job {entry.spec.job_id} -> {entry.state}",
            job_id=entry.spec.job_id,
            archetype=entry.spec.archetype,
            state=entry.state,
            requester_role=entry.spec.requester_role,
            agent_session_ids=list(entry.agent_session_ids),
            notes=entry.notes,
            **({"provider_retry_after": provider_retry_after} if provider_retry_after else {}),
        )

    def _save_entry(self, entry: LedgerEntry) -> LedgerEntry:
        """Raw upsert metadata, announcing only a new lifecycle state."""

        def mutate(ledger: list[dict[str, Any]]):
            payload = entry.to_mapping()
            index = self._find(ledger, entry.spec.job_id)
            if index >= 0:
                current = LedgerEntry.from_mapping(ledger[index])
                ledger[index] = payload
                announcements = [entry] if current.state != entry.state else []
            else:
                ledger.append(payload)
                announcements = [entry]
            return entry, announcements

        return self._transaction(mutate)

    def _transition(self, job_id: str, target: str, **changes: Any) -> LedgerEntry:
        """State change applied against the PERSISTED entry (stale-proof)."""

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            updated = current.with_state(target, **changes)
            ledger[index] = updated.to_mapping()
            return updated, [updated]

        return self._transaction(mutate)

    def _mark_running_process_note(self, entry: LedgerEntry, note: str) -> LedgerEntry:
        """Persist a process-liveness note without overwriting a race winner.

        The exact launch identity is compared inside the ledger transaction so
        a stale reconciler cannot replace a completed result or a newer launch.
        """

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if (
                current.state != "running"
                or current.launch_nonce != entry.launch_nonce
                or current.process_identity() != entry.process_identity()
            ):
                return current, []
            if current.notes == note:
                return current, []
            updated = replace(current, notes=str(note)[:300])
            ledger[index] = updated.to_mapping()
            return updated, []

        return self._transaction(mutate)

    def release_legacy_killed_process_capacity(self, entry: LedgerEntry) -> dict[str, Any]:
        """Persist proof that one killed worker no longer owns its recorded PID.

        The transaction rechecks the exact ledger row and release evidence.
        Modern token-bound identities enter only after direct retirement fails
        and their token no longer matches; exact command/spec mismatch must
        then prove that the live PID belongs to another process.
        """

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                return {"released": True, "newly_released": False, "reason": "missing-row"}, []
            current = LedgerEntry.from_mapping(ledger[index])
            if current.state != "killed":
                return {
                    "released": current.is_terminal(),
                    "newly_released": False,
                    "reason": f"terminal-state:{current.state}" if current.is_terminal() else "",
                }, []
            if (
                current.finished_at != entry.finished_at
                or current.process_identity() != entry.process_identity()
            ):
                return {"released": False, "newly_released": False, "reason": ""}, []
            if (
                current.process_released_at
                and current.process_release_reason in SAFE_LEGACY_PROCESS_RELEASE_REASONS
            ):
                report_key = current.process_release_report_key or _process_release_report_key(
                    current,
                    reason=current.process_release_reason,
                    evidence_sha256=current.process_release_evidence_sha256,
                )
                if report_key != current.process_release_report_key:
                    current = replace(current, process_release_report_key=report_key)
                    ledger[index] = current.to_mapping()
                return {
                    "released": True,
                    "newly_released": False,
                    "reason": current.process_release_reason,
                    "released_at": current.process_released_at,
                    "evidence_sha256": current.process_release_evidence_sha256,
                    "observed_started_at": current.process_release_observed_started_at,
                    "report_key": report_key,
                    "reported_at": current.process_release_reported_at,
                    "process_id": current.process_id,
                    "finished_at": current.finished_at,
                }, []

            evidence = _legacy_terminal_process_release_evidence(current)
            reason = str(evidence.get("reason", "") or "")
            if not reason:
                # Revoke tombstones produced by the historical wall-clock-only
                # policy. They are unsafe until current command/exit evidence
                # independently proves release.
                if current.process_released_at or current.process_release_reason:
                    current = replace(
                        current,
                        process_released_at="",
                        process_release_reason="",
                        process_release_evidence_sha256="",
                        process_release_observed_started_at="",
                        process_release_report_key="",
                        process_release_reported_at="",
                    )
                    ledger[index] = current.to_mapping()
                return {"released": False, "newly_released": False, "reason": ""}, []
            released_at = _now_iso()
            evidence_sha256 = str(evidence.get("evidence_sha256", "") or "")
            report_key = _process_release_report_key(
                current,
                reason=reason,
                evidence_sha256=evidence_sha256,
            )
            updated = replace(
                current,
                process_released_at=released_at,
                process_release_reason=reason,
                process_release_evidence_sha256=evidence_sha256,
                process_release_observed_started_at=str(
                    evidence.get("observed_started_at", "") or ""
                ),
                process_release_report_key=report_key,
                process_release_reported_at="",
            )
            ledger[index] = updated.to_mapping()
            return {
                "released": True,
                "newly_released": True,
                "reason": reason,
                "released_at": released_at,
                "evidence_sha256": evidence_sha256,
                "observed_started_at": updated.process_release_observed_started_at,
                "report_key": report_key,
                "reported_at": "",
                "process_id": current.process_id,
                "finished_at": current.finished_at,
            }, []

        return self._transaction(mutate)

    def mark_process_release_reported(
        self,
        *,
        job_id: str,
        report_key: str,
        reported_at: str = "",
    ) -> bool:
        """Durably acknowledge one idempotently persisted release diagnostic."""
        normalized_key = str(report_key or "").strip()
        if not normalized_key:
            return False

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                return False, []
            current = LedgerEntry.from_mapping(ledger[index])
            if (
                not current.process_released_at
                or current.process_release_reason not in SAFE_LEGACY_PROCESS_RELEASE_REASONS
                or current.process_release_report_key != normalized_key
            ):
                return False, []
            if current.process_release_reported_at:
                return True, []
            updated = replace(
                current,
                process_release_reported_at=str(reported_at or _now_iso()),
            )
            ledger[index] = updated.to_mapping()
            return True, []

        return bool(self._transaction(mutate))

    def _load_ledger(self) -> list[LedgerEntry]:
        summary = read_json_file(self._summary_path())
        return [
            LedgerEntry.from_mapping(raw)
            for raw in dispatch_ledger_compaction.hydrate_dispatch_ledger(
                summary.get(_LEDGER_KEY) or [],
                state_root=workflow_state_root(),
            )
        ]

    def _entry(self, job_id: str) -> LedgerEntry:
        """Return one exact ledger row without hydrating unrelated archives."""
        summary = read_json_file(self._summary_path())
        for raw in summary.get(_LEDGER_KEY) or []:
            if not isinstance(raw, Mapping):
                continue
            # Match the same first raw row used by transactional ``_find``.
            # Hydrating all prior/cold rows makes a point lookup scale with the
            # complete campaign history and needlessly retains archive payloads.
            if dict(raw.get("spec") or {}).get("job_id") != job_id:
                continue
            hydrated = dispatch_ledger_compaction.hydrate_dispatch_record(
                raw,
                state_root=workflow_state_root(),
            )
            return LedgerEntry.from_mapping(hydrated)
        raise KeyError(f"unknown dispatch job {job_id!r}")

    # ----- lifecycle -----------------------------------------------------

    def mint_job_id(self, archetype: str, *, role: str, parent_job_id: str = "") -> str:
        """Mint the next lineage id; the role becomes a path segment (N3).

        Grammar ``<root>.<role-path>.<tag>-<seq>``: a top-level dispatch by
        the planner mints ``<root>.planner.np-001``; nested dispatch passes
        the requesting JOB's id as ``parent_job_id`` to extend the chain.
        """
        parent = parent_job_id.strip() or f"{self.root_job_id}.{role}"
        existing = [entry.spec.job_id for entry in self._load_ledger()]
        return next_job_id(existing, parent, archetype)

    def propose(self, spec: JobSpec) -> LedgerEntry:
        """Validate and persist state='proposed' (always allowed — dark-plannable)."""
        problems = spec.validate()
        expected_parent = spec.job_id.rpartition(".")[0]
        if spec.parent_job_id and spec.parent_job_id != expected_parent:
            problems.append(
                f"parent_job_id {spec.parent_job_id!r} is not the direct parent "
                f"of {spec.job_id!r} (lineage must be a dotted chain)"
            )
        if problems:
            raise ValueError("; ".join(problems))
        entry = LedgerEntry(spec=spec, created_at=_now_iso())

        def mutate(ledger: list[dict[str, Any]]):
            if self._find(ledger, spec.job_id) >= 0:
                raise ValueError(f"job_id {spec.job_id!r} already exists")
            inputs = dict(spec.inputs or {})
            delta_signature = str(
                inputs.get(MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY, "") or ""
            ).strip()
            if delta_signature:
                target_symbol = _normalized_lean_symbol(inputs.get("target_symbol", ""))
                active_file = _normalized_dispatch_path(inputs.get("active_file", ""))
                if not target_symbol or not active_file:
                    raise ValueError(
                        "mathematical delta reservation requires exact target_symbol and active_file"
                    )
                for raw in ledger:
                    current = LedgerEntry.from_mapping(raw)
                    if current.is_terminal():
                        continue
                    current_inputs = dict(current.spec.inputs or {})
                    if (
                        str(
                            current_inputs.get(
                                MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY,
                                "",
                            )
                            or ""
                        ).strip()
                        != delta_signature
                        or _normalized_lean_symbol(current_inputs.get("target_symbol", ""))
                        != target_symbol
                        or _normalized_dispatch_path(current_inputs.get("active_file", ""))
                        != active_file
                    ):
                        continue
                    raise MathematicalDeltaReservationConflict(
                        winning_job_id=current.spec.job_id,
                        delta_signature=delta_signature,
                    )
            ledger.append(entry.to_mapping())
            return entry, [entry]

        return self._transaction(mutate)

    def deploy(self, job_id: str) -> LedgerEntry:
        """Sync v1: run the job to completion via its archetype backend."""
        if not dispatch_enabled():
            raise RuntimeError("dispatch is disabled (LEANFLOW_DISPATCH_ENABLED is off)")

        def start(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            in_flight = sum(
                1 for raw in ledger if str(raw.get("state", "")) in {"deployed", "running"}
            )
            if in_flight >= self._cap:
                raise RuntimeError(f"dispatch cap reached ({in_flight}/{self._cap} jobs in flight)")
            current = LedgerEntry.from_mapping(ledger[index])
            deployed = current.with_state("deployed")
            running = deployed.with_state("running", started_at=_now_iso())
            ledger[index] = running.to_mapping()
            return running, [deployed, running]

        entry = self._transaction(start)
        try:
            result = self._run_backend(entry.spec)
        except Exception as exc:
            logger.debug("dispatch backend failed", exc_info=True)
            final_state = "failed"
            changes: dict[str, Any] = {
                "finished_at": _now_iso(),
                "notes": f"backend error: {str(exc)[:300]}",
            }
        else:
            status = str(result.get("status", "") or "done")
            final_state = "done" if status in {"done", "ok", "success"} else "failed"
            changes = {"finished_at": _now_iso(), "result": dict(result)}
            if final_state == "failed":
                changes["notes"] = _worker_result_failure_note(status, result)
        try:
            return self._transition(job_id, final_state, **changes)
        except ValueError:
            # Lost the race to kill/reconcile while the backend ran: the
            # persisted terminal verdict wins; never resurrect the job.
            persisted = self._entry(job_id)
            logger.debug(
                "dispatch job %s finished as %s but ledger says %s; keeping the ledger",
                job_id,
                final_state,
                persisted.state,
            )
            return persisted

    @staticmethod
    def _artifact_stem(job_id: str) -> str:
        return _dispatch_artifact_stem(job_id)

    def _async_paths(self, job_id: str) -> tuple[Path, Path, Path]:
        """Return the current spec, legacy result, and append-only log paths."""
        root = workflow_state_root() / "dispatch-jobs"
        stem = self._artifact_stem(job_id)
        return root / f"{stem}.spec.json", root / f"{stem}.result.json", root / f"{stem}.log"

    def _nonce_artifact_path(self, job_id: str, launch_nonce: str, kind: str) -> Path:
        """Return a safe nonce-specific path without exposing the raw nonce."""
        root = workflow_state_root() / "dispatch-jobs"
        stem = self._artifact_stem(job_id)
        digest = process_token_sha256(launch_nonce)[:32]
        if not digest:
            raise ValueError("launch nonce is required for a modern dispatch artifact")
        return root / f"{stem}.{digest}.{kind}.json"

    def _async_spec_path(self, job_id: str, launch_nonce: str = "") -> Path:
        """Return the job-global current-nonce fence read twice by workers."""
        return self._async_paths(job_id)[0]

    def _async_result_path(self, job_id: str, launch_nonce: str = "") -> Path:
        """Return the nonce-specific worker result, or the legacy shared path."""
        if launch_nonce:
            return self._nonce_artifact_path(job_id, launch_nonce, "result")
        return self._async_paths(job_id)[1]

    def _async_identity_path(self, job_id: str, launch_nonce: str = "") -> Path:
        """Return the nonce-specific identity receipt, or the legacy shared path."""
        root = workflow_state_root() / "dispatch-jobs"
        if launch_nonce:
            return self._nonce_artifact_path(job_id, launch_nonce, "identity")
        return root / f"{self._artifact_stem(job_id)}.identity.json"

    def _async_incremental_evidence_path(self, job_id: str, launch_nonce: str = "") -> Path:
        """Return the nonce-specific worker-checked evidence journal path."""
        root = workflow_state_root() / "dispatch-jobs"
        if launch_nonce:
            return self._nonce_artifact_path(job_id, launch_nonce, "evidence")
        return root / f"{self._artifact_stem(job_id)}.evidence.json"

    def _async_launch_lock_path(self, job_id: str) -> Path:
        """Return the cross-process sidecar serializing one job's launch."""
        root = workflow_state_root() / "dispatch-jobs"
        return root / f"{self._artifact_stem(job_id)}.launch.lock"

    @contextlib.contextmanager
    def _async_launch_lock(self, job_id: str) -> Iterator[None]:
        """Hold one per-job thread/process lock across the full launch commit."""
        path = self._async_launch_lock_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        key = str(path)
        with _ASYNC_LAUNCH_LOCKS_GUARD:
            local_lock = _ASYNC_LAUNCH_LOCKS.setdefault(key, threading.RLock())
        with local_lock, path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _complete_launch_identity(identity: ProcessIdentity) -> bool:
        """Return whether a worker identity is exact enough to publish running."""
        return bool(
            identity.verifiable and identity.process_group_id > 0 and identity.session_id > 0
        )

    def _publish_async_spec_fence(self, entry: LedgerEntry) -> None:
        """Atomically make one nonce the job-global worker-entry fence."""
        path = self._async_spec_path(entry.spec.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            path,
            {
                "version": 2,
                "launch_nonce": entry.launch_nonce,
                "launch_attempt": entry.launch_attempt,
                "spec": entry.spec.to_mapping(),
            },
            sort_keys=True,
        )

    def _invalidate_async_spec_fence(self, entry: LedgerEntry, *, reason: str) -> None:
        """Atomically fence a terminal launch nonce out of worker backend entry."""
        path = self._async_spec_path(entry.spec.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            path,
            {
                "version": 2,
                "launch_nonce": "",
                "superseded_launch_sha256": process_token_sha256(entry.launch_nonce),
                "status": reason,
            },
            sort_keys=True,
        )

    def _reserve_async_launch(self, job_id: str) -> tuple[LedgerEntry, bool]:
        """Persist one launch nonce before any spec write or process creation."""

        def reserve(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.state in {"deployed", "running"}:
                return (current, False), []
            if current.state != "proposed":
                raise RuntimeError(
                    f"dispatch job {job_id!r} cannot launch from terminal state {current.state}"
                )
            in_flight = sum(
                1 for raw in ledger if str(raw.get("state", "")) in {"deployed", "running"}
            )
            if in_flight >= self._cap:
                raise RuntimeError(f"dispatch cap reached ({in_flight}/{self._cap} jobs in flight)")
            deployed = current.with_state(
                "deployed",
                launch_nonce=secrets.token_urlsafe(32),
                launch_started_at=_now_iso(),
                launch_attempt=max(1, current.launch_attempt + 1),
                notes="async worker launch reserved",
            )
            ledger[index] = deployed.to_mapping()
            return (deployed, True), [deployed]

        return self._transaction(
            reserve,
            summary_admission=self._async_launch_admission,
        )

    def _reserve_async_launch_retry(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        """Rotate one stale launch nonce using a persisted compare-and-swap."""

        def reserve(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.state != "deployed" or current.launch_nonce != entry.launch_nonce:
                return (current, False), []
            if current.launch_attempt >= ASYNC_LAUNCH_MAX_ATTEMPTS:
                self._invalidate_async_spec_fence(
                    current,
                    reason="launch-attempts-exhausted",
                )
                failed = current.with_state(
                    "failed",
                    finished_at=_now_iso(),
                    notes=(
                        "async worker launch produced no exact identity after "
                        f"{current.launch_attempt} attempt(s)"
                    ),
                )
                ledger[index] = failed.to_mapping()
                return (failed, False), [failed]
            retried = replace(
                current,
                launch_nonce=secrets.token_urlsafe(32),
                launch_started_at=_now_iso(),
                launch_attempt=current.launch_attempt + 1,
                process_id=0,
                process_group_id=0,
                process_session_id=0,
                process_token_sha256="",
                notes="retrying incomplete async worker launch",
            )
            # Publish the new current-nonce fence before the ledger update.
            # A crash between these writes leaves the old ledger with a newer
            # spec (safe rejection), never a new ledger with an old spec that a
            # suspended worker could accept.
            self._publish_async_spec_fence(retried)
            ledger[index] = retried.to_mapping()
            return (retried, True), []

        return self._transaction(reserve)

    def _write_async_launch_spec(self, entry: LedgerEntry) -> None:
        """Publish a nonce-bound worker spec after the ledger reservation."""
        spec_path = self._async_spec_path(entry.spec.job_id, entry.launch_nonce)
        result_path = self._async_result_path(entry.spec.job_id, entry.launch_nonce)
        identity_path = self._async_identity_path(entry.spec.job_id, entry.launch_nonce)
        evidence_path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear only this nonce's partial artifacts. Older attempts retain
        # disjoint paths, so a delayed child can never clobber this launch.
        result_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        evidence_path.unlink(missing_ok=True)
        self._publish_async_spec_fence(entry)

    def _spawn_async_worker(self, entry: LedgerEntry) -> ProcessIdentity:
        """Start one isolated worker and publish its exact identity receipt."""
        log_path = self._async_paths(entry.spec.job_id)[2]
        spec_path = self._async_spec_path(entry.spec.job_id, entry.launch_nonce)
        result_path = self._async_result_path(entry.spec.job_id, entry.launch_nonce)
        identity_path = self._async_identity_path(entry.spec.job_id, entry.launch_nonce)
        evidence_path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        launch_lock_path = self._async_launch_lock_path(entry.spec.job_id)
        command = [
            sys.executable,
            "-m",
            DISPATCH_WORKER_MODULE,
            "--spec-file",
            str(spec_path),
            "--result-file",
            str(result_path),
            f"--launch-nonce={entry.launch_nonce}",
            "--identity-file",
            str(identity_path),
            "--evidence-file",
            str(evidence_path),
            "--launch-lock-file",
            str(launch_lock_path),
            "--parent-pid",
            str(os.getpid()),
        ]
        env = dict(os.environ)
        env["LEANFLOW_DISPATCH_WORKER"] = "1"
        # Keep the parent campaign's live-actor capacity stable even though
        # dispatch_worker disables nested research mode locally.
        env.setdefault(
            BACKGROUND_PROVIDER_CAPACITY_ENV,
            str(background_provider_capacity()),
        )
        process_token = secrets.token_urlsafe(32)
        env[PROCESS_TOKEN_ENV] = process_token
        project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        identity = ProcessIdentity(
            pid=int(process.pid),
            process_group_id=int(process.pid),
            session_id=int(process.pid),
            token_sha256=process_token_sha256(process_token),
        )
        if not self._complete_launch_identity(identity):
            raise RuntimeError("worker launch did not produce an exact process identity")
        # The child writes the same receipt as its first main() action. The
        # parent's immediate receipt closes the ordinary Popen/ledger gap;
        # the child copy covers a parent crash between those two operations.
        atomic_json_write(
            identity_path,
            {
                "version": 1,
                "launch_nonce": entry.launch_nonce,
                "launch_attempt": entry.launch_attempt,
                "process_id": identity.pid,
                "process_group_id": identity.process_group_id,
                "process_session_id": identity.session_id,
                "process_token_sha256": identity.token_sha256,
                "parent_process_id": os.getpid(),
                "published_at": _now_iso(),
            },
            sort_keys=True,
        )
        return identity

    def _commit_async_running(
        self,
        entry: LedgerEntry,
        identity: ProcessIdentity,
    ) -> LedgerEntry:
        """Publish running only for the exact still-reserved launch nonce."""
        if not self._complete_launch_identity(identity):
            raise ValueError("cannot publish running without an exact process identity")

        def commit(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.state != "deployed" or current.launch_nonce != entry.launch_nonce:
                return current, []
            running = current.with_state(
                "running",
                started_at=_now_iso(),
                process_id=identity.pid,
                process_group_id=identity.process_group_id,
                process_session_id=identity.session_id,
                process_token_sha256=identity.token_sha256,
                notes="",
            )
            ledger[index] = running.to_mapping()
            return running, [running]

        return self._transaction(commit)

    def _fail_async_launch(self, entry: LedgerEntry, note: str) -> LedgerEntry:
        """Fail one exact launch reservation without overwriting a race winner."""

        def fail(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.state != "deployed" or current.launch_nonce != entry.launch_nonce:
                return current, []
            self._invalidate_async_spec_fence(current, reason="launch-failed")
            failed = current.with_state(
                "failed",
                finished_at=_now_iso(),
                notes=note[:300],
            )
            ledger[index] = failed.to_mapping()
            return failed, [failed]

        return self._transaction(fail)

    def _launch_reserved_async(
        self,
        entry: LedgerEntry,
        *,
        _lock_held: bool = False,
    ) -> LedgerEntry:
        """Write, spawn, and commit one already-durable launch reservation."""
        if not _lock_held:
            with self._async_launch_lock(entry.spec.job_id):
                return self._launch_reserved_async(entry, _lock_held=True)
        # A launcher can resume after another process recovered and rotated its
        # nonce. Recheck under the sidecar lock before touching the shared spec
        # or creating a process; the persisted winner is then returned intact.
        current = self._entry(entry.spec.job_id)
        if current.state != "deployed" or current.launch_nonce != entry.launch_nonce:
            return current
        try:
            self._write_async_launch_spec(entry)
            identity = self._spawn_async_worker(entry)
        except Exception as exc:
            logger.debug("dispatch worker launch failed", exc_info=True)
            return self._fail_async_launch(entry, f"worker launch failed: {str(exc)[:300]}")
        running = self._commit_async_running(entry, identity)
        if (
            running.state != "running"
            or running.launch_nonce != entry.launch_nonce
            or running.process_id != identity.pid
        ):
            # A kill or another retry won the ledger CAS after Popen. Signal
            # only the exact process we just created; never leave a duplicate.
            launched = replace(
                entry,
                state="running",
                process_id=identity.pid,
                process_group_id=identity.process_group_id,
                process_session_id=identity.session_id,
                process_token_sha256=identity.token_sha256,
            )
            _terminate_dispatch_process(launched)
        return running

    def deploy_async(self, job_id: str) -> LedgerEntry:
        """Reserve, launch, and exactly identify one process-isolated job."""
        if not dispatch_enabled():
            raise RuntimeError("dispatch is disabled (LEANFLOW_DISPATCH_ENABLED is off)")
        with self._async_launch_lock(job_id):
            entry, reserved = self._reserve_async_launch(job_id)
            if reserved:
                return self._launch_reserved_async(entry, _lock_held=True)
            if entry.state == "deployed":
                return self._recover_deployed_launch(
                    entry,
                    retry_if_stale=True,
                    _lock_held=True,
                )
            return entry

    def _launch_handshake_expired(self, entry: LedgerEntry) -> bool:
        """Return whether a deployed launch missed its short identity window."""
        try:
            launched_at = datetime.fromisoformat(entry.launch_started_at)
            if launched_at.tzinfo is None:
                launched_at = launched_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return True
        return (datetime.now(UTC) - launched_at).total_seconds() >= max(
            0.0,
            ASYNC_LAUNCH_HANDSHAKE_GRACE_S,
        )

    def _launch_identity(self, entry: LedgerEntry) -> tuple[ProcessIdentity, int] | None:
        """Return exact worker/parent identity only for this launch nonce."""
        path = self._async_identity_path(entry.spec.job_id, entry.launch_nonce)
        if not path.is_file():
            return None
        payload = read_json_file(path)
        if str(payload.get("launch_nonce", "") or "") != entry.launch_nonce:
            return None
        identity = process_identity_from_mapping(payload)
        if not self._complete_launch_identity(identity):
            return None
        try:
            parent_process_id = int(payload.get("parent_process_id", 0) or 0)
        except (TypeError, ValueError):
            parent_process_id = 0
        return identity, parent_process_id

    def _bound_async_result(self, entry: LedgerEntry) -> dict[str, Any] | None:
        """Read a result artifact only when its launch nonce is authoritative."""
        result_path = self._async_result_path(entry.spec.job_id, entry.launch_nonce)
        if not result_path.is_file():
            return None
        payload = read_json_file(result_path)
        artifact_nonce = str(payload.get("launch_nonce", "") or "")
        if entry.launch_nonce:
            if artifact_nonce != entry.launch_nonce:
                return None
        elif artifact_nonce:
            # Legacy ledger entries had no nonce; retain their historical
            # unbound artifacts, but never let a modern nonce target them.
            return None
        return payload

    def _bound_incremental_evidence(self, entry: LedgerEntry) -> list[dict[str, Any]]:
        """Read only exact-launch worker evidence that still requires parent recheck."""
        if not entry.launch_nonce:
            return []
        path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        return dispatch_incremental_evidence.load_checked_helpers(
            path,
            launch_nonce=entry.launch_nonce,
            spec=entry.spec,
        )

    def _discard_incremental_evidence(self, entry: LedgerEntry) -> None:
        """Remove a redundant journal after its complete result is durable."""
        if not entry.launch_nonce:
            return
        path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    def _harvest_incremental_evidence(
        self,
        entry: LedgerEntry,
        *,
        note: str,
    ) -> LedgerEntry:
        """Promote exited-worker helper evidence into a partial consumable finding."""
        if entry.state != "running" or not entry.process_id:
            return entry
        helpers = self._bound_incremental_evidence(entry)
        if not helpers:
            return entry
        path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        result = dispatch_incremental_evidence.interrupted_result(
            spec=entry.spec,
            helpers=helpers,
            artifact_path=path,
        )
        if not result:
            return entry
        try:
            return self._transition(
                entry.spec.job_id,
                "done",
                finished_at=_now_iso(),
                result=result,
                notes=str(note)[:300],
            )
        except ValueError:
            return self._entry(entry.spec.job_id)

    def _recover_deployed_launch(
        self,
        entry: LedgerEntry,
        *,
        retry_if_stale: bool,
        _lock_held: bool = False,
    ) -> LedgerEntry:
        """Adopt, harvest, or retry every durable async-launch crash window."""
        if not _lock_held:
            with self._async_launch_lock(entry.spec.job_id):
                return self._recover_deployed_launch(
                    self._entry(entry.spec.job_id),
                    retry_if_stale=retry_if_stale,
                    _lock_held=True,
                )
        if entry.state != "deployed" or not entry.launch_nonce:
            return entry
        receipt = self._launch_identity(entry)
        if receipt is not None:
            identity, launch_parent_process_id = receipt
            launch_entry = replace(
                entry,
                state="running",
                process_id=identity.pid,
                process_group_id=identity.process_group_id,
                process_session_id=identity.session_id,
                process_token_sha256=identity.token_sha256,
            )
            # A completed artifact remains authoritative after process exit.
            # Otherwise adopt only a still-live exact identity; a child that
            # died in the Popen/commit crash window did no recoverable work and
            # should be retried without first publishing a false running state.
            completed_result = self._bound_async_result(entry) is not None
            incremental_evidence = bool(self._bound_incremental_evidence(entry))
            # A modern nonce-bound receipt is only adoptable by the exact
            # launcher that published it.  A missing/invalid parent id is
            # ambiguous crash evidence, not backward-compatible ownership.
            same_parent = launch_parent_process_id == os.getpid()
            identity_is_live = same_parent and _dispatch_process_identity_is_live(launch_entry)
            if completed_result or identity_is_live:
                running = self._commit_async_running(entry, identity)
                if running.state != "running":
                    return running
                return self._harvest_async_result(running)
            # A worker keeps a parent-liveness guard for the process that
            # launched it. After a real runner restart it cannot be adopted by
            # the new PID. Retire that exact identity synchronously before
            # rotating the nonce; otherwise a cooperative SIGTERM handler
            # could leave old and replacement provider work overlapping.
            if same_parent:
                # A token lookup can fail transiently while the original child
                # still owns its provider and Lean processes. Neither checked
                # evidence nor retry capacity may cross that ambiguous boundary.
                if not _dispatch_process_identity_has_exited(launch_entry):
                    return entry
            elif not _terminate_dispatch_process_and_wait(launch_entry):
                return entry
            if incremental_evidence:
                running = self._commit_async_running(entry, identity)
                if running.state != "running":
                    return running
                return self._harvest_incremental_evidence(
                    running,
                    note=(
                        "recovered worker-checked helper evidence after interrupted "
                        "launch ownership; parent recheck required"
                    ),
                )
            if not retry_if_stale:
                return entry
            retried, reserved = self._reserve_async_launch_retry(entry)
            if not reserved:
                return retried
            return self._launch_reserved_async(retried, _lock_held=True)
        # The original launcher may still be between its durable reservation,
        # spec write, Popen, and identity publication. Give every incomplete
        # artifact shape the same short handshake before nonce rotation; this
        # prevents a concurrent reconciler from mistaking an in-progress launch
        # for a crashed one and spawning a duplicate worker.
        if not self._launch_handshake_expired(entry):
            return entry
        # A worker publishes exact identity and then rechecks the nonce-bound
        # spec before entering its backend. No receipt after the bounded
        # handshake therefore means this nonce never began job work and is safe
        # to rotate.
        if not retry_if_stale:
            return entry
        retried, reserved = self._reserve_async_launch_retry(entry)
        if not reserved:
            return retried
        return self._launch_reserved_async(retried, _lock_held=True)

    def _harvest_async_result(
        self,
        entry: LedgerEntry,
        *,
        accept_failure: bool = True,
    ) -> LedgerEntry:
        """Promote a completed worker result into the ledger once.

        Set ``accept_failure`` false after this process intentionally signals
        a worker.  The worker's termination handler publishes an ``ok: false``
        artifact while shutting down; that artifact describes the requested
        cancellation, not an independent job failure.  A successful artifact
        may still win the shutdown race and remains harvestable.
        """
        if entry.state != "running" or not entry.process_id:
            return entry
        payload = self._bound_async_result(entry)
        if payload is None:
            return entry
        process_exit_confirmed = bool(
            _reap_process(entry.process_id, block=True)
            or _dispatch_process_identity_has_exited(entry)
        )
        if not process_exit_confirmed:
            # Result publication precedes the final Python process boundary.
            # Retain capacity until reaping or exact structural exit evidence
            # proves that worker cleanup can no longer consume RAM/providers.
            return entry
        worker_ok = bool(payload.get("ok"))
        result = dict(payload.get("result") or {})
        status = str(result.get("status", "") or "")
        error = str(payload.get("error", "") or "")
        worker_succeeded = worker_ok and status in {"done", "ok", "success"}
        if worker_succeeded:
            incremental_helpers = self._bound_incremental_evidence(entry)
            if incremental_helpers:
                raw_deliverable = result.get("deliverable")
                deliverable = dict(raw_deliverable) if isinstance(raw_deliverable, Mapping) else {}
                deliverable = _attach_checked_helpers(deliverable, incremental_helpers)
                result["deliverable"] = _cap_deliverable_preserving_exact_code(
                    _bound_deliverable_preserving_exact_code(deliverable)
                )
        if not worker_succeeded:
            recovered = self._harvest_incremental_evidence(
                entry,
                note=(
                    "recovered worker-checked helper evidence after interrupted worker; "
                    "parent recheck required"
                ),
            )
            if recovered.state != "running":
                return recovered
        if not worker_succeeded and not accept_failure:
            return entry
        interruption_reason = _worker_interruption_reason(
            worker_ok=worker_ok,
            status=status,
            error=error,
        )
        if worker_succeeded:
            final_state = "done"
        elif interruption_reason:
            final_state = "killed"
        else:
            final_state = "failed"
        changes: dict[str, Any] = {"finished_at": _now_iso(), "result": result}
        if interruption_reason:
            changes["notes"] = f"worker interrupted: {interruption_reason}"[:300]
        elif not worker_ok:
            changes["notes"] = (error or "worker failed")[:300]
        elif not worker_succeeded:
            changes["notes"] = _worker_result_failure_note(status, result)
        try:
            updated = self._transition(entry.spec.job_id, final_state, **changes)
        except ValueError:
            updated = self._entry(entry.spec.job_id)
        if (
            worker_succeeded
            and updated.state == "done"
            and not updated.result.get("partial_worker_evidence")
        ):
            self._discard_incremental_evidence(updated)
        return updated

    def _await_async_completion_boundary(
        self,
        entry: LedgerEntry,
    ) -> tuple[LedgerEntry, bool]:
        """Recheck an apparently exited worker for one bounded publication grace.

        Exact identity lookup can transiently fail while a modern isolated
        worker is closing its service tree and atomically publishing its
        result. Legacy PID-only entries receive no grace because their owner
        cannot be revalidated safely. Return the freshest entry and whether
        exact live-process evidence reappeared.
        """
        identity = entry.process_identity()
        grace_s = max(0.0, ASYNC_RESULT_PUBLICATION_GRACE_S)
        if entry.state != "running" or not identity.verifiable or grace_s <= 0:
            return entry, False
        deadline = time.monotonic() + grace_s
        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return entry, False
            time.sleep(min(max(0.001, ASYNC_RESULT_RECHECK_INTERVAL_S), remaining_s))
            entry = self._harvest_async_result(entry)
            if entry.state != "running":
                return entry, False
            if _dispatch_process_identity_is_live(entry):
                return entry, True

    def _recover_completed_artifact(self, entry: LedgerEntry) -> LedgerEntry | None:
        """Recover a successful artifact published before a stale terminal verdict.

        Shutdown and reconciliation can race an already-published process
        result: the ledger may become ``killed`` or ``failed`` with the exact
        dead-process verdict even though the atomic result file is complete.
        Recovery is deliberately narrower than a normal state transition. It
        accepts only those race verdicts, process-isolated jobs, successful
        terminal payloads, and artifacts whose mtime predates the persisted
        terminal time.
        """
        recoverable_verdict = entry.state == "killed" or (
            entry.state == "failed" and entry.notes == "agent process died"
        )
        if (
            not recoverable_verdict
            or not entry.process_id
            or not entry.finished_at
            or entry.consumed
            or entry.result
        ):
            return None
        if not entry.process_identity().verifiable or not _dispatch_process_identity_has_exited(
            entry
        ):
            return None
        result_path = self._async_result_path(entry.spec.job_id, entry.launch_nonce)
        if not result_path.is_file():
            return None
        try:
            killed_at = datetime.fromisoformat(entry.finished_at)
            if killed_at.tzinfo is None:
                killed_at = killed_at.replace(tzinfo=UTC)
            artifact_at = datetime.fromtimestamp(result_path.stat().st_mtime, tz=UTC)
        except (OSError, ValueError):
            return None
        # Ledger timestamps have one-second precision. The worker is reaped
        # before the kill verdict is persisted, so this margin covers only the
        # truncated terminal second and cannot admit a later live-worker write.
        if artifact_at >= killed_at.astimezone(UTC) + timedelta(seconds=1):
            return None
        payload = self._bound_async_result(entry)
        if payload is None:
            return None
        result_raw = payload.get("result")
        if not bool(payload.get("ok")) or not isinstance(result_raw, Mapping):
            return None
        result = dict(result_raw)
        if str(result.get("status", "") or "") not in {"done", "ok", "success"}:
            return None
        evidence_path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        incremental_helpers: list[dict[str, Any]] = []
        try:
            evidence_at = datetime.fromtimestamp(evidence_path.stat().st_mtime, tz=UTC)
        except OSError:
            pass
        else:
            if evidence_at < killed_at.astimezone(UTC) + timedelta(seconds=1):
                incremental_helpers = self._bound_incremental_evidence(entry)
        if incremental_helpers:
            raw_deliverable = result.get("deliverable")
            deliverable = dict(raw_deliverable) if isinstance(raw_deliverable, Mapping) else {}
            deliverable = _attach_checked_helpers(deliverable, incremental_helpers)
            result["deliverable"] = _cap_deliverable_preserving_exact_code(
                _bound_deliverable_preserving_exact_code(deliverable)
            )

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            current_recoverable = current.state == "killed" or (
                current.state == "failed" and current.notes == "agent process died"
            )
            if (
                not current_recoverable
                or current.finished_at != entry.finished_at
                or current.process_identity() != entry.process_identity()
                or not current.process_identity().verifiable
                or not _dispatch_process_identity_has_exited(current)
                or current.consumed
                or current.result
            ):
                return None, []
            recovered = replace(
                current,
                state="done",
                result=result,
                notes="recovered completed result artifact published before terminal verdict",
            )
            ledger[index] = recovered.to_mapping()
            return recovered, [recovered]

        recovered = self._transaction(mutate)
        if recovered is not None and incremental_helpers:
            self._discard_incremental_evidence(recovered)
        return recovered

    def _recover_incremental_evidence_artifact(
        self,
        entry: LedgerEntry,
    ) -> LedgerEntry | None:
        """Recover pre-verdict helper evidence left by an interrupted worker."""
        if (
            entry.state not in {"killed", "failed"}
            or not entry.process_id
            or not entry.launch_nonce
            or not entry.finished_at
            or entry.consumed
            or entry.result
        ):
            return None
        if not entry.process_identity().verifiable or not _dispatch_process_identity_has_exited(
            entry
        ):
            return None
        path = self._async_incremental_evidence_path(
            entry.spec.job_id,
            entry.launch_nonce,
        )
        try:
            finished_at = datetime.fromisoformat(entry.finished_at)
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=UTC)
            artifact_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except (OSError, ValueError):
            return None
        if artifact_at >= finished_at.astimezone(UTC) + timedelta(seconds=1):
            return None
        helpers = self._bound_incremental_evidence(entry)
        if not helpers:
            return None
        result = dispatch_incremental_evidence.interrupted_result(
            spec=entry.spec,
            helpers=helpers,
            artifact_path=path,
        )
        if not result:
            return None

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, entry.spec.job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {entry.spec.job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if (
                current.state not in {"killed", "failed"}
                or current.finished_at != entry.finished_at
                or current.launch_nonce != entry.launch_nonce
                or current.process_identity() != entry.process_identity()
                or not current.process_identity().verifiable
                or not _dispatch_process_identity_has_exited(current)
                or current.consumed
                or current.result
            ):
                return None, []
            recovered = replace(
                current,
                state="done",
                result=result,
                notes=(
                    "recovered pre-interruption worker-checked helper evidence; "
                    "parent recheck required"
                ),
            )
            ledger[index] = recovered.to_mapping()
            return recovered, [recovered]

        return self._transaction(mutate)

    def recover_completed_artifacts(self) -> list[LedgerEntry]:
        """Recover all pre-verdict results and checked-helper journals exactly once."""
        recovered: list[LedgerEntry] = []
        for entry in self._load_ledger():
            restored = self._recover_completed_artifact(entry)
            if restored is None:
                restored = self._recover_incremental_evidence_artifact(entry)
            if restored is not None:
                recovered.append(restored)
        return recovered

    def join(self, job_id: str, timeout_s: int | None = None) -> LedgerEntry:
        """Wait for an async job up to timeout_s; sync jobs return immediately."""
        deadline = None if timeout_s is None else time.monotonic() + max(0, timeout_s)
        while True:
            entry = self._entry(job_id)
            if entry.state == "deployed" and entry.launch_nonce:
                self.reconcile()
                entry = self._entry(job_id)
            entry = self._harvest_async_result(entry)
            if entry.is_terminal():
                return entry
            if entry.state != "deployed" and not entry.process_id:
                return entry
            if deadline is not None and time.monotonic() >= deadline:
                return entry
            time.sleep(0.1)

    def poll(self, job_id: str) -> dict[str, Any]:
        """Reconciled status snapshot for one job."""
        entry = self._entry(job_id)
        self._harvest_async_result(entry)
        self.reconcile()
        entry = self._entry(job_id)
        return {
            "job_id": job_id,
            "state": entry.state,
            "archetype": entry.spec.archetype,
            "agent_session_ids": list(entry.agent_session_ids),
            "started_at": entry.started_at,
            "finished_at": entry.finished_at,
            "consumed": entry.consumed,
            "notes": entry.notes,
        }

    def kill(self, job_id: str, *, requester_job_id: str) -> dict[str, Any]:
        """Cancel a job only after proving its exact worker process exited.

        Serialize against modern launch publication. If TERM/KILL cannot prove
        the persisted PID/session boundary is gone, leave the row nonterminal
        and return ``killed=False`` so callers cannot reuse live capacity.
        """
        entry = self._entry(job_id)
        if entry.launch_nonce:
            with self._async_launch_lock(job_id):
                return self._kill_unlocked(job_id, requester_job_id=requester_job_id)
        return self._kill_unlocked(job_id, requester_job_id=requester_job_id)

    def _kill_unlocked(self, job_id: str, *, requester_job_id: str) -> dict[str, Any]:
        """Ancestor-gated kill (owner N3): only ancestors, the root, or a human."""
        allowed = requester_job_id in {self.root_job_id, "human"} or is_ancestor(
            requester_job_id, job_id
        )
        if not allowed:
            raise PermissionError(
                f"{requester_job_id!r} is not an ancestor of {job_id!r} and may not kill it"
            )
        entry = self._entry(job_id)
        if entry.state == "running" and entry.process_id:
            # An explicit cancellation owns the terminal verdict. Preserve a
            # completed success that won the race, but do not let the worker's
            # signal-raised interruption artifact become an independent
            # failure before the parent persists ``killed``.
            entry = self._harvest_async_result(entry, accept_failure=False)
        if entry.is_terminal() and entry.state != "stuck":
            return {"job_id": job_id, "state": entry.state, "killed": False}
        details: dict[str, Any] = {}
        process_exit_confirmed = True
        if entry.run_id or entry.process_id:
            for session_id in entry.agent_session_ids:
                details.setdefault("descendants", []).append(
                    terminate_workflow_agent_descendants(session_id)
                )
                details.setdefault("agents", []).append(terminate_workflow_agent(session_id))
            if entry.process_id:
                already_exited = _dispatch_process_identity_has_exited(entry)
                process_exit_confirmed = bool(
                    already_exited or _terminate_dispatch_process_and_wait(entry)
                )
                details["process_terminated"] = bool(process_exit_confirmed and not already_exited)
                details["process_reaped"] = process_exit_confirmed
                details["process_identity_verified"] = process_exit_confirmed
                details["process_exit_confirmed"] = process_exit_confirmed
            # The process may have atomically published its result between the
            # first harvest check and signal delivery. Reconcile that final
            # successful artifact before persisting a kill verdict. Ignore a
            # failure artifact produced by the requested SIGTERM itself.
            if entry.process_id:
                entry = self._harvest_async_result(
                    self._entry(job_id),
                    accept_failure=False,
                )
                if entry.state == "running" and process_exit_confirmed:
                    entry = self._harvest_incremental_evidence(
                        entry,
                        note=(
                            "recovered worker-checked helper evidence during shutdown; "
                            "parent recheck required"
                        ),
                    )
                if entry.is_terminal() and entry.state != "stuck":
                    return {
                        "job_id": job_id,
                        "state": entry.state,
                        "killed": False,
                        **details,
                    }
            if not process_exit_confirmed:
                # A terminal ledger verdict would incorrectly free actor
                # capacity while the exact worker may still own its provider
                # call. Leave the durable job nonterminal for retry/recovery.
                entry = self._entry(job_id)
                return {
                    "job_id": job_id,
                    "state": entry.state,
                    "killed": False,
                    **details,
                }
        elif self._parent_agent is not None:
            # Delegate-backend children are threads: v1 kill is cooperative —
            # the parent-wide interrupt reaches ALL children (documented).
            try:
                self._parent_agent.interrupt("dispatch kill: " + job_id)
                details["parent_interrupt"] = True
            except Exception:
                details["parent_interrupt"] = False
        if entry.state == "deployed" and entry.launch_nonce:
            # The public kill wrapper holds the same launch sidecar. Invalidate
            # the shared fence before publishing terminal state so a child
            # waiting on that lock cannot enter its backend after cancellation.
            self._invalidate_async_spec_fence(entry, reason="launch-killed")
        try:
            entry = self._transition(
                job_id,
                "killed",
                finished_at=_now_iso(),
                notes=f"killed by {requester_job_id}",
            )
        except ValueError:
            entry = self._entry(job_id)
            return {"job_id": job_id, "state": entry.state, "killed": False, **details}
        return {"job_id": job_id, "state": entry.state, "killed": True, **details}

    def consume(self, job_id: str) -> dict[str, Any]:
        """One-way result hand-off: bounded deliverable, never a raw transcript."""

        def mutate(ledger: list[dict[str, Any]]):
            index = self._find(ledger, job_id)
            if index < 0:
                raise KeyError(f"unknown dispatch job {job_id!r}")
            current = LedgerEntry.from_mapping(ledger[index])
            if current.consumed:
                raise RuntimeError(f"job {job_id} result was already consumed")
            if current.state != "done":
                raise RuntimeError(f"job {job_id} is {current.state}, not done")
            updated = LedgerEntry.from_mapping({**current.to_mapping(), "consumed": True})
            ledger[index] = updated.to_mapping()
            # Consumption is a one-way metadata mutation, not a second
            # transition into ``done``. The durable consumed flag is the audit.
            return dict(current.result), []

        result = self._transaction(mutate)
        return {
            "deliverable": result.get("deliverable") or {},
            "artifact_paths": list(result.get("artifact_paths") or []),
            "plan_delta": list(result.get("plan_delta") or []),
        }

    def list_descendants(self, job_id: str) -> list[LedgerEntry]:
        ledger = self._load_ledger()
        ids = set(descendants([entry.spec.job_id for entry in ledger], job_id))
        return [entry for entry in ledger if entry.spec.job_id in ids]

    def open_jobs(self) -> list[LedgerEntry]:
        """Non-terminal entries — the never-silently-lost audit (N1)."""
        summary = read_json_file(self._summary_path())
        entries: list[LedgerEntry] = []
        for raw in summary.get(_LEDGER_KEY) or []:
            if not isinstance(raw, Mapping):
                continue
            raw_state = raw.get("state")
            if isinstance(raw_state, str) and raw_state in TERMINAL_STATES:
                # Archive compaction preserves ``state`` as stable hot
                # lifecycle metadata. Terminal rows cannot own recovery work,
                # so avoid loading their exact cold result payloads here.
                continue
            hydrated = dispatch_ledger_compaction.hydrate_dispatch_record(
                raw,
                state_root=workflow_state_root(),
            )
            entry = LedgerEntry.from_mapping(hydrated)
            if not entry.is_terminal():
                entries.append(entry)
        return entries

    def shutdown_audit_entries(self) -> list[LedgerEntry]:
        """Return only rows that can still own a process at campaign shutdown.

        Complete campaign ledgers can contain hundreds of cold terminal result
        archives. Shutdown needs neither those payloads nor terminal rows that
        never owned a process. Keep malformed/unsupported killed-process
        evidence in the audit so quiescence continues to fail closed.
        """
        summary = read_json_file(self._summary_path())
        entries: list[LedgerEntry] = []
        for raw in summary.get(_LEDGER_KEY) or []:
            if not isinstance(raw, Mapping):
                continue
            raw_state = raw.get("state")
            if isinstance(raw_state, str) and raw_state in TERMINAL_STATES:
                if raw_state != "killed":
                    continue
                try:
                    process_id = int(raw.get("process_id", 0) or 0)
                except (TypeError, ValueError):
                    # Preserve the complete-ledger behavior for corrupt process
                    # metadata instead of silently declaring shutdown safe.
                    process_id = 2
                if process_id <= 1:
                    continue
                safe_legacy_release = bool(
                    not str(raw.get("launch_nonce", "") or "").strip()
                    and not str(raw.get("process_token_sha256", "") or "").strip()
                    and str(raw.get("process_released_at", "") or "").strip()
                    and str(raw.get("process_release_reason", "") or "")
                    in SAFE_LEGACY_PROCESS_RELEASE_REASONS
                )
                if safe_legacy_release:
                    continue
            hydrated = dispatch_ledger_compaction.hydrate_dispatch_record(
                raw,
                state_root=workflow_state_root(),
            )
            entry = LedgerEntry.from_mapping(hydrated)
            if not entry.is_terminal() or entry.state == "killed":
                entries.append(entry)
        return entries

    def entries(self) -> list[LedgerEntry]:
        """Return an immutable snapshot of the complete dispatch ledger."""
        return self._load_ledger()

    # ----- reconciliation --------------------------------------------------

    def reconcile(self) -> list[LedgerEntry]:
        """Cross-check running entries against agent evidence; never lose a job.

        A ``deployed`` async entry is a capacity-counted launch transaction:
        adopt its exact nonce-bound identity, or rotate/retry after the short
        handshake window. A live process identity is live evidence. An
        apparently exited modern worker gets one bounded result-publication
        grace before failure; dead delegate agents with no result fail
        immediately. Missing delegate evidence is only ``stuck`` after the
        two-clause patience test.
        """
        ledger = self._load_ledger()
        recovered_launches: list[LedgerEntry] = []
        for entry in ledger:
            if entry.state == "deployed" and entry.launch_nonce:
                with self._async_launch_lock(entry.spec.job_id):
                    current = self._entry(entry.spec.job_id)
                    entry = self._recover_deployed_launch(
                        current,
                        retry_if_stale=True,
                        _lock_held=True,
                    )
            recovered_launches.append(entry)
        ledger = recovered_launches
        # Process-isolated workers have a self-contained PID/token identity and
        # result artifact. Avoid scanning the potentially large activity corpus
        # unless at least one delegate-backed running entry needs agent evidence.
        agents: dict[str, dict[str, Any]] = {}
        if any(entry.state == "running" and not entry.process_id for entry in ledger):
            # Real summaries key agents by "agent_id" (workflow_state.py);
            # accept the session-id spelling so either evidence shape works.
            for agent in summarize_workflow_agents():
                for key in ("agent_id", "agent_session_id"):
                    identifier = str(agent.get(key, "") or "")
                    if identifier:
                        agents[identifier] = dict(agent)
        now = datetime.now(UTC)
        updated: list[LedgerEntry] = []
        for entry in ledger:
            if entry.state != "running":
                updated.append(entry)
                continue
            if entry.process_id:
                # Another job's poll can reconcile this entry after the worker
                # atomically publishes its result but before the parent harvests
                # it. Harvest every visible artifact before liveness probing;
                # a zombie/dead-process verdict must never overwrite a valid
                # structured deliverable.
                timeout_pending = entry.notes == WALL_CLOCK_TERMINATION_PENDING_NOTE
                if not timeout_pending:
                    entry = self._harvest_async_result(entry)
                    if entry.state != "running":
                        updated.append(entry)
                        continue
                process_is_live = _dispatch_process_identity_is_live(entry)
                if not process_is_live and not timeout_pending:
                    entry, process_is_live = self._await_async_completion_boundary(entry)
                    if entry.state != "running":
                        updated.append(entry)
                        continue
                if process_is_live:
                    if wall_clock_exceeded(
                        started_at=entry.started_at,
                        wall_clock_s=entry.spec.budget.wall_clock_s,
                        now=now,
                    ):
                        # Publish the nonterminal intent before signaling. A
                        # concurrent reconciler must not harvest the worker's
                        # signal-induced interruption artifact and free the
                        # actor slot while this exact process still exists.
                        entry = self._mark_running_process_note(
                            entry,
                            WALL_CLOCK_TERMINATION_PENDING_NOTE,
                        )
                        if entry.state != "running":
                            updated.append(entry)
                            continue
                        if not _terminate_dispatch_process_and_wait(entry):
                            updated.append(entry)
                            continue
                        # A successful result that won the termination race is
                        # still authoritative. Ignore signal-induced failure
                        # artifacts and persist the timeout only after exact
                        # process exit has been established.
                        entry = self._harvest_async_result(
                            self._entry(entry.spec.job_id),
                            accept_failure=False,
                        )
                        if entry.state == "running":
                            entry = self._harvest_incremental_evidence(
                                entry,
                                note=(
                                    "recovered worker-checked helper evidence at wall-clock "
                                    "shutdown; parent recheck required"
                                ),
                            )
                        if entry.state == "running":
                            try:
                                entry = self._transition(
                                    entry.spec.job_id,
                                    "failed",
                                    finished_at=_now_iso(),
                                    notes=WALL_CLOCK_EXIT_CONFIRMED_NOTE,
                                )
                            except ValueError:
                                entry = self._entry(entry.spec.job_id)
                        updated.append(entry)
                        continue
                    updated.append(entry)
                    continue
                if not _dispatch_process_identity_has_exited(entry):
                    note = (
                        WALL_CLOCK_TERMINATION_PENDING_NOTE
                        if timeout_pending
                        else PROCESS_EXIT_UNCONFIRMED_NOTE
                    )
                    entry = self._mark_running_process_note(entry, note)
                    updated.append(entry)
                    continue
                if timeout_pending:
                    entry = self._harvest_async_result(
                        self._entry(entry.spec.job_id),
                        accept_failure=False,
                    )
                    failure_note = WALL_CLOCK_EXIT_CONFIRMED_NOTE
                else:
                    failure_note = "agent process died"
                if entry.state == "running":
                    entry = self._harvest_incremental_evidence(
                        entry,
                        note=(
                            "recovered worker-checked helper evidence after worker exit; "
                            "parent recheck required"
                        ),
                    )
                if entry.state == "running":
                    try:
                        entry = self._transition(
                            entry.spec.job_id,
                            "failed",
                            finished_at=_now_iso(),
                            notes=failure_note,
                        )
                    except ValueError:
                        entry = self._entry(entry.spec.job_id)
                updated.append(entry)
                continue
            statuses = [
                str(agents.get(session_id, {}).get("status", "") or "missing")
                for session_id in entry.agent_session_ids
            ]
            if (
                statuses
                and all(status in {"dead", "exited"} for status in statuses)
                and not entry.result
            ):
                entry = self._transition(
                    entry.spec.job_id,
                    "failed",
                    finished_at=_now_iso(),
                    notes="agent died without a result",
                )
            elif (not statuses or all(status == "missing" for status in statuses)) and (
                patience_exceeded(
                    started_at=entry.started_at,
                    wall_clock_s=entry.spec.budget.wall_clock_s,
                    now=now,
                    last_event_age_s=None,
                )
            ):
                entry = self._transition(
                    entry.spec.job_id,
                    "stuck",
                    finished_at=_now_iso(),
                    notes="no agent evidence past the patience window",
                )
            updated.append(entry)
        return updated

    # ----- backends ---------------------------------------------------------

    def _run_backend(self, spec: JobSpec) -> dict[str, Any]:
        if spec.archetype == "prover" and not spec.scope.get("scratch_only"):
            return self._run_spawn_job(spec)
        return self._run_delegate_job(spec)

    def _run_delegate_job(self, spec: JobSpec) -> dict[str, Any]:
        """Run isolated research archetypes through one bounded delegate task."""
        from tools.implementations.delegate_tool import (  # lazy, like lean_worker_dispatch
            delegate_task,
        )

        if self._parent_agent is None:
            raise RuntimeError("delegate backend requires a parent agent")
        delegated_toolsets = _delegate_toolsets(spec)
        locks: list[str] = [str(p) for p in (spec.scope.get("file_locks") or [])]
        owner_id = f"dispatch:{spec.job_id}"
        acquired: list[str] = []
        try:
            for path in locks:
                lock = acquire_file_lock(
                    path,
                    owner_id=owner_id,
                    purpose=f"dispatch:{spec.archetype}",
                    ttl_seconds=spec.budget.wall_clock_s,
                )
                if not lock.get("success"):
                    raise RuntimeError(f"file lock unavailable for {path}")
                acquired.append(path)
            prompt_inputs = {
                key: value
                for key, value in spec.inputs.items()
                if key
                not in {
                    research_route_context.ROUTE_CONTEXT_INPUT_KEY,
                    "route_anchor_finding_summary",
                }
            }
            context_lines = [
                f"Dispatch job {spec.job_id} ({spec.archetype}; requester {spec.requester_role}).",
                f"Deliverable schema: {spec.deliverable}. Report findings as compact JSON.",
                f"Inputs: {json.dumps(prompt_inputs, ensure_ascii=False, sort_keys=True)[:1500]}",
            ]
            raw_route_context = spec.inputs.get(research_route_context.ROUTE_CONTEXT_INPUT_KEY)
            normalized_route_context = (
                research_route_context.normalize_route_context(raw_route_context)
                if isinstance(raw_route_context, Mapping)
                else None
            )
            if normalized_route_context is not None:
                context_lines.extend(
                    [
                        "Authoritative bounded parent route/proof-shape context JSON:",
                        json.dumps(
                            normalized_route_context,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                )
            if spec.scope.get("scratch_only"):
                context_lines.extend(
                    [
                        "Scratch-only isolation contract: return structured deliverables only.",
                        "Do not create, modify, rename, or delete any project file, including "
                        "ad hoc Lean scratch files. Do not call apply_verified_patch, write_file, "
                        "or patch. Verify candidate code with lean_incremental_check's inline "
                        "replacement or another read/check-only Lean tool. Terminal access and "
                        "nested LLM advisor tools are not delegated to scratch workers.",
                        "When a helper candidate elaborates, call lean_incremental_check with "
                        "action=check_helper and pass the full exact declaration as replacement. "
                        "The parent captures successful calls automatically in canonical "
                        "checked_helpers; do not fabricate, summarize, or copy checked_helpers "
                        "into your final JSON.",
                        "If any checked helper was captured, your final JSON must include "
                        "checked_helper_route_disposition as exactly advance_current_route or "
                        "evidence_only, plus checked_helper_dependency_advanced naming the exact "
                        "open dependency it discharges. Use advance_current_route only when the "
                        "helper advances the current live route; helpers for rejected routes, "
                        "counterexamples to abandoned methods, and standalone obstructions are "
                        "evidence_only even when they elaborate.",
                    ]
                )
            if spec.archetype == "empirical":
                context_lines.extend(
                    [
                        "Use empirical_compute for bounded exact integer/Fraction experiments; "
                        "Fraction, gcd, and isqrt are preloaded. Arbitrary Python through terminal "
                        "remains denied. Keep each experiment small and report its exact bounds.",
                    ]
                )
            if spec.archetype == "decomposition":
                context_lines.extend(
                    [
                        "Decomposition is proposal-only. The parent process is the sole writer "
                        "for Lean files, plan state, and the dependency graph; do not claim or "
                        "attempt any shared-state mutation.",
                        "Return compact JSON under this exact source-backed schema:",
                        DECOMPOSITION_REPORT_CONTRACT,
                        DECOMPOSITION_REPORT_DURABLE_CAPS,
                        DECOMPOSITION_TARGET_SENTINEL_PROMPT,
                        "Every subgoal and dependency proposal must cite source_basis ids backed "
                        "by an exact local/Mathlib declaration, proof-state fact, preserved "
                        "research finding, or URL actually inspected in this job. Do not invent "
                        "citations. State why each child is strictly easier and avoid moving the "
                        "entire original difficulty into one helper.",
                        "These are candidates only. Even an inline-checked helper remains "
                        "parent-review-required; never emit graph updates, plan deltas, file "
                        "edits, or adoption claims.",
                    ]
                )
            route_anchor_job_id = str(spec.inputs.get("route_anchor_job_id", "") or "").strip()
            if route_anchor_job_id:
                raw_provenance = spec.inputs.get("route_anchor_provenance")
                anchor_summary = str(
                    spec.inputs.get("route_anchor_finding_summary", "") or ""
                ).strip()
                if not isinstance(raw_provenance, Mapping) or not anchor_summary:
                    raise RuntimeError(
                        f"evidence-derived job {spec.job_id} lacks its source finding payload"
                    )
                context_lines.extend(
                    [
                        "Evidence anchor (already gathered; consume it directly instead of "
                        "rediscovering it):",
                        json.dumps(
                            dict(raw_provenance),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "Bounded exact source finding JSON:",
                        anchor_summary,
                        "Anchor consumption key: "
                        + str(spec.inputs.get("route_anchor_consumption_key", "") or ""),
                    ]
                )
            if spec.archetype == "deep_search" or spec.deliverable == "findings_report":
                context_lines.extend(
                    [
                        "Research completion contract: use a query portfolio with materially "
                        "different formulations, and prefer web_search search_depth=deep with "
                        "alternate_queries when web research is relevant. Search snippets are "
                        "discovery only: inspect promising primary sources with web_fetch and "
                        "search/inspect cloned source when repository access is available. A "
                        "single empty, throttled, or timed-out backend is not exhaustion; continue "
                        "with surviving providers and reformulated queries.",
                        "The compact report must preserve queries_tried, providers_tried, "
                        "sources_read, and dead_ends with a reason for each rejected route. Set "
                        "exhausted=true only after those routes are genuinely exhausted. Do not "
                        "discard a useful partial construction merely because it does not close "
                        "the full target; report its exact mathematical delta and next check.",
                        "If you claim a replacement for the assigned dispatched target was "
                        "kernel/Lean checked, the report MUST include this exact schema:",
                        CHECKED_REPLACEMENT_CONTRACT,
                        "replacement must contain the full exact target declaration supplied to "
                        "the inline check, with no ellipsis or truncation. Copy "
                        "replacement_matches_target from the check result; never infer it from "
                        "the declaration name. A worker check is advisory: the parent will re-run "
                        "Lean before using or accepting it. If exact code or check metadata is "
                        "unavailable, label the candidate incomplete_unverified.",
                        "This checked_replacements contract is target-only. For auxiliary helper "
                        "declarations, use action=check_helper; the parent captures their exact "
                        "source automatically, so never put helper candidates in "
                        "checked_replacements.",
                    ]
                )
            if str(spec.inputs.get("route_mode", "") or "") == "evidence_synthesis":
                context_lines.extend(
                    [
                        "Route policy: synthesize the preserved source finding first.",
                        "Do not call broad web/library search tools until you have stated and "
                        "checked one concrete candidate derived from that evidence.",
                    ]
                )
            checked_helpers: list[dict[str, Any]] = []
            checked_helper_identities: set[tuple[str, str, str]] = set()
            checked_helper_lock = threading.Lock()

            def capture_checked_helper(
                function_name: str, arguments: dict[str, Any], raw_result: str
            ) -> None:
                """Capture one exact, validated helper without trusting final model prose."""
                try:
                    artifact = _checked_helper_artifact(
                        function_name,
                        arguments,
                        raw_result,
                        expected_target_symbol=str(spec.inputs.get("target_symbol", "") or ""),
                        expected_active_file=str(spec.inputs.get("active_file", "") or ""),
                    )
                except Exception:
                    logger.debug("checked-helper capture failed", exc_info=True)
                    return
                if artifact is None:
                    return
                identity = (
                    _normalized_lean_symbol(artifact["anchor_target_symbol"]),
                    _normalized_dispatch_path(artifact["active_file"]),
                    str(artifact["declaration_sha256"]),
                )
                with checked_helper_lock:
                    if identity in checked_helper_identities:
                        return
                    checked_helper_identities.add(identity)
                    checked_helpers.append(artifact)
                    if len(checked_helpers) > MAX_CHECKED_HELPERS:
                        removed = checked_helpers.pop(0)
                        checked_helper_identities.discard(
                            (
                                _normalized_lean_symbol(removed["anchor_target_symbol"]),
                                _normalized_dispatch_path(removed["active_file"]),
                                str(removed["declaration_sha256"]),
                            )
                        )
                    sink = self._incremental_evidence_sink
                    if sink is not None:
                        try:
                            sink(list(checked_helpers))
                        except Exception:
                            # The in-memory result remains usable on normal completion;
                            # evidence I/O failure must not abort the research backend.
                            logger.debug(
                                "dispatch incremental-evidence checkpoint failed",
                                exc_info=True,
                            )

            raw = delegate_task(
                goal=spec.objective,
                context="\n".join(context_lines),
                toolsets=delegated_toolsets,
                max_iterations=spec.budget.api_steps,
                parent_agent=self._parent_agent,
                isolate_budget=True,
                post_tool_result_callback=capture_checked_helper,
            )
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            results = list(payload.get("results") or [])
            first = dict(results[0]) if results else {}
            provider_retry_after = normalize_provider_retry_after(
                first.get("provider_retry_after") or payload.get("provider_retry_after")
            )
            raw_error = first.get("error") or payload.get("error")
            error_detail = _bounded_delegate_error(raw_error)
            status = sanitize_auxiliary_error(first.get("status", ""), limit=100) or "error"
            deliverable = _delegate_deliverable(
                str(first.get("summary", "") or ""),
                spec.deliverable,
                expected_target_symbol=str(spec.inputs.get("target_symbol", "") or ""),
            )
            boundary_deliverable: dict[str, Any] = {}
            if status == "interrupted" and (
                spec.archetype == "deep_search" or spec.deliverable == "findings_report"
            ):
                raw_handoff = first.get("interrupted_handoff")
                boundary_deliverable = _managed_boundary_deliverable(
                    spec,
                    raw_handoff if isinstance(raw_handoff, Mapping) else None,
                )
            selected_deliverable = boundary_deliverable or deliverable
            if normalized_route_context is not None:
                selected_deliverable = research_route_context.attach_parent_route_context(
                    selected_deliverable,
                    normalized_route_context,
                )
            with checked_helper_lock:
                captured_helpers = list(checked_helpers)
            selected_deliverable = _attach_checked_helpers(
                selected_deliverable,
                captured_helpers,
            )
            selected_deliverable = _cap_deliverable_preserving_exact_code(
                _bound_deliverable_preserving_exact_code(selected_deliverable)
            )
            incomplete_decomposition = (
                spec.archetype == "decomposition"
                and selected_deliverable.get("status") == "incomplete_unverified"
            )
            result = {
                "status": (
                    "done"
                    if (status in {"ok", "success", "completed"} or boundary_deliverable)
                    and not incomplete_decomposition
                    else status
                ),
                "deliverable": selected_deliverable,
                "artifact_paths": [],
                "plan_delta": [],
                "api_calls": first.get("api_calls", 0),
            }
            if incomplete_decomposition and result["status"] in {
                "ok",
                "success",
                "completed",
            }:
                result["status"] = "failed"
                result["error"] = "decomposition report failed the source-backed contract"
            if error_detail and result["status"] != "done":
                result["error"] = error_detail
            if provider_retry_after:
                result.update(
                    {
                        "provider_retry_after": provider_retry_after,
                        "provider_globally_unavailable": True,
                        "provider_retries_exhausted": True,
                    }
                )
            return result
        finally:
            for path in acquired:
                try:
                    release_file_lock(path, owner_id=owner_id)
                except Exception:
                    logger.debug("dispatch lock release failed", exc_info=True)

    def _run_spawn_job(self, spec: JobSpec) -> dict[str, Any]:
        """Run a prover job as a nested file-scoped ``/prove`` workflow.

        prover_jobs owns the whole contract — hygienic child env, stub-file
        lock, synchronous wall-clock wait with kill escalation, and the
        parent-side kernel gate over the stub declarations.
        """
        from leanflow_cli.workflows import prover_jobs  # lazy: pulls workflow.py

        return prover_jobs.launch_stub_prove_job(spec)
