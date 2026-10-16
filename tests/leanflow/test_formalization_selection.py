"""Treat explicit revision selection as revoking live author authority without changing evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import proposal, publish
from leanflow_cli.formalization.preparation_campaign import (
    admit_author,
    campaign_budget,
    campaign_lock,
    load_campaign,
    select_campaign,
)
from leanflow_cli.formalization.records import (
    assert_author_selection,
    create_document,
    current_selection_token,
    document_lock,
    load_document,
    load_receipt,
    new_revision,
    save_receipt,
    save_revision,
    selection_pointer,
    source_bytes,
)
from leanflow_cli.formalization.service import act_document
from leanflow_cli.native import harness_workflow
from leanflow_cli.native.harness_artifacts import source_files


@pytest.fixture
def document(tmp_path: Path) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    return create_document(tmp_path, source)


@pytest.mark.parametrize("submitting_run", ["author-A", "author-B", ""])
@pytest.mark.parametrize("legacy", [False, True])
def test_only_same_author_run_can_replace_its_selected_candidate(
    document: dict, tmp_path: Path, submitting_run: str, legacy: bool
) -> None:
    token = current_selection_token(tmp_path, document["document_id"])
    candidate = new_revision(document)
    candidate.update(
        author_base_revision=document["revision_id"],
        author_run_id="author-A",
        submission_id="first-attempt",
    )
    if not legacy:
        candidate["author_selection_token"] = token
    selected = save_revision(Path(document["_directory"]), candidate)
    before = set((Path(document["_directory"]) / "revisions").iterdir())
    checked = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path / "author",
        baseline={},
        directory=tmp_path / ".leanflow/check",
        document_id=document["document_id"],
        base_revision_id=document["revision_id"],
        selection_token=token,
        allowed_axioms=(),
        author_run_id=submitting_run,
    )
    current = load_document(tmp_path, document["document_id"])
    if submitting_run == "author-A":
        # The absent proposal rejects later, after a new run-bound attempt is retained.
        assert checked.get("error_code") != "selection_changed"
        assert current["revision_id"] != selected["revision_id"]
        assert current["author_run_id"] == "author-A"
    else:
        assert checked["error_code"] == "selection_changed"
        assert current["revision_id"] == selected["revision_id"]
        assert set((Path(document["_directory"]) / "revisions").iterdir()) == before
    assert current_selection_token(tmp_path, document["document_id"]) == token


def test_source_edits_remove_author_run_ownership(document: dict) -> None:
    document.update(
        author_base_revision="base",
        author_run_id="author-A",
        author_selection_token="token",
        submission_id="first",
    )
    revised = new_revision(document)
    assert "author_run_id" not in revised and "author_base_revision" not in revised
    assert "author_selection_token" not in revised


@pytest.mark.parametrize("explicit_selection", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_foreign_candidate_cannot_be_adopted_as_a_new_base_without_user_selection(
    document: dict, tmp_path: Path, explicit_selection: bool, legacy: bool
) -> None:
    token = current_selection_token(tmp_path, document["document_id"])
    candidate = new_revision(document)
    candidate.update(
        author_base_revision=document["revision_id"],
        author_run_id="author-A",
        submission_id="first-attempt",
    )
    if not legacy:
        candidate["author_selection_token"] = token
    directory = Path(document["_directory"])
    selected = save_revision(directory, candidate)
    historical_receipt = save_receipt(
        selected,
        {
            "revision_id": selected["revision_id"],
            "status": "check_rejected",
            "checks": {"accepted": False},
            "reviews": [{"accepted": False}],
        },
    )
    historical_bytes = {
        path: path.read_bytes()
        for category in ("blobs", "revisions", "receipts", "receipt-history")
        for path in (directory / category).glob("*")
        if path.is_file()
    }
    original_source = (tmp_path / "paper.tex").read_bytes()
    if explicit_selection:
        act_document(
            tmp_path,
            selected["document_id"],
            selected["revision_id"],
            {"type": "select_revision", "revision_id": selected["revision_id"]},
        )
        token = current_selection_token(tmp_path, document["document_id"])
    before = set((Path(document["_directory"]) / "revisions").iterdir())
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path / "author",
        baseline={},
        directory=tmp_path / ".leanflow/check",
        document_id=document["document_id"],
        base_revision_id=selected["revision_id"],
        selection_token=token,
        allowed_axioms=(),
        author_run_id="author-B",
    )
    current = load_document(tmp_path, document["document_id"])
    if explicit_selection:
        assert result.get("error_code") != "selection_changed"
        assert current["author_run_id"] == "author-B"
        assert current["author_selection_token"] == token
    else:
        assert result["error_code"] == "selection_changed"
        assert current["revision_id"] == selected["revision_id"]
        assert set((Path(document["_directory"]) / "revisions").iterdir()) == before
    assert load_receipt(selected) == historical_receipt
    assert all(path.read_bytes() == data for path, data in historical_bytes.items())
    assert source_bytes(selected, "paper.tex") == original_source
    assert (tmp_path / "paper.tex").read_bytes() == original_source


@pytest.mark.parametrize("selection", ["initialize", "another_revision"])
def test_legacy_candidate_requires_its_own_explicit_selection(
    document: dict, tmp_path: Path, selection: str
) -> None:
    directory = Path(document["_directory"])
    candidate = new_revision(document)
    candidate.update(
        author_base_revision=document["revision_id"],
        author_run_id="author-A",
        submission_id="legacy-attempt",
    )
    selected = save_revision(directory, candidate)
    if selection == "initialize":
        (directory / "current.json").write_text(
            json.dumps({"schema_version": 1, "revision_id": selected["revision_id"]})
        )
    else:
        act_document(
            tmp_path,
            document["document_id"],
            selected["revision_id"],
            {"type": "select_revision", "revision_id": document["revision_id"]},
        )
        save_revision(directory, selected)
    token = current_selection_token(tmp_path, document["document_id"], initialize=True)
    before = (directory / "current.json").read_bytes()
    with pytest.raises(ValueError, match="Another formalization campaign owns"):
        assert_author_selection(load_document(tmp_path, document["document_id"]), "author-B", token)
    assert current_selection_token(tmp_path, document["document_id"]) == token
    assert (directory / "current.json").read_bytes() == before


@pytest.mark.parametrize("binding", ["document_id", "revision_id", "selection_token"])
def test_legacy_candidate_explicit_selection_provenance_is_exact(
    document: dict, tmp_path: Path, binding: str
) -> None:
    directory = Path(document["_directory"])
    candidate = new_revision(document)
    candidate.update(author_base_revision=document["revision_id"], author_run_id="author-A")
    selected = save_revision(directory, candidate)
    pointer = selection_pointer(directory, selected["revision_id"], revoke_authority=True)
    pointer["explicit_selection"][binding] = "different-" + binding
    (directory / "current.json").write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="Another formalization campaign owns"):
        assert_author_selection(selected, "author-B", pointer["selection_token"])


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
    directory = tmp_path / ".leanflow/native-workflows/campaign"
    with campaign_lock(tmp_path, "campaign"):
        binding = select_campaign(
            tmp_path,
            document,
            run_id="campaign",
            limits={},
            backend="codex",
            selection_token=token,
            source_draft_only=False,
            effective_draft=False,
            resume=False,
            budget_path=directory / "limits.json",
        )
        admit_author(tmp_path, binding, resume=False, state_path=directory / "state.json")
        ledger = campaign_budget(tmp_path, binding)
    ledger_bytes = ledger.read_bytes()
    state = {
        "version": 2,
        "run_id": "campaign",
        "campaign_binding_id": binding["binding_id"],
        "backend": "codex",
        "workflow": "formalize",
        "baseline": {name: data.hex() for name, data in source_files(tmp_path).items()},
        **({} if missing else {"formalization_selection_token": token}),
    }
    (directory / "state.json").write_text(json.dumps(state))
    if not missing:
        act_document(
            tmp_path,
            document["document_id"],
            document["revision_id"],
            {"type": "select_revision", "revision_id": document["revision_id"]},
        )
        assert current_selection_token(tmp_path, document["document_id"]) != token
    else:
        assert current_selection_token(tmp_path, document["document_id"]) == token
    monkeypatch.setattr(
        harness_workflow,
        "run_harness",
        lambda *a, **k: pytest.fail("Revoked resume must not start inference"),
    )
    expected = "selection changed" if missing else "Campaign source or run identity changed"
    with pytest.raises(ValueError, match=expected):
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
    assert ledger.read_bytes() == ledger_bytes
    assert load_campaign(tmp_path, "campaign") == binding
