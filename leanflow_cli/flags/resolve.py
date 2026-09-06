"""Resolve effective knob values and named knob profiles.

Effective resolution answers "what is this run actually going to use, and why" —
a value can come from the process environment, a named profile, or the declared
default, and a UI that cannot distinguish those is not much help when a result
fails to reproduce.

Profiles are ordinary JSON files, so the shell, the VS Code extension, and the
evaluation harness all read and write the same thing. Built-in profiles are
derived from the runtime rather than restated, so ``research`` cannot drift away
from what ``--research`` actually applies.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.flags.catalog import FLAG_CATALOG, lookup_flag
from leanflow_cli.flags.spec import FlagProfile

#: Directory name holding user-defined profiles, under a project or LEANFLOW_HOME.
PROFILE_DIR_NAME = "flag-profiles"


def _research_profile_overrides() -> dict[str, str]:
    """Read the research profile straight from the module that applies it."""
    from leanflow_cli.workflows.research_mode import research_profile_env

    overrides = {"LEANFLOW_RESEARCH_MODE": "1"}
    overrides.update(research_profile_env())
    return overrides


def _builtin_profiles() -> dict[str, FlagProfile]:
    return {
        "default": FlagProfile(
            name="default",
            summary="Declared defaults — every knob left unset.",
            overrides={},
        ),
        "research": FlagProfile(
            name="research",
            summary=(
                "The complete relentless-research profile applied by --research: plan, "
                "retrieval, orchestration, dispatch, feasibility, reporting, and learning."
            ),
            overrides=_research_profile_overrides(),
        ),
        "clean-room": FlagProfile(
            name="clean-room",
            summary=(
                "Benchmark-safe boundary: no solution lookup for the target. Pair with "
                "--clean-room so the label set is derived from the target path."
            ),
            overrides={"LEANFLOW_DISABLE_SOLUTION_RESEARCH": "1"},
        ),
    }


#: Built-in profiles, rebuilt on each access so runtime changes stay reflected.
BUILTIN_PROFILES = _builtin_profiles


def profile_search_paths(project_root: Path | None = None) -> tuple[Path, ...]:
    """Return the directories searched for user-defined profiles, nearest first."""
    from leanflow_cli.config import get_leanflow_home

    paths: list[Path] = []
    if project_root is not None:
        paths.append(Path(project_root) / ".leanflow" / PROFILE_DIR_NAME)
    paths.append(Path(get_leanflow_home()) / PROFILE_DIR_NAME)
    return tuple(paths)


def _load_profile_file(path: Path) -> FlagProfile | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, Mapping):
        return None
    overrides_raw = raw.get("overrides")
    if not isinstance(overrides_raw, Mapping):
        return None
    overrides = {
        str(key).strip().upper(): str(value)
        for key, value in overrides_raw.items()
        if str(key).strip()
    }
    return FlagProfile(
        name=str(raw.get("name") or path.stem),
        summary=str(raw.get("summary") or ""),
        overrides=overrides,
        builtin=False,
    )


def load_profiles(project_root: Path | None = None) -> dict[str, FlagProfile]:
    """Return built-in profiles merged with user-defined ones.

    A user profile whose name matches a built-in shadows it, and a project-local
    profile shadows a home-level one of the same name.
    """
    profiles = dict(_builtin_profiles())
    for directory in reversed(profile_search_paths(project_root)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            profile = _load_profile_file(path)
            if profile is not None:
                profiles[profile.name] = profile
    return profiles


def resolve_profile(name: str, project_root: Path | None = None) -> FlagProfile:
    """Return one profile by name.

    Raises:
        KeyError: when no built-in or user-defined profile carries that name.
    """
    profiles = load_profiles(project_root)
    key = str(name or "").strip()
    if key not in profiles:
        raise KeyError(key)
    return profiles[key]


#: A profile name is used as a filename, so it is allowlisted rather than escaped.
_SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validated_profile_name(name: str) -> str:
    """Return a profile name that is safe to use as a filename.

    Raises:
        ValueError: when the name could escape the profiles directory or is not
            a usable filename.
    """
    candidate = str(name or "").strip()
    if candidate in {".", ".."} or not _SAFE_PROFILE_NAME.match(candidate):
        raise ValueError(
            f"invalid profile name {name!r}: use letters, digits, dot, dash, and underscore"
        )
    return candidate


def save_profile(
    profile: FlagProfile,
    *,
    project_root: Path | None = None,
) -> Path:
    """Write one user-defined profile and return the file it was written to.

    Unknown knob names are rejected: a profile that silently carries a typo is
    worse than no profile, because the run looks configured and is not. The
    name is validated separately because it becomes a path.
    """
    safe_name = _validated_profile_name(profile.name)
    unknown = sorted(name for name in profile.overrides if lookup_flag(name) is None)
    if unknown:
        raise ValueError(f"profile references unknown flags: {', '.join(unknown)}")
    directory = profile_search_paths(project_root)[0]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe_name}.json"
    payload = profile.to_payload()
    payload.pop("builtin", None)
    payload["name"] = safe_name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def effective_flag_values(
    env: Mapping[str, str] | None = None,
    *,
    profile: FlagProfile | None = None,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    """Return each knob's effective value and where that value came from.

    ``source`` is one of ``environment``, ``profile``, or ``default``. The
    environment wins over a profile, matching how the launcher builds the child
    environment: an explicitly exported value is a deliberate override.
    """
    environ = dict(os.environ if env is None else env)
    overrides = dict(profile.overrides) if profile is not None else {}
    rows: list[dict[str, Any]] = []
    for spec in FLAG_CATALOG:
        if spec.kind == "internal" and not include_internal:
            continue
        if spec.name in environ:
            raw = environ[spec.name]
            source = "environment"
        elif spec.name in overrides:
            raw = overrides[spec.name]
            source = "profile"
        else:
            raw = spec.default
            source = "default"
        rows.append(
            {
                "name": spec.name,
                "group": spec.group,
                "kind": spec.kind,
                "value_type": spec.value_type,
                "raw": raw,
                "value": spec.coerce(raw),
                "default": spec.default,
                "source": source,
                "is_default": spec.is_default(raw),
                "ablatable": spec.ablatable,
                "summary": spec.summary,
            }
        )
    return rows


def diff_profiles(left: FlagProfile, right: FlagProfile) -> list[dict[str, Any]]:
    """Return the knobs whose effective value differs between two profiles.

    A knob absent from a profile resolves to its declared default, so the diff
    compares what the two runs would actually do rather than what each file
    happens to mention.
    """
    names = sorted(set(left.overrides) | set(right.overrides))
    rows: list[dict[str, Any]] = []
    for name in names:
        spec = lookup_flag(name)
        default = spec.default if spec is not None else ""
        left_value = left.overrides.get(name, default)
        right_value = right.overrides.get(name, default)
        if spec is not None:
            same = spec.coerce(left_value) == spec.coerce(right_value)
        else:
            same = left_value == right_value
        if same:
            continue
        rows.append(
            {
                "name": name,
                "left": left_value,
                "right": right_value,
                "group": spec.group if spec is not None else "Unknown",
                "summary": spec.summary if spec is not None else "",
                "known": spec is not None,
            }
        )
    return rows


#: Ways a knob profile can be applied to a run, in the order a UI should list them.
LAUNCH_SURFACES = ("terminal", "extension")


def profile_launch_surfaces(profile: FlagProfile) -> dict[str, Any]:
    """Return which launch surfaces can apply every knob in a profile, and why not.

    A saved profile that carries a terminal-only (sensitive) knob such as
    ``LEANFLOW_PROVER_ALLOWED_AXIOMS`` is valid in the shell but is rejected by
    the VS Code extension's allowlist at launch time. Reporting that here lets a
    profile be labelled when it is saved or listed rather than when a launch
    fails. Unknown and launcher-internal knobs are unusable on every surface.
    """
    unknown: list[str] = []
    internal: list[str] = []
    terminal_only: list[str] = []
    for name in sorted(profile.overrides):
        spec = lookup_flag(name)
        if spec is None:
            unknown.append(name)
        elif not spec.editable:
            internal.append(name)
        elif not spec.extension_editable:
            terminal_only.append(name)
    unusable = sorted(unknown + internal)
    return {
        "terminal": {"supported": not unusable, "rejected": unusable},
        "extension": {
            "supported": not unusable and not terminal_only,
            "rejected": sorted(unusable + terminal_only),
        },
        "unknown_knobs": unknown,
        "internal_knobs": internal,
        "terminal_only_knobs": terminal_only,
    }


def profile_payload(project_root: Path | None = None) -> dict[str, Any]:
    """Return every available profile in the JSON shape the extension consumes.

    Each profile carries ``launch_surfaces`` so a consumer can flag an
    incompatible profile before building a launch from it.
    """
    profiles = load_profiles(project_root)
    entries = []
    for name in sorted(profiles):
        entry = profiles[name].to_payload()
        entry["launch_surfaces"] = profile_launch_surfaces(profiles[name])
        entries.append(entry)
    return {
        "version": 1,
        "count": len(profiles),
        "search_paths": [str(path) for path in profile_search_paths(project_root)],
        "launch_surfaces": list(LAUNCH_SURFACES),
        "profiles": entries,
    }
