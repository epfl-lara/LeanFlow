"""Verify bounded research evidence, clean-room filtering, and safe resource storage."""

from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import session_research as research


def test_semantic_scholar_key_is_not_sent_to_other_research_providers(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("S2_API_KEY", raising=False)
    captured = []

    def request(url, **kwargs):
        captured.append((url, kwargs["headers"]))
        return SimpleNamespace(
            status_code=200,
            text='<feed xmlns="http://www.w3.org/2005/Atom"/>',
            raise_for_status=lambda: None,
            json=lambda: {
                "data": {} if url == research.providers.SOURCEGRAPH_GRAPHQL_URL else [],
                "message": {"items": []},
            },
        )

    monkeypatch.setattr(research.providers.requests, "get", request)
    monkeypatch.setattr(research.providers.requests, "post", request)
    research.providers._search_arxiv("formal mathematics", 1)
    research.providers._search_crossref("formal mathematics", 1)
    research.providers._search_sourcegraph_code("example_lemma", 1)
    research.providers._search_semantic_scholar("formal mathematics", 1)
    assert len(captured) >= 4
    for url, headers in captured:
        if url == research.providers.SEMANTIC_SCHOLAR_SEARCH_URL:
            assert headers["x-api-key"] == "synthetic-test-key"
        else:
            assert "x-api-key" not in headers


def test_search_calls_only_two_providers_and_bounds_evidence(monkeypatch):
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return [
            {"url": f"https://example.org/{n}", "title": "T" * 500, "snippet": "s" * 3000}
            for n in range(4)
        ], ""

    monkeypatch.setattr(research.providers, "_search_arxiv", search)
    monkeypatch.setattr(research.providers, "_search_duckduckgo_html", search)
    result = research.web_search("bounded mathematics", 2)
    assert result["success"] and result["model_calls"] == 0
    assert calls == [("bounded mathematics", 2), ("bounded mathematics", 2)]
    assert len(result["results"]) == 2
    assert len(result["results"][0]["title"]) == 300
    assert len(result["results"][0]["snippet"]) == 1200


def test_search_denies_clean_room_query_before_provider_calls(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_CLEAN_ROOM_TASK_LABELS", "erdos123")
    monkeypatch.setattr(research.providers, "_search_arxiv", lambda *_: pytest.fail("network"))
    result = research.web_search("proof erdos%31%32%33")
    assert not result["success"]
    assert result["status"] == "clean_room_denied"


def test_search_filters_repositories_and_target_solutions(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_CLEAN_ROOM_TASK_LABELS", "problem987")
    items = [
        {"url": "https://github.com/example/proofs"},
        {"url": "https://example.org/unrelated-url", "snippet": "Solution of problem987"},
        {"url": "https://example.org/general-lemma", "title": "General lemma"},
    ]
    monkeypatch.setattr(research.providers, "_search_arxiv", lambda *_: (items, ""))
    monkeypatch.setattr(research.providers, "_search_duckduckgo_html", lambda *_: ([], "throttled"))
    result = research.web_search("a general lemma")
    assert [item["url"] for item in result["results"]] == ["https://example.org/general-lemma"]
    assert result["degraded_reasons"] == ["throttled"]


@pytest.mark.parametrize("query,limit", [("", 2), ("x" * 1001, 2), ("query", 0), ("query", 11)])
def test_search_rejects_invalid_bounds(query, limit):
    assert not research.web_search(query, limit)["success"]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "ff0e::1"],
)
def test_download_rejects_private_dns_before_connection(monkeypatch, address):
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(ValueError, match="nonpublic"):
        research._public_connection("https://example.org/paper", time.monotonic() + 5)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "https://user:password@example.org", "https://example.org/\nHost:evil"],
)
def test_download_rejects_unsafe_url_syntax(url):
    with pytest.raises(ValueError, match="public HTTP"):
        research._public_connection(url, time.monotonic() + 5)


def test_https_pins_dns_address_but_preserves_hostname(monkeypatch):
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))],
    )
    connection, target, hostname = research._public_connection(
        "https://example.org/paper?q=lean", time.monotonic() + 5
    )
    assert isinstance(connection, research._PinnedHTTPSConnection)
    assert connection.address == "1.1.1.1"
    assert connection.host == hostname == "example.org"
    assert target == "/paper?q=lean"
    raw_socket = object()
    calls = []
    monkeypatch.setattr(research.socket, "create_connection", lambda target, timeout: raw_socket)
    connection.tls_context = SimpleNamespace(
        wrap_socket=lambda sock, server_hostname: calls.append((sock, server_hostname)) or sock
    )
    connection.connect()
    assert calls == [(raw_socket, "example.org")]


class _Response:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.chunks = iter(chunks)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return next(self.chunks, b"")


class _Connection:
    def __init__(self, response):
        self.response = response
        self.sock = None
        self.closed = False

    def request(self, *_args, **_kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_redirect_to_private_address_is_revalidated(monkeypatch):
    public = _Connection(_Response(302, {"Location": "http://127.0.0.1/secret"}))
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda host, *_: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host == "127.0.0.1" else "1.1.1.1", 80),
            )
        ],
    )
    connections = []

    def connect(address, port, timeout):
        connections.append(address)
        return public

    monkeypatch.setattr(research.http.client, "HTTPConnection", connect)
    with pytest.raises(ValueError, match="nonpublic"):
        research._download("http://example.org/start")
    assert connections == ["1.1.1.1"]
    assert public.closed


