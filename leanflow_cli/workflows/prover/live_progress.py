"""Publish live work independently of serialized, transactional source acceptance."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from leanflow_cli.workflows.prover.store import now

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

T = TypeVar("T")


def _snapshot(value: Any) -> Any:
    """Copy JSON containers from stable shallow views during metadata mutation.

    Writers serialize committed DAG/source pointers under the progress lock.
    Other metadata may gain keys between checkpoints; copying each builtin
    container first prevents iteration over a concurrently resized dictionary.
    """
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.copy().items()}
    if isinstance(value, list):
        return [_snapshot(item) for item in value.copy()]
    if isinstance(value, tuple):
        return tuple(_snapshot(item) for item in value)
    return copy.deepcopy(value)


class LiveProgress:
    """Publish metadata against the last committed DAG and source checkpoint.

    Never serialize mutable source documents during a transaction. Operation
    overlays describe current work without changing the scheduler's proof state.
    """

    def __init__(self, runtime: ProverRuntime) -> None:
        self.runtime = runtime
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def publish(self) -> None:
        """Publish usage and operations without taking the source transaction lock."""
        with self.lock:
            runtime = self.runtime
            runtime._refresh_metrics()
            snapshot = _snapshot(runtime.state)
            active = [op for op in snapshot.get("operations", []) if op["status"] == "running"]
            for node in snapshot.get("dag", {}).get("nodes", []):
                if node["status"] == "proved":
                    continue
                node["scheduler_status"] = node["status"]
                related = [op for op in active if op.get("node_id") == node["id"]]
                if any(op["kind"] == "proof_integration" for op in related):
                    node["status"] = "integrating"
                elif any(op["kind"] == "submission_check" for op in related):
                    node["status"] = "verifying"
                elif node["status"] == "running":
                    latest = next(
                        (
                            job
                            for job in reversed(snapshot.get("jobs", []))
                            if job.get("node_id") == node["id"]
                            and job.get("role") == "prover"
                            and job.get("node_revision") == node.get("revision")
                        ),
                        None,
                    )
                    if latest is not None and latest.get("phase") == "submitted":
                        node["status"] = "submitted"
            runtime.store.write_progress(snapshot)
            runtime.state.update(
                updated_at=snapshot["updated_at"], snapshot_sequence=snapshot["snapshot_sequence"]
            )
            if runtime.observer is not None:
                runtime.observer.publish(snapshot)

    def begin(self, kind: str, label: str, **details: Any) -> dict[str, Any]:
        """Publish an operation before beginning expensive work."""
        with self.lock:
            timestamp = now()
            operation = {
                **details,
                "id": uuid.uuid4().hex[:12],
                "kind": kind,
                "label": label,
                "status": "running",
                "started_at": timestamp,
                "updated_at": timestamp,
            }
            operations = self.runtime.state.setdefault("operations", [])
            completed = [op for op in operations if op["status"] != "running"][-31:]
            operations[:] = [op for op in operations if op["status"] == "running"] + completed
            operations.append(operation)
            self.runtime.store.event("operation-start", operation)
            self.publish()
            return operation

    def end(self, operation: dict[str, Any], *, failed: bool = False, error: str = "") -> None:
        """Finish an operation and preserve bounded diagnostic evidence."""
        with self.lock:
            operation.update(
                status="failed" if failed else "completed", finished_at=now(), updated_at=now()
            )
            if error:
                operation["error"] = error[:16000]
            self.runtime.store.event("operation-end", operation)
            self.publish()

    def call(self, kind: str, label: str, action: Callable[[], T], **details: Any) -> T:
        """Run a deterministic check with visible start, outcome, and duration."""
        operation = self.begin(kind, label, **details)
        started = time.monotonic()
        try:
            result = action()
        except BaseException as error:
            self.end(operation, failed=True, error=str(error))
            raise
        else:
            failed = isinstance(result, dict) and (
                result.get("accepted") is False or result.get("success") is False
            )
            with self.lock:
                operation["elapsed_s"] = round(time.monotonic() - started, 3)
            diagnostic = str(result.get("error", "")) if isinstance(result, dict) and failed else ""
            self.end(operation, failed=failed, error=diagnostic)
            return result

    def start(self) -> None:
        """Refresh elapsed time during long checks without spending model calls."""

        def heartbeat() -> None:
            while not self.stop_event.wait(2):
                try:
                    self.publish()
                except Exception as error:
                    # A transient publication failure must not silently kill all later updates.
                    self.runtime.store.event("progress-error", {"error": str(error)[:2000]})

        self.thread = threading.Thread(target=heartbeat, name="leanflow-progress", daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Stop heartbeat publication before writing the terminal snapshot."""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
