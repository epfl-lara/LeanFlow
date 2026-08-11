"""Assemble LeanFlow runtime, skill, and effective-behavior identities.

This facade owns installed LeanFlow content and selected skill trees, delegates
Python/provider and terminal/ambient identity to focused leaves, and builds the
canonical redacted launch environment.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.cli import run_python_identity as _python_identity
from leanflow_cli.cli import run_terminal_identity as _terminal_identity

_LAUNCH_RUNTIME_KEYS = {
    "agent_max_turns": "LEANFLOW_NATIVE_AGENT_MAX_TURNS",
    "base_url": "LEANFLOW_NATIVE_BASE_URL",
    "context_compression_model": "LEANFLOW_NATIVE_CONTEXT_COMPRESSION_MODEL",
    "model": "LEANFLOW_NATIVE_MODEL",
    "provider": "LEANFLOW_NATIVE_PROVIDER",
    "requested_target": "LEANFLOW_NATIVE_REQUESTED_TARGET",
    "reasoning_effort": "LEANFLOW_NATIVE_REASONING_EFFORT",
    "toolset": "LEANFLOW_NATIVE_TOOLSET",
    "active_skill": "LEANFLOW_NATIVE_ACTIVE_SKILL",
    "additional_skills": "LEANFLOW_NATIVE_ADDITIONAL_SKILLS",
}
_RUNTIME_SOURCE_SUFFIXES = frozenset({".py", ".md", ".json", ".yaml", ".yml", ".toml"})
_RUNTIME_SOURCE_ROOTS = (
    "leanflow_cli",
    "agent",
    "core",
    "tools",
    "leanflow_skills",
    "leanflow_specs",
    "run_agent.py",
)
_BEHAVIOR_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "agent": ("seed", "temperature", "top_p", "top_k", "min_p", "reasoning_effort"),
    "model": ("prover_light", "context_lengths"),
    "memory": (
        "memory_enabled",
        "user_profile_enabled",
        "nudge_interval",
        "flush_min_turns",
        "memory_char_limit",
        "user_char_limit",
    ),
    "skills": ("creation_nudge_interval",),
    "compression": (
        "threshold",
        "enabled",
        "summary_model",
        "reserved_output_tokens",
        "prune_tool_output",
        "prune_keep_recent_user_turns",
    ),
}
_AUXILIARY_BEHAVIOR_TASKS = (
    "AUTOFORMALIZER_VERIFICATION",
    "BLUEPRINT_VERIFICATION",
    "COMPRESSION",
    "LEAN_DECOMPOSE_HELPERS",
    "LEAN_REASONING",
    "MANAGER_NUDGE",
    "ORCHESTRATION",
    "PLANNER_SYNTHESIS",
    "PROVE_MANAGER",
    "STATEMENT_FIDELITY",
    "WEB_EXTRACT",
)
_AUXILIARY_BEHAVIOR_SUFFIXES = (
    "PROVIDER",
    "MODEL",
    "REASONING_EFFORT",
    "BASE_URL",
    "API_KEY",
    "COMMAND_TEMPLATE",
)
_BEHAVIOR_ENV_OVERRIDES = (
    "CONTEXT_COMPRESSION_ENABLED",
    "CONTEXT_COMPRESSION_MODEL",
    "CONTEXT_COMPRESSION_PRUNE_KEEP_RECENT_USER_TURNS",
    "CONTEXT_COMPRESSION_PRUNE_TOOL_OUTPUT",
    "CONTEXT_COMPRESSION_RESERVED_OUTPUT_TOKENS",
    "CONTEXT_COMPRESSION_THRESHOLD",
    "LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS",
    *(
        f"AUXILIARY_{task}_{suffix}"
        for task in _AUXILIARY_BEHAVIOR_TASKS
        for suffix in _AUXILIARY_BEHAVIOR_SUFFIXES
    ),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _content_records(
    root: Path, paths: Sequence[Path], *, require_within_root: bool = True
) -> tuple[list[dict[str, Any]], list[str]]:
    """Hash readable files and report unsafe or unavailable entries."""
    records: list[dict[str, Any]] = []
    issues: list[str] = []
    resolved_root = root.resolve()
    seen: set[str] = set()
    for index, path in enumerate(sorted(paths, key=lambda item: str(item))):
        try:
            resolved = path.resolve(strict=True)
            if require_within_root:
                resolved.relative_to(resolved_root)
            if not resolved.is_file():
                raise OSError("content path is not a file")
            raw = resolved.read_bytes()
            declared_relative = path.relative_to(root).as_posix()
            resolved_relative = resolved.relative_to(resolved_root).as_posix()
        except ValueError:
            issues.append(f"entry-{index}-out-of-root")
            continue
        except OSError:
            issues.append(f"entry-{index}-unreadable")
            continue
        if declared_relative in seen:
            continue
        seen.add(declared_relative)
        records.append(
            {
                "path": declared_relative,
                "resolved_path": resolved_relative,
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
            }
        )
    return records, issues


def _runtime_source_identity(*, module_file: str) -> dict[str, Any]:
    """Hash the installed LeanFlow runtime and packaged skill/spec content."""
    runtime_root = Path(module_file).resolve().parents[2]
    candidates: list[Path] = []
    issues: list[str] = []
    for relative in _RUNTIME_SOURCE_ROOTS:
        target = runtime_root / relative
        if target.is_file() or target.is_symlink():
            candidates.append(target)
            continue
        if not target.is_dir():
            issues.append(f"missing-{relative.replace('/', '-')}")
            continue
        try:
            candidates.extend(
                path
                for path in target.rglob("*")
                if (path.is_file() or path.is_symlink())
                and "__pycache__" not in path.parts
                and path.suffix in _RUNTIME_SOURCE_SUFFIXES
            )
        except OSError:
            issues.append(f"unreadable-{relative.replace('/', '-')}")
    records, record_issues = _content_records(runtime_root, candidates)
    issues.extend(record_issues)
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "runtime_source_scope": "installed-package-content",
        "runtime_source_sha256": _sha256_bytes(encoded),
        "runtime_source_file_count": len(records),
        "runtime_source_complete": not issues and bool(records),
        "runtime_source_issues": issues,
    }


def _python_runtime_identity(
    *,
    distribution_provider: Callable[[], Iterable[Any]],
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Bind Python state through the stable runtime-provenance seam."""
    return _python_identity.collect_python_runtime_identity(
        distribution_provider=distribution_provider,
        content_records=_content_records,
        project_root=project_root,
    )


