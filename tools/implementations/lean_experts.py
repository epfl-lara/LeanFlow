#!/usr/bin/env python3
"""Auxiliary LLM-advisor Lean tools for LeanFlow.

This module holds the LLM-backed advisor tools that were split out of
``tools/lean_tool.py``: ``lean_reasoning_help_tool`` (proof-strategy advice) and
``lean_decompose_helpers_tool`` (structured helper-lemma decomposition), together
with the prompt-building, expert-provider dispatch wrappers, response parsing and
helper-skeleton validation helpers they use.

``tools.implementations.lean_tool`` re-exports the two tool functions (and the timeout constants)
so callers and the tool registry keep resolving them as ``lean_tool.<name>``. This
module must NOT import ``tools.implementations.lean_tool`` (it would create an import cycle); it
reaches its collaborators directly via ``agent.auxiliary_client`` /
``leanflow_cli.cli.expert_help`` / ``leanflow_cli.lean.lean_incremental``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.providers.auxiliary_client import call_llm
from core.runtime_modes import scratch_only_dispatch_worker_enabled
from leanflow_cli.cli.expert_help import (
    is_command_expert_provider,
    record_expert_help_activity,
    resolve_expert_provider,
    run_command_expert_help,
)
from leanflow_cli.lean.lean_decomposition_shape import inspect_helper_skeleton
from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check
from leanflow_cli.lean.lean_helper_ephemeral import build_integrated_helper_source
from leanflow_cli.lean.lean_incremental import lean_incremental_check
from leanflow_cli.lean.lean_parsing import _find_assignment_marker_for_statement
from tools.utilities import (
    advisor_source_context,
    decomposer_admission,
    decomposer_prompt,
    decomposer_source_guard,
    helper_skeleton_diagnostics,
)
from tools.utilities.advisor_persistence import (
    REASONING_ADVISOR_NEXT_STEP,
    guard_reasoning_advice,
)
from tools.utilities.repository_research_policy import (
    clean_room_task_labels,
    solution_research_disabled,
)

LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S = 600
LEAN_REASONING_HELP_MIN_TIMEOUT_S = 10
LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S = LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S
LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S = LEAN_REASONING_HELP_MIN_TIMEOUT_S
ADVISOR_MODEL_HEARTBEAT_S = 30.0
ADVISOR_MODEL_TIMEOUT_GRACE_S = 5.0


def _clean_room_advisor_instructions() -> str:
    """Return the explicit advisor boundary for a clean-room proof campaign."""
    if not solution_research_disabled():
        return ""
    labels = ", ".join(clean_room_task_labels()) or "the active task"
    return (
        " This request belongs to a clean-room proof campaign. Do not inspect the project "
        "filesystem, shell history, Git history, sibling task files, prior run artifacts, or "
        f"web results for solutions to {labels}. Reason only from the source context and "
        "diagnostics supplied in this request."
    )


def _command_advisor_allowed(provider: str) -> bool:
    """Return whether a configured command advisor may receive this request."""
    return is_command_expert_provider(provider) and not solution_research_disabled()


def _advisor_timeout_s(timeout_s: Any, *, minimum_s: int) -> int:
    """Return an explicit advisor timeout, flooring only invalid or tiny values."""
    try:
        parsed = int(timeout_s)
    except (TypeError, ValueError):
        return minimum_s
    return max(minimum_s, parsed)


def _remaining_request_timeout_s(deadline: float) -> int:
    """Return whole seconds remaining before an authoritative request deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining))


def _call_model_advisor_with_heartbeat(
    *,
    task: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout_s: int,
    provider: str,
    theorem_id: str,
    file_path: str,
    heartbeat_s: float = ADVISOR_MODEL_HEARTBEAT_S,
) -> Any:
    """Call one isolated model advisor while persisting bounded wait heartbeats."""
    finished = threading.Event()
    outcome: list[tuple[bool, Any]] = []

    def invoke() -> None:
        try:
            outcome.append(
                (
                    True,
                    call_llm(
                        task=task,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=timeout_s,
                        isolate=True,
                    ),
                )
            )
        except BaseException as exc:
            outcome.append((False, exc))
        finally:
            finished.set()

    started = time.monotonic()
    deadline = started + timeout_s + ADVISOR_MODEL_TIMEOUT_GRACE_S
    thread = threading.Thread(
        target=invoke,
        name=f"leanflow-{task}-advisor",
        daemon=True,
    )
    thread.start()
    interval_s = max(0.01, float(heartbeat_s))
    while not finished.wait(timeout=min(interval_s, max(0.01, deadline - time.monotonic()))):
        elapsed_s = round(max(0.0, time.monotonic() - started), 1)
        record_expert_help_activity(
            "expert-help-heartbeat",
            "Expert model request remains active",
            provider=provider,
            mode="model",
            task=task,
            theorem_id=theorem_id,
            file_path=file_path,
            elapsed_s=elapsed_s,
            timeout_s=timeout_s,
            partial_response_available=False,
        )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{provider} expert model exceeded its {timeout_s}s request deadline."
            )
    if not outcome:
        raise RuntimeError(f"{provider} expert model ended without a result.")
    succeeded, value = outcome[0]
    if succeeded:
        return value
    if isinstance(value, BaseException):
        raise value
    raise RuntimeError(str(value))


def _advisor_failure(
    status: str, message: str, *, theorem_id: str = "", file_path: str = ""
) -> str:
    return json.dumps(
        {
            "success": False,
            "status": status,
            "theorem_id": theorem_id,
            "file_path": file_path,
            "message": (
                "lean_reasoning_help is not working for this request: "
                f"{message} Continue with the main proof workflow using any independently "
                "established evidence from source, counterexample, diagnostic, or graph state. "
                "This advisor failure neither supports nor refutes any proof route or statement "
                "repair."
            ),
        },
        ensure_ascii=False,
    )


def _reasoning_source_payload(
    context: advisor_source_context.AdvisorSourceContext,
    *,
    caller_statement: str,
) -> dict[str, Any]:
    """Return source-grounding telemetry for one reasoning-advisor request."""
    source_statement = str(context.target_statement or "").strip()
    caller = str(caller_statement or "").strip()
    return {
        "status": context.status,
        "source_sha256": context.source_sha256,
        "referenced_names": list(context.referenced_names),
        "provisional_names": list(context.provisional_names),
        "caller_statement_overridden": bool(
            source_statement
            and caller
            and _statement_identity_key(source_statement) != _statement_identity_key(caller)
        ),
    }


def _reasoning_answer(
    *,
    theorem_id: str,
    file_path: str,
    advice: str,
    context: advisor_source_context.AdvisorSourceContext,
    source_payload: dict[str, Any],
    provider_payload: dict[str, Any],
) -> str:
    """Return guarded advisor advice or fail closed on a source redefinition."""
    conflicts = advisor_source_context.advisor_source_conflicts(advice, context)
    if conflicts:
        return json.dumps(
            {
                "success": False,
                "status": "source_conflict",
                "theorem_id": theorem_id,
                "file_path": file_path,
                **provider_payload,
                "source_context": source_payload,
                "source_conflicts": list(conflicts),
                "message": (
                    "The advisor treated authoritative in-file declaration(s) as hypothetical "
                    "or redefined them: "
                    + ", ".join(conflicts)
                    + ". Ignore this advisor response and continue from the exact source "
                    "declarations, current diagnostics, and kernel-checked evidence."
                ),
            },
            ensure_ascii=False,
        )
    guarded_advice = guard_reasoning_advice(advice)
    return json.dumps(
        {
            "success": True,
            "status": (
                "answered_with_persistence_guard" if guarded_advice.guard_applied else "answered"
            ),
            "theorem_id": theorem_id,
            "file_path": file_path,
            **provider_payload,
            "advice": guarded_advice.text,
            "persistence_guard_applied": guarded_advice.guard_applied,
            "rejected_terminal_fragments": guarded_advice.rejected_fragment_count,
            "source_context": source_payload,
            "next_step": REASONING_ADVISOR_NEXT_STEP,
        },
        ensure_ascii=False,
    )


