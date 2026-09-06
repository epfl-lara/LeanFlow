"""Require fresh verification whenever cached submission inputs or trust change."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.job_controller import candidate_feedback
from leanflow_cli.workflows.prover.source import SourceDocument
from tests.leanflow.test_prover_live_progress import make_runtime


@pytest.mark.parametrize("changed", ["none", "candidate", "dependency", "during_check", "manifest"])
def test_reuse_requires_same_independently_checked_inputs(tmp_path: Path, changed: str) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    checks: list[str] = []

    def add_source() -> None:
        document = SourceDocument("Other.lean", "theorem other : True := by trivial\n")
        runtime.documents[document.path] = document
        (tmp_path / document.path).write_text(document.render())

    class Verifier:
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            checks.append(file.read_text())
            if changed == "during_check" and len(checks) == 1:
                add_source()
            return {"accepted": True, "axiom_profile_checked": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": True}

    runtime.verifier = Verifier()
    report = candidate_feedback(runtime, job, json.dumps({"proof": "exact True.intro"}))
    assert report["accepted"]
    if changed == "dependency":
        add_source()
    if changed == "manifest":
        (tmp_path / "lake-manifest.json").write_text('{"packages": []}')
    candidate = "trivial" if changed == "candidate" else "exact True.intro"
    assert runtime._accept(node, [candidate], job["id"])
    assert len(checks) == (1 if changed == "none" else 2)
    assert node.status == "proved"


def test_failed_or_conditional_results_never_populate_cache(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    cache = runtime.submission_cache
    cache.remember(node, "same", "same", {"accepted": False}, conditional=False)
    assert cache.take(node, "same") is None
    cache.remember(node, "same", "same", {"accepted": True}, conditional=True)
    assert cache.take(node, "same") is None
    cache.remember(node, "before", "after", {"accepted": True}, conditional=False)
    assert cache.take(node, "after") is None
