"""Require real frozen companion-image delivery before fresh or cached source review can pass."""

from __future__ import annotations

import base64
import json
import shutil
import time
from pathlib import Path
from threading import Event
from urllib.request import Request, urlopen

import pytest

from leanflow_cli.formalization import review
from leanflow_cli.formalization.evidence import digest, put_blob
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.source_visual import required_page_images
from leanflow_cli.formalization.source_visual_receipt import validated_visual_evidence
from tests.leanflow import test_formalization_revision_review as review_helpers
from tests.leanflow.test_source_inventory import _pdf

_BASE_ANSWER = review_helpers.answer
pytestmark = pytest.mark.skipif(
    not shutil.which("pdftoppm"), reason="Real Poppler rendering unavailable"
)


def _paper(tmp_path: Path, *, pages: int = 2) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}\label{thm:t}True.\end{theorem}" + "\n")
    pdf = tmp_path / "paper.pdf"
    _pdf(pdf, [f"Theorem True. Physical source page {index + 1}." for index in range(pages)])
    record = create_document(tmp_path, source, companion_pdf=pdf)
    item = record["inventory"]["items"][0]
    file = "Main/Formalization/Doc/Statements.lean"
    record.update(
        units=[
            {
                "id": "unit-t",
                "required_source_ids": [item["id"]],
                "declarations": [{"file": file, "name": "t"}],
                "representation_note": "The source assertion is True.",
                "depends_on_units": [],
            }
        ],
        layout={"library": "Main"},
        environment_id="fixture-env",
        lean_files={
            file: put_blob(Path(record["_directory"]) / "blobs", b"theorem t : True := by sorry\n")
        },
        checks={"accepted": True, "declarations": [{"name": "t", "type": "True"}]},
    )
    return record


def _transport(record, tmp_path, monkeypatch, *, mode: str):
    bridges = []
    delivered = set()
    current = {"mode": mode}
    bridge_type = review.EvidenceBridge

    def capture(**kwargs):
        bridge = bridge_type(**kwargs)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(review, "EvidenceBridge", capture)
    calls, kwargs = review_helpers._review_transport(record, tmp_path, monkeypatch)

    def answer(dossier, revision, key, *, decision="PASS"):
        bridge = bridges[-1]
        for image_id in sorted(required_page_images(dossier, bridge.documents)):
            if current["mode"] in {"image", "image_on_close"}:
                environment = next(iter(bridge.mcp_config()["mcpServers"].values()))["env"]
                payload = {
                    "jsonrpc": "2.0",
                    "id": image_id,
                    "method": "tools/call",
                    "params": {"name": "read_source_image", "arguments": {"id": image_id}},
                }
                with urlopen(
                    Request(
                        environment["LEANFLOW_BRIDGE_URL"],
                        data=json.dumps(payload).encode(),
                        headers={
                            "Authorization": "Bearer " + environment["LEANFLOW_BRIDGE_TOKEN"],
                            "Content-Type": "application/json",
                        },
                    ),
                    timeout=5,
                ) as connection:
                    response = json.loads(connection.read(6 * 1024**2 + 1))
                assert not response["result"]["isError"]
                content = response["result"]["content"]
                metadata = json.loads(content[0]["text"])
                assert content[1]["type"] == "image" and content[1]["mimeType"] == "image/png"
                assert digest(base64.b64decode(content[1]["data"])) == metadata["png_sha256"]
                if current["mode"] == "image":
                    deadline = time.monotonic() + 1
                    while image_id not in bridge.image_reads and time.monotonic() < deadline:
                        time.sleep(0.005)
                    assert bridge.image_reads[image_id] == metadata["png_sha256"]
                delivered.add(image_id)
            elif current["mode"] == "text":
                bridge.call("read_evidence", {"id": image_id, "offset": 0})
            elif current["mode"] == "call_only":
                # Reading raw bytes through an internal Python call is not an
                # actual image response delivered to the independent reviewer.
                assert bridge.call("read_source_image", {"id": image_id})["data"]
        return _BASE_ANSWER(
            dossier,
            revision,
            key,
            decision=(
                current.get("coherence_decision", decision)
                if dossier["phase"] == "coherence"
                else decision
            ),
        )

    monkeypatch.setattr(review_helpers, "answer", answer)
    return calls, kwargs, current, delivered


def test_real_multi_page_review_output_passes_portable_publication_validation(
    tmp_path, monkeypatch
):
    record = _paper(tmp_path, pages=7)
    _, kwargs, _, _ = _transport(record, tmp_path, monkeypatch, mode="image")
    outcome = review.review_revision(record, **kwargs)
    assert outcome["accepted"]
    assert [row["page_index"] for row in outcome["visual_evidence"]] == list(range(7))
    assert validated_visual_evidence(record, outcome) == outcome["visual_evidence"]


def test_delivery_completed_during_bridge_close_is_recorded_before_review_is_cached(
    tmp_path, monkeypatch
):
    original = review.EvidenceBridge

    class DelayedDelivery(original):
        def __init__(self, **kwargs):
            self.release_delivery = Event()
            super().__init__(**kwargs)

        def response_delivered(self, request, response):
            assert self.release_delivery.wait(5)
            super().response_delivered(request, response)

        def close(self):
            self.release_delivery.set()
            super().close()

    monkeypatch.setattr(review, "EvidenceBridge", DelayedDelivery)
    record = _paper(tmp_path)
    calls, kwargs, _, delivered = _transport(record, tmp_path, monkeypatch, mode="image_on_close")
    outcome = review.review_revision(record, **kwargs)
    assert outcome["accepted"] and len(calls) == 2 and len(delivered) == 2
    assert len(outcome["reviews"][-1]["image_reads"]) == 2
    assert validated_visual_evidence(record, outcome)
    assert review.review_revision(record, **kwargs)["accepted"] and len(calls) == 2


