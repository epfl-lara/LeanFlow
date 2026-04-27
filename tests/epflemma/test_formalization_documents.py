from __future__ import annotations

from pathlib import Path

import pytest

from epflemma_cli.formalization_documents import (
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
    assert context.context_path.is_file()
    assert context.manifest_path.is_file()
    assert context.extracted_text_path.is_file()
    assert context.blueprint_path.is_file()
    assert context.target_lean_path.is_file()

    target_text = context.target_lean_path.read_text(encoding="utf-8")
    assert target_text == "import Demo\n"

    startup_context = context.context_path.read_text(encoding="utf-8")
    assert "document formalization run" in startup_context
    assert "`thm:zero_good`" in startup_context
    assert "keep source pointers, ambiguity notes, dependencies, and proof notes in the planner blueprint" in startup_context
    assert "reread it easily" in startup_context
    assert "must begin with all `import` commands" in startup_context
    assert "document formalization handoff verifier" in startup_context
    assert "root project module imports the generated target module" in startup_context

    blueprint = context.blueprint_path.read_text(encoding="utf-8")
    assert "thm:zero_good" in blueprint
    assert "Target Lean entry file" in blueprint
    assert "Replace all `_pending_` entries before drafting Lean" in blueprint
    assert "Formal statement review: _pending_" in blueprint
    assert "Source proof / prover notes: _pending_" in blueprint

    env = context.to_env()
    assert env["EPFLEMMA_WORKFLOW_CONTEXT"] == str(context.context_path)
    assert env["EPFLEMMA_FORMALIZATION_TARGET_FILE"] == "Demo/Paper/Main.lean"


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


def test_doc_formalization_demo_fixture_is_parseable():
    repo_root = Path(__file__).resolve().parents[2]
    project = repo_root / "testdata" / "workflow_projects" / "DocFormalizationDemo"

    payload = inspect_formalization_document(
        "docs/RecountingTheRationals.tex",
        project_root=project,
        cwd=project,
    )

    assert payload["success"] is True
    assert payload["source_kind"] == "latex"
    assert payload["title"] == "Hyperbinary Representations and the Calkin-Wilf Enumeration"
    labels = [item["label"] for item in payload["theorem_blocks"]]
    assert labels == [
        "def:hyperbinary_representation",
        "def:hyperbinary_count",
        "def:calkin_wilf_fraction",
        "lem:hyperbinary_zero",
        "lem:hyperbinary_odd",
        "lem:hyperbinary_even",
        "lem:consecutive_coprime",
        "def:calkin_wilf_children",
        "lem:left_child_recurrence",
        "lem:right_child_recurrence",
        "lem:parent_step_decreases",
        "thm:calkin_wilf_enumeration",
        "cor:explicit_positive_rational_listing",
    ]


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

    with pytest.raises(FormalizationDocumentError, match="inside the EPFLemma project"):
        resolve_formalization_document(project, project, str(outside))
    with pytest.raises(FormalizationDocumentError, match=".tex or .pdf"):
        resolve_formalization_document(project, project, "")
    with pytest.raises(FormalizationDocumentError, match="Use `/prove`"):
        resolve_formalization_document(project, project, "Main.lean")
