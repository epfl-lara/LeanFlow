"""Keep cancelled page rendering distinct from unavailable or timed-out evidence."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from leanflow_cli.formalization import review, source_visual_process
from leanflow_cli.workflows.project_setup.process import SetupCancelled
from tests.leanflow.test_formalization_review_visual_gate import _paper, _transport


@pytest.mark.parametrize("reason", ["cancelled", "campaign_deadline", "page_deadline"])
def test_renderer_interruption_has_a_meaningful_typed_reason(tmp_path, monkeypatch, reason):
    interrupted = False
    deadline = time.monotonic() + 3600 if reason == "campaign_deadline" else None

    def run(*args, **kwargs):
        nonlocal interrupted
        interrupted = True
        raise SetupCancelled()

    if reason == "campaign_deadline":
        before = time.monotonic()
        monkeypatch.setattr(
            source_visual_process,
            "time",
            SimpleNamespace(monotonic=lambda: before + (7200 if interrupted else 0)),
        )
    monkeypatch.setattr(source_visual_process, "run_command", run)
    expected = (
        InterruptedError if reason == "cancelled" else TimeoutError if deadline else ValueError
    )
    pattern = "cancelled" if reason == "cancelled" else "deadline" if deadline else "render limit"
    with pytest.raises(expected, match=pattern):
        source_visual_process.render_page(
            tmp_path / "source.pdf",
            tmp_path / "image",
            0,
            {"executable": "unused-renderer"},
            deadline=deadline,
            cancelled=lambda: interrupted and reason == "cancelled",
        )


def test_user_cancellation_during_visual_review_never_becomes_missing_evidence(
    tmp_path, monkeypatch
):
    record = _paper(tmp_path)
    calls, kwargs, _, _ = _transport(record, tmp_path, monkeypatch, mode="unused")

    def interrupted(*args, **kwargs):
        raise InterruptedError("Source page rendering was cancelled")

    monkeypatch.setattr(review, "attach_visual_evidence", interrupted)
    result = review.review_revision(record, **kwargs)
    assert result["status"] == "cancelled" and not result["accepted"] and not calls
    assert "cancelled" in result["feedback"]


def test_campaign_deadline_during_visual_review_remains_a_campaign_deadline(tmp_path, monkeypatch):
    record = _paper(tmp_path)
    calls, kwargs, _, _ = _transport(record, tmp_path, monkeypatch, mode="unused")

    def expired(*args, **kwargs):
        raise TimeoutError("Source page rendering exceeded the campaign deadline")

    monkeypatch.setattr(review, "attach_visual_evidence", expired)
    result = review.review_revision(record, deadline=time.monotonic() - 1, **kwargs)
    assert result["status"] == "deadline_exceeded" and not result["accepted"] and not calls
    assert "deadline" in result["feedback"]


def test_expired_zero_deadline_never_starts_the_renderer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        source_visual_process,
        "run_command",
        lambda *a, **kw: pytest.fail("Expired rendering must not start a process"),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        source_visual_process.render_page(
            tmp_path / "source.pdf",
            tmp_path / "image",
            0,
            {"executable": "unused-renderer"},
            deadline=0.0,
            cancelled=None,
        )
