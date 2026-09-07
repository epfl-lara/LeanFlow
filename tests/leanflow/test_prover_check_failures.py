"""Keep Lake configuration failures out of mathematical proof retry handling."""

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
