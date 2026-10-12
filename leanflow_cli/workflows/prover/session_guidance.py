"""Load explicit skills and deliver durable user guidance to its addressed job."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.runtime.skill_core import load_skill


def skill_guidance(root: Path, role: str = "prover") -> str:
    """Read the active prover contract and explicitly selected skill overlays."""
    active = os.getenv("LEANFLOW_NATIVE_ACTIVE_SKILL") or "lean-bounded-prover"
    if active == "lean-bounded-prover" and role not in {"prover", "negation"}:
        active = "lean-prover-orchestrator"
    names = [active]
    names += os.getenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", "").split(os.pathsep)
    chunks = []
    for name in dict.fromkeys(value.strip() for value in names if value.strip()):
        path = Path(name).expanduser()
        path = path if path.is_absolute() else root / path
        if path.is_file():
            if path.stat().st_size > 128000:
                raise ValueError(f"Requested skill exceeds 128 KB: {name}")
            content = path.read_text(encoding="utf-8")
        else:
            payload = load_skill(name, cwd=root)
            if payload is None:
                raise ValueError(f"Requested prover skill was not found: {name}")
            content = str(payload["content"])
        chunks.append(f"\n\n[Selected skill: {name}]\n{content}")
    return "".join(chunks)


class GuidanceInbox:
    """Persist addressed guidance with its cursor for bounded, resumable prompt context."""

    def __init__(self, root: Path, ledger: Path, context: dict[str, Any], role: str) -> None:
        run_id = str(context.get("run_id", ""))
        self.path: Path | None = None
        self.position = ledger / "guidance-offset.json"
        self.recipient = str(context.get("job_id", ""))
        self.orchestrator = role in {"orchestrator", "planner", "review"}
        self.offset = 0
        self.history: list[dict[str, str]] = []
        self.omitted = 0
        self.plan_offset = 0
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            return
        self.path = root / ".leanflow/workflow-state/prover" / run_id / "inbox.jsonl"
        if self.orchestrator:
            captured_offset = context.get("inbox_offset")
            self.plan_offset = (
                captured_offset
                if isinstance(captured_offset, int) and captured_offset >= 0
                else self.path.stat().st_size if self.path.is_file() else 0
            )
        if self.position.is_file():
            saved = json.loads(self.position.read_text())
            if "messages" in saved:
                self.offset = int(saved["offset"])
                self.omitted = int(saved.get("omitted", 0))
                for value in saved["messages"]:
                    self._remember(value)
            # Upgrade cursor-only ledgers by rereading addressed messages;
            # they were never retained in the old session's pinned context.
        self._save()

    def _remember(self, value: dict[str, Any]) -> None:
        """Retain complete CLI-sized messages within a bounded recent guidance window."""
        self.history.append(
            {
                "id": str(value.get("id", ""))[:128],
                "message": str(value.get("message", ""))[:8192],
            }
        )
        while sum(len(item["message"]) + len(item["id"]) + 32 for item in self.history) > 15360:
            self.history.pop(0)
            self.omitted += 1

    def _save(self) -> None:
        """Atomically preserve the delivery cursor and the context needed after a restart."""
        atomic_json_write(
            self.position,
            {"offset": self.offset, "messages": self.history, "omitted": self.omitted},
        )

    def contract(self) -> str:
        """Return bounded durable user guidance to pin beside the original assignment."""
        prefix = (
            f"Earlier guidance outside this bounded window: {self.omitted} messages. "
            "The full messages remain in the run inbox; omission does not withdraw constraints.\n\n"
            if self.omitted
            else ""
        )
        return prefix + "\n\n".join(
            f"User guidance {item['id']}:\n{item['message']}" for item in self.history
        )

    def poll(self) -> list[dict[str, Any]]:
        """Return newly arrived guidance for this job at a model request boundary."""
        if self.path is None or not self.path.is_file():
            return []
        result = []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            while True:
                line = handle.readline(65537)
                if not line or not line.endswith(b"\n"):
                    break
                self.offset = handle.tell()
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                recipient = str(value.get("agent_id", "orchestrator"))
                if recipient == self.recipient or (
                    recipient == "orchestrator"
                    and self.orchestrator
                    and self.offset > self.plan_offset
                ):
                    self._remember(value)
                    result.append(value)
        self._save()
        return result
