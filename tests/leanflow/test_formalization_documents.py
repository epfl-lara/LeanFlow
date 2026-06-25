from __future__ import annotations

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


def test_prepare_formalization_document_context_creates_planner_artifacts(tmp_path):
    project = tmp_path / "Demo"
    (project / "Demo").mkdir(parents=True)
    source = project / "docs" / "paper.tex"
    _write_sample_tex(source)

    context = prepare_formalization_document_context(
        project_root=project,
        cwd=project,
        workflow_args="docs/paper.tex",
        project_label="Demo",
    )

    assert context.source_relative == "docs/paper.tex"
    assert context.source_kind == "latex"
    assert context.target_lean_relative == "Demo/Paper/Main.lean"
    assert context.blueprint_path == project / "Demo" / "Paper" / "Blueprint.md"
    assert context.blueprint_skill_path == (
        project / ".leanflow" / "skills" / "formalization-blueprint-Demo-Paper-Main" / "SKILL.md"
    )
    assert context.context_path.is_file()
    assert context.manifest_path.is_file()
    assert context.extracted_text_path.is_file()
    assert context.blueprint_path.is_file()
    assert context.blueprint_skill_path.is_file()
    assert context.target_lean_path.is_file()

    target_text = context.target_lean_path.read_text(encoding="utf-8")
    assert target_text == "import Mathlib\n"
    assert (project / "Demo" / "Paper.lean").read_text(
        encoding="utf-8"
    ) == "import Demo.Paper.Main\n"
    assert (project / "Demo.lean").read_text(encoding="utf-8") == "import Demo.Paper\n"

    startup_context = context.context_path.read_text(encoding="utf-8")
    assert "document formalization run" in startup_context
    assert "`thm:zero_good`" in startup_context
    assert (
        "keep source pointers, ambiguity notes, dependencies, complete source proof text, and proof notes"
        in startup_context
    )
    assert "reread it easily" in startup_context
    assert "must begin with all `import` commands" in startup_context
    assert "document formalization handoff verifier" in startup_context
    assert "root project module imports the generated target module path" in startup_context
    assert "`## Suggested Search Modules`" in startup_context
    assert "construction gaps block proof handoff" in startup_context
    assert "one final organization pass before the formalizer exits" in startup_context
    assert "run project-level Lean verification" in startup_context
    assert "supplemental blueprint skill" in startup_context

    blueprint = context.blueprint_path.read_text(encoding="utf-8")
    skill = context.blueprint_skill_path.read_text(encoding="utf-8")
    assert "Blueprint: `Demo/Paper/Blueprint.md`" in skill
    assert "Source document: `docs/paper.tex`" in skill
    assert "thm:zero_good" in blueprint
    assert "Target Lean entry file" in blueprint
    assert "## Import Plan" in blueprint
    assert "## Suggested Search Modules" in blueprint
    assert "## Generated File Layout" in blueprint
    assert "Replace all `_pending_` entries before drafting Lean" in blueprint
    assert "Formal statement review: _pending_" in blueprint
    assert "Complete source proof: _pending_" in blueprint
    assert "Source proof / prover notes: _pending_" in blueprint

    env = context.to_env()
    assert env["LEANFLOW_WORKFLOW_CONTEXT"] == str(context.context_path)
    assert env["LEANFLOW_FORMALIZATION_TARGET_FILE"] == "Demo/Paper/Main.lean"
    assert env["LEANFLOW_FORMALIZATION_REQUEST_KIND"] == "file"
    assert env["LEANFLOW_FORMALIZATION_REQUEST_RELATIVE"] == "docs/paper.tex"
    assert env["LEANFLOW_FORMALIZATION_SELECTED_SOURCE"] == "docs/paper.tex"


def test_prepare_formalization_document_context_extends_existing_root_imports(tmp_path):
    project = tmp_path / "Demo"
    (project / "Demo").mkdir(parents=True)
    (project / "Demo.lean").write_text(
        "import Demo.Existing\n\n/-! Existing root module. -/\n", encoding="utf-8"
    )
    source = project / "docs" / "paper.tex"
    _write_sample_tex(source)

    prepare_formalization_document_context(
        project_root=project,
        cwd=project,
        workflow_args="docs/paper.tex",
        project_label="Demo",
    )

    assert (project / "Demo" / "Paper.lean").read_text(
        encoding="utf-8"
    ) == "import Demo.Paper.Main\n"
    root_text = (project / "Demo.lean").read_text(encoding="utf-8")
    assert root_text.startswith(
        "import Demo.Existing\nimport Demo.Paper\n\n/-! Existing root module. -/"
    )


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


def test_directory_formalization_selects_main_tex_and_records_project_inventory(tmp_path):
    project = tmp_path / "Demo"
    source_dir = project / "docs" / "paper"
    source_dir.mkdir(parents=True)
    (source_dir / "macros.tex").write_text("\\def\\good{good}\n", encoding="utf-8")
    (source_dir / "refs.bbl").write_text(
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
        project_root=project,
        cwd=project,
        workflow_args="docs/paper",
        project_label="Demo",
    )

    assert context.source_relative == "docs/paper/main.tex"
    assert context.metadata["document_request_kind"] == "directory"
    assert context.metadata["document_request_relative"] == "docs/paper"
    assert context.metadata["tex_project_entrypoint"] == "docs/paper/main.tex"
    assert context.metadata["tex_project_included_tex_files"] == ["docs/paper/macros.tex"]
    assert context.metadata["tex_project_bibliography_files"] == ["docs/paper/refs.bbl"]
    assert context.metadata["tex_project_local_asset_files"] == ["docs/paper/style.bst"]
    assert context.metadata["tex_project_pdf_files"] == []
    assert context.metadata["tex_project_figure_files"] == []
    assert context.metadata["tex_project_support_files"] == [
        "docs/paper/refs.bbl",
        "docs/paper/style.bst",
    ]
    env = context.to_env()
    assert env["LEANFLOW_FORMALIZATION_REQUEST_KIND"] == "directory"
    assert env["LEANFLOW_FORMALIZATION_REQUEST_RELATIVE"] == "docs/paper"
    assert env["LEANFLOW_FORMALIZATION_SELECTED_SOURCE"] == "docs/paper/main.tex"
    manifest = context.manifest_path.read_text(encoding="utf-8")
    assert "selected_source_document_relative" in manifest
    startup_context = context.context_path.read_text(encoding="utf-8")
    assert "## TeX Project Discovery" in startup_context
    assert "`docs/paper/macros.tex`" in startup_context


def test_directory_formalization_records_pdf_figure_and_support_files(tmp_path):
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
        project_root=project,
        cwd=project,
        workflow_args="docs/paper",
        project_label="Demo",
    )

    assert context.metadata["tex_project_pdf_files"] == ["docs/paper/supplement.pdf"]
    assert context.metadata["tex_project_figure_files"] == [
        "docs/paper/figures/diagram.png",
        "docs/paper/supplement.pdf",
    ]
    assert context.metadata["tex_project_support_files"] == ["docs/paper/macros.sty"]
    startup_context = context.context_path.read_text(encoding="utf-8")
    assert "Nearby PDF files:" in startup_context
    assert "`docs/paper/supplement.pdf`" in startup_context
    assert "Figure/image files:" in startup_context
    assert "`docs/paper/figures/diagram.png`" in startup_context
    assert "TeX support files:" in startup_context


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
