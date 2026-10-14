"""Treat explicit revision selection as revoking live author authority without changing evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import proposal, publish
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_lock,
    load_document,
    load_receipt,
    new_revision,
    save_receipt,
    save_revision,
)
from leanflow_cli.formalization.service import act_document
from leanflow_cli.native import harness_workflow
from leanflow_cli.native.harness_artifacts import source_files


@pytest.fixture
def document(tmp_path: Path) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    return create_document(tmp_path, source)


@pytest.mark.parametrize("choice", ["base", "older_candidate", "current_candidate"])
def test_explicit_selection_revokes_live_author_even_when_revision_id_matches(
    document: dict, tmp_path: Path, choice: str
) -> None:
    token = current_selection_token(tmp_path, document["document_id"])
    candidates = []
    for number in range(2):
        candidate = new_revision(document)
        candidate.update(author_base_revision=document["revision_id"], submission_id=str(number))
        candidates.append(save_revision(Path(document["_directory"]), candidate))
    assert current_selection_token(tmp_path, document["document_id"]) == token
    selected = {
        "base": document,
        "older_candidate": candidates[0],
        "current_candidate": candidates[1],
    }[choice]
    accepted_receipt = save_receipt(
        selected, {"revision_id": selected["revision_id"], "status": "accepted_staged"}
    )
    before = set((Path(document["_directory"]) / "revisions").iterdir())
    viewed = act_document(
        tmp_path,
        document["document_id"],
        candidates[-1]["revision_id"],
        {"type": "select_revision", "revision_id": selected["revision_id"]},
    )
    assert viewed["revision_id"] == selected["revision_id"]
    assert current_selection_token(tmp_path, document["document_id"]) != token
    assert load_receipt(load_document(tmp_path, document["document_id"])) == accepted_receipt
    checked = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path / "author",
        baseline={},
        directory=tmp_path / ".leanflow/check",
        document_id=document["document_id"],
        base_revision_id=document["revision_id"],
        selection_token=token,
        allowed_axioms=(),
    )
    assert checked["accepted"] is False and checked["error_code"] == "selection_changed"
    assert (
        load_document(tmp_path, document["document_id"])["revision_id"] == selected["revision_id"]
    )
    assert load_receipt(selected) == accepted_receipt
    assert set((Path(document["_directory"]) / "revisions").iterdir()) == before


def test_same_candidate_reselection_blocks_previously_checked_publication(
    document: dict, tmp_path: Path, monkeypatch
) -> None:
    token = current_selection_token(tmp_path, document["document_id"])
    act_document(
        tmp_path,
        document["document_id"],
        document["revision_id"],
        {"type": "select_revision", "revision_id": document["revision_id"]},
    )
    monkeypatch.setattr(
        publish,
        "build_handoff",
        lambda *a, **k: pytest.fail("Revoked authority must stop before preparing publication"),
    )
    with pytest.raises(ValueError, match="selection changed"):
        publish.publish_checked(
            tmp_path,
            {},
            {},
            {"accepted": True, "record": document, "selection_token": token},
            {"accepted": True},
            tmp_path / ".leanflow/publication",
        )
    assert load_receipt(document) is None
    assert not (tmp_path / "formalization").exists()


def test_new_campaign_can_use_explicit_selection_without_cloning_revision(
    document: dict, tmp_path: Path
) -> None:
    selected = act_document(
        tmp_path,
        document["document_id"],
        document["revision_id"],
        {"type": "select_revision", "revision_id": document["revision_id"]},
    )
    token = current_selection_token(tmp_path, document["document_id"], initialize=True)
    assert selected["revision_id"] == document["revision_id"]
    with document_lock(tmp_path, document["document_id"]):
        assert current_selection_token(tmp_path, document["document_id"]) == token
    # Reaching the actual proposal read proves the new authority was admitted.
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path / "author",
        baseline={},
        directory=tmp_path / ".leanflow/check",
        document_id=document["document_id"],
        base_revision_id=document["revision_id"],
        selection_token=token,
        allowed_axioms=(),
    )
    assert result["accepted"] is False and result.get("error_code") != "selection_changed"
    assert (
        load_document(tmp_path, document["document_id"])["revision_id"] != document["revision_id"]
    )
    assert current_selection_token(tmp_path, document["document_id"]) == token


@pytest.mark.parametrize("missing", [False, True])
def test_resume_cannot_recapture_revoked_or_missing_campaign_authority(
    document: dict, tmp_path: Path, monkeypatch, missing: bool
) -> None:
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *args: {}
    )
    token = current_selection_token(tmp_path, document["document_id"])
    state = {
        "version": 2,
        "backend": "codex",
        "workflow": "formalize",
        "baseline": {name: data.hex() for name, data in source_files(tmp_path).items()},
        **({} if missing else {"formalization_selection_token": token}),
    }
    directory = tmp_path / ".leanflow/native-workflows/campaign"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(state))
    act_document(
        tmp_path,
        document["document_id"],
        document["revision_id"],
        {"type": "select_revision", "revision_id": document["revision_id"]},
    )
    monkeypatch.setattr(
        harness_workflow,
        "run_harness",
        lambda *a, **k: pytest.fail("Revoked resume must not start inference"),
    )
    with pytest.raises(ValueError, match="selection changed"):
        harness_workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_NATIVE_PROVIDER": "codex",
                "LEANFLOW_NATIVE_RESUME_RUN_ID": "campaign",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": document["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": document["revision_id"],
            }
        )
    assert json.loads((directory / "state.json").read_text()) == state
