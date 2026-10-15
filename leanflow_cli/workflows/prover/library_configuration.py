"""Parse bounded Lake configuration and configure helper targets in private previews."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.source import lean_code_mask

_LEAN_REQUIRE = re.compile(
    r'require\s+(?:"(?:[^"\\]|\\.)*"\s*/\s*)?'
    r'(?P<name>[A-Za-z][A-Za-z0-9_]*|«[^»]+»|"(?:[^"\\]|\\.)*")(?:\s+from\s+git\s+'
    r'(?P<git>"(?:[^"\\]|\\.)*")\s*@\s*(?P<rev>"(?:[^"\\]|\\.)*"))?'
)


def _configuration_bytes(path: Path, *, limit: int = 8 * 1024 * 1024) -> bytes:
    """Read bounded regular configuration without following a substituted symlink."""
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(
                f"Library configuration exceeds its bounded regular-file contract: {path}"
            )
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Library configuration grew beyond its bounded recovery limit: {path}")
    return content


def _confined(root: Path, path: Path) -> None:
    """Reject links that could let Lake mutate files beyond the project boundary."""
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError(f"Library infrastructure path escapes or aliases the project: {path.name}")


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
    original = _configuration_bytes(path, limit=1024 * 1024)
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
            name = (
                json.loads(match["name"])
                if match["name"].startswith('"')
                else match["name"].strip("«»")
            )
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
    """Configure helper targets in a controller-owned private preview without Lake or networking.

    The caller publishes validated bytes through a source transaction. This helper
    performs direct preview writes; nonstandard roots require manual configuration.
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
        # Keep the controller-owned preview anchored to its input bytes.
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
