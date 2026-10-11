"""Keep terminal launch and provider causes visible in top-level run evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover import observer


@pytest.mark.parametrize("cause", ["", "Authentication failed: sk-abcdefghijklm123456"])
def test_finish_publishes_redacted_cause_to_run_log_and_exit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    logs: list[str] = []
    events: list[dict] = []
    monkeypatch.setattr(observer.RunObserver, "publish", lambda *args: None)
    monkeypatch.setattr(observer, "append_workflow_run_log", logs.append)
    monkeypatch.setattr(
        observer, "append_workflow_activity", lambda *args, **kwargs: events.append(kwargs)
    )
    # The observer normally binds these process variables to its run.
    for key in (
        "LEANFLOW_PROJECT_ROOT",
        "LEANFLOW_WORKFLOW_RUN_ID",
        "LEANFLOW_NATIVE_WORKFLOW_KIND",
    ):
        monkeypatch.setenv(key, "")
    observer.RunObserver(tmp_path, "test-failure").finish(
        {"status": "provider_error", "error": cause}
    )
    if cause:
        assert "Authentication failed" in logs[0]
        assert "Authentication failed" in events[0]["error"]
        assert "sk-abcdefghijklm123456" not in "".join(logs) + str(events)
    else:
        assert len(logs) == 1
        assert "error" not in events[0]
    assert logs[-1] == "Prover finished: provider_error (exit 1)\n"
