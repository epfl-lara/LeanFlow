"""Fail known dependency blockers before inference while keeping source inspection available."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import environment, intake, lean_build
from leanflow_cli.formalization.formalization_documents import (
    FormalizationDocumentError,
    prepare_formalization_document_context,
)
from leanflow_cli.formalization.preparation_campaign import (
    admit_author,
    campaign_budget,
    campaign_lock,
    select_campaign,
)
from leanflow_cli.formalization.records import load_document, source_artifact_id
from leanflow_cli.formalization.service import act_document, inspect_document, show_document
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
        "module_prefix": "Main.Document",
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


def _native_preflight(
    root: Path, record: dict, monkeypatch: pytest.MonkeyPatch, *, resume: bool
) -> tuple[dict, dict, dict]:
    """Exercise monitored dependency admission with genuine resumable authority and accounting."""
    run_id = "saved-preflight" if resume else "fresh-preflight"
    directory = root / ".leanflow/native-workflows" / run_id
    before = {}
    if resume:
        selected = load_document(root, record["document_id"])
        token = harness_workflow.current_selection_token(
            root, record["document_id"], initialize=True
        )
        with campaign_lock(root, run_id):
            binding = select_campaign(
                root,
                selected,
                run_id=run_id,
                limits={},
                backend="codex",
                selection_token=token,
                source_draft_only=False,
                effective_draft=False,
                resume=False,
                budget_path=directory / "limits.json",
            )
            admit_author(root, binding, resume=False, state_path=directory / "state.json")
            limits = CampaignLimits(campaign_budget(root, binding), binding["limits"])
        state = {
            "version": 2,
            "run_id": run_id,
            "backend": "codex",
            "workflow": "formalize",
            "status": "blocked",
            "campaign_binding_id": binding["binding_id"],
            "baseline": {
                path: data.hex() for path, data in harness_workflow.source_files(root).items()
            },
            "formalization_selection_token": token,
        }
        (directory / "state.json").write_text(json.dumps(state))
        invocation = limits.reserve()
        limits.finish(invocation, {"input_tokens": 12, "output_tokens": 7}, started=True)
        before = json.loads((directory / "limits.json").read_text())["invocations"]

    def forbidden(*args, **kwargs):
        pytest.fail("Known dependency blockers must stop before dispatch or call reservation")

    monkeypatch.setattr(CampaignLimits, "reserve", forbidden)
    result = harness_workflow.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(root),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_WORKFLOW_RUN_ID": run_id,
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            **({"LEANFLOW_NATIVE_RESUME_RUN_ID": run_id} if resume else {}),
        }
    )
    ledger = json.loads((directory / "limits.json").read_text())
    persisted = json.loads((directory / "state.json").read_text())
    assert result == persisted and result["terminal"]
    assert ledger["invocations"] == before
    assert not (directory / "project").exists()
    assert not (directory / "accepted.json").exists()
    assert not (root / ".leanflow/formalization-publications.json").exists()
    return result, ledger, before


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
    if entrypoint == "intake":
        with pytest.raises(FormalizationDocumentError, match=message):
            prepare_formalization_document_context(
                project_root=root, cwd=root, workflow_args="paper.tex"
            )
        assert not list((root / ".leanflow/native-workflows").glob("*/limits.json"))
    else:
        state, ledger, charged = _native_preflight(
            root, record, monkeypatch, resume=entrypoint == "resume"
        )
        assert state["status"] == "error" and message in state["error"]
        assert len(ledger["invocations"]) == len(charged) == (entrypoint == "resume")
    assert (root / "Main.lean").read_bytes() == original
    assert not (root / "formalization").exists()


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


@pytest.mark.parametrize("preparation_changed", [False, True])
def test_prepared_intake_rebinds_previous_checked_environment_before_paid_work(
    build_admission_project, monkeypatch, preparation_changed
) -> None:
    """Use the current checked preparation rather than an inherited previous checker identity."""
    from leanflow_cli.formalization import preparation, slice_gate
    from leanflow_cli.formalization.formalization_documents import _select_formalization_document
    from leanflow_cli.formalization.records import (
        create_document,
        document_lock,
        new_revision,
        save_revision,
    )
    from tests.leanflow.preparation_helpers import inspector_readiness_record

    root, layout = build_admission_project
    layout["src_dir"] = "."
    _dependency(root, "example")
    record = create_document(root, root / "paper.tex")
    source_id = record["inventory"]["items"][0]["id"]
    with document_lock(root, record["document_id"]) as directory:
        previous = new_revision(record)
        previous.update(
            workflow_protocol=2,
            preparation_id="old-preparation" if preparation_changed else "current-preparation",
            preparation_plan_id="current-plan",
            slice_source_ids=[source_id],
            environment_id="old-checked-environment",
            checks={"accepted": True},
            reviews=[{"accepted": True}],
            handoff={"accepted": True},
        )
        previous = save_revision(directory, previous)
    receipt = {
        "preparation_id": "current-preparation",
        "preparation_plan_id": "current-plan",
        "source_bundle": record["source_bundle"],
        "environment_id": "new-checked-environment",
        "inspector_readiness": inspector_readiness_record(),
        "project_sources": preparation._source_identity(root),
        "plan": {"plan_id": "current-plan", "slice_source_ids": [source_id]},
    }
    monkeypatch.setattr(preparation, "load_preparation_receipt", lambda _: receipt)
    monkeypatch.setattr(preparation, "environment_identity", lambda _: receipt["environment_id"])
    monkeypatch.setattr(
        harness_workflow, "run_harness", lambda *a, **kw: pytest.fail("Intake cannot call a model")
    )
    monkeypatch.setattr(
        CampaignLimits, "reserve", lambda *a, **kw: pytest.fail("Intake cannot reserve a paid call")
    )
    source_before = (root / "paper.tex").read_bytes()
    context = intake.prepare_intake(
        root, _select_formalization_document(root, root, "paper.tex"), require_preparation=True
    )
    selected = load_document(root, record["document_id"])
    slice_gate.require_stage(root, selected, "representative_slice")
    assert selected["revision_id"] == context.metadata["revision_id"]
    assert selected["revision_id"] != previous["revision_id"]
    assert selected["environment_id"] == receipt["environment_id"]
    assert selected["preparation_id"] == receipt["preparation_id"]
    assert selected["checks"] == {} and selected["reviews"] == [] and not selected["handoff"]
    assert (
        load_document(root, record["document_id"], previous["revision_id"])["environment_id"]
        == "old-checked-environment"
    )
    assert (root / "paper.tex").read_bytes() == source_before
    assert not (root / ".leanflow/native-workflows").exists()


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
    if extension == "pdf":
        with pytest.raises(FormalizationDocumentError, match="Source extraction needs repair"):
            prepare_formalization_document_context(
                project_root=root, cwd=root, workflow_args=source.name
            )
        selected = load_document(root, inspected["document_id"])
        text = selected["inventory"]["pages"][0]["text"]
        act_document(
            root,
            selected["document_id"],
            selected["revision_id"],
            {
                "type": "replace_page_transcription",
                "page_index": 0,
                "text": text,
                "verified": True,
                "reason": "Verified every clause on this fixture's physical page",
            },
        )
        selected = load_document(root, inspected["document_id"])
        for item in selected["inventory"]["items"]:
            act_document(
                root,
                selected["document_id"],
                selected["revision_id"],
                {
                    "type": "update_boundary",
                    "item_id": item["id"],
                    "source_refs": [
                        {
                            "artifact_id": source_artifact_id(selected, source.name),
                            "page_index": 0,
                            "text": text.strip(),
                        }
                    ],
                },
            )
            selected = load_document(root, inspected["document_id"])
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
    state, ledger, charged = _native_preflight(root, record, monkeypatch, resume=resume)
    assert state["status"] == "error" and "unsupported_dependency_inputs: repl" in state["error"]
    assert len(ledger["invocations"]) == len(charged) == resume
    assert not (root / "formalization").exists()
    assert (root / "Main.lean").read_text() == "theorem existing : True := by trivial\n"


def test_source_diagnostics_precede_dependency_preflight(
    unsupported_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = unsupported_project
    source = root / "paper.tex"
    source.write_text(r"\write18{forbidden}\begin{theorem}True.\end{theorem}")
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
