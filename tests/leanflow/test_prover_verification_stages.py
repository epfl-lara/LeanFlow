"""Keep authoritative candidate checks single-pass, bounded, and observable."""

from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process, type_profile
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.verification import LeanVerifier


@pytest.mark.parametrize("axioms,accepted", [([], True), (["sorryAx"], False), (["extra"], False)])
def test_closed_candidate_uses_fresh_compiled_evidence_without_duplicate_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, axioms: list[str], accepted: bool
) -> None:
    source = tmp_path / "candidate.lean"
    source.write_text("theorem goal : True := by trivial\n")
    node = Node(
        id="n", name="goal", statement="theorem goal : True", file="Main.lean", module="Main"
    )
    node.signature_sha256 = "frozen"
    monkeypatch.setattr(
        check_process, "check_scratch", lambda **_: pytest.fail("duplicate elaboration")
    )

    def profile(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["source"] == source.read_text()
        assert kwargs["names"] == ["goal"]
        return {"accepted": True, "profiles": {"goal": {"sha256": "frozen", "axioms": axioms}}}

    monkeypatch.setattr(type_profile, "compiled_type_profiles", profile)
    result = LeanVerifier(tmp_path, ()).check(node, source)
    assert result["accepted"] is accepted
    assert result["axiom_profile_checked"] is True
    assert result["axiom_profile_axioms"] == axioms


def test_profile_stages_report_timing_and_share_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(type_profile.time, "monotonic", lambda: clock[0])
    budgets: list[float] = []
    stages: list[str] = []

    def command(**kwargs: Any) -> dict[str, Any]:
        budgets.append(kwargs["timeout_s"])
        argv = kwargs["argv"]
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"compiled")
            clock[0] += 4
            return {"success": True}
        clock[0] += 6
        return {"success": False, "error_code": "check_timeout", "timed_out": True}

    def stage(name: str, action: Any) -> dict[str, Any]:
        stages.append(name)
        return dict(action())

    monkeypatch.setattr(check_process, "isolated_command", command)
    result = type_profile.compiled_type_profiles(
        root=tmp_path,
        source="theorem goal : True := by trivial",
        names=["goal"],
        workspace=tmp_path / "checks",
        timeout_s=10,
        on_stage=stage,
    )
    assert not result["accepted"]
    assert budgets == [10, 6]
    assert stages == ["compile", "inspect"]
    assert result["timing"] == {"compile_s": 4, "inspect_s": 6}