def _selected_skills_identity(project_root: Path) -> dict[str, Any]:
    """Hash every resolved active/additional skill and its linked file trees."""
    from leanflow_cli.runtime.skill_core import find_skill

    active = str(os.getenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "") or "").strip()
    additional = [
        item.strip()
        for item in str(os.getenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", "") or "").split(os.pathsep)
        if item.strip()
    ]
    requested = [item for item in [active, *additional] if item]
    skills: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, request in enumerate(requested):
        record = find_skill(request, cwd=project_root)
        if record is None:
            issues.append(f"skill-{index}-unresolved")
            continue
        try:
            skill_root = record.skill_dir.resolve(strict=True)
            skill_md = record.skill_md.resolve(strict=True)
            skill_md.relative_to(skill_root)
        except (OSError, ValueError):
            issues.append(f"skill-{index}-unsafe-root")
            continue
        try:
            candidates = [
                path
                for path in skill_root.rglob("*")
                if (path.is_file() or path.is_symlink()) and "__pycache__" not in path.parts
            ]
        except OSError:
            issues.append(f"skill-{index}-tree-unreadable")
            continue
        file_records, file_issues = _content_records(skill_root, candidates)
        if file_issues or not file_records:
            issues.extend(f"skill-{index}-{issue}" for issue in file_issues or ["empty"])
            continue
        files_sha256 = _sha256_bytes(
            json.dumps(file_records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        skills.append(
            {
                "role": "active" if index == 0 and active else "additional",
                "name": record.name,
                "source": record.source,
                "files_sha256": files_sha256,
                "file_count": len(file_records),
            }
        )
    encoded = json.dumps(skills, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "selected_skills_sha256": _sha256_bytes(encoded),
        "selected_skills": skills,
        "selected_skills_complete": not issues and len(skills) == len(requested),
        "selected_skills_issues": issues,
    }


def _terminal_runtime_identity(project_root: Path) -> dict[str, Any]:
    """Bind terminal controls through the stable behavior-provenance seam."""
    return _terminal_identity.collect_terminal_runtime_identity(project_root)


def _ambient_environment_identity(project_root: Path) -> dict[str, Any]:
    """Bind safe ambient controls through the stable behavior-provenance seam."""
    return _terminal_identity.collect_ambient_environment_identity(project_root)


def _redacted_config_value(path: tuple[str, ...], value: Any) -> Any:
    """Return a canonical recursively redacted static-config value."""
    from leanflow_cli.workflow import _redacted_env_value

    if isinstance(value, Mapping):
        return {
            str(key): _redacted_config_value((*path, str(key)), item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _redacted_config_value((*path, str(index)), item) for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    synthetic_key = "LEANFLOW_BEHAVIOR_CONFIG_" + "_".join(path)
    return _redacted_env_value(synthetic_key, str(value))


def _behavior_config_identity(project_root: Path | None = None) -> dict[str, Any]:
    """Project safe effective config, environment, and persistent prompt inputs."""
    from leanflow_cli.config import load_config
    from leanflow_cli.workflow import _redacted_env_value

    try:
        config = load_config()
    except Exception:
        return {
            "behavior_config": {},
            "behavior_config_sha256": _sha256_bytes(b"{}"),
            "behavior_config_complete": False,
        }
    resolved_project_root = (
        project_root
        if project_root is not None
        else Path(str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "") or Path.cwd())
    ).expanduser()
    projection: dict[str, Any] = {}
    for section, fields in _BEHAVIOR_CONFIG_FIELDS.items():
        raw_section = config.get(section)
        raw_section = raw_section if isinstance(raw_section, Mapping) else {}
        projected_section: dict[str, Any] = {}
        for field in fields:
            projected_section[field] = _redacted_config_value(
                (section, field), raw_section.get(field)
            )
        projection[section] = projected_section
    raw_auxiliary = config.get("auxiliary")
    raw_auxiliary = raw_auxiliary if isinstance(raw_auxiliary, Mapping) else {}
    projected_auxiliary: dict[str, Any] = {}
    for task, raw_task in sorted(raw_auxiliary.items(), key=lambda item: str(item[0])):
        if not isinstance(task, str) or not isinstance(raw_task, Mapping):
            continue
        projected_task: dict[str, Any] = {}
        for field in ("provider", "model", "reasoning_effort", "base_url"):
            projected_task[field] = _redacted_env_value(
                f"LEANFLOW_BEHAVIOR_AUXILIARY_{task}_{field}",
                str(raw_task.get(field, "") or ""),
            )
        for field in (
            "command_template",
            "codex_command_template",
            "claude_code_command_template",
        ):
            projected_task[field] = _redacted_env_value(
                f"LEANFLOW_BEHAVIOR_AUXILIARY_{task}_{field}",
                str(raw_task.get(field, "") or ""),
            )
        projected_task["api_key_configured"] = bool(str(raw_task.get("api_key", "") or ""))
        projected_auxiliary[task] = projected_task
    projection["auxiliary"] = projected_auxiliary
    projection["environment_overrides"] = {
        key: _redacted_env_value(f"LEANFLOW_BEHAVIOR_{key}", str(os.getenv(key, "") or ""))
        for key in _BEHAVIOR_ENV_OVERRIDES
    }
    static_config = _redacted_config_value((), config)
    projection["effective_static_config_sha256"] = _sha256_bytes(
        json.dumps(static_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    from leanflow_cli.cli.run_prompt_identity import persistent_prompt_identity

    prompt_identity = persistent_prompt_identity(resolved_project_root, config)
    projection.update(prompt_identity)
    terminal_identity = _terminal_runtime_identity(resolved_project_root)
    projection["terminal_runtime"] = terminal_identity
    ambient_identity = _ambient_environment_identity(resolved_project_root)
    projection["ambient_runtime"] = ambient_identity
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "behavior_config": projection,
        "behavior_config_sha256": _sha256_bytes(encoded),
        "behavior_config_complete": bool(
            prompt_identity["persistent_prompt_inputs_complete"] is True
            and terminal_identity["complete"] is True
            and ambient_identity["complete"] is True
        ),
    }


def collect_launch_environment(
    environment: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return a sorted, credential-redacted effective LeanFlow environment."""
    from leanflow_cli.workflow import _redacted_env_value

    source = os.environ if environment is None else environment
    values = {
        key: _redacted_env_value(key, str(value))
        for key, value in sorted(source.items())
        if key.startswith("LEANFLOW_")
    }
    behavior = _behavior_config_identity(project_root)
    identity_payload = {
        "values": values,
        "behavior_config_sha256": behavior["behavior_config_sha256"],
    }
    encoded = json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "scope": "all-effective-LEANFLOW-prefix-values",
        "values": values,
        "sha256": _sha256_bytes(encoded),
        **behavior,
        "runtime": {
            label: values.get(env_name, "") for label, env_name in _LAUNCH_RUNTIME_KEYS.items()
        },
    }
