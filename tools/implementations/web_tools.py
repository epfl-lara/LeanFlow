#!/usr/bin/env python3
"""Search public research sources and extract bounded page text without model calls.

Search uses key-free public backends. Optional Firecrawl extraction returns
verbatim excerpts; each native runtime owns any interpretation or summarization.
Set WEB_TOOLS_DEBUG=true for retrieval diagnostics.
"""

# TODO: Search Capabilities over the scraped pages
# TODO: Store the pages in something
# TODO: Tool to see what pages are available/saved to search over

import json
import logging
import os
import re
import time
from typing import Any

from tools.implementations.web_research_providers import (  # noqa: F401
    ARXIV_API_URL,
    BING_SEARCH_URL,
    CODE_SEARCH_STOPWORDS,
    CROSSREF_SEARCH_URL,
    DUCKDUCKGO_HTML_URL,
    EXA_SEARCH_URL,
    GITHUB_REPOSITORY_SEARCH_URL,
    RESEARCH_SEARCH_TIMEOUT_SECONDS,
    RESEARCH_SEARCH_USER_AGENT,
    SEMANTIC_SCHOLAR_SEARCH_URL,
    SOURCEGRAPH_GRAPHQL_URL,
    SOURCEGRAPH_SEARCH_TIMEOUT_SECONDS,
    TAVILY_SEARCH_URL,
    _append_unique_result,
    _arxiv_search_query,
    _bounded_limit,
    _crossref_authors,
    _crossref_year,
    _normalize_whitespace,
    _research_headers,
    _search_arxiv,
    _search_bing_html,
    _search_crossref,
    _search_duckduckgo_html,
    _search_exa,
    _search_general_web,
    _search_github_repositories,
    _search_semantic_scholar,
    _search_sourcegraph_code,
    _search_tavily,
    _sourcegraph_code_terms,
    _sourcegraph_queries,
    _truncate_text,
    _web_search_provider_order,
)
from tools.implementations.web_search_orchestration import (
    degraded_reasons,
    filter_provider_batches,
    merge_provider_batches,
    normalize_search_queries,
    provider_status,
    run_provider_searches,
    searched_provider_names,
)
from tools.response import dumps, error
from tools.utilities.debug_helpers import DebugSession
from tools.utilities.repository_research_policy import (
    is_repository_url,
    repository_research_disabled,
    repository_url_block_reason,
    solution_research_query_block_reason,
    solution_research_text_block_reason,
    solution_research_url_block_reason,
)

try:
    from firecrawl import Firecrawl
except ModuleNotFoundError:
    Firecrawl = None

logger = logging.getLogger(__name__)

_firecrawl_client = None


def _get_firecrawl_client():
    """Get or create the Firecrawl client (lazy initialization).

    Uses the cloud API by default (requires FIRECRAWL_API_KEY).
    Set FIRECRAWL_API_URL to point at a self-hosted instance instead —
    in that case the API key is optional (set USE_DB_AUTHENTICATION=false
    on your Firecrawl server to disable auth entirely).
    """
    global _firecrawl_client
    if _firecrawl_client is None:
        if Firecrawl is None:
            raise ValueError("firecrawl package not installed. Install it to use the web tools.")
        api_key = os.getenv("FIRECRAWL_API_KEY")
        api_url = os.getenv("FIRECRAWL_API_URL")
        if not api_key and not api_url:
            raise ValueError(
                "FIRECRAWL_API_KEY environment variable not set. "
                "Set it for cloud Firecrawl, or set FIRECRAWL_API_URL "
                "to use a self-hosted instance."
            )
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if api_url:
            kwargs["api_url"] = api_url
        _firecrawl_client = Firecrawl(**kwargs)
    return _firecrawl_client


MAX_EXTRACT_CHARS = 5000

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


def clean_base64_images(text: str) -> str:
    """
    Remove base64 encoded images from text to reduce token count and clutter.

    This function finds and removes base64 encoded images in various formats:
    - (data:image/png;base64,...)
    - (data:image/jpeg;base64,...)
    - (data:image/svg+xml;base64,...)
    - data:image/[type];base64,... (without parentheses)

    Args:
        text: The text content to clean

    Returns:
        Cleaned text with base64 images replaced with placeholders
    """
    # Pattern to match base64 encoded images wrapped in parentheses
    # Matches: (data:image/[type];base64,[base64-string])
    base64_with_parens_pattern = r"\(data:image/[^;]+;base64,[A-Za-z0-9+/=]+\)"

    # Pattern to match base64 encoded images without parentheses
    # Matches: data:image/[type];base64,[base64-string]
    base64_pattern = r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+"

    # Replace parentheses-wrapped images first
    cleaned_text = re.sub(base64_with_parens_pattern, "[BASE64_IMAGE_REMOVED]", text)

    # Then replace any remaining non-parentheses images
    cleaned_text = re.sub(base64_pattern, "[BASE64_IMAGE_REMOVED]", cleaned_text)

    return cleaned_text


