"""Keep authoritative candidate checks single-pass, bounded, and observable."""

import threading
from pathlib import Path
from typing import Any

import pytest

from core.deadlines import operation_cancellation
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


@pytest.mark.parametrize("per_operation", [False, True])
def test_queued_verification_cancels_without_waiting_for_other_jobs(
    tmp_path: Path, per_operation: bool
) -> None:
    """Unlimited campaigns must still cancel operations waiting for the verifier."""
    verifier = LeanVerifier(tmp_path, ())
    cancelled = threading.Event()
    started = threading.Event()
    result: list[dict[str, Any]] = []
    if not per_operation:
        verifier.cancelled = cancelled.is_set
    node = Node(
        id="n", name="goal", statement="theorem goal : True", file="Main.lean", module="Main"
    )

    def check() -> None:
        with operation_cancellation(cancelled.is_set if per_operation else lambda: False):
            started.set()
            result.append(verifier.check(node, tmp_path / "unused.lean"))

    verifier.lock.acquire()
    thread = threading.Thread(target=check)
    thread.start()
    try:
        assert started.wait(1)
        cancelled.set()
        thread.join(1)
        assert not thread.is_alive()
        assert result[0]["error_code"] == "check_cancelled"
        assert result[0]["accepted"] is False
    finally:
        verifier.lock.release()
        thread.join(1)
