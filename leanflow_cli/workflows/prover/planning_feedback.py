"""Validate planning report contracts before the originating session may finish."""

from __future__ import annotations

from typing import Any

from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.planning import apply_proposal, json_report


def report_feedback(
    text: str, *, stage: str, dag: Dag, max_nodes: int, affected: set[str] | None = None
) -> dict[str, Any]:
    """Check format and graph structure without publishing a plan or approving its mathematics."""
    report = json_report(text)
    error = ""
    if not report:
        error = "Cannot parse a nonempty JSON object. Return only the requested JSON object, without introductory or trailing prose. Keep the useful plan in your previous response; correct its format."
    elif stage == "review":
        if not isinstance(report.get("accepted"), bool):
            error = "Review field 'accepted' must be a JSON boolean (true or false), not a string."
        elif report["accepted"] is False and not str(report.get("critique") or "").strip():
            error = "A rejected review must include a concrete 'critique'."
    elif not isinstance(report.get("plan"), str) or not report["plan"].strip():
        error = "Report field 'plan' must contain a nonempty concrete proof outline."
    elif stage == "proposal":
        try:
            apply_proposal(dag, report, max_nodes=max_nodes, affected=affected)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            error = str(exc)
    if (
        not error
        and "change_kind" in report
        and report["change_kind"] not in ("direction", "decomposition", "repair")
    ):
        error = "Field 'change_kind' must be direction, decomposition, or repair."
    return {"accepted": not error, "stage": stage, "error": error}
