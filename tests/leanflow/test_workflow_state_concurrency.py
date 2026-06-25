"""B1: concurrent workflow-state appends must not interleave or lose JSON-lines.

`leanflow_cli.workflows.workflow_state._locked_append` serializes appends so that the parallel `/swarm`
agents (threads within a process via the module lock; subprocesses via fcntl.flock) cannot corrupt
the activity/outcome/run-log streams. These tests exercise the in-process locking layer directly.
"""

from __future__ import annotations

import json
import threading

from leanflow_cli.workflows import workflow_state as ws


def test_locked_append_no_interleave_or_loss_under_threads(tmp_path):
    target = tmp_path / "activity.jsonl"
    n_threads, per_thread = 24, 60

    def worker(tid: int) -> None:
        for i in range(per_thread):
            ws._locked_append(target, json.dumps({"t": tid, "i": i}, sort_keys=True) + "\n")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    # No lost writes.
    assert len(lines) == n_threads * per_thread
    # Every line is a complete, parseable JSON object (no interleaving / torn writes).
    seen = set()
    for line in lines:
        obj = json.loads(line)
        seen.add((obj["t"], obj["i"]))
    # No duplicated or dropped records.
    assert len(seen) == n_threads * per_thread


def test_locked_append_is_resumable_after_concurrent_writes(tmp_path):
    target = tmp_path / "outcomes.jsonl"

    def worker(tid: int) -> None:
        for i in range(40):
            ws._locked_append(target, json.dumps({"kind": "lean-search", "t": tid, "i": i}) + "\n")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # A reader that parses line-by-line (as resume/status do) sees only valid records.
    parsed = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(parsed) == 10 * 40
    assert all(rec["kind"] == "lean-search" for rec in parsed)
