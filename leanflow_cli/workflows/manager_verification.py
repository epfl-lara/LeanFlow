"""Normalize manager-verification decisions and provider policy."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from leanflow_cli.config import load_config
from leanflow_cli.native.native_config import _read_int_env, _read_text_env
from leanflow_cli.workflows.queue_manager import verification_from_mapping, verification_to_mapping
from leanflow_cli.workflows.verification_providers import BLUEPRINT_VERIFICATION_TASK

__all__ = [
    "MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S",
    "MANAGER_INCREMENTAL_CHECK_TIMEOUT_DEFAULT_S",
    "_manager_incremental_prepare_timeout_s",
    "_manager_incremental_check_timeout_s",
    "_incremental_prepare_blocking_declaration",
    "_last_verification_record",
    "_verification_outcome",
    "_manager_feedback_retry_key",
    "_verification_task_has_aux_overrides",
    "_verification_review_system_prompt",
]

# Default deterministic-verification timeouts (seconds). Preparing the reusable
# environment may pay a cold-start cost; an individual inner-loop target check
# must stay short so a bad tactic cannot stall the managed proof turn.
MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S = 300
MANAGER_INCREMENTAL_CHECK_TIMEOUT_DEFAULT_S = 60

_ENV_BEFORE_TARGET_BLOCKER_RE = re.compile(
    r"failed to build env before target at\s+([A-Za-z_«][\w'.«»]*):",
    re.IGNORECASE,
)


def _incremental_prepare_blocking_declaration(error: str) -> str:
    """Return the earlier declaration named by an incremental warmup failure.

    LeanProbe reports this form when elaborating the environment before the
    requested target fails. The named prerequisite, rather than the later
    target, must become the managed queue assignment.
    """
    match = _ENV_BEFORE_TARGET_BLOCKER_RE.search(str(error or ""))
    return match.group(1) if match else ""


def _manager_incremental_prepare_timeout_s() -> int:
    return _read_int_env(
        "LEANFLOW_MANAGER_INCREMENTAL_PREPARE_TIMEOUT_S",
        MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S,
    )


def _manager_incremental_check_timeout_s() -> int:
    return _read_int_env(
        "LEANFLOW_MANAGER_INCREMENTAL_CHECK_TIMEOUT_S",
        MANAGER_INCREMENTAL_CHECK_TIMEOUT_DEFAULT_S,
    )


def _last_verification_record(
    autonomy_state: Mapping[str, Any] | None = None,
    live_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    for source in (autonomy_state, live_state):
        record = verification_to_mapping(
            verification_from_mapping(dict((source or {}).get("last_verification") or {}))
        )
        if record:
            return record
    return {}


def _verification_outcome(
    autonomy_state: Mapping[str, Any] | None = None,
    live_state: Mapping[str, Any] | None = None,
) -> Literal["unverified", "ok", "failed"]:
    record = _last_verification_record(autonomy_state, live_state)
    if record:
        return "ok" if bool(record.get("ok")) else "failed"
    for source in (live_state, autonomy_state):
        if isinstance(source, Mapping) and "verification_ok" in source:
            return "ok" if bool(source.get("verification_ok")) else "failed"
    return "unverified"


def _manager_feedback_retry_key(target_symbol: str, active_file: str) -> str:
    file_key = str(active_file or "").strip()
    if file_key:
        with contextlib.suppress(Exception):
            file_key = str(Path(file_key).expanduser().resolve())
    return f"{file_key}::{str(target_symbol or '').strip()}"


def _verification_task_has_aux_overrides(task: str) -> bool:
    task_key = str(task or "").strip().upper()
    if task_key:
        for suffix in ("MODEL", "BASE_URL", "API_KEY", "REASONING_EFFORT"):
            if _read_text_env(f"AUXILIARY_{task_key}_{suffix}", "").strip():
                return True
    try:
        config = load_config()
    except Exception:
        return False
    aux = config.get("auxiliary", {}) if isinstance(config, Mapping) else {}
    task_config = aux.get(task, {}) if isinstance(aux, Mapping) else {}
    if not isinstance(task_config, Mapping):
        return False
    return any(
        str(task_config.get(key, "") or "").strip()
        for key in ("model", "base_url", "api_key", "reasoning_effort")
    )


def _verification_review_system_prompt(task: str) -> str:
    if task == BLUEPRINT_VERIFICATION_TASK:
        return (
            "You are an independent LeanFlow statement/source verifier. Review the source document, "
            "blueprint, and Lean statement plan for fidelity. Start with PASS or BLOCK, then findings "
            "and correction steps. This is not Lean-kernel proof evidence."
        )
    return (
        "You are an independent LeanFlow autoformalization verifier. Review the handoff state and identify "
        "source-fidelity, blueprint, import, or Lean-readiness blockers. Start with PASS or BLOCK, then findings "
        "and correction steps. BLOCK forbids proof launch until the formalizer fixes the draft."
    )
