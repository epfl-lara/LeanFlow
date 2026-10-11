"""Exercise role-bound Lean tools and isolated scratch authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.session_tools import SessionTools


def test_tools_enforce_role_paths_and_symlinks(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    canonical = tmp_path / "Main.lean"
    canonical.write_text("theorem goal : True := by sorry")
    (job / "escape.lean").symlink_to(canonical)
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    for path in (
        str(canonical),
        "../Main.lean",
        "escape.lean",
        "PLAN.md",
        "request-count.json",
        "../.runtime/job/request-count.json",
    ):
        assert not prover.invoke("write_file", {"path": path, "content": "changed"})["success"]
    assert prover.invoke("write_file", {"path": "PLAN_job.md", "content": "Try trivial"})["success"]
    assert canonical.read_text().endswith("by sorry")
    assert not prover.invoke("lean_reasoning_help", {})["success"]
    orchestrator = SessionTools(
        role="orchestrator", project_root=tmp_path, workspace=job, context={}
    )
    assert not orchestrator.invoke("lean_check", {})["success"]
    assert not orchestrator.invoke("lean_search", {"query": "trivial"})["success"]


def test_lean_check_schema_distinguishes_target_name_from_complete_replacement(
    tmp_path: Path,
) -> None:
    tools = SessionTools(role="prover", project_root=tmp_path, workspace=tmp_path, context={})
    schema = next(
        item["function"] for item in tools.schemas() if item["function"]["name"] == "lean_check"
    )
    properties = schema["parameters"]["properties"]
    assert "exact existing declaration name" in properties["declaration"]["description"]
    assert properties["declaration"]["examples"] == ["test", "Namespace.target"]
    assert "COMPLETE declaration" in properties["replacement"]["description"]
    assert "signature AND body" in properties["replacement"]["description"]


def test_read_file_previews_a_non_utf8_document(tmp_path: Path) -> None:
    """A latin-1 .tex/.bib doc (now reachable via search_project) must preview.

    Decoding with replacement means a non-UTF-8 byte yields the replacement
    character instead of failing the whole read, and byte offsets keep working.
    """
    doc = tmp_path / "notes.tex"
    doc.write_bytes(b"Cauchy-Schwarz r\xe9sum\xe9\n")  # 0xe9 is invalid UTF-8
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})

    result = prover.invoke("read_file", {"path": str(doc)})

    assert result["success"] is True
    assert "Cauchy" in result["content"] and "Schwarz" in result["content"]
    assert "�" in result["content"]  # the non-UTF-8 bytes became U+FFFD


def test_read_file_pagination_never_skips_source_bytes(tmp_path: Path) -> None:
    """next_offset counts SOURCE bytes, not re-encoded replacement-char bytes.

    A malformed byte decodes to a 3-byte U+FFFD; counting the re-encoded length
    would advance the offset past unread bytes and drop them. Reading one char
    at a time must visit every byte exactly once.
    """
    doc = tmp_path / "notes.tex"
    doc.write_bytes(b"\xe9ABC")  # bad lead byte then three ASCII bytes
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})

    seen = []
    offset = 0
    for _ in range(10):
        out = prover.invoke("read_file", {"path": str(doc), "offset": offset, "limit": 1})
        assert out["success"] is True
        if out["next_offset"] == offset:  # end of file
            break
        seen.append(out["content"])
        offset = out["next_offset"]

    assert "".join(seen) == "�ABC"  # the 'A' and 'B' are not skipped
    assert offset == 4  # every one of the four source bytes was consumed


def test_read_file_malformed_multibyte_respects_the_character_limit(tmp_path: Path) -> None:
    """A malformed multibyte sequence must not overflow the requested char limit.

    Bytes like F4 90 80 80 are continuation-shaped but above U+10FFFF, so Python
    decodes them to SEVERAL U+FFFD; a request for one character must still return
    one, not the whole run.
    """
    from leanflow_cli.workflows.prover.session_tools import _utf8_window

    for raw in (b"\xf4\x90\x80\x80\x41", b"\xed\xa0\x80A", b"\xc0\x80B", b"\xf5\xf5A"):
        content, consumed = _utf8_window(raw, 1)
        assert len(content) == 1  # never more than the limit
        assert 1 <= consumed <= len(raw)  # forward progress, within the window

    doc = tmp_path / "bad.tex"
    doc.write_bytes(b"\xf4\x90\x80\x80done")
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    out = prover.invoke("read_file", {"path": str(doc), "limit": 1})
    assert out["success"] is True and len(out["content"]) == 1


def test_repeated_search_has_no_arbitrary_per_job_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import session_search

    monkeypatch.setattr(
        session_search, "search_sources", lambda *_, **kwargs: {"success": True, "matches": []}
    )
    job = tmp_path / "job"
    job.mkdir()
    tools = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    for _ in range(3):
        tools.invoke("write_file", {"path": "PLAN_job.md", "content": "Same idea"})
        result = tools.invoke("search_project", {"query": "same lemma"})
    assert result["success"] is True
