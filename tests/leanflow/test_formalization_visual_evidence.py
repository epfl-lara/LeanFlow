"""Require source-bound page images and actual image delivery for paired PDF review."""

from __future__ import annotations

import base64
import copy
import json
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.review_dossier import evidence_documents
from leanflow_cli.formalization.review_transport import EvidenceBridge
from leanflow_cli.formalization.source_visual import attach_visual_evidence, required_page_images
from leanflow_cli.formalization.source_visual_receipt import validated_visual_evidence
from leanflow_cli.workflows.prover.session_tools import SessionTools
from tests.leanflow.test_source_inventory import _pdf


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="Real Poppler rendering unavailable")
def test_real_frozen_companion_pages_are_hash_bound_and_delivered_as_mcp_images(
    tmp_path, monkeypatch
):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    pdf = tmp_path / "paper.pdf"
    _pdf(pdf, ["Theorem True.", "Second physical page."])
    record = create_document(tmp_path, source, companion_pdf=pdf)
    record.update(checks={"declarations": []}, layout={}, units=[], environment_id="env")
    documents = evidence_documents(record)
    # A complete paper can have more pages than one bounded command history.
    monkeypatch.setattr(
        "leanflow_cli.workflows.project_setup.invocation_evidence.MAX_INVOCATIONS", 1
    )
    monkeypatch.chdir(tmp_path)
    images = attach_visual_evidence(record, documents, directory=Path("render"))
    assert len(images) == 2
    frozen_pdf = record["source_files"]["paper.pdf"]
    pdf.write_bytes(b"Changed live PDF after freezing")
    repeated = attach_visual_evidence(record, documents, directory=tmp_path / "render-again")
    assert {key: value.metadata for key, value in repeated.items()} == {
        key: value.metadata for key, value in images.items()
    }
    dossier = {"regions": record["inventory"]["regions"], "phase": "coverage"}
    assert required_page_images(dossier, documents) == set(images)
    tools = SessionTools(
        role="reviewer",
        project_root=tmp_path,
        workspace=tmp_path / "tools",
        context={},
        allow_internet=False,
    )
    with EvidenceBridge(
        toolset=tools, context={}, callbacks={}, directory=tmp_path / "bridge"
    ) as bridge:
        bridge.documents, bridge.images = documents, images
        assert not bridge.image_reads
        for image_id, frozen in images.items():
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_source_image", "arguments": {"id": image_id}},
            }
            prepared = bridge.dispatch(request)
            assert prepared["result"]["isError"] is False
            assert image_id not in bridge.image_reads
            http = urllib.request.Request(
                f"http://127.0.0.1:{bridge.server.server_port}/",
                data=json.dumps(request).encode(),
                headers={
                    "Authorization": "Bearer " + bridge.token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(http, timeout=3) as reply:
                response = json.loads(reply.read())
            content = response["result"]["content"]
            metadata = json.loads(content[0]["text"])
            assert metadata["pdf_sha256"] == frozen_pdf
            assert content[1]["type"] == "image" and content[1]["mimeType"] == "image/png"
            assert digest(base64.b64decode(content[1]["data"])) == metadata["png_sha256"]
            deadline = time.monotonic() + 1
            while image_id not in bridge.image_reads and time.monotonic() < deadline:
                time.sleep(0.001)
            assert bridge.image_reads[image_id] == frozen.metadata["png_sha256"]
        bad = bridge.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_source_image", "arguments": {"id": str(pdf)}},
            }
        )
        assert bad["result"]["isError"] is True
        reviewed = {
            "visual_evidence": [image.metadata for image in images.values()],
            "reviews": [
                {
                    "accepted": True,
                    "cleanup_verified": True,
                    "image_reads": dict(bridge.image_reads),
                }
            ],
        }
        assert validated_visual_evidence(record, reviewed) == reviewed["visual_evidence"]
        for mutation in ("missing_page", "missing_delivery", "wrong_hash", "wrong_index"):
            altered = copy.deepcopy(reviewed)
            if mutation == "missing_page":
                altered["visual_evidence"].pop()
            elif mutation == "missing_delivery":
                altered["reviews"][0]["image_reads"].pop(next(iter(images)))
            elif mutation == "wrong_hash":
                altered["visual_evidence"][0]["png_sha256"] = "f" * 64
            else:
                altered["visual_evidence"][0]["page_index"] = 1
            with pytest.raises(ValueError):
                validated_visual_evidence(record, altered)
    assert not any(page.get("transcription") for page in record["inventory"]["pages"])
    assert record["inventory"]["companion_pdf"]["correspondence_verified"] is False


def test_visual_delivery_is_not_required_for_tex_without_a_companion(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    assert attach_visual_evidence(record, {}, directory=tmp_path / "unused") == {}
    assert not (tmp_path / "unused").exists()
