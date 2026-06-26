"""Pure verifier-review payload/decision/findings text helpers.

These were carved out of ``native_runner`` as the fixpoint closure under "calls"
of the verifier-review text parsing/formatting leaves. Every moved function's
only non-stdlib callees are other moved functions or already-extracted
``native_utils`` leaves (``_bounded_verifier_response``, ``_extract_json_payload``,
``_single_line``). None of them reaches native_runner-only mutable state, a Lean
or queue backend, or a name tests monkeypatch on ``native_runner``, so the move
is behavior-preserving and the in-module callers (the advisory/configured
verification-review orchestrators) keep resolving the names through the
re-export shim in ``native_runner``.

This module imports ONLY stdlib (``re``, ``typing``) plus ``native_utils`` and
does NOT import ``native_runner``, so the re-export introduces no import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from leanflow_cli.native.native_utils import (
    _bounded_verifier_response,
    _extract_json_payload,
    _single_line,
)


def _verification_review_result_payload(result: Any) -> dict[str, Any]:
    return {
        "task": str(getattr(result, "task", "") or ""),
        "provider": str(getattr(result, "provider", "") or ""),
        "mode": str(getattr(result, "mode", "") or ""),
        "status": str(getattr(result, "status", "") or ""),
        "command": list(getattr(result, "command", []) or []),
        "exit_status": getattr(result, "exit_status", None),
        "response": _bounded_verifier_response(str(getattr(result, "response", "") or "")),
        "response_chars": int(getattr(result, "response_chars", 0) or 0),
        "max_response_chars": int(getattr(result, "max_response_chars", 0) or 0),
        "truncated": bool(getattr(result, "truncated", False)),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "model": str(getattr(result, "model", "") or ""),
        "error": str(getattr(result, "error", "") or ""),
    }


def _verification_review_decision(payload: Mapping[str, Any] | None) -> str:
    response = str((payload or {}).get("response", "") or "").strip()
    if not response:
        return ""
    parsed = _extract_json_payload(response)
    if isinstance(parsed, Mapping):
        for key in ("decision", "status", "result"):
            value = str(parsed.get(key, "") or "").strip().upper()
            if value in {"PASS", "BLOCK"}:
                return value
    match = re.search(
        r"^\s*(?:[#>*_`\-]+\s*)?(?:Decision\s*[:=-]\s*)?\**(PASS|BLOCK)\**\b",
        response,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\bDecision\s*[:=-]\s*(PASS|BLOCK)\b", response, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _verification_review_findings(
    payload: Mapping[str, Any] | None, *, limit: int = 5
) -> list[str]:
    response = str((payload or {}).get("response", "") or "").strip()
    if not response:
        return []
    parsed = _extract_json_payload(response)
    findings: list[str] = []
    if isinstance(parsed, Mapping):
        raw_findings = (
            parsed.get("findings") or parsed.get("issues") or parsed.get("blockers") or []
        )
        if isinstance(raw_findings, list):
            for item in raw_findings:
                text = _single_line(item, 240)
                if text:
                    findings.append(text)
                    if len(findings) >= limit:
                        return findings
    for line in response.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^(PASS|BLOCK)\b", stripped, flags=re.IGNORECASE):
            continue
        bullet = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", stripped)
        if bullet:
            finding = _single_line(bullet.group(1), 240)
            if finding:
                findings.append(finding)
        elif (
            "block" in stripped.lower()
            or "missing" in stripped.lower()
            or "fix" in stripped.lower()
        ):
            findings.append(_single_line(stripped, 240))
        if len(findings) >= limit:
            break
    if findings:
        return findings
    return [_single_line(response, 240)] if response else []


def _print_verification_review_summary(payload: Mapping[str, Any]) -> None:
    provider = str(payload.get("provider", "") or "verifier")
    task = str(payload.get("task", "") or "verification").replace("_", " ")
    decision = _verification_review_decision(payload) or str(
        payload.get("status", "") or "reviewed"
    )
    findings = _verification_review_findings(payload, limit=3)
    print(f"{task.title()} verifier feedback ({provider}): {decision}")
    for finding in findings:
        print(f"- {finding}")


def _autoformalizer_advisory_block_issues(payload: Mapping[str, Any] | None) -> list[str]:
    if not payload:
        return []
    if _verification_review_decision(payload) != "BLOCK":
        return []
    findings = _verification_review_findings(payload, limit=4)
    if not findings:
        findings = ["configured verifier returned BLOCK without detailed findings"]
    return [f"configured autoformalizer verifier returned BLOCK: {finding}" for finding in findings]
