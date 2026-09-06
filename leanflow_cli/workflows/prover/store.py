"""Persist controller-owned plans, graph, events, and reviewable source baselines."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.utils import atomic_json_write
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

    def write(self, state: dict[str, Any], dag: Dag, documents: dict[str, SourceDocument]) -> None:
        """Atomically publish a coherent state snapshot after controller transitions."""
        with self.lock:
            state.update(
                version=1,
                run_id=self.run_id,
                updated_at=now(),
                dag=dag.to_dict(),
                plan_path=str(self.directory / "PLAN.md"),
                dag_path=str(self.directory / "DAG.json"),
            )
            atomic_json_write(self.directory / "DAG.json", dag.to_dict())
            atomic_json_write(
                self.directory / "source.json",
                {key: value.to_dict() for key, value in documents.items()},
            )
            atomic_json_write(self.directory / "state.json", state)
            plan_path = self.directory / "PLAN.md"
            pending = plan_path.with_suffix(".tmp")
            pending.write_text(str(state.get("plan_markdown", "")))
            pending.replace(plan_path)

    def event(self, kind: str, details: dict[str, Any]) -> None:
        """Append one bounded structured event for audit and live progress."""
        event = {"time": now(), "event": kind, **details}
        with self.lock, (self.directory / "events.jsonl").open("a") as handle:
            handle.write(
                redact_sensitive_text(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
            )
        if self.on_event is not None:
            self.on_event(kind, details)

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
