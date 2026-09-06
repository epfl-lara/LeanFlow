"""Check immutable source checkpoints without idle rewrites of project-sized payloads."""

from pathlib import Path

from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.source import SourceDocument
from leanflow_cli.workflows.prover.store import RunStore


def test_metric_updates_do_not_rewrite_unchanged_source_plan_or_dag(tmp_path: Path) -> None:
    store = RunStore(tmp_path, "run")
    documents = {"Main.lean": SourceDocument("Main.lean", "theorem goal : True := by sorry\n")}
    state = {"plan_markdown": "Prove goal.", "metrics": {"api_calls": 0}}
    store.write(state, Dag(), documents)
    paths = [store.directory / name for name in ("source.json", "PLAN.md", "DAG.json")]
    for path in paths:
        import os

        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    state["metrics"]["api_calls"] = 1
    store.write(state, Dag(), documents)
    assert all(path.stat().st_mtime_ns == 1_000_000_000 for path in paths)
    old_checkpoint = state["source_checkpoint"]
    documents["Main.lean"].replacements["0"] = "trivial"
    store.write(state, Dag(), documents)
    assert state["source_checkpoint"] != old_checkpoint
    assert (store.directory / old_checkpoint).is_file()
    assert "trivial" in paths[0].read_text()