def lean_reasoning_help_tool(
    theorem_id: str,
    file_path: str,
    *,
    theorem_statement: str = "",
    current_diagnostics: str = "",
    current_goals: str = "",
    current_attempt: str = "",
    recent_failed_attempts: str = "",
    question: str = "",
    cwd: str = "",
    timeout_s: int = LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S,
) -> str:
    """Ask the configured auxiliary theorem advisor for proof-strategy advice."""
    theorem_id = str(theorem_id or "").strip()
    file_path = str(file_path or "").strip()
    if not theorem_id:
        return _advisor_failure("invalid_request", "missing theorem_id.", file_path=file_path)
    if not file_path:
        return _advisor_failure("invalid_request", "missing file_path.", theorem_id=theorem_id)
    if scratch_only_dispatch_worker_enabled():
        return _advisor_failure(
            "disabled_in_scratch_dispatch",
            "nested LLM advisor calls are disabled inside scratch dispatch workers.",
            theorem_id=theorem_id,
            file_path=file_path,
        )
    request_timeout_s = _advisor_timeout_s(
        timeout_s,
        minimum_s=LEAN_REASONING_HELP_MIN_TIMEOUT_S,
    )

    try:
        max_tokens = max(1000, int(os.getenv("LEANFLOW_LEAN_REASONING_HELP_MAX_TOKENS", "64000")))
    except (TypeError, ValueError):
        max_tokens = 64000

    source_evidence = "\n\n".join(
        part
        for part in (
            theorem_statement,
            current_diagnostics,
            current_goals,
            current_attempt,
            recent_failed_attempts,
            question,
        )
        if part
    )
    source_context = advisor_source_context.load_advisor_source_context(
        theorem_id=theorem_id,
        file_path=file_path,
        cwd=cwd,
        evidence=source_evidence,
    )
    authoritative_statement = source_context.target_statement or theorem_statement
    source_block = source_context.render()
    source_payload = _reasoning_source_payload(
        source_context,
        caller_statement=theorem_statement,
    )
    system_prompt = (
        "You are an auxiliary Lean proof-strategy advisor for LeanFlow. "
        "Act as a world-class mathematical strategist, combining deep olympiad, "
        "analysis, algebra, and formal-verification taste with practical Lean and "
        "Mathlib expertise. "
        "Your role is advisory only: you do not edit files, you do not decide success, "
        "and your answer is not verification evidence. Give concrete proof ideas, "
        "search terms, likely lemmas, and tactic sketches for the assigned theorem only. "
        "Preserve every source-authored theorem/lemma/example statement exactly, including its "
        "name, binders, hypotheses, conclusion, attributes, namespace location, and "
        "surrounding API. A runtime-generated private helper is different: when independent "
        "counterexample, caller, or dependency evidence shows that its generated statement is "
        "false or omitted a required premise, recommend the smallest sound statement repair and "
        "name every caller/dependency update; the runtime provenance guard remains authoritative. "
        "Before proposing any helper statement, expand the supplied definitions and test boundary "
        "cases such as zero, one, empty, and constant inputs. If a required definition is absent, "
        "state the proposal conditionally instead of asserting it. If the statement appears wrong, "
        "underspecified, open in the "
        "mathematical literature, or too hard for the current route, report the evidence "
        "accurately. Such a blocker is route-change evidence, never a terminal verdict: "
        "never recommend reporting the unresolved theorem as mathematically blocked, "
        "stopping, giving up, or declining further attempts. End with `Continuation route:` "
        "followed by one concrete distinct proof route, helper decomposition, empirical or "
        "negation job, portfolio refresh, or fresh campaign epoch. Do not suggest deleting, "
        "weakening, renaming, moving, or splitting "
        "a source-authored declaration unless the user explicitly asked for a refactor. "
        "Do not suggest "
        "replacing the proof with sorry, admit, axiom, unsafe code, or a placeholder. "
        "You may suggest small helper lemmas or private supporting declarations when "
        "they preserve existing statements and directly help the assigned theorem. "
        "The authoritative in-file source context in the request outranks caller summaries and "
        "your prior knowledge. Never redefine or treat a supplied declaration as hypothetical. "
        "Quote and inspect its exact body before diagnosing a reduction, recursion, elaboration, "
        "or unification failure. Distinguish the source location where Lean reports an error from "
        "the operation that caused it; inspect the expected theorem type and intermediate goal "
        "before blaming an unfold or rewrite."
        f"{_clean_room_advisor_instructions()}"
    )
    user_prompt = "\n\n".join(
        part
        for part in [
            f"File: {file_path}",
            f"Theorem: {theorem_id}",
            f"Working directory: {cwd}" if cwd else "",
            (
                f"Authoritative in-file source context:\n{source_block}"
                if source_block
                else (
                    f"Theorem statement:\n{authoritative_statement}"
                    if authoritative_statement
                    else ""
                )
            ),
            f"Current diagnostics:\n{current_diagnostics}" if current_diagnostics else "",
            f"Current goals:\n{current_goals}" if current_goals else "",
            f"Current attempt:\n{current_attempt}" if current_attempt else "",
            f"Recent failed attempts:\n{recent_failed_attempts}" if recent_failed_attempts else "",
            (
                f"Question:\n{question}"
                if question
                else "Question:\nSuggest the next strongest proof strategy."
            ),
        ]
        if part
    )
    expert_provider = resolve_expert_provider("lean_reasoning")
    command_prompt = f"System instructions:\n{system_prompt}\n\nAdvisor request:\n{user_prompt}"

    if _command_advisor_allowed(expert_provider):
        try:
            command_result = run_command_expert_help(
                provider=expert_provider,
                task="lean_reasoning",
                prompt=command_prompt,
                cwd=cwd,
                timeout_s=request_timeout_s,
            )
        except RuntimeError as exc:
            return _advisor_failure(
                "unavailable", str(exc), theorem_id=theorem_id, file_path=file_path
            )
        except Exception as exc:
            return _advisor_failure(
                "error",
                f"{type(exc).__name__}: {exc}",
                theorem_id=theorem_id,
                file_path=file_path,
            )

        if command_result.timed_out:
            return _advisor_failure(
                "timeout",
                f"{command_result.provider} expert command timed out.",
                theorem_id=theorem_id,
                file_path=file_path,
            )
        if command_result.exit_status != 0:
            return _advisor_failure(
                "error",
                (
                    f"{command_result.provider} expert command exited with status "
                    f"{command_result.exit_status}: {command_result.stderr or '[no stderr]'}"
                ),
                theorem_id=theorem_id,
                file_path=file_path,
            )
        raw_advice = command_result.response.strip()
        if not raw_advice:
            return _advisor_failure(
                "no_answer",
                "the command expert advisor returned no content.",
                theorem_id=theorem_id,
                file_path=file_path,
            )
        return _reasoning_answer(
            theorem_id=theorem_id,
            file_path=file_path,
            advice=raw_advice,
            context=source_context,
            source_payload=source_payload,
            provider_payload={
                "provider": command_result.provider,
                "mode": "command",
                "command": command_result.command,
                "exit_status": command_result.exit_status,
                "truncated": command_result.truncated,
                "response_chars": command_result.response_chars,
                "max_response_chars": command_result.max_response_chars,
            },
        )

    try:
        record_expert_help_activity(
            "expert-help-request",
            "Expert help model request started",
            provider=expert_provider,
            mode="model",
            prompt=command_prompt,
            command=[],
            exit_status=None,
            theorem_id=theorem_id,
            file_path=file_path,
        )
        response = _call_model_advisor_with_heartbeat(
            task="lean_reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
            timeout_s=request_timeout_s,
            provider=expert_provider,
            theorem_id=theorem_id,
            file_path=file_path,
        )
    except TimeoutError as exc:
        return _advisor_failure("timeout", str(exc), theorem_id=theorem_id, file_path=file_path)
    except RuntimeError as exc:
        return _advisor_failure("unavailable", str(exc), theorem_id=theorem_id, file_path=file_path)
    except Exception as exc:
        return _advisor_failure(
            "error",
            f"{type(exc).__name__}: {exc}",
            theorem_id=theorem_id,
            file_path=file_path,
        )

    try:
        advice = str(response.choices[0].message.content or "").strip()
    except Exception:
        advice = ""
    if not advice:
        return _advisor_failure(
            "no_answer",
            "the auxiliary advisor returned no content.",
            theorem_id=theorem_id,
            file_path=file_path,
        )
    record_expert_help_activity(
        "expert-help-result",
        "Expert help model request finished",
        provider=expert_provider,
        mode="model",
        prompt=command_prompt,
        command=[],
        exit_status=None,
        response=advice,
        truncated=False,
        response_chars=len(advice),
        max_response_chars=len(advice),
        model=str(getattr(response, "model", "") or ""),
        theorem_id=theorem_id,
        file_path=file_path,
    )

    return _reasoning_answer(
        theorem_id=theorem_id,
        file_path=file_path,
        advice=advice,
        context=source_context,
        source_payload=source_payload,
        provider_payload={
            "provider": expert_provider,
            "mode": "model",
            "model": str(getattr(response, "model", "") or ""),
        },
    )


