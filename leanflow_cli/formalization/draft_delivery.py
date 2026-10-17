"""Retain complete best-effort Lean projects without granting translation or proof authority."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.draft_delivery_census import authored_census
from leanflow_cli.formalization.evidence import contained, digest, identifier, identity, put_blob
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES, read_regular

_SKIP = {
    ".lake",
    ".git",
    ".leanflow",
    ".leanflow-runtime",
    ".leanflow-job",
    ".venv",
    "node_modules",
}
_CONFIG = {"lakefile.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json", "Blueprint.md"}
_MAX_PROJECT_FILES = 4096
_MAX_PROJECT_BYTES = 64 * 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024


def project_manifest(snapshot: Path) -> dict[str, str]:
    """Hash bounded regular source/config files while leaving retained dependency views alone."""
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("Draft project must be a real controller-owned directory")
    files: dict[str, str] = {}
    visited = total = 0
    for parent, directories, names in os.walk(snapshot, followlinks=False):
        visited += len(directories) + len(names)
        if visited > 16384:
            raise ValueError("Draft project exceeds its bounded inventory")
        directories[:] = sorted(name for name in directories if name not in _SKIP)
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise ValueError("Draft project source subtree traverses an alias")
        for name in sorted(names):
            if not (name.endswith(".lean") or name in _CONFIG):
                continue
            path = Path(parent) / name
            relative = path.relative_to(snapshot).as_posix()
            data = read_regular(contained(snapshot, relative), MAX_DRAFT_FILE_BYTES)
            total += len(data)
            if len(files) >= _MAX_PROJECT_FILES or total > _MAX_PROJECT_BYTES:
                raise ValueError("Draft project source exceeds its bounded inventory")
            files[relative] = digest(data)
    return dict(sorted(files.items()))


def _source_copies_unchanged(snapshot: Path, record: dict[str, Any]) -> None:
    """Reject modified source copies if the saved project contains the original source paths."""
    for name, key in record["source_files"].items():
        path = contained(snapshot, name)
        if path.exists() or path.is_symlink():
            size = complete_inventory(record)["files"][name]["size"]
            if digest(read_regular(path, size)) != key:
                raise ValueError("Original source copy changed in draft project: " + name)


def validate_best_effort(
    snapshot: Path, record: dict[str, Any], *, layout: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Count the whole frozen denominator and saved files without claiming Lean or review approval."""
    census = authored_census(snapshot, record)
    manifest = project_manifest(snapshot)
    _source_copies_unchanged(snapshot, record)
    allocated = layout or record.get("layout", {})
    prefix, aggregator = allocated.get("owned_prefix", ""), allocated.get("aggregator", "")
    saved = bool(
        isinstance(prefix, str)
        and prefix
        and isinstance(aggregator, str)
        and aggregator in manifest
        and any(name.startswith(prefix + "/") and name.endswith(".lean") for name in manifest)
        and "lean-toolchain" in manifest
        and ("lakefile.lean" in manifest or "lakefile.toml" in manifest)
    )
    census.update(project_saved=saved, project_manifest=manifest)
    census["complete"] = bool(census["complete"] and saved)
    if not saved:
        census["issues"].append(
            "Allocated Lean modules, aggregator or project configuration missing"
        )
    return census


def _project_path(root: Path, run_id: str) -> Path:
    """Derive the only directory admitted as a retained project artifact."""
    return contained(root, f".leanflow/native-workflows/{identifier(run_id)}/project")


def _project_artifact(
    root: Path, snapshot: Path, run_id: str, manifest: dict[str, str]
) -> dict[str, Any]:
    """Bind a folder artifact to its controller run and stable source/config manifest."""
    expected = _project_path(root, run_id)
    if snapshot.absolute() != expected or not expected.is_dir():
        raise ValueError("Draft project differs from the controller-allocated run directory")
    relative = expected.relative_to(root.resolve()).as_posix()
    key = identity(manifest)
    return {
        "id": "draft-project-" + identity({"run_id": run_id, "path": relative, "sha256": key}),
        "path": relative,
        "kind": "project",
        "sha256": key,
        "title": "Open unaccepted Lean project",
    }


def assert_delivery_current(snapshot: Path, delivery: dict[str, Any]) -> None:
    """Reject project or proposal edits after the controller captured its delivery identities."""
    manifest = project_manifest(snapshot)
    if manifest != delivery.get("project_manifest"):
        raise ValueError("Draft project changed before delivery retention")
    proposal_path = contained(snapshot, ".leanflow-job/formalization-proposal.json")
    proposal_hash = (
        digest(read_regular(proposal_path, MAX_DRAFT_FILE_BYTES)) if proposal_path.exists() else ""
    )
    if proposal_hash != delivery.get("proposal_sha256"):
        raise ValueError("Draft proposal changed before delivery retention")


