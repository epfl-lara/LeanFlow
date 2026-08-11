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

from tools.implementations import web_fetch, web_tools
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


def test_web_fetch_cools_down_repeated_terminal_failure(monkeypatch):
    calls = {"n": 0}
    url = "https://example.net/unavailable-on-both-backends"

    def fake_get(*_args, **_kwargs):
        calls["n"] += 1
        raise TimeoutError("read timeout")

    web_fetch._FETCH_FAILURE_CACHE.clear()
    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    first = json.loads(_run(web_fetch.web_fetch_tool(url)))
    second = json.loads(_run(web_fetch.web_fetch_tool(url)))

    assert first["provider_called"] is True
    assert first["cached"] is False
    assert second["provider_called"] is False
    assert second["cached"] is True
    assert second["retry_after_seconds"] >= 1
    assert calls["n"] == 2  # Jina and direct once, not twice each.


def test_web_fetch_uses_direct_get_for_raw_github_text(monkeypatch):
    captured = {}
    source = "import Mathlib\n\ntheorem demo : True := by\n  trivial\n"

    def fake_get(url, *, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse(text=source, headers={"Content-Type": "text/plain"})

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)

    result = json.loads(
        _run(
            web_fetch.web_fetch_tool(
                "https://raw.githubusercontent.com/example/repo/main/Demo.lean"
            )
        )
    )

    assert result["success"] is True
    assert result["backend"] == "direct"
    assert result["content"] == source.strip()
    assert captured["url"].startswith("https://raw.githubusercontent.com/")


def test_web_fetch_blocks_repository_urls_in_clean_room(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(
        web_fetch.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("blocked repository URL reached requests"),
    )

    result = json.loads(
        _run(
            web_fetch.web_fetch_tool(
                "https://raw.githubusercontent.com/example/repo/main/Demo.lean"
            )
        )
    )

    assert "Repository-backed research is disabled" in result["error"]


def test_web_download_blocks_repository_urls_in_clean_room(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(
        web_fetch.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("blocked repository URL reached requests"),
    )

    result = json.loads(
        web_fetch.web_download_tool("https://github.com/example/repo/archive/main.zip")
    )

    assert "Repository-backed research is disabled" in result["error"]


def test_web_fetch_blocks_active_problem_url_in_clean_room(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )
    monkeypatch.setattr(
        web_fetch.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("blocked solution URL reached requests"),
    )

    result = json.loads(
        _run(web_fetch.web_fetch_tool("https://example.org/olympiads/imo-2026-problem-6-solution"))
    )

    assert "Prior-solution research is disabled" in result["error"]


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


def test_web_summarizer_uses_one_bounded_provider_attempt(monkeypatch):
    calls: list[dict] = []

    async def fail_once(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("provider read timeout")

    monkeypatch.setattr(web_tools, "async_call_llm", fail_once)

    with pytest.raises(TimeoutError, match="provider read timeout"):
        _run(web_tools._call_summarizer_llm("long content", "", None))

    assert len(calls) == 1
    assert calls[0]["timeout"] == web_tools.SUMMARIZER_TIMEOUT_SECONDS


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


class _StreamingResponse:
    """Minimal stand-in for a streaming requests.Response context manager."""

    def __init__(self, chunks, *, status_code=200, headers=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        yield from self._chunks


def test_web_download_saves_file_to_workspace(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_get(url, *, headers=None, timeout=None, stream=False):
        assert stream is True
        return _StreamingResponse(
            [b"%PDF-1.4 ", b"data"], headers={"Content-Type": "application/pdf"}
        )

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    out = json.loads(web_fetch.web_download_tool("https://example.org/paper.pdf"))

    assert out["success"] is True
    assert out["bytes"] == len(b"%PDF-1.4 data")
    assert out["content_type"] == "application/pdf"
    saved = tmp_path / ".leanflow" / "downloads" / "paper.pdf"
    assert saved.exists() and saved.read_bytes() == b"%PDF-1.4 data"
    assert out["path"] == str(saved.resolve())


def test_web_download_enforces_size_cap(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_get(url, *, headers=None, timeout=None, stream=False):
        return _StreamingResponse([b"x" * 1000], headers={"Content-Type": "text/plain"})

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    out = json.loads(web_fetch.web_download_tool("https://e/big.bin", max_bytes=100))

    assert "error" in out
    assert not (tmp_path / ".leanflow" / "downloads" / "big.bin").exists()


def test_web_download_sanitizes_filename(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)

    def fake_get(url, *, headers=None, timeout=None, stream=False):
        return _StreamingResponse([b"d"], headers={})

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    out = json.loads(web_fetch.web_download_tool("https://e/f", filename="../../etc/passwd"))

    downloads = (tmp_path / ".leanflow" / "downloads").resolve()
    assert Path(out["path"]).parent == downloads
    assert ".." not in Path(out["path"]).name


def test_web_download_rejects_empty_url():
    assert "error" in json.loads(web_fetch.web_download_tool(""))


def test_web_download_rejects_symlinked_downloads_escape(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # .leanflow/downloads is a symlink escaping the project sandbox.
    (project / ".leanflow").mkdir()
    (project / ".leanflow" / "downloads").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(project)

    def fake_get(url, *, headers=None, timeout=None, stream=False):
        return _StreamingResponse([b"d"], headers={})

    monkeypatch.setattr(web_fetch.requests, "get", fake_get)
    out = json.loads(web_fetch.web_download_tool("https://e/f"))

    assert "error" in out
    assert "escapes the project sandbox" in out["error"]
    assert not any(outside.iterdir())  # nothing was written outside the project


def test_web_download_registered_in_web_toolsets():
    from core.toolsets import resolve_toolset

    assert "web_download" in set(resolve_toolset("web"))
    assert "web_download" in set(resolve_toolset("leanflow-prove-worker"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
