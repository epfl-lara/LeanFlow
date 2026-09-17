"""Bound unusable prover responses without retaining or repeating private reasoning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.utils import atomic_json_write

RESPONSE_RECOVERY_PROMPT = (
    "Your previous response did not produce a usable proof action or submission. "
    "This is the single recovery request for this interruption, within the existing "
    "call budget. Use the saved assignment and proof artifacts to make one small, "
    "concrete Lean tool call, or submit the complete candidate if it is ready. "
    "Keep the next step concise. Do not restart or repeat the preceding derivation."
)
RESPONSE_FAILURE = (
    "This prover attempt ended after repeated unusable responses or rejected tool "
    "requests without an executed action or candidate. Its in-session recovery "
    "allowance is unavailable or already consumed. In research mode, control returns "
    "to the orchestrator to choose the next proof action within the campaign budget. "
    "Saved proof artifacts and unused campaign calls are preserved."
)
_MAX_STATE_BYTES = 8192


def executed_tool_result(name: str, result: Mapping[str, Any]) -> bool:
    """Recognize execution evidence without treating mathematical rejection as failure.

    Lean's ``success`` says elaboration ran, independently of its ``ok`` verdict.
    Structured computation failures also confirm execution, whereas denied calls,
    malformed arguments, and unavailable tools produce no such evidence.
    """
    if result.get("success") is True:
        return True
    if name == "lean_search":
        if result.get("success") is False:
            return False
        # LeanSearchResult has no success/status field. Completed native searches
        # must clear the guard even when the search found no matching lemmas.
        attempted = result.get("attempted_providers")
        if isinstance(result.get("results"), list) and isinstance(attempted, list) and attempted:
            return True
    if name == "compute":
        return result.get("status") in {
            "empirical_compute_error",
            "empirical_compute_output_limit",
            "empirical_compute_timeout",
            "empirical_compute_resource_limit",
        }
    if name in {"web_search", "lean_search"}:
        return result.get("status") == "no_results"
    return name == "research_job" and bool(result.get("job_id"))


@dataclass(frozen=True)
class ResponseNotice:
    """Describe recovery admission or a terminal job-local response failure."""

    action: Literal["recovery", "stop"]
    reason: str
    fingerprint: str
    count: int
    message: str


def _fingerprint(assistant: Mapping[str, Any]) -> str:
    """Hash response text without making provider IDs or usage counts significant."""
    payload = {
        key: assistant.get(key)
        for key in ("content", "reasoning", "reasoning_content", "reasoning_details")
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProverResponseGuard:
    """Allow one focused recovery until a proof action resumes, failing closed on restart.

    Unique complete prose is not itself a failure. Empty or truncated responses,
    and repeated identical prose without tools or a candidate, consume a single
    recovery allowance. Only an action or candidate clears that interruption.
    The ledger stores hashes and counters, never response or reasoning text.
    """

    def __init__(self, runtime_directory: Path, *, role: str) -> None:
        """Restore response history without granting another interrupted recovery."""
        self.path = runtime_directory / "response-guard.json"
        self.enabled = role in {"prover", "negation"}
        self.stopped = False
        self._recovering = False
        self._reason = ""
        self._fingerprint = ""
        self._repeats = 0
        self._failures = 0
        if self.enabled and self.path.exists():
            self._restore()

    def _restore(self) -> None:
        """Fail closed if a recovery was admitted or its ledger cannot be trusted."""
        try:
            if self.path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("Response guard ledger exceeds its size bound")
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != 1:
                raise ValueError("Invalid response guard ledger")
            if any(type(saved.get(key)) is not bool for key in ("recovering", "stopped")):
                raise ValueError("Invalid response guard state")
            for key in ("repeats", "failures"):
                value = saved.get(key)
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid response guard counter")
            fingerprint = saved.get("fingerprint")
            if not isinstance(fingerprint, str) or (
                fingerprint
                and (
                    len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)
                )
            ):
                raise ValueError("Invalid response fingerprint")
            self._fingerprint = fingerprint
            self._repeats = saved["repeats"]
            self._failures = saved["failures"]
            self._recovering = saved["recovering"]
            self.stopped = saved["stopped"] or self._recovering
            self._reason = str(saved.get("reason") or "interrupted_recovery")
        except (OSError, UnicodeError, ValueError, TypeError):
            self.stopped = True
            self._reason = "invalid_recovery_state"

    def _save(self) -> None:
        """Commit recovery admission before the caller can dispatch another request."""
        atomic_json_write(
            self.path,
            {
                "version": 1,
                "recovering": self._recovering,
                "stopped": self.stopped,
                "reason": self._reason,
                "fingerprint": self._fingerprint,
                "repeats": self._repeats,
                "failures": self._failures,
            },
        )

    @property
    def error(self) -> str:
        """Return a fixed failure explanation safe for prompts, logs, and UI."""
        return RESPONSE_FAILURE

    @property
    def stop_reason(self) -> dict[str, Any]:
        """Explain why this attempt stopped without declaring the campaign exhausted."""
        return {
            "code": "response_stalled",
            "scope": "job",
            "message": self.error,
            "response_kind": self._reason,
            "unusable_responses": self._failures,
            "next_step": (
                "In research mode, the orchestrator uses the saved artifacts and "
                "failure evidence to select the next proof action. Other runnable "
                "obligations may continue."
            ),
        }

    def _notice(self, action: Literal["recovery", "stop"]) -> ResponseNotice:
        """Build a notice containing only safe metadata and fixed guidance."""
        return ResponseNotice(
            action=action,
            reason=self._reason,
            fingerprint=self._fingerprint,
            count=self._failures,
            message=self.error if action == "stop" else RESPONSE_RECOVERY_PROMPT,
        )

    def complete(self) -> None:
        """Clear a pending failure after the controller accepts a submitted candidate."""
        if not self.enabled:
            return
        self.stopped = self._recovering = False
        self._reason = self._fingerprint = ""
        self._repeats = self._failures = 0
        self._save()

    def observe(
        self,
        assistant: Mapping[str, Any],
        *,
        candidate_ready: bool,
        used: int,
        limit: int,
        tool_executed: bool = False,
    ) -> ResponseNotice | None:
        """Allow useful work or one bounded recovery, without extending the API budget."""
        if not self.enabled:
            return None
        if self.stopped:
            return self._notice("stop")
        if candidate_ready or tool_executed:
            if self._fingerprint or self._recovering:
                self._recovering = False
                self._reason = self._fingerprint = ""
                self._repeats = self._failures = 0
                self._save()
            return None
        fingerprint = _fingerprint(assistant)
        self._repeats = self._repeats + 1 if fingerprint == self._fingerprint else 1
        self._fingerprint = fingerprint
        content = str(assistant.get("content") or "").strip()
        reason = (
            "invalid_tool_batch"
            if assistant.get("tool_calls")
            else (
                "truncated"
                if assistant.get("finish_reason") in {"length", "incomplete"}
                else "empty" if not content else "duplicate" if self._repeats > 1 else ""
            )
        )
        if not reason:
            self._save()
            return None
        self._reason = reason
        self._failures += 1
        self.stopped = self._recovering or used >= limit
        self._recovering = True
        self._save()
        return self._notice("stop" if self.stopped else "recovery")
