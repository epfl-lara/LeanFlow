"""Verify managed MCP specification, package metadata and installed patch receipts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from email.parser import Parser
from pathlib import Path
from typing import Any

from leanflow_cli.cli.mcp_install_generation import atomic_managed_config_write

RECEIPT_NAME = ".leanflow-mcp-receipt.json"


def mcp_install_fingerprint(specification: Mapping[str, Any], patch_sources: Iterable[str]) -> str:
    """Identify the exact requested package and managed patch implementations."""
    payload = {"specification": dict(specification), "patch_sources": list(patch_sources)}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def mcp_arguments_fingerprint(arguments: Iterable[str]) -> str:
    """Record validated arguments without copying possible credential values."""
    return hashlib.sha256(json.dumps(list(arguments), separators=(",", ":")).encode()).hexdigest()


def _normalized_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _package_metadata(venv: Path, required: Iterable[str]) -> dict[str, dict[str, str]]:
    """Read package versions and provenance directly without importing server code."""
    wanted = {_normalized_package(name) for name in required}
    found: dict[str, dict[str, str]] = {}
    roots = [*venv.glob("lib/python*/site-packages"), venv / "Lib/site-packages"]
    for root in roots:
        for metadata in root.glob("*.dist-info/METADATA"):
            raw = metadata.read_bytes()
            document = Parser().parsestr(raw.decode("utf-8"))
            name = _normalized_package(str(document.get("Name", "")))
            if name not in wanted:
                continue
            if name in found:
                raise ValueError(f"Installed MCP package metadata is ambiguous: {name}")
            version = str(document.get("Version", ""))
            if not version:
                raise ValueError(f"Installed MCP package has no version: {name}")
            provenance = metadata.with_name("direct_url.json")
            found[name] = {
                "version": version,
                "metadata_sha256": hashlib.sha256(raw).hexdigest(),
                "provenance_sha256": (
                    hashlib.sha256(provenance.read_bytes()).hexdigest()
                    if provenance.is_file()
                    else ""
                ),
            }
    if set(found) != wanted:
        raise ValueError(
            "Managed MCP package metadata is incomplete: " + ", ".join(sorted(wanted - set(found)))
        )
    return found


def write_managed_mcp_receipt(
    venv: Path,
    *,
    expected_fingerprint: str,
    packages: Iterable[str],
    installed_files: Iterable[Path],
    arguments: Iterable[str],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal observed package and patch provenance only after real initialization."""
    if not all(
        validation.get(name) is True for name in ("console_script", "fastmcp", "initialized")
    ):
        raise ValueError("Managed MCP validation did not establish all required checks")
    receipt = venv / RECEIPT_NAME
    if receipt.exists() or receipt.is_symlink():
        raise FileExistsError(f"Refusing to replace an immutable MCP receipt: {receipt}")
    files: dict[str, str] = {}
    for path in installed_files:
        relative = path.resolve().relative_to(venv.resolve()).as_posix()
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "schema": 1,
        "specification_fingerprint": expected_fingerprint,
        "packages": _package_metadata(venv, packages),
        "files": files,
        "arguments_fingerprint": mcp_arguments_fingerprint(arguments),
        "validated": True,
    }
    atomic_managed_config_write(receipt, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def managed_mcp_receipt_status(
    venv: Path,
    *,
    expected_fingerprint: str,
    packages: Iterable[str],
    arguments: Iterable[str],
    installed_files: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Reject stale specs, changed packages/patches and unvalidated environments."""
    try:
        receipt = venv / RECEIPT_NAME
        if receipt.is_symlink():
            raise ValueError("receipt is a symlink")
        payload = json.loads(receipt.read_text())
        if payload.get("schema") != 1 or payload.get("validated") is not True:
            raise ValueError("receipt schema or initialization is unverified")
        if payload.get("specification_fingerprint") != expected_fingerprint:
            raise ValueError("requested MCP package or patch implementations changed")
        if payload.get("packages") != _package_metadata(venv, packages):
            raise ValueError("installed package metadata changed")
        if payload.get("arguments_fingerprint") != mcp_arguments_fingerprint(arguments):
            raise ValueError("configured MCP arguments changed after initialization")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("receipt has no installed content fingerprints")
        if installed_files is not None:
            expected_paths = {
                path.resolve().relative_to(venv.resolve()).as_posix() for path in installed_files
            }
            if set(files) != expected_paths:
                raise ValueError("installed MCP console or managed patch file set changed")
        for relative, expected in files.items():
            path = venv / relative
            if not path.resolve().is_relative_to(venv.resolve()):
                raise ValueError("receipt content path escapes its environment")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("installed MCP console or managed patch content changed")
        return {
            "current": True,
            "reason": "validated specification, package and patch fingerprints match",
        }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"current": False, "reason": str(exc)}
