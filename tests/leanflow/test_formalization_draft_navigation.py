"""Navigate legacy frozen full drafts without importing provisional mappings into acceptance."""

from __future__ import annotations

import copy
import os
import shutil
import time

import pytest

from leanflow_cli.formalization.draft_delivery import validate_best_effort
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.records import load_document
from leanflow_cli.formalization.service_draft_navigation import draft_item_mappings
from leanflow_cli.formalization.service_snapshot import snapshot
from tests.leanflow.test_formalization_draft_delivery import _retain, _write_proposal
from tests.leanflow.test_formalization_draft_delivery import delivery_inputs as _delivery_inputs

delivery_inputs = _delivery_inputs


def _checked(inputs):
    record, project, layout, _ = inputs
    census = validate_best_effort(project, record, layout=layout)
    return {
        "complete": True,
        "census": census,
        "checks": {
            "accepted": True,
            "project_build": {"success": True},
            "declarations": [
                {
                    "name": name,
                    "file": "Main/Document/Statements.lean",
                    "line_start": line,
                    "pretty_type": "Nat" if line == 1 else "0 = 0",
                    "type_hash": digest(name + " type"),
                    "body_hash": digest(name + " body"),
                }
                for name, line in (("Paper.object", 1), ("Paper.result", 2))
            ],
        },
    }


def _seal(preview):
    preview["preview_id"] = "draft-preview-" + identity(
        {key: value for key, value in preview.items() if key != "preview_id"}
    )
    return preview


def test_legacy_full_draft_maps_late_items_from_frozen_evidence_without_project_reads(
    tmp_path, delivery_inputs
):
    record, project, _, _ = delivery_inputs
    preview = _retain(tmp_path, delivery_inputs, check_report=_checked(delivery_inputs))
    before = copy.deepcopy(record), copy.deepcopy(preview)
    # Immutable37 deliveries have no navigation field; the new viewer derives it.
    assert "draft_mapping" not in preview and not record["units"]
    shutil.rmtree(project)
    mapped = draft_item_mappings(tmp_path, record, preview)
    late = record["inventory"]["items"][-1]["id"]
    refs = mapped[late]["lean_refs"]
    assert {ref["name"] for ref in refs} == {"Paper.object", "Paper.result"}
    assert refs[-1]["line_start"] == 2 and refs[-1]["type"] == "0 = 0"
    assert all(ref["artifact_id"] in {row["id"] for row in preview["artifacts"]} for ref in refs)
    assert mapped[late]["issues"] == []
    assert (record, preview) == before
    shown = snapshot(tmp_path, load_document(tmp_path, record["document_id"]), verify_current=False)
    assert shown["items"][-1]["draft_mapping"] == mapped[late]
    assert all(not item["lean_refs"] for item in shown["items"])
    assert shown["can_prove"] is False and shown["handoff"] is None


@pytest.mark.parametrize("damage", ["file", "line", "hash", "duplicate", "malformed"])
def test_bad_compiled_locations_never_create_draft_links(tmp_path, delivery_inputs, damage):
    report = _checked(delivery_inputs)
    if damage == "file":
        report["checks"]["declarations"][0]["file"] = "Other.lean"
    elif damage == "line":
        report["checks"]["declarations"][0]["line_start"] = 999
    elif damage == "hash":
        report["checks"]["declarations"][0]["body_hash"] = "unverified"
    elif damage == "duplicate":
        report["checks"]["declarations"].append(report["checks"]["declarations"][0])
    else:
        report["checks"] = []
    preview = _retain(tmp_path, delivery_inputs, check_report=report)
    mapped = draft_item_mappings(tmp_path, delivery_inputs[0], preview)
    assert all(row["issues"] for row in mapped.values())
    assert all(
        "Paper.object" not in {ref["name"] for ref in row["lean_refs"]} for row in mapped.values()
    )


@pytest.mark.parametrize(
    "damage",
    [
        "artifact_hash",
        "cross_run",
        "cross_revision",
        "missing_report",
        "report_binding",
        "blob_bytes",
    ],
)
def test_frozen_identity_gaps_are_visible_for_every_source(tmp_path, delivery_inputs, damage):
    report = _checked(delivery_inputs)
    if damage == "report_binding":
        report["census"]["proposal_sha256"] = "a" * 64
    preview = _retain(tmp_path, delivery_inputs, check_report=report)
    artifact = next(row for row in preview["artifacts"] if row["kind"] == "lean")
    if damage == "artifact_hash":
        artifact["sha256"] = "a" * 64
        artifact["id"] = "draft-" + identity(
            {"run_id": preview["run_id"], "path": artifact["path"], "sha256": artifact["sha256"]}
        )
    elif damage == "cross_run":
        artifact["id"] = "draft-" + identity(
            {"run_id": "another-run", "path": artifact["path"], "sha256": artifact["sha256"]}
        )
    elif damage == "cross_revision":
        preview["base_revision_id"] = "another-revision"
    elif damage == "missing_report":
        preview["delivery"]["report_artifact_ids"].pop("Check-report")
    elif damage == "blob_bytes":
        directory = tmp_path / ".leanflow/formalizations" / delivery_inputs[0]["document_id"]
        (directory / "blobs" / artifact["sha256"]).write_text("Changed frozen bytes")
    _seal(preview)
    mapped = draft_item_mappings(tmp_path, delivery_inputs[0], preview)
    assert len(mapped) == len(delivery_inputs[0]["inventory"]["items"])
    assert all(not row["lean_refs"] and row["issues"] for row in mapped.values())


