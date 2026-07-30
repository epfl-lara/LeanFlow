#!/usr/bin/env python3
"""Web retrieval tools: ``web_fetch`` (read a URL) and ``web_download`` (save a URL to disk).

Closes the "found a paper but cannot read it" gap: ``web_search`` returns titles, URLs,
and snippets, but nothing the model can call actually opens the page/PDF it found or saves a
file for later use.

``web_fetch`` backend: Jina Reader (``GET https://r.jina.ai/<url>``), which converts both HTML
pages and PDFs to clean markdown with no API key required. ``JINA_API_KEY`` is attached as a
bearer token only when set (higher rate limits). On any Jina failure we fall back to a plain
``requests.get`` plus a stdlib-only HTML->text reduction (no new third-party deps). Long output
is bounded by reusing the existing summarizer ``process_content_with_llm`` from web_tools so a
single fetch never floods the model's context.

``web_download`` streams a URL to ``<cwd>/.leanflow/downloads/`` (size-capped, filename
sanitized, destination sandboxed under the project) and returns the local path so ``read_pdf`` /
file tools / Lean can consume it.
"""

import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from tools.implementations.web_tools import (
    DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    DEFAULT_SUMMARIZER_MODEL,
    clean_base64_images,
    process_content_with_llm,
)
from tools.response import dumps, error
from tools.utilities.repository_research_policy import (
    repository_url_block_reason,
    solution_research_url_block_reason,
)

logger = logging.getLogger(__name__)

JINA_READER_URL = "https://r.jina.ai/"
WEB_FETCH_TIMEOUT_SECONDS = 30
# Default ceiling on returned text; pages above it are summarized down to ~this size.
DEFAULT_MAX_CHARS = 5000
# Upper bound the model may request, to keep a single fetch from dominating context.
MAX_ALLOWED_CHARS = 20000
FETCH_FAILURE_COOLDOWN_SECONDS = 60.0
FETCH_FAILURE_CACHE_CAP = 128

_FETCH_USER_AGENT = "LeanFlow/0.3 web-fetch (+https://github.com/leanflow)"
_DIRECT_TEXT_HOSTS = frozenset(
    {
        "gist.githubusercontent.com",
        "raw.githubusercontent.com",
        "raw.github.com",
    }
)
_FETCH_FAILURE_CACHE_LOCK = threading.Lock()
_FETCH_FAILURE_CACHE: dict[str, tuple[float, str, str]] = {}


def _cached_fetch_failure(url: str) -> str | None:
    """Return a recent terminal fetch failure without repeating both network calls."""
    now = time.monotonic()
    with _FETCH_FAILURE_CACHE_LOCK:
        cached = _FETCH_FAILURE_CACHE.get(url)
        if cached is None:
            return None
        expires_at, message, backend = cached
        if expires_at <= now:
            _FETCH_FAILURE_CACHE.pop(url, None)
            return None
    return error(
        message,
        backend=backend,
        cached=True,
        provider_called=False,
        retry_after_seconds=max(1, int(expires_at - now)),
    )


def _remember_fetch_failure(url: str, message: str, *, backend: str) -> str:
    """Cache one dual-backend fetch failure briefly and return its tool payload."""
    expires_at = time.monotonic() + FETCH_FAILURE_COOLDOWN_SECONDS
    with _FETCH_FAILURE_CACHE_LOCK:
        if len(_FETCH_FAILURE_CACHE) >= FETCH_FAILURE_CACHE_CAP:
            oldest = min(_FETCH_FAILURE_CACHE, key=lambda key: _FETCH_FAILURE_CACHE[key][0])
            _FETCH_FAILURE_CACHE.pop(oldest, None)
        _FETCH_FAILURE_CACHE[url] = (expires_at, message, backend)
    return error(message, backend=backend, cached=False, provider_called=True)


def _clear_fetch_failure(url: str) -> None:
    """Forget a prior terminal failure after a successful retrieval."""
    with _FETCH_FAILURE_CACHE_LOCK:
        _FETCH_FAILURE_CACHE.pop(url, None)


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
    if content_type.startswith(("text/plain", "application/json")) or (
        (urlparse(url).hostname or "").lower() in _DIRECT_TEXT_HOSTS
    ):
        return response.text or ""
    return _html_to_text(response.text or "")


