"""Advertise immutable evidence reads without approving execution, edits, or arbitrary native servers."""

import pytest

from leanflow_cli.formalization.review_transport import EvidenceBridge
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.mark.parametrize("visual", [False, True])
def test_frozen_evidence_tools_only_advertise_bounded_readonly_capabilities(visual):
    bridge = object.__new__(EvidenceBridge)
    bridge.images = {"page-image": object()} if visual else {}
    tools = bridge.schemas()
    assert {row["name"] for row in tools} == {"get_assignment", "read_evidence"} | (
        {"read_source_image"} if visual else set()
    )
    assert all(
        row["annotations"]
        == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
        for row in tools
    )


def test_author_bridge_does_not_relabel_mutating_or_execution_tools_readonly(tmp_path):
    bridge = object.__new__(NativeBridge)
    bridge.callbacks = {"submit_files": lambda *_: {}}
    bridge.guidance = None
    bridge.toolset = SessionTools(
        role="prover", project_root=tmp_path, workspace=tmp_path, context={"workflow": "formalize"}
    )
    tools = bridge.schemas()
    readonly = {
        row["name"] for row in tools if row.get("annotations", {}).get("readOnlyHint") is True
    }
    assert readonly == {"get_assignment"}
    for name in ("start_command", "start_lean_check", "cancel_operation", "submit_files"):
        row = next(row for row in tools if row["name"] == name)
        assert not row.get("annotations", {}).get("readOnlyHint", False)
