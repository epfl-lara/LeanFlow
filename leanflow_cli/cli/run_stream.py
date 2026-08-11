"""Read exact run streams and aggregate run-scoped usage evidence.

This leaf owns hot/retained stream verification, journal attribution, provider
request coverage, and conversation-end accounting. It never consults mutable
project-global latest-run artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$")
#: Float slack when checking that a session's cumulative provider cost never
#: decreases. Real resets are orders of magnitude larger than binary rounding.
_CUMULATIVE_COST_EPSILON_USD = 1e-9
_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("verification-rejected", ("verification-failed", "verification-rejected", "rejected-edit")),
    ("proof-attempt-rejected", ("proof-attempt-rejected", "attempt-rejected")),
    ("timeout", ("timeout", "timed-out", "deadline")),
    ("axiom-violation", ("axiom-profile", "axiom-violation", "forbidden-axiom")),
    ("rollback", ("rollback", "restored", "revert")),
    ("owner-conflict", ("owner-conflict", "live-status-owner")),
    ("provider-error", ("provider-error", "provider-exhaustion", "provider-retry", "api-error")),
    ("stalled", ("stalled", "stuck", "no-progress", "breakpoint")),
    ("blocked", ("blocked", "obstruction")),
)
_TOKENS_RE = re.compile(r"Tokens this conversation:\s*input\s*([\d,]+)\s*·\s*output\s*([\d,]+)")
_ESTIMATED_COST_RE = re.compile(r"Total cost estimate:\s*\$([\d.]+)")
_REPORTED_COST_RE = re.compile(r"Total cost:\s*\$([\d.]+)\s*\(provider reported\)")
_API_CALLS_RE = re.compile(r"API calls:\s*([\d,]+)")


@dataclass(frozen=True)
class _RunStream:
    """Carry one selected run stream and its verification boundary."""

    run_id: str
    events: tuple[dict[str, Any], ...]
    source: str
    integrity_complete: bool
    archive_audit: dict[str, Any]


def validate_run_id(value: str, *, allow_empty: bool = False) -> str:
    """Return one exact safe run id or raise without normalizing it."""
    run_id = str(value or "").strip()
    if not run_id and allow_empty:
        return ""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run id must contain only letters, digits, '-' and '_' (maximum 192 characters)"
        )
    return run_id


def _parse_count(raw: Any) -> int | None:
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _classify_failure(event_type: str) -> str | None:
    lowered = event_type.lower()
    for bucket, needles in _FAILURE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return bucket
    return None


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield well-formed JSON objects from a project-scoped journal."""
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def _journal_rejections(state_root: Path, run_id: str) -> Counter[str]:
    """Count only journal records explicitly attributed to ``run_id``."""
    counts: Counter[str] = Counter()
    for record in _iter_jsonl(state_root / "journal.jsonl"):
        if str(record.get("run_id", "") or "") != run_id:
            continue
        event = str(record.get("event", "") or "")
        if "reject" in event or "blocked" in event:
            counts[event] += 1
        verdict = str(record.get("verdict", "") or "")
        if verdict and verdict.lower() not in {"pass", "ok"}:
            counts[f"verdict:{verdict}"] += 1
    return counts


