"""Resolve formalization check requests in the author's private project copy."""

from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process, session_tools
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.mark.parametrize("workflow", ["formalize", "prove"])
def test_relative_check_file_uses_workflow_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: str
) -> None:
    scratch = tmp_path / ".leanflow-job"
    scratch.mkdir()
    checked = []

    def check(**kwargs):
        checked.append(kwargs["file"])
        return {"success": True, "ok": True}

    monkeypatch.setattr(check_process, "check_scratch", check)
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=scratch,
        context={"workflow": workflow},
    )
    result = tools.invoke("lean_check", {"file": "Main/Formalization/Foundations.lean"})
    assert result["success"]
    expected = tmp_path if workflow == "formalize" else scratch
    assert checked == [expected / "Main/Formalization/Foundations.lean"]
    schema = next(
        row["function"] for row in tools.schemas() if row["function"]["name"] == "lean_check"
    )
    assert ("Relative file paths start at project_root" in schema["description"]) == (
        workflow == "formalize"
    )


def test_formalization_check_preserves_private_and_escape_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / ".leanflow-job"
    scratch.mkdir()
    monkeypatch.setattr(
        check_process, "check_scratch", lambda **kwargs: pytest.fail("outside check authority")
    )
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=scratch,
        context={"workflow": "formalize"},
    )
    for file in ("../Outside.lean", ".leanflow/native-workflows/other/Secret.lean"):
        assert not tools.invoke("lean_check", {"file": file})["success"]


@pytest.mark.parametrize("workflow", ["formalize", "prove"])
@pytest.mark.parametrize(
    ("explicit", "remaining", "expected"),
    [
        (None, None, None),
        (None, 480.0, 480.0),
        (180.0, None, 180.0),
        (180.0, 480.0, 180.0),
        (900.0, 90.0, 90.0),
        (0.0, 480.0, 0.0),
    ],
)
def test_feedback_preserves_campaign_deadline_and_explicit_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    explicit: float | None,
    remaining: float | None,
    expected: float | None,
) -> None:
    """Do not impose a legacy cold-import timeout on native formalization feedback."""
    received: list[float | None] = []

    def check(**kwargs: Any) -> dict[str, bool]:
        received.append(kwargs["timeout_s"])
        return {"success": True, "ok": True}

    monkeypatch.setattr(check_process, "check_scratch", check)
    monkeypatch.setattr(session_tools, "remaining_seconds", lambda deadline: remaining)
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=tmp_path / ".leanflow-job",
        context={"workflow": workflow},
        timeout_s=explicit,
        deadline=1000.0 if remaining is not None else None,
    )
    assert tools.invoke("lean_check", {"file": "Foundations.lean"})["success"]
    assert received == [expected]
