"""Project provisional source-to-Lean navigation from exact frozen delivery evidence only."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.draft_delivery import compilation_matches
from leanflow_cli.formalization.evidence import contained, digest, identity
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.records import document_directory
from leanflow_cli.formalization.source_draft import _authenticate_preview
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES, read_regular

_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MAX_REFS = 128


def _hash(value: Any) -> bool:
    """Recognize an exact frozen content identity."""
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _issue(mapping: dict[str, Any], message: str, *, field: str = "issues") -> None:
    """Bound repeated display diagnostics without hiding that more evidence needs inspection."""
    message = message[:4000]
    issues = mapping[field]
    if message not in issues and len(issues) < 32:
        issues.append(message)
    elif len(issues) == 32:
        issues.append("Additional navigation gaps remain; inspect the full frozen proposal.")


def _blob(directory: Path, artifact: dict[str, Any], limit: int) -> bytes:
    """Read bounded immutable bytes without reading the retained live project."""
    key = artifact.get("sha256")
    if not isinstance(key, str) or not _hash(key):
        raise ValueError("Draft navigation artifact has no exact content identity")
    data = read_regular(contained(directory, "blobs/" + key), limit)
    if digest(data) != key:
        raise ValueError("Draft navigation artifact bytes changed")
    return data


def _report(directory: Path, preview: dict[str, Any], name: str) -> dict[str, Any]:
    """Resolve reports only by their authenticated preview IDs and exact run paths."""
    report_ids = preview["delivery"].get("report_artifact_ids")
    if not isinstance(report_ids, dict):
        raise ValueError("Draft navigation reports are unavailable")
    key = report_ids.get(name)
    matches = [row for row in preview["artifacts"] if row.get("id") == key]
    if (
        len(matches) != 1
        or matches[0].get("kind") != "source"
        or matches[0].get("path") != f"draft/{preview['run_id']}/{name}.json"
    ):
        raise ValueError("Draft navigation needs the frozen " + name + " report")
    value = json.loads(_blob(directory, matches[0], _MAX_REPORT_BYTES))
    if not isinstance(value, dict):
        raise ValueError("Draft navigation report must be an object")
    return value


def _reference(
    directory: Path,
    authored: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    artifacts: list[dict[str, Any]],
    manifest: dict[str, str],
    lines: dict[str, int],
) -> dict[str, Any]:
    """Use compiled identities and locations, never an author's navigation coordinates."""
    name = authored.get("name")
    row = registry.get(name) if isinstance(name, str) and len(name) <= 1024 else None
    if row is None or not all(_hash(row.get(key)) for key in ("type_hash", "body_hash")):
        raise ValueError("No exact compiled declaration for " + str(name)[:1024])
    result = {key: row[key] for key in ("name", "type_hash", "body_hash")}
    pretty = row.get("pretty_type") or row.get("type")
    if isinstance(pretty, str) and len(pretty) <= 1_000_000:
        result["type"] = pretty
    file = authored.get("file")
    if not file:
        if (
            row.get("declaration_origin", "generated" if row.get("file") else "dependency")
            != "dependency"
        ):
            raise ValueError("A generated declaration needs its exact frozen file: " + str(name))
        result["declaration_origin"] = "dependency"
        return result
    if not isinstance(file, str) or row.get("file") != file:
        raise ValueError("Draft and compiled declaration files differ: " + str(name))
    matches = [artifact for artifact in artifacts if artifact.get("path") == file]
    if (
        len(matches) != 1
        or matches[0].get("kind") != "lean"
        or matches[0].get("sha256") != manifest.get(file)
        or not _hash(manifest.get(file))
    ):
        raise ValueError("No exact frozen Lean artifact for " + file)
    artifact = matches[0]
    if file not in lines:
        lines[file] = _blob(directory, artifact, MAX_DRAFT_FILE_BYTES).count(b"\n") + 1
    line = row.get("line_start")
    if type(line) is not int or not 1 <= line <= lines[file]:
        raise ValueError("No valid compiled declaration line for " + str(name))
    result.update(file=file, artifact_id=artifact["id"], line_start=line)
    return result


