"""Install explicitly pinned Lake libraries through protected additive updates."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.deadlines import deadline_after, remaining_seconds
from leanflow_cli.workflows.prover.library_configuration import (
    _append_requirements,
    _configuration,
    _configuration_bytes,
    _lock_packages,
    _validate_layout,
)
from leanflow_cli.workflows.prover.library_configuration import (
    ensure_helper_library as ensure_helper_library,
)
from leanflow_cli.workflows.prover.session_research import _public_connection
from tools.utilities.repository_research_policy import repository_research_disabled

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
_REV = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


def _entries(
    entries: list[dict[str, Any]], deadline: float | None, *, verify_remote: bool = True
) -> list[dict[str, str]]:
    """Validate immutable requests, optionally checking public network reachability.

    Offline reuse checks only identity against an existing locked checkout; it
    must never open a connection merely to validate that identity's URL.
    """
    if not isinstance(entries, list) or len(entries) > 8:
        raise ValueError("Install at most eight explicit libraries per plan revision")
    normalized: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "git", "rev"}:
            raise ValueError("Library entries require exactly name, git, and rev")
        if not all(isinstance(value, str) for value in entry.values()):
            raise ValueError("Library name, git, and rev must be strings")
        name, git, rev = (entry[key].strip() for key in ("name", "git", "rev"))
        if not _NAME.fullmatch(name) or not _REV.fullmatch(rev):
            raise ValueError("Use an identifier package name and a full immutable Git commit hash")
        parsed = urlsplit(git)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
            or not re.fullmatch(r"/[A-Za-z0-9_./~-]+", parsed.path)
            or ".." in parsed.path.split("/")
        ):
            raise ValueError("Library repositories must use public HTTPS URLs without options")
        if verify_remote:
            connection, _, _ = _public_connection(
                git,
                (
                    min(deadline, time.monotonic() + 10)
                    if deadline is not None
                    else time.monotonic() + 10
                ),
            )
            connection.close()
        candidate = {"name": name, "git": git.rstrip("/"), "rev": rev.lower()}
        if name in normalized and normalized[name] != candidate:
            raise ValueError(f"Conflicting library requests for {name}")
        normalized[name] = candidate
    return list(normalized.values())


def _run_update(
    root: Path, paths: list[Path], names: list[str], timeout_s: float | None
) -> dict[str, Any]:
    """Run Lake with protected original sources and HTTPS-only noninteractive Git."""
    from leanflow_cli.workflows.prover.check_process import isolated_command

    argv = [
        "/usr/bin/env",
        "GIT_TERMINAL_PROMPT=0",
        "GIT_ALLOW_PROTOCOL=https",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_CONFIG_COUNT=1",
        "GIT_CONFIG_KEY_0=http.followRedirects",
        "GIT_CONFIG_VALUE_0=false",
        "lake",
        "--keep-toolchain",
        "update",
        *names,
    ]
    with tempfile.TemporaryDirectory(prefix="leanflow-library-install-") as temporary:
        return isolated_command(
            project_root=root,
            workspace=Path(temporary),
            argv=argv,
            timeout_s=timeout_s,
            extra_writable_roots=paths,
            network_allowed=True,
        )


def _install_locked(
    root: Path, entries: list[dict[str, str]], deadline: float | None
) -> dict[str, Any]:
    """Resolve privately, then promote validated config without overwriting editor versions."""
    lakefile, original, known = _configuration(root)
    manifest, toolchain = root / "lake-manifest.json", root / "lean-toolchain"
    for path in (manifest, toolchain):
        if path.is_symlink():
            raise ValueError(f"Library configuration cannot be a symlink: {path.name}")
    if not toolchain.is_file():
        raise ValueError("Pinned project lean-toolchain is required")
    snapshots = {
        lakefile: original,
        manifest: _configuration_bytes(manifest) if manifest.exists() else None,
        toolchain: _configuration_bytes(toolchain),
    }
    previous = _lock_packages(snapshots[manifest])
    packages_directory = root / ".lake/packages"
    existing_packages = (
        {
            path.name: (path.stat().st_dev, path.stat().st_ino)
            for path in packages_directory.iterdir()
        }
        if packages_directory.is_dir()
        else {}
    )
    additions: list[dict[str, str]] = []
    for entry in entries:
        old = known.get(entry["name"])
        if old is None:
            additions.append(entry)
        elif old.get("git") != entry["git"] or old.get("rev") != entry["rev"]:
            raise ValueError(f"Cannot replace existing library requirement {entry['name']}")
    if not additions and all(
        previous.get(entry["name"], {}).get("type") == "git"
        and previous.get(entry["name"], {}).get("rev") == entry["rev"]
        and previous.get(entry["name"], {}).get("url") == entry["git"]
        for entry in entries
    ):
        from leanflow_cli.workflows.prover.library_checkout import require_checkout

        missing = False
        for entry in entries:
            checkout = packages_directory / entry["name"]
            if not checkout.exists() and not checkout.is_symlink():
                missing = True
            else:
                require_checkout(checkout, entry["rev"], deadline=deadline)
        if not missing:
            return {
                "accepted": True,
                "status": "already_installed",
                "changes": [],
                "libraries": [previous[entry["name"]] for entry in entries],
            }
    updated = _append_requirements(lakefile, original, additions) if additions else original
    from leanflow_cli.workflows.prover import library_transaction

    result: dict[str, Any] = {}
    promoted = False

    def check() -> None:
        if deadline is not None and deadline <= time.monotonic():
            raise TimeoutError("Library installation deadline exceeded")

    try:
        with tempfile.TemporaryDirectory(
            prefix="library-stage-", dir=root / ".leanflow"
        ) as temporary:
            stage = Path(temporary) / "project"
            library_transaction.copy_inputs(root, stage, previous, check=check)
            private_lakefile = stage / lakefile.name
            private_manifest, private_toolchain = stage / manifest.name, stage / toolchain.name
            private_lakefile.write_bytes(updated)
            if snapshots[manifest] is None:
                private_manifest.write_text(
                    json.dumps(
                        {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []}
                    ),
                    encoding="utf-8",
                )
            check()
            result = _run_update(
                stage,
                [private_lakefile, private_manifest, private_toolchain, stage / ".lake"],
                [entry["name"] for entry in entries],
                remaining_seconds(deadline),
            )
            if not result.get("success") or result.get("returncode", 0) != 0:
                raise ValueError(
                    str(
                        result.get("stderr")
                        or result.get("error_code")
                        or "Lake dependency update failed"
                    )[:3000]
                )
            _validate_layout(stage)
            library_transaction.validate_private_links(stage, check=check)
            if any((stage / path.name).is_symlink() for path in snapshots):
                raise ValueError("Lake replaced a configuration file with a symlink")
            if (
                _configuration_bytes(private_lakefile) != updated
                or _configuration_bytes(private_toolchain) != snapshots[toolchain]
            ):
                raise ValueError(
                    "Lake changed protected configuration or the project's Lean toolchain"
                )
            current_manifest = _configuration_bytes(private_manifest)
            current = _lock_packages(current_manifest)
            for name, package in previous.items():
                if any(
                    current.get(name, {}).get(key) != package.get(key)
                    for key in ("type", "url", "rev", "dir", "subDir")
                ):
                    raise ValueError(f"Lake changed the existing locked dependency {name}")
            for entry in entries:
                package = current.get(entry["name"], {})
                if (
                    package.get("type") != "git"
                    or package.get("rev") != entry["rev"]
                    or package.get("url") != entry["git"]
                ):
                    raise ValueError(
                        f"Lake did not resolve the requested pinned library {entry['name']}"
                    )
                from leanflow_cli.workflows.prover.library_checkout import require_checkout

                require_checkout(
                    stage / ".lake/packages" / entry["name"], entry["rev"], deadline=deadline
                )
            promoted = True
            retained = library_transaction.promote(
                root, stage, snapshots, current, existing_packages, check=check, deadline=deadline
            )
            changes = []
            for path, before in snapshots.items():
                after = _configuration_bytes(stage / path.name)
                if after != before:
                    changes.append(
                        {
                            "path": str(path),
                            "before_sha256": (
                                hashlib.sha256(before).hexdigest() if before is not None else None
                            ),
                            "after_sha256": hashlib.sha256(after).hexdigest(),
                            "added_text": (
                                updated[len(original) :].decode("utf-8") if path == lakefile else ""
                            ),
                        }
                    )
            return {
                "accepted": True,
                "status": "installed",
                "changes": changes,
                "libraries": [current[entry["name"]] for entry in entries],
                "manifest_sha256": hashlib.sha256(current_manifest).hexdigest(),
                "stdout": str(result.get("stdout", ""))[:16000],
                "retained_source_mutations": retained,
            }
    except Exception as exc:
        from leanflow_cli.workflows.prover.source import SourceConflictError

        recovery_error = None
        retained = []
        try:
            retained = library_transaction.recover_pending(root)
        except (OSError, ValueError, RuntimeError) as error:
            recovery_error = str(error)
        conflicted = isinstance(exc, SourceConflictError) or recovery_error is not None
        return {
            "accepted": False,
            "status": "source_conflict" if conflicted else "rolled_back",
            "error": str(exc)[:3000],
            "recovery_error": recovery_error,
            "changes": [],
            "libraries": [],
            "dependency_cache_may_have_changed": promoted
            and _package_cache_changed(packages_directory, existing_packages),
            "retained_source_mutations": retained,
            "timed_out": bool(result.get("timed_out")),
        }


def _package_cache_changed(directory: Path, before: dict[str, tuple[int, int]]) -> bool:
    """Distinguish actual canonical checkout promotion from an interrupted pre-effect attempt."""
    try:
        after = (
            {path.name: (path.lstat().st_dev, path.lstat().st_ino) for path in directory.iterdir()}
            if directory.is_dir()
            else {}
        )
    except OSError:
        # An unreadable cache cannot establish that no promotion occurred.
        return True
    return after != before


def install_libraries(
    root: Path, entries: list[dict[str, Any]], timeout_s: float | None = None
) -> dict[str, Any]:
    """Install requested immutable libraries or return a bounded, explicit rejection.

    Preserve original lakefile bytes as a prefix. Resolve and run dependency hooks
    in private inputs; journal configuration promotion with no-clobber recovery.
    Added package directories may remain as cache after a failed promotion.
    """
    if timeout_s is not None and (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s < 0
    ):
        return {
            "accepted": False,
            "status": "configuration_error",
            "error_code": "invalid_timeout",
            "error": "Library installation timeout must be nonnegative or unset",
            "changes": [],
            "libraries": [],
        }
    try:
        if not isinstance(entries, list):
            raise ValueError("Library installation requires a list of explicit entries")
        if not entries:
            return {"accepted": True, "status": "no_changes", "changes": [], "libraries": []}
        if repository_research_disabled():
            raise ValueError("Library Git installation is disabled by clean-room policy")
        root = root.resolve()
        deadline = deadline_after(timeout_s)
        validated = _entries(entries, deadline)
        _validate_layout(root)
        lock_directory = root / ".leanflow"
        lock_directory.mkdir(exist_ok=True)
        lock_path = lock_directory / "library-install.lock"
        if lock_path.is_symlink():
            raise ValueError("Library install lock cannot be a symlink")
        from leanflow_cli.formalization.publication import publication_guard
        from leanflow_cli.workflows.prover.library_transaction import recover_pending

        # Keep the same ownership order as ordinary prover materialization: P -> L.
        with publication_guard(root, deadline=deadline), lock_path.open("a") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("Library installation lock deadline exceeded")
                    time.sleep(0.05)
            recover_pending(root)
            return _install_locked(root, validated, deadline)
    except Exception as exc:
        return {
            "accepted": False,
            "status": "rejected",
            "error": str(exc)[:3000],
            "changes": [],
            "libraries": [],
        }
