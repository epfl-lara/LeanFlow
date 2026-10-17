"""Finish unaccepted whole-paper drafts without creating slice or publication authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from leanflow_cli.formalization.compiled_evidence import (
    assert_rebuild_artifacts,
    merge_reused_declarations,
)
from leanflow_cli.formalization.dependency_snapshot import create_formalization_snapshot
from leanflow_cli.formalization.draft_review import _proposal as _validate_advisory_proposal
from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import contained, digest, durable_json, put_blob
from leanflow_cli.formalization.lean_build import build_project
from leanflow_cli.formalization.lean_layout import generation_layout, module_for_file
from leanflow_cli.formalization.lean_policy import inspect_dependency, inspect_draft
from leanflow_cli.formalization.proposal import owned_path
from leanflow_cli.formalization.records import assert_source_current, load_document
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES, read_regular
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native.harness_formalization_stages import stage_assignment
from leanflow_cli.workflows.prover.source import lean_code_mask


class DraftRepairRequired(ValueError):
    """Report authored project defects that another bounded author turn can repair."""


def _reject_json_constant(value: str) -> NoReturn:
    """Reject nonfinite JSON spellings before treating authored data as reviewable input."""
    raise ValueError("Nonfinite JSON number is unsupported: " + value)


def _draft_proposal(snapshot: Path) -> tuple[dict[str, Any] | None, str]:
    """Validate the bounded advisory representation before spending time on compilation."""
    path = contained(snapshot, ".leanflow-job/formalization-proposal.json")
    if not path.is_file():
        return None, ""
    try:
        raw = read_regular(path, MAX_DRAFT_FILE_BYTES)
        proposal = json.loads(raw, parse_constant=_reject_json_constant)
        if not isinstance(proposal, dict):
            raise ValueError("The full draft proposal must be a JSON object")
        proposal = _validate_advisory_proposal(proposal)
    except (ValueError, RecursionError) as error:
        raise DraftRepairRequired("Repair the full draft proposal JSON: " + str(error)) from error
    units = proposal.get("units", [])
    if not isinstance(units, list) or any(
        not isinstance(unit, dict)
        or not isinstance(unit.get("declarations", []), list)
        or any(
            not isinstance(ref, dict) or not isinstance(ref.get("name"), str)
            for ref in unit.get("declarations", [])
        )
        for unit in units
    ):
        raise DraftRepairRequired(
            "Repair the authored unit and declaration rows in the full proposal"
        )
    return proposal, digest(raw)


def assignment(
    root: Path,
    environment: dict[str, str],
    context: dict[str, Any],
    prompt: str,
    state: dict[str, Any],
    feedback: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Continue the same charged campaign with complete scope and retained review findings."""
    state["best_effort"] = True
    state["best_effort_reason"] = "review_ambiguities"
    target = contained(Path(context["project_root"]), ".leanflow-job/review-findings.json")
    durable_json(target, feedback)
    context["review_findings_path"] = str(target)
    context, prompt = stage_assignment(root, environment, context, prompt, best_effort=True)
    return context, prompt + "\nRead all retained review findings at " + str(target) + ".\n"


