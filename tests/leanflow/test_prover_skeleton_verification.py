"""Separate skeleton elaboration from strict proof acceptance."""

from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process, type_profile
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.verification import LeanVerifier


@pytest.mark.parametrize(
    "feedback,profile,expected",
    [
        ({}, {}, True),
        ({"success": False}, {}, False),
        ({"has_errors": True}, {}, False),
        ({"has_errors": None}, {}, False),
        ({"has_sorry": False}, {}, False),
        ({"timed_out": True}, {}, False),
        ({"error_code": "target_not_found"}, {}, False),
        ({"messages": [{"severity": "error", "message": "unknown identifier"}]}, {}, False),
        ({}, {"accepted": False}, False),
        ({}, {"sha256": "changed"}, False),
        ({}, {"axioms": ["sorryAx", "invented"]}, False),
    ],
)
def test_skeleton_gate_requires_elaboration_and_matching_kernel_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feedback: dict[str, Any],
    profile: dict[str, Any],
    expected: bool,
) -> None:
    """Accept the observed sorry-only response without promoting it to a proof."""
    source = tmp_path / "Candidate.lean"
    source.write_text("theorem goal : True := by sorry\n")
    node = Node(
        id="goal",
        name="goal",
        statement="theorem goal : True",
        file="Main.lean",
        module="Main",
        original=True,
        signature_sha256="original",
    )
    observed = {
        "success": True,
        "ok": False,
        "has_errors": False,
        "has_sorry": True,
        "valid_without_sorry": False,
        "messages": [{"severity": "warning", "message": "declaration uses `sorry`"}],
        **feedback,
    }
    calls: list[dict[str, Any]] = []

    def check(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return observed

    monkeypatch.setattr(check_process, "check_scratch", check)
    monkeypatch.setattr(
        type_profile,
        "compiled_type_profiles",
        lambda **_: {
            "accepted": profile.get("accepted", True),
            "profiles": {
                "goal": {
                    "sha256": profile.get("sha256", "original"),
                    "axioms": profile.get("axioms", ["sorryAx"]),
                }
            },
        },
    )
    verifier = LeanVerifier(tmp_path, ())
    result = verifier.check(node, source, skeleton=True)
    assert result["accepted"] is expected
    assert result["ok"] is False
    assert result["valid_without_sorry"] is False
    assert calls[0]["allow_placeholders_for_elaboration"] is True
    assert verifier.check(node, source)["accepted"] is False
    assert calls[1]["allow_placeholders_for_elaboration"] is False
