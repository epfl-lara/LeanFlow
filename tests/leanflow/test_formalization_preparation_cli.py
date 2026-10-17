"""Preserve explicit preparation cancellation, output, and readiness boundaries."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.cli import formalization_preparation as command
from leanflow_cli.cli.formalization_command import (
    handle_formalization,
    register_formalization_parser,
)
from leanflow_cli.formalization.records import create_document, load_document
from leanflow_cli.workflows.project_setup.process import SetupCancelled


@pytest.mark.parametrize("outcome", ["prepared", "cancelled", "failed"])
def test_prepare_cli_restores_signals_and_keeps_progress_off_json_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    """Keep cancellation and bounded setup options intact after releasing document ownership."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    calls = []

    def prepare(root: Path, selected: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(selected["revision_id"])
        assert root == tmp_path and selected["revision_id"] == record["revision_id"]
        assert kwargs["plan"]["required_imports"] == ["Init"]
        assert kwargs["plan"]["source_revision_id"] == record["revision_id"]
        assert kwargs["timeout_s"] == 27 and kwargs["network_allowed"] is False
        assert not kwargs["cancel"].is_set()
        for signum, original in previous.items():
            assert signal.getsignal(signum) != original
        kwargs["progress"]({"stage": "checking", "message": "Checking the selected plan"})
        if outcome == "cancelled":
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
            assert kwargs["cancel"].is_set()
            raise SetupCancelled()
        if outcome == "failed":
            raise ValueError("Preparation fixture failed")
        return {"status": "prepared", "accepted": True}

    monkeypatch.setattr(command, "prepare_document_project", prepare)
    parser = argparse.ArgumentParser()
    register_formalization_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "formalization",
            "--project",
            str(tmp_path),
            "prepare",
            record["document_id"],
            "--expected-revision",
            record["revision_id"],
            "--preset",
            "lean",
            "--plan-json",
            '{"required_imports":["Init"]}',
            "--timeout-s",
            "27",
            "--offline",
            "--json",
        ]
    )
    assert handle_formalization(args) == (0 if outcome == "prepared" else 1)
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert json.loads(output.err) == {
        "type": "preparationProgress",
        "stage": "checking",
        "message": "Checking the selected plan",
    }
    if outcome == "prepared":
        assert result["revision_id"] == record["revision_id"]
        assert result["preparation_result"] == {"status": "prepared", "accepted": True}
        assert result["can_prove"] is False
    else:
        assert result["success"] is False
        assert result["error"] == (
            "Project preparation was cancelled"
            if outcome == "cancelled"
            else "Preparation fixture failed"
        )
    assert calls == [record["revision_id"]]
    assert {signum: signal.getsignal(signum) for signum in previous} == previous
    assert load_document(tmp_path, record["document_id"])["revision_id"] == record["revision_id"]
