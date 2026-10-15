"""Retain only new, independently derived Lake hash caches as explicit prepared infrastructure."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.preparation_authorization import (
    PreparationAuthorization,
    VerifiedHashCache,
)
from leanflow_cli.formalization.preparation_checkout import GitRead, checkout_head, checkout_sources
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.preparation_lifecycle import PrivateStage
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import (
    MAX_PROTOCOL_STDOUT_BYTES,
    Progress,
    run_command,
)

_HASH = re.compile(rb"[0-9a-f]{16}")
_QUERY_BATCH_SIZE = 256
_QUERY = """import Lake

def main (args : List String) : IO Unit := do
  let records ← args.toArray.mapM fun arg => do
    let path := System.FilePath.mk arg
    let binary ← Lake.computeFileHash path (text := false)
    let text ← try
      pure <| Lean.toJson (toString (← Lake.computeFileHash path (text := true)))
    catch _ => pure Lean.Json.null
    pure <| Lean.Json.arr #[Lean.toJson (toString binary), text]
  IO.println (Lean.Json.arr records).compress
"""


def _version(info: os.stat_result) -> tuple[int, ...]:
    """Bind a file version without treating read access times as mutations."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular(path: Path) -> tuple[int, ...]:
    """Refuse links and special files before any cache or input can be opened."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Lake hash cache reconciliation requires unaliased regular files")
    return _version(info)


@dataclass(frozen=True)
class _Cache:
    """Retain exact staged cache/input versions until independent admission finishes."""

    package: str
    checkout: Path
    relative: str
    version: tuple[int, ...]
    value: bytes
    cache_identity: str
    source: Path
    source_version: tuple[int, ...]
    source_identity: str


def _inspect(
    stage: Path, authority: PreparationAuthorization, check: Callable[[], None]
) -> list[_Cache]:
    """Require original pins and every previous input before classifying all new entries."""
    manifest = contained(stage, "lake-manifest.json")
    if (
        authority.manifest is None
        or authority.package_sources is None
        or authority.checkout_settings is None
        or authority.verified_hash_caches
        or authority.acquisition_root != str(stage.resolve())
        or json.loads(read_regular_bytes(manifest, MAX_PROTOCOL_STDOUT_BYTES)) != authority.manifest
    ):
        raise ValueError("Lake hash cache reconciliation lost its original private authority")
    authority.validate_configuration(stage, check=check)
    directory = contained(stage, ".lake/packages")
    if {path.name for path in directory.iterdir()} != set(authority.package_sources):
        raise ValueError("Lake hash cache reconciliation cannot change package membership")
    caches = []
    for package in authority.manifest["packages"]:
        check()
        name = package["name"]
        checkout = checkout_head(stage, package)
        if any(
            entry_identity(checkout / path, check=check) != expected
            for path, expected in authority.checkout_settings[name].items()
        ):
            raise ValueError("Lake hash cache reconciliation cannot change Git configuration")
        before = authority.package_sources[name]
        after = checkout_sources(checkout, check)
        if any(path not in after or after[path] != value for path, value in before.items()):
            raise ValueError("Lake hash cache reconciliation cannot change any previous input")
        for relative in sorted(set(after) - set(before)):
            check()
            original = relative.removesuffix(".hash")
            if not relative.endswith(".hash") or not before.get(original):
                raise ValueError("Preparation added an unclassified dependency input: " + relative)
            source, path = contained(checkout, original), contained(checkout, relative)
            source_version, version = _regular(source), _regular(path)
            value = read_regular_bytes(path, 16)
            if not _HASH.fullmatch(value) or _regular(path) != version:
                raise ValueError("Preparation added a malformed Lake hash cache: " + relative)
            caches.append(
                _Cache(
                    name,
                    checkout,
                    relative,
                    version,
                    value,
                    str(after[relative]),
                    source,
                    source_version,
                    str(before[original]),
                )
            )
    return caches


def _copy_input(cache: _Cache, target: Path, check: Callable[[], None]) -> None:
    """Stream an unchanged input into a distinct controller-owned query inode."""
    descriptor = os.open(
        cache.source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        if _version(os.fstat(descriptor)) != cache.source_version:
            raise ValueError("Lake hash input changed before copying")
        with os.fdopen(descriptor, "rb", closefd=False) as source, target.open("xb") as copied:
            os.fchmod(copied.fileno(), stat.S_IMODE(cache.source_version[2]))
            remaining = cache.source_version[4]
            while remaining:
                check()
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Lake hash input became incomplete")
                copied.write(block)
                remaining -= len(block)
            if source.read(1) or _version(os.fstat(descriptor)) != cache.source_version:
                raise ValueError("Lake hash input changed while copying")
        if (
            _regular(cache.source) != cache.source_version
            or entry_identity(target, check=check) != cache.source_identity
        ):
            raise ValueError("Lake hash query copy differs from its bound input")
    finally:
        os.close(descriptor)


def _query_hashes(
    directory: Path,
    caches: list[_Cache],
    policy: SetupExecutionPolicy,
    *,
    log: TextIO,
    cancel: threading.Event | None,
    progress: Progress | None,
) -> list[list[str | None]]:
    """Use only the pinned compiler's pure Lake hash API with a bounded JSON response."""
    script = directory / "HashCache.lean"
    script.write_text(_QUERY, encoding="utf-8")
    names = []
    for index, cache in enumerate(caches):
        name = f"input-{index}"
        _copy_input(cache, directory / name, lambda: policy.check(cancel))
        names.append(name)
    environment = {
        key: value for key, value in policy.environment.items() if not key.startswith("LEAN_")
    }
    environment["LEAN_NUM_THREADS"] = "1"
    environment["LEAN_PATH"] = str(Path(policy.executables["lean"]).parent.parent / "lib/lean")
    query_policy = replace(policy, environment=environment)
    code, output = run_command(
        ["lean", "--run", str(script), *names],
        cwd=directory,
        log=log,
        cancel=cancel,
        progress=progress,
        phase="checking_dependency_hash_cache",
        stdout_only=True,
        stdout_limit_bytes=64 * 1024,
        execution_policy=query_policy,
    )
    if code:
        raise ValueError(
            "The pinned Lake hash API could not verify generated caches: " + output[-2000:]
        )
    values = json.loads(output)
    if not isinstance(values, list) or len(values) != len(caches):
        raise ValueError("The pinned Lake hash API returned an incomplete response")
    for value, cache in zip(values, caches):
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or any(
                item is not None
                and (not isinstance(item, str) or not _HASH.fullmatch(item.encode()))
                for item in value
            )
            or cache.value.decode() not in value
        ):
            raise ValueError("Generated Lake hash cache does not derive from its unchanged input")
    return values


