"""Search academic and code sources and normalize provider results."""

import base64
import html as _html
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

import requests

from tools.utilities.repository_research_policy import repository_research_disabled

RESEARCH_SEARCH_USER_AGENT = "LeanFlow/0.3 free-research-search"
RESEARCH_SEARCH_TIMEOUT_SECONDS = 12
SOURCEGRAPH_SEARCH_TIMEOUT_SECONDS = 8
ARXIV_API_URL = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
SOURCEGRAPH_GRAPHQL_URL = "https://sourcegraph.com/.api/graphql"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
EXA_SEARCH_URL = "https://api.exa.ai/search"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
BING_SEARCH_URL = "https://www.bing.com/search"
GITHUB_REPOSITORY_SEARCH_URL = "https://api.github.com/search/repositories"
# DuckDuckGo's HTML endpoint rejects obvious bot user-agents, so present a browser-like one.
_GENERAL_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Bing selects a script-heavy shell for a full Chrome UA but serves its parseable,
# server-rendered result list to a minimal standards-compatible client.
_BING_USER_AGENT = "Mozilla/5.0"
_BING_RELEVANCE_NOISE = {
    "api",
    "article",
    "best",
    "configure",
    "current",
    "docs",
    "documentation",
    "guide",
    "how",
    "information",
    "install",
    "installation",
    "latest",
    "manual",
    "news",
    "official",
    "page",
    "release",
    "search",
    "setup",
    "site",
    "version",
    "web",
    "website",
    "what",
}
_BING_RETRY_NOISE = {
    "current",
    "docs",
    "documentation",
    "information",
    "latest",
    "official",
    "page",
    "search",
    "site",
    "web",
    "website",
}
_GITHUB_QUERY_NOISE = _BING_RELEVANCE_NOISE | {
    "reference",
    "tutorial",
}
_GITHUB_RESEARCH_SIGNALS = (
    " api",
    " code",
    " coq",
    " docs",
    " documentation",
    " formalisation",
    " formalization",
    " git",
    " github",
    " install",
    " lean",
    " library",
    " package",
    " programming",
    " proof",
    " repository",
    " rocq",
    " software",
    " theorem",
)
_GITHUB_CACHE_TTL_SECONDS = 300
_GITHUB_CACHE_LOCK = threading.Lock()
_GITHUB_SEARCH_CACHE: dict[tuple[str, int, bool], tuple[float, dict[str, Any]]] = {}
CODE_SEARCH_STOPWORDS = {
    "lean",
    "coq",
    "rocq",
    "code",
    "theorem",
    "lemma",
    "proof",
    "example",
    "examples",
    "formalization",
    "formalisation",
    "mathlib",
}


