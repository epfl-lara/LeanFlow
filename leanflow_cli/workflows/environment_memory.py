"""Persist deterministic campaign knowledge about unavailable tool environments.

The proving campaign deliberately starts fresh model contexts across epochs, but
an environment failure such as a missing Python module is not theorem-local
conversation state.  This module records narrow, machine-observed failure
signatures so a new context does not spend another tool call rediscovering the
same unavailable dependency.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

ENVIRONMENT_FAILURE_CAP = 32
ENVIRONMENT_FAILURES_KEY = "campaign_environment_failures"

_MISSING_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError\s*:\s*)?No module named\s+['\"](?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)['\"]",
    flags=re.IGNORECASE,
)
_PYTHON_INTERPRETER_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<interpreter>(?:[^\s;&|]+/)?python(?:3(?:\.\d+)?)?)(?![A-Za-z0-9_.-])",
    flags=re.IGNORECASE,
)
_PYTHON_IMPORT_RE = re.compile(
    r"(?<![A-Za-z0-9_])import\s+(?P<imports>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:\s+as\s+[A-Za-z_]\w*)?(?:\s*,\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:\s+as\s+[A-Za-z_]\w*)?)*)"
)
_PYTHON_FROM_IMPORT_RE = re.compile(
    r"(?<![A-Za-z0-9_])from\s+(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\b"
)


def _now_iso() -> str:
    """Return a stable UTC timestamp for persisted failure records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _summary_path() -> Path:
    """Return the shared campaign summary path."""
    return workflow_state_root() / "summary.json"


def _module_root(value: str) -> str:
    """Return a safe top-level Python module name."""
    candidate = str(value or "").strip().split(".", 1)[0]
    return candidate if re.fullmatch(r"[A-Za-z_]\w*", candidate) else ""


def python_interpreter(command: str) -> str:
    """Return the Python interpreter token used by a terminal command."""
    match = _PYTHON_INTERPRETER_RE.search(str(command or ""))
    if match is None:
        return ""
    return str(match.group("interpreter") or "").strip()


def imported_python_modules(command: str) -> tuple[str, ...]:
    """Return top-level modules imported by visible Python source in a command."""
    text = str(command or "")
    if not python_interpreter(text):
        return ()
    modules: list[str] = []
    for match in _PYTHON_FROM_IMPORT_RE.finditer(text):
        module = _module_root(match.group("module"))
        if module:
            modules.append(module)
    for match in _PYTHON_IMPORT_RE.finditer(text):
        statement_start = max(text.rfind("\n", 0, match.start()), text.rfind(";", 0, match.start()))
        statement_prefix = text[statement_start + 1 : match.start()]
        if re.search(r"\bfrom\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*$", statement_prefix):
            # The generic ``import`` regex also sees the tail of
            # ``from package import Name``; the package was recorded above.
            continue
        for raw in str(match.group("imports") or "").split(","):
            module = _module_root(raw.strip().split()[0] if raw.strip() else "")
            if module:
                modules.append(module)
    return tuple(dict.fromkeys(modules))


def missing_python_modules(result: str) -> tuple[str, ...]:
    """Extract top-level missing-module names from a terminal result payload."""
    text = str(result or "")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        text = "\n".join(str(payload.get(key, "") or "") for key in ("output", "error", "stderr"))
    modules = [_module_root(match.group("name")) for match in _MISSING_MODULE_RE.finditer(text)]
    return tuple(dict.fromkeys(module for module in modules if module))


