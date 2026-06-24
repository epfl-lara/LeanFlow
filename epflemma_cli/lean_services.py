"""Shared Lean workflow services for native EPFLemma workflows and tools."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from epflemma_cli.file_locks import acquire_file_lock as _acquire_file_lock
from epflemma_cli.file_locks import list_file_locks as _list_file_locks

# Phase 5: pure multi-attempt validation / path / comment text helpers (and the MULTI_ATTEMPT_*
# bounds) were extracted to lean_attempt_helpers. Re-export them here so the in-module callers
# (_count_sorries / lean_inspect / lean_multi_attempt / _local_incremental_auto_probe / lean_auto_probe
# and _canonical_tool_file_path) keep resolving them as ``lean_services.<name>`` unchanged.
# lean_attempt_helpers imports only stdlib and does NOT import lean_services / native_runner, so this
# introduces no import cycle.
from epflemma_cli.lean_attempt_helpers import (  # noqa: E402
    MULTI_ATTEMPT_MAX_CANDIDATES,
    MULTI_ATTEMPT_MAX_CHARS,
    MULTI_ATTEMPT_MAX_LINES,
    MULTI_ATTEMPT_MIN_CANDIDATES,
    _multi_attempt_validation_reasons,
    _normalize_multi_attempt_candidates,
    _strip_comments_and_strings,
    _strip_diff_path_prefix,
    _summarize_attempt_diagnostics,
)

# Phase 5: pure auto-prove normalization / parsing helpers (native-backend failure classifiers and
# message extractors, the unsupported-option preflight, probe-success / replacement / diagnostics
# shaping, and the objective->search-depth map) plus the UNSUPPORTED_PROOF_AUTO_OPTIONS constant were
# extracted to lean_automation. Re-export them here so the in-module auto-prove orchestrators
# (_invoke_native_mcp_wrapper / _normalize_native_backend_status / _local_incremental_auto_probe /
# lean_auto_probe / lean_auto_search / lean_auto_try) keep resolving them as ``lean_services.<name>``
# unchanged. lean_automation imports only stdlib and does NOT import lean_services / native_runner, so
# this introduces no import cycle.
from epflemma_cli.lean_automation import (  # noqa: E402
    UNSUPPORTED_PROOF_AUTO_OPTIONS,
    _auto_probe_attempt_succeeded,
    _auto_search_depth_for_objective,
    _automation_probe_replacement,
    _incremental_probe_diagnostics,
    _native_backend_failure_message,
    _native_backend_status_indicates_failure,
    _proof_auto_harness_failure_message,
    _proof_auto_unsupported_option_reason,
)

# Phase 5 (#4 lean backend): a thin LeanBackend façade over the two backend primitives below
# (_invoke_json_tool / _run_command) plus a capability-availability reader. lean_backend owns NO
# backend state and forwards verbatim, resolving _invoke_json_tool / _run_command lazily off this
# module at call time so test monkeypatches on those names still apply. It imports only stdlib at
# load and does NOT import lean_services / native_runner, so this introduces no import cycle. The
# stateful primitives (and their discovery / disable-for-run helpers) stay below; the JSON-tool /
# Lake invocation call sites route through ``_BACKEND`` instead of calling the primitive directly.
from epflemma_cli.lean_backend import LeanBackend  # noqa: E402

# Phase 5: pure path-based declaration indexing / lookup helpers were extracted to
# lean_declarations. Re-export them here (including LEAN_DECLARATION_PREAMBLE_RE) so existing
# callers keep resolving them as ``lean_services.<name>`` unchanged. lean_declarations imports only
# stdlib and does NOT import lean_services / native_runner, so this introduces no import cycle.
from epflemma_cli.lean_declarations import (  # noqa: E402
    LEAN_DECLARATION_PREAMBLE_RE,
    _declaration_index,
    _declaration_text_from_location,
    _find_declaration_entry,
    _find_symbol_line,
    _split_declaration_statement_and_proof,
    _surrounding_declarations,
)

# Phase 5: pure diagnostic / blocker / goal text parsers were extracted to lean_diagnostics.
# Re-export them here so existing importers (lean_tool, native_runner, native_utils, doctor, tests)
# keep resolving them as ``lean_services.<name>`` unchanged. lean_diagnostics imports only stdlib
# and does NOT import lean_services / native_runner, so this introduces no import cycle.
from epflemma_cli.lean_diagnostics import (  # noqa: E402
    ACTIONABLE_DIAGNOSTIC_SEVERITIES,
    _coerce_positive_int,
    _collect_diagnostic_items,
    _diagnostic_line_from_mapping,
    _diagnostic_line_numbers,
    _diagnostic_reason_for_entry,
    _json_diagnostic_values,
    _normalise_diagnostic_item,
    actionable_diagnostic_items,
    actionable_diagnostic_line_numbers,
    classify_blocker_kind,
    diagnostic_items,
    diagnostics_indicate_actionable_failure,
)

# Phase 5: the pure local proof-context fallback assembler (_local_proof_context_payload rebuilds a
# proof-context payload from an on-disk declaration slice, with no MCP backend or run state) was
# extracted to lean_proof_context_local. Re-export it here so the in-module orchestrator
# (lean_proof_context) and tests that call ``lean_services._local_proof_context_payload`` keep
# resolving it unchanged. lean_proof_context_local imports only stdlib plus lean_declarations and
# does NOT import lean_services / native_runner, so this introduces no import cycle.
from epflemma_cli.lean_proof_context_local import (  # noqa: E402
    _local_proof_context_payload,
)

# Phase 5: stateless Lean search-provider helpers (LeanExplore env/key/cache readers, the remote API
# search, and the search-payload normalizers) plus the SEARCH_PROVIDER_LABELS constant were extracted
# to lean_search_providers. Re-export them here so existing importers and the in-module orchestrators
# (lean_search / probe_capabilities) keep resolving them as ``lean_services.<name>`` unchanged. The
# stateful local-service trio (_leanexplore_local_service / _leanexplore_local_search and their globals)
# and _rg_search stay below. lean_search_providers imports only stdlib and does NOT import
# lean_services / native_runner, so this introduces no import cycle.
from epflemma_cli.lean_search_providers import (  # noqa: E402
    _LEANEXPLORE_LOCAL_REQUIRED_ENTRIES,
    SEARCH_PROVIDER_LABELS,
    _decode_nested_result,
    _format_search_payload_item,
    _is_leanexplore_reranker_load_error,
    _leanexplore_api_key,
    _leanexplore_api_search,
    _leanexplore_backend_preference,
    _leanexplore_cache_root,
    _leanexplore_local_cache_path,
    _leanexplore_local_status,
    _leanexplore_local_verbose,
    _model_to_plain_dict,
    _quiet_leanexplore_local_output,
    _search_payload_fragments,
)

# Phase 5: the pure ``sorry``-counting helpers (_count_sorries reads a single .lean file;
# _project_sorry_stats walks a project tree and aggregates the counts) were extracted to
# lean_sorry_stats. Re-export them here so existing callers (lean_inspect and tests that
# monkeypatch ``lean_services._project_sorry_stats``) keep resolving them as
# ``lean_services.<name>`` unchanged. lean_sorry_stats imports only stdlib plus
# lean_attempt_helpers and does NOT import lean_services / native_runner, so this introduces no
# import cycle.
from epflemma_cli.lean_sorry_stats import (  # noqa: E402
    _count_sorries,
    _project_sorry_stats,
)
from epflemma_cli.lean_workflow_specs import get_lean_spec, list_specs
from epflemma_cli.project import (
    ProjectManifestError,
    ProjectNotFoundError,
    discover_epflemma_project,
    find_lean_project_root,
)
from epflemma_cli.workflow_state import append_workflow_outcome, workflow_outcomes_path

STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
LEAN_WORKER_DISPATCH_ENABLED = False
MANAGED_MCP_TOOL_MAP = {
    "diagnostics": ("mcp_lean_lsp_lean_diagnostic_messages",),
    "goals": ("mcp_lean_lsp_lean_goal", "mcp_lean_lsp_lean_term_goal"),
    "code_actions": ("mcp_lean_lsp_lean_code_actions",),
    "multi_attempt": ("mcp_lean_lsp_lean_multi_attempt",),
    "run_code": ("mcp_lean_lsp_lean_run_code",),
    "state_search": ("mcp_lean_lsp_lean_state_search",),
    "hammer_premise": ("mcp_lean_lsp_lean_hammer_premise",),
    "hover_info": ("mcp_lean_lsp_lean_hover_info",),
    "file_outline": ("mcp_lean_lsp_lean_file_outline",),
    "declaration_file": ("mcp_lean_lsp_lean_declaration_file",),
    "profile_proof": ("mcp_lean_lsp_lean_profile_proof",),
    "local_search": ("mcp_lean_lsp_lean_local_search",),
    "leanfinder": ("mcp_lean_lsp_lean_leanfinder",),
    "leansearch": ("mcp_lean_lsp_lean_leansearch",),
    "loogle": ("mcp_lean_lsp_lean_loogle",),
    "leanexplore": (
        "mcp_lean_explore_search_summary",
        "mcp_lean_explore_search",
        "mcp_leanexplore_search_summary",
        "mcp_leanexplore_search",
    ),
    "proof_context": ("mcp_lean_proof_auto_get_proof_context",),
    "auto_search": ("mcp_lean_proof_auto_search_automated_proof",),
}
INTERNAL_MANAGED_MCP_TOOL_MAP = {
    "scan_theorem": ("mcp_lean_proof_auto_scan_theorem",),
}
MCP_CAPABILITY_DISABLED_LABELS = {
    "diagnostics": "lean diagnostics MCP",
    "goals": "lean goals MCP",
    "code_actions": "lean code actions MCP",
    "multi_attempt": "lean multi-attempt MCP",
    "run_code": "lean run-code MCP",
    "state_search": "lean state-search MCP",
    "hammer_premise": "lean hammer-premise MCP",
    "hover_info": "lean hover-info MCP",
    "file_outline": "lean file-outline MCP",
    "declaration_file": "lean declaration-file MCP",
    "profile_proof": "lean profile-proof MCP",
    "local_search": "lean local search MCP",
    "leanfinder": "lean leanfinder MCP",
    "leansearch": "lean leansearch MCP",
    "loogle": "lean loogle MCP",
    "leanexplore": "lean LeanExplore MCP",
    "proof_context": "lean proof context MCP",
    "auto_search": "lean automation search MCP",
}
_DISABLED_MCP_TOOLS_BY_RUN: dict[str, set[str]] = {}
LOCAL_INCREMENTAL_AUTO_PROBE_MIN_TIMEOUT_S = 60

# Phase 5 (#4 lean backend): shared stateless façade over the backend primitives. The wrapper
# forwards verbatim and resolves _invoke_json_tool / _run_command lazily off this module, so this
# stays behaviour-identical even when tests monkeypatch those names on lean_services.
_BACKEND = LeanBackend()


def recent_empty_search_streak(*, workflow_command: str, limit: int = 6) -> int:
    path = workflow_outcomes_path()
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    streak = 0
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("workflow_command", "") or "") != workflow_command:
            continue
        if str(payload.get("kind", "") or "") != "lean-search":
            if streak:
                break
            continue
        result_payload = payload.get("payload", {})
        if not isinstance(result_payload, Mapping):
            break
        results = result_payload.get("results", [])
        if isinstance(results, list) and not results:
            streak += 1
            if streak >= limit:
                break
            continue
        break
    return streak


def _workflow_run_key(cwd: str | os.PathLike[str] | None = None) -> str:
    run_id = str(os.getenv("EPFLEMMA_WORKFLOW_RUN_ID", "") or "").strip()
    if run_id:
        return run_id
    workflow_command = str(
        os.getenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "")
        or os.getenv("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", "")
        or ""
    ).strip()
    if workflow_command:
        return f"workflow:{workflow_command}"
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    return f"pid:{os.getpid()}:{base}"


def _managed_mcp_tool_names() -> set[str]:
    names: set[str] = set()
    for candidates in MANAGED_MCP_TOOL_MAP.values():
        names.update(candidates)
    for candidates in INTERNAL_MANAGED_MCP_TOOL_MAP.values():
        names.update(candidates)
    return names


def _disabled_mcp_tools_for_run(cwd: str | os.PathLike[str] | None = None) -> set[str]:
    return set(_DISABLED_MCP_TOOLS_BY_RUN.get(_workflow_run_key(cwd), set()))


def _disable_mcp_tool_for_run(tool_name: str, *, cwd: str | os.PathLike[str] | None = None) -> None:
    normalized = str(tool_name or "").strip()
    if not normalized:
        return
    run_key = _workflow_run_key(cwd)
    disabled = _DISABLED_MCP_TOOLS_BY_RUN.setdefault(run_key, set())
    disabled.add(normalized)


def _apply_disabled_mcp_tools(
    mcp_tools: dict[str, str],
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> list[str]:
    disabled = _disabled_mcp_tools_for_run(cwd)
    if not disabled:
        return []
    reasons: list[str] = []
    for capability, tool_name in list(mcp_tools.items()):
        if tool_name and tool_name in disabled:
            mcp_tools[capability] = ""
            label = MCP_CAPABILITY_DISABLED_LABELS.get(capability, f"{capability} MCP")
            reasons.append(f"{label} disabled for current run after previous backend failure")
    return reasons


def _canonical_tool_file_path(
    file_path: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> str:
    normalized = _strip_diff_path_prefix(file_path)
    if not normalized:
        return ""

    root = Path(cwd).expanduser().resolve() if cwd else None
    if root is not None and not root.is_dir():
        root = root.parent
    if root is None:
        project_root, _ = _project_root(cwd)
        root = Path(project_root).expanduser().resolve() if project_root else None
    configured_active = str(
        os.getenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "")
        or os.getenv("OPENGAUSS_NATIVE_ACTIVE_FILE", "")
        or ""
    ).strip()

    def _resolve_candidate(candidate: str) -> Path | None:
        raw = _strip_diff_path_prefix(candidate)
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        if root:
            return (root / path).resolve()
        return path.resolve()

    primary = _resolve_candidate(normalized)
    if primary and primary.is_file():
        return str(primary)

    active_candidate = _resolve_candidate(configured_active)
    if active_candidate and active_candidate.is_file():
        requested_name = Path(normalized).name
        if not primary or not requested_name or requested_name == active_candidate.name or normalized == configured_active:
            return str(active_candidate)

    return str(primary or normalized)


def _discover_raw_mcp_tool_names() -> tuple[list[str], set[str]]:
    try:
        from tools.mcp_tool import discover_mcp_tools

        discover_mcp_tools()
    except Exception:
        pass

    try:
        from tools.registry import registry

        tool_names = registry.get_all_tool_names()
    except Exception:
        tool_names = []
    raw_tool_names = [name for name in tool_names if str(name).startswith("mcp_")]
    return raw_tool_names, set(raw_tool_names)


def _discover_internal_managed_mcp_tool(capability: str) -> str:
    _, raw_tool_set = _discover_raw_mcp_tool_names()
    for candidate in INTERNAL_MANAGED_MCP_TOOL_MAP.get(capability, ()):
        if candidate in raw_tool_set:
            return candidate
    return ""


def _disable_proof_auto_backend_for_run(*, cwd: str | os.PathLike[str] | None = None) -> None:
    for tool_name in _managed_mcp_tool_names():
        if tool_name.startswith("mcp_lean_proof_auto_"):
            _disable_mcp_tool_for_run(tool_name, cwd=cwd)


@dataclass(frozen=True)
class LeanCapabilityReport:
    cwd: str
    project_root: str
    project_valid: bool
    project_error: str
    binaries: dict[str, bool]
    mcp_tools: dict[str, str]
    search_providers: list[str]
    helper_tools: dict[str, bool]
    workers: list[str]
    degraded_reasons: list[str]
    mcp_server_roles: dict[str, str] = field(default_factory=dict)
    managed_mcp_servers: dict[str, bool] = field(default_factory=dict)
    power_modes: dict[str, Any] = field(default_factory=dict)
    incremental: dict[str, Any] = field(default_factory=dict)
    remote_search_policy: str = "public-fallbacks-enabled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanSorryFinding:
    file: str
    line: int
    declaration: str
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanInspection:
    target: str
    project_root: str
    diagnostics: str
    goals: str
    sorry_count: int | None
    project_sorry_count: int | None
    blocker_kind: str
    queue_items: list[dict[str, Any]] = field(default_factory=list)
    capability_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanVerificationResult:
    ok: bool
    mode: str
    command: str
    target: str
    output: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanSearchResult:
    query: str
    mode: str
    attempted_providers: list[str]
    results: list[dict[str, Any]]
    degraded_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanAxiomReport:
    target: str
    file_path: str
    ok: bool
    axioms: list[str]
    custom_axioms: list[str]
    classical: bool
    choice: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowRouteDecision:
    workflow_kind: str
    skill_name: str
    route_action: str
    blocker_kind: str
    recommended_worker: str
    search_exhausted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerRequest:
    worker: str
    goal: str
    context: str
    file_path: str = ""
    line: int | None = None
    use_file_lock: bool = True
    allow_delegation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerResult:
    worker: str
    mode: str
    dispatched: bool
    summary: str
    result: Any = None
    lock: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _project_root(cwd: str | os.PathLike[str] | None = None) -> tuple[Path | None, str]:
    explicit = str(
        os.getenv(
            "EPFLEMMA_PROJECT_ROOT",
            os.getenv("OPENGAUSS_PROJECT_ROOT", os.getenv("GAUSS_PROJECT_ROOT", "")),
        )
        or ""
    ).strip()
    base = Path(cwd or explicit or os.getcwd()).expanduser().resolve()
    if cwd is None and explicit:
        lean_root = find_lean_project_root(base)
        if lean_root is not None:
            return lean_root, ""
    try:
        project = discover_epflemma_project(base)
        return project.root, ""
    except (ProjectNotFoundError, ProjectManifestError) as exc:
        lean_root = find_lean_project_root(base)
        if lean_root is not None:
            return lean_root, str(exc)
        return None, str(exc)


def _run_command(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _tool_parameter_names(tool_name: str) -> set[str]:
    try:
        from tools.registry import registry

        entry = registry._tools.get(tool_name)  # type: ignore[attr-defined]
    except Exception:
        entry = None
    if entry is None:
        return set()
    schema = getattr(entry, "schema", {}) or {}
    parameters = schema.get("parameters", {}) if isinstance(schema, Mapping) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    if isinstance(properties, Mapping):
        return {str(key) for key in properties.keys()}
    return set()


def _invoke_json_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from model_tools import handle_function_call

    accepted = _tool_parameter_names(tool_name)
    if accepted:
        filtered = {
            key: value
            for key, value in arguments.items()
            if key in accepted and value not in (None, "")
        }
    else:
        filtered = {key: value for key, value in arguments.items() if value not in (None, "")}
    raw = handle_function_call(tool_name, filtered)
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"raw": raw}


# Stateful LeanExplore local-service singleton: kept in lean_services (alongside _leanexplore_local_service
# / _leanexplore_local_search below) so the ``global`` rebinds and the tests'
# ``monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_*", ...)`` keep resolving in one namespace. The
# stateless readers / normalizers and SEARCH_PROVIDER_LABELS / _LEANEXPLORE_LOCAL_REQUIRED_ENTRIES live in
# lean_search_providers and are re-exported above.
_LEANEXPLORE_LOCAL_SERVICE: Any | None = None
_LEANEXPLORE_LOCAL_SERVICE_LOCK = threading.Lock()
_LEANEXPLORE_LOCAL_RERANK_DISABLED = False


def _leanexplore_local_service() -> Any:
    global _LEANEXPLORE_LOCAL_SERVICE
    if _LEANEXPLORE_LOCAL_SERVICE is not None:
        return _LEANEXPLORE_LOCAL_SERVICE
    from lean_explore.search import Service

    _LEANEXPLORE_LOCAL_SERVICE = Service()
    return _LEANEXPLORE_LOCAL_SERVICE


def _leanexplore_local_search(query: str, *, limit: int = 10) -> tuple[list[dict[str, Any]], str]:
    global _LEANEXPLORE_LOCAL_RERANK_DISABLED
    status = _leanexplore_local_status()
    if not status["package_available"]:
        return [], "LeanExplore local backend unavailable; install `lean-explore[local]`"
    if not status["data_ready"]:
        return [], "LeanExplore local data unavailable; run `lean-explore data fetch`"
    try:
        import asyncio

        async def _run_search(rerank_top: int | None) -> Any:
            service = _leanexplore_local_service()
            return await service.search(
                query=query,
                limit=max(1, int(limit or 10)),
                rerank_top=rerank_top,
            )

        with _LEANEXPLORE_LOCAL_SERVICE_LOCK, _quiet_leanexplore_local_output():
            try:
                response = asyncio.run(_run_search(0 if _LEANEXPLORE_LOCAL_RERANK_DISABLED else 50))
            except Exception as exc:
                if not _is_leanexplore_reranker_load_error(exc):
                    raise
                _LEANEXPLORE_LOCAL_RERANK_DISABLED = True
                response = asyncio.run(_run_search(0))
    except Exception as exc:
        return [], f"LeanExplore local search failed: {exc}"
    raw_results = getattr(response, "results", [])
    if not isinstance(raw_results, list):
        return [], "LeanExplore local backend returned results in an unexpected format"
    results: list[dict[str, Any]] = []
    for item in raw_results[:limit]:
        payload = _model_to_plain_dict(item)
        entry = {
            "provider": SEARCH_PROVIDER_LABELS["leanexplore_local"],
            "match": _format_search_payload_item(payload)[:400],
        }
        for key in ("id", "name", "module", "source_link"):
            value = payload.get(key)
            if value not in (None, ""):
                entry[key] = value
        results.append(entry)
    return results, ""


def _discover_lean_mcp_tools() -> dict[str, str]:
    raw_tool_names, raw_tool_set = _discover_raw_mcp_tool_names()

    discovered = {
        "diagnostics": "",
        "goals": "",
        "code_actions": "",
        "multi_attempt": "",
        "run_code": "",
        "state_search": "",
        "hammer_premise": "",
        "hover_info": "",
        "file_outline": "",
        "declaration_file": "",
        "profile_proof": "",
        "local_search": "",
        "leanfinder": "",
        "leansearch": "",
        "loogle": "",
        "leanexplore": "",
        "proof_context": "",
        "auto_search": "",
    }
    for capability, candidates in MANAGED_MCP_TOOL_MAP.items():
        for candidate in candidates:
            if candidate in raw_tool_set:
                discovered[capability] = candidate
                break

    for tool_name in raw_tool_names:
        lowered = tool_name.lower()
        if "lean" not in lowered:
            continue
        if not discovered["diagnostics"] and any(token in lowered for token in ("diagnostic", "message")):
            discovered["diagnostics"] = tool_name
        if (
            not discovered["goals"]
            and "proof_auto" not in lowered
            and ("_goal" in lowered or "term_goal" in lowered)
        ):
            discovered["goals"] = tool_name
        if not discovered["code_actions"] and "code_action" in lowered:
            discovered["code_actions"] = tool_name
        if not discovered["multi_attempt"] and "multi_attempt" in lowered:
            discovered["multi_attempt"] = tool_name
        if not discovered["run_code"] and "run_code" in lowered:
            discovered["run_code"] = tool_name
        if not discovered["state_search"] and "state_search" in lowered:
            discovered["state_search"] = tool_name
        if not discovered["hammer_premise"] and "hammer_premise" in lowered:
            discovered["hammer_premise"] = tool_name
        if not discovered["hover_info"] and "hover_info" in lowered:
            discovered["hover_info"] = tool_name
        if not discovered["file_outline"] and "file_outline" in lowered:
            discovered["file_outline"] = tool_name
        if not discovered["declaration_file"] and "declaration_file" in lowered:
            discovered["declaration_file"] = tool_name
        if not discovered["profile_proof"] and "profile_proof" in lowered:
            discovered["profile_proof"] = tool_name
        if not discovered["local_search"] and "local_search" in lowered:
            discovered["local_search"] = tool_name
        if not discovered["leanfinder"] and "leanfinder" in lowered:
            discovered["leanfinder"] = tool_name
        if not discovered["leansearch"] and "leansearch" in lowered:
            discovered["leansearch"] = tool_name
        if not discovered["loogle"] and "loogle" in lowered:
            discovered["loogle"] = tool_name
        if not discovered["leanexplore"] and "lean" in lowered and "explore" in lowered and (
            lowered.endswith("search_summary") or lowered.endswith("search")
        ):
            discovered["leanexplore"] = tool_name
        if "proof_auto" in lowered:
            if not discovered["proof_context"] and "get_proof_context" in lowered:
                discovered["proof_context"] = tool_name
            if not discovered["auto_search"] and "search_automated_proof" in lowered:
                discovered["auto_search"] = tool_name
    return discovered


def _helper_tools() -> dict[str, bool]:
    return {
        "sorry_analyzer": True,
        "axiom_checker": True,
        "error_parser": True,
        "search_fallback": True,
        "solver_cascade": True,
        "usage_search": True,
        "instance_search": True,
        "golf_candidates": True,
    }


def probe_capabilities(cwd: str | os.PathLike[str] | None = None) -> LeanCapabilityReport:
    explicit = str(
        os.getenv(
            "EPFLEMMA_PROJECT_ROOT",
            os.getenv("OPENGAUSS_PROJECT_ROOT", os.getenv("GAUSS_PROJECT_ROOT", "")),
        )
        or ""
    ).strip()
    base = Path(cwd or explicit or os.getcwd()).expanduser().resolve()
    project_root, project_error = _project_root(base)
    binaries = {name: bool(shutil.which(name)) for name in ("lean", "lake", "elan", "git", "rg")}
    mcp_tools = _discover_lean_mcp_tools()
    # These proof-auto surfaces have repeatedly produced low-signal harness
    # failures in managed workflows. Keep the lower-level service functions for
    # compatibility/tests, but do not advertise them through capabilities.
    mcp_tools.pop("auto_probe", None)
    mcp_tools.pop("auto_try", None)
    search_providers: list[str] = []
    leanexplore_preference = _leanexplore_backend_preference()
    leanexplore_local = _leanexplore_local_status()
    if leanexplore_preference not in {"api", "off", "disabled"} and leanexplore_local["available"]:
        search_providers.append(SEARCH_PROVIDER_LABELS["leanexplore_local"])
    if leanexplore_preference not in {"local", "off", "disabled"} and _leanexplore_api_key():
        search_providers.append(SEARCH_PROVIDER_LABELS["leanexplore_api"])
    for key in ("leanexplore", "leanfinder", "local_search", "leansearch", "loogle"):
        if mcp_tools.get(key):
            search_providers.append(SEARCH_PROVIDER_LABELS[key])
    if binaries.get("rg"):
        search_providers.append(SEARCH_PROVIDER_LABELS["project_rg"])
        if project_root and (project_root / ".lake" / "packages" / "mathlib").is_dir():
            search_providers.append(SEARCH_PROVIDER_LABELS["mathlib_rg"])
    try:
        from tools.mcp_tool import get_mcp_status

        mcp_status = list(get_mcp_status())
    except Exception:
        mcp_status = []
    mcp_server_roles = {
        str(entry.get("name", "") or ""): str(entry.get("role", "") or "")
        for entry in mcp_status
        if str(entry.get("name", "") or "").strip()
    }
    managed_mcp_servers = {
        str(entry.get("name", "") or ""): bool(entry.get("healthy", False) or entry.get("connected", False))
        for entry in mcp_status
        if entry.get("managed") and str(entry.get("name", "") or "").strip()
    }
    degraded: list[str] = _apply_disabled_mcp_tools(mcp_tools, cwd=base)
    if not binaries.get("lean"):
        degraded.append("lean binary unavailable")
    if not binaries.get("lake"):
        degraded.append("lake binary unavailable")
    if project_root is None:
        degraded.append("lean project not detected")
    if not mcp_tools.get("diagnostics"):
        degraded.append("lean diagnostics MCP unavailable")
    if not mcp_tools.get("proof_context"):
        degraded.append("lean proof context MCP unavailable")
    if not mcp_tools.get("auto_search"):
        degraded.append("lean automation MCP unavailable")
    if not search_providers:
        degraded.append("no search providers available")
    try:
        from epflemma_cli.mcp_bootstrap import REMOTE_SEARCH_POLICY, managed_mcp_power_status

        power_modes = managed_mcp_power_status(project_root=project_root)
        remote_search_policy = REMOTE_SEARCH_POLICY
    except Exception:
        power_modes = {}
        remote_search_policy = "public-fallbacks-enabled"
    power_modes["leanexplore_backend"] = leanexplore_preference
    power_modes["leanexplore_local_available"] = bool(leanexplore_local["available"])
    power_modes["leanexplore_local_package_available"] = bool(leanexplore_local["package_available"])
    power_modes["leanexplore_local_data_ready"] = bool(leanexplore_local["data_ready"])
    power_modes["leanexplore_local_cache_path"] = str(leanexplore_local["cache_path"])
    power_modes["leanexplore_api_configured"] = bool(_leanexplore_api_key())
    power_modes["leanexplore_cli_installed"] = bool(shutil.which("lean-explore"))
    if power_modes:
        loogle_status = str(power_modes.get("loogle_local_status", "") or "")
        if power_modes.get("loogle_local_configured") and loogle_status == "incompatible":
            degraded.append(
                "local Loogle disabled for this project because its managed Lean toolchain differs "
                "from the project; public remote Loogle fallback remains enabled"
            )
        elif power_modes.get("loogle_local_configured") and not power_modes.get("loogle_local_available"):
            degraded.append("local Loogle configured but unsupported on this platform; public remote Loogle fallback remains enabled")
        elif power_modes.get("loogle_local_configured") and not power_modes.get("loogle_local_ready"):
            degraded.append("local Loogle configured but cache is not warmed yet; first local query may build it or fall back remotely")
        if project_root and power_modes.get("repl_configured") and not power_modes.get("repl_available"):
            degraded.append("Lean REPL acceleration configured but repl binary is unavailable; run `epflemma project init` to build it")
    try:
        from epflemma_cli.lean_incremental import lean_incremental_capabilities

        incremental = lean_incremental_capabilities(base)
    except Exception as exc:
        incremental = {
            "available": False,
            "degraded_reasons": [f"LeanInteract incremental verifier unavailable: {exc}"],
        }
    if not bool(incremental.get("available", False)):
        for reason in list(incremental.get("degraded_reasons", []) or []):
            if reason and reason not in degraded:
                degraded.append(str(reason))
    return LeanCapabilityReport(
        cwd=str(base),
        project_root=str(project_root or ""),
        project_valid=project_root is not None and not project_error,
        project_error=project_error,
        binaries=binaries,
        mcp_tools=mcp_tools,
        search_providers=search_providers,
        helper_tools=_helper_tools(),
        workers=[record.spec_id for record in list_specs("worker")] if LEAN_WORKER_DISPATCH_ENABLED else [],
        degraded_reasons=degraded,
        mcp_server_roles=mcp_server_roles,
        managed_mcp_servers=managed_mcp_servers,
        power_modes=power_modes,
        incremental=incremental,
        remote_search_policy=remote_search_policy,
    )


def _scan_theorem_by_range(
    file_path: Path,
    *,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    tool_name = _discover_internal_managed_mcp_tool("scan_theorem")
    if not tool_name:
        return {}
    raw = _BACKEND.invoke_tool(
        tool_name,
        {
            "file": str(file_path),
            "target": {"range": {"start_line": int(start_line), "end_line": int(end_line)}},
        },
    )
    if raw.get("error"):
        return {"error": str(raw.get("error", "") or "")}
    parsed = _decode_nested_result(raw)
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {}


def _diagnostics_text(file_path: Path, project_root: Path | None, mcp_tools: Mapping[str, str]) -> str:
    diagnostics_tool = str(mcp_tools.get("diagnostics", "") or "")
    if diagnostics_tool:
        payload = _BACKEND.invoke_tool(
            diagnostics_tool,
            {"file_path": str(file_path), "path": str(file_path)},
        )
        fragments = [str(value).strip() for value in payload.values() if isinstance(value, str) and value.strip()]
        if fragments:
            return "\n".join(fragments[:8])
    if project_root is None:
        return "Lean project unavailable."
    try:
        relative = str(file_path.resolve().relative_to(project_root.resolve()))
    except Exception:
        relative = str(file_path)
    _, output = _BACKEND.run_command(["lake", "env", "lean", relative], cwd=project_root)
    return output or "no diagnostics available"


def _goals_text(
    file_path: Path,
    project_root: Path | None,
    mcp_tools: Mapping[str, str],
    *,
    line: int | None = None,
    symbol: str | None = None,
) -> str:
    goals_tool = str(mcp_tools.get("goals", "") or "")
    if goals_tool:
        payload = _BACKEND.invoke_tool(
            goals_tool,
            {
                "file_path": str(file_path),
                "path": str(file_path),
                "line": line or _find_symbol_line(file_path, symbol) or 1,
            },
        )
        fragments = [str(value).strip() for value in payload.values() if isinstance(value, str) and value.strip()]
        if fragments:
            return "\n".join(fragments[:8])
    return "Lean goals unavailable."


def lean_sorries(scope: str = "project", target: str = "", cwd: str | os.PathLike[str] | None = None) -> list[LeanSorryFinding]:
    project_root, _ = _project_root(cwd)
    if scope == "file" and target:
        paths = [Path(target).expanduser().resolve()]
    elif project_root:
        paths = [
            path
            for path in project_root.rglob("*.lean")
            if not any(part in {".git", ".lake", ".epflemma", "build"} for part in path.parts)
        ]
    else:
        paths = []
    findings: list[LeanSorryFinding] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        decl_entries = _declaration_index(path)
        for line_number, line in enumerate(lines, start=1):
            if "sorry" not in line or re.search(r"--.*\bsorry\b", line):
                continue
            declaration = ""
            for entry in decl_entries:
                if int(entry["line"]) <= line_number <= int(entry["end_line"]):
                    declaration = str(entry["name"])
                    break
            findings.append(
                LeanSorryFinding(
                    file=str(path),
                    line=line_number,
                    declaration=declaration,
                    preview=line.strip(),
                )
            )
    return findings


def lean_inspect(
    target: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    line: int | None = None,
    symbol: str | None = None,
) -> LeanInspection:
    report = probe_capabilities(cwd)
    file_path = Path(target).expanduser().resolve()
    project_root = Path(report.project_root).resolve() if report.project_root else None
    diagnostics = _diagnostics_text(file_path, project_root, report.mcp_tools)
    goals = _goals_text(file_path, project_root, report.mcp_tools, line=line, symbol=symbol)
    sorry_count = _count_sorries(file_path)
    project_sorry_count, _ = _project_sorry_stats(project_root)
    queue_items: list[dict[str, Any]] = []
    diagnostic_lines: list[int] = []
    for diagnostic in diagnostic_items(diagnostics):
        if str(diagnostic.get("severity", "") or "").strip().lower() != "error":
            continue
        line = diagnostic.get("line")
        if isinstance(line, int) and line > 0 and line not in diagnostic_lines:
            diagnostic_lines.append(line)
    for entry in _declaration_index(file_path):
        reasons: list[str] = []
        text = str(entry.get("text", "") or "")
        if re.search(r"\bsorry\b", _strip_comments_and_strings(text)):
            reasons.append("contains sorry")
        diagnostic_reason = _diagnostic_reason_for_entry(entry, diagnostic_lines)
        if diagnostic_reason:
            reasons.append(diagnostic_reason)
        if reasons:
            queue_items.append(
                {
                    "label": entry["name"],
                    "kind": entry["kind"],
                    "line": entry["line"],
                    "end_line": entry["end_line"],
                    "reasons": reasons,
                    "blocker_signature": hashlib.sha1(
                        f"{file_path}:{entry['line']}:{','.join(reasons)}".encode("utf-8")
                    ).hexdigest()[:12],
                    "search_hints": [entry["name"], entry["kind"]],
                    "verification_gate": f"lake env lean {file_path.name}",
                }
            )
    inspection = LeanInspection(
        target=str(file_path),
        project_root=str(project_root or ""),
        diagnostics=diagnostics,
        goals=goals,
        sorry_count=sorry_count,
        project_sorry_count=project_sorry_count,
        blocker_kind=classify_blocker_kind("\n".join((diagnostics, goals))),
        queue_items=queue_items,
        capability_report=report.to_dict(),
    )
    append_workflow_outcome("lean-inspect", inspection.to_dict())
    return inspection


def _module_name_for_file(project_root: Path, file_path: Path) -> str:
    relative = file_path.resolve().relative_to(project_root.resolve())
    return ".".join(relative.with_suffix("").parts)


def lean_verify(
    target: str = "",
    *,
    cwd: str | os.PathLike[str] | None = None,
    mode: str = "project",
) -> LeanVerificationResult:
    project_root, _ = _project_root(cwd)
    root = Path(project_root) if project_root else None
    normalized_mode = str(mode or "project").strip().lower()
    target_path = Path(target).expanduser().resolve() if target else None
    if normalized_mode == "file_exact" and root and target_path:
        try:
            relative = str(target_path.relative_to(root))
        except Exception:
            relative = str(target_path)
        command = ["lake", "env", "lean", relative]
    elif normalized_mode == "module" and root and target_path:
        command = ["lake", "build", _module_name_for_file(root, target_path)]
    else:
        normalized_mode = "project"
        command = ["lake", "build"]
    code, output = _BACKEND.run_command(command, cwd=root)
    result = LeanVerificationResult(
        ok=code == 0,
        mode=normalized_mode,
        command=" ".join(command),
        target=str(target_path or (root or "")),
        output=output,
    )
    append_workflow_outcome("lean-verify", result.to_dict())
    return result


def _rg_search(root: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    if not shutil.which("rg") or not root.is_dir():
        return []
    code, output = _BACKEND.run_command(
        ["rg", "-n", "-m", str(limit), "--color", "never", query, str(root)],
        cwd=root,
    )
    if code not in {0, 1}:
        return []
    results: list[dict[str, Any]] = []
    for line in output.splitlines()[:limit]:
        path_text, _, rest = line.partition(":")
        line_no, _, preview = rest.partition(":")
        results.append(
            {
                "file": path_text,
                "line": int(line_no) if line_no.isdigit() else None,
                "preview": preview.strip(),
            }
        )
    return results


def lean_search(
    query: str,
    *,
    mode: str = "auto",
    cwd: str | os.PathLike[str] | None = None,
    limit: int = 10,
    file_path: str = "",
) -> LeanSearchResult:
    report = probe_capabilities(cwd)
    attempted: list[str] = []
    results: list[dict[str, Any]] = []
    degraded = list(report.degraded_reasons)

    mcp_order = []
    normalized_mode = str(mode or "auto").strip().lower()
    leanexplore_preference = _leanexplore_backend_preference()
    leanexplore_local_available = SEARCH_PROVIDER_LABELS["leanexplore_local"] in report.search_providers
    leanexplore_api_available = SEARCH_PROVIDER_LABELS["leanexplore_api"] in report.search_providers
    semantic_provider_keys = ("leanexplore", "leanfinder", "leansearch", "loogle")
    semantic_provider_labels = [
        SEARCH_PROVIDER_LABELS[key]
        for key in semantic_provider_keys
        if report.mcp_tools.get(key)
    ]
    if leanexplore_local_available:
        semantic_provider_labels.insert(0, SEARCH_PROVIDER_LABELS["leanexplore_local"])
    if leanexplore_api_available:
        semantic_provider_labels.insert(
            0 if not leanexplore_local_available else 1,
            SEARCH_PROVIDER_LABELS["leanexplore_api"],
        )
    def _append_provider(provider_key: str, tool_name: str = "") -> None:
        if any(existing_key == provider_key for existing_key, _ in mcp_order):
            return
        mcp_order.append((provider_key, tool_name))

    def _append_leanexplore_semantic_fallbacks(*, allow_remote_api: bool) -> None:
        if leanexplore_local_available:
            _append_provider("leanexplore_local")
        if allow_remote_api and leanexplore_api_available and leanexplore_preference != "local":
            _append_provider("leanexplore_api")
        if _BACKEND.is_available(report, "leanexplore"):
            _append_provider("leanexplore", report.mcp_tools["leanexplore"])

    if normalized_mode in {"auto", "local"} and _BACKEND.is_available(report, "local_search"):
        _append_provider("local_search", report.mcp_tools["local_search"])
    if normalized_mode in {"auto", "semantic", "natural-language", "natural"}:
        _append_leanexplore_semantic_fallbacks(allow_remote_api=True)
    if normalized_mode == "local":
        _append_leanexplore_semantic_fallbacks(allow_remote_api=False)
    if normalized_mode in {"auto", "semantic"} and _BACKEND.is_available(report, "leanfinder"):
        _append_provider("leanfinder", report.mcp_tools["leanfinder"])
    if normalized_mode in {"auto", "natural-language", "natural"} and _BACKEND.is_available(report, "leansearch"):
        _append_provider("leansearch", report.mcp_tools["leansearch"])
    if normalized_mode in {"auto", "type-pattern", "type"} and _BACKEND.is_available(report, "loogle"):
        _append_provider("loogle", report.mcp_tools["loogle"])
    if normalized_mode in {"type-pattern", "type"}:
        _append_leanexplore_semantic_fallbacks(allow_remote_api=True)
    for provider_key, tool_name in mcp_order:
        if results:
            break
        attempted.append(SEARCH_PROVIDER_LABELS[provider_key])
        if provider_key == "leanexplore_local":
            local_results, local_error = _leanexplore_local_search(query, limit=limit)
            if local_results:
                results.extend(local_results[:limit])
                break
            if local_error:
                degraded.append(local_error)
            continue
        if provider_key == "leanexplore_api":
            api_results, api_error = _leanexplore_api_search(query, limit=limit)
            if api_results:
                results.extend(api_results[:limit])
                break
            if api_error:
                degraded.append(api_error)
            continue
        payload = _BACKEND.invoke_tool(
            tool_name,
            {
                "query": query,
                "q": query,
                "path": file_path,
                "file_path": file_path,
                "limit": limit,
            },
        )
        if payload.get("error"):
            continue
        text_fragments = _search_payload_fragments(payload, limit=limit)
        if text_fragments:
            results.extend(
                {
                    "provider": SEARCH_PROVIDER_LABELS[provider_key],
                    "match": fragment[:400],
                }
                for fragment in text_fragments[:limit]
            )
            break

    root = Path(report.project_root) if report.project_root else None
    if not results and root:
        attempted.append(SEARCH_PROVIDER_LABELS["project_rg"])
        for match in _rg_search(root, query, limit=limit):
            results.append({"provider": SEARCH_PROVIDER_LABELS["project_rg"], **match})
    mathlib_root = root / ".lake" / "packages" / "mathlib" if root else None
    if not results and mathlib_root and mathlib_root.is_dir():
        attempted.append(SEARCH_PROVIDER_LABELS["mathlib_rg"])
        for match in _rg_search(mathlib_root, query, limit=limit):
            results.append({"provider": SEARCH_PROVIDER_LABELS["mathlib_rg"], **match})

    if any(provider in attempted for provider in (SEARCH_PROVIDER_LABELS["project_rg"], SEARCH_PROVIDER_LABELS["mathlib_rg"])):
        if not semantic_provider_labels:
            degraded.append("semantic providers unavailable")
        elif (
            normalized_mode in {"auto", "semantic", "natural-language", "natural", "type-pattern", "type"}
            and not any(provider in attempted for provider in semantic_provider_labels)
        ):
            degraded.append("semantic providers skipped; falling back to rg")
    if not results:
        degraded.append("search returned no results")
        workflow_command = str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "") or os.getenv("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", ""))
        if workflow_command:
            empty_streak = recent_empty_search_streak(workflow_command=workflow_command)
            if empty_streak >= 2:
                degraded.append("repeated empty search loop detected; stop searching and change tactic")
    result = LeanSearchResult(
        query=query,
        mode=normalized_mode,
        attempted_providers=attempted,
        results=results[:limit],
        degraded_reasons=list(dict.fromkeys(degraded)),
    )
    append_workflow_outcome("lean-search", result.to_dict())
    return result


def _wrapper_unavailable_result(
    *,
    report: LeanCapabilityReport,
    tool_name: str,
    unavailable_reason: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "success": False,
        "backend_tool": tool_name,
        "degraded_reasons": list(dict.fromkeys([*report.degraded_reasons, unavailable_reason])),
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _invoke_native_mcp_wrapper(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    report: LeanCapabilityReport,
    unavailable_reason: str,
    outcome_kind: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not tool_name:
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name="",
            unavailable_reason=unavailable_reason,
            extra=extra,
        )
        append_workflow_outcome(outcome_kind, payload)
        return payload
    raw = _BACKEND.invoke_tool(tool_name, arguments)
    if raw.get("error"):
        _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name=tool_name,
            unavailable_reason=str(raw.get("error", unavailable_reason)),
            extra=extra,
        )
        payload["degraded_reasons"] = list(
            dict.fromkeys(
                [
                    *payload.get("degraded_reasons", []),
                    "managed MCP wrapper disabled for current run after previous backend failure",
                ]
            )
        )
        append_workflow_outcome(outcome_kind, payload)
        return payload
    parsed = _decode_nested_result(raw)
    payload: dict[str, Any] = {
        "success": bool(parsed.get("success", True)),
        "backend_tool": tool_name,
        "degraded_reasons": list(report.degraded_reasons),
        **(dict(extra or {})),
    }
    if isinstance(parsed, Mapping):
        for key, value in parsed.items():
            if key not in {"success"}:
                payload[key] = value
    _normalize_native_backend_status(
        payload,
        outcome_kind=outcome_kind,
        tool_name=tool_name,
        cwd=report.cwd,
    )
    append_workflow_outcome(outcome_kind, payload)
    return payload


def _normalize_native_backend_status(
    payload: dict[str, Any],
    *,
    outcome_kind: str,
    tool_name: str,
    cwd: str | os.PathLike[str] | None,
) -> None:
    if not _native_backend_status_indicates_failure(payload):
        return
    payload["success"] = False
    degraded_reasons = list(payload.get("degraded_reasons", []) or [])
    failure_message = _native_backend_failure_message(payload)
    lowered = failure_message.lower()
    if outcome_kind == "lean-auto-try" and "unknown option" in lowered and "linter.style.longline" in lowered:
        _disable_mcp_tool_for_run(tool_name, cwd=cwd)
        payload["setup_blocker"] = {
            "kind": "unsupported_project_option",
            "option": "linter.style.longLine",
            "scope": "file",
            "message": failure_message,
        }
        degraded_reasons.extend(
            [
                "lean automation try disabled for this run after backend rejected the project-level long-line linter option",
                "Treat unsupported project options as file-level setup blockers, not theorem proof failures; do not edit unrelated examples or solved declarations just to satisfy lean_auto_try.",
            ]
        )
    elif failure_message:
        degraded_reasons.append(f"{outcome_kind} backend rejected: {failure_message}")
    payload["degraded_reasons"] = list(dict.fromkeys(degraded_reasons))


def _local_auto_try_preflight_failure(
    *,
    report: LeanCapabilityReport,
    tool_name: str,
    file_path: str,
    theorem_id: str,
    proof_attempt: str,
    reason: str,
) -> dict[str, Any]:
    if tool_name:
        _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
    payload: dict[str, Any] = {
        "success": False,
        "backend_tool": tool_name,
        "file_path": file_path,
        "theorem_id": theorem_id,
        "proof_attempt": proof_attempt,
        "setup_blocker": {
            "kind": "unsupported_project_option",
            "option": "linter.style.longLine",
            "scope": "file",
            "message": reason,
        },
        "degraded_reasons": list(
            dict.fromkeys(
                [
                    *report.degraded_reasons,
                    reason,
                    "lean automation try disabled for this run before MCP call because the project contains an option the backend rejects",
                    "Use lean_incremental_check or managed patch verification for this theorem; do not edit unrelated examples or solved declarations just to satisfy lean_auto_try.",
                ]
            )
        ),
    }
    append_workflow_outcome("lean-auto-try", payload)
    return payload


def _local_incremental_auto_probe(
    *,
    file_path: str,
    theorem_id: str,
    cwd: str | os.PathLike[str] | None,
    methods: list[str],
    timeout_s: int,
    report: LeanCapabilityReport,
) -> dict[str, Any] | None:
    incremental = report.incremental if isinstance(report.incremental, Mapping) else {}
    if not bool(incremental.get("available", False)):
        return None
    path = Path(file_path)
    entry = _find_declaration_entry(path, theorem_id)
    if not entry:
        return None
    try:
        from epflemma_cli.lean_incremental import lean_incremental_check
    except Exception:
        return None

    effective_timeout_s = max(int(timeout_s or 0), LOCAL_INCREMENTAL_AUTO_PROBE_MIN_TIMEOUT_S)
    attempts: list[dict[str, Any]] = []
    for method in methods:
        replacement = _automation_probe_replacement(entry, method)
        started = time.monotonic()
        if not replacement:
            attempts.append(
                {
                    "mode": method,
                    "api_version": "epflemma-local-lean-interact",
                    "status": "error",
                    "probe_result": {"mode": method, "outcome": "error", "classification": "error", "suggested_script": None},
                    "diagnostics": [
                        {
                            "severity": "error",
                            "message": "local_incremental_probe: could not construct an automation replacement for this declaration",
                            "location": None,
                        }
                    ],
                    "timing": {"elapsed_ms": 0.0, "budget_s": float(effective_timeout_s)},
                    "metadata": {"backend": "lean_incremental_check", "error_code": "replacement_construction_failed"},
                }
            )
            continue
        result = lean_incremental_check(
            action="check_target",
            file_path=file_path,
            theorem_id=theorem_id,
            cwd=str(cwd or report.project_root or report.cwd or ""),
            replacement=replacement,
            include_tactics=False,
            timeout_s=effective_timeout_s,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        ok = bool(result.get("success")) and bool(result.get("ok"))
        status = "success" if ok else ("failed" if bool(result.get("success")) else "error")
        classification = "success" if ok else ("failed" if bool(result.get("success")) else "error")
        attempts.append(
            {
                "mode": method,
                "api_version": "epflemma-local-lean-interact",
                "status": status,
                "probe_result": {
                    "mode": method,
                    "outcome": status,
                    "classification": classification,
                    "suggested_script": replacement if ok else None,
                },
                "diagnostics": _incremental_probe_diagnostics(result),
                "timing": {"elapsed_ms": elapsed_ms, "budget_s": float(effective_timeout_s)},
                "metadata": {
                    "backend": "lean_incremental_check",
                    "cache": result.get("cache", {}),
                    "valid_without_sorry": result.get("valid_without_sorry"),
                    "has_errors": result.get("has_errors"),
                    "has_sorry": result.get("has_sorry"),
                },
            }
        )

    recommended_mode = ""
    for attempt in attempts:
        if _auto_probe_attempt_succeeded(attempt):
            recommended_mode = str(attempt.get("mode", "") or "")
            break
    if not recommended_mode and attempts:
        recommended_mode = str(attempts[0].get("mode", "") or "")
    degraded_reasons = list(report.degraded_reasons)
    if not any(_auto_probe_attempt_succeeded(attempt) for attempt in attempts):
        degraded_reasons.extend(_summarize_attempt_diagnostics(attempts))
    return {
        "success": any(_auto_probe_attempt_succeeded(attempt) for attempt in attempts),
        "backend_tool": "lean_incremental_check",
        "degraded_reasons": list(dict.fromkeys(degraded_reasons)),
        "file_path": file_path,
        "theorem_id": theorem_id,
        "attempts": attempts,
        "recommended_mode": recommended_mode,
    }


def lean_proof_context(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    include_similar_proofs: bool = True,
    similarity_threshold: float = 0.7,
) -> dict[str, Any]:
    report = probe_capabilities(cwd)
    canonical_file_path = _canonical_tool_file_path(file_path, cwd=cwd or report.cwd)
    target_path = Path(canonical_file_path).expanduser().resolve() if canonical_file_path else Path("")
    declaration_entry = _find_declaration_entry(target_path, theorem_id) if canonical_file_path else None
    scan_payload: dict[str, Any] = {}
    resolved_theorem_id = str(theorem_id or "").strip()
    if declaration_entry:
        scan_payload = _scan_theorem_by_range(
            target_path,
            start_line=int(declaration_entry.get("line", 0) or 0),
            end_line=int(declaration_entry.get("end_line", 0) or declaration_entry.get("line", 0) or 0),
        )
        theorem_info = dict(scan_payload.get("theorem") or {}) if isinstance(scan_payload, Mapping) else {}
        theorem_name = str(theorem_info.get("name", "") or "").strip()
        if theorem_name:
            resolved_theorem_id = theorem_name

    tool_name = report.mcp_tools.get("proof_context", "")
    extra = {"file_path": canonical_file_path, "theorem_id": resolved_theorem_id}
    if not tool_name:
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name="",
            unavailable_reason="lean proof context MCP unavailable",
            extra=extra,
        )
        append_workflow_outcome("lean-proof-context", payload)
        return payload
    raw = _BACKEND.invoke_tool(
        tool_name,
        {
            "file": canonical_file_path,
            "theorem_id": resolved_theorem_id,
            "include_similar_proofs": include_similar_proofs,
            "similarity_threshold": similarity_threshold,
        },
    )
    if raw.get("error"):
        _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name=tool_name,
            unavailable_reason=str(raw.get("error", "lean proof context MCP unavailable")),
            extra=extra,
        )
        payload["degraded_reasons"] = list(
            dict.fromkeys(
                [
                    *payload.get("degraded_reasons", []),
                    "managed MCP wrapper disabled for current run after previous backend failure",
                ]
            )
        )
        local_payload = _local_proof_context_payload(
            target_path,
            theorem_id,
            degraded_reasons=[
                *payload["degraded_reasons"],
                "using local declaration fallback after proof context backend failure",
            ],
            scan_payload=scan_payload,
        )
        if local_payload is not None:
            append_workflow_outcome("lean-proof-context", local_payload)
            return local_payload
        append_workflow_outcome("lean-proof-context", payload)
        return payload
    parsed = _decode_nested_result(raw)
    payload: dict[str, Any] = {
        "success": bool(parsed.get("success", True)),
        "backend_tool": tool_name,
        "degraded_reasons": list(report.degraded_reasons),
        **extra,
    }
    if isinstance(parsed, Mapping):
        for key, value in parsed.items():
            if key not in {"success"}:
                payload[key] = value
    backend_status = str(payload.get("status", "") or "").strip().lower()
    if backend_status and backend_status != "success":
        fail_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}
        fail_code = str(fail_metadata.get("fail_code", "") or "").strip().lower()
        fail_message = str(
            fail_metadata.get("fail_message", "")
            or payload.get("error", "")
            or payload.get("message", "")
            or payload.get("status", "")
        ).strip()
        degraded_reasons = list(payload.get("degraded_reasons", []) or [])
        if fail_message:
            degraded_reasons.append(f"proof context backend failure: {fail_message}")
        if fail_code == "theorem_not_found":
            degraded_reasons.append(
                "using local declaration fallback after theorem_not_found without disabling proof-auto MCP"
            )
        elif tool_name:
            _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
            degraded_reasons.append(
                "managed MCP wrapper disabled for current run after previous backend failure"
            )
        local_payload = _local_proof_context_payload(
            target_path,
            theorem_id,
            degraded_reasons=[
                *degraded_reasons,
                "using local declaration fallback after proof context backend failure",
            ],
            scan_payload=scan_payload,
        )
        if local_payload is not None:
            append_workflow_outcome("lean-proof-context", local_payload)
            return local_payload
        payload["success"] = False
        payload["degraded_reasons"] = list(dict.fromkeys(degraded_reasons))
        append_workflow_outcome("lean-proof-context", payload)
        return payload
    payload.setdefault("theorem_statement", "")
    payload.setdefault("original_proof", "")
    payload.setdefault("hypotheses", [])
    payload.setdefault("in_scope", [])
    payload.setdefault("namespace", "")
    payload.setdefault("similar_proofs", [])
    payload.setdefault("metadata", {})
    payload.setdefault("timing", {})
    if declaration_entry and not str(payload.get("theorem_statement", "") or "").strip() and not str(
        payload.get("original_proof", "") or ""
    ).strip():
        local_payload = _local_proof_context_payload(
            target_path,
            theorem_id,
            degraded_reasons=[
                *payload.get("degraded_reasons", []),
                "using local declaration fallback after proof context backend returned empty declaration context",
            ],
            scan_payload=scan_payload,
        )
        if local_payload is not None:
            append_workflow_outcome("lean-proof-context", local_payload)
            return local_payload
    append_workflow_outcome("lean-proof-context", payload)
    return payload


def lean_multi_attempt(
    file_path: str,
    line: int,
    attempts: list[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    report = probe_capabilities(cwd)
    normalized_attempts = _normalize_multi_attempt_candidates(attempts)
    validation_reasons = _multi_attempt_validation_reasons(normalized_attempts)
    canonical_file_path = _canonical_tool_file_path(file_path, cwd=cwd or report.cwd)
    if validation_reasons:
        payload = {
            "success": False,
            "backend_tool": report.mcp_tools.get("multi_attempt", ""),
            "degraded_reasons": list(
                dict.fromkeys(
                    [
                        *report.degraded_reasons,
                        *validation_reasons,
                        "use the managed edit path for one full candidate proof, or patch the file and finish with `lean_verify`",
                    ]
                )
            ),
            "file_path": canonical_file_path,
            "line": line,
            "column": column,
            "attempts": normalized_attempts,
            "action_required": "provide 2-6 short local tactic candidates at one proof location",
        }
        append_workflow_outcome("lean-multi-attempt", payload)
        return payload
    return _invoke_native_mcp_wrapper(
        report.mcp_tools.get("multi_attempt", ""),
        {
            "file_path": canonical_file_path,
            "line": line,
            "column": column,
            "snippets": normalized_attempts,
        },
        report=report,
        unavailable_reason="lean multi-attempt MCP unavailable",
        outcome_kind="lean-multi-attempt",
        extra={"file_path": canonical_file_path, "line": line, "column": column, "attempts": normalized_attempts},
    )


def lean_auto_probe(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    methods: list[str] | None = None,
    timeout_s: int = 60,
) -> dict[str, Any]:
    report = probe_capabilities(cwd)
    tool_name = report.mcp_tools.get("auto_probe", "")
    canonical_file_path = _canonical_tool_file_path(file_path, cwd=cwd or report.cwd)
    extra = {"file_path": canonical_file_path, "theorem_id": theorem_id}
    normalized_methods = [
        str(method).strip()
        for method in list(methods or ["aesop", "aesop?", "grind"])
        if str(method).strip()
    ]
    if not normalized_methods:
        normalized_methods = ["aesop", "aesop?", "grind"]

    local_payload = _local_incremental_auto_probe(
        file_path=canonical_file_path,
        theorem_id=theorem_id,
        cwd=cwd,
        methods=normalized_methods,
        timeout_s=timeout_s,
        report=report,
    )
    if local_payload is not None:
        append_workflow_outcome("lean-auto-probe", local_payload)
        return local_payload

    if not tool_name:
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name="",
            unavailable_reason="lean automation probe MCP unavailable",
            extra=extra,
        )
        payload["attempts"] = []
        append_workflow_outcome("lean-auto-probe", payload)
        return payload

    attempts_payload: list[dict[str, Any]] = []
    for method in normalized_methods:
        raw = _BACKEND.invoke_tool(
            tool_name,
            {
                "file": canonical_file_path,
                "theorem_id": theorem_id,
                "mode": method,
                "budget_s": float(timeout_s),
            },
        )
        if raw.get("error"):
            _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
            payload = _wrapper_unavailable_result(
                report=report,
                tool_name=tool_name,
                unavailable_reason=str(raw.get("error", "lean automation probe MCP unavailable")),
                extra=extra,
            )
            payload["degraded_reasons"] = list(
                dict.fromkeys(
                    [
                        *payload.get("degraded_reasons", []),
                        "managed MCP wrapper disabled for current run after previous backend failure",
                    ]
                )
            )
            payload["attempts"] = attempts_payload
            append_workflow_outcome("lean-auto-probe", payload)
            return payload
        parsed = _decode_nested_result(raw)
        attempt_payload = {"mode": method}
        if isinstance(parsed, Mapping):
            attempt_payload.update(dict(parsed))
        attempts_payload.append(attempt_payload)

    recommended_mode = ""
    for attempt in attempts_payload:
        if _auto_probe_attempt_succeeded(attempt):
            recommended_mode = str(attempt.get("mode", "") or "")
            break
    if not recommended_mode and attempts_payload:
        recommended_mode = str(attempts_payload[0].get("mode", "") or "")
    degraded_reasons = list(report.degraded_reasons)
    if not any(_auto_probe_attempt_succeeded(attempt) for attempt in attempts_payload):
        degraded_reasons.extend(_summarize_attempt_diagnostics(attempts_payload))
    payload = {
        "success": any(_auto_probe_attempt_succeeded(attempt) for attempt in attempts_payload),
        "backend_tool": tool_name,
        "degraded_reasons": list(dict.fromkeys(degraded_reasons)),
        "file_path": canonical_file_path,
        "theorem_id": theorem_id,
        "attempts": attempts_payload,
        "recommended_mode": recommended_mode,
    }
    append_workflow_outcome("lean-auto-probe", payload)
    return payload


def lean_auto_search(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout_s: int = 10,
    objective: str = "balanced",
) -> dict[str, Any]:
    report = probe_capabilities(cwd)
    canonical_file_path = _canonical_tool_file_path(file_path, cwd=cwd or report.cwd)
    return _invoke_native_mcp_wrapper(
        report.mcp_tools.get("auto_search", ""),
        {
            "file": canonical_file_path,
            "theorem_id": theorem_id,
            "search_budget_s": float(timeout_s),
            "search_depth": _auto_search_depth_for_objective(objective),
        },
        report=report,
        unavailable_reason="lean automation search MCP unavailable",
        outcome_kind="lean-auto-search",
        extra={"file_path": canonical_file_path, "theorem_id": theorem_id, "objective": objective},
    )


def lean_auto_try(
    file_path: str,
    theorem_id: str,
    proof_attempt: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout_s: int = 10,
) -> dict[str, Any]:
    report = probe_capabilities(cwd)
    canonical_file_path = _canonical_tool_file_path(file_path, cwd=cwd or report.cwd)
    tool_name = report.mcp_tools.get("auto_try", "")
    unsupported_option_reason = _proof_auto_unsupported_option_reason(canonical_file_path)
    if unsupported_option_reason:
        return _local_auto_try_preflight_failure(
            report=report,
            tool_name=tool_name,
            file_path=canonical_file_path,
            theorem_id=theorem_id,
            proof_attempt=proof_attempt,
            reason=unsupported_option_reason,
        )
    extra = {"file_path": canonical_file_path, "theorem_id": theorem_id, "proof_attempt": proof_attempt}
    if not tool_name:
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name="",
            unavailable_reason="lean automation try MCP unavailable",
            extra=extra,
        )
        append_workflow_outcome("lean-auto-try", payload)
        return payload
    raw = _BACKEND.invoke_tool(
        tool_name,
        {
            "file": canonical_file_path,
            "theorem_id": theorem_id,
            "proof_attempt": proof_attempt,
            "timeout_s": timeout_s,
            "return_proof_state": True,
        },
    )
    if raw.get("error"):
        _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
        payload = _wrapper_unavailable_result(
            report=report,
            tool_name=tool_name,
            unavailable_reason=str(raw.get("error", "lean automation try MCP unavailable")),
            extra=extra,
        )
        payload["degraded_reasons"] = list(
            dict.fromkeys(
                [
                    *payload.get("degraded_reasons", []),
                    "managed MCP wrapper disabled for current run after previous backend failure",
                ]
            )
        )
        append_workflow_outcome("lean-auto-try", payload)
        return payload
    parsed = _decode_nested_result(raw)
    payload: dict[str, Any] = {
        "success": bool(parsed.get("success", True)),
        "backend_tool": tool_name,
        "degraded_reasons": list(report.degraded_reasons),
        **extra,
    }
    if isinstance(parsed, Mapping):
        for key, value in parsed.items():
            if key not in {"success"}:
                payload[key] = value
    _normalize_native_backend_status(
        payload,
        outcome_kind="lean-auto-try",
        tool_name=tool_name,
        cwd=report.cwd,
    )
    harness_failure = _proof_auto_harness_failure_message(payload)
    if harness_failure:
        if tool_name:
            _disable_mcp_tool_for_run(tool_name, cwd=report.cwd)
        payload["success"] = False
        payload["setup_blocker"] = {
            "kind": "proof_auto_harness_construction",
            "scope": "theorem",
            "message": harness_failure,
        }
        payload["degraded_reasons"] = list(
            dict.fromkeys(
                [
                    *payload.get("degraded_reasons", []),
                    "lean automation try disabled for this run after backend could not construct a proof harness",
                    "Use lean_incremental_check or managed patch verification for this theorem; treat this as a backend setup failure, not a proof failure.",
                ]
            )
        )
    append_workflow_outcome("lean-auto-try", payload)
    return payload


def lean_axioms(
    target: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    file_path: str = "",
) -> LeanAxiomReport:
    project_root, _ = _project_root(cwd)
    root = Path(project_root) if project_root else None
    target_file = Path(file_path).expanduser().resolve() if file_path else None
    if root is None or target_file is None:
        report = LeanAxiomReport(
            target=target,
            file_path=str(target_file or ""),
            ok=False,
            axioms=[],
            custom_axioms=[],
            classical=False,
            choice=False,
            note="Provide both a Lean project and file_path to inspect axioms.",
        )
        append_workflow_outcome("lean-axioms", report.to_dict())
        return report
    try:
        module_name = _module_name_for_file(root, target_file)
    except Exception:
        module_name = ""
    if not module_name:
        report = LeanAxiomReport(
            target=target,
            file_path=str(target_file),
            ok=False,
            axioms=[],
            custom_axioms=[],
            classical=False,
            choice=False,
            note="Could not resolve module name for the target file.",
        )
        append_workflow_outcome("lean-axioms", report.to_dict())
        return report
    with tempfile.NamedTemporaryFile("w", suffix=".lean", delete=False, dir=str(root)) as handle:
        handle.write(f"import {module_name}\n#print axioms {target}\n")
        temp_path = Path(handle.name)
    try:
        try:
            relative = str(temp_path.relative_to(root))
        except Exception:
            relative = str(temp_path)
        _, output = _BACKEND.run_command(["lake", "env", "lean", relative], cwd=root)
    finally:
        temp_path.unlink(missing_ok=True)
    axioms = sorted(
        {
            token
            for token in re.findall(r"[A-Za-z0-9_.]+", output)
            if "." in token or token in STANDARD_AXIOMS
        }
        - {
            token
            for token in (
                target,
                module_name,
                *(
                    f"{prefix}.{target.split('.')[-1]}"
                    for prefix in {
                        module_name,
                        module_name.rsplit(".", 1)[0] if "." in module_name else "",
                    }
                    if prefix
                ),
            )
            if token
        }
    )
    nonstandard = [axiom for axiom in axioms if axiom not in STANDARD_AXIOMS]
    report = LeanAxiomReport(
        target=target,
        file_path=str(target_file),
        ok=bool(output) and not nonstandard,
        axioms=axioms,
        custom_axioms=nonstandard,
        classical=any("Classical" in axiom for axiom in axioms),
        choice="Classical.choice" in axioms,
        note="no non-standard axioms found" if output and not nonstandard else output[:600],
    )
    append_workflow_outcome("lean-axioms", report.to_dict())
    return report


def route_workflow_step(
    workflow_kind: str,
    live_state: Mapping[str, Any] | None,
    *,
    configured_skill: str = "",
    autonomy_state: Mapping[str, Any] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> WorkflowRouteDecision:
    current = dict(live_state or {})
    autonomy = dict(autonomy_state or {})
    report = probe_capabilities(cwd)
    queue_item = dict(current.get("current_queue_item") or {})
    blocker_text = "\n".join(
        part
        for part in (
            str(current.get("current_blocker", "") or ""),
            str(current.get("diagnostics", "") or ""),
            str(current.get("goals", "") or ""),
            str(current.get("build_status", "") or ""),
        )
        if part
    )
    blocker_kind = classify_blocker_kind(blocker_text)
    target_symbol = str(queue_item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or "").strip()
    attempts = [
        dict(item)
        for item in autonomy.get("failed_attempts", [])
        if isinstance(item, Mapping)
        and str(item.get("target_symbol", "") or "").strip() == target_symbol
        and str(item.get("active_file", "") or "").strip() in {active_file, str(current.get("active_file_label", "") or "")}
    ]
    attempt_count = len(attempts)
    workflow_command = str(
        os.getenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "")
        or os.getenv("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", "")
    ).strip()
    empty_search_streak = recent_empty_search_streak(workflow_command=workflow_command) if workflow_command else 0
    search_exhausted = bool(current.get("search_exhausted")) or attempt_count >= 2 or not report.search_providers or empty_search_streak >= 3
    normalized_workflow = str(workflow_kind or "").strip().lower()
    if normalized_workflow == "autoprove":
        normalized_workflow = "prove"
    elif normalized_workflow == "autoformalize":
        normalized_workflow = "formalize"
    recommended_worker = ""
    route_action = "final-sweep"
    skill_name = configured_skill.strip() or "lean-proof-loop"
    reason = "default autonomous workflow path"

    if normalized_workflow in {"review", "checkpoint"}:
        return WorkflowRouteDecision(
            workflow_kind=normalized_workflow,
            skill_name="lean-diagnostics",
            route_action="diagnostics",
            blocker_kind=blocker_kind,
            recommended_worker="",
            search_exhausted=search_exhausted,
            reason="review/checkpoint use the diagnostics skill",
        )
    if normalized_workflow in {"refactor", "golf"}:
        return WorkflowRouteDecision(
            workflow_kind=normalized_workflow,
            skill_name="lean-refactor-golf",
            route_action="golf" if normalized_workflow == "golf" else "refactor",
            blocker_kind=blocker_kind,
            recommended_worker="",
            search_exhausted=search_exhausted,
            reason="refactor/golf routes through the dedicated refactor skill",
        )

    if queue_item:
        skill_name = "lean-theorem-queue-worker"
        route_action = "queue-worker"
        reason = "file-scoped queue item active"
    if LEAN_WORKER_DISPATCH_ENABLED:
        if blocker_kind in {"unknown_ident", "synth_instance", "type_mismatch", "timeout"} and attempt_count >= 2:
            recommended_worker = "proof-repair"
            route_action = "delegate-proof-repair"
            reason = f"compiler-style blocker {blocker_kind} repeated {attempt_count} times"
        elif blocker_kind == "axiom-risk":
            recommended_worker = "axiom-eliminator"
            route_action = "delegate-axiom-eliminator"
            reason = "axiom-sensitive blocker detected"
        elif queue_item and (attempt_count >= 3 or (search_exhausted and blocker_kind in {"sorry", "open_goals", "diagnostics"})):
            recommended_worker = "sorry-filler-deep"
            route_action = "delegate-sorry-filler-deep"
            reason = "queue item remains blocked after repeated attempts/search exhaustion"

    decision = WorkflowRouteDecision(
        workflow_kind=normalized_workflow,
        skill_name=skill_name,
        route_action=route_action,
        blocker_kind=blocker_kind,
        recommended_worker=recommended_worker,
        search_exhausted=search_exhausted,
        reason=reason,
    )
    append_workflow_outcome("workflow-route", decision.to_dict())
    return decision


def _worker_prompt(worker: str, request: LeanWorkerRequest) -> str:
    record = get_lean_spec(worker)
    title = record.title if record else worker
    summary = record.summary if record else worker
    parts = [
        f"Native Lean worker: {title}",
        summary,
        "",
        f"Goal: {request.goal}",
    ]
    if request.file_path:
        parts.append(f"File: {request.file_path}")
    if request.line:
        parts.append(f"Line: {request.line}")
    if request.context:
        parts.extend(["", "Context:", request.context])
    if record:
        parts.extend(
            [
                "",
                "Worker contract:",
                f"- route action(s): {', '.join(record.route_actions) or '[none]'}",
                f"- tools: {', '.join(record.tools) or '[none]'}",
            ]
        )
    return "\n".join(parts).strip()


def dispatch_worker(
    request: LeanWorkerRequest,
    *,
    parent_agent: Any = None,
    owner_id: str = "",
) -> LeanWorkerResult:
    worker = request.worker.strip()
    lock_result: dict[str, Any] | None = None
    if request.use_file_lock and request.file_path and owner_id:
        lock_result = _acquire_file_lock(
            request.file_path,
            owner_id=owner_id,
            purpose=f"lean-worker:{worker}",
            ttl_seconds=1800,
            force=False,
        )
        if not lock_result.get("success"):
            result = LeanWorkerResult(
                worker=worker,
                mode="plan",
                dispatched=False,
                summary=f"File lock unavailable for {request.file_path}: {lock_result.get('error', 'unknown error')}",
                lock=lock_result,
            )
            append_workflow_outcome("lean-worker", result.to_dict())
            return result

    prompt = _worker_prompt(worker, request)
    if not request.allow_delegation or parent_agent is None:
        result = LeanWorkerResult(
            worker=worker,
            mode="plan",
            dispatched=False,
            summary=prompt,
            lock=lock_result,
        )
        append_workflow_outcome("lean-worker", result.to_dict())
        return result

    from tools.delegate_tool import delegate_task

    delegated = delegate_task(
        goal=request.goal,
        context=prompt,
        toolsets=["terminal", "file", "skills", "coordination"],
        parent_agent=parent_agent,
        max_iterations=40,
    )
    result = LeanWorkerResult(
        worker=worker,
        mode="delegate",
        dispatched=True,
        summary=f"Delegated {worker}",
        result=delegated,
        lock=lock_result,
    )
    append_workflow_outcome("lean-worker", result.to_dict())
    return result
