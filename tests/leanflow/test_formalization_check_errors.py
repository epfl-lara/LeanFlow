"""Separate operational checking failures from author-correctable Lean rejections."""

from __future__ import annotations

import copy
import time
from pathlib import Path

import pytest

from leanflow_cli.formalization import dependency_snapshot, proposal
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    load_document,
    load_receipt,
)
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot


@pytest.mark.parametrize(
    ("stage", "failure"),
    [
        (stage, failure)
        for stage in ("layout", "build", "dependency")
        for failure in [
            {"success": False, "error_code": "check_timeout", "error": "Timed out"},
            {"success": False, "error_code": "check_cancelled", "error": "Cancelled"},
            {
                "success": False,
                "error_code": "isolation_unavailable",
                "error": "Sandbox unavailable",
            },
            {"success": False, "error_code": "check_setup_failed", "error": "Compiler unavailable"},
            {"success": False, "returncode": 1, "stdout": "Main.lean: type mismatch"},
        ]
    ]
    + [("mapping", {"error": "Conflicting library mapping"})],
)
def test_process_failure_kind_survives_proposal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, failure: dict
) -> None:
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    baseline = source_files(tmp_path)
    snapshot = create_snapshot(tmp_path, tmp_path / ".leanflow/author")
    (snapshot / ".leanflow-job").mkdir()
    (snapshot / ".leanflow-job/formalization-proposal.json").write_text("{}")
    candidate = copy.deepcopy(record)
    candidate["units"] = [{"declarations": [{"name": "True.intro", "imports": ["Init"]}]}]
    monkeypatch.setattr(proposal, "apply_proposal", lambda *args: copy.deepcopy(candidate))
    layout = {
        "accepted": True,
        "aggregator": "Main/Document.lean",
        "owned_prefix": "Main/Document",
        "build_dir": ".lake/build",
    }
    monkeypatch.setattr(
        dependency_snapshot,
        "inspect_layout",
        lambda root: {
            "accepted": True,
            "build_dir": ".lake/build",
            "lean_lib_dir": ".lake/build/lib/lean",
        },
    )
    rejected = {"accepted": False, "error": "Checking failed", "details": failure}
    monkeypatch.setattr(
        proposal, "resolve_layout", lambda *args: rejected if stage == "layout" else layout
    )
    monkeypatch.setattr(proposal, "environment_identity", lambda root: "environment")
    monkeypatch.setattr(proposal, "lean_environment_generation", lambda root: "generation")
    monkeypatch.setattr(
        proposal,
        "build_project",
        lambda *args, **kwargs: failure if stage == "build" else {"success": True},
    )
    monkeypatch.setattr(proposal, "inspect_dependency", lambda *args, **kwargs: rejected)
    if stage == "mapping":
        monkeypatch.setattr(
            proposal, "inspect_dependency", lambda *args, **kwargs: {"accepted": True}
        )

        def mapping_error(*args, **kwargs):
            raise ValueError(failure["error"])

        monkeypatch.setattr(proposal, "merge_reused_declarations", mapping_error)
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=snapshot,
        baseline=baseline,
        directory=tmp_path / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(tmp_path, record["document_id"]),
        allowed_axioms=(),
    )
    operational = "error_code" in failure or stage == "layout"
    expected = "check_error" if operational else "check_rejected"
    assert not result["accepted"] and result["status"] == expected
    receipt = load_receipt(load_document(tmp_path, record["document_id"]))
    assert receipt and receipt["status"] == expected
    if "error_code" in failure:
        assert result["error_code"] == failure["error_code"]
        assert receipt["checks"]["error"] == failure["error"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_expired_or_cancelled_check_stops_before_compiler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    monkeypatch.setattr(
        proposal, "resolve_layout", lambda *args: pytest.fail("Must stop before compiler")
    )
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path,
        baseline=source_files(tmp_path),
        directory=tmp_path / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(tmp_path, record["document_id"]),
        allowed_axioms=(),
        deadline=None if cancelled else time.monotonic() - 1,
        cancelled=lambda: cancelled,
    )
    assert result["status"] == "check_error"
    assert result["error_code"] == ("check_cancelled" if cancelled else "check_timeout")
