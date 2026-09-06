"""Retrieve bounded research results and source artifacts without hidden model calls."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from tools.implementations import web_research_providers as providers
from tools.utilities.repository_research_policy import (
    repository_url_block_reason,
    solution_research_query_block_reason,
    solution_research_text_block_reason,
    solution_research_url_block_reason,
)

MAX_RESOURCE_BYTES = 8 * 1024 * 1024
RESOURCE_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 4
_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prover-resource-dns")


def _url_policy(url: str) -> str:
    """Return clean-room denials, including percent-encoded target spellings."""
    for _ in range(4):
        reason = repository_url_block_reason(url) or solution_research_url_block_reason(url)
        if reason:
            return reason
        decoded = unquote(url)
        if decoded == url:
            break
        url = decoded
    return ""


#: Words so common in mathematical queries that matching them says little about
#: relevance; they still count, but far less than a distinctive term.
_GENERIC_QUERY_TERMS = frozenset(
    {
        "abstract",
        "and",
        "arxiv",
        "bound",
        "bounds",
        "conjecture",
        "for",
        "formal",
        "formalization",
        "from",
        "lemma",
        "math",
        "mathematics",
        "new",
        "note",
        "notes",
        "the",
        "paper",
        "problem",
        "proof",
        "proofs",
        "result",
        "results",
        "survey",
        "theorem",
        "theorems",
        "with",
    }
)
_DASH_TRANSLATION = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_COMPOUND_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


def _normalize_search_text(text: str) -> str:
    """Casefold and unify dashes so ``Beck–Fiala`` and ``beck-fiala`` compare equal."""
    return re.sub(r"\s+", " ", str(text or "").translate(_DASH_TRANSLATION).casefold()).strip()


def _query_terms(query: str) -> dict[str, float]:
    """Return the query's weighted terms; generic mathematical words weigh less."""
    terms: dict[str, float] = {}
    for token in _TOKEN_RE.findall(_normalize_search_text(query)):
        if len(token) < 2:
            continue
        terms.setdefault(token, 0.3 if token in _GENERIC_QUERY_TERMS else 1.0)
    return terms


def result_relevance(
    query: str, title: str, snippet: str, *, terms: Mapping[str, float] | None = None
) -> tuple[float, list[str]]:
    """Score one result against the query by term coverage and phrase matches.

    Title matches count fully, snippet matches partially, and the whole phrase
    or a hyphenated compound such as ``beck-fiala`` earns a bonus.  The score is
    provider-neutral, so an unrelated paper that a provider ranked first sinks
    below a modest web hit that actually names the subject.
    """
    weights = dict(terms) if terms is not None else _query_terms(query)
    if not weights:
        return 0.0, []
    title_text = _normalize_search_text(title)
    snippet_text = _normalize_search_text(snippet)
    title_tokens = set(_TOKEN_RE.findall(title_text))
    snippet_tokens = set(_TOKEN_RE.findall(snippet_text))
    matched: list[str] = []
    score = 0.0
    for term, weight in weights.items():
        if term in title_tokens:
            score += weight
            matched.append(term)
        elif term in snippet_tokens:
            score += 0.6 * weight
            matched.append(term)
    score /= sum(weights.values())
    phrase = _normalize_search_text(query)
    if len(weights) > 1 and phrase:
        if phrase in title_text:
            score += 0.5
        elif phrase in snippet_text:
            score += 0.25
        else:
            for compound in _COMPOUND_RE.findall(phrase):
                if compound in title_text:
                    score += 0.3
                    break
                if compound in snippet_text:
                    score += 0.15
                    break
    return round(min(1.0, score), 3), matched


