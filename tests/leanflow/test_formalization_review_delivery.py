"""Keep complete review evidence usable within bounded native tool responses."""

import json
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.formalization import review
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.review_dossier import (
    EvidenceBridge,
    evidence_documents,
    mandatory_dossier,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools
from tests.leanflow.test_formalization_revision_review import (
    _review_transport,
    answer,
)
from tests.leanflow.test_formalization_revision_review import source_record as _source_record

source_record = _source_record


def test_batch_summary_omits_unrelated_declarations_and_large_proof_terms(source_record):
    huge = "Expr.app " * 250000
    source_record["checks"]["declarations"][0]["body"] = huge
    source_record["checks"]["declarations"].append(
        {"name": "unrelated", "type": "True", "body": huge}
    )
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=False)
    assert [row["name"] for row in dossier["compiled_declarations"]] == ["t"]
    assert "body" not in dossier["compiled_declarations"][0]
    assert len(json.dumps(dossier)) < 200000
    documents = evidence_documents(source_record)
    assert documents["declaration:t"]["body"] == huge
    assert documents["declaration:unrelated"]["body"] == huge


def test_large_region_explicitly_identifies_paged_original_text(source_record):
    content = "α" * 40000
    source_record["source_files"]["paper.tex"] = put_blob(
        Path(source_record["_directory"]) / "blobs", content.encode()
    )
    region = source_record["inventory"]["regions"][0]
    region.update(byte_start=0, byte_end=len(content.encode()))
    source_record["inventory"]["regions"] = [region]
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    entry = dossier["regions"][0]
    assert entry["original_text"] is None and entry["text_delivery"] == "paged"
    assert evidence_documents(source_record)[entry["evidence_id"]]["original_text"] == content


@pytest.mark.parametrize("role", ["project", "dependency"])
def test_library_definitions_have_exact_retrievable_authority_and_bind_review(source_record, role):
    row = {
        "name": "Library.Semantics",
        "module": "Library",
        "role": role,
        "kind": "definition",
        "type": "Expr.sort 1",
        "body": "Expr.const Nat []",
        "pretty_type": "Type",
        "pretty_body": "Nat",
        "levels": [],
        "direct_dependencies": ["Nat"],
    }
    source_record["checks"]["library_declarations"] = [row]
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=False)
    item = dossier["library_evidence"]["declarations"][0]
    assert item["role"] == role
    evidence_id = item["evidence_id"]
    assert evidence_id == "library-declaration:Library.Semantics"
    assert "body" not in item
    document = evidence_documents(source_record)[evidence_id]
    assert document["authority"] == (
        "independently-replayed-compiled-project-context"
        if role == "project"
        else "independently-replayed-compiled-library"
    )
    assert document["body"] == row["body"]
    assert document["source_build_correspondence"] == "not_claimed"
    before = dossier["evidence_catalog"][evidence_id]["sha256"]
    row["body"] = "Expr.const Bool []"
    after = mandatory_dossier(source_record, source_record["units"], coherence=False)
    assert after["evidence_catalog"][evidence_id]["sha256"] != before


def test_companion_pdf_regions_and_proofs_reach_review_as_page_text(source_record):
    raw = b"%PDF-1.7\n\xff frozen companion bytes"
    key = put_blob(Path(source_record["_directory"]) / "blobs", raw)
    source_record["source_files"]["paper.pdf"] = key
    inventory = source_record["inventory"]
    inventory["files"]["paper.pdf"] = {"encoding": "binary", "sha256": key}
    inventory["companion_pdf"] = {"file": "paper.pdf", "correspondence_verified": False}
    inventory["pages"] = [
        {"file": "paper.pdf", "index": 0, "text": "Complete statement and proof."}
    ]
    region = {
        "id": "companion-page",
        "file": "paper.pdf",
        "coordinate_space": "pdf_page",
        "page_index": 0,
        "byte_start": 0,
        "byte_end": len(raw),
        "text_start": 0,
        "text_end": 29,
    }
    inventory["regions"].append(region)
    inventory["items"][0]["proof_spans"] = [{**region, "text_start": 23, "text_end": 29}]
    documents = evidence_documents(source_record)
    assert documents["source:paper.pdf"]["pages"] == inventory["pages"]
    assert documents["region:companion-page"]["original_text"] == "Complete statement and proof."
    assert not documents["region:companion-page"]["binary_requires_visual_repair"]
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=True)
    assert dossier["source_items"][0]["proof_text"] == ["proof."]
    assert dossier["regions"][-1]["text_delivery"] == "inline"