def test_author_navigation_coordinates_and_approval_fields_do_not_override_compiled_evidence(
    tmp_path, delivery_inputs
):
    record, _, _, proposal = delivery_inputs
    proposal["units"][0]["declarations"][0].update(
        line_start=999, type="forged", artifact_id="outside"
    )
    _write_proposal(delivery_inputs[1], proposal)
    preview = _retain(tmp_path, delivery_inputs, check_report=_checked(delivery_inputs))
    mappings = draft_item_mappings(tmp_path, record, preview)
    ref = next(iter(mappings.values()))["lean_refs"][0]
    assert ref["line_start"] == 1 and ref["type"] == "Nat" and ref["artifact_id"] != "outside"
    assert "accepted" not in ref and not record["units"]


def test_old_source_uncertain_preview_has_no_provisional_mapping(tmp_path, delivery_inputs):
    assert draft_item_mappings(tmp_path, delivery_inputs[0], None) == {}
    assert (
        draft_item_mappings(tmp_path, delivery_inputs[0], {"status": "unaccepted_source_uncertain"})
        == {}
    )


def test_pinned_library_reuse_is_visible_without_inventing_a_local_file(tmp_path, delivery_inputs):
    record, project, _, proposal = delivery_inputs
    unit = proposal["units"][0]
    unit["declarations"] = [{"name": "Nat.add_comm", "imports": ["Init"]}]
    unit["clause_dispositions"][0]["declaration_names"] = ["Nat.add_comm"]
    _write_proposal(project, proposal)
    report = _checked(delivery_inputs)
    report["checks"]["declarations"] = [
        {
            "name": "Nat.add_comm",
            "declaration_origin": "dependency",
            "type_hash": "a" * 64,
            "body_hash": "b" * 64,
        }
    ]
    preview = _retain(tmp_path, delivery_inputs, check_report=report)
    mapped = draft_item_mappings(tmp_path, record, preview)
    assert all(row["lean_refs"] == report["checks"]["declarations"] for row in mapped.values())
    assert all("artifact_id" not in row["lean_refs"][0] for row in mapped.values())


def test_explicit_exclusion_keeps_a_row_without_a_fictitious_mapping(tmp_path, delivery_inputs):
    from leanflow_cli.formalization.service import act_document

    record, project, layout, proposal = delivery_inputs
    excluded = record["inventory"]["items"][-1]["id"]
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "set_scope",
            "item_id": excluded,
            "scope": "excluded",
            "reason": "The user explicitly excludes this separate theorem from the translation.",
        },
    )
    record = load_document(tmp_path, record["document_id"])
    proposal["units"][0]["required_source_ids"].remove(excluded)
    _write_proposal(project, proposal)
    inputs = record, project, layout, proposal
    preview = _retain(tmp_path, inputs, check_report=_checked(inputs))
    mapped = draft_item_mappings(tmp_path, record, preview)
    assert len(mapped) == 2 and mapped[excluded]["lean_refs"] == []
    assert mapped[excluded]["issues"] == ["Explicitly excluded source item."]
    assert preview["delivery"]["counts"]["source_items"] == 2
    assert preview["delivery"]["counts"]["excluded_source_items"] == 1


@pytest.mark.integration
def test_real_inspector_locations_and_types_drive_frozen_draft_navigation(
    tmp_path, delivery_inputs
):
    """Exercise the real current compiler report shape rather than assuming synthetic location keys."""
    from leanflow_cli.formalization.lean_build import build_project
    from leanflow_cli.formalization.lean_layout import inspect_layout
    from leanflow_cli.formalization.lean_policy import inspect_draft

    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned compiler integration")
    record, project, _, _ = delivery_inputs
    (project / "lean-toolchain").write_text(toolchain + "\n")
    deadline = time.monotonic() + 90
    checks = inspect_draft(
        project,
        ["Main/Document/Statements.lean"],
        tmp_path / "real-inspector",
        (),
        deadline=deadline,
    )
    assert checks["accepted"], checks
    checks["project_build"] = build_project(
        project, inspect_layout(project), tmp_path / "real-build", deadline=deadline
    )
    assert checks["project_build"]["success"], checks["project_build"]
    report = _checked(delivery_inputs)
    report["checks"] = checks
    preview = _retain(tmp_path, delivery_inputs, check_report=report)
    mapped = draft_item_mappings(tmp_path, record, preview)
    refs = next(iter(mapped.values()))["lean_refs"]
    assert {row["name"]: row["line_start"] for row in refs} == {
        "Paper.object": 1,
        "Paper.result": 2,
    }
    compiled = {row["name"]: row for row in checks["declarations"]}
    assert all(ref["type"] == compiled[ref["name"]]["pretty_type"] for ref in refs)
    assert all(not row["issues"] for row in mapped.values())
