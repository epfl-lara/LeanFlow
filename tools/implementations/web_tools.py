#!/usr/bin/env python3
"""
Standalone Web Tools Module

This module provides research and extraction tools that work with multiple
backend providers. Search uses free public providers by default. Firecrawl is
only used for optional extraction/crawling when explicitly configured.

Available tools:
- web_search_tool: Search free public research providers
- web_extract_tool: Extract content from specific web pages

Backend compatibility:
- arXiv API: https://info.arxiv.org/help/api/
- Semantic Scholar Graph API: https://api.semanticscholar.org/api-docs/graph
- Sourcegraph public code search: https://sourcegraph.com/docs/code-search
- Exa search API: https://exa.ai/docs/reference/search
- Firecrawl: https://docs.firecrawl.dev/introduction

LLM Processing:
- Uses OpenRouter API with Gemini 3 Flash Preview for intelligent content extraction
- Extracts key excerpts and creates markdown summaries to reduce token usage

Debug Mode:
- Set WEB_TOOLS_DEBUG=true to enable detailed logging
- Creates web_tools_debug_UUID.json in ./logs directory
- Captures all tool calls, results, and compression metrics

Usage:
    from web_tools import web_search_tool, web_extract_tool

    # Search external research sources
    results = web_search_tool("prime number theorem formalization Lean", limit=3)

    # Extract content from URLs
    content = web_extract_tool(["https://example.com"], format="markdown")

"""

# TODO: Search Capabilities over the scraped pages
# TODO: Store the pages in something
# TODO: Tool to see what pages are available/saved to search over

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from agent.providers.auxiliary_client import async_call_llm
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


DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000
SUMMARIZER_TIMEOUT_SECONDS = 30.0

# Allow per-task override via env var
DEFAULT_SUMMARIZER_MODEL = os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip() or None

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


async def process_content_with_llm(
    content: str,
    url: str = "",
    title: str = "",
    model: str = DEFAULT_SUMMARIZER_MODEL,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
) -> str | None:
    """
    Process web content using LLM to create intelligent summaries with key excerpts.

    This function uses Gemini 3 Flash Preview (or specified model) via OpenRouter API
    to intelligently extract key information and create markdown summaries,
    significantly reducing token usage while preserving all important information.

    For very large content (>500k chars), uses chunked processing with synthesis.
    For extremely large content (>2M chars), refuses to process entirely.

    Args:
        content (str): The raw content to process
        url (str): The source URL (for context, optional)
        title (str): The page title (for context, optional)
        model (str): The model to use for processing (default: google/gemini-3-flash-preview)
        min_length (int): Minimum content length to trigger processing (default: 5000)

    Returns:
        Optional[str]: Processed markdown content, or None if content too short or processing fails
    """
    # Size thresholds
    MAX_CONTENT_SIZE = 2_000_000  # 2M chars - refuse entirely above this
    CHUNK_THRESHOLD = 500_000  # 500k chars - use chunked processing above this
    CHUNK_SIZE = 100_000  # 100k chars per chunk
    MAX_OUTPUT_SIZE = 5000  # Hard cap on final output size

    try:
        content_len = len(content)

        # Refuse if content is absurdly large
        if content_len > MAX_CONTENT_SIZE:
            size_mb = content_len / 1_000_000
            logger.warning("Content too large (%.1fMB > 2MB limit). Refusing to process.", size_mb)
            return f"[Content too large to process: {size_mb:.1f}MB. Search for a more focused source, or fetch a specific sub-page.]"

        # Skip processing if content is too short
        if content_len < min_length:
            logger.debug(
                "Content too short (%d < %d chars), skipping LLM processing",
                content_len,
                min_length,
            )
            return None

        # Create context information
        context_info = []
        if title:
            context_info.append(f"Title: {title}")
        if url:
            context_info.append(f"Source: {url}")
        context_str = "\n".join(context_info) + "\n\n" if context_info else ""

        # Check if we need chunked processing
        if content_len > CHUNK_THRESHOLD:
            logger.info("Content large (%d chars). Using chunked processing...", content_len)
            return await _process_large_content_chunked(
                content, context_str, model, CHUNK_SIZE, MAX_OUTPUT_SIZE
            )

        # Standard single-pass processing for normal content
        logger.info("Processing content with LLM (%d characters)", content_len)

        processed_content = await _call_summarizer_llm(content, context_str, model)

        if processed_content:
            # Enforce output cap
            if len(processed_content) > MAX_OUTPUT_SIZE:
                processed_content = (
                    processed_content[:MAX_OUTPUT_SIZE]
                    + "\n\n[... summary truncated for context management ...]"
                )

            # Log compression metrics
            processed_length = len(processed_content)
            compression_ratio = processed_length / content_len if content_len > 0 else 1.0
            logger.info(
                "Content processed: %d -> %d chars (%.1f%%)",
                content_len,
                processed_length,
                compression_ratio * 100,
            )

        return processed_content

    except Exception as e:
        logger.debug("Error processing content with LLM: %s", e)
        return f"[Failed to process content: {str(e)[:100]}. Content size: {len(content):,} chars]"


