"""Pin explicit reviewer routing and revision-scoped prover launches."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli import workflow
from leanflow_cli.cli.cli_handlers import workflow_run_help_text
from leanflow_cli.formalization import launch_options


@pytest.mark.parametrize(
    "command",
    [
        "/prove --from-formalization doc",
        "/prove --formalization-revision rev-1",
        "/prove Main.lean --from-formalization doc --formalization-revision rev-1",
        "/formalize x.tex --from-formalization doc --formalization-revision rev-1",
        "/prove Main.lean --review-model other",
        "/draft Main.lean --correction-rounds 1",
        "/formalize x.tex --correction-rounds 9",
        "/formalize x.tex --correction-rounds -1",
        "/formalize x.tex --correction-rounds no",
        "/formalize x.tex --review-effort impossible",
        "/formalize x.tex --review-model --review-provider claude",
    ],
)
def test_invalid_formalization_options_reject_before_launch(command: str) -> None:
    with pytest.raises(ValueError):
        workflow.parse_workflow_command(command)


def test_review_options_are_removed_from_source_path() -> None:
    spec = workflow.parse_workflow_command(
        "/formalize paper.tex --review-provider claude --review-model opus --review-effort max --correction-rounds 2"
    )
    assert spec.workflow_args == "paper.tex"
    assert spec.review_provider == "claude" and spec.review_model == "opus"
    assert spec.review_effort == "max" and spec.correction_rounds == 2


def test_different_reviewer_backend_keeps_its_native_defaults(monkeypatch) -> None:
    for suffix in ("PROVIDER", "MODEL", "EFFORT"):
        monkeypatch.delenv("LEANFLOW_FORMALIZATION_REVIEW_" + suffix, raising=False)
    calls = []
    monkeypatch.setattr(
        launch_options,
        "resolve_runtime_provider",
        lambda **kwargs: calls.append(kwargs)
        or {"model": "author-config-model", "reasoning_effort": "author-config-effort"},
    )
    author = {"provider": "codex", "model": "codex-model", "reasoning_effort": "xhigh"}
    result = launch_options.reviewer_settings(author, provider="claude")
    assert result["LEANFLOW_FORMALIZATION_REVIEW_MODEL"] == ""
    assert result["LEANFLOW_FORMALIZATION_REVIEW_EFFORT"] == ""
    assert calls == [{"requested": "claude"}]
    result = launch_options.reviewer_settings(author)
    assert result["LEANFLOW_FORMALIZATION_REVIEW_MODEL"] == "codex-model"
    assert result["LEANFLOW_FORMALIZATION_REVIEW_EFFORT"] == "xhigh"
    assert (
        launch_options.reviewer_settings(author, effort="auto")[
            "LEANFLOW_FORMALIZATION_REVIEW_EFFORT"
        ]
        == ""
    )


def test_handoff_preview_sets_exact_path_and_clears_active_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Other.lean")
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda _: type("Project", (), {"root": tmp_path, "label": "Test", "lean_root": tmp_path})(),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "codex", "model": "", "reasoning_effort": ""},
    )
    plan = workflow.resolve_workflow_request(
        "/prove --from-formalization doc --formalization-revision rev-123",
        active_cwd=tmp_path,
        preview=True,
    )
    assert plan.child_env["LEANFLOW_FORMALIZATION_HANDOFF"] == str(
        tmp_path / "formalization/doc/handoff.json"
    )
    assert plan.child_env["LEANFLOW_FORMALIZATION_REVISION"] == "rev-123"
    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == ""
    assert workflow.describe_launch_plan(plan)["formalization_revision"] == "rev-123"


def test_review_preview_resolves_options_without_intake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        workflow,
        "discover_leanflow_project",
        lambda _: type("Project", (), {"root": tmp_path, "label": "Test", "lean_root": tmp_path})(),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "codex", "model": "author", "reasoning_effort": "xhigh"},
    )
    monkeypatch.setattr(
        launch_options, "resolve_runtime_provider", lambda **kwargs: {"provider": "claude"}
    )
    monkeypatch.setattr(
        workflow,
        "prepare_formalization_document_context",
        lambda **kwargs: pytest.fail("Preview cannot freeze intake"),
    )
    plan = workflow.resolve_workflow_request(
        "/formalize paper.tex --review-provider claude --review-model opus --review-effort max --correction-rounds 1",
        active_cwd=tmp_path,
        preview=True,
    )
    assert plan.child_env["LEANFLOW_FORMALIZATION_REVIEW_PROVIDER"] == "claude"
    assert plan.child_env["LEANFLOW_FORMALIZATION_REVIEW_MODEL"] == "opus"
    assert plan.child_env["LEANFLOW_FORMALIZATION_REVIEW_EFFORT"] == "max"
    assert workflow.describe_launch_plan(plan)["correction_rounds"] == "1"
    assert not (tmp_path / ".leanflow").exists()


def test_help_explains_review_and_handoff_options() -> None:
    assert "--review-provider" in workflow_run_help_text("formalize")
    assert "--formalization-revision" in workflow_run_help_text("prove")
