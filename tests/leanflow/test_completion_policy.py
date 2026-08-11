from __future__ import annotations

from leanflow_cli.native import completion_policy


def test_focused_completion_always_uses_exact_file_gate() -> None:
    assert completion_policy.focused_verification_mode(full_project=False) == "file_exact"
    assert completion_policy.focused_verification_mode(full_project=True) == "project"


def test_file_scoped_prove_does_not_require_project_build() -> None:
    assert (
        completion_policy.requires_project_verification(
            focused_ok=True,
            project_sorry_count=0,
            declaration_scope="file",
            workflow_kind="prove",
        )
        is False
    )


def test_formalization_and_project_scope_keep_project_gate() -> None:
    assert completion_policy.requires_project_verification(
        focused_ok=True,
        project_sorry_count=0,
        declaration_scope="file",
        workflow_kind="formalize",
    )
    assert completion_policy.requires_project_verification(
        focused_ok=True,
        project_sorry_count=0,
        declaration_scope="project",
        workflow_kind="prove",
    )


def test_research_quiesces_only_after_final_file_target() -> None:
    assert completion_policy.should_quiesce_research_after_target(
        target_gate_accepted=True,
        declaration_scope="file",
        source_sorry_count=0,
    )
    assert not completion_policy.should_quiesce_research_after_target(
        target_gate_accepted=True,
        declaration_scope="file",
        source_sorry_count=1,
    )
    assert not completion_policy.should_quiesce_research_after_target(
        target_gate_accepted=True,
        declaration_scope="project",
        source_sorry_count=0,
    )


def test_unnecessary_simpa_does_not_spend_another_model_turn() -> None:
    assert not completion_policy.warning_cleanup_worth_model_turn(
        "line 184: try 'simp' instead of 'simpa'"
    )
    assert completion_policy.warning_cleanup_worth_model_turn("line 12: unused variable `h`")