def _require_stopped(lease: PrivateStage, policy: SetupExecutionPolicy) -> None:
    """Require actual controller/stage ownership and observed quiescence before admission."""
    if (
        not policy.usage.cleanup_verified
        or lease.path.resolve() != policy.isolation.snapshot_root.resolve()
    ):
        raise ValueError("Lake hash cache verification requires its stopped private preparation")
    lease.precommit_cleanup()


def _require_untracked(caches: list[_Cache], git: GitRead) -> None:
    """Refuse tracked cache paths before and after the independent hash query."""
    for checkout in sorted({cache.checkout for cache in caches}):
        names = [cache.relative for cache in caches if cache.checkout == checkout]
        for offset in range(0, len(names), _QUERY_BATCH_SIZE):
            batch = names[offset : offset + _QUERY_BATCH_SIZE]
            if git(checkout, ["--literal-pathspecs", "ls-files", "-z", "--", *batch]):
                raise ValueError("Preparation cannot admit a Git-tracked hash cache")


def verify_generated_hash_caches(
    stage: Path,
    authority: PreparationAuthorization,
    policy: SetupExecutionPolicy,
    lease: PrivateStage,
    *,
    git: GitRead,
    log: TextIO,
    cancel: threading.Event | None,
    progress: Progress | None,
) -> PreparationAuthorization:
    """Admit only a complete verified cache set after all builds and hash queries stop."""
    if stage.resolve() != lease.path.resolve():
        raise ValueError("Lake hash cache verification must use its exact leased stage")
    _require_stopped(lease, policy)
    check = lambda: policy.check(cancel)
    caches = _inspect(stage, authority, check)
    if not caches:
        return authority
    private = contained(stage, ".leanflow/preparation-checks")
    private.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="hash-cache-", dir=private))
    lease.require_cleanup()
    values: list[list[str | None]] = []
    try:
        with supervise_preparation(policy, cancel=cancel):
            _require_untracked(caches, git)
            for offset in range(0, len(caches), _QUERY_BATCH_SIZE):
                batch = directory / f"batch-{offset // _QUERY_BATCH_SIZE}"
                batch.mkdir()
                values.extend(
                    _query_hashes(
                        batch,
                        caches[offset : offset + _QUERY_BATCH_SIZE],
                        policy,
                        log=log,
                        cancel=cancel,
                        progress=progress,
                    )
                )
            _require_untracked(caches, git)
    finally:
        lease.verify_cleanup(policy.evidence())
    _require_stopped(lease, policy)
    if _inspect(stage, authority, check) != caches:
        raise ValueError("Dependency inputs changed during Lake hash cache verification")
    assert authority.manifest is not None
    revisions = {package["name"]: package["rev"] for package in authority.manifest["packages"]}
    records = tuple(
        VerifiedHashCache(
            package=cache.package,
            revision=revisions[cache.package],
            input_path=cache.relative.removesuffix(".hash"),
            input_identity=cache.source_identity,
            cache_path=cache.relative,
            cache_identity=cache.cache_identity,
            hash_value=cache.value.decode("ascii"),
            hash_modes=tuple(
                mode
                for mode, value in zip(("binary", "text"), matches)
                if value == cache.value.decode("ascii")
            ),
        )
        for cache, matches in zip(caches, values)
    )
    return authority.with_verified_hash_caches(stage, records, check=check)
