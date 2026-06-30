"""Tests for the web_fetch tool (single-URL fetch-and-read via Jina Reader + fallback).

Coverage:
  - page fetch returns markdown via Jina,
  - PDF URLs are handled by the Jina backend,
  - JINA_API_KEY is attached as a bearer token only when set,
  - the direct-fetch fallback runs on Jina failure,
  - long pages are routed through the summarizer and capped,
  - web_fetch is resolvable in the "web" toolset / registry.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.implementations import web_fetch
from tools.registry import registry


class FakeResponse:
    def __init__(self, *, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _run(coro):
    return asyncio.run(coro)


def test_web_fetch_returns_markdown_from_jina(monkeypatch):
    captured = {}

    def fake_get(url, *, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResponse(text="# Title\n\nReadable markdown body.")

    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://example.com/paper")))

    assert result["success"] is True
    assert result["backend"] == "jina"
    assert "Readable markdown body." in result["content"]
    assert captured["url"] == "https://r.jina.ai/https://example.com/paper"
    # No key configured -> no Authorization header.
    assert "Authorization" not in captured["headers"]


def test_web_fetch_attaches_jina_api_key_when_set(monkeypatch):
    captured = {}

    def fake_get(url, *, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(text="content")

    monkeypatch.setenv("JINA_API_KEY", "jina-secret")
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    _run(web_fetch.web_fetch_tool("https://example.com"))

    assert captured["headers"]["Authorization"] == "Bearer jina-secret"


def test_web_fetch_handles_pdf_url_via_jina(monkeypatch):
    def fake_get(url, *, headers=None, timeout=None):
        # Jina Reader converts the PDF to markdown; we just return the reader output.
        assert url == "https://r.jina.ai/https://arxiv.org/pdf/2401.00001"
        return FakeResponse(text="## Abstract\n\nExtracted PDF text.")

    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://arxiv.org/pdf/2401.00001")))

    assert result["success"] is True
    assert result["backend"] == "jina"
    assert "Extracted PDF text." in result["content"]


def test_web_fetch_falls_back_to_direct_get_on_jina_error(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, *, headers=None, timeout=None):
        calls["n"] += 1
        if url.startswith("https://r.jina.ai/"):
            raise RuntimeError("jina down")
        # Direct fetch of the real URL returns HTML reduced to text.
        return FakeResponse(
            text="<html><head><style>x</style></head><body><p>Hello world</p>"
            "<script>bad()</script></body></html>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://example.com")))

    assert result["success"] is True
    assert result["backend"] == "fallback"
    assert "Hello world" in result["content"]
    # Script/style content is stripped by the stdlib reducer.
    assert "bad()" not in result["content"]
    assert "x" not in result["content"].split("Hello")[0]
    assert calls["n"] == 2  # one Jina attempt + one fallback


def test_fallback_refuses_pdf_without_jina(monkeypatch):
    def fake_get(url, *, headers=None, timeout=None):
        if url.startswith("https://r.jina.ai/"):
            raise RuntimeError("jina down")
        return FakeResponse(text="%PDF-1.7 binary", headers={"Content-Type": "application/pdf"})

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://arxiv.org/pdf/2401.00001")))

    assert "error" in result


def test_web_fetch_summarizes_long_pages(monkeypatch):
    long_text = "word " * 4000  # ~20k chars, well over the summarization threshold

    def fake_get(url, *, headers=None, timeout=None):
        return FakeResponse(text=long_text)

    async def fake_summarize(content, *, url="", title="", model=None, min_length=0):
        return "SUMMARIZED"

    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    monkeypatch.setattr(web_fetch, "process_content_with_llm", fake_summarize)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://example.com")))

    assert result["content"] == "SUMMARIZED"


def test_web_fetch_caps_when_summarizer_unavailable(monkeypatch):
    long_text = "abcdefghij" * 2000  # 20k chars

    def fake_get(url, *, headers=None, timeout=None):
        return FakeResponse(text=long_text)

    async def fake_summarize(content, *, url="", title="", model=None, min_length=0):
        return None  # no auxiliary model available

    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    monkeypatch.setattr(web_fetch, "process_content_with_llm", fake_summarize)

    result = json.loads(_run(web_fetch.web_fetch_tool("https://example.com", max_chars=1000)))

    assert result["truncated"] is True
    assert "[... truncated for context management ...]" in result["content"]
    assert len(result["content"]) <= 1000 + 80


def test_web_fetch_rejects_empty_url():
    result = json.loads(_run(web_fetch.web_fetch_tool("")))
    assert "error" in result


def test_web_fetch_registered_and_resolvable_in_web_toolset():
    # web_fetch self-registers when the module is imported.
    definitions = registry.get_definitions({"web_fetch"}, quiet=True)
    assert [item["function"]["name"] for item in definitions] == ["web_fetch"]

    from toolsets import resolve_toolset

    assert "web_fetch" in resolve_toolset("web")
    # And it reaches the prove-worker surface through the included "web" toolset.
    assert "web_fetch" in resolve_toolset("leanflow-prove-worker")


def test_html_to_text_strips_tags_and_scripts():
    html = "<div><h1>Head</h1><script>evil()</script><p>Body text.</p></div>"
    text = web_fetch._html_to_text(html)
    assert "Head" in text
    assert "Body text." in text
    assert "evil()" not in text
    assert "<" not in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
