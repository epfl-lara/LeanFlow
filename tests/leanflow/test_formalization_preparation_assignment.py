"""Deliver complete checked estimates to native authors while preserving separate semantic gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import intake, preparation, slice_gate
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.formalization_documents import _select_formalization_document
from leanflow_cli.formalization.preparation_assignment import checked_preparation_assignment
from leanflow_cli.formalization.records import create_document, document_directory, save_revision
from leanflow_cli.formalization.source_review_regions import coalesce_regions, compact_region
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native import harness_formalization_stages
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.native.harness_formalization_stages import stage_assignment


@pytest.fixture
def estimated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paper = tmp_path / "paper.tex"
    paper.write_text(
        r"\begin{definition}Double a natural number.\end{definition}"
        "\n"
        r"\begin{theorem}Doubling zero yields zero.\end{theorem}"
    )
    record = create_document(tmp_path, paper)
    record.update(
        workflow_protocol=2,
        environment_id="checked-fixture-environment",
        preparation_id="prepared",
        preparation_plan_id="plan",
        slice_source_ids=[record["inventory"]["items"][0]["id"]],
    )
    record = save_revision(document_directory(tmp_path, record["document_id"]), record)
    receipt = {
        "environment_id": "checked-fixture-environment",
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
    environment["LEANFLOW_NATIVE_WORKFLOW_KIND"] = "formalize"
    frozen = json.loads(Path(environment["LEANFLOW_FORMALIZATION_MANIFEST"]).read_text())
    assert frozen["checked_preparation_plan"]["plan"] == receipt["plan"]
    snapshot, scratch = root / ".leanflow/author", root / ".leanflow/author/.leanflow-job"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    assignment, prompt = prepare_context(
        root=root, snapshot=snapshot, scratch=scratch, environment=environment, allowed=()
    )
    assignment, prompt = stage_assignment(root, environment, assignment, prompt)
    reference = assignment["formalization_stage"]["checked_preparation_plan"]
    copied = json.loads(Path(reference["manifest"]).read_text())
    assert copied["checked_preparation_plan"]["plan"] == receipt["plan"]
    assert reference["json_pointer"] == "/checked_preparation_plan"
    assert reference["required_imports"] == receipt["plan"]["required_imports"]
    assert "estimates" in prompt and "Existing.double" in json.dumps(copied)
    audit = json.loads(Path(frozen["source_region_partition"]["path"]).read_text())
    assert copy.deepcopy(record["inventory"]) == audit["canonical_inventory"]
    assert frozen["inventory"]["regions"] == [compact_region(row) for row in audit["groups"]]
    private_ref = copied["source_region_partition"]
    private_bytes = Path(private_ref["path"]).read_bytes()
    assert private_bytes == Path(frozen["source_region_partition"]["path"]).read_bytes()
    assert private_ref["bytes_sha256"] == digest(private_bytes)
    assert identity(json.loads(private_bytes)) == private_ref["sha256"]
    assert Path(private_ref["path"]).is_relative_to(snapshot)
    stage = assignment["formalization_stage"]
    selected_partition = json.loads(Path(stage["source_region_partition"]["path"]).read_text())
    assert selected_partition["canonical_regions"] == [
        row
        for row in record["inventory"]["regions"]
        if row["id"] in slice_gate.slice_regions(record)
    ]
    assert stage["required_region_ids"] == [
        row["id"] for row in coalesce_regions(selected_partition["canonical_regions"])
    ]


@pytest.mark.parametrize("phase", ["representative_slice", "complete"])
def test_stage_groups_bind_exact_phase_members_without_dumping_canonical_ids(
    estimated, monkeypatch, phase
):
    root, record, _ = estimated
    before = copy.deepcopy(record["inventory"])
    monkeypatch.setattr(
        harness_formalization_stages,
        "slice_status",
        lambda *args: {"phase": phase, "required": True},
    )
    snapshot = root / ".leanflow/author"
    (snapshot / ".leanflow-job").mkdir(parents=True)
    context, prompt = stage_assignment(
        root,
        {
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        },
        {"project_root": str(snapshot)},
        "Translate the assigned source.",
    )
    stage = context["formalization_stage"]
    reference = stage["source_region_partition"]
    partition = json.loads(Path(reference["path"]).read_text())
    expected = [
        row
        for row in before["regions"]
        if phase == "complete" or row["id"] in slice_gate.slice_regions(record)
    ]
    assert partition["canonical_regions"] == expected
    assert partition["full_inventory"] == before
    assert identity(partition) == reference["sha256"]
    assert stage["required_region_ids"] == [row["id"] for row in coalesce_regions(expected)]
    assert [row for group in partition["groups"] for row in group["member_regions"]] == expected
    assert all(
        "member_regions" not in group and "member_region_ids" not in group
        for group in stage["regions"]
    )
    assert not any(row["id"] in prompt for row in expected)
    assert record["inventory"] == before


@pytest.mark.parametrize("mutation", ["contents", "hash", "outside", "alias"])
def test_native_partition_copy_rejects_missing_or_changed_authority(
    estimated, monkeypatch, mutation
):
    from leanflow_cli.native.harness_context import _copy_source_partition

    root, record, _ = estimated
    partition = {
        "canonical_inventory": record["inventory"],
        "groups": coalesce_regions(record["inventory"]["regions"]),
    }
    original = root / "source-partition.json"
    original.write_text(json.dumps(partition))
    reference = {
        "path": str(original),
        "sha256": identity(partition),
        "canonical_region_count": len(record["inventory"]["regions"]),
        "group_count": len(partition["groups"]),
    }
    if mutation == "contents":
        original.write_text(json.dumps({**partition, "groups": []}))
    elif mutation == "hash":
        reference["sha256"] = "forged"
    elif mutation == "outside":
        reference["path"] = str(root.parent / "outside-partition.json")
    else:
        alias = root / "partition-alias.json"
        alias.symlink_to(original)
        reference["path"] = str(alias)
    snapshot = root / ".leanflow/author"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    with pytest.raises((OSError, ValueError)):
        _copy_source_partition(
            root, snapshot, scratch, json.dumps({"source_region_partition": reference})
        )
    assert not (scratch / "formalization-source-partition.json").exists()