def test_redirect_to_repository_honors_clean_room(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    connection = _Connection(_Response(302, {"Location": "https://github.com/owner/repo"}))

    def public_connection(url, deadline):
        reason = research._url_policy(url)
        if reason:
            raise ValueError(reason)
        return connection, "/", "example.org"

    monkeypatch.setattr(research, "_public_connection", public_connection)
    with pytest.raises(ValueError, match="Repository-backed"):
        research._download("https://example.org/start")


@pytest.mark.parametrize(
    "headers,chunks", [({"Content-Length": "9"}, ()), ({}, (b"12345", b"6789"))]
)
def test_download_enforces_declared_and_streaming_byte_caps(monkeypatch, headers, chunks):
    monkeypatch.setattr(research, "MAX_RESOURCE_BYTES", 8)
    connection = _Connection(_Response(headers=headers, chunks=chunks))
    monkeypatch.setattr(research, "_public_connection", lambda *_: (connection, "/", "example.org"))
    with pytest.raises(ValueError, match="byte limit"):
        research._download("https://example.org/paper")
    assert connection.closed


def test_truncated_content_is_not_saved(monkeypatch):
    connection = _Connection(_Response(headers={"Content-Length": "9"}, chunks=(b"123",)))
    monkeypatch.setattr(research, "_public_connection", lambda *_: (connection, "/", "example.org"))
    with pytest.raises(ValueError, match="Content-Length"):
        research._download("https://example.org/paper")


def test_fetch_saves_clean_markdown_and_exact_source_with_provenance(monkeypatch, tmp_path):
    payload = b"<html><head><script>hidden()</script></head><body><h1>Title</h1><p>Lemma.</p></body></html>"
    monkeypatch.setattr(
        research, "_download", lambda *_: (payload, "https://example.org/final", "text/html")
    )
    result = research.fetch_resource("https://example.org/start", tmp_path)
    assert result["success"] and result["model_calls"] == 0
    artifact = Path(result["path"])
    assert artifact.is_relative_to(tmp_path / "resources")
    assert artifact.read_text() == "# Title\n\nLemma.\n"
    assert Path(result["source_path"]).read_bytes() == payload
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["url"] == "https://example.org/start"
    assert manifest["resolved_url"] == "https://example.org/final"
    assert research.fetch_resource("https://example.org/start", tmp_path)["status"] == "cached"


def test_pdf_stays_binary_and_does_not_invoke_extraction(monkeypatch, tmp_path):
    payload = b"%PDF-1.7\n\x00pdfbytes"
    monkeypatch.setattr(
        research, "_download", lambda *_: (payload, "https://example.org/p.pdf", "application/pdf")
    )
    result = research.fetch_resource("https://example.org/p.pdf", tmp_path, "paper.pdf")
    assert result["success"]
    assert Path(result["path"]).read_bytes() == payload


@pytest.mark.parametrize("filename", ["../escape", "/absolute", "folder/file", "..", "bad\x00name"])
def test_filename_escape_is_denied_before_network(monkeypatch, tmp_path, filename):
    monkeypatch.setattr(research, "_download", lambda *_: pytest.fail("network"))
    assert not research.fetch_resource("https://example.org", tmp_path, filename)["success"]


def test_resource_directory_symlink_cannot_escape_workspace(monkeypatch, tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "resources").symlink_to(tmp_path)
    monkeypatch.setattr(research, "_download", lambda *_: pytest.fail("network"))
    assert not research.fetch_resource("https://example.org", root)["success"]


def test_existing_resource_is_preserved(monkeypatch, tmp_path):
    directory = tmp_path / "resources"
    directory.mkdir()
    original = directory / "paper.txt"
    original.write_text("user content")
    monkeypatch.setattr(
        research, "_download", lambda *_: (b"new content", "https://example.org/p", "text/plain")
    )
    assert not research.fetch_resource("https://example.org/p", tmp_path, original.name)["success"]
    assert original.read_text() == "user content"
    assert list(directory.iterdir()) == [original]


def test_partial_artifact_is_removed_if_manifest_cannot_be_written(monkeypatch, tmp_path):
    directory = tmp_path / "resources"
    directory.mkdir()
    (directory / "paper.txt.metadata.json").write_text("owned by user")
    monkeypatch.setattr(
        research, "_download", lambda *_: (b"new content", "https://example.org/p", "text/plain")
    )
    assert not research.fetch_resource("https://example.org/p", tmp_path, "paper.txt")["success"]
    assert sorted(path.name for path in directory.iterdir()) == ["paper.txt.metadata.json"]


def test_false_pdf_and_binary_content_are_rejected():
    with pytest.raises(ValueError, match="PDF signature"):
        research._artifact_content(b"<html>failure</html>", "application/pdf")
    with pytest.raises(ValueError, match="textual source"):
        research._artifact_content(b"ziparchive", "application/zip")
