"""Fail known dependency blockers before inference while keeping source inspection available."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import environment, intake, lean_build
from leanflow_cli.formalization.formalization_documents import (
    FormalizationDocumentError,
    prepare_formalization_document_context,
)
from leanflow_cli.formalization.service import inspect_document, show_document
from leanflow_cli.native import harness_workflow
from tests.leanflow.test_source_inventory import _pdf


@pytest.fixture
def build_admission_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    """Use the real builder admission with recorded fixture bytes and no compiler process."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "Main.lean").write_text("import Library\n")
    (root / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    compiler = tmp_path / "compiler"
    (compiler / "bin").mkdir(parents=True)
    (compiler / "bin/lean").write_bytes(b"compiler fixture")
    (compiler / "lib/lean").mkdir(parents=True)
    (compiler / "lib/lean/Init.olean").write_bytes(b"compiler artifact fixture")
    monkeypatch.setattr(environment, "_compiler_root", lambda _: compiler)
    monkeypatch.setattr(lean_build, "compiler_executable", lambda *a: str(compiler / "bin/lean"))
    monkeypatch.setattr(
        lean_build, "isolated_command", lambda **kw: pytest.fail("Preflight cannot compile")
    )
    layout = {
        "accepted": True,
        "toolchain": "4.34.1",
        "library": "Main",
        "libraries": [{"src_dir": ".", "roots": ["Main"]}],
        "owned_prefix": "Main/Document",
        "aggregator": "Main/Document.lean",
        "build_dir": ".lake/build",
        "lean_lib_dir": ".lake/build/lib/lean",
    }
    monkeypatch.setattr(intake, "resolve_layout", lambda *args: layout)
    return root, layout


def _dependency(root: Path, name: str, *, kind: str = "pinned", module: str = "Library") -> None:
    """Create unauthenticated inventory fixtures without implying Lean acceptance."""
    directory = f"vendor/{name}" if kind == "path" else f".lake/packages/{name}"
    package = root / directory
    package.mkdir(parents=True)
    (package / "lakefile.toml").write_text(f'name = "{name}"\n[[lean_lib]]\nname = "{module}"\n')
    (package / f"{module}.lean").write_text("def value : Nat := 0\n")
    if kind == "pinned":
        library = package / ".lake/build/lib/lean"
        library.mkdir(parents=True)
        (library / f"{module}.olean").write_bytes(b"untrusted inventory-only artifact")
    entry = (
        {"name": name, "type": "path", "dir": directory}
        if kind == "path"
        else {"name": name, "type": "git", "rev": "a" * 40 if kind == "pinned" else "v1.0"}
    )
    manifest = root / "lake-manifest.json"
    value = json.loads(manifest.read_text()) if manifest.exists() else {"packages": []}
    value["packages"].append(entry)
    manifest.write_text(json.dumps(value))


@pytest.mark.parametrize("entrypoint", ["intake", "native", "resume"])
@pytest.mark.parametrize(
    "case, message",
    [
        ("tag", "unsupported_declarative_dependency_build"),
        ("path", "unsupported_declarative_dependency_build"),
        ("missing", "Missing compiled mathematical library artifacts"),
        ("duplicate", "Ambiguous module root"),
        ("compiler", "Ambiguous module root"),
        ("local", "Local module shadows"),
    ],
)
def test_all_launch_paths_apply_builder_admission_before_paid_work(
    build_admission_project: tuple[Path, dict],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    case: str,
    message: str,
) -> None:
    root, _ = build_admission_project
    _dependency(
        root,
        "example",
        kind=case if case in {"tag", "path"} else "pinned",
        module={"compiler": "Init", "local": "Main"}.get(case, "Library"),
    )
    if case == "missing":
        (root / ".lake/packages/example/.lake/build/lib/lean/Library.olean").unlink()
    elif case == "duplicate":
        _dependency(root, "other")
    record = inspect_document(root, root / "paper.tex")
    original = (root / "Main.lean").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Known build incompatibility must stop before snapshot, author or publication")

    monkeypatch.setattr(harness_workflow, "run_harness", forbidden)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", forbidden)
    monkeypatch.setattr("leanflow_cli.formalization.publish.publish_checked", forbidden)
    with pytest.raises(FormalizationDocumentError, match=message):
        if entrypoint == "intake":
            prepare_formalization_document_context(
                project_root=root, cwd=root, workflow_args="paper.tex"
            )
        else:
            harness_workflow.run_workflow(
                {
                    "LEANFLOW_PROJECT_ROOT": str(root),
                    "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                    "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                    "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
                    **(
                        {"LEANFLOW_NATIVE_RESUME_RUN_ID": "saved"} if entrypoint == "resume" else {}
                    ),
                }
            )
    assert (root / "Main.lean").read_bytes() == original
    assert not (root / "formalization").exists()
    assert not list((root / ".leanflow/native-workflows").glob("*/limits.json"))


def test_admitted_dependencies_allow_companion_backed_draft_without_acceptance(
    build_admission_project: tuple[Path, dict],
) -> None:
    root, layout = build_admission_project
    _dependency(root, "example")
    source = root / "paper.tex"
    source.write_text(source.read_text() + r"\includegraphics{missing.png}")
    companion = root / "paper.pdf"
    _pdf(companion, ["Theorem 1. True. Figure."])
    inspected = inspect_document(root, source, companion_pdf=companion)
    context = prepare_formalization_document_context(
        project_root=root, cwd=root, workflow_args="paper.tex"
    )
    assert context.metadata["layout"] == layout
    assert "DRAFT PREPARATION ONLY" in context.context_path.read_text()
    assert context.metadata["inventory"]["extraction_status"] == "blocked"
    assert not inspected["can_prove"] and not context.target_lean_path.exists()


@pytest.fixture
def unsupported_project(tmp_path: Path) -> Path:
    package = tmp_path / ".lake/packages/repl"
    package.mkdir(parents=True)
    (package / "lakefile.toml").write_text('name = "repl"\n')
    (package / "Test.lean").write_text('def run : IO Unit := IO.println "test"\n')
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "repl", "type": "git", "rev": "pinned"}]})
    )
    (tmp_path / "Main.lean").write_text("theorem existing : True := by trivial\n")
    return tmp_path


