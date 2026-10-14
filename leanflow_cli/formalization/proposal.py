"""Freeze author proposals and independently check their project and source identities."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.formalization.compiled_evidence import (
    assert_rebuild_artifacts,
    assert_reuse_sources_unchanged,
    merge_reused_declarations,
)
from leanflow_cli.formalization.dependency_snapshot import create_formalization_snapshot
from leanflow_cli.formalization.environment import LIBRARY_SEMANTICS, environment_identity
from leanflow_cli.formalization.evidence import contained, digest, put_blob
from leanflow_cli.formalization.lean_build import build_project
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.formalization.lean_layout import (
    generation_layout,
    module_for_file,
    resolve_layout,
)
from leanflow_cli.formalization.lean_policy import POLICY_VERSION, inspect_dependency, inspect_draft
from leanflow_cli.formalization.proposal_schema import apply_proposal, bind_declarations
from leanflow_cli.formalization.publication import publication_guard
from leanflow_cli.formalization.publication_io import PublicationConflict
from leanflow_cli.formalization.records import (
    assert_source_current,
    document_lock,
    load_document,
    new_revision,
    rehydrate_document,
    save_receipt,
    save_revision,
)
from leanflow_cli.formalization.review_scope import bind_review_scope
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation
from leanflow_cli.workflows.prover.source import lean_code_mask


def owned_path(layout: dict[str, Any], relative: str) -> bool:
    """Recognize only the explicitly allocated document module tree."""
    return relative == layout["aggregator"] or (
        relative.startswith(layout["owned_prefix"] + "/") and relative.endswith(".lean")
    )


def _operational_failure(result: dict[str, Any]) -> tuple[str, str] | None:
    """Recover process failure provenance without treating Lean diagnostics as infrastructure."""
    code = result.get("error_code")
    if code in {
        "check_cancelled",
        "check_timeout",
        "check_setup_failed",
        "isolation_unavailable",
        "check_artifact_drift",
        "needs_reprepare",
    }:
        return str(code), str(result.get("error", code))
    if result.get("timed_out"):
        return "check_timeout", str(result.get("error", "Lean checking exceeded its deadline"))
    for value in result.values():
        if isinstance(value, dict) and (failure := _operational_failure(value)):
            return failure
    return None


def check_proposal(
    *,
    root: Path,
    snapshot: Path,
    baseline: dict[str, bytes],
    directory: Path,
    document_id: str,
    base_revision_id: str,
    selection_token: str,
    allowed_axioms: tuple[str, ...],
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    review_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a checked immutable revision; every submission replaces prior selection."""
    selected_attempt: dict[str, Any] | None = None
    stage = "submission"

    def rejected(result: dict[str, Any]) -> dict[str, Any]:
        """Retain a terminal check result on the exact submitted revision."""
        failure = _operational_failure(result)
        if operation_cancelled() or (cancelled is not None and cancelled()):
            failure = ("check_cancelled", "Formalization checking was cancelled")
        elif deadline is not None and remaining_seconds(deadline) == 0:
            failure = ("check_timeout", "Formalization checking exceeded its deadline")
        result = {**result, "status": "check_error" if failure else "check_rejected"}
        if failure:
            result.update(error_code=failure[0], error=failure[1])
        if selected_attempt is not None:
            save_receipt(
                selected_attempt,
                {
                    "schema_version": 1,
                    "document_id": document_id,
                    "revision_id": selected_attempt["revision_id"],
                    "status": result["status"],
                    "checks": result,
                    "reviews": [],
                    "handoff": None,
                },
            )
            result = {**result, "revision_id": selected_attempt["revision_id"]}
        return result

    try:
        # Editor actions acquire project ownership first; match that order before
        # stage admission reenters the project lock while this document is owned.
        with publication_guard(root), document_lock(root, document_id) as store:
            from leanflow_cli.formalization.records import current_selection_token

            if current_selection_token(root, document_id) != selection_token:
                return {
                    "accepted": False,
                    "error_code": "selection_changed",
                    "error": "Source inventory selection changed; restart from its selected revision",
                }
            base = load_document(root, document_id, base_revision_id)
            rehydrate_document(base, store)
            base["_directory"] = str(store)
            selected = load_document(root, document_id)
            # A preceding candidate from this run may be selected; a UI/source
            # edit from another actor must not be silently overwritten.
            if (
                selected["revision_id"] != base_revision_id
                and selected.get("author_base_revision") != base_revision_id
            ):
                return {
                    "accepted": False,
                    "error_code": "selection_changed",
                    "error": "Source inventory changed; restart from its selected revision",
                }
            attempt = new_revision(base)
            attempt.update(
                author_base_revision=base_revision_id,
                status="submission_pending",
                submission_id=uuid.uuid4().hex,
            )
            selected_attempt = save_revision(store, attempt)
            if operation_cancelled() or (cancelled is not None and cancelled()):
                return rejected({"accepted": False, "error_code": "check_cancelled"})
            if deadline is not None and remaining_seconds(deadline) == 0:
                return rejected({"accepted": False, "error_code": "check_timeout"})
            try:
                assert_source_current(root, base)
            except (OSError, ValueError) as error:
                raise RepreparationRequired(str(error)) from error
            proposal_path = contained(snapshot, ".leanflow-job/formalization-proposal.json")
            proposal = json.loads(proposal_path.read_text())
            from leanflow_cli.formalization.slice_gate import require_stage

            require_stage(root, base, proposal.get("phase"))
            candidate = apply_proposal(base, proposal)
            candidate = new_revision(candidate)
            candidate["author_base_revision"] = base_revision_id
            stage = "environment"
            candidate["layout"] = layout = generation_layout(
                resolve_layout(root, document_id), base
            )
            if not layout.get("accepted"):
                return rejected(
                    layout
                    if _operational_failure(layout)
                    else {**layout, "error_code": "check_setup_failed"}
                )
            stage = "submission"
            proposed = source_files(snapshot)
            if baseline.keys() - proposed.keys():
                raise ValueError("Formalization cannot delete existing project sources")
            changed = {name: data for name, data in proposed.items() if baseline.get(name) != data}
            if any(not owned_path(layout, name) for name in changed):
                raise ValueError("Author edits must remain in the allocated document module tree")
            modules = sorted(name for name in proposed if owned_path(layout, name))
            if modules and layout["aggregator"] not in modules:
                raise ValueError("Generated modules require their document import aggregator")
            if modules:
                aggregator_imports: set[str] = set()
                for line in lean_code_mask(proposed[layout["aggregator"]].decode()).splitlines():
                    if line.strip().startswith("import "):
                        aggregator_imports.update(line.strip().split()[1:])
                missing = {
                    module_for_file(layout, name)
                    for name in modules
                    if name != layout["aggregator"]
                } - aggregator_imports
                if missing:
                    raise ValueError(
                        "Document aggregator must import every generated module: "
                        + ", ".join(sorted(missing))
                    )
                required_import = f"import {layout['module_prefix']}\n".encode()
                root_file = layout["root_file"]
                root_before = baseline.get(root_file, b"")
                if required_import.strip() not in root_before.splitlines():
                    # The controller owns this bounded import registration.
                    changed[root_file] = required_import + root_before
            owned = {name: proposed[name] for name in modules}
            owned.update(changed)
            candidate["lean_files"] = {
                name: put_blob(store / "blobs", data) for name, data in owned.items()
            }
            candidate["context_lean_files"] = {
                name: put_blob(store / "blobs", data)
                for name, data in {**proposed, **changed}.items()
                if name.endswith(".lean")
            }
            candidate["project_source_hashes"] = {
                name: digest(data) for name, data in {**proposed, **changed}.items()
            }
            stage = "environment"
            candidate["environment_id"] = environment_identity(root)
            candidate["library_semantics"] = LIBRARY_SEMANTICS
            candidate["allowed_axioms"] = list(allowed_axioms)
            candidate["review_policy"] = review_policy or {}
            candidate["status"] = "checking"
            # Select the exact submitted bytes before checking. A rejected B
            # therefore cannot accidentally leave an accepted A selected.
            candidate = save_revision(store, candidate)
            selected_attempt = candidate
            if source_files(root) != baseline:
                raise RepreparationRequired("Canonical project sources changed during submission")
            generation = lean_environment_generation(root)
            frozen = create_formalization_snapshot(
                root,
                directory / uuid.uuid4().hex / "project",
                environment=environment_identity,
                expected_environment=candidate["environment_id"],
            )
            for relative, data in changed.items():
                target = contained(frozen, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            checks: dict[str, Any] = {
                "accepted": True,
                "declarations": [],
                "obligations": [],
                "profiles": {},
                "modules": {},
                "policy_id": POLICY_VERSION,
                "checker_sha256": digest(INSPECTOR),
                "allowed_axioms": sorted(set(allowed_axioms) - {"sorryAx"}),
            }
            if modules:
                checks = inspect_draft(
                    frozen,
                    modules,
                    frozen.parent / "checks",
                    allowed_axioms=allowed_axioms,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            if not checks.get("accepted"):
                return rejected({**checks, "revision_id": candidate["revision_id"]})
            build_directory = contained(frozen, layout["build_dir"])
            build_directory.mkdir(parents=True, exist_ok=True)
            build_workspace = frozen.parent / "whole-project"
            build_workspace.mkdir(parents=True, exist_ok=True)
            build = build_project(
                frozen, layout, build_workspace, deadline=deadline, cancelled=cancelled
            )
            if not build.get("success"):
                return rejected(
                    {
                        "accepted": False,
                        "revision_id": candidate["revision_id"],
                        "error": "Final whole-project Lean build failed",
                        "build": build,
                    }
                )
            if modules:
                try:
                    assert_rebuild_artifacts(
                        checks.get("local_artifacts"), build.get("local_artifacts")
                    )
                except ValueError as error:
                    return rejected(
                        {
                            "accepted": False,
                            "error_code": "check_artifact_drift",
                            "error": str(error),
                        }
                    )
            checks["project_build"] = {
                "accepted": True,
                "authority": build.get("authority"),
                "modules": build.get("modules", []),
                "excluded_sources": build.get("excluded_sources", []),
            }
            stage = "binding"
            reused: dict[tuple[str, ...], list[str]] = {}
            for unit in candidate["units"]:
                for ref in unit["declarations"]:
                    relative = ref.get("file")
                    if relative and not owned_path(layout, relative):
                        if relative not in baseline or not relative.endswith(".lean"):
                            raise ValueError(
                                "Existing project reuse must identify an unchanged Lean source"
                            )
                        module = module_for_file(layout, relative)
                        imports = ref.get("imports", [module])
                        if module not in imports:
                            raise ValueError(
                                "Existing project reuse must import its recorded source module"
                            )
                        ref["imports"] = imports
                    if not relative or not owned_path(layout, relative):
                        imports = ref.get("imports")
                        if not isinstance(imports, list) or any(
                            not isinstance(name, str) for name in imports
                        ):
                            raise ValueError("Library imports must be explicit module names")
                        reused.setdefault(tuple(imports), []).append(ref["name"])
            for imports, names in reused.items():
                result = inspect_dependency(
                    frozen,
                    sorted(set(names)),
                    frozen.parent / "dependencies",
                    imports=list(imports),
                    allowed_axioms=allowed_axioms,
                    deadline=deadline,
                    cancelled=cancelled,
                    local_artifacts=build.get("local_artifacts", {}),
                )
                if not result.get("accepted"):
                    return rejected({**result, "revision_id": candidate["revision_id"]})
                if any(
                    row.get("file") and owned_path(layout, row["file"])
                    for row in result["declarations"]
                ):
                    raise ValueError(
                        "Generated declarations must retain their owned source mapping"
                    )
                assert_reuse_sources_unchanged(result["declarations"], baseline, changed)
                merge_reused_declarations(checks, result)
                checks.setdefault("dependency_replays", []).append(
                    {
                        "imports": list(imports),
                        "names": sorted(set(names)),
                        "kernel_replay": result.get("kernel_replay"),
                        "environment_id": candidate["environment_id"],
                    }
                )
            bind_declarations(candidate, checks)
            if (
                candidate.get("workflow_protocol") == 2
                and candidate["proposal_phase"] == "complete"
            ):
                from leanflow_cli.formalization.slice_gate import assert_complete_slice

                candidate["checks"] = checks
                assert_complete_slice(root, candidate)
            if source_files(root) != baseline or lean_environment_generation(root) != generation:
                raise RepreparationRequired(
                    "Project or dependency inputs changed during verification"
                )
            if (
                environment_identity(root) != candidate["environment_id"]
                or environment_identity(frozen) != candidate["environment_id"]
            ):
                raise RepreparationRequired(
                    "Compiled dependency inputs changed during verification"
                )
            try:
                assert_source_current(root, base)
            except (OSError, ValueError) as error:
                raise RepreparationRequired(str(error)) from error
            candidate.pop("revision_id")
            candidate.update(checks=checks, status="awaiting_review")
            bind_review_scope(root, candidate)
            candidate = save_revision(store, candidate)
            selected_attempt = candidate
            from leanflow_cli.formalization.handoff import build_handoff

            # Reject an unlocatable proof slot before spending source-review
            # calls. Publication must never discover new acceptance blockers.
            build_handoff(
                frozen,
                document_id=document_id,
                revision_id=candidate["revision_id"],
                registry=checks,
                files={
                    name: value
                    for name, value in candidate["project_source_hashes"].items()
                    if name.endswith(".lean")
                },
                environment_id=candidate["environment_id"],
                allowed_axioms=tuple(candidate["allowed_axioms"]),
            )
            return {
                "accepted": True,
                "proposal_phase": candidate.get("proposal_phase", "complete"),
                "revision_id": candidate["revision_id"],
                "document_id": document_id,
                "files": {name: data.decode() for name, data in owned.items()},
                "profiles": checks["profiles"],
                "record": candidate,
                "frozen_root": str(frozen),
                "environment_generation": generation,
                "selection_token": selection_token,
            }
    except (PublicationConflict, RepreparationRequired) as exc:
        return rejected({"accepted": False, "error_code": "needs_reprepare", "error": str(exc)})
    except subprocess.SubprocessError as exc:
        return rejected(
            {
                "accepted": False,
                "error_code": (
                    "check_timeout"
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else "check_setup_failed"
                ),
                "error": str(exc),
            }
        )
    except (InterruptedError, TimeoutError) as exc:
        return rejected(
            {
                "accepted": False,
                "error_code": (
                    "check_cancelled" if isinstance(exc, InterruptedError) else "check_timeout"
                ),
                "error": str(exc),
            }
        )
    except OSError as exc:
        return rejected(
            {
                "accepted": False,
                **({"error_code": "check_setup_failed"} if stage != "submission" else {}),
                "error": str(exc),
            }
        )
    except (ValueError, KeyError, TypeError) as exc:
        return rejected(
            {
                "accepted": False,
                **({"error_code": "check_setup_failed"} if stage == "environment" else {}),
                "error": str(exc),
            }
        )