def draft_item_mappings(
    root: Path, record: dict[str, Any], preview: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Expose saved unaccepted mappings separately, preserving all items and visible gaps."""
    if not preview or preview.get("status") != "unaccepted_formalization":
        return {}
    items = complete_inventory(record)["items"]
    result = {
        item["id"]: {
            "preview_id": preview["preview_id"],
            "lean_refs": [],
            "issues": [],
            "notes": [],
        }
        for item in items
    }
    try:
        _authenticate_preview(preview, preview["run_id"])
        if (
            not isinstance(preview.get("delivery"), dict)
            or not isinstance(preview.get("artifacts"), list)
            or any(not isinstance(row, dict) for row in preview["artifacts"])
        ):
            raise ValueError("Draft navigation metadata is malformed")
        if any(
            preview.get(field, "") != record.get(target, "")
            for field, target in (
                ("document_id", "document_id"),
                ("base_revision_id", "revision_id"),
                ("source_bundle", "source_bundle"),
                ("preparation_id", "preparation_id"),
            )
        ):
            raise ValueError("Draft navigation belongs to another source revision")
        for artifact in preview["artifacts"]:
            if artifact.get("kind") != "project" and artifact.get("id") != "draft-" + identity(
                {
                    "run_id": preview["run_id"],
                    "path": artifact["path"],
                    "sha256": artifact["sha256"],
                }
            ):
                raise ValueError("Draft navigation artifact belongs to another preview")
        directory = document_directory(root, record["document_id"])
        census = _report(directory, preview, "Delivery-census")
        report = _report(directory, preview, "Check-report")
        if (
            not _hash(census.get("proposal_sha256"))
            or preview["delivery"].get("compiled") is not True
            or not compilation_matches(census, report)
        ):
            raise ValueError("Draft navigation has no matching saved compilation check")
        checks = report.get("checks")
        if (
            not isinstance(checks, dict)
            or checks.get("accepted") is not True
            or not isinstance(checks.get("project_build"), dict)
            or checks["project_build"].get("success") is not True
        ):
            raise ValueError("Draft navigation needs independent Lean and project build checks")
        rows = checks.get("declarations")
        if (
            not isinstance(rows, list)
            or len(rows) > 16384
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not 1 <= len(row["name"]) <= 1024
                for row in rows
            )
        ):
            raise ValueError("Draft compiled declaration registry is malformed")
        registry = {row["name"]: row for row in rows}
        if len(registry) != len(rows):
            raise ValueError("Draft compiled declarations have duplicate identities")
        manifest = census.get("project_manifest")
        proposed = census.get("raw_proposal")
        units = proposed.get("units") if isinstance(proposed, dict) else None
        if (
            not isinstance(manifest, dict)
            or not isinstance(units, list)
            or len(units) > 512
            or census.get("source_ids") != sorted(result)
        ):
            raise ValueError("Draft mapping proposal is unavailable or exceeds navigation bounds")
        projects = [row for row in preview["artifacts"] if row.get("kind") == "project"]
        if len(projects) != 1 or projects[0].get("sha256") != identity(manifest):
            raise ValueError("Draft navigation differs from the saved project manifest")
        lines: dict[str, int] = {}
        oversized: set[str] = set()
        excluded = set(census.get("excluded_source_ids", []))
        for unit in units:
            linked = unit.get("required_source_ids") if isinstance(unit, dict) else None
            refs = unit.get("declarations") if isinstance(unit, dict) else None
            if (
                not isinstance(linked, list)
                or not all(
                    isinstance(key, str) and key in result and key not in excluded for key in linked
                )
                or len(set(linked)) != len(linked)
                or not isinstance(refs, list)
                or not 1 <= len(refs) <= _MAX_REFS
            ):
                raise ValueError("Draft unit has malformed source or declaration mappings")
            if (
                isinstance(unit.get("representation_note"), str)
                and unit["representation_note"].strip()
            ):
                for key in linked:
                    _issue(
                        result[key],
                        "Provisional representation: " + unit["representation_note"],
                        field="notes",
                    )
            for authored in refs:
                try:
                    if not isinstance(authored, dict):
                        raise ValueError("Draft declaration mapping is malformed")
                    ref = _reference(
                        directory, authored, registry, preview["artifacts"], manifest, lines
                    )
                except (OSError, ValueError, KeyError, TypeError) as error:
                    for key in linked:
                        _issue(result[key], str(error))
                    continue
                for key in linked:
                    mapped = result[key]["lean_refs"]
                    if key not in oversized and ref not in mapped:
                        mapped.append(ref)
                        if len(mapped) > _MAX_REFS:
                            oversized.add(key)
                            mapped.clear()
                            _issue(
                                result[key],
                                "Draft navigation exceeds 128 declarations; inspect the full proposal.",
                            )
        clauses = obligation_registry(record)
        for key in census.get("unresolved_clause_ids", []):
            clause = clauses.get(key) if isinstance(key, str) else None
            if clause is not None and clause["source_id"] in result:
                _issue(
                    result[clause["source_id"]], "Unresolved draft clause: " + key, field="notes"
                )
        for item in items:
            mapping = result[item["id"]]
            if not mapping["lean_refs"] and not mapping["issues"]:
                mapping["issues"].append(
                    "Explicitly excluded source item."
                    if item.get("scope") == "excluded"
                    else "No saved draft declaration mapping is available."
                )
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        for mapping in result.values():
            mapping["lean_refs"] = []
            mapping["issues"] = ["Draft navigation unavailable: " + str(error)[:4000]]
    return result