def _normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate_text(value: Any, max_chars: int = 500) -> str:
    text = _normalize_whitespace(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _bounded_limit(limit: int, *, default: int = 5, maximum: int = 10) -> int:
    try:
        parsed = int(limit)
    except Exception:
        return default
    return max(1, min(maximum, parsed))


def _append_unique_result(
    results: list[dict[str, Any]], seen_urls: set[str], result: dict[str, Any]
) -> None:
    url = str(result.get("url") or "").strip()
    dedupe_key = url or f"{result.get('provider')}:{result.get('title')}:{result.get('snippet')}"
    if not dedupe_key or dedupe_key in seen_urls:
        return
    seen_urls.add(dedupe_key)
    result["position"] = len(results) + 1
    results.append(result)


def _research_headers() -> dict[str, str]:
    """Return shared HTTP headers for the free research providers.

    Attaches a Semantic Scholar API key (``SEMANTIC_SCHOLAR_API_KEY`` / ``S2_API_KEY``) when set, so
    S2 uses its authenticated quota instead of the heavily-throttled shared anonymous pool. Behavior
    is unchanged when no key is configured.
    """
    headers = {"User-Agent": RESEARCH_SEARCH_USER_AGENT}
    api_key = (os.getenv("SEMANTIC_SCHOLAR_API_KEY") or os.getenv("S2_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _arxiv_search_query(query: str) -> str:
    """Build an arXiv ``search_query`` that favors relevance.

    AND-ing every token across all fields (the previous behavior) is far too strict and surfaced
    unrelated papers (e.g. "liquid tensor experiment" matched a nematic-liquid-crystals paper).
    Instead, quote the full phrase against title/abstract and OR it with a token disjunction so a
    strong phrase match ranks first while individual terms still match.
    """
    phrase = query.strip()
    tokens = re.findall(r"[A-Za-z0-9_.+-]+", query)[:12]
    if not tokens:
        return f'all:"{phrase}"' if phrase else "all:mathematics"
    token_clause = " OR ".join(f"all:{token}" for token in tokens)
    if len(tokens) > 1 and phrase:
        safe_phrase = phrase.replace('"', "")
        return f'(ti:"{safe_phrase}" OR abs:"{safe_phrase}" OR ({token_clause}))'
    return token_clause


def _search_arxiv(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            ARXIV_API_URL,
            params={
                "search_query": _arxiv_search_query(query),
                "start": 0,
                "max_results": max(1, min(limit, 5)),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except Exception as exc:
        return [], f"arXiv search unavailable: {exc}"

    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        title = _normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ns))
        url = _normalize_whitespace(entry.findtext("atom:id", default="", namespaces=ns))
        summary = _truncate_text(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [
            _normalize_whitespace(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        authors = [author for author in authors if author]
        published = _normalize_whitespace(
            entry.findtext("atom:published", default="", namespaces=ns)
        )
        arxiv_id = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ns)
            if category.attrib.get("term")
        ]
        if title or url:
            results.append(
                {
                    "provider": "arxiv",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": summary,
                    "authors": authors[:8],
                    "year": published[:4] if published else "",
                    "source": "arXiv",
                    "arxiv_id": arxiv_id,
                    "pdf_url": pdf_url,
                    "categories": categories[:6],
                }
            )
    return results, ""


def _search_semantic_scholar(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            SEMANTIC_SCHOLAR_SEARCH_URL,
            params={
                "query": query,
                "limit": max(1, min(limit, 5)),
                "fields": "title,url,abstract,authors,year,venue,externalIds,citationCount,openAccessPdf",
            },
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Semantic Scholar search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Semantic Scholar search unavailable: {exc}"

    results: list[dict[str, Any]] = []
    for item in payload.get("data", [])[:limit]:
        if not isinstance(item, dict):
            continue
        authors = [
            _normalize_whitespace(author.get("name"))
            for author in item.get("authors", [])
            if isinstance(author, dict) and author.get("name")
        ]
        external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        title = _normalize_whitespace(item.get("title"))
        url = _normalize_whitespace(item.get("url"))
        if title or url:
            results.append(
                {
                    "provider": "semantic-scholar",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": _truncate_text(item.get("abstract")),
                    "authors": authors[:8],
                    "year": item.get("year") or "",
                    "source": _normalize_whitespace(item.get("venue")) or "Semantic Scholar",
                    "citation_count": item.get("citationCount"),
                    "external_ids": external_ids,
                    "pdf_url": _normalize_whitespace(open_pdf.get("url")) if open_pdf else "",
                }
            )
    return results, ""


def _crossref_year(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "created"):
        date_parts = (
            item.get(key, {}).get("date-parts") if isinstance(item.get(key), dict) else None
        )
        if date_parts and isinstance(date_parts, list) and date_parts[0]:
            return str(date_parts[0][0])
    return ""


def _crossref_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in item.get("author", []) if isinstance(item.get("author"), list) else []:
        if not isinstance(author, dict):
            continue
        given = _normalize_whitespace(author.get("given"))
        family = _normalize_whitespace(author.get("family"))
        name = _normalize_whitespace(f"{given} {family}")
        if name:
            authors.append(name)
    return authors


def _search_crossref(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            CROSSREF_SEARCH_URL,
            params={"query": query, "rows": max(1, min(limit, 5))},
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Crossref search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Crossref search unavailable: {exc}"

    items = payload.get("message", {}).get("items", [])
    results: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        title_values = item.get("title") if isinstance(item.get("title"), list) else []
        title = _normalize_whitespace(title_values[0] if title_values else "")
        url = _normalize_whitespace(
            item.get("URL") or item.get("DOI") and f"https://doi.org/{item.get('DOI')}"
        )
        container_values = (
            item.get("container-title") if isinstance(item.get("container-title"), list) else []
        )
        source = _normalize_whitespace(
            container_values[0] if container_values else item.get("publisher") or "Crossref"
        )
        abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract", "") or ""))
        snippet_parts = [
            part
            for part in (
                source,
                f"DOI: {item.get('DOI')}" if item.get("DOI") else "",
                _truncate_text(abstract, 350),
            )
            if part
        ]
        if title or url:
            results.append(
                {
                    "provider": "crossref",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": _truncate_text(" - ".join(snippet_parts)),
                    "authors": _crossref_authors(item)[:8],
                    "year": _crossref_year(item),
                    "source": source,
                    "doi": _normalize_whitespace(item.get("DOI")),
                }
            )
    return results, ""


def _strip_html(value: str) -> str:
    """Reduce an HTML fragment to readable text (drop tags, unescape entities, collapse space)."""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return _normalize_whitespace(_html.unescape(text))


def _decode_duckduckgo_href(href: str) -> str:
    """Resolve a DuckDuckGo HTML result href to its real target URL.

    DDG wraps results in a redirect (`//duckduckgo.com/l/?uddg=<encoded-url>`); extract and decode
    the `uddg` parameter, otherwise return the href if it is already an absolute http(s) URL.
    """
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    # parse_qs already percent-decodes once; do NOT unquote again or a target URL that
    # legitimately contains an encoded value (e.g. %252B) gets corrupted to %2B/+.
    params = urllib.parse.parse_qs(parsed.query)
    if "uddg" in params and params["uddg"]:
        return params["uddg"][0]
    return href if href.startswith("http") else ""


def _parse_duckduckgo_html(html_text: str, limit: int) -> list[dict[str, Any]]:
    """Parse general web results out of a DuckDuckGo HTML response into normalized records."""
    anchor_re = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_re = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    snippets = [_strip_html(snip) for snip in snippet_re.findall(html_text)]
    results: list[dict[str, Any]] = []
    for index, (href, title_html) in enumerate(anchor_re.findall(html_text)):
        if len(results) >= limit:
            break
        url = _decode_duckduckgo_href(href)
        title = _strip_html(title_html)
        if not url or not title:
            continue
        results.append(
            {
                "provider": "duckduckgo",
                "kind": "web",
                "title": title,
                "url": url,
                "snippet": _truncate_text(snippets[index] if index < len(snippets) else ""),
            }
        )
    return results


def _decode_bing_href(href: str) -> str:
    """Return the target URL from either a direct or Bing redirect result link."""
    href = _html.unescape(str(href or "").strip())
    parsed = urllib.parse.urlparse(href)
    if (parsed.hostname or "").lower().endswith("bing.com") and parsed.path == "/ck/a":
        encoded = (urllib.parse.parse_qs(parsed.query).get("u") or [""])[0]
        if encoded.startswith("a1"):
            token = encoded[2:]
            token += "=" * (-len(token) % 4)
            try:
                decoded = base64.urlsafe_b64decode(token).decode("utf-8")
            except Exception:
                decoded = ""
            if decoded.startswith(("http://", "https://")):
                return decoded
    return href if href.startswith(("http://", "https://")) else ""


def _parse_bing_html(html_text: str, limit: int) -> list[dict[str, Any]]:
    """Parse Bing's public HTML result list into normalized web records."""
    block_re = re.compile(
        r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
        re.DOTALL | re.IGNORECASE,
    )
    anchor_re = re.compile(
        r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippet_re = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
    results: list[dict[str, Any]] = []
    for block in block_re.findall(html_text):
        if len(results) >= limit:
            break
        anchor = anchor_re.search(block)
        if anchor is None:
            continue
        url = _decode_bing_href(anchor.group(1))
        title = _strip_html(anchor.group(2))
        snippet_match = snippet_re.search(block)
        snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""
        if not url or not title:
            continue
        results.append(
            {
                "provider": "bing",
                "kind": "web",
                "title": title,
                "url": url,
                "snippet": _truncate_text(snippet),
            }
        )
    return results


def _bing_relevance_terms(query: str) -> list[str]:
    """Return entity-bearing terms that Bing results should mention."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", query.casefold())
    return [
        token
        for token in tokens
        if (len(token) >= 2 or token.isdigit()) and token not in _BING_RELEVANCE_NOISE
    ][:8]


def _filter_bing_results(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Reject generic Bing pages that do not mention the query's entity terms."""
    terms = _bing_relevance_terms(query)
    if not terms:
        return results
    minimum_matches = min(2, len(terms))
    return [
        result
        for result in results
        if sum(
            term in haystack
            for term in terms
            for haystack in (
                " ".join(
                    str(result.get(field) or "").casefold() for field in ("title", "url", "snippet")
                ),
            )
        )
        >= minimum_matches
    ]


def _bing_retry_query(query: str) -> str:
    """Build one less noisy retry while preserving product and topic terms."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", query)
    kept = [token for token in tokens if token.casefold() not in _BING_RETRY_NOISE]
    return " ".join(kept) or _normalize_whitespace(query)


def _search_tavily(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """General web search via the Tavily API (used when TAVILY_API_KEY is configured)."""
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return [], "Tavily API key not configured"
    try:
        response = requests.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max(1, min(limit, 10)),
                "search_depth": "basic",
            },
            headers={"User-Agent": RESEARCH_SEARCH_USER_AGENT},
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Tavily search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Tavily search unavailable: {exc}"

    results: list[dict[str, Any]] = []
    for item in (payload.get("results") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        title = _normalize_whitespace(item.get("title"))
        url = _normalize_whitespace(item.get("url"))
        if not (title or url):
            continue
        results.append(
            {
                "provider": "tavily",
                "kind": "web",
                "title": title,
                "url": url,
                "snippet": _truncate_text(_normalize_whitespace(item.get("content"))),
            }
        )
    return results, ""


def _search_exa(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Search the live web through Exa when ``EXA_API_KEY`` is configured."""
    api_key = (os.getenv("EXA_API_KEY") or "").strip()
    if not api_key:
        return [], "Exa API key not configured"
    try:
        response = requests.post(
            EXA_SEARCH_URL,
            json={
                "query": query,
                "numResults": max(1, min(limit, 10)),
                "type": "auto",
                "contents": {"highlights": True},
            },
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "User-Agent": RESEARCH_SEARCH_USER_AGENT,
            },
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Exa search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Exa search unavailable: {exc}"

    results: list[dict[str, Any]] = []
    for item in (payload.get("results") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        title = _normalize_whitespace(item.get("title"))
        url = _normalize_whitespace(item.get("url"))
        if not (title or url):
            continue
        raw_highlights = item.get("highlights")
        highlights = raw_highlights if isinstance(raw_highlights, list) else []
        snippet = next(
            (_normalize_whitespace(value) for value in highlights if _normalize_whitespace(value)),
            "",
        )
        snippet = snippet or _normalize_whitespace(item.get("summary") or item.get("text"))
        results.append(
            {
                "provider": "exa",
                "kind": "web",
                "title": title,
                "url": url,
                "snippet": _truncate_text(snippet),
                "source": "Exa",
                "author": _normalize_whitespace(item.get("author")),
                "published": _normalize_whitespace(item.get("publishedDate")),
            }
        )
    return results, ""


def _search_duckduckgo_html(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """General web search by scraping DuckDuckGo's HTML endpoint (no API key required)."""
    try:
        response = requests.get(
            DUCKDUCKGO_HTML_URL,
            params={"q": query},
            headers={"User-Agent": _GENERAL_WEB_USER_AGENT},
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "DuckDuckGo search throttled; retry later"
        response.raise_for_status()
    except Exception as exc:
        return [], f"DuckDuckGo search unavailable: {exc}"
    response_text = response.text or ""
    lowered = response_text.lower()
    if response.status_code == 202 and any(
        marker in lowered for marker in ("anomaly-modal", "challenge-form", "bots use duckduckgo")
    ):
        return [], "DuckDuckGo blocked this anonymous search with an anti-bot challenge"
    results = _parse_duckduckgo_html(response_text, limit)
    if not results:
        return [], "DuckDuckGo returned no parseable results"
    return results, ""


def _search_bing_html(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Search Bing's public HTML page with one relevance-guarded retry."""

    def fetch(search_query: str) -> tuple[list[dict[str, Any]], str]:
        try:
            response = requests.get(
                BING_SEARCH_URL,
                # An explicit neutral market prevents Bing from returning a generic
                # Microsoft navigation page for container IPs with no locale history.
                params={"q": search_query, "setlang": "en", "cc": "us"},
                headers={
                    "User-Agent": _BING_USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
            )
            if response.status_code == 429:
                return [], "Bing search throttled; retry later"
            response.raise_for_status()
        except Exception as exc:
            return [], f"Bing search unavailable: {exc}"
        parsed = _parse_bing_html(response.text or "", limit)
        return _filter_bing_results(parsed, query), ""

    results, error = fetch(query)
    if results or error:
        return results, error
    retry_query = _bing_retry_query(query)
    if retry_query.casefold() != _normalize_whitespace(query).casefold():
        results, error = fetch(retry_query)
        if results or error:
            return results, error
    return [], "Bing returned no relevant parseable results"


def _github_repository_queries(query: str) -> tuple[str, ...]:
    """Return bounded software-focused GitHub repository query formulations."""
    lowered = f" {query.casefold()}"
    if not any(signal in lowered for signal in _GITHUB_RESEARCH_SIGNALS):
        return ()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", query)
    primary_tokens = [token for token in tokens if token.casefold() not in _GITHUB_QUERY_NOISE][:8]
    if not primary_tokens:
        return ()
    primary = " ".join(primary_tokens)
    without_version = " ".join(
        token for token in primary_tokens if not any(char.isdigit() for char in token)
    )
    return tuple(
        candidate
        for index, candidate in enumerate((primary, without_version))
        if candidate
        and candidate.casefold()
        not in {previous.casefold() for previous in (primary, without_version)[:index] if previous}
    )


def _search_github_repositories(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Search public GitHub repositories when repository research is allowed."""
    if repository_research_disabled():
        return [], "GitHub repository search disabled by clean-room policy"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": RESEARCH_SEARCH_USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api_key = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    errors: list[str] = []
    candidates: list[tuple[dict[str, Any], int, int]] = []
    queries = _github_repository_queries(query)
    if not queries:
        return [], "GitHub repository search not applicable to this query"
    for query_index, search_query in enumerate(queries):
        per_page = max(1, min(limit, 5))
        cache_key = (search_query.casefold(), per_page, bool(api_key))
        with _GITHUB_CACHE_LOCK:
            now = time.monotonic()
            cached = _GITHUB_SEARCH_CACHE.get(cache_key)
            if cached and now - cached[0] <= _GITHUB_CACHE_TTL_SECONDS:
                payload = cached[1]
            else:
                try:
                    response = requests.get(
                        GITHUB_REPOSITORY_SEARCH_URL,
                        params={"q": search_query, "per_page": per_page},
                        headers=headers,
                        timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
                    )
                    if response.status_code in {403, 429}:
                        return [], "GitHub repository search throttled; retry later"
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    errors.append(f"GitHub repository search unavailable: {exc}")
                    continue
                if isinstance(payload, dict):
                    _GITHUB_SEARCH_CACHE[cache_key] = (now, payload)
        if not isinstance(payload, dict):
            errors.append("GitHub repository search returned an invalid response")
            continue
        for result_index, item in enumerate(payload.get("items", [])[:limit]):
            if not isinstance(item, dict):
                continue
            full_name = _normalize_whitespace(item.get("full_name"))
            url = _normalize_whitespace(item.get("html_url"))
            if not full_name or not url:
                continue
            candidates.append(
                (
                    {
                        "provider": "github",
                        "kind": "repository",
                        "title": full_name,
                        "url": url,
                        "snippet": _truncate_text(item.get("description")),
                        "source": "GitHub",
                        "clone_url": _normalize_whitespace(item.get("clone_url")),
                        "default_branch": _normalize_whitespace(item.get("default_branch")),
                        "language": _normalize_whitespace(item.get("language")),
                        "stars": item.get("stargazers_count"),
                        "updated": _normalize_whitespace(item.get("updated_at")),
                    },
                    query_index,
                    result_index,
                )
            )
    signal_terms = _bing_relevance_terms(query)

    def score(candidate: tuple[dict[str, Any], int, int]) -> tuple[int, int, int, str]:
        result, query_index, result_index = candidate
        title = str(result.get("title") or "").casefold()
        snippet = str(result.get("snippet") or "").casefold()
        title_hits = sum(term in title for term in signal_terms)
        snippet_hits = sum(term in snippet for term in signal_terms)
        try:
            stars = max(0, int(result.get("stars") or 0))
        except (TypeError, ValueError):
            stars = 0
        return (
            -(5 * title_hits + snippet_hits),
            -min(stars, 100_000),
            query_index * 100 + result_index,
            title,
        )

    ranked: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result, _, _ in sorted(candidates, key=score):
        url = str(result.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        ranked.append(result)
        if len(ranked) >= limit:
            break
    return ranked, "; ".join(errors)


def _search_general_web(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Search keyed providers, keyless engines, then public repositories."""
    if (os.getenv("TAVILY_API_KEY") or "").strip():
        results, err = _search_tavily(query, limit)
        if results or not err:
            return results, err
        # A configured provider failed this call; continue through other keyed/free paths.
    if (os.getenv("EXA_API_KEY") or "").strip():
        results, err = _search_exa(query, limit)
        if results or not err:
            return results, err
    duckduckgo_results, duckduckgo_error = _search_duckduckgo_html(query, limit)
    if duckduckgo_results:
        return duckduckgo_results, duckduckgo_error
    bing_results, bing_error = _search_bing_html(query, limit)
    if bing_results:
        degraded = (
            f"{duckduckgo_error}; Bing fallback succeeded"
            if duckduckgo_error
            else "Bing fallback succeeded"
        )
        return bing_results, degraded
    github_results, github_error = _search_github_repositories(query, limit)
    if github_results:
        failures = "; ".join(reason for reason in (duckduckgo_error, bing_error) if reason)
        degraded = (
            f"{failures}; GitHub repository fallback succeeded"
            if failures
            else "GitHub repository fallback succeeded"
        )
        return github_results, degraded
    failures = "; ".join(
        reason for reason in (duckduckgo_error, bing_error, github_error) if reason
    )
    return [], failures


def _web_search_provider_order(query: str) -> tuple:
    lowered = query.lower()
    has_code_signal = any(token in lowered for token in (".lean", " coq", " rocq", ".v", " code"))
    has_identifier = bool(
        re.search(r"\b[A-Z][A-Za-z0-9]*\.[A-Za-z0-9_.]+|\b[a-zA-Z0-9]+_[a-zA-Z0-9_]+\b", query)
    )
    has_paper_signal = any(
        token in lowered
        for token in (
            "paper",
            "article",
            "arxiv",
            "doi",
            "citation",
            "formal proof",
            "formalization",
            "formalisation",
        )
    )
    has_general_web_signal = any(
        token in lowered
        for token in (
            " api ",
            " changelog",
            " configure",
            " documentation",
            " docs",
            " error ",
            " how to ",
            " install",
            " latest",
            " release",
            " setup",
            " version",
        )
    )
    # General web search is always appended so the model can research arbitrary topics (docs,
    # installation, math background), not only papers/code. Specialists stay routed by signal.
    if has_code_signal and not has_paper_signal:
        return (_search_sourcegraph_code, _search_general_web)
    if has_identifier and not has_paper_signal:
        return (_search_sourcegraph_code, _search_general_web)
    if has_general_web_signal and not has_paper_signal:
        return (_search_general_web,)
    return (
        _search_arxiv,
        _search_semantic_scholar,
        _search_crossref,
        _search_sourcegraph_code,
        _search_general_web,
    )


def _sourcegraph_queries(query: str, limit: int) -> list[tuple[str, str, str]]:
    cleaned_query = _sourcegraph_code_terms(query)
    count = max(1, min(limit, 5))
    lowered = query.lower()
    wants_lean = "lean" in lowered or ".lean" in lowered
    wants_coq = "coq" in lowered or "rocq" in lowered or ".v" in lowered
    if wants_coq and not wants_lean:
        languages = [
            (
                "coq-rocq",
                "Coq/Rocq code",
                "repo:github.com/(rocq|coq|math-comp|rocq-community|coq-community) file:\\.v$",
            )
        ]
    elif wants_lean and not wants_coq:
        languages = [("lean", "Lean code", "file:\\.lean$")]
    else:
        languages = [
            ("lean", "Lean code", "file:\\.lean$"),
            ("coq-rocq", "Coq/Rocq code", "file:\\.v$"),
        ]
    queries: list[tuple[str, str, str]] = []
    for language, source, file_filter in languages:
        queries.append(
            (language, source, f"context:global {cleaned_query} {file_filter} count:{count}")
        )
        if " OR " not in cleaned_query and " " in cleaned_query:
            relaxed = " OR ".join(part for part in cleaned_query.split() if part)
            queries.append(
                (language, source, f"context:global ({relaxed}) {file_filter} count:{count}")
            )
    return queries


def _sourcegraph_code_terms(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.+-]+", query)
    identifiers = [token for token in tokens if "." in token or "_" in token]
    if identifiers:
        return " ".join(identifiers[:3])
    terms = [
        token for token in tokens if len(token) > 2 and token.lower() not in CODE_SEARCH_STOPWORDS
    ]
    return " ".join(terms[:6]) or _normalize_whitespace(query)


def _search_sourcegraph_code(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Query Sourcegraph for code snippets in Lean/Coq repositories via GraphQL API. Generates language-specific queries (Lean and/or Coq/Rocq), fetches FileMatch results, and extracts repository, file path, code preview, and line numbers. Returns (results, error_msgs) where results is a list of code-match dicts and error_msgs is a semicolon-joined string of per-language failures."""
    graphql_query = """
query LeanFlowCodeSearch($query: String!) {
  search(query: $query, version: V3) {
    results {
      results {
        __typename
        ... on FileMatch {
          repository { name url }
          file { path url }
          lineMatches { preview lineNumber }
        }
      }
    }
  }
}
"""
    results: list[dict[str, Any]] = []
    degraded: list[str] = []
    for language, source, sourcegraph_query in _sourcegraph_queries(query, limit):
        try:
            response = requests.post(
                SOURCEGRAPH_GRAPHQL_URL,
                json={"query": graphql_query, "variables": {"query": sourcegraph_query}},
                headers=_research_headers(),
                timeout=SOURCEGRAPH_SEARCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            degraded.append(f"Sourcegraph {language} search unavailable: {exc}")
            continue
        if payload.get("errors"):
            degraded.append(f"Sourcegraph {language} search returned errors")
            continue
        matches = payload.get("data", {}).get("search", {}).get("results", {}).get("results", [])
        for match in matches:
            if not isinstance(match, dict) or match.get("__typename") != "FileMatch":
                continue
            repo = match.get("repository") if isinstance(match.get("repository"), dict) else {}
            file_info = match.get("file") if isinstance(match.get("file"), dict) else {}
            repo_name = _normalize_whitespace(repo.get("name"))
            path = _normalize_whitespace(file_info.get("path"))
            file_url = urljoin(
                "https://sourcegraph.com", _normalize_whitespace(file_info.get("url"))
            )
            line_matches = (
                match.get("lineMatches") if isinstance(match.get("lineMatches"), list) else []
            )
            preview = ""
            line_number = None
            if line_matches:
                first_line = line_matches[0] if isinstance(line_matches[0], dict) else {}
                preview = _truncate_text(first_line.get("preview"), 300)
                line_number = first_line.get("lineNumber")
            url = f"{file_url}#L{line_number}" if line_number else file_url
            title = f"{repo_name}:{path}" if repo_name and path else path or repo_name
            if title or url:
                results.append(
                    {
                        "provider": "sourcegraph",
                        "kind": "code",
                        "title": title,
                        "url": url,
                        "snippet": preview,
                        "source": source,
                        "repository": repo_name,
                        "path": path,
                        "line": line_number,
                        "language": language,
                    }
                )
    return results, "; ".join(degraded)
