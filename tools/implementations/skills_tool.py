#!/usr/bin/env python3
"""
Skills Tool Module

This module provides tools for listing and viewing skill documents.
Skills are organized as directories containing a SKILL.md file (the main instructions)
and optional supporting files like references, templates, and examples.

Inspired by Anthropic's Claude Skills system with progressive disclosure architecture:
- Metadata (name ≤64 chars, description ≤1024 chars) - shown in skills_list
- Full Instructions - loaded via skill_view when needed
- Linked Files (references, templates) - loaded on demand

Directory Structure:
    skills/
    ├── my-skill/
    │   ├── SKILL.md           # Main instructions (required)
    │   ├── references/        # Supporting documentation
    │   │   ├── api.md
    │   │   └── examples.md
    │   ├── templates/         # Templates for output
    │   │   └── template.md
    │   └── assets/            # Supplementary files (agentskills.io standard)
    └── category/              # Category folder for organization
        └── another-skill/
            └── SKILL.md

SKILL.md Format (YAML Frontmatter, agentskills.io compatible):
    ---
    name: skill-name              # Required, max 64 chars
    description: Brief description # Required, max 1024 chars
    version: 1.0.0                # Optional
    license: MIT                  # Optional (agentskills.io)
    platforms: [macos]            # Optional — restrict to specific OS platforms
                                  #   Valid: macos, linux, windows
                                  #   Omit to load on all platforms (default)
    prerequisites:                # Optional — legacy runtime requirements
      env_vars: [API_KEY]         #   Legacy env var names are normalized into
                                  #   required_environment_variables on load.
      commands: [curl, jq]        #   Command checks remain advisory only.
    compatibility: Requires X     # Optional (agentskills.io)
    metadata:                     # Optional, arbitrary key-value (agentskills.io)
      leanflow:
        tags: [fine-tuning, llm]
        related_skills: [peft, lora]
    ---

    # Skill Title

    Full instructions and content here...

Available tools:
- skills_list: List skills with metadata (progressive disclosure tier 1)
- skill_view: Load full skill content (progressive disclosure tier 2-3)

Usage:
    from tools.implementations.skills_tool import skills_list, skill_view, check_skills_requirements

    # List all skills (returns metadata only - token efficient)
    result = skills_list()

    # View a skill's main content (loads full instructions)
    content = skill_view("axolotl")

    # View a reference file within a skill (loads linked file)
    content = skill_view("axolotl", "references/dataset-formats.md")
"""

import json
import logging
import os
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from leanflow_cli.config import _ENV_VAR_NAME_RE, get_leanflow_home, load_config, load_env
from leanflow_cli.lean.lean_workflow_specs import specs_for_skill
from leanflow_cli.runtime.skill_core import discover_skills as _og_discover_skills
from leanflow_cli.runtime.skill_core import load_skill as _og_load_skill
from leanflow_cli.runtime.skill_core import load_skill_file as _og_load_skill_file
from tools.registry import registry

logger = logging.getLogger(__name__)


# All skills live in ~/.leanflow/skills/ (seeded from bundled skills/ on install).
# This is the single source of truth -- agent edits, hub installs, and bundled
# skills all coexist here without polluting the git repo.
LEANFLOW_HOME_DIR = get_leanflow_home()
SKILLS_DIR = LEANFLOW_HOME_DIR / "skills"

# Anthropic-recommended limits for progressive disclosure efficiency
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

# Platform identifiers for the 'platforms' frontmatter field.
# Maps user-friendly names to sys.platform prefixes.
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
_EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))
_REMOTE_ENV_BACKENDS = frozenset({"singularity", "ssh", "daytona"})
_secret_capture_callback = None


class SkillReadinessStatus(str, Enum):
    AVAILABLE = "available"
    SETUP_NEEDED = "setup_needed"
    UNSUPPORTED = "unsupported"


def set_secret_capture_callback(callback) -> None:
    global _secret_capture_callback
    _secret_capture_callback = callback


def skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    """Check if a skill is compatible with the current OS platform.

    Skills declare platform requirements via a top-level ``platforms`` list
    in their YAML frontmatter::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    Valid values: ``macos``, ``linux``, ``windows``.

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True  # No restriction → loads everywhere
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for p in platforms:
        mapped = _PLATFORM_MAP.get(str(p).lower().strip(), str(p).lower().strip())
        if current.startswith(mapped):
            return True
    return False


def _normalize_prerequisite_values(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item) for item in value if str(item).strip()]


def _collect_prerequisite_values(
    frontmatter: dict[str, Any],
) -> tuple[list[str], list[str]]:
    prereqs = frontmatter.get("prerequisites")
    if not prereqs or not isinstance(prereqs, dict):
        return [], []
    return (
        _normalize_prerequisite_values(prereqs.get("env_vars")),
        _normalize_prerequisite_values(prereqs.get("commands")),
    )


def _normalize_setup_metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    setup = frontmatter.get("setup")
    if not isinstance(setup, dict):
        return {"help": None, "collect_secrets": []}

    help_text = setup.get("help")
    normalized_help = (
        str(help_text).strip() if isinstance(help_text, str) and help_text.strip() else None
    )

    collect_secrets_raw = setup.get("collect_secrets")
    if isinstance(collect_secrets_raw, dict):
        collect_secrets_raw = [collect_secrets_raw]
    if not isinstance(collect_secrets_raw, list):
        collect_secrets_raw = []

    collect_secrets: list[dict[str, Any]] = []
    for item in collect_secrets_raw:
        if not isinstance(item, dict):
            continue

        env_var = str(item.get("env_var") or "").strip()
        if not env_var:
            continue

        prompt = str(item.get("prompt") or f"Enter value for {env_var}").strip()
        provider_url = str(item.get("provider_url") or item.get("url") or "").strip()

        entry: dict[str, Any] = {
            "env_var": env_var,
            "prompt": prompt,
            "secret": bool(item.get("secret", True)),
        }
        if provider_url:
            entry["provider_url"] = provider_url
        collect_secrets.append(entry)

    return {
        "help": normalized_help,
        "collect_secrets": collect_secrets,
    }


def _get_required_environment_variables(
    frontmatter: dict[str, Any],
    legacy_env_vars: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect and normalize environment variable requirements from frontmatter, merging current `required_environment_variables`, `setup.collect_secrets`, and legacy `prerequisites.env_vars` sources. Deduplicates by env name, validates against _ENV_VAR_NAME_RE, and adds default prompts plus optional help and required_for metadata."""
    setup = _normalize_setup_metadata(frontmatter)
    required_raw = frontmatter.get("required_environment_variables")
    if isinstance(required_raw, dict):
        required_raw = [required_raw]
    if not isinstance(required_raw, list):
        required_raw = []

    required: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_required(entry: dict[str, Any]) -> None:
        env_name = str(entry.get("name") or entry.get("env_var") or "").strip()
        if not env_name or env_name in seen:
            return
        if not _ENV_VAR_NAME_RE.match(env_name):
            return

        normalized: dict[str, Any] = {
            "name": env_name,
            "prompt": str(entry.get("prompt") or f"Enter value for {env_name}").strip(),
        }

        help_text = (
            entry.get("help") or entry.get("provider_url") or entry.get("url") or setup.get("help")
        )
        if isinstance(help_text, str) and help_text.strip():
            normalized["help"] = help_text.strip()

        required_for = entry.get("required_for")
        if isinstance(required_for, str) and required_for.strip():
            normalized["required_for"] = required_for.strip()

        seen.add(env_name)
        required.append(normalized)

    for item in required_raw:
        if isinstance(item, str):
            _append_required({"name": item})
            continue
        if isinstance(item, dict):
            _append_required(item)

    for item in setup["collect_secrets"]:
        _append_required(
            {
                "name": item.get("env_var"),
                "prompt": item.get("prompt"),
                "help": item.get("provider_url") or setup.get("help"),
            }
        )

    if legacy_env_vars is None:
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
    for env_var in legacy_env_vars:
        _append_required({"name": env_var})

    return required