def web_search_tool(
    query: str,
    limit: int = 5,
    search_depth: str = "auto",
    alternate_queries: list[str] | None = None,
) -> str:
    """
    Search free public research sources without requiring paid keys.

    This is for external research: papers, source references, docs, public Lean
    examples outside the local project, Coq/Rocq examples, and general
    mathematical context. For local project/mathlib theorem lookup, the model
    should use lean_search first.

    Args:
        query (str): The search query to look up
        limit (int): Maximum number of results to return (default: 5)
        search_depth (str): ``fast``, ``auto``, or ``deep`` provider breadth.
        alternate_queries (list[str] | None): Up to three additional formulations searched
            concurrently and merged with the primary query.

    Returns:
        str: JSON string containing search results with the following structure:
             {
                 "success": bool,
                 "data": {
                     "web": [
                         {"provider": str, "kind": str, "title": str,
                          "url": str, "snippet": str, "position": int},
                         ...
                     ]
                 },
                 "degraded_reasons": [...]
             }
    """
    debug_call_data: dict[str, Any] = {
        "parameters": {
            "query": query,
            "limit": limit,
            "search_depth": search_depth,
            "alternate_queries": list(alternate_queries or ()),
        },
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0,
    }

    try:
        started = time.perf_counter()
        from tools.utilities.interrupt import is_interrupted

        if is_interrupted():
            return json.dumps({"error": "Interrupted", "success": False})
        queries = normalize_search_queries(query, alternate_queries)
        if not queries:
            return dumps({"success": False, "error": "web_search requires a non-empty query"})
        for candidate_query in queries:
            solution_denial = solution_research_query_block_reason(candidate_query)
            if solution_denial:
                return dumps(
                    {
                        "success": False,
                        "error": solution_denial,
                        "status": "clean_room_solution_research_denied",
                        "query": candidate_query,
                        "queries": list(queries),
                    }
                )

        normalized_limit = _bounded_limit(limit)
        normalized_depth = str(search_depth or "auto").strip().lower()
        if normalized_depth not in {"auto", "fast", "deep"}:
            normalized_depth = "auto"
        logger.info(
            "Searching free research providers for %d query formulation(s) "
            "(depth: %s, limit: %d)",
            len(queries),
            normalized_depth,
            normalized_limit,
        )

        per_provider_limit = max(2, min(5, normalized_limit))
        provider_orders = []
        for candidate_query in queries:
            provider_order = _web_search_provider_order(candidate_query)
            if repository_research_disabled():
                provider_order = tuple(
                    search_fn
                    for search_fn in provider_order
                    if search_fn is not _search_sourcegraph_code
                )
            if normalized_depth == "fast" and len(provider_order) > 2:
                provider_order = (provider_order[0], provider_order[-1])
            provider_orders.append(provider_order)

        raw_batches = run_provider_searches(
            queries,
            provider_orders,
            per_provider_limit=per_provider_limit,
        )

        def result_allowed(result: dict[str, Any]) -> bool:
            if repository_research_disabled() and (
                is_repository_url(str(result.get("url", "") or ""))
                or str(result.get("provider", "") or "").lower() == "sourcegraph"
            ):
                return False
            result_text = " ".join(
                str(result.get(key, "") or "") for key in ("title", "url", "snippet")
            )
            return not solution_research_text_block_reason(
                result_text,
                surface="search result",
            )

        batches = filter_provider_batches(raw_batches, result_allowed)
        provider_results = merge_provider_batches(
            batches,
            queries=queries,
            limit=normalized_limit,
        )
        failures = degraded_reasons(batches)
        statuses = provider_status(batches)

        results_count = len(provider_results)
        logger.info("Found %d free research results", results_count)

        response_data = {
            "success": bool(provider_results),
            "status": "complete" if provider_results else "no_results",
            "retryable": not provider_results,
            "query": query,
            "queries": list(queries),
            "search_depth": normalized_depth,
            "data": {"web": provider_results},
            "providers_tried": searched_provider_names(batches),
            "provider_status": statuses,
            "degraded_reasons": failures,
            "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
        }

        # Surface degraded backends to the model: results may be incomplete, and
        # web_fetch on a known URL is the reliable fallback for a thin result set.
        if failures:
            response_data["degraded"] = (
                "Some search backends were degraded ("
                + "; ".join(failures)
                + "); results may be incomplete. Continue with surviving sources, rephrase with "
                "alternate_queries, and use web_fetch on promising URLs before concluding that "
                "research is exhausted."
            )
        elif not provider_results:
            response_data["degraded"] = (
                "No relevant sources were returned for this formulation. This is retryable: use "
                "materially different alternate_queries or a known primary URL; do not treat one "
                "empty search as exhausted research."
            )

        # Capture debug information
        debug_call_data["results_count"] = results_count
        debug_call_data["provider_status"] = statuses
        debug_call_data["queries"] = list(queries)
        debug_call_data["search_depth"] = normalized_depth

        # Convert to JSON
        result_json = dumps(response_data, indent=2)

        debug_call_data["final_response_size"] = len(result_json)

        # Log debug information
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return result_json

    except Exception as exc:
        error_msg = f"Error searching web: {str(exc)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return dumps({"success": False, "error": error_msg})


