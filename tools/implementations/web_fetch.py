#!/usr/bin/env python3
"""Single-URL fetch-and-read tool (``web_fetch``).

Closes the "found a paper but cannot read it" gap: ``web_search`` returns titles, URLs,
and snippets, but nothing the model can call actually opens the page or PDF it found.

Backend: Jina Reader (``GET https://r.jina.ai/<url>``), which converts both HTML pages and
PDFs to clean markdown with no API key required. ``JINA_API_KEY`` is attached as a bearer
token only when set (higher rate limits). On any Jina failure we fall back to a plain
``requests.get`` plus a stdlib-only HTML->text reduction (no new third-party deps). Long
output is bounded by reusing the existing summarizer ``process_content_with_llm`` from
web_tools so a single fetch never floods the model's context.
"""

import logging
import os
from html.parser import HTMLParser

import requests

from tools.implementations.web_tools import (
    DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    DEFAULT_SUMMARIZER_MODEL,
    clean_base64_images,
    process_content_with_llm,
)
from tools.response import dumps, error

logger = logging.getLogger(__name__)

JINA_READER_URL = "https://r.jina.ai/"
WEB_FETCH_TIMEOUT_SECONDS = 30
# Default ceiling on returned text; pages above it are summarized down to ~this size.
DEFAULT_MAX_CHARS = 5000
# Upper bound the model may request, to keep a single fetch from dominating context.
MAX_ALLOWED_CHARS = 20000

_FETCH_USER_AGENT = "LeanFlow/0.3 web-fetch (+https://github.com/leanflow)"


class _TextExtractor(HTMLParser):
    """Collect human-readable text from HTML, dropping script/style/head noise."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "title", "meta", "link"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def get_text(self) -> str:
        """Return the accumulated text with whitespace collapsed to a readable shape."""
        raw = "".join(self._parts)
        # Collapse runs of blank lines/spaces while preserving paragraph breaks.
        lines = [line.strip() for line in raw.splitlines()]
        out: list[str] = []
        blank = False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    """Reduce an HTML document to readable plain text using only the stdlib."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # Malformed markup: salvage whatever the parser collected so far.
        logger.debug("HTML parse incomplete; returning partial text")
    return parser.get_text()


def _jina_fetch(url: str) -> str:
    """Fetch ``url`` through Jina Reader and return markdown. Raises on HTTP/network error."""
    headers = {"Accept": "text/markdown", "User-Agent": _FETCH_USER_AGENT}
    api_key = (os.getenv("JINA_API_KEY") or "").strip()
    if api_key:
        # Bearer auth lifts Jina's anonymous rate limit when a key is configured.
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.get(
        f"{JINA_READER_URL}{url}", headers=headers, timeout=WEB_FETCH_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.text or ""


def _fallback_fetch(url: str) -> str:
    """Fetch ``url`` directly and reduce it to text without third-party deps.

    Used when Jina Reader is unavailable. Binary content (e.g. a raw PDF) cannot be
    meaningfully decoded here — Jina is the path that reads PDFs — so we surface a clear
    note instead of returning garbage bytes.
    """
    response = requests.get(
        url,
        headers={"User-Agent": _FETCH_USER_AGENT},
        timeout=WEB_FETCH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        raise ValueError(
            "PDF fetch requires the Jina Reader backend, which was unavailable; "
            "retry later or supply an HTML source URL."
        )
    return _html_to_text(response.text or "")


async def web_fetch_tool(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Fetch a single URL (web page or PDF) and return readable markdown/text as JSON.

    Tries Jina Reader first (handles HTML and PDFs, no key needed), falling back to a direct
    ``requests.get`` plus stdlib HTML->text on Jina failure. Content longer than ``max_chars``
    is summarized via ``process_content_with_llm`` and capped; shorter content is returned
    as-is. Always returns a JSON string ``{success, url, content, backend}`` or ``{error}``.
    """
    url = (url or "").strip()
    if not url:
        return error("web_fetch requires a non-empty 'url'")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        bound = int(max_chars)
    except (TypeError, ValueError):
        bound = DEFAULT_MAX_CHARS
    bound = max(500, min(MAX_ALLOWED_CHARS, bound))

    from tools.utilities.interrupt import is_interrupted

    if is_interrupted():
        return error("Interrupted")

    backend = "jina"
    jina_error: str | None = None
    try:
        text = _jina_fetch(url)
    except Exception as jina_exc:
        jina_error = str(jina_exc)
        logger.info("Jina Reader failed for %s (%s); falling back to direct fetch", url, jina_error)
        backend = "fallback"
        try:
            text = _fallback_fetch(url)
        except Exception as fallback_exc:
            return error(
                f"Failed to fetch {url}: jina error: {jina_error}; "
                f"fallback error: {fallback_exc}"
            )

    text = clean_base64_images(text or "").strip()
    if not text:
        return error(f"No readable content found at {url}", backend=backend)

    truncated = False
    # Reuse the (otherwise-dead) summarizer for long pages so output stays bounded.
    if len(text) > max(DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION, bound):
        summary = await process_content_with_llm(
            text,
            url=url,
            model=DEFAULT_SUMMARIZER_MODEL,
            min_length=DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
        )
        if summary:
            text = summary
        # Hard cap regardless of whether summarization ran (e.g. no auxiliary model).
        if len(text) > bound:
            text = text[:bound].rstrip() + "\n\n[... truncated for context management ...]"
            truncated = True

    payload = {
        "success": True,
        "url": url,
        "backend": backend,
        "truncated": truncated,
        "content": text,
    }
    if jina_error:
        payload["note"] = f"Jina Reader unavailable; used direct fetch ({jina_error})"
    return dumps(payload)


def check_web_fetch_available() -> bool:
    """web_fetch is always available: Jina Reader needs no key and a direct fetch is the fallback."""
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry  # noqa: E402

WEB_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch a single URL — a web page OR a PDF (e.g. an arXiv paper link) — and return its "
        "readable content as markdown/text. Use this after web_search to actually READ a result: "
        "pass the page or PDF URL and get the text back. Pages under ~5000 chars are returned in "
        "full; longer pages are summarized and capped. Backed by Jina Reader with a direct-fetch "
        "fallback (no API key required)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The single page or PDF URL to fetch and read.",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "Optional cap on returned characters (default 5000, max 20000). Longer "
                    "content is summarized/truncated to fit."
                ),
                "minimum": 500,
                "maximum": MAX_ALLOWED_CHARS,
            },
        },
        "required": ["url"],
    },
}

registry.register(
    name="web_fetch",
    toolset="web",
    schema=WEB_FETCH_SCHEMA,
    handler=lambda args, **kw: web_fetch_tool(
        args.get("url", ""), max_chars=args.get("max_chars", DEFAULT_MAX_CHARS)
    ),
    check_fn=check_web_fetch_available,
    requires_env=[],
    is_async=True,
    emoji="📖",
)
