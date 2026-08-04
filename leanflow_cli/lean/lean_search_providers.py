"""Normalize and route stateless Lean search-provider requests.

The helpers configure LeanExplore backends, probe local availability, query
the remote service, and shape raw results for ``lean_search``. Stateful local
service management remains in ``lean_services``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.runtime_modes import dispatch_worker_enabled, low_memory_mode_enabled

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
    if low_memory_mode_enabled():
        return "off"
    if dispatch_worker_enabled():
        # Each worker is already a process-isolated research lane. Loading a
        # second local FAISS/BM25 service in every lane defeats that isolation's
        # memory bound; the foreground keeps its full configured backend.
        value = str(os.getenv("LEANFLOW_DISPATCH_LEANEXPLORE_BACKEND", "off") or "off")
        value = value.strip().lower()
        return value if value in {"auto", "local", "api", "off", "disabled"} else "off"
    value = (
        str(
            os.getenv("LEANFLOW_LEANEXPLORE_BACKEND", "")
            or os.getenv("LEANEXPLORE_BACKEND", "")
            or "auto"
        )
        .strip()
        .lower()
    )
    return value if value in {"auto", "local", "api", "off", "disabled"} else "auto"


def _leanexplore_local_rerank_top() -> int:
    """Return the opt-in local cross-encoder rerank candidate count.

    LeanExplore's Qwen reranker materializes full-vocabulary logits for every
    token in a candidate batch.  Its historical default of 50 can therefore
    create multi-gigabyte transient allocations even though LeanFlow only
    consumes the final-token score.  Hybrid BM25 plus FAISS retrieval remains
    enabled by default; deployments with sufficient memory can explicitly
    restore cross-encoder reranking through the environment.
    """
    raw = str(os.getenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", "") or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, min(value, 50))


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


# Substrings that mark a *corrupt* local LeanExplore SQLite index (not a transient
# query error). A 100MB+ index in this state cannot self-repair, so we move it aside
# and let `lean-explore data fetch` rebuild it; meanwhile remote/MCP providers answer
# the query. Matching is done on the lower-cased exception text so it catches the
# SQLAlchemy wrapper around the underlying sqlite3 error.
_LEANEXPLORE_CORRUPT_DB_SIGNATURES = (
    "database disk image is malformed",
    "file is not a database",
    "file is encrypted or is not a database",
    "malformed database schema",
    "database or disk is full",
)


def _is_leanexplore_corrupt_db_error(exc: BaseException) -> bool:
    """True if *exc* indicates the local LeanExplore index is corrupt (vs a query bug)."""
    text = str(exc).lower()
    return any(signature in text for signature in _LEANEXPLORE_CORRUPT_DB_SIGNATURES)


def _quarantine_corrupt_leanexplore_db() -> Path | None:
    """Move a corrupt local LeanExplore DB aside so the next fetch rebuilds it.

    Renames ``lean_explore.db`` to ``lean_explore.db.corrupt`` (never deletes — the move
    is reversible). With the DB gone the cache no longer satisfies the required-entries
    check, so ``_leanexplore_local_cache_path`` reports the data as unavailable and the
    backend returns the clean "run `lean-explore data fetch`" message instead of erroring
    on every query. Best-effort: returns the quarantine path, or ``None`` if there was
    nothing to move or the move failed.
    """
    try:
        cache_path = _leanexplore_local_cache_path()
        if cache_path is None:
            return None
        db_path = cache_path / "lean_explore.db"
        if not db_path.is_file():
            return None
        target = db_path.parent / (db_path.name + ".corrupt")
        index = 1
        while target.exists():
            target = db_path.parent / (f"{db_path.name}.corrupt{index}")
            index += 1
        db_path.rename(target)
        return target
    except Exception:
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