async def _call_summarizer_llm(
    content: str,
    context_str: str,
    model: str,
    max_tokens: int = 20000,
    is_chunk: bool = False,
    chunk_info: str = "",
) -> str | None:
    """
    Make a single LLM call to summarize content.

    Args:
        content: The content to summarize
        context_str: Context information (title, URL)
        model: Model to use
        max_tokens: Maximum output tokens
        is_chunk: Whether this is a chunk of a larger document
        chunk_info: Information about chunk position (e.g., "Chunk 2/5")

    Returns:
        Summarized content or None on failure
    """
    if is_chunk:
        # Chunk-specific prompt - aware that this is partial content
        system_prompt = """You are an expert content analyst processing a SECTION of a larger document. Your job is to extract and summarize the key information from THIS SECTION ONLY.

Important guidelines for chunk processing:
1. Do NOT write introductions or conclusions - this is a partial document
2. Focus on extracting ALL key facts, figures, data points, and insights from this section
3. Preserve important quotes, code snippets, and specific details verbatim
4. Use bullet points and structured formatting for easy synthesis later
5. Note any references to other sections (e.g., "as mentioned earlier", "see below") without trying to resolve them

Your output will be combined with summaries of other sections, so focus on thorough extraction rather than narrative flow."""

        user_prompt = f"""Extract key information from this SECTION of a larger document:

{context_str}{chunk_info}

SECTION CONTENT:
{content}

Extract all important information from this section in a structured format. Focus on facts, data, insights, and key details. Do not add introductions or conclusions."""

    else:
        # Standard full-document prompt
        system_prompt = """You are an expert content analyst. Your job is to process web content and create a comprehensive yet concise summary that preserves all important information while dramatically reducing bulk.

Create a well-structured markdown summary that includes:
1. Key excerpts (quotes, code snippets, important facts) in their original format
2. Comprehensive summary of all other important information
3. Proper markdown formatting with headers, bullets, and emphasis

Your goal is to preserve ALL important information while reducing length. Never lose key facts, figures, insights, or actionable information. Make it scannable and well-organized."""

        user_prompt = f"""Please process this web content and create a comprehensive markdown summary:

{context_str}CONTENT TO PROCESS:
{content}

Create a markdown summary that captures all key information in a well-organized, scannable format. Include important quotes and code snippets in their original formatting. Focus on actionable information, specific details, and unique insights."""

    # Page extraction is an optional context-reduction step. Give it one true
    # bounded attempt, then return the original hard-capped content. Stacking
    # six retries here on top of provider retries used to freeze a concurrent
    # planner web batch for many minutes.
    max_retries = 1
    retry_delay = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            call_kwargs = {
                "task": "web_extract",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "timeout": SUMMARIZER_TIMEOUT_SECONDS,
            }
            if model:
                call_kwargs["model"] = model
            response = await async_call_llm(**call_kwargs)
            return response.choices[0].message.content.strip()
        except RuntimeError:
            logger.warning("No auxiliary model available for web content processing")
            return None
        except Exception as api_error:
            last_error = api_error
            if attempt < max_retries - 1:
                logger.warning(
                    "LLM API call failed (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    str(api_error)[:100],
                )
                logger.warning("Retrying in %ds...", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                raise last_error

    return None


async def _process_large_content_chunked(
    content: str, context_str: str, model: str, chunk_size: int, max_output_size: int
) -> str | None:
    """
    Process large content by chunking, summarizing each chunk in parallel,
    then synthesizing the summaries.

    Args:
        content: The large content to process
        context_str: Context information
        model: Model to use
        chunk_size: Size of each chunk in characters
        max_output_size: Maximum final output size

    Returns:
        Synthesized summary or None on failure
    """
    # Split content into chunks
    chunks = []
    for i in range(0, len(content), chunk_size):
        chunk = content[i : i + chunk_size]
        chunks.append(chunk)

    logger.info("Split into %d chunks of ~%d chars each", len(chunks), chunk_size)

    # Summarize each chunk in parallel
    async def summarize_chunk(chunk_idx: int, chunk_content: str) -> tuple[int, str | None]:
        """Summarize a single chunk."""
        try:
            chunk_info = f"[Processing chunk {chunk_idx + 1} of {len(chunks)}]"
            summary = await _call_summarizer_llm(
                chunk_content,
                context_str,
                model,
                max_tokens=10000,
                is_chunk=True,
                chunk_info=chunk_info,
            )
            if summary:
                logger.info(
                    "Chunk %d/%d summarized: %d -> %d chars",
                    chunk_idx + 1,
                    len(chunks),
                    len(chunk_content),
                    len(summary),
                )
            return chunk_idx, summary
        except Exception as e:
            logger.warning("Chunk %d/%d failed: %s", chunk_idx + 1, len(chunks), str(e)[:50])
            return chunk_idx, None

    # Run all chunk summarizations in parallel
    tasks = [summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks)

    # Collect successful summaries in order
    summaries = []
    for chunk_idx, summary in sorted(results, key=lambda x: x[0]):
        if summary:
            summaries.append(f"## Section {chunk_idx + 1}\n{summary}")

    if not summaries:
        logger.debug("All chunk summarizations failed")
        return "[Failed to process large content: all chunk summarizations failed]"

    logger.info("Got %d/%d chunk summaries", len(summaries), len(chunks))

    # If only one chunk succeeded, just return it (with cap)
    if len(summaries) == 1:
        result = summaries[0]
        if len(result) > max_output_size:
            result = result[:max_output_size] + "\n\n[... truncated ...]"
        return result

    # Synthesize the summaries into a final summary
    logger.info("Synthesizing %d summaries...", len(summaries))

    combined_summaries = "\n\n---\n\n".join(summaries)

    synthesis_prompt = f"""You have been given summaries of different sections of a large document. 
Synthesize these into ONE cohesive, comprehensive summary that:
1. Removes redundancy between sections
2. Preserves all key facts, figures, and actionable information
3. Is well-organized with clear structure
4. Is under {max_output_size} characters

{context_str}SECTION SUMMARIES:
{combined_summaries}

Create a single, unified markdown summary."""

    try:
        call_kwargs = {
            "task": "web_extract",
            "messages": [
                {
                    "role": "system",
                    "content": "You synthesize multiple summaries into one cohesive, comprehensive summary. Be thorough but concise.",
                },
                {"role": "user", "content": synthesis_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 20000,
        }
        if model:
            call_kwargs["model"] = model
        response = await async_call_llm(**call_kwargs)
        final_summary = response.choices[0].message.content.strip()

        # Enforce hard cap
        if len(final_summary) > max_output_size:
            final_summary = (
                final_summary[:max_output_size]
                + "\n\n[... summary truncated for context management ...]"
            )

        original_len = len(content)
        final_len = len(final_summary)
        compression = final_len / original_len if original_len > 0 else 1.0

        logger.info(
            "Synthesis complete: %d -> %d chars (%.2f%%)",
            original_len,
            final_len,
            compression * 100,
        )
        return final_summary

    except Exception as e:
        logger.warning("Synthesis failed: %s", str(e)[:100])
        # Fall back to concatenated summaries with truncation
        fallback = "\n\n".join(summaries)
        if len(fallback) > max_output_size:
            fallback = (
                fallback[:max_output_size] + "\n\n[... truncated due to synthesis failure ...]"
            )
        return fallback


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
    debug_call_data = {
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
    format: str = None,
    use_llm_processing: bool = True,
    model: str = DEFAULT_SUMMARIZER_MODEL,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
) -> str:
    """
    Extract content from specific web pages using available extraction API backend.

    This function provides a generic interface for web content extraction that
    can work with multiple backends. Currently uses Firecrawl.

    Args:
        urls (List[str]): List of URLs to extract content from
        format (str): Desired output format ("markdown" or "html", optional)
        use_llm_processing (bool): Whether to process content with LLM for summarization (default: True)
        model (str): The model to use for LLM processing (default: google/gemini-3-flash-preview)
        min_length (int): Minimum content length to trigger LLM processing (default: 5000)

    Returns:
        str: JSON string containing extracted content. If LLM processing is enabled and successful,
             the 'content' field will contain the processed markdown summary instead of raw content.

    Raises:
        Exception: If extraction fails or API key is not set
    """
    debug_call_data = {
        "parameters": {
            "urls": urls,
            "format": format,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length,
        },
        "error": None,
        "pages_extracted": 0,
        "pages_processed_with_llm": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "compression_metrics": [],
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

        # Process each result with LLM if enabled
        if use_llm_processing:
            logger.info("Processing extracted content with LLM (parallel)...")
            debug_call_data["processing_applied"].append("llm_processing")

            # Prepare tasks for parallel processing
            async def process_single_result(result):
                """Process a single result with LLM and return updated result with metrics."""
                url = result.get("url", "Unknown URL")
                title = result.get("title", "")
                raw_content = result.get("raw_content", "") or result.get("content", "")

                if not raw_content:
                    return result, None, "no_content"

                original_size = len(raw_content)

                # Process content with LLM
                processed = await process_content_with_llm(
                    raw_content, url, title, model, min_length
                )

                if processed:
                    processed_size = len(processed)
                    compression_ratio = processed_size / original_size if original_size > 0 else 1.0

                    # Update result with processed content
                    result["content"] = processed
                    result["raw_content"] = raw_content

                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": processed_size,
                        "compression_ratio": compression_ratio,
                        "model_used": model,
                    }
                    return result, metrics, "processed"
                else:
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": original_size,
                        "compression_ratio": 1.0,
                        "model_used": None,
                        "reason": "content_too_short",
                    }
                    return result, metrics, "too_short"

            # Run all LLM processing in parallel
            results_list = response.get("results", [])
            tasks = [process_single_result(result) for result in results_list]
            processed_results = await asyncio.gather(*tasks)

            # Collect metrics and print results
            for result, metrics, status in processed_results:
                url = result.get("url", "Unknown URL")
                if status == "processed":
                    debug_call_data["compression_metrics"].append(metrics)
                    debug_call_data["pages_processed_with_llm"] += 1
                    logger.info("%s (processed)", url)
                elif status == "too_short":
                    debug_call_data["compression_metrics"].append(metrics)
                    logger.info("%s (no processing - content too short)", url)
                else:
                    logger.warning("%s (no content to process)", url)
        else:
            # Print summary of extracted pages for debugging (original behavior)
            for result in response.get("results", []):
                url = result.get("url", "Unknown URL")
                content_length = len(result.get("raw_content", ""))
                logger.info("%s (%d characters)", url, content_length)

        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
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
    """Check if an auxiliary text model is available for LLM content processing."""
    try:
        from agent.providers.auxiliary_client import resolve_provider_client

        for p in ("openrouter", "nous", "custom", "codex"):
            client, _ = resolve_provider_client(p)
            if client is not None:
                return True
        return False
    except Exception:
        return False


def get_debug_session_info() -> dict[str, Any]:
    """Get information about the current debug session."""
    return _debug.get_session_info()


if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)

    # Check if API keys are available
    firecrawl_available = check_firecrawl_api_key()
    nous_available = check_auxiliary_model()

    print("✅ Free research search available without paid keys")
    if not firecrawl_available:
        print(
            "ℹ️  Firecrawl extraction unavailable; set FIRECRAWL_API_URL for a self-hosted instance"
        )
    else:
        print("✅ Firecrawl extraction configured")

    if not nous_available:
        print("❌ No auxiliary model available for LLM content processing")
        print(
            "Set OPENROUTER_API_KEY, configure Nous Portal, or set OPENAI_BASE_URL + OPENAI_API_KEY"
        )
        print("⚠️  Without an auxiliary model, LLM content processing will be disabled")
    else:
        print(f"✅ Auxiliary model available: {DEFAULT_SUMMARIZER_MODEL}")

    print("🛠️  Web tools ready for use!")

    if nous_available:
        print(f"🧠 LLM content processing available with {DEFAULT_SUMMARIZER_MODEL}")
        print(f"   Default min length for processing: {DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION} chars")

    # Show debug mode status
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(
            f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json"
        )
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")

    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract and crawl (asynchronous)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("  asyncio.run(main())")

    if nous_available:
        print("\nLLM-enhanced usage:")
        print("  # Content automatically processed for pages >5000 chars (default)")
        print("  content = await web_extract_tool(['https://python.org/about/'])")
        print("")
        print("  # Customize processing parameters")
        print("      'docs.python.org',")
        print("      'Find key concepts',")
        print("      model='google/gemini-3-flash-preview',")
        print("      min_length=3000")
        print("  )")
        print("")
        print("  # Disable LLM processing")
        print(
            "  raw_content = await web_extract_tool(['https://example.com'], use_llm_processing=False)"
        )

    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Debug logs capture:")
    print("  # - All tool calls with parameters")
    print("  # - Original API responses")
    print("  # - LLM compression metrics")
    print("  # - Final processed results")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")

    print("\n📝 Run 'python test_web_tools_llm.py' to test LLM processing capabilities")

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
    "description": "Extract content from web page URLs. Returns page content in markdown format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass the PDF link directly and it converts to markdown text. Pages under 5000 chars return full markdown; larger pages are LLM-summarized and capped at ~5000 chars per page. Pages over 2M chars are refused. If a URL fails or times out, use the browser tool to access it instead.",
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