def _normalized_entries(raw: Any) -> list[dict[str, Any]]:
    """Return bounded, valid missing-module records from persisted state."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind", "") or "") != "missing_python_module":
            continue
        module = _module_root(str(item.get("module", "") or ""))
        interpreter = str(item.get("interpreter", "") or "").strip()
        if not module or not interpreter:
            continue
        signature = f"missing-python-module:{interpreter}:{module}"
        if signature in seen:
            continue
        seen.add(signature)
        entries.append(
            {
                "signature": signature,
                "kind": "missing_python_module",
                "interpreter": interpreter,
                "module": module,
                "count": max(1, int(item.get("count", 1) or 1)),
                "first_seen_at": str(item.get("first_seen_at", "") or ""),
                "last_seen_at": str(item.get("last_seen_at", "") or ""),
            }
        )
    return entries[-ENVIRONMENT_FAILURE_CAP:]


def hydrate(autonomy_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Load campaign environment failures into process-local autonomy state."""
    summary = read_json_file(_summary_path())
    campaign = dict(summary.get("campaign") or {})
    entries = _normalized_entries(campaign.get("environment_failures"))
    if entries:
        autonomy_state[ENVIRONMENT_FAILURES_KEY] = entries
    else:
        autonomy_state.pop(ENVIRONMENT_FAILURES_KEY, None)
    return entries


def observe_terminal_result(
    autonomy_state: dict[str, Any],
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    result: str,
) -> list[dict[str, Any]]:
    """Persist missing Python modules observed in one terminal tool result.

    Only an exact ``No module named`` diagnostic is authoritative.  Generic
    command failures and model prose never become campaign environment facts.
    """
    if str(function_name or "") != "terminal":
        return []
    command = str(dict(args or {}).get("command", "") or "")
    interpreter = python_interpreter(command)
    modules = missing_python_modules(result)
    if not interpreter or not modules:
        return []
    observed_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> list[dict[str, Any]]:
        campaign = dict(summary.get("campaign") or {})
        entries = _normalized_entries(campaign.get("environment_failures"))
        by_signature = {str(entry["signature"]): dict(entry) for entry in entries}
        for module in modules:
            signature = f"missing-python-module:{interpreter}:{module}"
            previous = dict(by_signature.get(signature) or {})
            by_signature[signature] = {
                "signature": signature,
                "kind": "missing_python_module",
                "interpreter": interpreter,
                "module": module,
                "count": int(previous.get("count", 0) or 0) + 1,
                "first_seen_at": str(previous.get("first_seen_at", "") or observed_at),
                "last_seen_at": observed_at,
            }
        updated = list(by_signature.values())[-ENVIRONMENT_FAILURE_CAP:]
        campaign["environment_failures"] = updated
        campaign["updated_at"] = observed_at
        summary["campaign"] = campaign
        return updated

    updated = list(update_json_file(_summary_path(), mutate))
    autonomy_state[ENVIRONMENT_FAILURES_KEY] = updated
    return [
        entry
        for entry in updated
        if str(entry.get("interpreter", "") or "") == interpreter
        and str(entry.get("module", "") or "") in modules
    ]


def blocked_imports(
    autonomy_state: Mapping[str, Any] | None,
    args: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return known-unavailable modules imported by a proposed terminal call."""
    command = str(dict(args or {}).get("command", "") or "")
    interpreter = python_interpreter(command)
    imports = set(imported_python_modules(command))
    if not interpreter or not imports:
        return ()
    entries = _normalized_entries(dict(autonomy_state or {}).get(ENVIRONMENT_FAILURES_KEY))
    blocked = [
        str(entry.get("module", "") or "")
        for entry in entries
        if str(entry.get("interpreter", "") or "") == interpreter
        and str(entry.get("module", "") or "") in imports
    ]
    return tuple(dict.fromkeys(blocked))


def prompt_block(autonomy_state: Mapping[str, Any] | None) -> str:
    """Render campaign environment failures for fresh prover contexts."""
    entries = _normalized_entries(dict(autonomy_state or {}).get(ENVIRONMENT_FAILURES_KEY))
    if not entries:
        return ""
    lines = [
        "[LEANFLOW CAMPAIGN ENVIRONMENT MEMORY]",
        "The following failures were observed by tools and survive context/epoch rollover:",
    ]
    for entry in entries:
        lines.append(
            f"- `{entry['interpreter']}` cannot import `{entry['module']}` "
            "(`ModuleNotFoundError`); do not retry the unchanged import."
        )
    lines.append(
        "Use the Python standard library, existing Lean tools, or a dependency-free calculation instead."
    )
    return "\n".join(lines)