def test_semantic_surface_omits_proof_only_names_but_preserves_audit(source_record):
    source_record["checks"]["library_declarations"] = [
        {"name": "Library.Data", "module": "Library", "semantic_surface": True},
        {"name": "Library.Proof", "module": "Library", "semantic_surface": False},
    ]
    dossier = mandatory_dossier(source_record, source_record["units"], coherence=False)
    library = dossier["library_evidence"]
    assert [row["name"] for row in library["declarations"]] == ["Library.Data"]
    assert library["audit_declaration_count"] == 2
    assert "library-declaration:Library.Proof" in dossier["evidence_catalog"]
    assert library["semantics"]["source_build_correspondence"] == "not_claimed"
    assert any("binary importer" in item for item in library["semantics"]["trust_assumptions"])


def test_serialized_mcp_pages_are_bounded_and_reconstruct_unicode_exactly(tmp_path):
    tools = SessionTools(
        role="reviewer",
        project_root=tmp_path,
        workspace=tmp_path / "tools",
        context={},
        allow_internet=False,
    )
    bridge = EvidenceBridge(toolset=tools, context={}, callbacks={}, directory=tmp_path / "bridge")
    original = {"text": ('漢字😀"\\\n' * 3000) + "final required qualifier"}
    bridge.documents = {"dense": original}
    with bridge:
        offset = 0
        chunks = []
        while offset is not None:
            response = bridge.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "read_evidence",
                        "arguments": {
                            "id": "dense",
                            "offset": offset,
                        },
                    },
                }
            )
            result = response["result"]
            assert not result["isError"]
            serialized = result["content"][0]["text"]
            assert len(serialized.encode()) <= 16000
            page = json.loads(serialized)
            chunks.append(page["text"])
            assert page["next_offset"] is None or page["next_offset"] > offset
            offset = page["next_offset"]
        assert json.loads("".join(chunks)) == original
        tiny = bridge.call("read_evidence", {"id": "dense", "offset": 0, "max_characters": 17})
        assert len(tiny["text"]) == 17 and tiny["next_offset"] == 17


def test_unavailable_evidence_is_not_a_cached_semantic_rejection(
    source_record, tmp_path, monkeypatch
):
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch)

    def unavailable(request):
        request.on_started()
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        value = json.loads(
            answer(
                assignment["dossier"],
                assignment["revision_id"],
                assignment["dossier_id"],
                decision="BLOCK",
            )
        )
        value.update(
            coherence_decision="UNAVAILABLE", feedback="Evidence read failed; retry delivery."
        )
        return HarnessResult(status="completed", final_response=json.dumps(value))

    monkeypatch.setattr(review, "run_harness", unavailable)
    result = review.review_revision(source_record, **kwargs)
    assert not result["accepted"] and result["status"] == "evidence_unavailable"
    assert not list((Path(source_record["_directory"]) / "review-cache").glob("*.json"))


def test_large_phase_response_ids_are_explicit_and_not_context_ids(
    source_record, tmp_path, monkeypatch
):
    source_record["units"] = [{**source_record["units"][0], "id": f"u-{i}"} for i in range(9)]
    calls, kwargs = _review_transport(source_record, tmp_path, monkeypatch)
    assert review.review_revision(source_record, **kwargs)["accepted"]
    for assignment in calls:
        dossier = assignment["dossier"]
        expected = dossier["expected_response"]
        assert expected["unit_ids"] == [u["id"] for u in dossier["units"]]
        assert expected["region_ids"] == [r["id"] for r in dossier["regions"]]
        if dossier["phase"] == "coverage":
            assert expected["unit_ids"] == expected["source_ids"] == []
    final = calls[-1]["dossier"]
    assert final["project_unit_ids"] and final["project_region_ids"]
    assert final["expected_response"] == {"unit_ids": [], "source_ids": [], "region_ids": []}
