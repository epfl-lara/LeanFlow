"""Stop real prover worker lock waits before an unrelated publisher releases ownership."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime

SOURCE = b"theorem goal : True := by sorry\n"


class _NoVerifier:
    """Refuse a verifier launch so cancellation cannot accidentally perform further work."""

    def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("cancelled ownership waiter launched verification")


def _controller(root: str, channel: Connection, mode: str) -> None:
    """Run the actual candidate-feedback caller in a pool under an exact-owner SIGINT."""
    from leanflow_cli.formalization import publication_io
    from leanflow_cli.workflows.prover.job_controller import candidate_feedback

    project = Path(root)
    runtime = ProverRuntime(
        root=project,
        targets=[project / "Main.lean"],
        config=ProverConfig(wall_time_s=0.3 if mode == "deadline" else None),
        session=lambda **kwargs: {},
        verifier=_NoVerifier(),
    )
    node = runtime.dag.nodes[0]
    job = {
        "id": "cancellation-worker",
        "node_id": node.id,
        "node_revision": node.revision,
        "workspace": str(runtime.store.directory / "cancellation-worker"),
    }
    actual = publication_io.fcntl.flock
    announced = False

    def acquiring(descriptor: int, operation: int) -> Any:
        nonlocal announced
        if not announced:
            channel.send("waiting")
            announced = True
        return actual(descriptor, operation)

    publication_io.fcntl.flock = acquiring
    channel.send("ready")
    assert channel.recv() == "start"
    outcome = "unexpected"
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(
                candidate_feedback, runtime, job, json.dumps({"proof": "trivial"})
            )
            try:
                future.result()
                outcome = "verified"
            except KeyboardInterrupt:
                # Match runtime.run's inner exception handling before executor shutdown.
                runtime.cancelled.set()
                outcome = "interrupted"
            except Exception as error:
                outcome = getattr(error, "code", getattr(error, "status", str(error)))
    finally:
        channel.send(
            {
                "outcome": outcome,
                "node_status": node.status,
                "cancelled": runtime.cancelled.is_set(),
                "run_directory": str(runtime.store.directory),
            }
        )
        channel.close()


@pytest.mark.parametrize("mode", ["signal", "deadline"])
def test_prover_worker_and_executor_drain_while_publisher_still_holds_lock(
    tmp_path: Path, mode: str
) -> None:
    target = tmp_path / "Main.lean"
    target.write_bytes(SOURCE)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(target=_controller, args=(str(tmp_path), remote, mode))
    process.start()
    remote.close()
    assert local.poll(15) and local.recv() == "ready"
    with publication_lock(tmp_path):
        local.send("start")
        assert local.poll(15) and local.recv() == "waiting"
        if mode == "signal":
            os.kill(process.pid, signal.SIGINT)
        assert local.poll(2), "Stopped worker/executor requires unrelated publisher release"
        result = local.recv()
        process.join(2)
        assert process.exitcode == 0
        assert result["outcome"] == ("interrupted" if mode == "signal" else "campaign_wall_time")
        assert result["cancelled"] and result["node_status"] != "proved"
        assert target.read_bytes() == SOURCE
        assert not (tmp_path / ".leanflow/prover-source-transactions").exists()
        directory = Path(result["run_directory"])
        assert not (directory / "source-transaction.json").exists()
        events = directory / "events.jsonl"
        assert '"candidate_checked"' not in (events.read_text() if events.exists() else "")
    local.close()