async def web_extract_tool(
    urls: list[str],
    format: str | None = None,
    use_llm_processing: bool = False,
    model: str | None = None,
    min_length: int = 5000,
) -> str:
    """Fetch page text through Firecrawl and return bounded verbatim excerpts.

    Legacy summarization arguments are ignored; native runtimes own reasoning
    about retrieved content. This tool performs no model invocation.
    """
    debug_call_data: dict[str, Any] = {
        "parameters": {
            "urls": urls,
            "format": format,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length,
        },
        "error": None,
        "pages_extracted": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "processing_applied": [],
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(urls))
        for url in urls:
            blocked = repository_url_block_reason(url) or solution_research_url_block_reason(url)
            if blocked:
                return error(blocked)

        # Determine requested formats for Firecrawl v2
        formats: list[str] = []
        if format == "markdown":
            formats = ["markdown"]
        elif format == "html":
            formats = ["html"]
        else:
            # Default: request markdown for LLM-readiness and include html as backup
            formats = ["markdown", "html"]

        # Always use individual scraping for simplicity and reliability
        # Batch scraping adds complexity without much benefit for small numbers of URLs
        results: list[dict[str, Any]] = []

        from tools.utilities.interrupt import is_interrupted as _is_interrupted

        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "error": "Interrupted", "title": ""})
                continue

            try:
                logger.info("Scraping: %s", url)
                scrape_result = _get_firecrawl_client().scrape(url=url, formats=formats)

                # Process the result - properly handle object serialization
                metadata = {}
                title = ""
                content_markdown = None
                content_html = None

                # Extract data from the scrape result
                if hasattr(scrape_result, "model_dump"):
                    # Pydantic model - use model_dump to get dict
                    result_dict = scrape_result.model_dump()
                    content_markdown = result_dict.get("markdown")
                    content_html = result_dict.get("html")
                    metadata = result_dict.get("metadata", {})
                elif hasattr(scrape_result, "__dict__"):
                    # Regular object with attributes
                    content_markdown = getattr(scrape_result, "markdown", None)
                    content_html = getattr(scrape_result, "html", None)

                    # Handle metadata - convert to dict if it's an object
                    metadata_obj = getattr(scrape_result, "metadata", {})
                    if hasattr(metadata_obj, "model_dump"):
                        metadata = metadata_obj.model_dump()
                    elif hasattr(metadata_obj, "__dict__"):
                        metadata = metadata_obj.__dict__
                    elif isinstance(metadata_obj, dict):
                        metadata = metadata_obj
                    else:
                        metadata = {}
                elif isinstance(scrape_result, dict):
                    # Already a dictionary
                    content_markdown = scrape_result.get("markdown")
                    content_html = scrape_result.get("html")
                    metadata = scrape_result.get("metadata", {})

                # Ensure metadata is a dict (not an object)
                if not isinstance(metadata, dict):
                    if hasattr(metadata, "model_dump"):
                        metadata = metadata.model_dump()
                    elif hasattr(metadata, "__dict__"):
                        metadata = metadata.__dict__
                    else:
                        metadata = {}

                # Get title from metadata
                title = metadata.get("title", "")

                # Choose content based on requested format
                chosen_content = (
                    content_markdown
                    if (format == "markdown" or (format is None and content_markdown))
                    else content_html or content_markdown or ""
                )

                results.append(
                    {
                        "url": metadata.get("sourceURL", url),
                        "title": title,
                        "content": chosen_content,
                        "raw_content": chosen_content,
                        "metadata": metadata,  # Now guaranteed to be a dict
                    }
                )

            except Exception as scrape_err:
                logger.debug("Scrape failed for %s: %s", url, scrape_err)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": str(scrape_err),
                    }
                )

        response = {"results": results}

        pages_extracted = len(response.get("results", []))
        logger.info("Extracted content from %d pages", pages_extracted)

        debug_call_data["pages_extracted"] = pages_extracted
        debug_call_data["original_response_size"] = len(json.dumps(response))

        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", "")[:MAX_EXTRACT_CHARS],
                "truncated": len(r.get("content", "")) > MAX_EXTRACT_CHARS,
                "error": r.get("error"),
            }
            for r in response.get("results", [])
        ]
        trimmed_response = {"results": trimmed_results}

        if trimmed_response.get("results") == []:
            result_json = error("Content was inaccessible or not found")

            cleaned_result = clean_base64_images(result_json)

        else:
            result_json = dumps(trimmed_response, indent=2)

            cleaned_result = clean_base64_images(result_json)

        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_removal")

        # Log debug information
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()

        return cleaned_result

    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()

        return error(error_msg)