@pytest.mark.parametrize("extension", ["tex", "pdf"])
def test_intake_blocks_unsupported_dependencies_without_blocking_inspection(
    unsupported_project: Path, monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    root = unsupported_project
    source = root / f"paper.{extension}"
    if extension == "pdf":
        _pdf(source, ["Theorem 1. Every natural number equals itself."])
    else:
        source.write_text(r"\begin{theorem}Every natural number equals itself.\end{theorem}")
    inspected = inspect_document(root, source)
    assert inspected["items"] and not inspected["source_stale"]
    original = (root / "Main.lean").read_bytes()
    monkeypatch.setattr(
        intake, "resolve_layout", lambda *args: pytest.fail("Must stop before compiler setup")
    )
    with pytest.raises(FormalizationDocumentError, match="unsupported_dependency_inputs: repl"):
        prepare_formalization_document_context(
            project_root=root, cwd=root, workflow_args=source.name
        )
    assert show_document(root, inspected["document_id"])["items"]
    assert (root / "Main.lean").read_bytes() == original
    assert not (root / "formalization").exists()
    assert not list((root / ".leanflow/formalizations").glob("*/intake/*"))


@pytest.mark.parametrize("resume", [False, True])
def test_direct_native_launch_blocks_before_paid_author_or_publication(
    unsupported_project: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    root = unsupported_project
    source = root / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = inspect_document(root, source)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported dependencies must block before author or publication")

    monkeypatch.setattr(harness_workflow, "run_harness", forbidden)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", forbidden)
    monkeypatch.setattr("leanflow_cli.formalization.publish.publish_checked", forbidden)
    with pytest.raises(FormalizationDocumentError, match="unsupported_dependency_inputs: repl"):
        harness_workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(root),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
                **({"LEANFLOW_NATIVE_RESUME_RUN_ID": "saved-run"} if resume else {}),
            }
        )
    assert not list((root / ".leanflow/native-workflows").glob("*/limits.json"))
    assert not (root / "formalization").exists()
    assert (root / "Main.lean").read_text() == "theorem existing : True := by trivial\n"


def test_source_diagnostics_precede_dependency_preflight(
    unsupported_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = unsupported_project
    source = root / "paper.tex"
    source.write_text(r"\begin{theorem}\UnknownClaim{x} holds.\end{theorem}")
    record = inspect_document(root, source)
    monkeypatch.setattr(
        intake,
        "dependency_inputs",
        lambda *args: pytest.fail("Source repair must be reported first"),
    )
    with pytest.raises(FormalizationDocumentError, match="Source extraction needs repair"):
        prepare_formalization_document_context(
            project_root=root, cwd=root, workflow_args=source.name
        )
    with pytest.raises(ValueError, match="Repair source inventory"):
        harness_workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(root),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            }
        )
