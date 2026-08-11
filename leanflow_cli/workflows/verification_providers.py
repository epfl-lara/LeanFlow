"""Verification provider dispatch for LeanFlow formalization workflows."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agent.providers.isolated_auxiliary import (
    IsolatedAuxiliaryError,
    IsolatedAuxiliaryTimeout,
    IsolatedAuxiliaryUnavailable,
    resolve_auxiliary_call_identity,
    run_isolated_auxiliary_text,
    sanitize_auxiliary_error,
)
from leanflow_cli.cli.expert_help import (
    is_command_expert_provider,
    normalize_expert_provider,
    run_command_expert_help,
)
from leanflow_cli.workflows.workflow_state import append_workflow_activity
from tools.utilities.interrupt import raise_if_interrupted

logger = logging.getLogger(__name__)

BLUEPRINT_VERIFICATION_TASK = "blueprint_verification"
AUTOFORMALIZER_VERIFICATION_TASK = "autoformalizer_verification"
VERIFICATION_TASKS = {
    BLUEPRINT_VERIFICATION_TASK,
    AUTOFORMALIZER_VERIFICATION_TASK,
}
ADVISORY_VERIFICATION_TIMEOUT_ENV = "LEANFLOW_ADVISORY_VERIFICATION_TIMEOUT_S"
ADVISORY_VERIFICATION_TIMEOUT_DEFAULT_S = 180
ADVISORY_VERIFICATION_TIMEOUT_MAX_S = 300

LOCAL_VERIFIER_ALIASES = {
    "deterministic",
    "deterministic-local",
    "lean",
    "lean-kernel",
    "local",
    "local-verifier",
}


@dataclass(frozen=True)
class VerificationReviewResult:
    task: str
    provider: str
    mode: str
    response: str
    status: str
    command: list[str]
    exit_status: int | None
    truncated: bool
    response_chars: int
    max_response_chars: int
    timed_out: bool = False
    model: str = ""
    error: str = ""


def normalize_verification_provider(value: str) -> str:
    normalized = normalize_expert_provider(str(value or ""))
    if not normalized:
        return "local"
    if normalized in LOCAL_VERIFIER_ALIASES:
        return "local"
    if normalized in {"model", "rpc"}:
        return "main"
    return normalized


def default_verification_provider(task: str) -> str:
    if str(task or "") == BLUEPRINT_VERIFICATION_TASK:
        return "main"
    return "local"


def resolve_verification_provider(task: str, explicit: str | None = None) -> str:
    from leanflow_cli.cli.expert_help import resolve_expert_provider

    task_name = str(task or "").strip()
    provider = normalize_verification_provider(
        resolve_expert_provider(task_name, explicit=explicit)
    )
    if provider == "auto":
        return default_verification_provider(task_name)
    return provider or default_verification_provider(task_name)


def is_local_verification_provider(provider: str) -> bool:
    return normalize_verification_provider(provider) == "local"


def is_command_verification_provider(provider: str) -> bool:
    return is_command_expert_provider(normalize_verification_provider(provider))


def advisory_verification_timeout_s() -> int:
    """Return the bounded deadline for non-authoritative verifier advice."""
    try:
        configured = int(str(os.getenv(ADVISORY_VERIFICATION_TIMEOUT_ENV, "") or "").strip())
    except (TypeError, ValueError):
        configured = ADVISORY_VERIFICATION_TIMEOUT_DEFAULT_S
    return max(5, min(configured, ADVISORY_VERIFICATION_TIMEOUT_MAX_S))


def _record_verification_activity(event_type: str, message: str, **details: Any) -> None:
    try:
        append_workflow_activity(event_type, message, **details)
    except Exception:
        logger.debug("Failed to append verification telemetry activity", exc_info=True)


def run_command_verification_review(
    *,
    provider: str,
    task: str,
    prompt: str,
    cwd: str = "",
    timeout_s: int = 1200,
) -> VerificationReviewResult:
    """Execute a verification review via a command-based expert provider and return its output and status. Normalizes the provider name, invokes run_command_expert_help with the given prompt and timeout, and constructs a VerificationReviewResult with command execution details including exit status and response truncation. Records telemetry before and after execution."""
    raise_if_interrupted("verification command review interrupted before launch")
    normalized = normalize_verification_provider(provider)
    review_id = uuid.uuid4().hex
    started_at = time.monotonic()
    _record_verification_activity(
        "verification-review-request",
        "Verification command review started",
        review_id=review_id,
        task=task,
        provider=normalized,
        mode="command",
        cwd=cwd,
        timeout_s=timeout_s,
        elapsed_s=0.0,
        prompt=prompt,
    )
    command_result = run_command_expert_help(
        provider=normalized,
        task=task,
        prompt=prompt,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    raise_if_interrupted("verification command review interrupted after provider return")
    status = (
        "timeout"
        if command_result.timed_out
        else ("ok" if command_result.exit_status == 0 else "error")
    )
    result = VerificationReviewResult(
        task=task,
        provider=command_result.provider,
        mode="command",
        response=command_result.response,
        status=status,
        command=list(command_result.command),
        exit_status=command_result.exit_status,
        truncated=command_result.truncated,
        response_chars=command_result.response_chars,
        max_response_chars=command_result.max_response_chars,
        timed_out=command_result.timed_out,
    )
    _record_verification_activity(
        "verification-review-result",
        "Verification command review finished",
        review_id=review_id,
        task=task,
        provider=result.provider,
        mode=result.mode,
        timeout_s=timeout_s,
        elapsed_s=max(0.0, time.monotonic() - started_at),
        status=result.status,
        command=result.command,
        exit_status=result.exit_status,
        response=result.response,
        truncated=result.truncated,
        response_chars=result.response_chars,
        max_response_chars=result.max_response_chars,
        timed_out=result.timed_out,
    )
    return result


def run_model_verification_review(
    *,
    provider: str,
    task: str,
    prompt: str,
    system_prompt: str = "",
    timeout_s: int = 1200,
    max_tokens: int = 12000,
) -> VerificationReviewResult:
    """Execute a verification review via an LLM call, building optional system/user message pair and capturing model response, timeout behavior, and error states. Handles RuntimeError (provider unavailable) and generic exceptions distinctly, returning a VerificationReviewResult with the model's content or appropriate error message. Records telemetry before and after execution."""
    raise_if_interrupted("verification model review interrupted before launch")
    normalized = normalize_verification_provider(provider)
    effective_provider = None if normalized == "auto" else normalized
    review_id = uuid.uuid4().hex
    started_at = time.monotonic()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    _record_verification_activity(
        "verification-review-request",
        "Verification model review started",
        review_id=review_id,
        task=task,
        provider=normalized,
        mode="model",
        prompt=prompt,
        timeout_s=timeout_s,
        elapsed_s=0.0,
    )
    timed_out = False
    resolved_provider = normalized
    try:
        identity = resolve_auxiliary_call_identity(
            task=task,
            provider=effective_provider,
        )
    except Exception:
        identity = None
    heartbeat_provider = str(getattr(identity, "provider", "") or "").strip() or normalized
    heartbeat_model = str(getattr(identity, "model", "") or "").strip()

    def heartbeat(elapsed_s: float, deadline_s: float) -> None:
        message = (
            f"Verification review still waiting on {heartbeat_provider}"
            f"{f'/{heartbeat_model}' if heartbeat_model else ''} "
            f"({elapsed_s:.0f}s elapsed, {deadline_s:.0f}s deadline)"
        )
        print(f"   ⏳ {message}", flush=True)
        _record_verification_activity(
            "verification-review-heartbeat",
            message,
            review_id=review_id,
            task=task,
            provider=heartbeat_provider,
            model=heartbeat_model,
            mode="model",
            timeout_s=deadline_s,
            elapsed_s=elapsed_s,
        )

    try:
        response = run_isolated_auxiliary_text(
            task=task,
            provider=effective_provider,
            messages=messages,
            temperature=0.1,
            max_tokens=max_tokens,
            timeout=max(1, int(timeout_s or 0)),
            progress_callback=heartbeat,
        )
        raise_if_interrupted("verification model review interrupted after provider return")
        content = response.content.strip()
        model = response.model
        status = "ok" if content else "no_answer"
        error = "" if content else "the configured verifier returned no content"
    except IsolatedAuxiliaryTimeout as exc:
        content = ""
        model = sanitize_auxiliary_error(getattr(exc, "model", ""), limit=200)
        resolved_provider = (
            sanitize_auxiliary_error(getattr(exc, "provider", ""), limit=200) or normalized
        )
        status = "timeout"
        error = sanitize_auxiliary_error(exc)
        timed_out = True
    except IsolatedAuxiliaryUnavailable as exc:
        content = ""
        model = sanitize_auxiliary_error(getattr(exc, "model", ""), limit=200)
        resolved_provider = (
            sanitize_auxiliary_error(getattr(exc, "provider", ""), limit=200) or normalized
        )
        status = "unavailable"
        error = sanitize_auxiliary_error(exc)
    except IsolatedAuxiliaryError as exc:
        content = ""
        model = sanitize_auxiliary_error(getattr(exc, "model", ""), limit=200)
        resolved_provider = (
            sanitize_auxiliary_error(getattr(exc, "provider", ""), limit=200) or normalized
        )
        status = "error"
        error = sanitize_auxiliary_error(exc)
    except RuntimeError as exc:
        content = ""
        model = ""
        status = "unavailable"
        error = sanitize_auxiliary_error(exc)
    except Exception as exc:
        content = ""
        model = ""
        status = "error"
        error = sanitize_auxiliary_error(f"{type(exc).__name__}: {exc}")

    result = VerificationReviewResult(
        task=task,
        provider=resolved_provider,
        mode="model",
        response=content,
        status=status,
        command=[],
        exit_status=None,
        truncated=False,
        response_chars=len(content),
        max_response_chars=max_tokens,
        timed_out=timed_out,
        model=model,
        error=error,
    )
    _record_verification_activity(
        "verification-review-result",
        "Verification model review finished",
        review_id=review_id,
        task=task,
        provider=result.provider,
        requested_provider=normalized,
        mode=result.mode,
        timeout_s=timeout_s,
        elapsed_s=max(0.0, time.monotonic() - started_at),
        status=result.status,
        response=result.response,
        response_chars=result.response_chars,
        max_response_chars=result.max_response_chars,
        model=result.model,
        error=result.error,
        timed_out=result.timed_out,
    )
    return result