def _prefer_direct_text_fetch(url: str) -> bool:
    """Return whether a known raw-text host should bypass Jina conversion."""
    return (urlparse(url).hostname or "").lower() in _DIRECT_TEXT_HOSTS


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
    blocked = repository_url_block_reason(url) or solution_research_url_block_reason(url)
    if blocked:
        return error(blocked)
    cached_failure = _cached_fetch_failure(url)
    if cached_failure is not None:
        return cached_failure

    try:
        bound = int(max_chars)
    except (TypeError, ValueError):
        bound = DEFAULT_MAX_CHARS
    bound = max(500, min(MAX_ALLOWED_CHARS, bound))

    from tools.utilities.interrupt import is_interrupted

    if is_interrupted():
        return error("Interrupted")

    backend = "direct" if _prefer_direct_text_fetch(url) else "jina"
    jina_error: str | None = None
    if backend == "direct":
        try:
            text = _fallback_fetch(url)
        except Exception as direct_exc:
            return _remember_fetch_failure(
                url,
                f"Failed to fetch {url} directly: {direct_exc}",
                backend="direct",
            )
    else:
        try:
            text = _jina_fetch(url)
        except Exception as jina_exc:
            jina_error = str(jina_exc)
            logger.info(
                "Jina Reader failed for %s (%s); falling back to direct fetch",
                url,
                jina_error,
            )
            backend = "fallback"
            try:
                text = _fallback_fetch(url)
            except Exception as fallback_exc:
                return _remember_fetch_failure(
                    url,
                    (
                        f"Failed to fetch {url}: jina error: {jina_error}; "
                        f"fallback error: {fallback_exc}"
                    ),
                    backend="fallback",
                )

    text = clean_base64_images(text or "").strip()
    if not text:
        return _remember_fetch_failure(
            url,
            f"No readable content found at {url}",
            backend=backend,
        )
    _clear_fetch_failure(url)

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
# web_download
# ---------------------------------------------------------------------------
WEB_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard ceiling
WEB_DOWNLOAD_DIRNAME = ".leanflow/downloads"


def _safe_download_filename(url: str, override: str) -> str:
    """Derive a safe basename for a downloaded file (no path traversal, restricted charset)."""
    name = (override or "").strip()
    if not name:
        name = unquote(os.path.basename(urlparse(url).path)) or "download"
    name = os.path.basename(name)  # strip any directory components
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "download"
    return name[:200]


def web_download_tool(url: str, filename: str = "", max_bytes: int = WEB_DOWNLOAD_MAX_BYTES) -> str:
    """Download a URL (PDF, dataset, artifact) into the project workspace; return its local path.

    Streams the body to ``<cwd>/.leanflow/downloads/`` with the destination sandboxed under that
    directory, a sanitized filename, and a hard size cap so a runaway download can't fill the disk.
    Returns JSON ``{success, url, path, bytes, content_type}`` so the model can then read the file
    (e.g. via read_pdf) or hand it to Lean.
    """
    url = (url or "").strip()
    if not url:
        return error("web_download requires a non-empty 'url'")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    blocked = repository_url_block_reason(url) or solution_research_url_block_reason(url)
    if blocked:
        return error(blocked)
    try:
        cap = int(max_bytes)
    except (TypeError, ValueError):
        cap = WEB_DOWNLOAD_MAX_BYTES
    cap = max(1, min(WEB_DOWNLOAD_MAX_BYTES, cap))

    project_root = Path.cwd().resolve()
    dest_dir = (project_root / WEB_DOWNLOAD_DIRNAME).resolve()
    # Reject if the downloads dir (or an ancestor) is a symlink that escapes the project — the
    # filename is already basename-sanitized, but a symlinked .leanflow/downloads could redirect
    # writes outside the sandbox while still passing the dest.parent == dest_dir check.
    if project_root != dest_dir and project_root not in dest_dir.parents:
        return error("Refusing to write: downloads directory escapes the project sandbox")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / _safe_download_filename(url, filename)).resolve()
    if dest.parent != dest_dir:  # defense in depth against traversal
        return error("Refusing to write outside the downloads directory")

    total = 0
    try:
        with requests.get(
            url,
            headers={"User-Agent": _FETCH_USER_AGENT},
            timeout=WEB_FETCH_TIMEOUT_SECONDS,
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            with open(dest, "wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > cap:
                        handle.close()
                        dest.unlink(missing_ok=True)
                        return error(f"Download exceeds the {cap}-byte cap; aborted")
                    handle.write(chunk)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        return error(f"Failed to download {url}: {exc}")

    return dumps(
        {
            "success": True,
            "url": url,
            "path": str(dest),
            "bytes": total,
            "content_type": content_type,
        }
    )


def check_web_download_available() -> bool:
    """web_download is always available (plain HTTP GET to a sandboxed local directory)."""
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

WEB_DOWNLOAD_SCHEMA = {
    "name": "web_download",
    "description": (
        "Download a file from a URL (PDF, dataset, artifact) into the project workspace and return "
        "its local path. Use this when you need the actual file — e.g. save an arXiv PDF, then read "
        "it with read_pdf. The file is saved under .leanflow/downloads/ with a sanitized name and a "
        "50MB cap. Returns {path, bytes, content_type}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL of the file to download."},
            "filename": {
                "type": "string",
                "description": "Optional destination filename (basename only; sanitized).",
            },
        },
        "required": ["url"],
    },
}

registry.register(
    name="web_download",
    toolset="web",
    schema=WEB_DOWNLOAD_SCHEMA,
    handler=lambda args, **kw: web_download_tool(
        args.get("url", ""), filename=args.get("filename", "")
    ),
    check_fn=check_web_download_available,
    requires_env=[],
    emoji="⬇️",
)