def _events_sha256(events: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(
            json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _single_run_lifecycle(events: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether events describe one start followed by terminal exit(s)."""
    started = False
    seen_exit = False
    for event in events:
        event_type = str(event.get("type", "") or "")
        if event_type == "runner-start":
            if started or seen_exit:
                return False
            started = True
        elif event_type == "runner-exit":
            if not started:
                return False
            seen_exit = True
    return bool(
        started and seen_exit and events and str(events[-1].get("type", "") or "") == "runner-exit"
    )


def _read_hot_stream(path: Path, run_id: str) -> _RunStream:
    events: list[dict[str, Any]] = []
    integrity = True
    try:
        with path.open("rb") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            raw = handle.read()
    except OSError:
        return _RunStream(run_id, (), "hot", False, {})
    if raw and not raw.endswith(b"\n"):
        integrity = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            integrity = False
            continue
        if not isinstance(parsed, dict) or str(parsed.get("run_id", "") or "") != run_id:
            integrity = False
            continue
        events.append(parsed)
    return _RunStream(run_id, tuple(events), "hot", integrity, {})


def _archive_audit_payload(result: Any) -> dict[str, Any]:
    return {
        "complete": bool(result.complete),
        "catalog_status": str(result.catalog_status),
        "catalog_runs": int(result.catalog_runs),
        "verified_runs": int(result.verified_runs),
        "verified_events": int(result.verified_events),
        "matched_events": int(result.matched_events),
        "issue_counts": dict(result.issue_counts),
        "issue_samples": [
            {
                "code": item.code,
                "run_id": item.run_id,
                "path": item.path,
                "detail": item.detail,
            }
            for item in result.issue_samples
        ],
    }


def _read_retained_stream(state_root: Path, requested_run_id: str) -> _RunStream:
    from leanflow_cli.workflows.workflow_activity_retention import audit_retained_run_events

    by_run: dict[str, list[dict[str, Any]]] = {}

    def consume(event: dict[str, Any]) -> None:
        event_run_id = str(event.get("run_id", "") or "")
        if requested_run_id and event_run_id != requested_run_id:
            return
        if not _RUN_ID_RE.fullmatch(event_run_id):
            return
        by_run.setdefault(event_run_id, []).append(event)

    result = audit_retained_run_events(state_root, on_event=consume)
    audit = _archive_audit_payload(result)
    if not result.complete:
        return _RunStream(requested_run_id, (), "retained", False, audit)
    if requested_run_id:
        events = by_run.get(requested_run_id, [])
        return _RunStream(requested_run_id, tuple(events), "retained", bool(events), audit)
    if not by_run:
        return _RunStream("", (), "retained", True, audit)
    run_id, events = max(
        by_run.items(),
        key=lambda item: str((item[1][-1] if item[1] else {}).get("timestamp", "") or ""),
    )
    return _RunStream(run_id, tuple(events), "retained", True, audit)


def _select_run_stream(state_root: Path, requested_run_id: str) -> _RunStream:
    run_id = validate_run_id(requested_run_id, allow_empty=True)
    hot_root = state_root / "activity" / "runs"
    if run_id:
        hot = hot_root / f"{run_id}.jsonl"
        if hot.is_file():
            return _read_hot_stream(hot, run_id)
        return _read_retained_stream(state_root, run_id)
    candidates = sorted(hot_root.glob("*.jsonl")) if hot_root.is_dir() else []
    safe_candidates = [path for path in candidates if _RUN_ID_RE.fullmatch(path.stem)]
    if safe_candidates:
        latest = max(safe_candidates, key=lambda path: path.stat().st_mtime_ns)
        return _read_hot_stream(latest, latest.stem)
    return _read_retained_stream(state_root, "")


def _run_log_path(state_root: Path, run_id: str) -> Path:
    return state_root / "runs" / f"{validate_run_id(run_id)}.log"


def _usage_from_log(path: Path) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "api_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "cost_source": "unavailable",
    }
    try:
        tail = path.read_text(encoding="utf-8", errors="replace")[-50000:]
    except OSError:
        return usage
    token_matches = list(_TOKENS_RE.finditer(tail))
    tokens = token_matches[-1] if token_matches else None
    if tokens:
        usage["input_tokens"] = _parse_count(tokens.group(1))
        usage["output_tokens"] = _parse_count(tokens.group(2))
    call_matches = list(_API_CALLS_RE.finditer(tail))
    calls = call_matches[-1] if call_matches else None
    if calls:
        usage["api_calls"] = _parse_count(calls.group(1))
    reported_matches = list(_REPORTED_COST_RE.finditer(tail))
    estimated_matches = list(_ESTIMATED_COST_RE.finditer(tail))
    reported = reported_matches[-1] if reported_matches else None
    estimated = estimated_matches[-1] if estimated_matches else None
    if reported:
        usage["cost_usd"] = _parse_float(reported.group(1))
        usage["cost_source"] = "provider_reported"
    elif estimated:
        usage["cost_usd"] = _parse_float(estimated.group(1))
        usage["cost_source"] = "estimated"
    return usage


def _api_request_evidence(events: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
    """Count provider requests once when both compatibility events are present.

    The agent emits canonical ``api-request`` telemetry immediately before the
    provider call, while the native step callback also emits ``api-call`` for
    the same step. Retain both raw counts for auditability and use canonical
    requests whenever available. Counting canonical events (rather than unique
    iterations) preserves provider retries that legitimately share an iteration.
    """
    request_events = sum(1 for event in events if str(event.get("type", "") or "") == "api-request")
    call_events = sum(1 for event in events if str(event.get("type", "") or "") == "api-call")
    return request_events if request_events else call_events, request_events, call_events


def _aggregate_usage(events: Sequence[Mapping[str, Any]], log_path: Path) -> dict[str, Any]:
    """Aggregate each conversation turn once across foreground and child agents."""
    recorded_api_requests, api_request_events, api_call_events = _api_request_evidence(events)
    unmetered_api_attempts = sum(
        1 for event in events if str(event.get("type", "") or "") == "api-usage-unmetered"
    )
    unmetered_reason_counts: Counter[str] = Counter()
    for event in events:
        if str(event.get("type", "") or "") != "api-usage-unmetered":
            continue
        details = event.get("details")
        reason = str(details.get("reason", "") or "") if isinstance(details, Mapping) else ""
        unmetered_reason_counts[reason or "unspecified"] += 1
    command_expert_attempts = unmetered_reason_counts.get("command-expert-provider-attempt", 0)
    descendant_dispatch_events = sum(
        1 for event in events if str(event.get("type", "") or "") == "dispatch-job"
    )
    descendant_usage_complete = descendant_dispatch_events == 0
    groups: dict[str, dict[str, Any]] = {}
    api_calls = 0
    input_tokens = 0
    output_tokens = 0
    usage_events = 0
    api_calls_complete = True
    tokens_complete = True
    models: set[str] = set()
    providers: set[str] = set()
    for event in events:
        if str(event.get("type", "") or "") != "conversation-end":
            continue
        usage_events += 1
        details = event.get("details")
        agent_id = str(
            (details.get("agent_session_id", "") if isinstance(details, Mapping) else "")
            or event.get("agent_id", "")
            or f"event-{event.get('event_id', usage_events)}"
        )
        group = groups.setdefault(
            agent_id,
            {
                "cost_entries": [],
            },
        )
        if not isinstance(details, Mapping):
            api_calls_complete = False
            tokens_complete = False
            group["cost_entries"].append(("missing", None))
            continue
        model = str(details.get("model", "") or "")
        provider = str(details.get("provider", "") or "")
        if model:
            models.add(model)
        if provider:
            providers.add(provider)
        calls = _parse_count(details.get("api_calls"))
        if calls is None:
            api_calls_complete = False
        else:
            api_calls += calls
        raw_usage = details.get("usage")
        if not isinstance(raw_usage, Mapping):
            tokens_complete = False
            group["cost_entries"].append(("missing", None))
            continue
        turn = raw_usage.get("turn")
        if not isinstance(turn, Mapping):
            tokens_complete = False
            group["cost_entries"].append(("missing", None))
            continue
        prompt = _parse_count(turn.get("prompt_tokens"))
        completion = _parse_count(turn.get("completion_tokens"))
        if prompt is None or completion is None:
            tokens_complete = False
        else:
            input_tokens += prompt
            output_tokens += completion
        cost = raw_usage.get("cost")
        if not isinstance(cost, Mapping):
            group["cost_entries"].append(("missing", None))
            continue
        reported = _parse_float(cost.get("provider_reported_total_usd"))
        if reported is None and str(cost.get("source", "") or "") == "provider_reported":
            reported = _parse_float(cost.get("total_usd"))
        estimated_turn = _parse_float(cost.get("estimated_turn_usd"))
        if reported is not None:
            group["cost_entries"].append(("provider_reported", reported))
        elif estimated_turn is not None:
            group["cost_entries"].append(("estimated", estimated_turn))
        else:
            group["cost_entries"].append(("missing", None))

    if not usage_events:
        fallback = _usage_from_log(log_path)
        return {
            **fallback,
            # A text summary cannot prove that every foreground/child session
            # is represented, even when each displayed number parsed cleanly.
            "api_calls_complete": False,
            "tokens_complete": False,
            "cost_complete": False,
            "complete": False,
            "aggregation_scope": "last-run-log-summary-only",
            "source": (
                "run-log"
                if any(
                    fallback[key] is not None
                    for key in ("api_calls", "input_tokens", "output_tokens", "cost_usd")
                )
                else "unavailable"
            ),
            "conversation_end_events": 0,
            "recorded_api_requests": recorded_api_requests,
            "recorded_api_request_events": api_request_events,
            "recorded_api_call_events": api_call_events,
            "unmetered_api_attempts": unmetered_api_attempts,
            "unmetered_reason_counts": dict(unmetered_reason_counts.most_common()),
            "command_expert_attempts": command_expert_attempts,
            "descendant_dispatch_events": descendant_dispatch_events,
            "descendant_usage_complete": descendant_usage_complete,
            "api_request_coverage_complete": False,
            "agent_sessions": 0,
            "models": [],
            "providers": [],
        }

    cost_total = 0.0
    cost_sources: set[str] = set()
    cost_complete = True
    for group in groups.values():
        group_total = 0.0
        group_complete = True
        group_sources: set[str] = set()
        reported_high_water: float | None = None
        reported_regressed = False
        for kind, raw_value in list(group["cost_entries"]):
            if kind == "provider_reported":
                # Provider totals are cumulative per agent session. A later
                # provider total therefore replaces all earlier evidence and
                # repairs an earlier missing segment. Estimates after it remain
                # independently additive until another cumulative total arrives.
                reported = float(raw_value)
                if (
                    reported_high_water is not None
                    and reported < reported_high_water - _CUMULATIVE_COST_EPSILON_USD
                ):
                    # Replacing with the latest total is only sound while that
                    # total never decreases. A provider that resets it mid
                    # session would make this session silently drop the earlier
                    # spend, so its cost can no longer claim completeness. The
                    # flag is sticky: a later total must not repair it.
                    reported_regressed = True
                reported_high_water = (
                    reported if reported_high_water is None else max(reported_high_water, reported)
                )
                group_total = reported
                group_complete = True
                group_sources = {"provider_reported"}
            elif kind == "estimated":
                group_total += float(raw_value)
                group_sources.add("estimated")
            else:
                group_complete = False
        if group_complete and group_sources and not reported_regressed:
            cost_total += group_total
            cost_sources.update(group_sources)
        else:
            cost_complete = False
    api_request_coverage_complete = bool(
        not unmetered_api_attempts
        and descendant_usage_complete
        and api_calls == recorded_api_requests
    )
    if not api_request_coverage_complete:
        # A provider path returned without a conversation-end envelope. None
        # of the aggregate API/token/cost totals can claim complete coverage.
        api_calls_complete = False
        tokens_complete = False
        cost_complete = False
    if unmetered_api_attempts:
        api_calls_complete = False
        tokens_complete = False
        cost_complete = False
    input_value: int | None = input_tokens if tokens_complete else None
    output_value: int | None = output_tokens if tokens_complete else None
    api_value: int | None = api_calls if api_calls_complete else None
    return {
        "api_calls": api_value,
        "input_tokens": input_value,
        "output_tokens": output_value,
        "cost_usd": cost_total if cost_complete else None,
        "cost_source": (
            "mixed" if len(cost_sources) > 1 else next(iter(cost_sources), "unavailable")
        ),
        "api_calls_complete": api_calls_complete,
        "tokens_complete": tokens_complete,
        "cost_complete": cost_complete,
        "complete": api_calls_complete and tokens_complete and cost_complete,
        "source": "conversation-end-events",
        "aggregation_scope": "all-recorded-conversation-end-events",
        "conversation_end_events": usage_events,
        "recorded_api_requests": recorded_api_requests,
        "recorded_api_request_events": api_request_events,
        "recorded_api_call_events": api_call_events,
        "unmetered_api_attempts": unmetered_api_attempts,
        "unmetered_reason_counts": dict(unmetered_reason_counts.most_common()),
        "command_expert_attempts": command_expert_attempts,
        "descendant_dispatch_events": descendant_dispatch_events,
        "descendant_usage_complete": descendant_usage_complete,
        "api_request_coverage_complete": api_request_coverage_complete,
        "agent_sessions": len(groups),
        "models": sorted(models),
        "providers": sorted(providers),
    }


def _event_run_metadata(events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Return stable run metadata from the first event that records each field."""
    fields = {"workflow_kind": "", "workflow_command": "", "active_skill": ""}
    for event in events:
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        for field in fields:
            if not fields[field]:
                fields[field] = str(details.get(field, "") or "")
    return fields
