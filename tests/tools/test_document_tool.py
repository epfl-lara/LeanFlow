from __future__ import annotations

import json

from tools.document_tool import formalization_document_inspect_tool


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
