"""Bound recovery of unusable stage reports across requests and process restarts."""

from __future__ import annotations

import json
from pathlib import Path

from core.utils import atomic_json_write

REPORT_FAILURE = (
    "Empty or truncated stage response; the single report-only recovery allowance "
    "is unavailable or already admitted. No further stage requests will be sent."
)
REPORT_RECOVERY_PROMPT = (
    "Your previous response was empty or truncated before a usable final report. "
    "This is the single recovery request, charged to the existing stage budget. "
    "Tools are disabled. Use the findings already in the conversation and return "
    "the complete requested JSON/report now. Keep it concise, state uncertainties, "
    "and do not restart the research or repeat the derivation."
)


class ReportRecovery:
    """Admit at most one final-report follow-up, failing closed after a restart."""

    def __init__(self, runtime_directory: Path) -> None:
        """Read the durable marker before any recovery request can be admitted."""
        self.path = runtime_directory / "report-recovery.json"
        self.previously_admitted = self.path.exists()
        self.attempted = self.previously_admitted
        saved = json.loads(self.path.read_text()) if self.previously_admitted else {}
        self.final_response = str(saved.get("final_response", ""))

    def admit(self, used: int, limit: int) -> bool:
        """Persist the allowance before dispatch without extending the call ceiling.

        A crash after this write consumes the allowance conservatively. Reopening
        the workspace cannot turn a failed report into a new retry loop.
        """
        if self.attempted:
            return False
        self.attempted = True
        atomic_json_write(self.path, {"after_api_call": used, "limit": limit})
        return used < limit

    def complete(self, response: str) -> None:
        """Preserve a recovered report so reopening does not repeat or lose it."""
        if self.attempted:
            atomic_json_write(self.path, {"final_response": response})
