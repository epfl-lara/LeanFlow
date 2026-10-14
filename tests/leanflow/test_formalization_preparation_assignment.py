"""Deliver complete checked estimates to native authors while preserving separate semantic gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import intake, preparation
from leanflow_cli.formalization.formalization_documents import _select_formalization_document
from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment
from leanflow_cli.formalization.records import create_document, document_directory, save_revision
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.native.harness_formalization_stages import stage_assignment


@pytest.fixture
def estimated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paper = tmp_path / "paper.tex"
    paper.write_text(r"\begin{definition}Double a natural number.\end{definition}")
    record = create_document(tmp_path, paper)
    record.update(
        workflow_protocol=2,
        preparation_id="prepared",
        preparation_plan_id="plan",
        slice_source_ids=[record["inventory"]["items"][0]["id"]],
    )
    record = save_revision(document_directory(tmp_path, record["document_id"]), record)
    receipt = {
        "preparation_id": "prepared",
        "preparation_plan_id": "plan",
        "source_bundle": record["source_bundle"],
        "plan": {
            "plan_id": "plan",
            "slice_source_ids": record["slice_source_ids"],
            "required_imports": ["Init.Data.Nat.Basic"],
            "packages": [{"name": "repl", "revision": "a" * 40, "authorization": "catalog"}],
            "network_policy": {"allow_unlisted_sources": False},
            "mathematical_prerequisites": [
                {
                    "source_ids": record["slice_source_ids"],
                    "description": "Reuse the checked natural-number doubling candidate",
                    "strategy": "search_existing_then_define",
                    "candidate_declarations": ["Existing.double"],
                }
            ],
            "representation_notes": ["Use addition over natural numbers"],
        },
    }
    monkeypatch.setattr(preparation, "load_preparation_receipt", lambda root: receipt)
    monkeypatch.setattr(preparation, "assert_preparation_current", lambda root, **kwargs: receipt)
    return tmp_path, record, receipt


def test_checked_plan_is_complete_but_mathematical_estimates_have_no_approval(estimated):
    root, record, receipt = estimated
    value = checked_preparation_assignment(root, record)
    assert value["plan"] == receipt["plan"] and value["infrastructure_status"] == "checked"
    assert value["mathematical_approval"].startswith("none;")
    value["plan"]["network_policy"]["allow_unlisted_sources"] = True
    assert receipt["plan"]["network_policy"]["allow_unlisted_sources"] is False


@pytest.mark.parametrize(
    "field", ["preparation_id", "preparation_plan_id", "source_bundle", "slice_source_ids"]
)
def test_different_receipt_cannot_supply_author_estimates(estimated, field):
    root, record, receipt = estimated
    if field == "slice_source_ids":
        receipt["plan"][field] = []
    else:
        receipt[field] = "changed"
    with pytest.raises(RepreparationRequired):
        checked_preparation_assignment(root, record)


def test_intake_and_native_stage_deliver_plan_through_copied_manifest(estimated, monkeypatch):
    root, record, receipt = estimated
    monkeypatch.setattr(
        intake,
        "preflight_dependencies",
        lambda *args: {
            "accepted": True,
            "library": "Main",
            "src_dir": ".",
            "module_prefix": "Main.Document",
            "owned_prefix": "Main/Document",
            "root_file": "Main.lean",
            "aggregator": "Main/Document.lean",
            "modules": ["Main.Document"],
        },
    )
    selection = _select_formalization_document(root, root, "paper.tex")
    context = intake.prepare_intake(root, selection, require_preparation=True)
    environment = context.to_env()
    frozen = json.loads(Path(environment["LEANFLOW_FORMALIZATION_MANIFEST"]).read_text())
    assert frozen["checked_preparation_plan"]["plan"] == receipt["plan"]
    snapshot, scratch = root / ".leanflow/author", root / ".leanflow/author/.leanflow-job"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    assignment, prompt, _ = prepare_context(
        root=root, snapshot=snapshot, scratch=scratch, environment=environment, allowed=()
    )
    assignment, prompt = stage_assignment(root, environment, assignment, prompt)
    reference = assignment["formalization_stage"]["checked_preparation_plan"]
    copied = json.loads(Path(reference["manifest"]).read_text())
    assert copied["checked_preparation_plan"]["plan"] == receipt["plan"]
    assert reference["json_pointer"] == "/checked_preparation_plan"
    assert reference["required_imports"] == receipt["plan"]["required_imports"]
    assert "estimates" in prompt and "Existing.double" in json.dumps(copied)
    assert copy.deepcopy(record["inventory"]) == frozen["inventory"]