def test_unverified_bridge_close_cannot_persist_a_positive_review_cache(tmp_path, monkeypatch):
    from agent.runtime.harness.models import NativeCleanupUnverified

    original = review.EvidenceBridge

    class UnverifiedClose(original):
        def close(self):
            super().close()
            raise NativeCleanupUnverified("Injected uncertain review bridge closure")

    monkeypatch.setattr(review, "EvidenceBridge", UnverifiedClose)
    record = _paper(tmp_path)
    _, kwargs, _, _ = _transport(record, tmp_path, monkeypatch, mode="image")
    with pytest.raises(NativeCleanupUnverified, match="uncertain review bridge"):
        review.review_revision(record, **kwargs)
    assert not list((Path(record["_directory"]) / "review-cache").glob("*.json"))


def test_renderer_change_retains_semantic_rejection_without_new_paid_opinion(tmp_path, monkeypatch):
    from leanflow_cli.formalization import source_visual

    record = _paper(tmp_path)
    calls, kwargs, current, _ = _transport(record, tmp_path, monkeypatch, mode="image")
    current["coherence_decision"] = "BLOCK"
    first = review.review_revision(record, **kwargs)
    assert not first["accepted"] and first["status"] == "completed" and len(calls) == 2
    original = source_visual.renderer_identity()
    monkeypatch.setattr(
        source_visual,
        "renderer_identity",
        lambda: {**original, "protocol": "next-frozen-renderer-protocol"},
    )
    second = review.review_revision(record, **kwargs)
    assert not second["accepted"] and second["status"] == "completed"
    assert len(calls) == 3
    assert sum(call["dossier"]["phase"] == "coherence" for call in calls) == 1, (
        "Renderer changes cannot reroll an unchanged semantic rejection; "
        "positive translation reuse still requires the exact evidence catalog"
    )
    failed = second["reviews"][-1]
    assert failed["cache_reused"] and failed["visual_delivery_current"] is False
    assert failed["verdicts"] == first["reviews"][-1]["verdicts"]
    assert failed["feedback"] == first["reviews"][-1]["feedback"]
    assert set(failed["image_reads"]).isdisjoint(
        row["evidence_id"] for row in second["visual_evidence"]
    )


@pytest.mark.parametrize("mode", ["unused", "text", "call_only"])
def test_unused_or_metadata_only_images_cannot_pass_and_retry_can_deliver_real_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    record = _paper(tmp_path)
    calls, kwargs, current, delivered = _transport(record, tmp_path, monkeypatch, mode=mode)
    result = review.review_revision(record, **kwargs)
    assert not result["accepted"] and result["status"] == "evidence_unavailable"
    assert len(calls) == 2 and not delivered
    failed = result["reviews"][-1]
    assert len(failed["required_image_ids"]) == 2 and not failed["image_reads"]
    assert len(list((Path(record["_directory"]) / "review-cache").glob("*.json"))) == 1
    current["mode"] = "image"
    corrected = review.review_revision(record, **kwargs)
    assert corrected["accepted"] and len(calls) == 3 and len(delivered) == 2
    assert {row["evidence_id"] for row in corrected["visual_evidence"]} == delivered
    assert corrected["reviews"][-1]["image_reads"] == {
        row["evidence_id"]: row["png_sha256"] for row in corrected["visual_evidence"]
    }
    assert not any(page.get("transcription") for page in record["inventory"]["pages"])
    assert record["inventory"]["companion_pdf"]["correspondence_verified"] is False


@pytest.mark.parametrize("mutation", ["missing_reads", "wrong_png_hash", "missing_required_id"])
def test_cached_pass_requires_the_exact_delivered_page_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    record = _paper(tmp_path)
    calls, kwargs, _, delivered = _transport(record, tmp_path, monkeypatch, mode="image")
    first = review.review_revision(record, **kwargs)
    assert first["accepted"] and len(calls) == 2 and len(delivered) == 2
    outcome = first["reviews"][-1]
    path = Path(record["_directory"]) / "review-cache" / (outcome["cache_key"] + ".json")
    cached = json.loads(path.read_text())
    if mutation == "missing_reads":
        cached.pop("image_reads")
    elif mutation == "wrong_png_hash":
        cached["image_reads"][cached["required_image_ids"][0]] = "another-image"
    else:
        cached["required_image_ids"].pop()
    path.write_text(json.dumps(cached))
    second = review.review_revision(record, **kwargs)
    assert not second["accepted"] and second["status"] == "evidence_unavailable"
    assert len(calls) == 2, "Invalid cached image authority must not reroll a paid review"


def test_missing_renderer_fails_before_any_native_review(tmp_path, monkeypatch) -> None:
    record = _paper(tmp_path)
    calls, kwargs, _, _ = _transport(record, tmp_path, monkeypatch, mode="image")

    def unavailable(*args, **kwargs):
        raise ValueError("A frozen source renderer is unavailable")

    monkeypatch.setattr(review, "attach_visual_evidence", unavailable)
    result = review.review_revision(record, **kwargs)
    assert not result["accepted"] and result["status"] == "evidence_unavailable"
    assert not calls and not result["reviews"]
