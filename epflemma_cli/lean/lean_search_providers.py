"""Stateless Lean search-provider helpers for lean_services.

Extracted verbatim from ``lean_services.py`` (refactor Phase 5 — the search-provider cluster). These
helpers back the Lean ``lean_search`` fallback chain: they read the LeanExplore backend preference /
API key / local cache from the environment and filesystem, probe local-backend availability, query the
remote LeanExplore API, and normalise / shape raw search payloads (nested-result decoding, per-item
formatting, fragment extraction, model-to-dict coercion) into the ``provider`` / ``match`` records the
search result surface expects.

They depend only on stdlib (``contextlib``/``importlib``/``io``/``json``/``os``/``re``/``pathlib``/
``typing``) plus the module-level ``SEARCH_PROVIDER_LABELS`` / ``_LEANEXPLORE_LOCAL_REQUIRED_ENTRIES``
constants that live here, and they do NOT read or mutate any cross-module state, invoke a Lean backend
(``_invoke_json_tool`` / ``_run_command``), or touch the warm LeanExplore local service singleton. That
stateful local-service trio (the ``_LEANEXPLORE_LOCAL_SERVICE`` / ``_LEANEXPLORE_LOCAL_RERANK_DISABLED``
globals plus ``_leanexplore_local_service`` / ``_leanexplore_local_search``) stays in lean_services so
its ``global`` rebinds and the tests' ``monkeypatch.setattr(lean_services, ...)`` keep resolving in one
namespace. ``_rg_search`` also stays because it calls the lean_services-only ``_run_command``.

This module does NOT import lean_services or native_runner, so the re-export shim in lean_services
introduces no import cycle. ``SEARCH_PROVIDER_LABELS`` is re-exported there because the lean_services
orchestrators (``lean_search`` / ``probe_capabilities``) still reference it by bare name.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SEARCH_PROVIDER_LABELS = {
    "local_search": "mcp-local-search",
    "leanexplore_local": "leanexplore-local",
    "leanexplore_api": "leanexplore-api",
    "leanfinder": "mcp-leanfinder",
    "leansearch": "mcp-leansearch",
    "loogle": "mcp-loogle",
    "leanexplore": "mcp-leanexplore",
    "project_rg": "project-rg",
    "mathlib_rg": "mathlib-rg",
}

_LEANEXPLORE_LOCAL_REQUIRED_ENTRIES = (
    "lean_explore.db",
    "informalization_faiss.index",
    "informalization_faiss_ids_map.json",
    "bm25_ids_map.json",
    "bm25_name_raw",
    "bm25_name_spaced",
)


def _leanexplore_api_key() -> str:
    return str(os.getenv("LEANEXPLORE_API_KEY", "") or "").strip()


def _leanexplore_backend_preference() -> str:
    value = str(
        os.getenv("EPFLEMMA_LEANEXPLORE_BACKEND", "")
        or os.getenv("LEANEXPLORE_BACKEND", "")
        or "auto"
    ).strip().lower()
    return value if value in {"auto", "local", "api", "off", "disabled"} else "auto"


def _leanexplore_cache_root() -> Path:
    return Path(os.getenv("LEAN_EXPLORE_CACHE_DIR", "~/.lean_explore/cache")).expanduser()


def _leanexplore_local_cache_path() -> Path | None:
    cache_root = _leanexplore_cache_root()
    candidates: list[Path] = []
    version = str(os.getenv("LEAN_EXPLORE_VERSION", "") or "").strip()
    if version:
        candidates.append(cache_root / version)
    active_version_file = cache_root.parent / "active_version"
    try:
        active_version = active_version_file.read_text(encoding="utf-8").strip()
    except Exception:
        active_version = ""
    if active_version:
        candidates.append(cache_root / active_version)
    if cache_root.is_dir():
        candidates.extend(path for path in cache_root.iterdir() if path.is_dir())
    for candidate in candidates:
        if all((candidate / entry).exists() for entry in _LEANEXPLORE_LOCAL_REQUIRED_ENTRIES):
            return candidate
    return None


def _leanexplore_local_status() -> dict[str, Any]:
    try:
        package_available = importlib.util.find_spec("lean_explore.search") is not None
    except (ImportError, AttributeError, ValueError):
        package_available = False
    cache_path = _leanexplore_local_cache_path()
    return {
        "package_available": package_available,
        "data_ready": cache_path is not None,
        "cache_path": str(cache_path or ""),
        "available": bool(package_available and cache_path is not None),
    }


def _model_to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        dumped = as_dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    return {"value": value}


def _is_leanexplore_reranker_load_error(exc: Exception) -> bool:
    message = str(exc)
    return "Cannot copy out of meta tensor" in message and "to_empty()" in message


def _leanexplore_local_verbose() -> bool:
    value = str(os.getenv("EPFLEMMA_LEANEXPLORE_VERBOSE", "") or os.getenv("LEANEXPLORE_VERBOSE", "") or "")
    return value.strip().lower() in {"1", "true", "yes", "on", "debug"}


@contextlib.contextmanager
def _quiet_leanexplore_local_output():
    if _leanexplore_local_verbose():
        yield
        return
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        yield


def _leanexplore_api_search(query: str, *, limit: int = 10) -> tuple[list[dict[str, Any]], str]:
    api_key = _leanexplore_api_key()
    if not api_key:
        return [], "LEANEXPLORE_API_KEY is not configured"
    try:
        import httpx

        response = httpx.get(
            "https://www.leanexplore.com/api/v2/search",
            params={"q": query, "limit": max(1, int(limit or 10))},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"LeanExplore API search failed: {exc}"
    if not isinstance(payload, Mapping):
        return [], "LeanExplore API returned an unexpected payload"
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return [], "LeanExplore API returned results in an unexpected format"
    results: list[dict[str, Any]] = []
    for item in raw_results[:limit]:
        if isinstance(item, Mapping):
            entry: dict[str, Any] = {
                "provider": SEARCH_PROVIDER_LABELS["leanexplore_api"],
                "match": _format_search_payload_item(item)[:400],
            }
            for key in ("id", "name", "module", "source_link"):
                value = item.get(key)
                if value not in (None, ""):
                    entry[key] = value
            results.append(entry)
        else:
            fragment = str(item).strip()
            if fragment:
                results.append(
                    {
                        "provider": SEARCH_PROVIDER_LABELS["leanexplore_api"],
                        "match": fragment[:400],
                    }
                )
    return results, ""


def _decode_nested_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, str):
        text = result.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", text)
            text = re.sub(r"\n```$", "", text)
        try:
            parsed = json.loads(text)
        except Exception:
            return {"text": result}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"value": parsed}
    return dict(payload)


def _format_search_payload_item(item: Any) -> str:
    if isinstance(item, Mapping):
        name = str(item.get("name", "") or "").strip()
        module = str(item.get("module", "") or "").strip()
        description = str(
            item.get("description", "")
            or item.get("informalization", "")
            or item.get("docstring", "")
            or item.get("source_text", "")
            or ""
        ).strip()
        source_link = str(item.get("source_link", "") or "").strip()
        parts = []
        if name:
            parts.append(name)
        if module:
            parts.append(f"[{module}]")
        if description:
            parts.append(description)
        if source_link:
            parts.append(source_link)
        if parts:
            return " - ".join(parts)
        return json.dumps(dict(item), sort_keys=True, default=str)
    if isinstance(item, list):
        return "; ".join(_format_search_payload_item(part) for part in item)
    return str(item).strip()


def _search_payload_fragments(payload: Mapping[str, Any], *, limit: int) -> list[str]:
    decoded = _decode_nested_result(payload)
    candidates: list[Any] = []
    for key in ("results", "matches", "items", "declarations"):
        value = decoded.get(key)
        if isinstance(value, list):
            candidates.extend(value)
            break
    if not candidates:
        for value in decoded.values():
            if isinstance(value, str) and value.strip():
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(value[:limit])

    fragments: list[str] = []
    for item in candidates:
        fragment = _format_search_payload_item(item)
        if fragment:
            fragments.append(fragment)
        if len(fragments) >= limit:
            break
    return fragments
