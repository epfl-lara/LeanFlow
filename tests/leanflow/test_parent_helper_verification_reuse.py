"""Characterize fail-closed reuse of an exact parent helper verification."""

from __future__ import annotations

import hashlib

import pytest

from leanflow_cli.native import parent_helper_verification_reuse
from leanflow_cli.workflows import (
    research_helper_candidate_priority as _leaf_research_helper_candidate_priority,
)
from tools.utilities.patch_parser import preview_v4a_update as _leaf_preview_v4a_update


def _sha256(text: str) -> str:
    """Return the exact UTF-8 source identity used by the test fixture."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ready_candidate(
    tmp_path,
    *,
    axioms: tuple[str, ...] = ("Classical.choice",),
    declaration_prefix: str = "",
    before: str = "theorem demo : True := by\n  sorry\n",
):
    """Stage one accepted parent helper check against the original source."""
    active = tmp_path / "Main.lean"
    declaration = declaration_prefix + "private lemma checked_family : True := by\n  trivial"
    active.write_text(before, encoding="utf-8")
    declaration_hash = _sha256(declaration)
    finding = {
        "job_id": "campaign.em-checked",
        "target_symbol": "demo",
        "active_file": str(active),
        "deliverable": {
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "active_file": str(active),
                    "anchor_target_symbol": "demo",
                    "declaration": declaration,
                    "declaration_sha256": declaration_hash,
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["checked_family"],
                    },
                }
            ],
        },
    }
    state = {
        "campaign_id": "campaign",
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": before.strip(),
        },
    }
    pending = _leaf_research_helper_candidate_priority.remember_from_findings(
        state,
        (finding,),
        campaign_id="campaign",
        target_symbol="demo",
        active_file=str(active),
    )
    assert pending is not None
    expected = parent_helper_verification_reuse.expected_integrated_source(
        before,
        pending,
    )
    assert expected
    ready = _leaf_research_helper_candidate_priority.mark_parent_recheck(
        state,
        candidate_id=pending.candidate_id,
        status="accepted",
        source_revision_sha256=_sha256(before),
        expected_integrated_source_revision_sha256=_sha256(expected),
        axiom_profile_axioms=axioms,
    )
    assert ready is not None and ready.ready
    return active, before, expected, declaration, state, ready


def test_scoped_option_parent_helper_has_authenticated_insertion_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Bind a parent check to the exact source image including its option wrapper."""
    monkeypatch.setattr(
        _leaf_research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )

    _active, _before, expected, declaration, _state, ready = _ready_candidate(
        tmp_path,
        declaration_prefix="set_option maxRecDepth 100000 in\n",
    )

    assert expected.startswith(declaration)
    assert ready.expected_integrated_source_revision_sha256 == _sha256(expected)
    assert _leaf_research_helper_candidate_priority.parent_recheck_evidence_authenticated(ready)


def test_exact_helper_patch_ignores_preserved_commented_declaration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Build the authenticated insertion beside a live target, not its failed-attempt comment."""
    monkeypatch.setattr(
        _leaf_research_helper_candidate_priority.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    before = (
        "-- LeanFlow failed attempt preserved.\n"
        "-- theorem demo : True := by\n"
        "--   sorry\n"
        "\n"
        "theorem demo : True := by\n"
        "  sorry\n"
    )
    active, _before, expected, _declaration, _state, ready = _ready_candidate(
        tmp_path,
        before=before,
    )

    patch = parent_helper_verification_reuse.exact_integrated_source_patch(
        before,
        ready,
        path=str(active),
    )
    preview, error = _leaf_preview_v4a_update(patch, before)

    assert patch
    assert error is None
    assert preview == expected