def _decompose_failure(
    status: str, message: str, *, theorem_id: str = "", file_path: str = ""
) -> str:
    return json.dumps(
        {
            "success": False,
            "status": status,
            "theorem_id": theorem_id,
            "file_path": file_path,
            "message": (
                "lean_decompose_helpers is not working for this request: "
                f"{message} Continue with the main proof workflow using any independently "
                "established evidence from source, counterexample, diagnostic, or graph state. "
                "This advisor failure neither supports nor refutes any proof route or statement "
                "repair."
            ),
        },
        ensure_ascii=False,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _target_sorry_skeleton(theorem_statement: str) -> str:
    """Return the target declaration signature closed by a temporary sorry body."""
    statement = str(theorem_statement or "").strip()
    if not statement:
        return ""
    assignment = _find_assignment_marker_for_statement(statement)
    if assignment >= 0:
        # Agents often pass the exact current declaration, including its proof.
        # Keeping that proof and appending another `sorry` produces the misleading
        # Lean error "No goals to be solved" for every otherwise-valid helper.
        signature = statement[:assignment].rstrip()
        return f"{signature} := by\n  sorry"
    if re.search(r":=\s*by\s*$", statement):
        return f"{statement}\n  sorry"
    if re.search(r":=\s*$", statement):
        return f"{statement} by\n  sorry"
    if re.search(r"\bby\s*$", statement):
        return f"{statement}\n  sorry"
    if ":=" in statement:
        return f"{statement}\n  sorry"
    return f"{statement} := by\n  sorry"


def _statement_identity_key(theorem_statement: str) -> str:
    """Return a whitespace-insensitive key for one temporary target skeleton."""
    return " ".join(_target_sorry_skeleton(theorem_statement).split())


def _diagnostic_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("messages", "items", "diagnostics"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            nested = value.get("items") or value.get("messages")
            if isinstance(nested, list):
                items.extend(item for item in nested if isinstance(item, dict))
    return items


def _payload_error_count(payload: dict[str, Any]) -> int:
    count = 0
    for key in ("errors", "error_count"):
        value = payload.get(key)
        if isinstance(value, int):
            count = max(count, value)
        if isinstance(value, str) and value.isdigit():
            count = max(count, int(value))
    diagnostic_count = sum(
        1 for item in _diagnostic_items(payload) if str(item.get("severity", "")).lower() == "error"
    )
    if payload.get("success") is False or payload.get("has_errors") is True:
        count = max(count, 1)
    if str(payload.get("error_code", "") or "").strip():
        count = max(count, 1)
    if str(payload.get("error", "") or "").strip():
        count = max(count, 1)
    return max(count, diagnostic_count)


def _validation_diagnostics(payload: dict[str, Any]) -> str:
    output = str(payload.get("output", "") or payload.get("error", "") or "").strip()
    if output:
        return output[:2000]
    messages: list[str] = []
    for item in _diagnostic_items(payload):
        message = str(item.get("message", "") or item.get("text", "") or "").strip()
        if message:
            messages.append(message)
    return "\n".join(messages)[:2000]


def _incremental_environment_failure(payload: dict[str, Any] | None) -> bool:
    """Return whether LeanProbe failed before checking the replacement."""
    if not payload:
        return False
    error_code = str(payload.get("error_code", "") or "").strip().lower()
    text = " ".join(
        str(payload.get(key, "") or "") for key in ("error", "output", "message", "hint")
    ).lower()
    return bool(
        error_code in {"header_failed", "prior_decl_failed"}
        or "failed to build env before target" in text
    )


def _canonical_skeleton_fallback(
    *,
    helper_source: str,
    file_path: str,
    theorem_id: str,
    cwd: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Check helper skeletons in an exact-project full-source harness.

    LeanProbe can occasionally segment a valid source file but fail while
    rebuilding its prefix environment. Insert the proposed helpers before the
    unchanged target in a system-temporary full-source copy and ask canonical
    ``lake env lean`` instead. A completed Lean elaboration failure is reported
    as a valid check result with errors, while infrastructure failures remain
    fail-closed.
    """
    root = Path(cwd or ".").expanduser().resolve()
    target = Path(file_path).expanduser()
    if not target.is_absolute():
        target = root / target
    try:
        source = target.resolve().read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_exact_ephemeral",
            "tool": "lake_env_lean",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": theorem_id,
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "error": str(exc)[:500],
            "error_code": "source_read_failed",
            "output": "",
            "messages": [],
        }
    integrated = build_integrated_helper_source(source, helper_source, theorem_id)
    if not integrated:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_exact_ephemeral",
            "tool": "lake_env_lean",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": theorem_id,
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "error": "could not build exact pre-target helper harness",
            "error_code": "source_segmentation_failed",
            "output": "",
            "messages": [],
        }
    checked = dict(
        lean_ephemeral_source_check(
            integrated,
            cwd=root,
            timeout_s=max(1, int(timeout_s or 1)),
        )
        or {}
    )
    infrastructure_failure = bool(
        checked.get("timed_out") is True
        or checked.get("retryable") is True
        or str(checked.get("failure_kind", "") or "").strip().lower()
        in {
            "infrastructure_timeout",
            "infrastructure_unavailable",
            "project_environment_unavailable",
            "resource_admission_retained",
        }
    )
    elaborated = bool(checked.get("ok") is True)
    output = str(checked.get("output", "") or "")
    return {
        **checked,
        # Match LeanProbe's two-level contract: the check ran successfully
        # even when Lean rejected a proposed skeleton.
        "success": not infrastructure_failure,
        "ok": elaborated,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_target",
        "file": str(target.resolve()),
        "target": theorem_id,
        "replacement_matches_target": True,
        "verification_scope": "target_candidate",
        "has_errors": not elaborated,
        "errors": 0 if elaborated else 1,
        "error": "" if elaborated else str(checked.get("error", "") or output)[:500],
        "error_code": (
            ""
            if elaborated
            else (str(checked.get("error_code", "") or "") if infrastructure_failure else "")
        ),
        "messages": list(checked.get("messages") or []),
        "canonical_fallback": True,
    }


def _validate_helper_skeletons(
    *,
    helpers: list[dict[str, Any]],
    theorem_statement: str,
    file_path: str,
    theorem_id: str,
    cwd: str,
    timeout_s: int,
    deadline: float | None = None,
    source_constraints: Sequence[decomposer_source_guard.SourceConstraint] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate helper templates without authorizing placeholder insertion.

    Build elaborating templates cumulatively so dependent proposals can be
    checked. A template containing ``sorry`` or ``admit`` may be ready to prove,
    but it is never ready to insert into the working file.
    """
    target_skeleton = _target_sorry_skeleton(theorem_statement)
    if not target_skeleton:
        for helper in helpers:
            helper["check_status"] = "skipped"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["check_diagnostics"] = (
                "missing theorem_statement; skeleton validation requires the unchanged target declaration closed with `by sorry`."
            )
        return helpers, {
            "status": "skipped",
            "reason": "missing_theorem_statement",
            "validated_count": 0,
            "ready_count": 0,
            "ready_to_prove_count": 0,
            "ready_to_insert_count": 0,
            "ready_for_managed_placement_count": 0,
            "ready_to_insert_requires_sorry_free": True,
            "instantiated_parent_rejected_count": 0,
            "source_conflict_count": 0,
            "shape_rejected_count": 0,
            "dependency_blocked_count": 0,
            "lean_check_count": 0,
            "validation_mode": "skipped",
        }

    validated_count = 0
    ready_to_prove_count = 0
    ready_to_insert_count = 0
    ready_for_managed_placement_count = 0
    placeholder_count = 0
    instantiated_parent_rejected_count = 0
    source_conflict_count = 0
    shape_rejected_count = 0
    dependency_blocked_count = 0
    lean_check_count = 0
    canonical_fallback_count = 0
    unavailable_helper_names: set[str] = set()
    prepared: list[tuple[int, dict[str, Any], str, bool, bool]] = []

    for index, helper in enumerate(helpers):
        skeleton = str(helper.get("lean_skeleton", "") or helper.get("skeleton", "") or "").strip()
        shape = inspect_helper_skeleton(
            skeleton,
            expected_name=str(helper.get("name", "") or "").strip(),
        )
        has_placeholder = shape.has_placeholder
        helper["has_placeholder"] = has_placeholder
        helper["exact_sorry_stub"] = shape.exact_sorry_stub
        helper["declared_name"] = shape.declared_name
        helper["validation_order"] = index + 1
        helper["ready_for_managed_placement"] = False
        if not shape.valid:
            helper_name = str(helper.get("name", "") or "").strip()
            if helper_name:
                unavailable_helper_names.add(helper_name)
            helper["check_status"] = "rejected_shape"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["check_diagnostics"] = shape.reason
            shape_rejected_count += 1
            continue
        admission = decomposer_admission.assess_helper_admission(
            theorem_statement,
            skeleton,
        )
        if not admission.accepted:
            helper_name = str(helper.get("name", "") or "").strip()
            if helper_name:
                unavailable_helper_names.add(helper_name)
            helper["check_status"] = (
                "rejected_instantiated_parent"
                if admission.reason_code == "closed_literal_parent_instantiation"
                else "rejected_admission"
            )
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["admission_rejection"] = admission.reason
            helper["admission_reason_code"] = admission.reason_code
            helper["admission_guard"] = admission.journal_fields()
            helper["instantiated_parameters"] = [
                {"name": name, "literal": literal}
                for name, literal in admission.instantiated_parameters
            ]
            helper["rejected_lean_skeleton_sha256"] = hashlib.sha256(
                skeleton.encode("utf-8")
            ).hexdigest()
            helper["rejected_lean_skeleton_chars"] = len(skeleton)
            helper["lean_skeleton"] = ""
            if "skeleton" in helper:
                helper["skeleton"] = ""
            helper["proof_hints"] = []
            if "hints" in helper:
                helper["hints"] = []
            helper["check_diagnostics"] = admission.reason
            instantiated_parent_rejected_count += 1
            continue
        conflict_reason = decomposer_source_guard.helper_source_conflict_reason(
            helper,
            theorem_id=theorem_id,
            constraints=source_constraints,
        )
        if conflict_reason:
            helper_name = str(helper.get("name", "") or "").strip()
            if helper_name:
                unavailable_helper_names.add(helper_name)
            helper["check_status"] = "rejected_source_conflict"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["source_conflict"] = conflict_reason
            helper["source_conflict_facts"] = [
                constraint.name for constraint in source_constraints[:3]
            ]
            helper["rejected_lean_skeleton_sha256"] = hashlib.sha256(
                skeleton.encode("utf-8")
            ).hexdigest()
            helper["rejected_lean_skeleton_chars"] = len(skeleton)
            helper["lean_skeleton"] = ""
            if "skeleton" in helper:
                helper["skeleton"] = ""
            helper["proof_hints"] = []
            if "hints" in helper:
                helper["hints"] = []
            helper["check_diagnostics"] = conflict_reason
            source_conflict_count += 1

    for index, helper in enumerate(helpers):
        if str(helper.get("check_status", "") or "") in {
            "rejected_shape",
            "rejected_instantiated_parent",
            "rejected_admission",
            "rejected_source_conflict",
        }:
            continue
        skeleton = str(helper.get("lean_skeleton", "") or helper.get("skeleton", "") or "").strip()
        has_placeholder = bool(helper.get("has_placeholder"))
        if not skeleton:
            helper["check_status"] = "skipped"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["check_diagnostics"] = "missing lean_skeleton"
            continue
        dependencies = helper.get("dependencies", [])
        dependency_names = (
            {str(item).strip() for item in dependencies if str(item).strip()}
            if isinstance(dependencies, list)
            else {str(dependencies).strip()}
        )
        blocked_dependencies = sorted(dependency_names & unavailable_helper_names)
        if blocked_dependencies:
            helper_name = str(helper.get("name", "") or "").strip()
            if helper_name:
                unavailable_helper_names.add(helper_name)
            helper["check_status"] = "blocked_by_source_conflict"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["check_diagnostics"] = (
                "Depends on rejected or transitively unavailable helper(s): "
                + ", ".join(blocked_dependencies)
            )
            dependency_blocked_count += 1
            continue
        prepared.append(
            (index, helper, skeleton, has_placeholder, bool(helper.get("exact_sorry_stub")))
        )

    def run_check(
        replacement: str,
        helper_source: str,
    ) -> tuple[dict[str, Any] | None, str]:
        nonlocal lean_check_count
        nonlocal canonical_fallback_count
        check_timeout_s = max(1, int(timeout_s or 1))
        if deadline is not None:
            remaining_s = _remaining_request_timeout_s(deadline)
            if remaining_s <= 0:
                return None, (
                    "decomposition request deadline exhausted before Lean skeleton validation"
                )
            check_timeout_s = min(check_timeout_s, remaining_s)
        lean_check_count += 1
        try:
            check = lean_incremental_check(
                action="check_target",
                file_path=file_path,
                theorem_id=theorem_id,
                cwd=cwd,
                replacement=replacement,
                include_tactics=False,
                timeout_s=check_timeout_s,
                timeout_ceiling_s=(check_timeout_s if deadline is not None else None),
                allow_placeholders_for_elaboration=True,
            )
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        check = dict(check)
        if not _incremental_environment_failure(check):
            return check, ""
        if deadline is not None:
            remaining_s = _remaining_request_timeout_s(deadline)
            if remaining_s <= 0:
                return check, ""
            check_timeout_s = min(check_timeout_s, remaining_s)
        canonical_fallback_count += 1
        fallback = _canonical_skeleton_fallback(
            helper_source=helper_source,
            file_path=file_path,
            theorem_id=theorem_id,
            cwd=cwd,
            timeout_s=check_timeout_s,
        )
        fallback["incremental_fallback_reason"] = _validation_diagnostics(check)
        return fallback, ""

    def check_identity_matches(check: dict[str, Any] | None) -> bool:
        if check is None:
            return False
        tool = str(check.get("tool", "") or "").strip()
        backend = str(check.get("backend", "") or "").strip()
        if tool != "lean_probe" and not (
            tool == "lake_env_lean" and backend == "lean_exact_ephemeral"
        ):
            return False
        if str(check.get("action", "") or "").strip() != "check_target":
            return False
        if check.get("replacement_matches_target") is not True:
            return False
        if str(check.get("verification_scope", "") or "").strip() != "target_candidate":
            return False
        reported_target = str(check.get("target", "") or "").strip().removeprefix("_root_.")
        expected_target = theorem_id.removeprefix("_root_.")
        if reported_target != expected_target:
            return False
        reported_file = str(check.get("file", "") or "").strip()
        if not reported_file:
            return False
        expected_path = Path(file_path).expanduser()
        if not expected_path.is_absolute():
            expected_path = (Path(cwd) if cwd else Path.cwd()) / expected_path
        try:
            if Path(reported_file).expanduser().resolve() != expected_path.resolve():
                return False
        except OSError:
            return False
        return True

    def check_succeeded(check: dict[str, Any] | None) -> bool:
        return bool(
            check_identity_matches(check)
            and check is not None
            and check.get("success") is True
            and _payload_error_count(check) == 0
        )

    def mark_success(
        helper: dict[str, Any],
        has_placeholder: bool,
        managed_stub: bool,
        check: dict[str, Any],
    ) -> None:
        nonlocal ready_to_prove_count
        nonlocal ready_for_managed_placement_count
        nonlocal placeholder_count
        helper["check_status"] = "ok"
        helper["ready_to_prove"] = True
        helper["ready_for_managed_placement"] = managed_stub
        # This target-candidate check proves elaboration only. Manual insertion
        # still requires a sorry-free helper check and an allowed-axiom profile.
        helper["ready_to_insert"] = False
        helper["ready_to_insert_reason"] = (
            "requires lean_incremental_check(action=check_helper, "
            "include_axiom_profile=true) to return a sorry-free helper and a complete "
            "allowed-axiom profile"
        )
        helper["check_diagnostics"] = _validation_diagnostics(check)
        ready_to_prove_count += 1
        if managed_stub:
            ready_for_managed_placement_count += 1
        if has_placeholder:
            placeholder_count += 1

    validation_mode = "sequential"
    batch_diagnostics = ""
    unprovided_identifiers: tuple[str, ...] = ()
    if len(prepared) > 1:
        batch_replacement = "\n\n".join(
            [*(skeleton for _, _, skeleton, _, _ in prepared), target_skeleton]
        )
        batch_check, batch_error = run_check(
            batch_replacement,
            "\n\n".join(skeleton for _, _, skeleton, _, _ in prepared),
        )
        if check_succeeded(batch_check):
            assert batch_check is not None
            validated_count = len(prepared)
            for _, helper, _, has_placeholder, managed_stub in prepared:
                mark_success(helper, has_placeholder, managed_stub, batch_check)
            validation_mode = "batch"
        else:
            batch_diagnostics = batch_error or _validation_diagnostics(batch_check or {})
            if (
                batch_check is None
                or not check_identity_matches(batch_check)
                or (batch_check.get("success") is not True)
            ):
                validation_mode = "batch_contract_failure"
                if batch_check is None:
                    reason = batch_error or "Lean batch check did not return a payload."
                elif not check_identity_matches(batch_check):
                    reason = "Lean batch check did not attest the exact target identity and scope."
                else:
                    reason = batch_diagnostics or "Lean batch check did not complete successfully."
                for _, helper, _, _, _ in prepared:
                    helper["check_status"] = "failed"
                    helper["ready_to_prove"] = False
                    helper["ready_to_insert"] = False
                    helper["ready_for_managed_placement"] = False
                    helper["check_diagnostics"] = reason
            else:
                assert batch_check is not None
                unprovided_identifiers = helper_skeleton_diagnostics.batch_unprovided_identifiers(
                    batch_check,
                    [
                        (str(helper.get("declared_name", "") or ""), skeleton)
                        for _, helper, skeleton, _, _ in prepared
                    ],
                )
                if unprovided_identifiers:
                    validation_mode = "batch_unprovided_identifiers"
                    validated_count = len(prepared)
                    reason = (
                        "Batch validation proved every helper directly references "
                        "identifier(s) absent from the proposal set: "
                        + ", ".join(unprovided_identifiers)
                        + "."
                    )
                    if batch_diagnostics:
                        reason = f"{reason}\n{batch_diagnostics}"
                    for _, helper, _, _, _ in prepared:
                        helper["check_status"] = "failed"
                        helper["ready_to_prove"] = False
                        helper["ready_to_insert"] = False
                        helper["ready_for_managed_placement"] = False
                        helper["check_diagnostics"] = reason
                else:
                    validation_mode = "sequential_fallback"

    if validation_mode not in {
        "batch",
        "batch_contract_failure",
        "batch_unprovided_identifiers",
    }:
        accepted_prefix: list[str] = []
        for _, helper, skeleton, has_placeholder, managed_stub in prepared:
            replacement = "\n\n".join([*accepted_prefix, skeleton, target_skeleton])
            check, check_error = run_check(
                replacement,
                "\n\n".join([*accepted_prefix, skeleton]),
            )
            if check is not None:
                validated_count += 1
            if check_succeeded(check):
                assert check is not None
                mark_success(helper, has_placeholder, managed_stub, check)
                accepted_prefix.append(skeleton)
                continue
            helper["check_status"] = "failed"
            helper["ready_to_prove"] = False
            helper["ready_to_insert"] = False
            helper["ready_for_managed_placement"] = False
            helper["check_diagnostics"] = (
                check_error or _validation_diagnostics(check or {}) or "Lean skeleton check failed."
            )

    if not prepared:
        validation_mode = "no_eligible_helpers"

    return helpers, {
        "status": "checked",
        "validated_count": validated_count,
        # Keep ready_count as a safe compatibility alias: it authorizes insertion.
        "ready_count": ready_to_insert_count,
        "ready_to_prove_count": ready_to_prove_count,
        "ready_to_insert_count": ready_to_insert_count,
        "ready_for_managed_placement_count": ready_for_managed_placement_count,
        "placeholder_count": placeholder_count,
        "allows_sorry_warnings_for_ready_to_prove": True,
        "ready_to_insert_requires_sorry_free": True,
        "instantiated_parent_rejected_count": instantiated_parent_rejected_count,
        "source_conflict_count": source_conflict_count,
        "shape_rejected_count": shape_rejected_count,
        "dependency_blocked_count": dependency_blocked_count,
        "lean_check_count": lean_check_count,
        "canonical_fallback_count": canonical_fallback_count,
        "validation_mode": validation_mode,
        "deadline_exhausted": bool(
            deadline is not None and _remaining_request_timeout_s(deadline) <= 0
        ),
        **(
            {
                "unprovided_identifier_count": len(unprovided_identifiers),
                "unprovided_identifiers": list(unprovided_identifiers),
            }
            if unprovided_identifiers
            else {}
        ),
        **({"batch_check_diagnostics": batch_diagnostics} if batch_diagnostics else {}),
    }


def _normalize_decomposition_payload(
    payload: dict[str, Any],
    *,
    max_helper_count: int,
) -> dict[str, Any]:
    raw_helpers = payload.get("helpers")
    if raw_helpers is None:
        raw_helpers = payload.get("helper_lemmas")
    helpers: list[dict[str, Any]] = []
    if isinstance(raw_helpers, list):
        for item in raw_helpers[:max_helper_count]:
            if not isinstance(item, dict):
                continue
            proof_hints = item.get("proof_hints", item.get("hints", []))
            dependencies = item.get("dependencies", [])
            helpers.append(
                {
                    "name": str(item.get("name", "") or "").strip(),
                    "purpose": str(item.get("purpose", "") or item.get("why", "") or "").strip(),
                    "lean_skeleton": str(
                        item.get("lean_skeleton", "") or item.get("skeleton", "") or ""
                    ).strip(),
                    "dependencies": (
                        dependencies if isinstance(dependencies, list) else [str(dependencies)]
                    ),
                    "proof_hints": (
                        proof_hints if isinstance(proof_hints, list) else [str(proof_hints)]
                    ),
                    "insertion_point": str(item.get("insertion_point", "") or "").strip(),
                }
            )

    return {
        "obstacle_summary": str(payload.get("obstacle_summary", "") or "").strip(),
        "recommended_split": str(
            payload.get("recommended_split", "") or payload.get("proof_split", "") or ""
        ).strip(),
        "insertion_guidance": str(payload.get("insertion_guidance", "") or "").strip(),
        "first_concrete_next_edit": str(
            payload.get("first_concrete_next_edit", "") or payload.get("next_edit", "") or ""
        ).strip(),
        "helpers": helpers,
        "raw_helper_count": len(raw_helpers) if isinstance(raw_helpers, list) else 0,
    }


def _decomposition_next_step(validation: dict[str, Any]) -> str:
    """Return concrete prover guidance that reflects skeleton validation."""
    ready_to_prove_count = int(validation.get("ready_to_prove_count", 0) or 0)
    ready_to_insert_count = int(validation.get("ready_to_insert_count", 0) or 0)
    managed_placement_count = int(validation.get("ready_for_managed_placement_count", 0) or 0)
    instantiated_parent_rejected_count = int(
        validation.get("instantiated_parent_rejected_count", 0) or 0
    )
    source_conflict_count = int(validation.get("source_conflict_count", 0) or 0)
    if ready_to_prove_count == 0:
        if instantiated_parent_rejected_count:
            return (
                "Discard every helper marked rejected_instantiated_parent: substituting one "
                "literal into the full parent conclusion only moves the unresolved proof. "
                "Preserve the parent parameter in a reusable residue lemma, or state a "
                "distinct structural base fact, then rerun lean_decompose_helpers."
            )
        if source_conflict_count:
            return (
                "Discard every helper marked rejected_source_conflict: the proposed terminal "
                "contradiction conflicts with sorry-free source constraints. Choose a distinct "
                "non-False coverage or witness-producing helper, then rerun "
                "lean_decompose_helpers. Do not spend a Lean check on the rejected route."
            )
        return (
            "Do not insert the proposed helpers: none even passed Lean template validation. "
            "Use each helper's check_diagnostics to revise the declarations, then rerun "
            "lean_decompose_helpers."
        )
    if ready_to_insert_count == 0:
        guidance = (
            "Do not insert any proposed helper template manually. The ready_to_prove helpers "
            "only elaborated as candidates. Replace every `sorry` or `admit` with a complete "
            "proof, then validate it before manual insertion with "
            "`lean_incremental_check(action=check_helper, theorem_id=<assigned theorem>, "
            "replacement=<complete helper>, include_axiom_profile=true)`. Insert only a helper "
            "reported sorry-free, profile-checked, and free of disallowed axioms."
        )
        if managed_placement_count:
            guidance += (
                " The deterministic decomposer may transactionally place only exact helpers "
                "marked ready_for_managed_placement as unresolved queue work; that flag is not "
                "proof success or permission for a model-authored edit."
            )
        if source_conflict_count:
            return "Ignore helpers marked rejected_source_conflict. " + guidance
        if instantiated_parent_rejected_count:
            return "Ignore helpers marked rejected_instantiated_parent. " + guidance
        return guidance
    guidance = (
        "Insert only complete, sorry-free helper declarations explicitly marked "
        "ready_to_insert. Any helper marked only ready_to_prove is still a planning "
        "template: complete it and validate it with `lean_incremental_check(action=check_helper, "
        "theorem_id=<assigned theorem>, replacement=<complete helper>, "
        "include_axiom_profile=true)` before editing the file. Then assemble and "
        "verify the assigned declaration with lean_incremental_check(check_target)."
    )
    if source_conflict_count:
        return "Ignore helpers marked rejected_source_conflict. " + guidance
    if instantiated_parent_rejected_count:
        return "Ignore helpers marked rejected_instantiated_parent. " + guidance
    return guidance


def lean_decompose_helpers_tool(
    theorem_id: str,
    file_path: str,
    *,
    theorem_statement: str = "",
    current_diagnostics: str = "",
    current_goals: str = "",
    current_attempt: str = "",
    recent_failed_attempts: str = "",
    question: str = "",
    cwd: str = "",
    max_helper_count: int = 6,
    timeout_s: int = LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S,
) -> str:
    """Ask the auxiliary theorem advisor for a structured helper-lemma split."""
    theorem_id = str(theorem_id or "").strip()
    file_path = str(file_path or "").strip()
    if not theorem_id:
        return _decompose_failure("invalid_request", "missing theorem_id.", file_path=file_path)
    if not file_path:
        return _decompose_failure("invalid_request", "missing file_path.", theorem_id=theorem_id)
    if scratch_only_dispatch_worker_enabled():
        return _decompose_failure(
            "disabled_in_scratch_dispatch",
            "nested LLM advisor calls are disabled inside scratch dispatch workers.",
            theorem_id=theorem_id,
            file_path=file_path,
        )
    request_timeout_s = _advisor_timeout_s(
        timeout_s,
        minimum_s=LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S,
    )
    request_deadline = time.monotonic() + request_timeout_s
    timeout_s = request_timeout_s
    try:
        max_helper_count = min(12, max(1, int(max_helper_count or 6)))
    except (TypeError, ValueError):
        max_helper_count = 6
    try:
        max_tokens = max(
            1000, int(os.getenv("LEANFLOW_LEAN_DECOMPOSE_HELPERS_MAX_TOKENS", "64000"))
        )
    except (TypeError, ValueError):
        max_tokens = 64000

    source_context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id=theorem_id,
        file_path=file_path,
        cwd=cwd,
    )
    advisor_context = advisor_source_context.load_advisor_source_context(
        theorem_id=theorem_id,
        file_path=file_path,
        cwd=cwd,
        evidence="\n\n".join(
            part
            for part in (
                theorem_statement,
                current_diagnostics,
                current_goals,
                current_attempt,
                recent_failed_attempts,
                question,
            )
            if part
        ),
    )
    source_statement = (
        str(source_context.target_statement or "").strip()
        if source_context.status == "loaded"
        else ""
    )
    authoritative_statement = (
        source_statement or advisor_context.target_statement or theorem_statement
    )
    caller_statement_overridden = bool(
        source_statement
        and str(theorem_statement or "").strip()
        and _statement_identity_key(theorem_statement) != _statement_identity_key(source_statement)
    )
    prompt_context = decomposer_prompt.shape_decomposer_prompt_context(
        theorem_id=theorem_id,
        theorem_statement=authoritative_statement,
        current_diagnostics=current_diagnostics,
        current_goals=current_goals,
        current_attempt=current_attempt,
        recent_failed_attempts=recent_failed_attempts,
        source_context=source_context,
    )

    system_prompt = (
        "You are an auxiliary Lean proof-decomposition planner for LeanFlow. "
        "Return strict JSON only. Do not use markdown fences or prose outside JSON. "
        "Your job is to split one hard Lean theorem into small helper declarations "
        "that preserve the target statement exactly. You do not edit files and your "
        "answer is not verification evidence. Propose at most the requested number "
        "of helper lemmas, ordered by dependency. Each helper must include a Lean "
        "skeleton in `lean_skeleton` ending with `by sorry`; these skeletons are "
        "temporary planning artifacts only, not final proof success. Never suggest "
        "inserting or patching a skeleton while it still contains `sorry` or `admit`. "
        "Use `insertion_guidance` only for the eventual location of a completed helper, "
        "and make `first_concrete_next_edit` the work of completing and checking a "
        "sorry-free helper proof before any insertion. Do not suggest "
        "weakening, deleting, renaming, moving, or changing the target declaration. "
        "Do not copy declaration attributes such as `@[category ...]` or `@[AMS ...]` "
        "onto helpers unless those attributes are visibly available in the supplied "
        "theorem statement or diagnostics. Treat supplied sorry-free source-backed "
        "negative or consistency constraints as authoritative route constraints. Never "
        "propose a helper whose conclusion conflicts with them, and never turn a "
        "source-verified consistent terminal branch into `False`; prefer a coverage or "
        "witness-producing helper for that branch. "
        "The authoritative referenced declarations in the request are exact source, not "
        "informal hints. Never redefine them, replace their bodies with assumed formulas, or "
        "diagnose an unfold/rewrite without first checking the exact supplied body and the "
        "intermediate Lean goal. Current-source declaration availability and the parent "
        "manager's banked-helper status override contradictory narrative history. "
        f"{decomposer_admission.DECOMPOSITION_ADMISSION_PROMPT_CONTRACT}"
        "Prefer local/private helper lemmas and concrete proof hints over broad strategy."
        f"{_clean_room_advisor_instructions()}"
    )
    json_contract = (
        "{"
        '"obstacle_summary":"...",'
        '"recommended_split":"...",'
        '"insertion_guidance":"...",'
        '"first_concrete_next_edit":"...",'
        '"helpers":[{"name":"...","purpose":"...","lean_skeleton":"private lemma ... := by\\n  sorry","dependencies":["..."],"proof_hints":["..."],"insertion_point":"before target theorem"}]'
        "}"
    )
    user_prompt, prompt_stats = decomposer_prompt.compose_decomposer_user_prompt(
        context=prompt_context,
        file_path=file_path,
        theorem_id=theorem_id,
        cwd=cwd,
        max_helper_count=max_helper_count,
        question=question,
        json_contract=json_contract,
        source_declarations="\n\n".join(advisor_context.referenced_declarations),
    )
    prompt_stats.update(
        {
            "theorem_statement_source": "source" if source_statement else "caller",
            "caller_statement_overridden": caller_statement_overridden,
            "target_statement_sha256": (
                hashlib.sha256(authoritative_statement.encode("utf-8")).hexdigest()
                if authoritative_statement
                else ""
            ),
            "referenced_source_status": advisor_context.status,
            "referenced_source_names": list(advisor_context.referenced_names),
            "provisional_source_names": list(advisor_context.provisional_names),
            "referenced_source_sha256": advisor_context.source_sha256,
        }
    )
    expert_provider = resolve_expert_provider("lean_decompose_helpers")
    command_prompt = (
        f"System instructions:\n{system_prompt}\n\nDecomposition request:\n{user_prompt}"
    )

    provider_payload: dict[str, Any]
    response_text = ""
    provider_timeout_s = _remaining_request_timeout_s(request_deadline)
    if provider_timeout_s <= 0:
        return _decompose_failure(
            "timeout",
            "the request deadline expired before the decomposition advisor started.",
            theorem_id=theorem_id,
            file_path=file_path,
        )
    if _command_advisor_allowed(expert_provider):
        try:
            command_result = run_command_expert_help(
                provider=expert_provider,
                task="lean_decompose_helpers",
                prompt=command_prompt,
                cwd=cwd,
                timeout_s=provider_timeout_s,
            )
        except RuntimeError as exc:
            return _decompose_failure(
                "unavailable", str(exc), theorem_id=theorem_id, file_path=file_path
            )
        except Exception as exc:
            return _decompose_failure(
                "error", f"{type(exc).__name__}: {exc}", theorem_id=theorem_id, file_path=file_path
            )
        if command_result.timed_out:
            return _decompose_failure(
                "timeout",
                f"{command_result.provider} expert command timed out.",
                theorem_id=theorem_id,
                file_path=file_path,
            )
        if command_result.exit_status != 0:
            return _decompose_failure(
                "error",
                f"{command_result.provider} expert command exited with status {command_result.exit_status}: {command_result.stderr or '[no stderr]'}",
                theorem_id=theorem_id,
                file_path=file_path,
            )
        response_text = command_result.response.strip()
        provider_payload = {
            "provider": command_result.provider,
            "mode": "command",
            "command": command_result.command,
            "exit_status": command_result.exit_status,
            "truncated": command_result.truncated,
            "response_chars": command_result.response_chars,
            "max_response_chars": command_result.max_response_chars,
        }
    else:
        try:
            record_expert_help_activity(
                "expert-help-request",
                "Expert helper-decomposition model request started",
                provider=expert_provider,
                mode="model",
                prompt=command_prompt,
                command=[],
                exit_status=None,
                theorem_id=theorem_id,
                file_path=file_path,
            )
            response = _call_model_advisor_with_heartbeat(
                task="lean_decompose_helpers",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                timeout_s=provider_timeout_s,
                provider=expert_provider,
                theorem_id=theorem_id,
                file_path=file_path,
            )
        except TimeoutError as exc:
            return _decompose_failure(
                "timeout", str(exc), theorem_id=theorem_id, file_path=file_path
            )
        except RuntimeError as exc:
            return _decompose_failure(
                "unavailable", str(exc), theorem_id=theorem_id, file_path=file_path
            )
        except Exception as exc:
            return _decompose_failure(
                "error", f"{type(exc).__name__}: {exc}", theorem_id=theorem_id, file_path=file_path
            )
        try:
            response_text = str(response.choices[0].message.content or "").strip()
        except Exception:
            response_text = ""
        provider_payload = {
            "provider": expert_provider,
            "mode": "model",
            "model": str(getattr(response, "model", "") or ""),
        }
        record_expert_help_activity(
            "expert-help-result",
            "Expert helper-decomposition model request finished",
            provider=expert_provider,
            mode="model",
            prompt=command_prompt,
            command=[],
            exit_status=None,
            response=response_text,
            truncated=False,
            response_chars=len(response_text),
            max_response_chars=len(response_text),
            model=provider_payload["model"],
            theorem_id=theorem_id,
            file_path=file_path,
        )

    if _remaining_request_timeout_s(request_deadline) <= 0:
        return _decompose_failure(
            "timeout",
            "the decomposition advisor consumed the whole request deadline before Lean validation.",
            theorem_id=theorem_id,
            file_path=file_path,
        )

    parsed = _extract_json_object(response_text)
    if parsed is None:
        return json.dumps(
            {
                "success": False,
                "status": "invalid_json",
                "theorem_id": theorem_id,
                "file_path": file_path,
                **provider_payload,
                "message": "The helper-decomposition advisor did not return a JSON object.",
                "raw_response": response_text[:4000],
            },
            ensure_ascii=False,
        )

    normalized = _normalize_decomposition_payload(parsed, max_helper_count=max_helper_count)
    response_grounding_text = "\n\n".join(
        [
            str(normalized.get("obstacle_summary", "") or ""),
            str(normalized.get("recommended_split", "") or ""),
            str(normalized.get("insertion_guidance", "") or ""),
            str(normalized.get("first_concrete_next_edit", "") or ""),
            json.dumps(normalized.get("helpers", []), ensure_ascii=False),
        ]
    )
    source_redefinitions = advisor_source_context.advisor_source_conflicts(
        response_grounding_text,
        advisor_context,
    )
    if source_redefinitions:
        return json.dumps(
            {
                "success": False,
                "status": "source_conflict",
                "theorem_id": theorem_id,
                "file_path": file_path,
                **provider_payload,
                "source_conflicts": list(source_redefinitions),
                "context_shaping": prompt_stats,
                "message": (
                    "The helper-decomposition advisor treated authoritative in-file "
                    "declaration(s) as hypothetical or redefined them: "
                    + ", ".join(source_redefinitions)
                    + ". Ignore this decomposition and continue from exact source and "
                    "kernel-checked evidence."
                ),
            },
            ensure_ascii=False,
        )
    validation_timeout_s = min(
        120,
        max(1, _remaining_request_timeout_s(request_deadline)),
    )
    helpers, validation = _validate_helper_skeletons(
        helpers=list(normalized["helpers"]),
        theorem_statement=authoritative_statement,
        file_path=file_path,
        theorem_id=theorem_id,
        cwd=cwd,
        timeout_s=validation_timeout_s,
        deadline=request_deadline,
        source_constraints=source_context.constraints,
    )
    if validation.get("deadline_exhausted") is True:
        return _decompose_failure(
            "timeout",
            "the whole-request deadline expired during Lean skeleton validation.",
            theorem_id=theorem_id,
            file_path=file_path,
        )
    normalized["helpers"] = helpers
    instantiated_parent_rejected_count = int(
        validation.get("instantiated_parent_rejected_count", 0) or 0
    )
    source_conflict_count = int(validation.get("source_conflict_count", 0) or 0)
    constraint_names = [constraint.name for constraint in source_context.constraints]
    source_guard = {
        "applied": bool(source_conflict_count),
        "rejected_helper_count": source_conflict_count,
        "constraint_names": constraint_names,
        "source_sha256": source_context.source_sha256,
    }
    admission_guard = {
        "applied": bool(instantiated_parent_rejected_count),
        "rejected_helper_count": instantiated_parent_rejected_count,
        "reason_code": (
            "closed_literal_parent_instantiation" if instantiated_parent_rejected_count else ""
        ),
    }
    if instantiated_parent_rejected_count:
        normalized["obstacle_summary"] = (
            "The proposed helper only substitutes a numeral into the complete parameterized "
            "parent conclusion, so it moves the unresolved proof instead of decomposing it."
        )
        normalized["recommended_split"] = (
            "Preserve the parent parameter in a reusable residue helper, or isolate a distinct "
            "structural finite-case fact whose conclusion is not the instantiated parent goal."
        )
    if source_conflict_count:
        fact_names = ", ".join(constraint_names[:3])
        normalized["obstacle_summary"] = (
            "The proposed terminal-False route is not useful because sorry-free target-scoped "
            f"source constraints block its claimed exhaustive prefix: {fact_names}."
        )
        normalized["recommended_split"] = (
            "Discard the source-conflicted terminal contradiction. Decompose the remaining "
            "branch through a non-False coverage, witness, or exact-characterization helper "
            "consistent with the cited source facts."
        )
    safe_next_step = _decomposition_next_step(validation)
    # Action-bearing advisor prose is untrusted. Individual insertion_point
    # fields preserve placement information after the proof is complete.
    normalized["insertion_guidance"] = (
        "Treat each helper's insertion_point as an eventual manual location only. Do not make "
        "a model-authored file edit until the exact declaration has a complete, checked, "
        "sorry-free proof and is marked ready_to_insert. The deterministic decomposer alone "
        "may transactionally place exact ready_for_managed_placement stubs as unresolved queue work."
    )
    normalized["first_concrete_next_edit"] = safe_next_step
    return json.dumps(
        {
            "success": True,
            "status": ("answered_with_source_guard" if source_conflict_count else "answered"),
            "theorem_id": theorem_id,
            "file_path": file_path,
            **provider_payload,
            **normalized,
            "context_shaping": prompt_stats,
            "source_constraint_guard": source_guard,
            "decomposition_admission_guard": admission_guard,
            "skeleton_validation": validation,
            "next_step": safe_next_step,
        },
        ensure_ascii=False,
    )