def web_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search two existing providers once each and return bounded, filtered evidence.

    Results are ranked by :func:`result_relevance` and, at equal relevance,
    interleaved across providers by their own rank, so one provider's broad
    disjunction query cannot push the other's relevant hits past the limit.
    ``providers`` reports what each provider returned, kept, or failed with.
    """
    query = query.strip()
    if not query or len(query) > 1000 or not 1 <= limit <= 10:
        return {
            "success": False,
            "error": "Use a nonempty query up to 1000 characters and limit 1–10",
        }
    reason = solution_research_query_block_reason(unquote(query))
    if reason:
        return {"success": False, "status": "clean_room_denied", "error": reason}
    terms = _query_terms(query)
    ranked: list[tuple[float, int, int, dict[str, Any]]] = []
    failures: list[str] = []
    provider_reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Avoid the general provider's fallback cascade and the extraction summarizer.
    searchers = (("arxiv", providers._search_arxiv), ("web", providers._search_duckduckgo_html))
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="prover-search") as pool:
        jobs = [(label, pool.submit(searcher, query, limit)) for label, searcher in searchers]
        for order, (label, job) in enumerate(jobs):
            report: dict[str, Any] = {"name": label, "returned": 0, "kept": 0, "degraded": ""}
            provider_reports.append(report)
            try:
                found, error = job.result()
            except Exception as exc:
                report["degraded"] = str(exc)[:300]
                failures.append(str(exc)[:300])
                continue
            if error:
                report["degraded"] = error[:300]
                failures.append(error[:300])
            report["returned"] = len(found)
            for rank, item in enumerate(found[:limit]):
                url = str(item.get("url") or "")[:2000]
                evidence = " ".join(str(item.get(key) or "") for key in ("title", "url", "snippet"))
                if (
                    not url.startswith(("https://", "http://"))
                    or url in seen
                    or _url_policy(url)
                    or solution_research_text_block_reason(evidence, surface="search result")
                ):
                    continue
                seen.add(url)
                title = str(item.get("title") or "")[:300]
                snippet = str(item.get("snippet") or "")[:1200]
                relevance, matched = result_relevance(query, title, snippet, terms=terms)
                report["kept"] += 1
                ranked.append(
                    (
                        -relevance,
                        rank,
                        order,
                        {
                            "url": url,
                            "title": title,
                            "snippet": snippet,
                            "provider": str(item.get("provider") or label)[:80],
                            "relevance": relevance,
                            "matched_terms": matched[:12],
                        },
                    )
                )
    ranked.sort(key=lambda entry: entry[:3])
    results = [entry[3] for entry in ranked]
    return {
        "success": bool(results),
        "status": "complete" if results else "no_results",
        "query": query,
        "results": results[:limit],
        "truncated": len(results) > limit,
        "ranking": "relevance descending; ties interleave providers by their own rank",
        "provider_calls": len(searchers),
        "providers": provider_reports,
        "degraded_reasons": failures,
        "model_calls": 0,
    }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated address while retaining hostname certificate checks."""

    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(hostname, port, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        """Pin the TCP destination and authenticate the original HTTPS hostname."""
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _public_connection(url: str, deadline: float) -> tuple[http.client.HTTPConnection, str, str]:
    """Reject private destinations and pin DNS so a second lookup cannot rebind."""
    reason = _url_policy(url)
    if reason:
        raise ValueError(reason)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or re.search(r"[\x00-\x20\x7f]", url)
    ):
        raise ValueError("Resource URLs must be public HTTP(S) URLs without credentials")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Resource deadline exceeded")
    future = _DNS_POOL.submit(socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
    try:
        addresses = future.result(timeout=remaining)
    finally:
        future.cancel()
    ips = [str(address[4][0]) for address in addresses]
    if not ips or any(
        not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
        for address in ips
    ):
        raise ValueError("Resource URL resolves to a private or nonpublic network address")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Resource deadline exceeded")
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(hostname, ips[0], port, remaining)
    else:
        connection = http.client.HTTPConnection(ips[0], port, timeout=remaining)
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        host_header = f"{host_header}:{port}"
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return connection, target, host_header


def _abort_connection(connection: http.client.HTTPConnection) -> None:
    """Interrupt slow headers or body streams when the overall deadline expires."""
    active_socket = connection.sock
    if active_socket is not None:
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


def _download(url: str) -> tuple[bytes, str, str]:
    """Follow only validated redirects and enforce cumulative time and byte ceilings."""
    deadline = time.monotonic() + RESOURCE_TIMEOUT_SECONDS
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        connection, target, hostname = _public_connection(current, deadline)
        timer = threading.Timer(
            max(0, deadline - time.monotonic()), _abort_connection, (connection,)
        )
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Host": hostname,
                    "User-Agent": "LeanFlow/prover-research",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Resource redirect has no Location")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise ValueError(f"Resource request returned HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Compressed transfer content is not supported")
            declared = response.getheader("Content-Length")
            if declared is not None and int(declared) > MAX_RESOURCE_BYTES:
                raise ValueError("Resource exceeds the download byte limit")
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Resource deadline exceeded")
                chunk = response.read1(min(65536, MAX_RESOURCE_BYTES - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESOURCE_BYTES:
                    raise ValueError("Resource exceeds the download byte limit")
                chunks.append(chunk)
            if declared is not None and size != int(declared):
                raise ValueError("Resource body does not match its Content-Length")
            return b"".join(chunks), current, response.getheader("Content-Type", "text/plain")
        finally:
            timer.cancel()
            connection.close()
    raise ValueError("Resource exceeds the redirect limit")


class _ReadableHTML(HTMLParser):
    """Extract a small Markdown representation without scripts or model summarization."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head", "nav", "footer", "noscript"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in {"p", "div", "section", "br", "pre"}:
            self.parts.append("\n\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in {
            "p",
            "div",
            "section",
            "pre",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _artifact_content(payload: bytes, content_type: str) -> tuple[bytes, str]:
    """Preserve PDF/text sources or deterministically reduce HTML to Markdown."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if payload.startswith(b"%PDF-"):
        return payload, ".pdf"
    if media_type == "application/pdf":
        raise ValueError("Response advertised PDF but has no PDF signature")
    if not (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "application/xhtml+xml"}
    ):
        raise ValueError("Resource must be a PDF, HTML page, or textual source")
    text = payload.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("Binary resource content is not supported")
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _ReadableHTML()
        parser.feed(text)
        text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()
        return (text + "\n").encode("utf-8"), ".md"
    return payload, ".md" if "markdown" in media_type else ".txt"


def fetch_resource(url: str, workspace: Path, filename: str = "") -> dict[str, Any]:
    """Save one bounded public resource and a URL/hash provenance manifest."""
    from leanflow_cli.workflows.prover.resource_handoff import record_download

    created: list[Path] = []
    try:
        url = url.strip()
        if filename and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", filename) or ".." in filename
        ):
            raise ValueError("Resource filename must be a simple name without path components")
        root = workspace.resolve()
        directory = (root / "resources").resolve()
        if not directory.is_relative_to(root):
            raise ValueError("Resource directory escapes the workspace")
        payload, resolved_url, content_type = _download(url)
        content, extension = _artifact_content(payload, content_type)
        source_hash = hashlib.sha256(payload).hexdigest()
        identity = hashlib.sha256((url + source_hash).encode()).hexdigest()[:24]
        destination = directory / (filename or f"{identity}{extension}")
        if destination.suffix.lower() in {".html", ".htm"} and extension == ".md":
            destination = destination.with_suffix(".md")
        source = destination.with_name(destination.name + ".source")
        manifest = destination.with_name(destination.name + ".metadata.json")
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "url": url,
            "resolved_url": resolved_url,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "content_type": content_type,
            "source_sha256": source_hash,
            "sha256": hashlib.sha256(content).hexdigest(),
            "source_bytes": len(payload),
            "bytes": len(content),
            "path": str(destination),
            "source_path": str(source),
            "model_calls": 0,
        }
        if (
            all(
                path.is_file() and not path.is_symlink() for path in (destination, source, manifest)
            )
            and destination.stat().st_size == len(content)
            and source.stat().st_size == len(payload)
            and manifest.stat().st_size <= 16384
        ):
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                isinstance(previous, dict)
                and previous.get("url") == url
                and destination.read_bytes() == content
                and source.read_bytes() == payload
            ):
                record_download(root, metadata)
                return {
                    "success": True,
                    "status": "cached",
                    "manifest_path": str(manifest),
                    **metadata,
                }
        # Exclusive writes preserve existing user resources and reject symlink targets.
        with destination.open("xb") as handle:
            created.append(destination)
            handle.write(content)
        with source.open("xb") as handle:
            created.append(source)
            handle.write(payload)
        with manifest.open("x", encoding="utf-8") as handle:
            created.append(manifest)
            json.dump(metadata, handle, indent=2)
        record_download(root, metadata)
        return {"success": True, "status": "saved", "manifest_path": str(manifest), **metadata}
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "success": False,
            "status": "resource_error",
            "error": str(exc)[:500],
            "model_calls": 0,
        }
