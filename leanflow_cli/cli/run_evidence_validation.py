"""Validate canonical launch and provenance evidence.

This leaf recomputes every sealed digest and rejects incomplete, noncanonical,
or credential-bearing reproducibility payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_IDENTITY_RE = re.compile(r"^\[content-sha256:[0-9a-f]{64};chars:[0-9]+\]$")
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


def _parse_count(raw: Any) -> int | None:
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _redacted_structure_is_canonical(value: Any, *, path: tuple[str, ...] = ()) -> bool:
    """Return whether a nested behavior projection contains only safe values."""
    from leanflow_cli.workflow import _is_prompt_content_env_key, _redacted_env_value

    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _redacted_structure_is_canonical(item, path=(*path, key))
            for key, item in value.items()
        )
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if not isinstance(value, str):
        return False
    synthetic_key = "LEANFLOW_BEHAVIOR_" + "_".join(path)
    if value and _is_prompt_content_env_key(synthetic_key):
        return bool(_CONTENT_IDENTITY_RE.fullmatch(value))
    return _redacted_env_value(synthetic_key, value) == value


def _launch_environment_is_valid(environment: Mapping[str, Any] | None) -> bool:
    """Verify the canonical digest of one sealed launch environment."""
    from leanflow_cli.workflow import _is_prompt_content_env_key, _redacted_env_value

    if not isinstance(environment, Mapping):
        return False
    values = environment.get("values")
    if not isinstance(values, Mapping) or not all(
        isinstance(key, str) and key.startswith("LEANFLOW_") and isinstance(value, str)
        for key, value in values.items()
    ):
        return False
    behavior_config = environment.get("behavior_config")
    if not isinstance(behavior_config, Mapping):
        return False
    behavior_encoded = json.dumps(
        dict(behavior_config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    behavior_sha256 = _sha256_bytes(behavior_encoded)
    identity_payload = {
        "values": dict(values),
        "behavior_config_sha256": behavior_sha256,
    }
    encoded = json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_runtime = {
        label: values.get(env_name, "") for label, env_name in _LAUNCH_RUNTIME_KEYS.items()
    }
    values_canonical = all(
        (
            bool(_CONTENT_IDENTITY_RE.fullmatch(value))
            if _is_prompt_content_env_key(key) and value
            else _redacted_env_value(key, value) == value
        )
        for key, value in values.items()
    )
    return bool(
        environment.get("scope") == "all-effective-LEANFLOW-prefix-values"
        and str(environment.get("sha256", "") or "") == _sha256_bytes(encoded)
        and environment.get("runtime") == expected_runtime
        and values_canonical
        and _redacted_structure_is_canonical(behavior_config)
        and environment.get("behavior_config_complete") is True
        and str(environment.get("behavior_config_sha256", "") or "") == behavior_sha256
    )


def _provenance_is_valid(provenance: Mapping[str, Any] | None) -> bool:
    """Verify one sealed provenance identity and its required evidence hashes."""
    from leanflow_cli.workflow import redact_url_credentials

    if not isinstance(provenance, Mapping):
        return False
    dependencies = provenance.get("dependencies")
    if not isinstance(dependencies, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in dependencies.items()
    ):
        return False
    dependency_sources = provenance.get("dependency_sources")
    if not isinstance(dependency_sources, Mapping) or set(dependency_sources) != set(dependencies):
        return False
    for source in dependency_sources.values():
        if not isinstance(source, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in source.items()
        ):
            return False
        source_url = source.get("url")
        if isinstance(source_url, str) and source_url != redact_url_credentials(source_url):
            return False
    guidance_files = provenance.get("workflow_guidance_files")
    if not isinstance(guidance_files, list):
        return False
    for record in guidance_files:
        if not isinstance(record, Mapping):
            return False
        if (
            not str(record.get("declared_path", "") or "")
            or not str(record.get("resolved_path", "") or "")
            or not _SHA256_RE.fullmatch(str(record.get("sha256", "") or ""))
            or _parse_count(record.get("bytes")) is None
            or int(record.get("bytes", -1)) < 0
        ):
            return False
    guidance_sha256 = _sha256_bytes(
        json.dumps(guidance_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if guidance_sha256 != str(provenance.get("workflow_guidance_sha256", "") or ""):
        return False
    selected_skills = provenance.get("selected_skills")
    if not isinstance(selected_skills, list):
        return False
    for skill in selected_skills:
        if not isinstance(skill, Mapping) or (
            skill.get("role") not in {"active", "additional"}
            or not str(skill.get("name", "") or "")
            or not str(skill.get("source", "") or "")
            or not _SHA256_RE.fullmatch(str(skill.get("files_sha256", "") or ""))
            or (_parse_count(skill.get("file_count")) or 0) <= 0
        ):
            return False
    selected_skills_sha256 = _sha256_bytes(
        json.dumps(selected_skills, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if selected_skills_sha256 != str(provenance.get("selected_skills_sha256", "") or ""):
        return False
    behavior_config = provenance.get("behavior_config")
    if not isinstance(behavior_config, Mapping):
        return False
    if not _redacted_structure_is_canonical(behavior_config):
        return False
    behavior_config_sha256 = _sha256_bytes(
        json.dumps(dict(behavior_config), sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if behavior_config_sha256 != str(provenance.get("behavior_config_sha256", "") or ""):
        return False
    python_runtime = provenance.get("python_runtime")
    installed_distributions = provenance.get("installed_distributions")
    if not isinstance(python_runtime, Mapping) or not isinstance(installed_distributions, Mapping):
        return False
    if not all(
        isinstance(key, str) and isinstance(value, str) and key and value
        for key, value in installed_distributions.items()
    ):
        return False
    python_runtime_sha256 = _sha256_bytes(
        json.dumps(
            {
                "python_runtime": dict(python_runtime),
                "installed_distributions": dict(sorted(installed_distributions.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if python_runtime_sha256 != str(provenance.get("python_runtime_sha256", "") or ""):
        return False
    runtime_content_sha256 = str(provenance.get("runtime_content_sha256", "") or "")
    combined_runtime_sha256 = _sha256_bytes(
        json.dumps(
            {
                "runtime_content_sha256": runtime_content_sha256,
                "python_runtime_sha256": python_runtime_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if combined_runtime_sha256 != str(provenance.get("runtime_source_sha256", "") or ""):
        return False
    lean_toolchain = str(provenance.get("lean_toolchain", "") or "")
    lean_toolchain_sha256 = _sha256_bytes(lean_toolchain.encode("utf-8", errors="surrogatepass"))
    if lean_toolchain_sha256 != str(provenance.get("lean_toolchain_sha256", "") or ""):
        return False
    build_payload = {
        "toolchain_status": str(provenance.get("toolchain_status", "") or ""),
        "lean_toolchain_sha256": lean_toolchain_sha256,
        "dependency_manifest_status": str(provenance.get("dependency_manifest_status", "") or ""),
        "dependency_manifest_sha256": str(provenance.get("dependency_manifest_sha256", "") or ""),
        "lakefile_lean_status": str(provenance.get("lakefile_lean_status", "") or ""),
        "lakefile_lean_sha256": str(provenance.get("lakefile_lean_sha256", "") or ""),
        "lakefile_toml_status": str(provenance.get("lakefile_toml_status", "") or ""),
        "lakefile_toml_sha256": str(provenance.get("lakefile_toml_sha256", "") or ""),
    }
    build_configuration_sha256 = _sha256_bytes(
        json.dumps(build_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if build_configuration_sha256 != str(provenance.get("build_configuration_sha256", "") or ""):
        return False
    identity_payload = {
        "git_commit": str(provenance.get("git_commit", "") or ""),
        "git_diff_sha256": str(provenance.get("git_diff_sha256", "") or ""),
        "git_untracked_index_sha256": str(provenance.get("git_untracked_index_sha256", "") or ""),
        "git_submodules_sha256": str(provenance.get("git_submodules_sha256", "") or ""),
        "project_manifest_sha256": str(provenance.get("project_manifest_sha256", "") or ""),
        "workflow_guidance_sha256": guidance_sha256,
        "runtime_source_sha256": str(provenance.get("runtime_source_sha256", "") or ""),
        "python_runtime_sha256": python_runtime_sha256,
        "selected_skills_sha256": selected_skills_sha256,
        "behavior_config_sha256": behavior_config_sha256,
        "build_configuration_sha256": build_configuration_sha256,
        "source_tree_sha256": str(provenance.get("source_tree_sha256", "") or ""),
        "lean_toolchain": str(provenance.get("lean_toolchain", "") or ""),
        "dependencies": dict(sorted(dependencies.items())),
        "dependency_sources": {
            str(name): dict(sorted(source.items()))
            for name, source in sorted(dependency_sources.items())
        },
        "dependency_manifest_sha256": str(provenance.get("dependency_manifest_sha256", "") or ""),
    }
    expected_identity = _sha256_bytes(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    required_hashes = (
        provenance.get("git_status_sha256"),
        identity_payload["git_diff_sha256"],
        identity_payload["git_untracked_index_sha256"],
        identity_payload["git_submodules_sha256"],
        identity_payload["project_manifest_sha256"],
        identity_payload["workflow_guidance_sha256"],
        runtime_content_sha256,
        identity_payload["runtime_source_sha256"],
        identity_payload["python_runtime_sha256"],
        identity_payload["selected_skills_sha256"],
        identity_payload["behavior_config_sha256"],
        identity_payload["build_configuration_sha256"],
        identity_payload["source_tree_sha256"],
        lean_toolchain_sha256,
        identity_payload["dependency_manifest_sha256"],
        build_payload["lakefile_lean_sha256"],
        build_payload["lakefile_toml_sha256"],
        provenance.get("source_identity_sha256"),
    )
    source_scope = str(provenance.get("source_identity_scope", "") or "")
    commands = provenance.get("git_commands")
    commands_valid = bool(
        isinstance(commands, Mapping)
        and commands
        and all(isinstance(key, str) and isinstance(value, bool) for key, value in commands.items())
    )
    commands_all = bool(isinstance(commands, Mapping) and commands and all(commands.values()))
    if source_scope == "git-index-plus-untracked":
        scope_complete = bool(
            provenance.get("git_executable_available") is True
            and provenance.get("git_repository") is True
            and provenance.get("git_evidence_status") == "complete"
            and provenance.get("git_evidence_complete") is True
            and commands_valid
            and commands_all
            and identity_payload["git_commit"]
        )
    elif source_scope == "lean-project-files":
        scope_complete = bool(
            provenance.get("git_executable_available") is True
            and provenance.get("git_marker_present") is False
            and provenance.get("git_repository") is False
            and provenance.get("git_evidence_status") == "not-a-git-worktree"
            and provenance.get("git_evidence_complete") is False
            and commands_valid
        )
    else:
        scope_complete = False
    return bool(
        str(provenance.get("leanflow_version", "") or "")
        and str(provenance.get("project_root", "") or "")
        and provenance.get("provenance_complete") is True
        and provenance.get("source_tree_complete") is True
        and provenance.get("project_configuration_complete") is True
        and provenance.get("project_manifest_status") in {"complete", "missing"}
        and provenance.get("workflow_guidance_status") in {"complete", "not-declared"}
        and provenance.get("workflow_guidance_issues") == []
        and provenance.get("runtime_source_complete") is True
        and provenance.get("runtime_source_issues") == []
        and (_parse_count(provenance.get("runtime_source_file_count")) or 0) > 0
        and provenance.get("python_runtime_complete") is True
        and provenance.get("python_runtime_issues") == []
        and provenance.get("selected_skills_complete") is True
        and provenance.get("selected_skills_issues") == []
        and provenance.get("behavior_config_complete") is True
        and provenance.get("build_configuration_complete") is True
        and provenance.get("toolchain_status") == "complete"
        and provenance.get("dependency_manifest_status") == "complete"
        and provenance.get("dependencies_complete") is True
        and provenance.get("lakefile_lean_status") in {"complete", "missing"}
        and provenance.get("lakefile_toml_status") in {"complete", "missing"}
        and scope_complete
        and all(_SHA256_RE.fullmatch(str(value or "")) for value in required_hashes)
        and provenance.get("source_identity_sha256") == expected_identity
    )
