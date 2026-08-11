"""Hash project source and ignored LeanFlow project configuration.

This leaf owns Git-index/fallback source-tree identities plus the ignored
project manifest and its resolved workflow-guidance dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

_EXCLUDED_FALLBACK_PARTS = frozenset(
    {".git", ".lake", ".leanflow", ".venv", "node_modules", "__pycache__"}
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_project_path(path: Path) -> tuple[str, int, str]:
    """Hash one source entry without following a symlink out of the project."""
    try:
        if path.is_symlink():
            raw = os.readlink(path).encode("utf-8", errors="surrogateescape")
            return _sha256_bytes(raw), len(raw), "symlink"
        if not path.is_file():
            return _sha256_bytes(b"[missing]"), 0, "missing"
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size, "file"
    except OSError:
        return _sha256_bytes(b"[unreadable]"), 0, "unreadable"


def _fallback_source_paths(project_root: Path) -> tuple[list[str], bool]:
    """Return Lean-project source paths when the project is not in git."""
    paths: list[str] = []
    try:
        candidates = project_root.rglob("*")
        for path in candidates:
            try:
                relative = path.relative_to(project_root)
            except ValueError:
                continue
            if any(part in _EXCLUDED_FALLBACK_PARTS for part in relative.parts):
                continue
            if path.is_symlink() or (
                path.is_file()
                and (
                    path.suffix == ".lean"
                    or path.name
                    in {"lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"}
                )
            ):
                paths.append(relative.as_posix())
    except OSError:
        return [], False
    return sorted(set(paths)), True


def _source_tree_identity(
    project_root: Path, *, git_bytes: Callable[..., bytes | None]
) -> tuple[str, int, str, bool]:
    """Return an aggregate content identity for tracked and untracked sources."""
    raw_paths = git_bytes(
        project_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    if raw_paths is None:
        paths, complete = _fallback_source_paths(project_root)
        scope = "lean-project-files"
    else:
        paths = sorted(
            {
                part.decode("utf-8", errors="surrogateescape")
                for part in raw_paths.split(b"\0")
                if part
            }
        )
        scope = "git-index-plus-untracked"
        complete = True

    digest = hashlib.sha256()
    for relative in paths:
        item_sha256, size, kind = _hash_project_path(project_root / relative)
        if kind == "unreadable":
            complete = False
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), len(paths), scope, complete


def _project_configuration_identity(project_root: Path) -> dict[str, Any]:
    """Hash the ignored project manifest and effective guidance dependencies."""
    manifest_path = project_root / ".leanflow" / "project.yaml"
    missing_sha256 = _sha256_bytes(b"[missing project manifest]")
    empty_guidance_sha256 = _sha256_bytes(b"[]")
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return {
            "project_manifest_status": "missing",
            "project_manifest_sha256": missing_sha256,
            "project_configuration_complete": True,
            "workflow_guidance_status": "not-declared",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": [],
        }

    try:
        resolved_manifest = manifest_path.resolve(strict=True)
        resolved_manifest.relative_to(project_root)
    except (OSError, ValueError):
        return {
            "project_manifest_status": "out-of-root-or-unreadable",
            "project_manifest_sha256": _sha256_bytes(b"[unsafe project manifest]"),
            "project_configuration_complete": False,
            "workflow_guidance_status": "unavailable",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": ["manifest-unavailable"],
        }

    try:
        manifest_raw = resolved_manifest.read_bytes()
        payload = yaml.safe_load(manifest_raw.decode("utf-8")) or {}
    except OSError:
        return {
            "project_manifest_status": "unreadable",
            "project_manifest_sha256": _sha256_bytes(b"[unreadable project manifest]"),
            "project_configuration_complete": False,
            "workflow_guidance_status": "unavailable",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": ["manifest-unreadable"],
        }
    except (UnicodeError, yaml.YAMLError):
        return {
            "project_manifest_status": "invalid",
            "project_manifest_sha256": _sha256_bytes(manifest_raw),
            "project_configuration_complete": False,
            "workflow_guidance_status": "unavailable",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": ["manifest-invalid"],
        }
    manifest_sha256 = _sha256_bytes(manifest_raw)
    if not isinstance(payload, Mapping):
        return {
            "project_manifest_status": "invalid",
            "project_manifest_sha256": manifest_sha256,
            "project_configuration_complete": False,
            "workflow_guidance_status": "unavailable",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": ["manifest-not-a-mapping"],
        }

    raw_entries = payload.get("workflow_guidance", [])
    if raw_entries in (None, []):
        raw_entries = []
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        return {
            "project_manifest_status": "complete",
            "project_manifest_sha256": manifest_sha256,
            "project_configuration_complete": False,
            "workflow_guidance_status": "invalid",
            "workflow_guidance_sha256": empty_guidance_sha256,
            "workflow_guidance_files": [],
            "workflow_guidance_issues": ["workflow-guidance-not-a-list"],
        }

    records: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            issues.append(f"entry-{index}-not-a-mapping")
            continue
        declared = str(raw_entry.get("path", "") or "").strip()
        if not declared:
            issues.append(f"entry-{index}-path-missing")
            continue
        try:
            candidate = (project_root / declared).resolve(strict=True)
            candidate.relative_to(project_root)
            if not candidate.is_file():
                raise OSError("guidance path is not a file")
            raw = candidate.read_bytes()
            raw.decode("utf-8")
        except ValueError:
            issues.append(f"entry-{index}-out-of-root")
            continue
        except (OSError, UnicodeError):
            issues.append(f"entry-{index}-unreadable")
            continue
        records.append(
            {
                "declared_path": declared,
                "resolved_path": candidate.relative_to(project_root).as_posix(),
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
            }
        )
    guidance_encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    guidance_status = "complete" if records else "not-declared"
    if issues:
        guidance_status = "incomplete"
    return {
        "project_manifest_status": "complete",
        "project_manifest_sha256": manifest_sha256,
        "project_configuration_complete": not issues,
        "workflow_guidance_status": guidance_status,
        "workflow_guidance_sha256": _sha256_bytes(guidance_encoded),
        "workflow_guidance_files": records,
        "workflow_guidance_issues": issues,
    }
