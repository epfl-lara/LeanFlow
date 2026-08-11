"""Run web-search providers concurrently and merge citation-ready evidence."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SearchProvider = Callable[[str, int], tuple[list[dict[str, Any]], str]]
ResultPredicate = Callable[[dict[str, Any]], bool]

_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
    }
)
_ARXIV_PATH_RE = re.compile(r"^/(?:abs|pdf)/([^/?#]+?)(?:\.pdf)?$")
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class ProviderBatch:
    """Hold one provider/query outcome with deterministic merge coordinates."""

    query_index: int
    provider_index: int
    query: str
    provider_name: str
    results: tuple[dict[str, Any], ...]
    error: str
    duration_ms: int


def normalize_search_queries(
    query: str,
    alternate_queries: Sequence[str] | None,
    *,
    maximum: int = 4,
) -> tuple[str, ...]:
    """Return a bounded, order-preserving portfolio of distinct search queries."""
    queries: list[str] = []
    seen: set[str] = set()
    for raw in (query, *(alternate_queries or ())):
        candidate = str(raw or "").strip()
        key = candidate.casefold()
        if not candidate or key in seen:
            continue
        queries.append(candidate)
        seen.add(key)
        if len(queries) >= maximum:
            break
    return tuple(queries)


def _provider_name(provider: SearchProvider) -> str:
    name = str(getattr(provider, "__name__", "") or provider.__class__.__name__)
    return name.removeprefix("_search_").replace("_", "-")


def _search_concurrency(job_count: int) -> int:
    try:
        requested = int(os.getenv("LEANFLOW_WEB_SEARCH_CONCURRENCY", "8") or "8")
    except ValueError:
        requested = 8
    return max(1, min(job_count, 16, requested))


def _run_one(
    query_index: int,
    provider_index: int,
    query: str,
    provider: SearchProvider,
    limit: int,
) -> ProviderBatch:
    started = time.perf_counter()
    provider_name = _provider_name(provider)
    try:
        raw_results, raw_error = provider(query, limit)
        results = tuple(dict(item) for item in (raw_results or ()) if isinstance(item, dict))
        error = str(raw_error or "").strip()
    except Exception as exc:
        results = ()
        error = f"{provider_name} search failed: {type(exc).__name__}: {exc}"
    return ProviderBatch(
        query_index=query_index,
        provider_index=provider_index,
        query=query,
        provider_name=provider_name,
        results=results,
        error=error,
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
    )


def run_provider_searches(
    queries: Sequence[str],
    provider_orders: Sequence[Sequence[SearchProvider]],
    *,
    per_provider_limit: int,
) -> tuple[ProviderBatch, ...]:
    """Run independent provider/query requests concurrently and return stable-order batches."""
    jobs = [
        (query_index, provider_index, query, provider)
        for query_index, (query, providers) in enumerate(zip(queries, provider_orders, strict=True))
        for provider_index, provider in enumerate(providers)
    ]
    if not jobs:
        return ()

    batches: list[ProviderBatch] = []
    with ThreadPoolExecutor(
        max_workers=_search_concurrency(len(jobs)),
        thread_name_prefix="leanflow-web-search",
    ) as executor:
        futures = [
            executor.submit(
                _run_one,
                query_index,
                provider_index,
                query,
                provider,
                per_provider_limit,
            )
            for query_index, provider_index, query, provider in jobs
        ]
        for future in as_completed(futures):
            batches.append(future.result())
    return tuple(sorted(batches, key=lambda item: (item.query_index, item.provider_index)))


def filter_provider_batches(
    batches: Sequence[ProviderBatch],
    predicate: ResultPredicate,
) -> tuple[ProviderBatch, ...]:
    """Return batches whose result records satisfy a shared policy predicate."""
    return tuple(
        replace(batch, results=tuple(item for item in batch.results if predicate(item)))
        for batch in batches
    )


def degraded_reasons(batches: Sequence[ProviderBatch]) -> list[str]:
    """Return distinct provider failures in deterministic query/provider order."""
    reasons: list[str] = []
    for batch in batches:
        if batch.error and batch.error not in reasons:
            reasons.append(batch.error)
    return reasons


def provider_status(batches: Sequence[ProviderBatch]) -> list[dict[str, Any]]:
    """Aggregate provider latency, result counts, and failures for audit logs."""
    grouped: dict[str, dict[str, Any]] = {}
    for batch in batches:
        record = grouped.setdefault(
            batch.provider_name,
            {
                "provider": batch.provider_name,
                "status": "empty",
                "queries": 0,
                "results": 0,
                "max_latency_ms": 0,
                "errors": [],
            },
        )
        record["queries"] += 1
        record["results"] += len(batch.results)
        record["max_latency_ms"] = max(record["max_latency_ms"], batch.duration_ms)
        if batch.error and batch.error not in record["errors"]:
            record["errors"].append(batch.error)
    for record in grouped.values():
        has_results = bool(record["results"])
        has_errors = bool(record["errors"])
        record["status"] = (
            "degraded"
            if has_results and has_errors
            else "ok" if has_results else "error" if has_errors else "empty"
        )
        if not record["errors"]:
            record.pop("errors")
    return list(grouped.values())


def _normalized_arxiv_id(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    candidate = candidate.removesuffix(".pdf")
    return _ARXIV_VERSION_RE.sub("", candidate)


def _canonical_result_key(result: dict[str, Any]) -> str:
    arxiv_id = _normalized_arxiv_id(result.get("arxiv_id"))
    external_ids = result.get("external_ids")
    if isinstance(external_ids, dict):
        arxiv_id = arxiv_id or _normalized_arxiv_id(external_ids.get("ArXiv"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"

    doi = str(result.get("doi") or "").strip().lower()
    if isinstance(external_ids, dict):
        doi = doi or str(external_ids.get("DOI") or "").strip().lower()
    if doi:
        return f"doi:{doi.removeprefix('https://doi.org/').removeprefix('doi:')}"

    url = str(result.get("url") or "").strip()
    if url:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if hostname in {"arxiv.org", "export.arxiv.org"}:
            match = _ARXIV_PATH_RE.match(parsed.path)
            if match:
                return f"arxiv:{_normalized_arxiv_id(match.group(1))}"
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
        ]
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit(
            (
                parsed.scheme.lower() or "https",
                parsed.netloc.lower(),
                path,
                urlencode(query, doseq=True),
                "",
            )
        )
    return "text:" + "|".join(
        str(result.get(key) or "").strip().casefold() for key in ("provider", "title", "snippet")
    )


def _query_terms(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_.+-]+", query.casefold())
        if len(token) > 1 and token not in _QUERY_STOPWORDS
    }


def _relevance_score(
    result: dict[str, Any],
    *,
    queries: Sequence[str],
    matched_queries: Sequence[str],
    provider_index: int,
    result_index: int,
) -> float:
    title = str(result.get("title") or "").casefold()
    snippet = str(result.get("snippet") or "").casefold()
    best = 0.0
    for query in queries:
        terms = _query_terms(query)
        if not terms:
            continue
        title_hits = sum(term in title for term in terms) / len(terms)
        snippet_hits = sum(term in snippet for term in terms) / len(terms)
        normalized_query = " ".join(query.casefold().split())
        score = (6.0 * title_hits) + (2.5 * snippet_hits)
        if len(normalized_query) >= 6 and normalized_query in title:
            score += 6.0
        elif len(normalized_query) >= 6 and normalized_query in snippet:
            score += 3.0
        best = max(best, score)

    lowered_queries = " ".join(queries).casefold()
    kind = str(result.get("kind") or "").casefold()
    if kind == "code" and any(
        marker in lowered_queries for marker in (".lean", ".v", " code", "lemma", "theorem")
    ):
        best += 2.5
    if kind == "paper" and any(
        marker in lowered_queries
        for marker in ("paper", "article", "arxiv", "doi", "formal proof", "literature")
    ):
        best += 2.5
    if kind == "web" and any(
        marker in lowered_queries
        for marker in ("docs", "documentation", "install", "latest", "release", "api")
    ):
        best += 2.5
    if queries and queries[0] in matched_queries:
        best += 0.5
    return best - (0.08 * result_index) - (0.01 * provider_index)


def _result_domain(result: dict[str, Any]) -> str:
    url = str(result.get("url") or "").strip()
    return (urlsplit(url).hostname or "").lower()


def merge_provider_batches(
    batches: Sequence[ProviderBatch],
    *,
    queries: Sequence[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Deduplicate, relevance-rank, diversify, and identify search evidence."""
    merged: dict[str, dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for result_index, raw_result in enumerate(batch.results):
            result = dict(raw_result)
            key = _canonical_result_key(result)
            provider = str(result.get("provider") or batch.provider_name).strip()
            if key not in merged:
                merged[key] = result
                metadata[key] = {
                    "matched_queries": [batch.query],
                    "source_providers": [provider] if provider else [],
                    "provider_index": batch.provider_index,
                    "result_index": result_index,
                }
                continue
            record = metadata[key]
            if batch.query not in record["matched_queries"]:
                record["matched_queries"].append(batch.query)
            if provider and provider not in record["source_providers"]:
                record["source_providers"].append(provider)
            current = merged[key]
            if len(str(result.get("snippet") or "")) > len(str(current.get("snippet") or "")):
                current["snippet"] = result.get("snippet")
            for field in ("authors", "year", "pdf_url", "doi", "arxiv_id", "external_ids"):
                if not current.get(field) and result.get(field):
                    current[field] = result[field]

    candidates: list[tuple[str, dict[str, Any], dict[str, Any], float]] = []
    for key, result in merged.items():
        item_meta = metadata[key]
        score = _relevance_score(
            result,
            queries=queries,
            matched_queries=item_meta["matched_queries"],
            provider_index=item_meta["provider_index"],
            result_index=item_meta["result_index"],
        )
        candidates.append((key, result, item_meta, score))

    remaining = sorted(
        candidates,
        key=lambda item: (
            -item[3],
            item[2]["provider_index"],
            item[2]["result_index"],
            item[0],
        ),
    )
    selected: list[tuple[str, dict[str, Any], dict[str, Any], float]] = []
    provider_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    while remaining and len(selected) < limit:
        best_index = max(
            range(len(remaining)),
            key=lambda index: (
                remaining[index][3]
                - (1.25 * provider_counts.get(str(remaining[index][1].get("provider") or ""), 0))
                - (0.75 * domain_counts.get(_result_domain(remaining[index][1]), 0)),
                -remaining[index][2]["provider_index"],
                -remaining[index][2]["result_index"],
            ),
        )
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        provider = str(chosen[1].get("provider") or "")
        domain = _result_domain(chosen[1])
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        if domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

    results: list[dict[str, Any]] = []
    for position, (_key, raw_result, item_meta, _score) in enumerate(selected, start=1):
        result = dict(raw_result)
        result["position"] = position
        result["source_id"] = f"web-{position}"
        result["matched_queries"] = list(item_meta["matched_queries"])
        if len(item_meta["source_providers"]) > 1:
            result["source_providers"] = list(item_meta["source_providers"])
        results.append(result)
    return results


def searched_provider_names(batches: Iterable[ProviderBatch]) -> list[str]:
    """Return the distinct provider-route names attempted in stable order."""
    names: list[str] = []
    for batch in batches:
        if batch.provider_name not in names:
            names.append(batch.provider_name)
    return names
