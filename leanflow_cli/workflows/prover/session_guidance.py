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
    """Consume complete addressed messages without replaying them after resume."""

    def __init__(self, root: Path, ledger: Path, context: dict[str, Any], role: str) -> None:
        run_id = str(context.get("run_id", ""))
        self.path: Path | None = None
        self.position = ledger / "guidance-offset.json"
        self.recipient = str(context.get("job_id", ""))
        self.orchestrator = role in {"orchestrator", "planner", "review"}
        self.offset = 0
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            return
        self.path = root / ".leanflow/workflow-state/prover" / run_id / "inbox.jsonl"
        if self.position.is_file():
            self.offset = int(json.loads(self.position.read_text())["offset"])
        elif self.path.is_file():
            # Existing guidance is already included in the controller's plan.
            self.offset = self.path.stat().st_size
        atomic_json_write(self.position, {"offset": self.offset})

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
                    recipient == "orchestrator" and self.orchestrator
                ):
                    result.append(value)
        atomic_json_write(self.position, {"offset": self.offset})
        return result
