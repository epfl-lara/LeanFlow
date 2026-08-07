"""Build and briefly cache exact multi-declaration Lean axiom inspections."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _strip_lean_comments_and_strings,
    _trim_declaration_region_end,
)

_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_REVISIONS = 8
_PREFETCH_KINDS = frozenset({"theorem", "lemma"})
_CONFIG_FILES = (
    "lean-toolchain",
    "lakefile.lean",
    "lakefile.toml",
    "lake-manifest.json",
)
_IMPORT_ENV_NAMES = (
    "ELAN_HOME",
    "LAKE_HOME",
    "LEAN_PATH",
    "LEAN_SRC_PATH",
)


@dataclass(frozen=True)
class AxiomBatchQuery:
    """Identify one declaration and its isolated output markers."""

    identity: str
    target: str
    kind: str
    line: int
    begin_marker: str
    end_marker: str


@dataclass(frozen=True)
class AxiomBatchPlan:
    """Describe one source harness and the requested declaration within it."""

    source: str
    queries: tuple[AxiomBatchQuery, ...]
    requested_identity: str
    requested_identities: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AxiomBatchProfile:
    """Hold the exact parsed axiom output for one marked declaration."""

    axioms: tuple[str, ...]
    output: str


@dataclass(frozen=True)
class _CacheEntry:
    created_at: float
    profiles: Mapping[str, AxiomBatchProfile]


_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple[str, str, str, str], _CacheEntry] = OrderedDict()


def _entry_identity(entry: Mapping[str, Any], index: int) -> str:
    """Return a revision-local identity for one indexed declaration."""
    payload = "\0".join(
        (
            str(index),
            str(entry.get("kind", "") or ""),
            str(entry.get("name", "") or ""),
            str(entry.get("line", 0) or 0),
            str(entry.get("text", "") or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolved_entry_index(entries: Sequence[Mapping[str, Any]], target: str) -> int:
    """Resolve a target exactly first, then by the legacy short-name behavior."""
    wanted = str(target or "").strip()
    if not wanted:
        return -1
    for index, entry in enumerate(entries):
        if str(entry.get("name", "") or "").strip() == wanted:
            return index
    short = wanted.split(".")[-1]
    for index, entry in enumerate(entries):
        if str(entry.get("name", "") or "").strip().split(".")[-1] == short:
            return index
    return -1


def _last_declaration_insertion_index(
    source: str,
    lines: list[str],
    *,
    declaration_line: int,
) -> int:
    """Return an insertion index before trailing namespace endings."""
    sanitized_lines = _strip_lean_comments_and_strings(source).splitlines()
    insertion_index = len(lines)
    cursor = len(sanitized_lines) - 1
    declaration_index = max(0, declaration_line - 1)
    while cursor >= declaration_index:
        line = sanitized_lines[cursor].strip()
        if not line:
            cursor -= 1
            continue
        if re.fullmatch(r"end(?:\s+[A-Za-z0-9_'.\u00ab\u00bb]+)?", line):
            insertion_index = cursor
            cursor -= 1
            continue
        break
    return insertion_index


def build_axiom_batch_plan(
    source: str,
    entries: Sequence[Mapping[str, Any]],
    target: str,
    *,
    requested_targets: Sequence[str] = (),
    prefetch_siblings: bool = True,
    truncate_after_last_query: bool = True,
) -> AxiomBatchPlan | None:
    """Build a marked harness for requested declarations.

    Sibling prefetch is limited to declarations at or before the latest
    requested target. Later declarations cannot affect an existing target's
    axiom profile and may be slow, broken, or intentionally unresolved, so
    ordinary axiom inspection truncates after the last query. Exact helper
    checks can retain their temporary parent skeleton by disabling truncation.
    """
    targets = tuple(
        dict.fromkeys(
            value
            for value in (
                str(target or "").strip(),
                *(str(item or "").strip() for item in requested_targets),
            )
            if value
        )
    )
    requested_indices = {
        requested: _resolved_entry_index(entries, requested) for requested in targets
    }
    if not targets or any(index < 0 for index in requested_indices.values()):
        return None
    requested_index = requested_indices[targets[0]]
    explicitly_requested = set(requested_indices.values())
    latest_requested_index = max(explicitly_requested)
    selected_indices = [
        index
        for index, entry in enumerate(entries)
        if (
            prefetch_siblings
            and index <= latest_requested_index
            and str(entry.get("kind", "") or "").strip().lower() in _PREFETCH_KINDS
        )
        or index in explicitly_requested
    ]
    if not selected_indices:
        return None

    lines = source.splitlines()
    insertions: list[tuple[int, AxiomBatchQuery]] = []
    requested_identity = ""
    for index in selected_indices:
        entry = entries[index]
        identity = _entry_identity(entry, index)
        marker_digest = identity[:24].upper()
        query = AxiomBatchQuery(
            identity=identity,
            target=str(entry.get("name", "") or "").strip(),
            kind=str(entry.get("kind", "") or "").strip(),
            line=max(1, int(entry.get("line", 1) or 1)),
            begin_marker=f"LEANFLOW_AXIOMS_BEGIN_{marker_digest}",
            end_marker=f"LEANFLOW_AXIOMS_END_{marker_digest}",
        )
        if index == requested_index:
            requested_identity = identity
        if index + 1 < len(entries):
            insertion_index = _trim_declaration_region_end(
                lines,
                start=query.line,
                next_start=max(1, int(entries[index + 1].get("line", 1) or 1)),
            )
        else:
            insertion_index = _last_declaration_insertion_index(
                source,
                lines,
                declaration_line=query.line,
            )
        insertions.append((insertion_index, query))

    for insertion_index, query in reversed(insertions):
        lines[insertion_index:insertion_index] = [
            f'#check ("{query.begin_marker}" : String)',
            f"#print axioms {query.target}",
            f'#check ("{query.end_marker}" : String)',
        ]
    final_insertion_index = max(insertion_index for insertion_index, _query in insertions)
    inserted_before_final = sum(
        3 for insertion_index, _query in insertions if insertion_index <= final_insertion_index
    )
    if truncate_after_last_query:
        lines = lines[: final_insertion_index + inserted_before_final]
    return AxiomBatchPlan(
        source="\n".join(lines) + "\n",
        queries=tuple(query for _, query in insertions),
        requested_identity=requested_identity,
        requested_identities=tuple(
            (
                requested,
                _entry_identity(entries[index], index),
            )
            for requested, index in requested_indices.items()
        ),
    )


def parse_axiom_batch_output(
    output: str,
    queries: Sequence[AxiomBatchQuery],
) -> dict[str, AxiomBatchProfile] | None:
    """Parse every marked profile, rejecting partial or ambiguous batch output."""
    profiles: dict[str, AxiomBatchProfile] = {}
    for query in queries:
        if output.count(query.begin_marker) != 1 or output.count(query.end_marker) != 1:
            return None
        begin = output.find(query.begin_marker)
        end = output.find(query.end_marker, begin + len(query.begin_marker))
        if begin < 0 or end < begin + len(query.begin_marker):
            return None
        segment = output[begin + len(query.begin_marker) : end].strip()
        dependency_lists = re.findall(r"depends on axioms:\s*\[([^\]]*)\]", segment)
        if not dependency_lists and "does not depend on any axioms" not in segment:
            return None
        axioms = tuple(
            sorted(
                {
                    token
                    for dependency_list in dependency_lists
                    for token in (item.strip() for item in dependency_list.split(","))
                    if token
                }
            )
        )
        profiles[query.identity] = AxiomBatchProfile(axioms=axioms, output=segment)
    return profiles


def source_revision_sha256(source: str) -> str:
    """Return the exact UTF-8 source revision digest."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def import_environment_fingerprint(project_root: Path) -> str:
    """Fingerprint Lake configuration and compiled imports used by a Lean invocation."""
    root = project_root.expanduser().resolve()
    digest = hashlib.sha256()
    digest.update(str(root).encode("utf-8"))
    for name in _IMPORT_ENV_NAMES:
        digest.update(name.encode("utf-8"))
        digest.update(str(os.environ.get(name, "") or "").encode("utf-8"))
    for name in _CONFIG_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")

    import_roots = [root / ".lake" / "build" / "lib" / "lean", root / "build" / "lib" / "lean"]
    packages = root / ".lake" / "packages"
    if packages.is_dir():
        try:
            import_roots.extend(
                package / ".lake" / "build" / "lib" / "lean"
                for package in packages.iterdir()
                if package.is_dir()
            )
        except OSError:
            pass
    for import_root in import_roots:
        if not import_root.is_dir():
            continue
        try:
            olean_paths = sorted(import_root.rglob("*.olean"))
        except OSError:
            continue
        for path in olean_paths:
            try:
                stat = path.stat()
                relative = path.relative_to(root) if path.is_relative_to(root) else path
            except OSError:
                continue
            digest.update(str(relative).encode("utf-8"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def cache_key(
    project_root: Path,
    target_file: Path,
    source_revision: str,
    environment_fingerprint: str,
) -> tuple[str, str, str, str]:
    """Return an exact cache key for a file revision and import environment."""
    return (
        str(project_root.expanduser().resolve()),
        str(target_file.expanduser().resolve()),
        source_revision,
        environment_fingerprint,
    )


def cached_profile(
    key: tuple[str, str, str, str],
    identity: str,
) -> AxiomBatchProfile | None:
    """Return a fresh cached profile for the exact declaration identity."""
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if now - entry.created_at > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        profile = entry.profiles.get(identity)
        if profile is not None:
            _CACHE.move_to_end(key)
        return profile


def store_profiles(
    key: tuple[str, str, str, str],
    profiles: Mapping[str, AxiomBatchProfile],
) -> None:
    """Store one successful all-query batch and prune older revisions."""
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(created_at=time.monotonic(), profiles=dict(profiles))
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX_REVISIONS:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    """Clear process-local batch evidence for deterministic tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
