"""Separate recoverable proof failures from unavailable verification infrastructure."""

from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from tests.leanflow.test_prover_live_progress import make_runtime

LAKE_ERROR = (
    "error: operation not permitted (error code: 1)\n"
    "  file: /project/.lake/config/2/lakefile.olean.lock\n"
)
HEARTBEAT_ERROR = "(deterministic) timeout at `whnf`, maximum number of heartbeats reached"


@pytest.mark.parametrize("stage", ["compile", "inspect", "kernel_profile", "backend"])
def test_lake_cache_lock_denial_is_an_infrastructure_failure(stage: str) -> None:
    failed: dict[str, Any] = {"success": False, "returncode": 1, "stderr": LAKE_ERROR}
    result = (
        {"error_code": "backend_error", "error": LAKE_ERROR}
        if stage == "backend"
        else {stage: failed}
    )
    assert infrastructure_code(result) == "lake_config_cache_error"


def test_ordinary_lean_rejection_remains_a_proof_failure() -> None:
    assert infrastructure_code({"error": "unsolved goals", "accepted": False}) == ""


@pytest.mark.parametrize("stage", [None, "compile", "inspect", "kernel_profile"])
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"timed_out": True}, "check_timeout"),
        ({"error_code": "check_timeout"}, "check_timeout"),
        (
            {"error_code": "lean_probe_wall_clock_timeout", "timed_out": True},
            "check_timeout",
        ),
        ({"error_code": "check_cancelled"}, "check_cancelled"),
        ({"error_code": "check_setup_failed"}, "check_setup_failed"),
        ({"error_code": "isolation_unavailable"}, "isolation_unavailable"),
    ],
)
def test_process_failures_remain_infrastructure_failures(
    stage: str | None, payload: dict[str, Any], expected: str
) -> None:
    result = {stage: payload} if stage else payload
    assert infrastructure_code(result) == expected


@pytest.mark.parametrize("stage", [None, "compile", "inspect", "kernel_profile"])
@pytest.mark.parametrize("legacy", [False, True])
def test_heartbeat_exhaustion_remains_a_proof_failure(stage: str | None, legacy: bool) -> None:
    failed: dict[str, Any] = {"ok": False, "timed_out": legacy, "error": HEARTBEAT_ERROR}
    if legacy:
        failed["timed_out_inferred_from_diagnostics"] = True
    else:
        failed["error_code"] = "lean_heartbeat_limit"
    result = {stage: failed} if stage else failed

    assert infrastructure_code(result) == ""


@pytest.mark.parametrize(
    "failure",
    [
        {"timed_out": True},
        {
            "timed_out": True,
            "timed_out_inferred_from_diagnostics": True,
            "stderr": "LeanProbe wall-clock deadline exceeded",
        },
        {
            "timed_out": True,
            "timed_out_inferred_from_diagnostics": True,
            "stderr": "Lean process timed out",
        },
        {"error_code": "check_timeout", "timed_out_inferred_from_diagnostics": True},
        {
            "error_code": "lean_probe_wall_clock_timeout",
            "timed_out_inferred_from_diagnostics": True,
        },
        {"error_code": "check_cancelled", "timed_out_inferred_from_diagnostics": True},
        {"error_code": "check_setup_failed", "timed_out_inferred_from_diagnostics": True},
    ],
)
def test_heartbeat_diagnostic_cannot_mask_a_real_infrastructure_failure(
    failure: dict[str, Any],
) -> None:
    assert infrastructure_code({"error": HEARTBEAT_ERROR, **failure})


def test_heartbeat_failure_does_not_hide_independent_nested_timeout() -> None:
    result = {
        "error_code": "lean_heartbeat_limit",
        "timed_out": False,
        "kernel_profile": {"compile": {"timed_out": True}},
    }
    assert infrastructure_code(result) == "check_timeout"


@pytest.mark.parametrize("legacy", [False, True])
def test_heartbeat_rejection_returns_to_controller_proof_retry(
    tmp_path: Path, legacy: bool
) -> None:
    runtime = make_runtime(tmp_path)

    class Verifier:
        def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "accepted": False,
                "compile": {
                    "ok": False,
                    "error": HEARTBEAT_ERROR,
                    "timed_out": legacy,
                    "timed_out_inferred_from_diagnostics": legacy,
                },
            }

    runtime.verifier = Verifier()
    node = runtime.dag.nodes[0]

    assert runtime._accept(node, ["trivial"], "controller") is False
    assert node.status == "pending"
    assert HEARTBEAT_ERROR in node.notes


def test_cache_denial_stops_before_controller_proof_retry(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)

    class Verifier:
        def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"accepted": False, "kernel_profile": {"compile": {"stderr": LAKE_ERROR}}}

    runtime.verifier = Verifier()
    with pytest.raises(InfrastructureFailure, match="verification environment"):
        runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
    assert runtime.dag.nodes[0].candidate == ["trivial"]
    assert runtime.dag.nodes[0].status == "candidate"
