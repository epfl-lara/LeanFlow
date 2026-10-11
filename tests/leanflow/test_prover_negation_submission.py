"""Pin negation submission authority, accounting, and interrupted-check recovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import negation_job
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.negation import prepare_negation
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure, ProverRuntime
from leanflow_cli.workflows.prover.source import SourceDocument


class RecordingVerifier:
    """Record exact sources while returning a controlled kernel-check result."""

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.result: dict[str, Any] = {"accepted": True}

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        self.sources.append(file.read_text())
        return dict(self.result)


def make_runtime(tmp_path: Path, **overrides: Any) -> tuple[ProverRuntime, RecordingVerifier]:
    """Build a real controller without invoking a model or Lean process."""
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    path = tmp_path / "Main.lean"
    path.write_text("import Std\n\ntheorem goal : False := by sorry\n")
    verifier = RecordingVerifier()
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(**{"mode": "research", "negation_api_calls": 3, **overrides}),
        session=lambda **kwargs: {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "intro h\n  exact h"}),
        },
        verifier=verifier,
    )
    return runtime, verifier


def test_negation_finishes_with_cached_literal_before_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the terminal job and its literal replacement in one durable outcome."""
    runtime, verifier = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    task = prepare_negation(tmp_path, node)
    assert task is not None
    proof = "intro h\n  exact h"
    check = verifier.check

    def inspect(node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        persisted = json.loads((runtime.store.directory / "state.json").read_text())
        assert persisted["negation_proofs"][runtime.dag.nodes[0].id] == proof
        assert persisted["jobs"][-1]["status"] == "completed"
        assert persisted["jobs"][-1]["accounted"] is True
        return check(node, file, **kwargs)

    monkeypatch.setattr(verifier, "check", inspect)
    outcome = negation_job.attempt_negation(runtime, node)

    assert outcome["certified"] is True
    assert verifier.sources == [SourceDocument(node.file, task.source).render({0: proof})]
    assert runtime.consumed == 1 and runtime.reserved == 0
    assert (tmp_path / "Main.lean").read_text().endswith("False := by sorry\n")


def test_actual_negation_check_deadline_preserves_proof_for_free_recheck(
    tmp_path: Path,
) -> None:
    """A process deadline cannot spend a second model allocation on resume."""
    runtime, verifier = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    verifier.result = {"accepted": False, "error_code": "check_timeout", "timed_out": True}

    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, node)

    assert runtime.state["negation_proofs"][node.id] == "intro h\n  exact h"
    verifier.result = {"accepted": True}
    outcome = negation_job.attempt_negation(runtime, node)
    assert outcome["certified"] is True
    assert len(runtime.state["jobs"]) == 1 and runtime.consumed == 1
    assert len(verifier.sources) == 2


def submission_response(proof: str, channel: str) -> dict[str, Any]:
    """Submit a candidate through the model's JSON or candidate-file interface."""
    if channel == "json":
        return {"role": "assistant", "content": json.dumps({"proof": proof})}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "submit",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "candidate.txt", "content": proof}),
                },
            }
        ],
    }