# Convenience function to check if API key is available
def check_firecrawl_api_key() -> bool:
    """
    Check if Firecrawl is configured in environment variables.

    Cloud Firecrawl requires FIRECRAWL_API_KEY. A self-hosted Firecrawl
    instance can be configured with FIRECRAWL_API_URL and no key.

    Returns:
        bool: True if Firecrawl has enough configuration to run, False otherwise
    """
    return bool(os.getenv("FIRECRAWL_API_KEY") or os.getenv("FIRECRAWL_API_URL"))


def check_research_search_available() -> bool:
    """Free research search is available without paid keys or configured services."""
    return True


def check_auxiliary_model() -> bool:
    """Report that LeanFlow no longer runs auxiliary models for web retrieval."""
    return False


def get_debug_session_info() -> dict[str, Any]:
    """Get information about the current debug session."""
    return _debug.get_session_info()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
import requests  # noqa: F401

from tools.registry import registry

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Fast, source-attributed external research across the open web (Tavily / Exa / DuckDuckGo / Bing), code "
        "and repositories (Sourcegraph / GitHub), and papers/related work (arXiv, Semantic Scholar, Crossref). Independent backends "
        "run concurrently and fail independently. Use alternate_queries with search_depth=deep to search "
        "several formulations in one call; results are relevance-ranked, source-diversified, and deduplicated "
        "across providers with matched-query provenance. Use it for anything outside the local project: "
        "background, current documentation, installation, similar results, prior formalizations, lemma "
        "references, and source examples. Pair it with web_fetch to READ promising sources—search snippets "
        "alone are not evidence—and web_download/repo_clone for concrete artifacts. If a backend degrades, "
        "continue with surviving sources and alternate formulations before declaring research exhausted. "
        "A no_results response is explicitly retryable and must not end a research branch by itself. "
        "Still prefer lean_search FIRST for local project facts, mathlib declarations, theorem names, "
        "type-pattern matching, and proof hints."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query to look up on the web"},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5, max 10).",
                "minimum": 1,
                "maximum": 10,
            },
            "search_depth": {
                "type": "string",
                "enum": ["fast", "auto", "deep"],
                "description": (
                    "fast uses a narrow provider route; auto balances breadth and latency "
                    "(default); deep is intended for a multi-formulation search portfolio."
                ),
            },
            "alternate_queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
                "maxItems": 3,
                "description": (
                    "Up to three materially different formulations to search concurrently "
                    "and merge with the primary query."
                ),
            },
        },
        "required": ["query"],
    },
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns page content in markdown format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass the PDF link directly and it converts to markdown text. Returns up to 5000 characters of verbatim text per page and marks truncated pages. No model calls are made.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5,
            }
        },
        "required": ["urls"],
    },
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(
        args.get("query", ""),
        limit=args.get("limit", 5),
        search_depth=args.get("search_depth", "auto"),
        alternate_queries=(
            args.get("alternate_queries", [])[:3]
            if isinstance(args.get("alternate_queries"), list)
            else []
        ),
    ),
    check_fn=check_research_search_available,
    requires_env=[],
    emoji="🔍",
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown"
    ),
    check_fn=check_firecrawl_api_key,
    requires_env=[],
    is_async=True,
    emoji="📄",
)
