"""Assemble content-addressed run provenance through focused identity leaves.

The public facade keeps injected Git, runtime-path, and distribution-discovery
seams stable while project, runtime, and validation responsibilities live in
narrow sibling modules.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.cli import run_evidence_validation as _validation
from leanflow_cli.cli import run_runtime_identity as _runtime_identity
from leanflow_cli.cli import run_source_identity as _source_identity
from leanflow_cli.cli.run_build_identity import collect_build_configuration_identity

_parse_count = _validation._parse_count
_sha256_bytes = _source_identity._sha256_bytes
_hash_project_path = _source_identity._hash_project_path
_fallback_source_paths = _source_identity._fallback_source_paths
_source_tree_identity = _source_identity._source_tree_identity
_project_configuration_identity = _source_identity._project_configuration_identity
_content_records = _runtime_identity._content_records
_runtime_source_identity = _runtime_identity._runtime_source_identity
_python_runtime_identity = _runtime_identity._python_runtime_identity
_selected_skills_identity = _runtime_identity._selected_skills_identity
_behavior_config_identity = _runtime_identity._behavior_config_identity
collect_launch_environment = _runtime_identity.collect_launch_environment
_redacted_structure_is_canonical = _validation._redacted_structure_is_canonical
_launch_environment_is_valid = _validation._launch_environment_is_valid
_provenance_is_valid = _validation._provenance_is_valid


def collect_provenance(
    project_root: Path,
    *,
    git_bytes: Callable[..., bytes | None],
    runtime_module_file: str,
    distribution_provider: Callable[[], Iterable[Any]],
) -> dict[str, Any]:
    """Return a content-addressed source, toolchain, and dependency identity."""
    from leanflow_cli import __version__
    from leanflow_cli.workflow import redact_url_credentials

    project_root = project_root.expanduser().resolve()
    project_configuration = _project_configuration_identity(project_root)
    runtime_source = _runtime_source_identity(module_file=runtime_module_file)
    python_runtime = _python_runtime_identity(
        distribution_provider=distribution_provider,
        project_root=project_root,
    )
    runtime_content_sha256 = str(runtime_source["runtime_source_sha256"])
    runtime_source = {
        **runtime_source,
        "runtime_source_scope": "installed-package-content-plus-python-environment",
        "runtime_content_sha256": runtime_content_sha256,
        "runtime_source_sha256": _sha256_bytes(
            json.dumps(
                {
                    "runtime_content_sha256": runtime_content_sha256,
                    "python_runtime_sha256": python_runtime["python_runtime_sha256"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    }
    selected_skills = _selected_skills_identity(project_root)
    behavior_config = _behavior_config_identity(project_root)
    toolchain = ""
    toolchain_status = "missing"
    try:
        toolchain = (project_root / "lean-toolchain").read_text(encoding="utf-8").strip()
        toolchain_status = "complete" if toolchain else "empty"
    except OSError:
        pass
    except UnicodeError:
        toolchain_status = "unreadable"

    dependencies: dict[str, str] = {}
    dependency_sources: dict[str, dict[str, str]] = {}
    dependency_manifest_status = "missing"
    dependency_manifest_raw = b""
    dependencies_complete = False
    try:
        dependency_manifest_raw = (project_root / "lake-manifest.json").read_bytes()
        manifest = json.loads(dependency_manifest_raw.decode("utf-8"))
        packages = manifest.get("packages", []) if isinstance(manifest, Mapping) else []
        if not isinstance(manifest, Mapping) or not isinstance(packages, list):
            raise ValueError("lake manifest does not contain a package list")
        dependencies_complete = True
        for package in packages if isinstance(packages, list) else []:
            if not isinstance(package, Mapping):
                dependencies_complete = False
                continue
            name = str(package.get("name", "") or "")
            if not name:
                dependencies_complete = False
                continue
            if name in dependencies:
                dependencies_complete = False
            revision = str(package.get("rev", "") or package.get("inputRev", "") or "")
            if not revision:
                dependencies_complete = False
            dependencies[name] = revision
            source: dict[str, str] = {}
            for key in ("type", "url", "path", "rev", "inputRev"):
                if package.get(key) in (None, ""):
                    continue
                value = str(package.get(key, "") or "")
                source[key] = redact_url_credentials(value) if key == "url" else value
            dependency_sources[name] = source
        dependency_manifest_status = "complete"
    except FileNotFoundError:
        pass
    except OSError:
        dependency_manifest_status = "unreadable"
    except (UnicodeError, json.JSONDecodeError, ValueError):
        dependency_manifest_status = "invalid"

    dependency_manifest_sha256 = _sha256_bytes(dependency_manifest_raw)
    build_configuration = collect_build_configuration_identity(
        project_root,
        lean_toolchain=toolchain,
        toolchain_status=toolchain_status,
        dependency_manifest_sha256=dependency_manifest_sha256,
        dependency_manifest_status=dependency_manifest_status,
        dependencies_complete=dependencies_complete,
    )

    git_executable_available = shutil.which("git") is not None
    git_marker_present = any(
        (parent / ".git").exists() for parent in (project_root, *project_root.parents)
    )
    repository_probe = git_bytes(project_root, "rev-parse", "--is-inside-work-tree")
    git_repository = bool(repository_probe and repository_probe.strip() == b"true")
    commit = git_bytes(project_root, "rev-parse", "HEAD")
    branch = git_bytes(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    status = git_bytes(project_root, "status", "--porcelain=v1", "-z")
    diff = git_bytes(project_root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    untracked = git_bytes(project_root, "ls-files", "-z", "--others", "--exclude-standard")
    submodules = git_bytes(project_root, "submodule", "status", "--recursive")
    source_tree_sha256, source_file_count, source_scope, source_tree_complete = (
        _source_tree_identity(project_root, git_bytes=git_bytes)
    )
    git_commands = {
        "repository_probe": repository_probe is not None,
        "commit": commit is not None and bool(commit.strip()),
        "branch": branch is not None and bool(branch.strip()),
        "status": status is not None,
        "diff": diff is not None,
        "untracked": untracked is not None,
        "source_index": source_scope == "git-index-plus-untracked" and source_tree_complete,
        "submodules": submodules is not None,
    }
    git_evidence_complete = bool(git_repository and all(git_commands.values()))
    if git_evidence_complete:
        git_evidence_status = "complete"
    elif git_repository or git_marker_present:
        git_evidence_status = "incomplete"
    elif git_executable_available:
        git_evidence_status = "not-a-git-worktree"
    else:
        git_evidence_status = "git-unavailable"
    provenance_complete = bool(
        source_tree_complete
        and project_configuration["project_configuration_complete"] is True
        and runtime_source["runtime_source_complete"] is True
        and python_runtime["python_runtime_complete"] is True
        and selected_skills["selected_skills_complete"] is True
        and behavior_config["behavior_config_complete"] is True
        and toolchain_status == "complete"
        and dependency_manifest_status == "complete"
        and dependencies_complete
        and build_configuration["build_configuration_complete"] is True
        and (
            git_evidence_complete
            or (
                git_evidence_status == "not-a-git-worktree" and source_scope == "lean-project-files"
            )
        )
    )
    dirty_records = [item for item in (status or b"").split(b"\0") if item]
    untracked_records = [item for item in (untracked or b"").split(b"\0") if item]
    identity_payload = {
        "git_commit": (commit or b"").decode("utf-8", errors="replace").strip(),
        "git_diff_sha256": _sha256_bytes(diff or b""),
        "git_untracked_index_sha256": _sha256_bytes(untracked or b""),
        "git_submodules_sha256": _sha256_bytes(submodules or b""),
        "project_manifest_sha256": project_configuration["project_manifest_sha256"],
        "workflow_guidance_sha256": project_configuration["workflow_guidance_sha256"],
        "runtime_source_sha256": runtime_source["runtime_source_sha256"],
        "python_runtime_sha256": python_runtime["python_runtime_sha256"],
        "selected_skills_sha256": selected_skills["selected_skills_sha256"],
        "behavior_config_sha256": behavior_config["behavior_config_sha256"],
        "build_configuration_sha256": build_configuration["build_configuration_sha256"],
        "source_tree_sha256": source_tree_sha256,
        "lean_toolchain": toolchain,
        "dependencies": dict(sorted(dependencies.items())),
        "dependency_sources": dict(sorted(dependency_sources.items())),
        "dependency_manifest_sha256": dependency_manifest_sha256,
    }
    source_identity_sha256 = _sha256_bytes(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {
        "leanflow_version": __version__,
        "project_root": str(project_root),
        "provenance_complete": provenance_complete,
        "git_executable_available": git_executable_available,
        "git_marker_present": git_marker_present,
        "git_repository": git_repository,
        "git_evidence_status": git_evidence_status,
        "git_evidence_complete": git_evidence_complete,
        "git_commands": git_commands,
        "source_tree_complete": source_tree_complete,
        **project_configuration,
        **runtime_source,
        **python_runtime,
        **selected_skills,
        **behavior_config,
        **build_configuration,
        "toolchain_status": toolchain_status,
        "dependency_manifest_status": dependency_manifest_status,
        "dependency_manifest_sha256": identity_payload["dependency_manifest_sha256"],
        "dependencies_complete": dependencies_complete,
        "git_commit": identity_payload["git_commit"],
        "git_branch": (branch or b"").decode("utf-8", errors="replace").strip(),
        "git_dirty": bool(dirty_records),
        "git_dirty_files": len(dirty_records),
        "git_status_sha256": _sha256_bytes(status or b""),
        "git_diff_sha256": identity_payload["git_diff_sha256"],
        "git_diff_bytes": len(diff or b""),
        "git_untracked_files": len(untracked_records),
        "git_untracked_index_sha256": identity_payload["git_untracked_index_sha256"],
        "git_submodules": (submodules or b"").decode("utf-8", errors="replace").strip(),
        "git_submodules_sha256": identity_payload["git_submodules_sha256"],
        "source_identity_scope": source_scope,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": source_file_count,
        "source_identity_sha256": source_identity_sha256,
        "lean_toolchain": toolchain,
        "dependencies": dict(sorted(dependencies.items())),
        "dependency_sources": identity_payload["dependency_sources"],
    }
