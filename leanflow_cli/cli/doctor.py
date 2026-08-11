"""Native Lean workflow diagnostics for the LeanFlow shell."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.config import ensure_leanflow_home, get_leanflow_home, load_config
from leanflow_cli.lean.lean_services import probe_capabilities
from leanflow_cli.runtime.branding import get_cli_command_name, get_product_name
from leanflow_cli.runtime.runtime_provider import (
    format_runtime_provider_error,
    resolve_runtime_provider,
)
from tools.mcp.mcp_tool import get_mcp_status

DOCTOR_MODES = {"all", "env", "mcp", "search", "web-search", "migrate", "cleanup"}
WEB_SEARCH_SMOKE_QUERY = "prime number theorem formalization Lean"


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "all").strip().lower() or "all"
    return normalized if normalized in DOCTOR_MODES else "all"


def _default_model(config: dict[str, Any]) -> str:
    model_cfg = config.get("model", {})
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default", "") or "")
    if isinstance(model_cfg, str):
        return model_cfg.strip()
    return ""


def _provider_payload() -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        runtime = resolve_runtime_provider()
        return {
            "available": True,
            "provider": str(runtime.get("provider", "") or ""),
            "base_url": str(runtime.get("base_url", "") or ""),
            "api_mode": str(runtime.get("api_mode", "") or ""),
            "model": str(runtime.get("model", "") or ""),
        }, issues
    except Exception as exc:
        message = format_runtime_provider_error(exc)
        issues.append(message)
        return {
            "available": False,
            "error": message,
        }, issues


def _legacy_reference_payload() -> dict[str, Any]:
    home = Path.home()
    plugin_root = home / ".claude" / "plugins" / "cache" / "lean4-skills" / "lean4" / "4.4.8"
    return {
        "plugin_reference_path": str(plugin_root),
        "plugin_reference_present": plugin_root.is_dir(),
    }


def _cleanup_payload(cwd: Path) -> dict[str, Any]:
    candidates = []
    for relative in (".claude",):
        path = cwd / relative
        if path.exists():
            candidates.append(str(path))
    return {
        "candidate_paths": candidates,
        "apply_supported": False,
        "note": "Cleanup mode is advisory only; no files are removed automatically.",
    }


def _web_search_payload() -> tuple[dict[str, Any], list[str]]:
    """Smoke-test the web_search tool by executing a query and validating tool exposure, Lean-first guidance, and result availability. Returns a status payload and list of diagnostic issues."""
    issues: list[str] = []
    payload: dict[str, Any] = {
        "available": False,
        "tool_exposed": False,
        "lean_search_guidance": False,
        "query": WEB_SEARCH_SMOKE_QUERY,
        "success": False,
        "result_count": 0,
        "providers": [],
        "kinds": [],
        "degraded_reasons": [],
        "first_results": [],
        "firecrawl_error": False,
    }
    try:
        from tools.implementations import web_tools  # noqa: F401
        from tools.registry import registry

        definitions = registry.get_definitions({"web_search"}, quiet=True)
        payload["tool_exposed"] = any(
            item.get("function", {}).get("name") == "web_search" for item in definitions
        )
        description = ""
        for item in definitions:
            function = item.get("function", {})
            if function.get("name") == "web_search":
                description = str(function.get("description", "") or "")
                break
        payload["lean_search_guidance"] = "prefer lean_search first" in description.lower()
        if not payload["tool_exposed"]:
            issues.append("web_search is not exposed to the model tool registry.")
        if not payload["lean_search_guidance"]:
            issues.append("web_search description is missing the Lean-first lean_search guidance.")

        from tools.implementations.web_tools import web_search_tool

        raw_result = web_search_tool(WEB_SEARCH_SMOKE_QUERY, limit=5)
        parsed = json.loads(raw_result)
        payload["success"] = bool(parsed.get("success"))
        payload["degraded_reasons"] = [
            str(reason) for reason in parsed.get("degraded_reasons", []) if str(reason).strip()
        ]
        payload["firecrawl_error"] = "Firecrawl" in raw_result or "FIRECRAWL" in raw_result
        results = parsed.get("data", {}).get("web", [])
        if isinstance(results, list):
            payload["result_count"] = len(results)
            payload["providers"] = sorted(
                {
                    str(item.get("provider", "") or "")
                    for item in results
                    if isinstance(item, dict) and item.get("provider")
                }
            )
            payload["kinds"] = sorted(
                {
                    str(item.get("kind", "") or "")
                    for item in results
                    if isinstance(item, dict) and item.get("kind")
                }
            )
            payload["first_results"] = [
                {
                    "provider": str(item.get("provider", "") or ""),
                    "kind": str(item.get("kind", "") or ""),
                    "title": str(item.get("title", "") or ""),
                    "url": str(item.get("url", "") or ""),
                }
                for item in results[:3]
                if isinstance(item, dict)
            ]
        if not payload["success"]:
            issues.append("web_search smoke query did not report success.")
        if not payload["result_count"]:
            issues.append("web_search smoke query returned no results.")
        if payload["firecrawl_error"]:
            issues.append("web_search smoke query still depends on Firecrawl configuration.")
        payload["available"] = bool(
            payload["tool_exposed"]
            and payload["lean_search_guidance"]
            and payload["success"]
            and payload["result_count"]
            and not payload["firecrawl_error"]
        )
    except Exception as exc:
        issues.append(f"web_search smoke check failed: {exc}")
        payload["error"] = str(exc)
    return payload, issues


def _doctor_payload(
    active_cwd: str | Path | None = None, *, mode: str = "all"
) -> tuple[dict[str, Any], list[str]]:
    """Build a mode-filtered diagnostic payload aggregating Lean project health, runtime provider, MCP, search providers, and optional migration/cleanup guidance. Returns the complete payload dict and accumulated issues list."""
    ensure_leanflow_home()
    cwd = Path(active_cwd or Path.cwd()).expanduser().resolve()
    normalized_mode = _normalize_mode(mode)
    config = load_config()
    cli_name = get_cli_command_name()
    if normalized_mode == "web-search":
        web_search, issues = _web_search_payload()
        payload: dict[str, Any] = {
            "product": get_product_name(),
            "command": cli_name,
            "mode": normalized_mode,
            "cwd": str(cwd),
            "home": str(get_leanflow_home()),
            "config_path": str(get_leanflow_home() / "config.yaml"),
            "default_model": _default_model(config),
            "web_search": web_search,
            "issues": list(dict.fromkeys(issues)),
        }
        return payload, payload["issues"]

    capability = probe_capabilities(cwd).to_dict()

    issues: list[str] = []
    issues.extend(
        str(reason) for reason in capability.get("degraded_reasons", []) if str(reason).strip()
    )
    if not capability.get("project_valid"):
        project_error = str(capability.get("project_error", "") or "").strip()
        if project_error:
            issues.append(project_error)
        else:
            issues.append(f"Initialize a Lean project with `{cli_name} project init`.")

    provider, provider_issues = _provider_payload()
    issues.extend(provider_issues)

    mcp_status = get_mcp_status()
    if normalized_mode in {"all", "mcp"}:
        if not mcp_status:
            issues.append("No MCP servers configured.")
        else:
            for entry in mcp_status:
                if entry.get("bootstrap_recommended"):
                    issues.append(
                        f"MCP bootstrap recommended for {entry.get('name', '[unknown]')}."
                    )

    search_providers = [
        str(item) for item in capability.get("search_providers", []) if str(item).strip()
    ]
    if normalized_mode in {"all", "search"} and not search_providers:
        issues.append("No Lean search providers available.")

    payload: dict[str, Any] = {
        "product": get_product_name(),
        "command": cli_name,
        "mode": normalized_mode,
        "cwd": str(cwd),
        "home": str(get_leanflow_home()),
        "config_path": str(get_leanflow_home() / "config.yaml"),
        "default_model": _default_model(config),
        "capability_report": capability,
        "provider": provider,
        "issues": list(dict.fromkeys(issues)),
    }

    if normalized_mode in {"all", "mcp"}:
        payload["mcp_status"] = mcp_status
    if normalized_mode in {"all", "search"}:
        payload["search"] = {
            "providers": search_providers,
            "helper_tools": dict(capability.get("helper_tools", {}) or {}),
        }
    if normalized_mode in {"all", "migrate"}:
        payload["migrate"] = _legacy_reference_payload()
    if normalized_mode in {"all", "cleanup"}:
        payload["cleanup"] = _cleanup_payload(cwd)

    return payload, payload["issues"]


def _format_doctor_report(payload: dict[str, Any]) -> str:
    """Format a doctor payload into a human-readable text report, branching on mode (web-search or full diagnostics) and rendering each section with status indicators and issue summaries."""
    if payload.get("mode") == "web-search":
        web_search = dict(payload.get("web_search", {}) or {})
        lines = [
            f"{payload.get('product', 'LeanFlow')} Doctor",
            "Mode: web-search",
            f"Home: {payload.get('home', '')}",
            "",
            "Web search:",
            f"- available: {'yes' if web_search.get('available') else 'no'}",
            f"- model tool exposed: {'yes' if web_search.get('tool_exposed') else 'no'}",
            f"- Lean-first guidance: {'yes' if web_search.get('lean_search_guidance') else 'no'}",
            f"- smoke query: {web_search.get('query') or '[none]'}",
            f"- success: {'yes' if web_search.get('success') else 'no'}",
            f"- result count: {web_search.get('result_count', 0)}",
            f"- providers: {', '.join(web_search.get('providers', []) or []) or '[none]'}",
            f"- kinds: {', '.join(web_search.get('kinds', []) or []) or '[none]'}",
            f"- Firecrawl dependency error: {'yes' if web_search.get('firecrawl_error') else 'no'}",
        ]
        first_results = list(web_search.get("first_results", []) or [])
        if first_results:
            lines.append("- first results:")
            for item in first_results:
                lines.append(
                    f"  - {item.get('kind') or 'result'} via {item.get('provider') or 'unknown'}: "
                    f"{item.get('title') or '[untitled]'} ({item.get('url') or '[no url]'})"
                )
        degraded = list(web_search.get("degraded_reasons", []) or [])
        if degraded:
            lines.append("- degraded:")
            lines.extend(f"  - {reason}" for reason in degraded)
        issues = list(payload.get("issues", []) or [])
        lines.extend(["", "Issues:"])
        if issues:
            lines.extend(f"- {issue}" for issue in issues)
        else:
            lines.append("- none")
        return "\n".join(lines)

    capability = dict(payload.get("capability_report", {}) or {})
    provider = dict(payload.get("provider", {}) or {})
    lines = [
        f"{payload.get('product', 'LeanFlow')} Doctor",
        f"Mode: {payload.get('mode', 'all')}",
        f"CWD: {payload.get('cwd', '')}",
        f"Home: {payload.get('home', '')}",
        f"Config: {payload.get('config_path', '')}",
        f"Default model: {payload.get('default_model', '') or '[unset]'}",
        "",
        "Environment:",
        f"- project root: {capability.get('project_root') or '[none]'}",
        f"- project valid: {'yes' if capability.get('project_valid') else 'no'}",
        f"- project error: {capability.get('project_error') or '[none]'}",
    ]
    binaries = dict(capability.get("binaries", {}) or {})
    for name in ("lean", "lake", "elan", "git", "rg"):
        lines.append(f"- {name}: {'OK' if binaries.get(name) else 'missing'}")

    if provider.get("available"):
        lines.extend(
            [
                "",
                "Runtime provider:",
                f"- provider: {provider.get('provider') or '[unset]'}",
                f"- base URL: {provider.get('base_url') or '[unset]'}",
                f"- API mode: {provider.get('api_mode') or '[unset]'}",
                f"- model: {provider.get('model') or '[unset]'}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Runtime provider:",
                f"- unavailable: {provider.get('error') or '[unknown error]'}",
            ]
        )

    mcp_status = payload.get("mcp_status")
    if isinstance(mcp_status, list):
        lines.extend(["", "MCP:"])
        if not mcp_status:
            lines.append("- no configured servers")
        for entry in mcp_status:
            name = str(entry.get("name", "") or "[unknown]")
            transport = str(entry.get("transport", "") or "stdio")
            connected = "connected" if entry.get("connected") else "down"
            tools = int(entry.get("tools", 0) or 0)
            extras: list[str] = []
            role = str(entry.get("role", "") or "").strip()
            if role:
                extras.append(f"role={role}")
            if entry.get("managed"):
                extras.append("managed")
            if entry.get("configured") is False:
                extras.append("not configured")
            if entry.get("installed") is False:
                extras.append("not installed")
            if entry.get("bootstrap_recommended"):
                extras.append("bootstrap recommended")
            suffix = f" [{', '.join(extras)}]" if extras else ""
            lines.append(f"- {name}: {connected} ({transport}, {tools} tools){suffix}")
            power = dict(entry.get("power_modes", {}) or {})
            if power:
                lines.append(
                    f"  power: local Loogle={power.get('loogle_local_status', 'unknown')}, "
                    f"REPL={power.get('repl_status', 'unknown')}, "
                    f"search={power.get('remote_search_policy', 'public-fallbacks-enabled')}"
                )

    search = payload.get("search")
    if isinstance(search, dict):
        lines.extend(["", "Search:"])
        providers = list(search.get("providers", []) or [])
        lines.append(
            f"- providers: {', '.join(str(item) for item in providers) if providers else '[none]'}"
        )
        helpers = dict(search.get("helper_tools", {}) or {})
        helper_summary = ", ".join(
            f"{name}={'yes' if enabled else 'no'}" for name, enabled in sorted(helpers.items())
        )
        lines.append(f"- helpers: {helper_summary or '[none]'}")

    lines.extend(
        [
            "",
            "Workers:",
            f"- available: {', '.join(capability.get('workers', []) or []) or '[none]'}",
        ]
    )

    migrate = payload.get("migrate")
    if isinstance(migrate, dict):
        lines.extend(
            [
                "",
                "Migration reference:",
                f"- plugin cache: {migrate.get('plugin_reference_path')}",
                f"- plugin present: {'yes' if migrate.get('plugin_reference_present') else 'no'}",
            ]
        )

    cleanup = payload.get("cleanup")
    if isinstance(cleanup, dict):
        candidates = list(cleanup.get("candidate_paths", []) or [])
        lines.extend(
            [
                "",
                "Cleanup:",
                f"- candidate paths: {', '.join(candidates) if candidates else '[none]'}",
                f"- note: {cleanup.get('note') or '[none]'}",
            ]
        )

    issues = list(payload.get("issues", []) or [])
    lines.extend(["", "Issues:"])
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("- none")
    return "\n".join(lines)


def run_doctor(
    active_cwd: str | Path | None = None,
    *,
    mode: str = "all",
    json_output: bool = False,
) -> tuple[list[str], str | dict[str, Any]]:
    payload, issues = _doctor_payload(active_cwd, mode=mode)
    if json_output:
        return issues, payload
    return issues, _format_doctor_report(payload)
