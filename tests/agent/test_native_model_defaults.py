"""Keep model-default lookup bounded, isolated, and distinct from model execution."""

import time

import pytest

from agent.runtime.harness import HarnessRequest, IsolationPolicy, model_defaults
from tests.leanflow.test_provider_models import FakeSession, native_row, rpc


@pytest.mark.parametrize("backend", ["codex", "lemex"])
def test_effort_uses_matching_model_in_native_catalog(monkeypatch, tmp_path, backend):
    session = FakeSession(
        [
            rpc(1, {}),
            rpc(2, {"data": [native_row("other")], "nextCursor": "next"}),
            rpc(2, {"data": [native_row("actual", defaultReasoningEffort="xhigh")]}),
        ]
    )
    monkeypatch.setattr(model_defaults, "MetadataSession", session)
    monkeypatch.setattr(model_defaults, "sandbox_command", lambda args, *a, **kw: args)
    request = HarnessRequest(backend, "proof", tmp_path)
    assert (
        model_defaults.reasoning_default(
            request, backend, {"KEY": "private"}, IsolationPolicy(tmp_path), "actual"
        )
        == "xhigh"
    )
    assert session.cwd == str(tmp_path) and session.environment == {"KEY": "private"}
    assert [r.get("method") for r in session.requests] == [
        "initialize",
        "initialized",
        "model/list",
        "model/list",
    ]
    assert all(r.get("method") not in {"thread/start", "turn/start"} for r in session.requests)
    assert session.closed


def test_missing_or_failed_metadata_stays_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(model_defaults, "sandbox_command", lambda args, *a, **kw: args)
    request = HarnessRequest("lemex", "proof", tmp_path)
    for response in [{"data": []}, {"data": [native_row("actual", defaultReasoningEffort=None)]}]:
        session = FakeSession([rpc(1, {}), rpc(2, response)])
        monkeypatch.setattr(model_defaults, "MetadataSession", session)
        assert (
            model_defaults.reasoning_default(
                request, "lemex", {}, IsolationPolicy(tmp_path), "actual"
            )
            == ""
        )
        assert session.closed
    monkeypatch.setattr(
        model_defaults,
        "sandbox_command",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")),
    )
    assert (
        model_defaults.reasoning_default(request, "lemex", {}, IsolationPolicy(tmp_path), "actual")
        == ""
    )


def test_cancelled_or_expired_campaign_never_starts_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(
        model_defaults, "MetadataSession", lambda *a, **kw: pytest.fail("unexpected process")
    )
    for request in [
        HarnessRequest("codex", "", tmp_path, deadline=time.monotonic() - 1),
        HarnessRequest("codex", "", tmp_path, cancelled=lambda: True),
    ]:
        assert (
            model_defaults.reasoning_default(
                request, "codex", {}, IsolationPolicy(tmp_path), "actual"
            )
            == ""
        )
