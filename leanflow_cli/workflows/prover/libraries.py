"""Install explicitly pinned Lake libraries through protected additive updates."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from leanflow_cli.workflows.prover.session_research import _public_connection
from leanflow_cli.workflows.prover.source import lean_code_mask
from tools.utilities.repository_research_policy import repository_research_disabled

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
_REV = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_LEAN_REQUIRE = re.compile(
    r"require\s+(?P<name>[A-Za-z][A-Za-z0-9_]*|«[^»]+»)(?:\s+from\s+git\s+"
    r'(?P<git>"(?:[^"\\]|\\.)*")\s*@\s*(?P<rev>"(?:[^"\\]|\\.)*"))?'
)


def _entries(entries: list[dict[str, Any]], deadline: float) -> list[dict[str, str]]:
    """Validate names, immutable revisions, and public HTTPS repository destinations."""
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
        connection, _, _ = _public_connection(git, min(deadline, time.monotonic() + 10))
        connection.close()
        candidate = {"name": name, "git": git.rstrip("/"), "rev": rev.lower()}
        if name in normalized and normalized[name] != candidate:
            raise ValueError(f"Conflicting library requests for {name}")
        normalized[name] = candidate
    return list(normalized.values())


def _confined(root: Path, path: Path) -> None:
    """Reject links that could let Lake mutate files beyond the project boundary."""
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Library path escapes the project: {path.name}")


def _validate_layout(root: Path) -> None:
    """Reject package-location escapes before and after dependency hooks execute."""
    for path in (root / ".leanflow", root / ".lake", root / ".lake/packages"):
        _confined(root, path)
    packages = root / ".lake/packages"
    if packages.is_dir():
        for package in packages.iterdir():
            _confined(root, package)
    if (root / ".lake/package-overrides.json").exists():
        raise ValueError("Lake package overrides require manual library installation")


def _configuration(root: Path) -> tuple[Path, bytes, dict[str, dict[str, Any]]]:
    """Read dependency names while preserving the complete original configuration."""
    candidates = [
        root / name for name in ("lakefile.toml", "lakefile.lean") if (root / name).exists()
    ]
    if len(candidates) != 1 or candidates[0].is_symlink():
        raise ValueError("Expected exactly one regular lakefile.toml or lakefile.lean")
    path = candidates[0]
    original = path.read_bytes()
    if len(original) > 1024 * 1024:
        raise ValueError("Lake configuration exceeds the supported size")
    text = original.decode("utf-8")
    known: dict[str, dict[str, Any]] = {}
    if path.suffix == ".toml":
        configuration = tomllib.loads(text)
        if (
            configuration.get("manifestFile", "lake-manifest.json") != "lake-manifest.json"
            or configuration.get("packagesDir", ".lake/packages") != ".lake/packages"
        ):
            raise ValueError("Custom Lake manifest/package directories require manual installation")
        declared = configuration.get("require", [])
        if not isinstance(declared, list):
            raise ValueError("Invalid Lake dependency declarations")
        for item in declared:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("Lake dependency declarations require names")
            if item["name"] in known:
                raise ValueError("Duplicate existing Lake dependency names")
            known[item["name"]] = item
    else:
        mask = lean_code_mask(text)
        if re.search(r"\b(?:manifestFile|packagesDir)\s*:=", mask):
            raise ValueError("Custom Lake manifest/package directories require manual installation")
        for occurrence in re.finditer(r"(?m)^[ \t]*require\b", mask):
            match = _LEAN_REQUIRE.match(
                text, occurrence.start() + len(occurrence.group()) - len("require")
            )
            if match is None:
                raise ValueError("Unsupported existing Lean require syntax; install manually")
            name = match["name"].strip("«»")
            if name in known:
                raise ValueError("Duplicate existing Lake dependency names")
            known[name] = {
                "name": name,
                "git": json.loads(match["git"]) if match["git"] else "",
                "rev": json.loads(match["rev"]) if match["rev"] else "",
            }
    return path, original, known


def _lock_packages(content: bytes | None) -> dict[str, dict[str, Any]]:
    """Parse manifest identities without accepting ambiguous duplicate package names."""
    if content is None:
        return {}
    document = json.loads(content)
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, list):
        raise ValueError("Invalid Lake manifest package list")
    result: dict[str, dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ValueError("Invalid Lake manifest package entry")
        if package["name"] in result:
            raise ValueError("Duplicate Lake manifest package names")
        result[package["name"]] = package
    return result


def _append_requirements(path: Path, original: bytes, entries: list[dict[str, str]]) -> bytes:
    """Append only safely encoded dependency declarations to the original bytes."""
    newline = "\r\n" if b"\r\n" in original else "\n"
    lines: list[str] = ["", ""]
    for entry in entries:
        if path.suffix == ".toml":
            lines += [
                "[[require]]",
                *(f"{key} = {json.dumps(entry[key])}" for key in ("name", "git", "rev")),
                "",
            ]
        else:
            lines += [
                f'require «{entry["name"]}» from git {json.dumps(entry["git"])} @ {json.dumps(entry["rev"])}',
                "",
            ]
    return original + newline.join(lines).encode("utf-8")


def ensure_helper_library(root: Path) -> dict[str, Any]:
    """Register generated helper modules additively without running Lake or networking.

    The controller owns transaction rollback if later skeleton validation fails.
    Nonstandard source directories require manual configuration.
    """
    try:
        root = root.resolve()
        lakefile, original, _ = _configuration(root)
        text = original.decode("utf-8")
        if lakefile.suffix == ".toml":
            configuration = tomllib.loads(text)
            if configuration.get("srcDir", ".") != ".":
                raise ValueError("Generated helpers require the standard package source directory")
            existing: list[Any] = [
                item
                for item in configuration.get("lean_lib", [])
                if isinstance(item, dict) and item.get("name") == "LeanFlowProofs"
            ]
            if len(existing) > 1:
                raise ValueError("Duplicate LeanFlowProofs library targets")
            if existing and (
                existing[0].get("srcDir", ".") != "."
                or "LeanFlowProofs" not in existing[0].get("roots", ["LeanFlowProofs"])
            ):
                raise ValueError("Existing LeanFlowProofs library has incompatible source roots")
            addition = '[[lean_lib]]\nname = "LeanFlowProofs"\n'
        else:
            mask = lean_code_mask(text)
            if re.search(r"\b(?:srcDir|roots)\s*:=", mask):
                raise ValueError("Custom Lean source roots require manual helper registration")
            existing = [
                occurrence
                for occurrence in re.finditer(r"(?m)^\s*lean_lib\b", mask)
                if re.match(
                    r"\s*lean_lib\s+(?:LeanFlowProofs\b|«LeanFlowProofs»)",
                    text[occurrence.start() :],
                )
            ]
            if len(existing) > 1:
                raise ValueError("Duplicate LeanFlowProofs library targets")
            addition = "lean_lib LeanFlowProofs\n"
        if existing:
            return {"accepted": True, "status": "already_registered", "changes": []}
        newline = "\r\n" if b"\r\n" in original else "\n"
        added = ("\n\n" + addition).replace("\n", newline)
        updated = original + added.encode("utf-8")
        # Recheck immediately before mutation so an intervening user edit is not lost.
        if lakefile.read_bytes() != original:
            raise ValueError("Lake configuration changed during helper registration")
        lakefile.write_bytes(updated)
        return {
            "accepted": True,
            "status": "registered",
            "changes": [
                {
                    "path": str(lakefile),
                    "before_sha256": hashlib.sha256(original).hexdigest(),
                    "after_sha256": hashlib.sha256(updated).hexdigest(),
                    "added_text": added,
                }
            ],
        }
    except (OSError, ValueError, TypeError) as exc:
        return {"accepted": False, "status": "rejected", "changes": [], "error": str(exc)}


def _run_update(root: Path, paths: list[Path], names: list[str], timeout_s: int) -> dict[str, Any]:
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


def _install_locked(root: Path, entries: list[dict[str, str]], deadline: float) -> dict[str, Any]:
    """Apply one serialized update and restore exact protected inputs on failure."""
    lakefile, original, known = _configuration(root)
    manifest, toolchain = root / "lake-manifest.json", root / "lean-toolchain"
    for path in (manifest, toolchain):
        if path.is_symlink():
            raise ValueError(f"Library configuration cannot be a symlink: {path.name}")
    if not toolchain.is_file():
        raise ValueError("Pinned project lean-toolchain is required")
    snapshots = {
        lakefile: original,
        manifest: manifest.read_bytes() if manifest.exists() else None,
        toolchain: toolchain.read_bytes(),
    }
    previous = _lock_packages(snapshots[manifest])
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
        return {
            "accepted": True,
            "status": "already_installed",
            "changes": [],
            "libraries": [previous[entry["name"]] for entry in entries],
        }
    updated = _append_requirements(lakefile, original, additions) if additions else original
    result: dict[str, Any] = {}
    update_attempted = False
    try:
        if deadline <= time.monotonic():
            raise TimeoutError("Library installation deadline exceeded")
        lakefile.write_bytes(updated)
        if snapshots[manifest] is None:
            manifest.write_text(
                json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []}),
                encoding="utf-8",
            )
        lake_dir = root / ".lake"
        lake_dir.mkdir(exist_ok=True)
        update_attempted = True
        result = _run_update(
            root,
            [lakefile, manifest, toolchain, lake_dir],
            [entry["name"] for entry in entries],
            max(1, int(deadline - time.monotonic())),
        )
        if not result.get("success") or result.get("returncode", 0) != 0:
            raise ValueError(
                str(
                    result.get("stderr")
                    or result.get("error_code")
                    or "Lake dependency update failed"
                )[:3000]
            )
        _validate_layout(root)
        if any(path.is_symlink() for path in snapshots):
            raise ValueError("Lake replaced a configuration file with a symlink")
        if lakefile.read_bytes() != updated or toolchain.read_bytes() != snapshots[toolchain]:
            raise ValueError("Lake changed protected configuration or the project's Lean toolchain")
        current_manifest = manifest.read_bytes()
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
        changes = []
        for path, before in snapshots.items():
            after = path.read_bytes()
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
        }
    except Exception as exc:
        for path, content in snapshots.items():
            if path.is_symlink():
                path.unlink()
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        return {
            "accepted": False,
            "status": "rolled_back",
            "error": str(exc)[:3000],
            "changes": [],
            "libraries": [],
            "dependency_cache_may_have_changed": update_attempted,
            "timed_out": bool(result.get("timed_out")),
        }


def install_libraries(
    root: Path, entries: list[dict[str, Any]], timeout_s: int = 180
) -> dict[str, Any]:
    """Install requested immutable libraries or return a bounded, explicit rejection.

    Original lakefile bytes are preserved as a prefix. Resolution runs with an OS
    write boundary; failure restores lakefile, manifest, and toolchain exactly.
    Downloaded package/build caches may remain and are reported as such.
    """
    try:
        if not isinstance(timeout_s, int) or not 1 <= timeout_s <= 600:
            raise ValueError("Library installation timeout must be between 1 and 600 seconds")
        if not isinstance(entries, list):
            raise ValueError("Library installation requires a list of explicit entries")
        if not entries:
            return {"accepted": True, "status": "no_changes", "changes": [], "libraries": []}
        if repository_research_disabled():
            raise ValueError("Library Git installation is disabled by clean-room policy")
        root = root.resolve()
        deadline = time.monotonic() + timeout_s
        validated = _entries(entries, deadline)
        _validate_layout(root)
        lock_directory = root / ".leanflow"
        lock_directory.mkdir(exist_ok=True)
        lock_path = lock_directory / "library-install.lock"
        if lock_path.is_symlink():
            raise ValueError("Library install lock cannot be a symlink")
        with lock_path.open("a") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Library installation lock deadline exceeded")
                    time.sleep(0.05)
            return _install_locked(root, validated, deadline)
    except Exception as exc:
        return {
            "accepted": False,
            "status": "rejected",
            "error": str(exc)[:3000],
            "changes": [],
            "libraries": [],
        }
