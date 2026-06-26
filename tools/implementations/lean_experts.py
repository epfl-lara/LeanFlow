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

import json
import os
import re
from typing import Any

from agent.providers.auxiliary_client import call_llm
from leanflow_cli.cli.expert_help import (
    is_command_expert_provider,
    record_expert_help_activity,
    resolve_expert_provider,
    run_command_expert_help,
)
from leanflow_cli.lean.lean_incremental import lean_incremental_check

LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S = 1200
LEAN_REASONING_HELP_MIN_TIMEOUT_S = 1200
LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S = LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S
LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S = LEAN_REASONING_HELP_MIN_TIMEOUT_S


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
                f"{message} Continue with the main proof workflow; do not treat missing advisor "
                "advice as evidence that the theorem statement should change."
            ),
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

    try:
        max_tokens = max(1000, int(os.getenv("LEANFLOW_LEAN_REASONING_HELP_MAX_TOKENS", "64000")))
    except (TypeError, ValueError):
        max_tokens = 64000

    system_prompt = (
        "You are an auxiliary Lean proof-strategy advisor for LeanFlow. "
        "Act as a world-class mathematical strategist, combining deep olympiad, "
        "analysis, algebra, and formal-verification taste with practical Lean and "
        "Mathlib expertise. "
        "Your role is advisory only: you do not edit files, you do not decide success, "
        "and your answer is not verification evidence. Give concrete proof ideas, "
        "search terms, likely lemmas, and tactic sketches for the assigned theorem only. "
        "Preserve the existing theorem/lemma/example statement exactly, including its "
        "name, binders, hypotheses, conclusion, attributes, namespace location, and "
        "surrounding API. If the statement appears wrong, underspecified, or too hard, "
        "report a blocker and explain the evidence instead of proposing a changed "
        "statement. Do not suggest deleting, weakening, renaming, moving, or splitting "
        "the declaration unless the user explicitly asked for a refactor. Do not suggest "
        "replacing the proof with sorry, admit, axiom, unsafe code, or a placeholder. "
        "You may suggest small helper lemmas or private supporting declarations when "
        "they preserve existing statements and directly help the assigned theorem."
    )
    user_prompt = "\n\n".join(
        part
        for part in [
            f"File: {file_path}",
            f"Theorem: {theorem_id}",
            f"Working directory: {cwd}" if cwd else "",
            f"Theorem statement:\n{theorem_statement}" if theorem_statement else "",
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

    if is_command_expert_provider(expert_provider):
        try:
            command_result = run_command_expert_help(
                provider=expert_provider,
                task="lean_reasoning",
                prompt=command_prompt,
                cwd=cwd,
                timeout_s=max(LEAN_REASONING_HELP_MIN_TIMEOUT_S, int(timeout_s or 0)),
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
        advice = command_result.response.strip()
        if not advice:
            return _advisor_failure(
                "no_answer",
                "the command expert advisor returned no content.",
                theorem_id=theorem_id,
                file_path=file_path,
            )
        return json.dumps(
            {
                "success": True,
                "status": "answered",
                "theorem_id": theorem_id,
                "file_path": file_path,
                "provider": command_result.provider,
                "mode": "command",
                "command": command_result.command,
                "exit_status": command_result.exit_status,
                "truncated": command_result.truncated,
                "response_chars": command_result.response_chars,
                "max_response_chars": command_result.max_response_chars,
                "advice": advice,
                "next_step": (
                    "Use this as advice only. Ignore any suggestion that changes the declaration "
                    "or uses a placeholder proof, then apply a concrete proof edit and verify the "
                    "assigned queue declaration with lean_incremental_check(check_target)."
                ),
            },
            ensure_ascii=False,
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
        response = call_llm(
            task="lean_reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=max(LEAN_REASONING_HELP_MIN_TIMEOUT_S, int(timeout_s or 0)),
        )
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

    return json.dumps(
        {
            "success": True,
            "status": "answered",
            "theorem_id": theorem_id,
            "file_path": file_path,
            "provider": expert_provider,
            "mode": "model",
            "model": str(getattr(response, "model", "") or ""),
            "advice": advice,
            "next_step": (
                "Use this as advice only. Ignore any suggestion that changes the declaration "
                "or uses a placeholder proof, then apply a concrete proof edit and verify the "
                "assigned queue declaration with lean_incremental_check(check_target)."
            ),
        },
        ensure_ascii=False,
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
                f"{message} Continue with the main proof workflow; do not treat missing helper "
                "decomposition as evidence that the theorem statement should change."
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
    statement = str(theorem_statement or "").strip()
    if not statement:
        return ""
    if re.search(r":=\s*by\s*$", statement):
        return f"{statement}\n  sorry"
    if re.search(r":=\s*$", statement):
        return f"{statement} by\n  sorry"
    if re.search(r"\bby\s*$", statement):
        return f"{statement}\n  sorry"
    if ":=" in statement:
        return f"{statement}\n  sorry"
    return f"{statement} := by\n  sorry"


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
    for key in ("errors", "error_count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return sum(
        1 for item in _diagnostic_items(payload) if str(item.get("severity", "")).lower() == "error"
    )


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


def _validate_helper_skeletons(
    *,
    helpers: list[dict[str, Any]],
    theorem_statement: str,
    file_path: str,
    theorem_id: str,
    cwd: str,
    timeout_s: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate proposed helper-lemma skeletons via incremental Lean checks, building accepted skeletons cumulatively to check each new helper against all previously-valid ones. Mutates each helper dict with check_status, ready_to_insert, check_diagnostics, and validation_order; returns the list and a summary of validated/ready counts for the decompose-helpers response."""
    target_skeleton = _target_sorry_skeleton(theorem_statement)
    if not target_skeleton:
        for helper in helpers:
            helper["check_status"] = "skipped"
            helper["ready_to_insert"] = False
            helper["check_diagnostics"] = (
                "missing theorem_statement; skeleton validation requires the unchanged target declaration closed with `by sorry`."
            )
        return helpers, {
            "status": "skipped",
            "reason": "missing_theorem_statement",
            "validated_count": 0,
            "ready_count": 0,
        }

    validated_count = 0
    ready_count = 0
    accepted_prefix: list[str] = []
    for index, helper in enumerate(helpers):
        skeleton = str(helper.get("lean_skeleton", "") or helper.get("skeleton", "") or "").strip()
        if not skeleton:
            helper["check_status"] = "skipped"
            helper["ready_to_insert"] = False
            helper["check_diagnostics"] = "missing lean_skeleton"
            continue
        replacement = "\n\n".join([*accepted_prefix, skeleton, target_skeleton])
        try:
            check = lean_incremental_check(
                action="check_target",
                file_path=file_path,
                theorem_id=theorem_id,
                cwd=cwd,
                replacement=replacement,
                include_tactics=False,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            helper["check_status"] = "failed"
            helper["ready_to_insert"] = False
            helper["check_diagnostics"] = f"{type(exc).__name__}: {exc}"
            continue

        validated_count += 1
        errors = _payload_error_count(dict(check))
        success = bool(dict(check).get("success", True))
        if success and errors == 0:
            helper["check_status"] = "ok"
            helper["ready_to_insert"] = True
            helper["check_diagnostics"] = _validation_diagnostics(dict(check))
            accepted_prefix.append(skeleton)
            ready_count += 1
        else:
            helper["check_status"] = "failed"
            helper["ready_to_insert"] = False
            helper["check_diagnostics"] = (
                _validation_diagnostics(dict(check)) or "Lean skeleton check failed."
            )
        helper["validation_order"] = index + 1

    return helpers, {
        "status": "checked",
        "validated_count": validated_count,
        "ready_count": ready_count,
        "allows_sorry_warnings": True,
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

    system_prompt = (
        "You are an auxiliary Lean proof-decomposition planner for LeanFlow. "
        "Return strict JSON only. Do not use markdown fences or prose outside JSON. "
        "Your job is to split one hard Lean theorem into small helper declarations "
        "that preserve the target statement exactly. You do not edit files and your "
        "answer is not verification evidence. Propose at most the requested number "
        "of helper lemmas, ordered by dependency. Each helper must include a Lean "
        "skeleton in `lean_skeleton` ending with `by sorry`; these skeletons are "
        "temporary planning artifacts only, not final proof success. Do not suggest "
        "weakening, deleting, renaming, moving, or changing the target declaration. "
        "Prefer local/private helper lemmas and concrete proof hints over broad strategy."
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
    user_prompt = "\n\n".join(
        part
        for part in [
            f"File: {file_path}",
            f"Theorem: {theorem_id}",
            f"Working directory: {cwd}" if cwd else "",
            f"Maximum helper count: {max_helper_count}",
            f"Theorem statement:\n{theorem_statement}" if theorem_statement else "",
            f"Current diagnostics:\n{current_diagnostics}" if current_diagnostics else "",
            f"Current goals:\n{current_goals}" if current_goals else "",
            f"Current attempt:\n{current_attempt}" if current_attempt else "",
            f"Recent failed attempts:\n{recent_failed_attempts}" if recent_failed_attempts else "",
            (
                f"Question:\n{question}"
                if question
                else "Question:\nDecompose this hard proof into helper lemmas that the main agent can insert and prove one at a time."
            ),
            f"Required JSON shape:\n{json_contract}",
        ]
        if part
    )
    expert_provider = resolve_expert_provider("lean_decompose_helpers")
    command_prompt = (
        f"System instructions:\n{system_prompt}\n\nDecomposition request:\n{user_prompt}"
    )

    provider_payload: dict[str, Any]
    response_text = ""
    if is_command_expert_provider(expert_provider):
        try:
            command_result = run_command_expert_help(
                provider=expert_provider,
                task="lean_decompose_helpers",
                prompt=command_prompt,
                cwd=cwd,
                timeout_s=max(LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S, int(timeout_s or 0)),
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
            response = call_llm(
                task="lean_decompose_helpers",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                timeout=max(LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S, int(timeout_s or 0)),
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
    helpers, validation = _validate_helper_skeletons(
        helpers=list(normalized["helpers"]),
        theorem_statement=theorem_statement,
        file_path=file_path,
        theorem_id=theorem_id,
        cwd=cwd,
        timeout_s=min(120, max(10, int(timeout_s or 0))),
    )
    normalized["helpers"] = helpers
    return json.dumps(
        {
            "success": True,
            "status": "answered",
            "theorem_id": theorem_id,
            "file_path": file_path,
            **provider_payload,
            **normalized,
            "skeleton_validation": validation,
            "next_step": (
                "Use ready_to_insert helper skeletons as a decomposition proposal only. "
                "Patch helpers deliberately, prove them without lingering `sorry`, and verify "
                "the assigned queue declaration with lean_incremental_check(check_target)."
            ),
        },
        ensure_ascii=False,
    )
