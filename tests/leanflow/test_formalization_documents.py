from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization.formalization_documents import (
    FormalizationDocumentError,
    inspect_formalization_document,
    prepare_formalization_document_context,
    resolve_formalization_document,
)


def _write_sample_tex(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""
\title{Tiny Source}
\section{Main result}

\begin{definition}
  \label{def:good}
  A good number is a natural number equal to zero.
\end{definition}

\begin{theorem}[Toy theorem]
  \label{thm:zero_good}
  \uses{def:good}
  Zero is good.
\end{theorem}
""".strip(),
        encoding="utf-8",
    )


@pytest.fixture
def evaluated_layout(monkeypatch):
    def resolve(root, document_id):
        prefix = "Main/Formalization/D" + document_id.removeprefix("doc-")
        return {
            "accepted": True,
            "library": "Main",
            "root_file": "Main.lean",
            "owned_prefix": prefix,
            "aggregator": prefix + ".lean",
            "module_prefix": prefix.replace("/", "."),
        }

    monkeypatch.setattr("leanflow_cli.formalization.intake.resolve_layout", resolve)
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.lean_build.validate_build_inputs", lambda *a, **kw: {}
    )
    return resolve


def test_prepare_formalization_document_context_freezes_planner_artifacts(
    tmp_path, evaluated_layout
):
    project = tmp_path / "Demo"
    source = project / "docs" / "paper.tex"
    _write_sample_tex(source)
    context = prepare_formalization_document_context(
        project_root=project,
        cwd=project,
        workflow_args="docs/paper.tex",
    )
    document_id, revision = context.metadata["document_id"], context.metadata["revision_id"]
    layout = evaluated_layout(project, document_id)
    assert context.source_relative == "docs/paper.tex"
    assert context.source_kind == "latex"
    assert context.target_lean_relative == layout["aggregator"]
    assert context.blueprint_path == project / "formalization" / document_id / "Blueprint.md"
    assert context.context_path.is_file()
    assert context.manifest_path.is_file()
    assert context.extracted_text_path.is_file()
    assert context.blueprint_skill_path.is_file()
    assert not context.blueprint_path.exists()
    assert not context.target_lean_path.exists()
    assert not list(project.rglob("*.lean"))
    manifest = json.loads(context.manifest_path.read_text())
    assert manifest["document_id"] == document_id
    assert manifest["revision_id"] == revision
    assert {item["label"] for item in manifest["inventory"]["items"]} == {
        "def:good",
        "thm:zero_good",
    }
    assert manifest["inventory"]["files"]["docs/paper.tex"]["text"] == source.read_text()
    guidance = context.context_path.read_text()
    assert "formalization-proposal.json" in guidance
    assert "Every selected region" in guidance
    assert "Author self-approval never satisfies a gate" in guidance
    env = context.to_env()
    assert env["LEANFLOW_WORKFLOW_CONTEXT"] == str(context.context_path)
    assert env["LEANFLOW_FORMALIZATION_TARGET_FILE"] == layout["aggregator"]
    assert env["LEANFLOW_FORMALIZATION_DOCUMENT_ID"] == document_id
    assert env["LEANFLOW_FORMALIZATION_REVISION"] == revision
    assert env["LEANFLOW_FORMALIZATION_REQUEST_KIND"] == "file"
    assert env["LEANFLOW_FORMALIZATION_REQUEST_RELATIVE"] == "docs/paper.tex"
    assert env["LEANFLOW_FORMALIZATION_SELECTED_SOURCE"] == "docs/paper.tex"


def test_prepare_formalization_document_context_preserves_existing_root_imports(
    tmp_path, evaluated_layout
):
    project = tmp_path / "Demo"
    project.mkdir()
    root_file = project / "Main.lean"
    original = "import Main.Existing\n\n/-! Existing root module. -/\n"
    root_file.write_text(original)
    source = project / "docs" / "paper.tex"
    _write_sample_tex(source)
    context = prepare_formalization_document_context(
        project_root=project, cwd=project, workflow_args="docs/paper.tex"
    )
    assert root_file.read_text() == original
    assert not context.target_lean_path.exists()
    assert list(project.rglob("*.lean")) == [root_file]


def test_inspect_formalization_document_extracts_latex_inventory(tmp_path):
    project = tmp_path / "Demo"
    source = project / "docs" / "paper.tex"
    _write_sample_tex(source)

    payload = inspect_formalization_document("docs/paper.tex", project_root=project, cwd=project)

    assert payload["success"] is True
    assert payload["source_relative"] == "docs/paper.tex"
    assert payload["title"] == "Tiny Source"
    assert payload["sections"][0]["title"] == "Main result"
    labels = {item["label"] for item in payload["theorem_blocks"]}
    assert {"def:good", "thm:zero_good"} <= labels
    theorem = next(item for item in payload["theorem_blocks"] if item["label"] == "thm:zero_good")
    assert theorem["uses"] == ["def:good"]
    assert "Zero is good" in theorem["statement"]


def test_inspect_formalization_document_extracts_custom_newtheorem_blocks(tmp_path):
    project = tmp_path / "Demo"
    source = project / "docs" / "custom.tex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        r"""
\documentclass{article}
\newtheorem{thm}{Theorem}
\newtheorem{prop}[thm]{Proposition}
\newtheorem*{conj}{Conjecture}
\begin{document}

\begin{thm}\label{main-thm}
Every good object is good.
\end{thm}
\begin{proof}
Unfold the definition and close the goal.
\end{proof}

\begin{prop}
Every better object is good.
\end{prop}

\begin{conj}
Every best object is good.
\end{conj}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    payload = inspect_formalization_document("docs/custom.tex", project_root=project, cwd=project)

    blocks = payload["theorem_blocks"]
    assert [block["kind"] for block in blocks] == ["theorem", "proposition", "conjecture"]
    assert blocks[0]["label"] == "main-thm"
    assert "Unfold the definition" in blocks[0]["proof"]
    assert blocks[1]["label"].startswith("line-")
    assert blocks[1]["proof"] == ""
    assert blocks[2]["label"].startswith("line-")


def test_inspect_formalization_document_extracts_common_theorem_declaration_families(tmp_path):
    project = tmp_path / "Demo"
    source = project / "docs" / "families.tex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        r"""
\documentclass{article}
\usepackage{thmtools}
\usepackage{mdframed}
\declaretheorem[name=Claim]{claimbox}
\newmdtheoremenv[backgroundcolor=gray!10]{boxeddef}{Definition}
\mdtheorem{boxedrem}{Remark}
\spnewtheorem{springprop}{Proposition}{\bfseries}{\itshape}
\newtcbtheorem[number within=section]{tcblemma}{Lemma}{colback=white}{lem}
\newenvironment{problem}{\par\noindent\textbf{Problem.}}{\par}
\begin{document}

\begin{claimbox}\label{claim:boxed}
The boxed claim is true.
\end{claimbox}
\proof
This is a plain proof macro body.
\endproof

\begin{boxeddef}
The boxed definition is useful.
\end{boxeddef}

\begin{boxedrem}
The boxed remark is useful.
\end{boxedrem}

\begin{springprop}
The Springer proposition is useful.
\end{springprop}

\begin{tcblemma}
The tcolorbox lemma is useful.
\end{tcblemma}

\begin{problem}
Find the useful object.
\end{problem}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    payload = inspect_formalization_document("docs/families.tex", project_root=project, cwd=project)

    blocks = payload["theorem_blocks"]
    assert [block["kind"] for block in blocks] == [
        "claim",
        "definition",
        "remark",
        "proposition",
        "lemma",
        "problem",
    ]
    assert blocks[0]["label"] == "claim:boxed"
    assert "plain proof macro body" in blocks[0]["proof"]
    assert [block["environment"] for block in blocks] == [
        "claimbox",
        "boxeddef",
        "boxedrem",
        "springprop",
        "tcblemma",
        "problem",
    ]


def test_inspect_formalization_document_extracts_plain_tex_profess_blocks(tmp_path):
    project = tmp_path / "Demo"
    source = project / "docs" / "plain.tex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        r"""
\centerline{\titlefont Plain TeX Result}

\profess{Theorem.}
Every good number is good.
\endprofess

\proof
This follows by unfolding the definition and applying the obvious witness.
\endproof
""".strip(),
        encoding="utf-8",
    )

    payload = inspect_formalization_document("docs/plain.tex", project_root=project, cwd=project)

    assert payload["title"] == "Plain TeX Result"
    assert len(payload["theorem_blocks"]) == 1
    block = payload["theorem_blocks"][0]
    assert block["label"].startswith("line-")
    assert block["kind"] == "theorem"
    assert "Every good number is good" in block["statement"]
    assert "unfolding the definition" in block["proof"]


def test_directory_formalization_selects_main_tex_and_records_project_inventory(
    tmp_path, evaluated_layout
):
    project = tmp_path / "Demo"
    source_dir = project / "docs" / "paper"
    source_dir.mkdir(parents=True)
    (source_dir / "macros.tex").write_text("\\def\\good{good}\n", encoding="utf-8")
    (source_dir / "main.bbl").write_text(
        "\\begin{thebibliography}{1}\\end{thebibliography}\n", encoding="utf-8"
    )
    (source_dir / "style.bst").write_text("ENTRY {}{}{}\n", encoding="utf-8")
    (source_dir / "main.tex").write_text(
        r"""
\documentclass{article}
\input{macros}
\begin{document}
\title{Directory Source}
\section{Main}
\begin{theorem}\label{thm:dir}Directory input works.\end{theorem}
\bibliography{refs}
\bibliographystyle{style}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    resolved, relative, kind = resolve_formalization_document(project, project, "docs/paper")
    assert resolved == (source_dir / "main.tex").resolve()
    assert relative == "docs/paper/main.tex"
    assert kind == "latex"

    context = prepare_formalization_document_context(
        project_root=project, cwd=project, workflow_args="docs/paper"
    )
    manifest = json.loads(context.manifest_path.read_text())
    assert manifest["source_reconciliation_requirements"]["diagnostics"]
    payload = inspect_formalization_document("docs/paper", project_root=project, cwd=project)

    assert payload["source_relative"] == "docs/paper/main.tex"
    assert payload["document_request_kind"] == "directory"
    assert payload["document_request_relative"] == "docs/paper"
    assert payload["tex_project_entrypoint"] == "docs/paper/main.tex"
    assert payload["tex_project_included_tex_files"] == ["docs/paper/macros.tex"]
    assert payload["tex_project_bibliography_files"] == ["docs/paper/main.bbl"]
    assert payload["tex_project_local_asset_files"] == ["docs/paper/style.bst"]
    assert payload["tex_project_pdf_files"] == []
    assert payload["tex_project_figure_files"] == []
    assert payload["tex_project_support_files"] == [
        "docs/paper/main.bbl",
        "docs/paper/style.bst",
    ]
    assert "docs/paper/macros.tex" in payload["inventory"]["files"]
    assert payload["inventory"]["extraction_status"] == "blocked"
    assert payload["inventory"]["files"]["docs/paper/macros.tex"]["text"] == "\\def\\good{good}\n"
    assert not list(project.rglob("*.lean"))


def test_directory_formalization_records_pdf_figure_and_support_files(tmp_path, evaluated_layout):
    project = tmp_path / "Demo"
    source_dir = project / "docs" / "paper"
    figures = source_dir / "figures"
    figures.mkdir(parents=True)
    (source_dir / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\\begin{theorem}T.\\end{theorem}\\end{document}\n",
        encoding="utf-8",
    )
    (source_dir / "supplement.pdf").write_bytes(b"%PDF-1.4\n")
    (figures / "diagram.png").write_bytes(b"png")
    (source_dir / "macros.sty").write_text("\\newcommand{\\good}{good}\n", encoding="utf-8")

    context = prepare_formalization_document_context(
        project_root=project, cwd=project, workflow_args="docs/paper"
    )
    manifest = json.loads(context.manifest_path.read_text())
    assert manifest["source_reconciliation_requirements"]["diagnostics"]
    payload = inspect_formalization_document("docs/paper", project_root=project, cwd=project)

    assert payload["tex_project_pdf_files"] == ["docs/paper/supplement.pdf"]
    assert payload["tex_project_figure_files"] == [
        "docs/paper/figures/diagram.png",
        "docs/paper/supplement.pdf",
    ]
    assert payload["tex_project_support_files"] == ["docs/paper/macros.sty"]
    assert payload["inventory"]["source_relative"] == "docs/paper/main.tex"
    assert payload["inventory"]["extraction_status"] == "blocked"
    assert not list(project.rglob("*.lean"))


def test_directory_formalization_rejects_ambiguous_tex_roots(tmp_path):
    project = tmp_path / "Demo"
    source_dir = project / "docs" / "paper"
    source_dir.mkdir(parents=True)
    for name in ("alpha.tex", "beta.tex"):
        (source_dir / name).write_text(
            "\\documentclass{article}\\begin{document}\\title{Same}\\end{document}\n",
            encoding="utf-8",
        )

    with pytest.raises(FormalizationDocumentError, match="multiple possible TeX entrypoints"):
        resolve_formalization_document(project, project, "docs/paper")


def test_doc_formalization_demo_fixture_is_parseable():
    repo_root = Path(__file__).resolve().parents[2]
    project = repo_root / "testdata" / "workflow_projects" / "DocFormalizationDemo"

    payload = inspect_formalization_document(
        "docs/QuantizingPythagoreanTriples/Pythagore2.tex",
        project_root=project,
        cwd=project,
    )

    assert payload["success"] is True
    assert payload["source_kind"] == "latex"
    assert payload["title"] == "Quantizing Pythagorean triples"
    section_titles = [section["title"] for section in payload["sections"]]
    assert "The $q$-deformed Pythagoras equation" in section_titles
    assert "Classical Pythagorean triples" in section_titles
    assert "A construction of $q$-Pythagorean triples" in section_titles
    assert any(
        item["kind"] == "definition"
        and item.get("environment") == "defn"
        and "\\cC_{\\frac{m}{n}}" in item["statement"]
        for item in payload["theorem_blocks"]
    )
    assert any(
        item["label"] == "CalcThm" and item["kind"] == "theorem" and item.get("proof")
        for item in payload["theorem_blocks"]
    )


def test_doc_formalization_demo_pythagorean_directory_is_parseable():
    repo_root = Path(__file__).resolve().parents[2]
    project = repo_root / "testdata" / "workflow_projects" / "DocFormalizationDemo"

    payload = inspect_formalization_document(
        "docs/PythagoreanPolynomialParametrization",
        project_root=project,
        cwd=project,
    )

    assert payload["success"] is True
    assert payload["source_relative"] == "docs/PythagoreanPolynomialParametrization/pyth.tex"
    assert payload["document_request_kind"] == "directory"
    assert payload["tex_project_bibliography_files"] == [
        "docs/PythagoreanPolynomialParametrization/pyth.bbl"
    ]
    assert payload["tex_project_local_asset_files"] == [
        "docs/PythagoreanPolynomialParametrization/siamese.bst"
    ]
    assert "Parametrization of Pythagorean triples" in payload["title"]
    assert any(item["kind"] == "theorem" for item in payload["theorem_blocks"])


def test_resolve_formalization_document_requires_project_local_supported_file(tmp_path):
    project = tmp_path / "Demo"
    source = project / "paper.tex"
    _write_sample_tex(source)
    outside = tmp_path / "outside.tex"
    _write_sample_tex(outside)

    resolved, relative, kind = resolve_formalization_document(project, project, "paper.tex")
    assert resolved == source.resolve()
    assert relative == "paper.tex"
    assert kind == "latex"

    with pytest.raises(FormalizationDocumentError, match="inside the LeanFlow project"):
        resolve_formalization_document(project, project, str(outside))
    with pytest.raises(FormalizationDocumentError, match="TeX project directory"):
        resolve_formalization_document(project, project, "")
    with pytest.raises(FormalizationDocumentError, match="Use `/prove`"):
        resolve_formalization_document(project, project, "Main.lean")


def test_document_display_uses_inventory_commands_and_keeps_complete_statements(tmp_path):
    """Preserve full authority and ignore commands hidden in source comments/definitions."""
    source = tmp_path / "paper.tex"
    statement = "A mathematical qualifier. " * 90
    source.write_text(
        "% \\title{Comment title}\n"
        "\\newcommand{\\hidden}{\\title{Definition title}}\n"
        "\\title{Actual {nested} title}\n"
        + "\n".join(
            f"\\section{{Section {index}}}\\begin{{theorem}}\\label{{claim:{index}}}{statement}\\end{{theorem}}"
            for index in range(83)
        )
    )
    payload = inspect_formalization_document(source, project_root=tmp_path)
    assert payload["title"] == "Actual {nested} title"
    assert len(payload["theorem_blocks"]) == len(payload["sections"]) == 83
    assert payload["theorem_blocks"][-1]["label"] == "claim:82"
    assert statement in payload["theorem_blocks"][-1]["statement"]
    assert len(payload["extracted_text"]) > 120_000
    assert len(payload["text_excerpt"]) <= 24_000 and payload["text_excerpt_truncated"]
    assert payload["inventory"]["audit_complete"] is False


def test_pdf_document_tool_projects_one_inventory_without_second_extraction(tmp_path, monkeypatch):
    """Keep page evidence and displayed text from the same frozen extraction result."""
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-source")
    calls = []

    def inventory(root, path):
        calls.append((root, path))
        return {
            "source_kind": "pdf",
            "files": {"paper.pdf": {"size": 11, "encoding": "binary"}},
            "pages": [{"index": 0, "text": "Original extracted page"}],
            "items": [],
            "diagnostics": [],
            "extraction_status": "audit_required",
        }

    def no_second_extraction(*args, **kwargs):
        raise AssertionError("Document display must not run another external extractor")

    monkeypatch.setattr("leanflow_cli.formalization.source_inventory.inventory_source", inventory)
    monkeypatch.setattr("subprocess.run", no_second_extraction)
    payload = inspect_formalization_document(source, project_root=tmp_path)
    assert calls == [(tmp_path, source)]
    assert payload["pages"] == 1
    assert payload["extracted_text"] == "Original extracted page"