def _capture_required_environment_variables(
    skill_name: str,
    missing_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Invoke the registered secret capture callback for each missing env var entry, returning a result dict with remaining unset names and setup_skipped flag. Suppresses callback exceptions and marks entries as failed when callback returns success=false or raises."""
    if not missing_entries:
        return {
            "missing_names": [],
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    missing_names = [entry["name"] for entry in missing_entries]
    if _secret_capture_callback is None:
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    setup_skipped = False
    remaining_names: list[str] = []

    for entry in missing_entries:
        metadata = {"skill_name": skill_name}
        if entry.get("help"):
            metadata["help"] = entry["help"]
        if entry.get("required_for"):
            metadata["required_for"] = entry["required_for"]

        try:
            callback_result = _secret_capture_callback(
                entry["name"],
                entry["prompt"],
                metadata,
            )
        except Exception:
            logger.warning(f"Secret capture callback failed for {entry['name']}", exc_info=True)
            callback_result = {
                "success": False,
                "stored_as": entry["name"],
                "validated": False,
                "skipped": True,
            }

        success = isinstance(callback_result, dict) and bool(callback_result.get("success"))
        skipped = isinstance(callback_result, dict) and bool(callback_result.get("skipped"))
        if success and not skipped:
            continue

        setup_skipped = True
        remaining_names.append(entry["name"])

    return {
        "missing_names": remaining_names,
        "setup_skipped": setup_skipped,
        "gateway_setup_hint": None,
    }


def _get_terminal_backend_name() -> str:
    return str(os.getenv("TERMINAL_ENV", "local")).strip().lower() or "local"


def _is_env_var_persisted(var_name: str, env_snapshot: dict[str, str] | None = None) -> bool:
    if env_snapshot is None:
        env_snapshot = load_env()
    if var_name in env_snapshot:
        return bool(env_snapshot.get(var_name))
    return bool(os.getenv(var_name))


def _remaining_required_environment_names(
    required_env_vars: list[dict[str, Any]],
    capture_result: dict[str, Any],
    *,
    env_snapshot: dict[str, str] | None = None,
    backend: str | None = None,
) -> list[str]:
    if backend is None:
        backend = _get_terminal_backend_name()
    missing_names = set(capture_result["missing_names"])
    if backend in _REMOTE_ENV_BACKENDS:
        return [entry["name"] for entry in required_env_vars]

    if env_snapshot is None:
        env_snapshot = load_env()
    remaining = []
    for entry in required_env_vars:
        name = entry["name"]
        if name in missing_names or not _is_env_var_persisted(name, env_snapshot):
            remaining.append(name)
    return remaining


def _build_setup_note(
    readiness_status: SkillReadinessStatus,
    missing: list[str],
    setup_help: str | None = None,
) -> str | None:
    if readiness_status == SkillReadinessStatus.SETUP_NEEDED:
        missing_str = ", ".join(missing) if missing else "required prerequisites"
        note = f"Setup needed before using this skill: missing {missing_str}."
        if setup_help:
            return f"{note} {setup_help}"
        return note
    return None


def _backend_setup_help(backend: str) -> str | None:
    normalized = str(backend or "").strip().lower()
    if normalized in {"ssh", "daytona"}:
        return "This skill runs in a remote environment."
    if normalized == "singularity":
        return "This skill runs through singularity-backed skills."
    return None


def _spec_summary(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_summary_dict"):
        return dict(record.to_summary_dict())
    return {
        "id": str(getattr(record, "spec_id", "") or ""),
        "kind": str(getattr(record, "kind", "") or ""),
        "summary": str(getattr(record, "summary", "") or ""),
        "path": str(getattr(record, "path", "") or ""),
    }


def _workflow_spec_file_payload(name: str, file_path: str | None) -> dict[str, Any] | None:
    """Load an explicitly linked workflow spec through the skill-view boundary."""
    requested = str(file_path or "").strip().replace("\\", "/").lstrip("./")
    if not requested:
        return None
    records = list(specs_for_skill(name))
    for record in records:
        path = Path(str(getattr(record, "path", "") or ""))
        normalized = str(path).replace("\\", "/")
        if requested != normalized and not normalized.endswith(f"/{requested}"):
            continue
        return {
            "success": True,
            "name": name,
            "file": normalized,
            "content": str(getattr(record, "content", "") or ""),
            "linked_files": {
                "workflow_specs": [str(getattr(item, "path", "") or "") for item in records]
            },
        }
    return None


def check_skills_requirements() -> bool:
    """LeanFlow curated skills are always available."""
    return True


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """
    Parse YAML frontmatter from markdown content.

    Uses yaml.safe_load for full YAML support (nested metadata, lists, etc.)
    with a fallback to simple key:value splitting for robustness.

    Args:
        content: Full markdown file content

    Returns:
        Tuple of (frontmatter dict, remaining content)
    """
    frontmatter = {}
    body = content

    if content.startswith("---"):
        end_match = re.search(r"\n---\s*\n", content[3:])
        if end_match:
            yaml_content = content[3 : end_match.start() + 3]
            body = content[end_match.end() + 3 :]

            try:
                parsed = yaml.safe_load(yaml_content)
                if isinstance(parsed, dict):
                    frontmatter = parsed
                # yaml.safe_load returns None for empty frontmatter
            except yaml.YAMLError:
                # Fallback: simple key:value parsing for malformed YAML
                for line in yaml_content.strip().split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        frontmatter[key.strip()] = value.strip()

    return frontmatter, body


def _get_category_from_path(skill_path: Path) -> str | None:
    """
    Extract category from skill path based on directory structure.

    For paths like: ~/.leanflow/skills/mlops/axolotl/SKILL.md -> "mlops"
    """
    try:
        rel_path = skill_path.relative_to(SKILLS_DIR)
        parts = rel_path.parts
        if len(parts) >= 3:
            return parts[0]
        return None
    except ValueError:
        return None


def _estimate_tokens(content: str) -> int:
    """
    Rough token estimate (4 chars per token average).

    Args:
        content: Text content

    Returns:
        Estimated token count
    """
    return len(content) // 4


def _parse_tags(tags_value) -> list[str]:
    """
    Parse tags from frontmatter value.

    Handles:
    - Already-parsed list (from yaml.safe_load): [tag1, tag2]
    - String with brackets: "[tag1, tag2]"
    - Comma-separated string: "tag1, tag2"

    Args:
        tags_value: Raw tags value — may be a list or string

    Returns:
        List of tag strings
    """
    if not tags_value:
        return []

    # yaml.safe_load already returns a list for [tag1, tag2]
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]

    # String fallback — handle bracket-wrapped or comma-separated
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]

    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _get_disabled_skill_names() -> set[str]:
    """Load disabled skill names from config (once per call).

    Resolves platform from ``LEANFLOW_PLATFORM`` env var, falls back to
    the global disabled list.
    """
    import os

    try:
        config = load_config()
        skills_cfg = config.get("skills", {})
        resolved_platform = os.getenv("LEANFLOW_PLATFORM")
        if resolved_platform:
            platform_disabled = skills_cfg.get("platform_disabled", {}).get(resolved_platform)
            if platform_disabled is not None:
                return set(platform_disabled)
        return set(skills_cfg.get("disabled", []))
    except Exception:
        return set()


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """Check if a skill is disabled in config."""
    import os

    try:
        config = load_config()
        skills_cfg = config.get("skills", {})
        resolved_platform = platform or os.getenv("LEANFLOW_PLATFORM")
        if resolved_platform:
            platform_disabled = skills_cfg.get("platform_disabled", {}).get(resolved_platform)
            if platform_disabled is not None:
                return name in platform_disabled
        return name in skills_cfg.get("disabled", [])
    except Exception:
        return False


def _find_all_skills(*, skip_disabled: bool = False) -> list[dict[str, Any]]:
    """Recursively find all skills in ~/.leanflow/skills/.

    Args:
        skip_disabled: If True, return ALL skills regardless of disabled
            state (used by the ``leanflow skills`` config UI). Default False
            filters out disabled skills.

    Returns:
        List of skill metadata dicts (name, description, category).
    """
    skills = []

    if not SKILLS_DIR.exists():
        return skills

    # Load disabled set once (not per-skill)
    disabled = set() if skip_disabled else _get_disabled_skill_names()

    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue

        skill_dir = skill_md.parent

        try:
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, body = _parse_frontmatter(content)

            if not skill_matches_platform(frontmatter):
                continue

            name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
            if name in disabled:
                continue

            description = frontmatter.get("description", "")
            if not description:
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break

            if len(description) > MAX_DESCRIPTION_LENGTH:
                description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

            category = _get_category_from_path(skill_md)

            skills.append(
                {
                    "name": name,
                    "description": description,
                    "category": category,
                }
            )

        except (UnicodeDecodeError, PermissionError) as e:
            logger.debug("Failed to read skill file %s: %s", skill_md, e)
            continue
        except Exception as e:
            logger.debug("Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True)
            continue

    return skills


def _iter_local_skill_files() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    files: list[Path] = []
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        files.append(skill_md)
    for flat_md in SKILLS_DIR.glob("*.md"):
        if flat_md.name in {"DESCRIPTION.md"}:
            continue
        files.append(flat_md)
    deduped: list[Path] = []
    for path in files:
        if path not in deduped:
            deduped.append(path)
    return deduped


def _local_skill_identity(path: Path, frontmatter: dict[str, Any]) -> tuple[str, str]:
    skill_name = str(frontmatter.get("name") or "").strip()
    if not skill_name:
        skill_name = path.parent.name if path.name == "SKILL.md" else path.stem
    if path.name == "SKILL.md":
        rel_name = str(path.parent.relative_to(SKILLS_DIR))
    else:
        rel_name = path.stem
    return skill_name, rel_name


def _resolve_local_skill(name: str) -> tuple[Path, dict[str, Any], str] | None:
    wanted = str(name or "").strip()
    if not wanted or not SKILLS_DIR.exists():
        return None
    direct_dir = SKILLS_DIR / wanted / "SKILL.md"
    direct_flat = SKILLS_DIR / f"{wanted}.md"
    for candidate in (direct_dir, direct_flat):
        if candidate.is_file():
            try:
                raw = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError) as exc:
                raise ValueError(f"Failed to read skill '{wanted}': {exc}") from exc
            frontmatter, _body = _parse_frontmatter(raw)
            skill_name, _rel_name = _local_skill_identity(candidate, frontmatter)
            return candidate, frontmatter, skill_name
    for candidate in _iter_local_skill_files():
        try:
            raw = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError) as exc:
            candidate_name = (
                candidate.parent.name if candidate.name == "SKILL.md" else candidate.stem
            )
            if candidate_name == wanted:
                raise ValueError(f"Failed to read skill '{wanted}': {exc}") from exc
            continue
        frontmatter, _body = _parse_frontmatter(raw)
        skill_name, rel_name = _local_skill_identity(candidate, frontmatter)
        if wanted in {
            skill_name,
            rel_name,
            candidate.parent.name if candidate.name == "SKILL.md" else candidate.stem,
        }:
            return candidate, frontmatter, skill_name
    return None


def _linked_files_for_local_skill(path: Path) -> dict[str, list[str]]:
    root = path.parent if path.name == "SKILL.md" else path.parent
    linked: dict[str, list[str]] = {}
    for subdir in ("references", "templates", "assets", "scripts"):
        base = root / subdir
        if not base.is_dir():
            continue
        linked[subdir] = [
            str(file.relative_to(root)) for file in sorted(base.rglob("*")) if file.is_file()
        ]
    return linked


def _first_body_line(body: str) -> str:
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _local_skill_payload(name: str, file_path: str | None = None) -> dict[str, Any] | None:
    """Build the complete skill response payload for skill_view, supporting two modes: if file_path is set, return the linked file content with path-traversal validation; otherwise return full skill metadata including required env vars, readiness status, missing vars after capture attempt, and setup notes. Returns None if skill not found."""
    resolved = _resolve_local_skill(name)
    if resolved is None:
        return None
    path, frontmatter, skill_name = resolved
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as exc:
        raise ValueError(f"Failed to read skill '{name}': {exc}") from exc
    frontmatter, body = _parse_frontmatter(raw)
    base_dir = path.parent if path.name == "SKILL.md" else path.parent

    if file_path:
        base_dir_resolved = base_dir.resolve()
        requested = (base_dir / file_path).resolve()
        try:
            requested.relative_to(base_dir_resolved)
        except Exception:
            return {
                "success": False,
                "error": f"Path traversal blocked: file path '{file_path}' escapes the skill boundary for skill '{name}'.",
            }
        if not requested.is_file():
            return {
                "success": False,
                "error": f"File '{file_path}' not found for skill '{name}'.",
            }
        return {
            "success": True,
            "name": skill_name,
            "file": str(requested),
            "content": requested.read_text(encoding="utf-8"),
            "linked_files": _linked_files_for_local_skill(path),
        }

    required_env_vars = _get_required_environment_variables(frontmatter)
    env_snapshot = load_env()
    missing_entries = [
        entry
        for entry in required_env_vars
        if not _is_env_var_persisted(entry["name"], env_snapshot)
    ]
    capture_result = _capture_required_environment_variables(skill_name, missing_entries)
    env_snapshot = load_env()
    backend = _get_terminal_backend_name()
    remaining = _remaining_required_environment_names(
        required_env_vars,
        capture_result,
        env_snapshot=env_snapshot,
        backend=backend,
    )
    readiness_status = (
        SkillReadinessStatus.SETUP_NEEDED if remaining else SkillReadinessStatus.AVAILABLE
    )
    setup_help = _backend_setup_help(backend)
    payload: dict[str, Any] = {
        "name": skill_name,
        "description": str(frontmatter.get("description") or _first_body_line(body) or ""),
        "content": raw,
        "file": str(path),
        "linked_files": _linked_files_for_local_skill(path),
        "tags": _parse_tags(
            ((frontmatter.get("metadata") or {}).get("leanflow") or {}).get("tags")
        ),
        "required_environment_variables": required_env_vars,
        "missing_required_environment_variables": remaining,
        "setup_needed": bool(remaining),
        "setup_skipped": bool(capture_result.get("setup_skipped")),
        "gateway_setup_hint": capture_result.get("gateway_setup_hint"),
        "readiness_status": readiness_status.value,
        "setup_note": _build_setup_note(readiness_status, remaining, setup_help),
    }
    return payload


def _load_category_description(category_dir: Path) -> str | None:
    """
    Load category description from DESCRIPTION.md if it exists.

    Args:
        category_dir: Path to the category directory

    Returns:
        Description string or None if not found
    """
    desc_file = category_dir / "DESCRIPTION.md"
    if not desc_file.exists():
        return None

    try:
        content = desc_file.read_text(encoding="utf-8")
        # Parse frontmatter if present
        frontmatter, body = _parse_frontmatter(content)

        # Prefer frontmatter description, fall back to first non-header line
        description = frontmatter.get("description", "")
        if not description:
            for line in body.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line
                    break

        # Truncate to reasonable length
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

        return description if description else None
    except (UnicodeDecodeError, PermissionError) as e:
        logger.debug("Failed to read category description %s: %s", desc_file, e)
        return None
    except Exception as e:
        logger.warning("Error parsing category description %s: %s", desc_file, e, exc_info=True)
        return None


def skills_categories(verbose: bool = False, task_id: str = None) -> str:
    """
    List available skill categories with descriptions (progressive disclosure tier 0).

    Returns category names and descriptions for efficient discovery before drilling down.
    Categories can have a DESCRIPTION.md file with a description frontmatter field
    or first paragraph to explain what skills are in that category.

    Args:
        verbose: If True, include skill counts per category (default: False, but currently always included)
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with list of categories and their descriptions
    """
    try:
        if not SKILLS_DIR.exists():
            return json.dumps(
                {
                    "success": True,
                    "categories": [],
                    "message": "No skills directory found.",
                },
                ensure_ascii=False,
            )

        category_dirs = {}
        category_counts: dict[str, int] = {}
        for skill_md in SKILLS_DIR.rglob("SKILL.md"):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue

            try:
                frontmatter, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
            except Exception:
                frontmatter = {}

            if not skill_matches_platform(frontmatter):
                continue

            category = _get_category_from_path(skill_md)
            if category:
                category_counts[category] = category_counts.get(category, 0) + 1
                if category not in category_dirs:
                    category_dirs[category] = SKILLS_DIR / category

        categories = []
        for name in sorted(category_dirs.keys()):
            category_dir = category_dirs[name]
            description = _load_category_description(category_dir)

            cat_entry = {"name": name, "skill_count": category_counts[name]}
            if description:
                cat_entry["description"] = description
            categories.append(cat_entry)

        return json.dumps(
            {
                "success": True,
                "categories": categories,
                "hint": "If a category is relevant to your task, use skills_list with that category to see available skills",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def skills_list(category: str = None, task_id: str = None) -> str:
    """
    List all available skills (progressive disclosure tier 1 - minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view() to
    load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops")
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with minimal skill info: name, description, category
    """
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        local_skills = _find_all_skills()
        default_skills_dir = LEANFLOW_HOME_DIR / "skills"
        if local_skills or default_skills_dir != SKILLS_DIR:
            all_skills = [
                {
                    **skill,
                    "source": "local",
                    "workflow_specs": [
                        _spec_summary(record) for record in specs_for_skill(skill["name"])
                    ],
                }
                for skill in local_skills
                if not category or skill.get("category") == category
            ]
            categories = sorted(
                {skill.get("category") for skill in all_skills if skill.get("category")}
            )
        else:
            all_skills = [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "category": "leanflow",
                    "source": skill.source,
                    "workflow_specs": [
                        _spec_summary(record) for record in specs_for_skill(skill.name)
                    ],
                }
                for skill in _og_discover_skills()
            ]
            if category and category != "leanflow":
                all_skills = []
            categories = ["leanflow"] if all_skills else []
        return json.dumps(
            {
                "success": True,
                "skills": all_skills,
                "categories": categories,
                "count": len(all_skills),
                "hint": "Use skill_view(name) to see the full skill content or linked files.",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def skill_view(name: str, file_path: str = None, task_id: str = None) -> str:
    """
    View the content of a skill or a specific file within a skill directory.

    Args:
        name: Name or path of the skill (e.g., "axolotl" or "03-fine-tuning/axolotl")
        file_path: Optional path to a specific file within the skill (e.g., "references/api.md")
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with skill content or error message
    """
    try:
        payload = _workflow_spec_file_payload(name, file_path)
        if payload is None:
            payload = _local_skill_payload(name, file_path)
        if payload and not payload.get("success", True):
            return json.dumps(payload, ensure_ascii=False)
        if payload is None:
            payload = _og_load_skill_file(name, file_path) if file_path else _og_load_skill(name)
        if not payload:
            available = [skill["name"] for skill in _find_all_skills()] or [
                skill.name for skill in _og_discover_skills()[:20]
            ]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skills_list to inspect the curated LeanFlow skill set.",
                },
                ensure_ascii=False,
            )
        workflow_specs = [
            _spec_summary(record)
            for record in specs_for_skill(str(payload.get("name", "") or name))
        ]
        if workflow_specs:
            linked = dict(payload.get("linked_files") or {})
            linked.setdefault("workflow_specs", [record["path"] for record in workflow_specs])
            payload["linked_files"] = linked
        return json.dumps(
            {"success": True, **payload, "workflow_specs": workflow_specs}, ensure_ascii=False
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# Tool description for model_tools.py
SKILLS_TOOL_DESCRIPTION = """Access skill documents providing specialized instructions, guidelines, and executable knowledge.

Progressive disclosure workflow:
1. skills_list() - Returns metadata (name, description, tags, linked_file_count) for all skills
2. skill_view(name) - Loads full SKILL.md content + shows available linked_files
3. skill_view(name, file_path) - Loads specific linked file (e.g., 'references/api.md', 'scripts/train.py')

Skills may include:
- references/: Additional documentation, API specs, examples
- templates/: Output formats, config files, boilerplate code
- assets/: Supplementary files (agentskills.io standard)
- scripts/: Executable helpers (Python, shell scripts)"""


if __name__ == "__main__":
    """Test the skills tool"""
    print("🎯 Skills Tool Test")
    print("=" * 60)

    # Test listing skills
    print("\n📋 Listing all skills:")
    result = json.loads(skills_list())
    if result["success"]:
        print(f"Found {result['count']} skills in {len(result.get('categories', []))} categories")
        print(f"Categories: {result.get('categories', [])}")
        print("\nFirst 10 skills:")
        for skill in result["skills"][:10]:
            cat = f"[{skill['category']}] " if skill.get("category") else ""
            print(f"  • {cat}{skill['name']}: {skill['description'][:60]}...")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a skill
    print("\n📖 Viewing skill 'axolotl':")
    result = json.loads(skill_view("axolotl"))
    if result["success"]:
        print(f"Name: {result['name']}")
        print(f"Description: {result.get('description', 'N/A')[:100]}...")
        print(f"Content length: {len(result['content'])} chars")
        if result.get("linked_files"):
            print(f"Linked files: {result['linked_files']}")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a reference file
    print("\n📄 Viewing reference file 'axolotl/references/dataset-formats.md':")
    result = json.loads(skill_view("axolotl", "references/dataset-formats.md"))
    if result["success"]:
        print(f"File: {result['file']}")
        print(f"Content length: {len(result['content'])} chars")
        print(f"Preview: {result['content'][:150]}...")
    else:
        print(f"Error: {result['error']}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. The first call returns SKILL.md content plus linked files and any native workflow-spec metadata attached to that skill.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills)",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path advertised in linked_files, including a skill-local reference/template/script or a linked native workflow spec. Omit to get the main SKILL.md content.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(
        category=args.get("category"), task_id=kw.get("task_id")
    ),
    check_fn=check_skills_requirements,
    emoji="📚",
)
registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=lambda args, **kw: skill_view(
        args.get("name", ""), file_path=args.get("file_path"), task_id=kw.get("task_id")
    ),
    check_fn=check_skills_requirements,
    emoji="📚",
)
