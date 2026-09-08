"""Persist controller-owned plans, graph, events, and reviewable source baselines."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover import plan_journal
from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.source import SourceDocument


def now() -> str:
    return datetime.now(UTC).isoformat()


class RunStore:
    """Serialize state updates and expose one stable extension-facing snapshot."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.directory = root / ".leanflow" / "workflow-state" / "prover" / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.inbox_offset = 0
        self.on_event: Any = None
        self._source_snapshot: dict[str, Any] | None = None
        self._source_checkpoint = ""
        self._dag_snapshot: dict[str, Any] | None = None
        self._plan_snapshot: str | None = None
        self._sequence = 0

    def write_progress(self, state: dict[str, Any]) -> None:
        """Publish metadata with a monotonic sequence, retaining committed source identity."""
        with self.lock:
            self._sequence = max(self._sequence, int(state.get("snapshot_sequence", 0))) + 1
            state.update(updated_at=now(), snapshot_sequence=self._sequence)
            atomic_json_write(self.directory / "state.json", state)

    def write(
        self,
        state: dict[str, Any],
        dag: Dag,
        documents: dict[str, SourceDocument],
        *,
        publish: bool = True,
    ) -> None:
        """Atomically publish a coherent state snapshot after controller transitions."""
        with self.lock:
            source_documents = {key: value.to_dict() for key, value in documents.items()}
            if source_documents != self._source_snapshot:
                source_digest = hashlib.sha256(
                    json.dumps(source_documents, sort_keys=True).encode()
                ).hexdigest()
                source_checkpoint = Path("source-checkpoints") / f"{source_digest}.json"
                checkpoint_path = self.directory / source_checkpoint
                if not checkpoint_path.exists():
                    atomic_json_write(checkpoint_path, source_documents)
                atomic_json_write(self.directory / "source.json", source_documents)
                self._source_snapshot = source_documents
                self._source_checkpoint = str(source_checkpoint)
            dag_snapshot = dag.to_dict()
            state.update(
                version=1,
                run_id=self.run_id,
                updated_at=now(),
                dag=dag_snapshot,
                plan_path=str(self.directory / "PLAN.md"),
                dag_path=str(self.directory / "DAG.json"),
                source_checkpoint=self._source_checkpoint,
            )
            if dag_snapshot != self._dag_snapshot:
                atomic_json_write(self.directory / "DAG.json", dag_snapshot)
                self._dag_snapshot = dag_snapshot
            # PLAN.md shows the plan and the journal together. They are stored
            # apart because an accepted proposal replaces plan_markdown wholesale
            # and must not take the journal with it.
            plan = str(state.get("plan_markdown", ""))
            journal = plan_journal.render(state.get("plan_journal", []))
            if journal:
                plan = plan.rstrip("\n") + "\n\n" + journal
            if plan != self._plan_snapshot:
                plan_path = self.directory / "PLAN.md"
                pending = plan_path.with_suffix(".tmp")
                pending.write_text(plan)
                pending.replace(plan_path)
                self._plan_snapshot = plan
            if publish:
                self.write_progress(state)

    def event(self, kind: str, details: dict[str, Any]) -> None:
        """Append one bounded structured event for audit and live progress.

        Every record carries an ``evidence_id``. A session that already logged
        the same event in its private job log supplies the id it used there, so
        the compact activity row, this record, and the job transcript entry can
        be joined later without guessing by timestamp.
        """
        payload = {**details}
        payload.setdefault("evidence_id", uuid.uuid4().hex[:12])
        event = {"time": now(), "event": kind, **payload}
        with self.lock, (self.directory / "events.jsonl").open("a") as handle:
            handle.write(
                redact_sensitive_text(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
            )
        if self.on_event is not None:
            self.on_event(kind, payload)

    def baseline(self, document: SourceDocument) -> Path:
        """Preserve a before-view once so editors can show exact diffs."""
        path = self.directory / "baselines" / document.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("" if document.generated else document.baseline)
        return path

    def messages(self) -> list[dict[str, Any]]:
        """Consume complete user inbox lines once, ignoring a concurrent partial write."""
        path = self.directory / "inbox.jsonl"
        if not path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self.lock, path.open() as handle:
            handle.seek(self.inbox_offset)
            while True:
                line = handle.readline()
                if not line or not line.endswith("\n"):
                    break
                self.inbox_offset = handle.tell()
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    result.append(message)
        return result
