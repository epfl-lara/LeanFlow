"""Require exact reviewed proof scope while preserving unrelated source holes."""

from __future__ import annotations

import copy
import json
import math
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import freshness, handoff, publication
from leanflow_cli.formalization.evidence import digest, durable_json, identity
from leanflow_cli.formalization.publication_journal import load_registry, save_registry
from leanflow_cli.formalization.records import (
    document_directory,
    load_document,
    save_receipt,
    save_revision,
)
from leanflow_cli.workflows.prover import entrypoint, runtime
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.source import SourceDocument, discover


@pytest.fixture
def admitted_snapshot_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep certificate tests independent of Lake while exercising the real frozen copy."""
    monkeypatch.setattr(
        "leanflow_cli.formalization.dependency_snapshot.inspect_layout",
        lambda root: {
            "accepted": True,
            "libraries": [{"src_dir": "src", "roots": ["Main"]}],
            "build_dir": ".lake/build",
            "lean_lib_dir": ".lake/build/lib/lean",
        },
    )


def _registry() -> dict[str, Any]:
    row = {
        "name": "Paper.target",
        "file": "src/Main.lean",
        "module": "Main",
        "kind": "theorem",
        "type_hash": "type-target",
        "body_hash": "body-target",
        "depends_on": [],
        "pending": True,
    }
    return {
        "accepted": True,
        "policy_id": handoff.POLICY_VERSION,
        "checker_sha256": digest(handoff.INSPECTOR),
        "allowed_axioms": sorted(ProverConfig().allowed_axioms),
        "modules": {"Main": "src/Main.lean"},
        "declarations": [
            row,
            {
                "name": "Nat.add_comm",
                "module": "Init.Data.Nat.Lemmas",
                "kind": "theorem",
                "type_hash": "library-type",
                "body_hash": "library-body",
            },
        ],
        "obligations": [row],
    }


def test_readonly_reuse_cannot_expand_proof_write_scope(tmp_path: Path) -> None:
    registry = _registry()
    registry["obligations"][0]["declaration_origin"] = "existing_project"
    with pytest.raises(ValueError, match="cannot authorize proof writes"):
        handoff.build_handoff(
            tmp_path,
            document_id="paper",
            revision_id="revision",
            registry=registry,
            files={},
            environment_id="environment",
        )


def test_handoff_reuses_only_exact_checked_environment_and_default_stays_strict(
    reviewed, monkeypatch
) -> None:
    root, result = reviewed
    path = document_directory(root, "paper", published=True) / "handoff.json"
    monkeypatch.setattr(
        handoff,
        "environment_identity",
        lambda root: pytest.fail("Already checked environment must not be hashed twice"),
    )
    assert (
        handoff.validate_handoff(
            root, path, result["revision_id"], checked_environment_id="environment"
        )
        == result
    )
    with pytest.raises(ValueError, match="environment changed"):
        handoff.validate_handoff(
            root, path, result["revision_id"], checked_environment_id="different"
        )
    monkeypatch.setattr(handoff, "environment_identity", lambda root: "drift")
    with pytest.raises(ValueError, match="environment changed"):
        handoff.validate_handoff(root, path, result["revision_id"])


@pytest.fixture
def reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    # Keep Lean checking synthetic while using actual committed publication
    # authority for the portable receipt consumed by handoff gates.
    save_registry(tmp_path, load_registry(tmp_path))
    (tmp_path / "src").mkdir()
    source = "def unrelated : Nat := sorry\nnamespace Paper\ntheorem target : True := by sorry\nend Paper\n"
    (tmp_path / "src/Main.lean").write_text(source)
    (tmp_path / "Unrelated.lean").write_text("def other := 42\n")
    files = {
        "src/Main.lean": digest(source),
        "Unrelated.lean": digest((tmp_path / "Unrelated.lean").read_bytes()),
    }
    directory = document_directory(tmp_path, "paper", published=True)
    record = save_revision(
        directory,
        {
            "schema_version": 1,
            "document_id": "paper",
            "checks": _registry(),
            "project_source_hashes": files,
            "environment_id": "environment",
            "source_files": {},
            "allowed_axioms": list(ProverConfig().allowed_axioms),
            "lean_files": {"src/Main.lean": digest(source)},
            "inventory": {
                "items": [
                    {
                        "id": "source-1",
                        "kind": "theorem",
                        "scope": "included",
                        "statement_spans": [],
                    }
                ],
                "regions": [],
                "diagnostics": [],
                "source_kind": "latex",
                "source_relative": "paper.tex",
            },
            "units": [
                {
                    "id": "target",
                    "required_source_ids": ["source-1"],
                    "representation_note": "The source theorem",
                    "declarations": [
                        {"name": "Paper.target", "file": "src/Main.lean"},
                        {"name": "Nat.add_comm"},
                    ],
                }
            ],
        },
    )
    result = handoff.build_handoff(
        tmp_path,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=_registry(),
        files=files,
        environment_id="environment",
        guidance="Use the complete frozen paper proof.",
        source_ids={"Paper.target": ["source-1"]},
    )
    durable_json(directory / "handoff.json", result)
    save_receipt(
        directory,
        {
            "status": "published",
            "revision_id": record["revision_id"],
            "handoff": result,
            "reviews": [
                {
                    "verdicts": [
                        {"unit_id": "target", "source_ids": ["source-1"], "decision": "PASS"}
                    ]
                }
            ],
        },
    )
    _publish_fixture_controls(tmp_path, "paper", record["revision_id"])
    monkeypatch.setattr(handoff, "environment_identity", lambda root: "environment")
    monkeypatch.setattr(freshness, "environment_identity", lambda root: "environment")
    monkeypatch.setattr(
        publication, "assert_no_pending_publication", lambda root: None, raising=False
    )
    return tmp_path, result


def _publish_fixture_controls(
    root: Path, document_id: str, revision_id: str, *, staged: Path | None = None
) -> None:
    """Commit a synthetic fresh acceptance through the real publication kernel."""
    directory = document_directory(root, document_id, published=True)
    staged = staged or directory
    outputs = {
        (directory / path.relative_to(staged)).relative_to(root).as_posix(): path.read_bytes()
        for path in staged.rglob("*")
        if path.is_file() and path.name not in {"document.lock", "receipt-history.lock"}
    }
    if staged == directory:
        shutil.rmtree(directory)
    before = {
        name: (root / name).read_bytes() if (root / name).is_file() else None for name in outputs
    }
    publication.publish_revision(
        root=root,
        directory=root / ".leanflow/fixture-publication" / document_id / revision_id,
        revision_id=revision_id,
        plan=publication.freeze_publication_plan(revision_id, before, outputs, owned_paths=outputs),
        before=before,
        outputs=outputs,
        validate=lambda: None,
    )


def test_builder_maps_compiled_names_to_exact_source_slots(reviewed) -> None:
    root, result = reviewed
    item = result["obligations"][0]
    assert item["declaration"] == "Paper.target" and item["source_name"] == "target"
    assert item["holes"] == [1] and item["source_ids"] == ["source-1"]
    assert result["protected_declarations"]["Nat.add_comm"]["file"] == ""
    dag, documents = discover(
        root,
        [root / "src/Main.lean"],
        fill_definitions=False,
        authorized_holes=handoff.authorized_holes(result),
    )
    assert [(node.name, node.module, node.holes) for node in dag.nodes] == [
        ("Paper.target", "Main", [1])
    ]
    document = documents["src/Main.lean"]
    document.replacements["1"] = "trivial"
    assert "unrelated : Nat := sorry" in document.render()
    assert "target : True := by trivial" in document.render()


@pytest.mark.parametrize("mutation", ["name", "span", "index", "file"])
def test_mismatched_handoff_scope_rejected(reviewed, mutation: str) -> None:
    root, result = reviewed
    allowed = copy.deepcopy(handoff.authorized_holes(result))
    item = allowed["src/Main.lean"][0]
    if mutation == "name":
        item["source_name"] = "wrong"
    elif mutation == "span":
        item["hole_spans"][0][0] += 1
    elif mutation == "index":
        item["holes"] = [0]
    else:
        allowed["Other.lean"] = allowed.pop("src/Main.lean")
    with pytest.raises(ValueError, match="handoff"):
        discover(root, [root / "src/Main.lean"], fill_definitions=False, authorized_holes=allowed)


def test_loader_requires_exact_selected_published_revision(reviewed) -> None:
    root, result = reviewed
    directory = document_directory(root, "paper", published=True)
    assert (
        handoff.validate_handoff(root, directory / "handoff.json", result["revision_id"]) == result
    )
    durable_json(directory / "current.json", {"revision_id": "rev-other"})
    with pytest.raises(ValueError, match="selected committed"):
        handoff.validate_handoff(root, directory / "handoff.json", result["revision_id"])


def test_new_private_selection_invalidates_previous_published_handoff(reviewed) -> None:
    root, result = reviewed
    save_revision(
        document_directory(root, "paper"),
        {
            "schema_version": 1,
            "document_id": "paper",
            "status": "inventory_pending",
        },
    )
    path = document_directory(root, "paper", published=True) / "handoff.json"
    with pytest.raises(ValueError, match="selected document revision"):
        handoff.validate_handoff(root, path, result["revision_id"])


def test_loader_rejects_source_and_environment_drift(reviewed, monkeypatch) -> None:
    root, result = reviewed
    path = document_directory(root, "paper", published=True) / "handoff.json"
    monkeypatch.setattr(handoff, "environment_identity", lambda root: "other")
    with pytest.raises(ValueError, match="environment changed"):
        handoff.validate_handoff(root, path, result["revision_id"])
    monkeypatch.setattr(handoff, "environment_identity", lambda root: "environment")
    (root / "src/Main.lean").write_text("theorem changed : True := by trivial")
    with pytest.raises(ValueError, match="source changed"):
        handoff.validate_handoff(root, path, result["revision_id"])


def test_loader_binds_projection_to_immutable_checks(reviewed) -> None:
    root, result = reviewed
    directory = document_directory(root, "paper", published=True)
    result["protected_declarations"]["Paper.target"]["type_hash"] = "tampered"
    result["handoff_id"] = "handoff-" + identity(
        {key: value for key, value in result.items() if key != "handoff_id"}
    )
    durable_json(directory / "handoff.json", result)
    save_receipt(
        directory,
        {
            "status": "published",
            "revision_id": result["revision_id"],
            "handoff": result,
        },
    )
    with pytest.raises(ValueError, match="immutable compiled acceptance"):
        handoff.validate_handoff(root, directory / "handoff.json", result["revision_id"])


def test_new_rejected_receipt_invalidates_same_revision_handoff(reviewed) -> None:
    root, result = reviewed
    published = document_directory(root, "paper", published=True)
    private = document_directory(root, "paper")
    from leanflow_cli.formalization.records import load_document

    record = load_document(root, "paper")
    save_revision(private, record)
    save_receipt(private, {"revision_id": result["revision_id"], "status": "review_rejected"})
    with pytest.raises(ValueError, match="published acceptance receipt"):
        handoff.validate_handoff(root, published / "handoff.json", result["revision_id"])


def test_loader_rejects_changed_original_proof_guidance_blob(reviewed) -> None:
    root, result = reviewed
    directory = document_directory(root, "paper", published=True)
    artifact = directory / "blobs" / digest("original proof")
    artifact.parent.mkdir()
    artifact.write_text("changed proof")
    result["guidance_refs"] = [
        {"artifact": artifact.relative_to(root).as_posix(), "sha256": artifact.name}
    ]
    result["handoff_id"] = "handoff-" + identity(
        {key: value for key, value in result.items() if key != "handoff_id"}
    )
    durable_json(directory / "handoff.json", result)
    save_receipt(
        directory, {"status": "published", "revision_id": result["revision_id"], "handoff": result}
    )
    with pytest.raises(ValueError, match="guidance artifact changed"):
        handoff.validate_handoff(root, directory / "handoff.json", result["revision_id"])


def test_no_obligations_never_starts_prover(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_HANDOFF", "formalization/paper/handoff.json")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_REVISION", "rev-test")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "empty-formalization")
    monkeypatch.delenv("LEANFLOW_PROVER_RESUME_RUN_ID", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(
        handoff,
        "validate_handoff",
        lambda *args, **kwargs: {
            "obligations": [],
            "acceptance_policy": {"allowed_axioms": list(ProverConfig().allowed_axioms)},
        },
    )
    monkeypatch.setattr(
        runtime, "ProverRuntime", lambda **kwargs: pytest.fail("No prover/model may launch")
    )

    class Observer:
        def __init__(self, *args):
            pass

        def start(self, *args):
            pass

        def finish(self, *args):
            pass

    monkeypatch.setattr(entrypoint, "RunObserver", Observer)
    assert entrypoint._run_project(tmp_path) == 0
    state = json.loads(
        (tmp_path / ".leanflow/workflow-state/prover/empty-formalization/state.json").read_text()
    )
    assert state["status"] == "no_proof_obligations" and state["metrics"]["api_calls"] == 0


def test_runtime_retains_formalization_scope_and_guidance(reviewed) -> None:
    root, result = reviewed
    controller = runtime.ProverRuntime(
        root=root,
        targets=[root / "src/Main.lean"],
        config=ProverConfig(),
        run_id="scoped",
        formalization_handoff=result,
    )
    assert [node.name for node in controller.dag.nodes] == ["Paper.target"]
    assert controller.state["formalization_handoff"] == result
    assert controller._context()["formalization_guidance"] == result["guidance"]
    assert "source-1" in controller.dag.nodes[0].notes


def test_prover_acceptance_holds_publication_ownership(reviewed, monkeypatch) -> None:
    root, result = reviewed
    controller = runtime.ProverRuntime(
        root=root,
        targets=[root / "src/Main.lean"],
        config=ProverConfig(),
        run_id="guarded",
        formalization_handoff=result,
    )
    held = []

    @contextmanager
    def guard(project, *, cancelled=None, deadline=None):
        assert project == root
        assert callable(cancelled) and cancelled() is False
        assert deadline is None or math.isfinite(deadline)
        held.append(project)
        try:
            yield
        finally:
            held.pop()

    def accepted(*args):
        assert held == [root]
        return True

    monkeypatch.setattr(publication, "publication_guard", guard)
    monkeypatch.setattr(controller, "_accept_locked", accepted)
    assert controller._accept(controller.dag.nodes[0], ["trivial"], "job")
    assert not held


def test_pending_publication_blocks_ordinary_prover_startup(tmp_path, monkeypatch) -> None:
    def pending(root):
        raise ValueError("publication requires recovery")

    monkeypatch.setattr(publication, "assert_no_pending_publication", pending)
    # The real startup observer writes these process-global values directly.
    # Register them for restoration so later tests do not share this project's logs.
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "blocked-publication")
    monkeypatch.delenv("LEANFLOW_FORMALIZATION_HANDOFF", raising=False)
    monkeypatch.delenv("LEANFLOW_FORMALIZATION_REVISION", raising=False)
    monkeypatch.setattr(runtime, "ProverRuntime", lambda **kwargs: pytest.fail("must not start"))
    assert entrypoint._run_project(tmp_path) == 1
    state = json.loads(
        (tmp_path / ".leanflow/workflow-state/prover/blocked-publication/state.json").read_text()
    )
    assert "publication requires recovery" in state["error"]


def test_proof_certificate_is_immutable_and_rechecks_protected_types(
    reviewed, monkeypatch, admitted_snapshot_layout
) -> None:
    root, result = reviewed
    path = root / "src/Main.lean"
    document = SourceDocument("src/Main.lean", path.read_text(), replacements={"1": "trivial"})
    path.write_text(document.render())
    profiles = {
        "Paper.target": {"type_hash": "type-target", "body_hash": "proved-body", "axioms": []}
    }
    monkeypatch.setattr(
        handoff, "inspect_draft", lambda *args, **kwargs: {"accepted": True, "profiles": profiles}
    )
    before = (document_directory(root, "paper", published=True) / "handoff.json").read_bytes()
    certificate = handoff.proof_preservation_certificate(
        root, result, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
    )
    assert certificate["reviewed_revision_id"] == result["revision_id"]
    assert certificate["before"] != certificate["after"]
    assert (
        document_directory(root, "paper", published=True) / "handoff.json"
    ).read_bytes() == before
    profiles["Paper.target"]["type_hash"] = "changed-type"
    with pytest.raises(ValueError, match="type changed"):
        handoff.proof_preservation_certificate(
            root, result, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
        )


@pytest.mark.parametrize("change", ["live", "frozen", "new_file"])
def test_proof_certificate_rejects_inspection_input_drift(
    reviewed, monkeypatch, change, admitted_snapshot_layout
) -> None:
    root, result = reviewed
    path = root / "src/Main.lean"
    document = SourceDocument("src/Main.lean", path.read_text(), replacements={"1": "trivial"})
    path.write_text(document.render())

    def inspect(frozen, modules, workspace, axioms, **kwargs):
        assert frozen != root
        assert (frozen / document.path).read_text() == document.render()
        target = frozen if change == "frozen" else root
        (target / ("New.lean" if change == "new_file" else document.path)).write_text(
            "def changed := 4"
        )
        return {"accepted": True, "profiles": {}}

    monkeypatch.setattr(handoff, "inspect_draft", inspect)
    with pytest.raises(ValueError, match="source changed"):
        handoff.proof_preservation_certificate(
            root, result, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
        )


def test_proof_certificate_rejects_policy_widening_and_cancellation(reviewed, monkeypatch) -> None:
    root, result = reviewed
    monkeypatch.setattr(handoff, "inspect_draft", lambda *a, **k: pytest.fail("must not inspect"))
    with pytest.raises(ValueError, match="axiom policy differs"):
        handoff.proof_preservation_certificate(root, result, {}, root / "checks", ("invented",))
    with pytest.raises(ValueError, match="cancelled"):
        handoff.proof_preservation_certificate(
            root, result, {}, root / "checks", ProverConfig().allowed_axioms, cancelled=lambda: True
        )
    with pytest.raises(ValueError, match="deadline"):
        handoff.proof_preservation_certificate(
            root, result, {}, root / "checks", ProverConfig().allowed_axioms, deadline=0
        )


def test_snapshot_authenticates_proof_completion_and_opens_certified_source(
    reviewed, monkeypatch, admitted_snapshot_layout
) -> None:
    from leanflow_cli.formalization.service import resolve_artifact, show_document

    root, result = reviewed
    initial = show_document(root, "paper")
    assert initial["items"][0]["proof_status"] == "pending" and initial["can_prove"]
    path = root / "src/Main.lean"
    document = SourceDocument("src/Main.lean", path.read_text(), replacements={"1": "trivial"})
    path.write_text(document.render())
    assert show_document(root, "paper")["lean_stale"]
    profiles = {
        "Paper.target": {
            "name": "Paper.target",
            "file": "src/Main.lean",
            "type_hash": "type-target",
            "body_hash": "proved",
            "pending": False,
            "conditional": False,
            "axioms": [],
        }
    }
    monkeypatch.setattr(
        handoff,
        "inspect_draft",
        lambda *args, **kwargs: {
            "accepted": True,
            "profiles": profiles,
            **result["acceptance_policy"],
        },
    )
    certificate = handoff.proof_preservation_certificate(
        root, result, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
    )
    shown = show_document(root, "paper")
    assert (
        not shown["lean_stale"]
        and shown["proof_completion"] == "checked"
        and not shown["can_prove"]
    )
    assert shown["proof_revision_id"] == certificate["proof_revision_id"]
    assert (
        shown["items"][0]["proof_status"] == "checked"
        and shown["items"][0]["review_status"] == "pass"
    )
    reference = shown["items"][0]["lean_refs"][0]
    opened = resolve_artifact(root, "paper", result["revision_id"], reference["artifact_id"])
    assert Path(opened["path"]).read_text() == document.render()
    path.write_text(document.render() + "-- drift\n")
    stale = show_document(root, "paper")
    assert (
        stale["lean_stale"]
        and stale["proof_completion"] == "pending"
        and not stale["proof_revision_id"]
    )


def test_mixed_existing_reuse_and_generated_proof_authenticates_completed_certificate(
    reviewed, monkeypatch, admitted_snapshot_layout
) -> None:
    """Retain readonly reuse without expecting it in the owned proof compiler profiles."""
    from leanflow_cli.formalization.service import show_document

    original_root, old_handoff = reviewed
    record = load_document(original_root, "paper")
    root = original_root / "mixed-project"
    shutil.copytree(
        original_root,
        root,
        ignore=shutil.ignore_patterns(".leanflow", "formalization", "mixed-project"),
    )
    save_registry(root, load_registry(root))
    directory = document_directory(root, "paper", published=True)
    record["checks"]["declarations"].append(
        {
            "name": "other",
            "module": "Other",
            "file": "Unrelated.lean",
            "declaration_origin": "existing_project",
            "kind": "definition",
            "type_hash": "readonly-type",
            "body_hash": "readonly-body",
            "source_sha256": old_handoff["files"]["Unrelated.lean"],
            "pending": False,
        }
    )
    record = save_revision(directory, record)
    accepted = handoff.build_handoff(
        root,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=record["checks"],
        files=record["project_source_hashes"],
        environment_id=record["environment_id"],
    )
    durable_json(directory / "handoff.json", accepted)
    save_receipt(
        directory,
        {"status": "published", "revision_id": record["revision_id"], "handoff": accepted},
    )
    _publish_fixture_controls(root, "paper", record["revision_id"])
    untouched = (root / "Unrelated.lean").read_bytes()
    path = root / "src/Main.lean"
    document = SourceDocument("src/Main.lean", path.read_text(), replacements={"1": "trivial"})
    path.write_text(document.render())
    profiles = {
        "Paper.target": {
            "name": "Paper.target",
            "type_hash": "type-target",
            "body_hash": "proved",
            "pending": False,
            "conditional": False,
            "axioms": [],
        }
    }
    monkeypatch.setattr(
        handoff,
        "inspect_draft",
        lambda *args, **kwargs: {
            "accepted": True,
            "profiles": profiles,
            **accepted["acceptance_policy"],
        },
    )
    certificate = handoff.proof_preservation_certificate(
        root, accepted, {document.path: document}, root / "checks", ProverConfig().allowed_axioms
    )
    assert "other" not in certificate["checks"]["profiles"]
    assert (root / "Unrelated.lean").read_bytes() == untouched
    view = show_document(root, "paper")
    assert view["proof_completion"] == "checked", view["diagnostics"]
    assert view["proof_revision_id"] == certificate["proof_revision_id"]
    assert not view["can_prove"]
    profiles["Paper.target"]["type_hash"] = "changed"
    with pytest.raises(ValueError, match="type changed"):
        handoff.proof_preservation_certificate(
            root,
            accepted,
            {document.path: document},
            root / "checks",
            ProverConfig().allowed_axioms,
        )