def saved_draft(snapshot: Path, record: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    """Report full-source accounting while leaving all mathematical approval false."""
    from leanflow_cli.formalization.draft_delivery import validate_best_effort

    census = validate_best_effort(snapshot, record, layout=layout)
    return {
        "accepted": False,
        "draft_saved": True,
        "status": "unaccepted_formalization",
        "publication_authority": False,
        "proof_authority": False,
        "census": census,
    }


def repair_feedback(report: dict[str, Any]) -> str:
    """Send actionable errors without repeating the full compiled library evidence in a prompt."""
    checks = report.get("checks", {})
    return json.dumps(
        {
            "census": report.get("census"),
            "missing_compiled_mappings": report.get("missing_compiled_mappings", []),
            "checks": {
                key: checks[key]
                for key in ("accepted", "error", "error_code", "details")
                if key in checks
            },
            "build": {
                key: value
                for key, value in checks.get("project_build", {}).items()
                if key != "local_artifacts"
            },
        }
    )


def check_project(
    root: Path,
    snapshot: Path,
    record: dict[str, Any],
    baseline: dict[str, bytes],
    layout: dict[str, Any],
    directory: Path,
    allowed_axioms: tuple[str, ...],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Check the closed author's actual full project, without accepting its source interpretation."""
    from leanflow_cli.formalization.draft_delivery import (
        assert_delivery_current,
        project_manifest,
        validate_best_effort,
    )

    def sources(project: Path) -> dict[str, bytes]:
        """Read only bounded manifest members after the author has physically stopped."""
        return {
            name: read_regular(contained(project, name), MAX_DRAFT_FILE_BYTES)
            for name in project_manifest(project)
        }

    def canonical_current() -> None:
        """Reject external source drift instead of charging an author to repair it."""
        try:
            assert_source_current(root, record)
            if sources(root) != baseline:
                raise ValueError("Canonical project sources changed during best-effort checking")
        except (ValueError, OSError, KeyError) as error:
            raise RepreparationRequired(str(error)) from error

    canonical_current()
    proposal, proposal_sha256 = _draft_proposal(snapshot)
    proposed = sources(snapshot)
    prefix = f"import {layout['module_prefix']}\n".encode()
    root_name = layout["root_file"]
    root_before = baseline.get(root_name, b"")
    root_after = root_before if prefix.strip() in root_before.splitlines() else prefix + root_before
    if baseline.keys() - proposed.keys() or any(
        data != baseline.get(name)
        and not owned_path(layout, name)
        and not (name == root_name and data == root_after)
        for name, data in proposed.items()
    ):
        raise ValueError("Best-effort author changed protected project sources or configuration")
    modules = sorted(name for name in proposed if owned_path(layout, name))
    if not modules or layout["aggregator"] not in modules:
        raise DraftRepairRequired(
            "Best-effort project needs generated modules and their aggregator"
        )
    imports = {
        word
        for line in lean_code_mask(proposed[layout["aggregator"]].decode()).splitlines()
        if line.strip().startswith("import ")
        for word in line.strip().split()[1:]
    }
    if any(
        module_for_file(layout, name) not in imports
        for name in modules
        if name != layout["aggregator"]
    ):
        raise DraftRepairRequired("Best-effort aggregator must import every generated module")
    # Register only this private project's bounded import. Canonical files stay untouched.
    contained(snapshot, root_name).write_bytes(root_after)
    frozen = create_formalization_snapshot(
        root,
        directory / "project",
        environment=environment_identity,
        expected_environment=record.get("environment_id") or environment_identity(root),
    )
    for name in [*modules, root_name]:
        target = contained(frozen, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(root_after if name == root_name else proposed[name])
    expected = {**proposed, root_name: root_after}
    if sources(frozen) != expected:
        raise RepreparationRequired(
            "Frozen best-effort project differs from the retained author project"
        )
    checks = inspect_draft(
        frozen, modules, directory / "lean", allowed_axioms, deadline=deadline, cancelled=cancelled
    )
    if checks.get("accepted"):
        checks["project_build"] = build_project(
            frozen, layout, directory / "build", deadline=deadline, cancelled=cancelled
        )
        if checks["project_build"].get("success"):
            assert_rebuild_artifacts(
                checks.get("local_artifacts"), checks["project_build"].get("local_artifacts")
            )
    census = validate_best_effort(snapshot, record, layout=layout)
    if census.get("proposal_sha256") != proposal_sha256:
        raise RepreparationRequired("Draft proposal changed during independent checking")
    units = (proposal or {}).get("units", [])
    reuse_groups: dict[tuple[str, ...], set[str]] = {}
    for unit in units:
        for ref in unit.get("declarations", []):
            if (
                not ref.get("file")
                and isinstance(ref.get("name"), str)
                and isinstance(ref.get("imports"), list)
                and all(isinstance(value, str) for value in ref["imports"])
            ):
                reuse_groups.setdefault(tuple(sorted(set(ref["imports"]))), set()).add(ref["name"])
    verified_reuse: set[str] = set()
    for index, (reuse_imports, names) in enumerate(reuse_groups.items()):
        reused = inspect_dependency(
            frozen,
            sorted(names),
            directory / f"reuse-{index}",
            imports=list(reuse_imports),
            allowed_axioms=allowed_axioms,
            local_artifacts=checks.get("project_build", {}).get("local_artifacts"),
            deadline=deadline,
            cancelled=cancelled,
        )
        if reused.get("accepted"):
            merge_reused_declarations(checks, reused)
            verified_reuse.update(names)
        else:
            checks.setdefault("reuse_failures", []).append(reused)
    registry = {row["name"]: row for row in checks.get("declarations", [])}
    missing_mappings = []
    for unit in units:
        for ref in unit.get("declarations", []):
            row = registry.get(ref.get("name"), {})
            if not row or (
                row.get("file") != ref["file"]
                if ref.get("file")
                else ref.get("name") not in verified_reuse
            ):
                missing_mappings.append({"unit_id": unit.get("id"), "reference": ref})
    canonical_current()
    if sources(snapshot) != expected:
        raise RepreparationRequired("Retained author project changed during independent checking")
    try:
        assert_delivery_current(snapshot, census)
    except (ValueError, OSError) as error:
        raise RepreparationRequired(str(error)) from error
    complete = bool(
        checks.get("accepted")
        and checks.get("project_build", {}).get("success")
        and census.get("complete")
        and not missing_mappings
    )
    report = {
        "accepted": False,
        "publication_authority": False,
        "proof_authority": False,
        "lean_accepted": checks.get("accepted") is True,
        "project_build_passed": checks.get("project_build", {}).get("success") is True,
        "complete": complete,
        "checks": checks,
        "census": census,
        "missing_compiled_mappings": missing_mappings,
    }
    durable_json(directory / "report.json", report)
    subject = {**record, "checks": checks, "layout": layout}
    proposed = sources(snapshot)
    blobs = Path(record["_directory"]) / "blobs"
    subject["lean_files"] = {
        name: put_blob(blobs, data)
        for name, data in proposed.items()
        if name in modules or name == root_name
    }
    subject["context_lean_files"] = {
        name: put_blob(blobs, data) for name, data in proposed.items() if name.endswith(".lean")
    }
    return report, subject, proposal if isinstance(proposal, dict) else None


def layout_for(record: dict[str, Any], checked_layout: dict[str, Any]) -> dict[str, Any]:
    """Reuse the original checked allocation rather than adopting author-provided paths."""
    return generation_layout(checked_layout, record)


def current_record(root: Path, document_id: str) -> dict[str, Any]:
    """Read the current rejected candidate without inferring source acceptance."""
    return load_document(root, document_id)