def compilation_matches(census: dict[str, Any], report: Any) -> bool:
    """Bind controller compilation to exact current saved sources and authored proposal bytes."""
    checked = report.get("census") if isinstance(report, dict) else None
    return bool(
        isinstance(checked, dict)
        and report.get("complete") is True
        and checked.get("project_manifest") == census.get("project_manifest")
        and checked.get("proposal_sha256") == census.get("proposal_sha256")
    )


def freeze_delivery_artifacts(
    root: Path,
    snapshot: Path,
    blobs: Path,
    *,
    run_id: str,
    delivery: dict[str, Any],
    reports: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze controller reports and bind the actual saved project without adopting raw claims."""
    assert_delivery_current(snapshot, delivery)
    manifest = delivery["project_manifest"]
    project = _project_artifact(root, snapshot, run_id, manifest)
    if any(name not in {"Review-notes", "Check-report"} for name in (reports or {})):
        raise ValueError("Draft delivery reports must use controller-assigned names")
    contents = {"Delivery-census": delivery, **(reports or {})}
    artifacts = [project]
    report_ids = {}
    for name, value in contents.items():
        data = (
            json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode()
        if len(data) > _MAX_REPORT_BYTES:
            raise ValueError("Draft delivery report exceeds its bounded size")
        relative = f"draft/{run_id}/{name}.json"
        key = put_blob(blobs, data)
        artifact_id = "draft-" + identity({"run_id": run_id, "path": relative, "sha256": key})
        artifacts.append(
            {
                "id": artifact_id,
                "path": relative,
                "kind": "source",
                "sha256": key,
                "title": "Unaccepted draft: " + name,
            }
        )
        report_ids[name] = artifact_id
    return artifacts, {
        "complete": delivery.get("complete") is True,
        "coverage_complete": delivery.get("coverage_complete") is True,
        "compiled": delivery.get("compiled") is True,
        "project_saved": delivery.get("project_saved") is True,
        "counts": delivery["counts"],
        "project_artifact_id": project["id"],
        "census_artifact_id": report_ids["Delivery-census"],
        "report_artifact_ids": report_ids,
    }


def resolve_project_artifact(root: Path, preview: dict[str, Any], artifact: dict[str, Any]) -> Path:
    """Resolve only the authenticated current preview's exact retained run folder."""
    if (
        preview.get("status") != "unaccepted_formalization"
        or artifact.get("kind") != "project"
        or artifact.get("id") != preview.get("delivery", {}).get("project_artifact_id")
    ):
        raise ValueError("Project artifact requires an unaccepted formalization delivery")
    project = _project_path(root, preview["run_id"])
    expected = _project_artifact(root, project, preview["run_id"], project_manifest(project))
    if any(artifact.get(key) != value for key, value in expected.items()):
        raise ValueError("Retained draft project source changed or artifact path is invalid")
    return project


def retain_best_effort(
    root: Path,
    record: dict[str, Any],
    snapshot: Path,
    *,
    layout: dict[str, Any],
    run_id: str,
    selection_token: str,
    model_report: str,
    check_report: Any = None,
    review_notes: Any = None,
    compiled: bool = False,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Attach a full-census draft after the caller has closed all authors, checkers and accounting."""
    from leanflow_cli.formalization.source_draft import freeze_preview

    _project_artifact(root, snapshot, run_id, {})
    delivery = validate_best_effort(snapshot, record, layout=layout)
    if type(compiled) is not bool:
        raise ValueError("Draft compilation status must be a controller boolean")
    if compiled and not compilation_matches(delivery, check_report):
        compiled = False
        delivery["compilation_issue"] = (
            "Independent compilation is missing or refers to earlier project/proposal bytes; repeat the technical check."
        )
    delivery.update(coverage_complete=delivery["complete"], compiled=compiled)
    delivery["complete"] = bool(delivery["complete"] and compiled)
    reports = {}
    if check_report is not None:
        reports["Check-report"] = check_report
    if review_notes is not None:
        reports["Review-notes"] = review_notes
    return freeze_preview(
        root,
        record,
        snapshot,
        layout=layout,
        run_id=run_id,
        selection_token=selection_token,
        model_report=model_report,
        deadline=deadline,
        cancelled=cancelled,
        kind="formalization",
        delivery=delivery,
        reports=reports,
    )
