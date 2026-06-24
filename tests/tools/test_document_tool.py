from __future__ import annotations

import json

from tools.implementations.document_tool import formalization_document_inspect_tool, read_pdf_tool


def test_formalization_document_inspect_tool_returns_latex_summary(tmp_path):
    project = tmp_path / "Demo"
    source = project / "docs" / "paper.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\\section{Main}\\begin{theorem}\\label{thm:toy}True.\\end{theorem}\n",
        encoding="utf-8",
    )

    payload = json.loads(
        formalization_document_inspect_tool(
            "docs/paper.tex",
            cwd=str(project),
            project_root=str(project),
        )
    )

    assert payload["success"] is True
    assert payload["source_relative"] == "docs/paper.tex"
    assert "extracted_text" not in payload
    assert payload["text_excerpt"]
    assert payload["theorem_blocks"][0]["label"] == "thm:toy"


def test_read_pdf_tool_returns_extracted_text(monkeypatch):
    def fake_inspect(path, *, project_root=None, cwd=None):
        return {
            "success": True,
            "source_kind": "pdf",
            "source_relative": path,
            "extracted_text": "A" * 2000,
            "text_excerpt": "A" * 2000,
            "pdf_tools": {"pdftotext": True, "pdfinfo": True, "pdfimages": True},
            "degraded_reasons": [],
        }

    monkeypatch.setattr(
        "tools.implementations.document_tool.inspect_formalization_document", fake_inspect
    )

    payload = json.loads(
        read_pdf_tool(
            "docs/paper.pdf",
            cwd="/project",
            project_root="/project",
            max_chars=1200,
        )
    )

    assert payload["success"] is True
    assert payload["source_kind"] == "pdf"
    assert len(payload["extracted_text"]) == 1200
    assert payload["text_truncated"] is True
    assert payload["text_limit_chars"] == 1200


def test_read_pdf_tool_rejects_non_pdf(monkeypatch):
    def fake_inspect(path, *, project_root=None, cwd=None):
        return {
            "success": True,
            "source_kind": "latex",
            "source_relative": path,
        }

    monkeypatch.setattr(
        "tools.implementations.document_tool.inspect_formalization_document", fake_inspect
    )

    payload = json.loads(read_pdf_tool("docs/paper.tex", cwd="/project", project_root="/project"))

    assert payload["success"] is False
    assert "only accepts" in payload["error"]
