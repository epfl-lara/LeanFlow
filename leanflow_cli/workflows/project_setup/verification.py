"""Prepare a toolchain-compatible REPL and verify its real JSON protocol."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings
from leanflow_cli.lean.lean_statement_guard import _strip_lean_comments
from leanflow_cli.workflows.project_setup.process import SetupFailure
from leanflow_cli.workflows.project_setup.scaffold import (
    DEFAULT_TOOLCHAIN,
    REPL_REVISION,
    REPL_URL,
    read_toolchain,
    validate_lakefile,
)

Run = Callable[..., str]
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?$")


def _tag_key(tag: str) -> tuple[int, int, int, int, int] | None:
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), 0 if rc else 1, int(rc or 0)


def resolve_repl_revision(version: str, *, tags: list[str]) -> dict[str, Any]:
    """Choose the newest same-minor REPL tag no newer than this toolchain."""
    if version in tags:
        return {"rev": version, "status": "exact"}
    requested = _tag_key(version)
    candidates = [
        (key, tag)
        for tag in tags
        if (key := _tag_key(tag)) is not None
        and requested is not None
        and key[:2] == requested[:2]
        and key <= requested
    ]
    if candidates:
        return {"rev": max(candidates)[1], "status": "nearest"}
    return {"rev": "", "status": "unresolved"}


def dependency_names(root: Path) -> set[str]:
    """Return declared dependency names without executing a Lake configuration."""
    config = validate_lakefile(root)
    if config is not None:
        return {
            str(item.get("name", ""))
            for item in config.get("require", [])
            if isinstance(item, dict)
        }
    path = root / "lakefile.lean"
    text = _strip_lean_comments(path.read_text(encoding="utf-8")) if path.is_file() else ""
    # Lake supports both identifiers and quoted scope/name requirements.
    masked = re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        text,
    )
    names: set[str] = set()
    for command in re.finditer(r"^\s*require\b", masked, re.MULTILINE):
        match = re.match(
            r'\s+(?:"[^"\n]+"\s*/\s*)?(?:"([^"\n]+)"|([A-Za-z_][A-Za-z0-9_]*))',
            text[command.end() :],
        )
        if match:
            names.add(match[1] or match[2])
    return names


def repl_binary(root: Path) -> Path | None:
    """Return an executable project REPL, never just a file with that name."""
    suffix = ".exe" if os.name == "nt" else ""
    for path in (
        root / ".lake/build/bin" / f"repl{suffix}",
        root / ".lake/packages/repl/.lake/build/bin" / f"repl{suffix}",
    ):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def configure_repl(root: Path, run: Run, changed: list[str]) -> bool:
    """Add only the missing declarative REPL requirement at a compatible revision."""
    if "repl" in dependency_names(root):
        return False
    toolchain = read_toolchain(root)
    version = toolchain.rsplit(":", 1)[-1]
    if toolchain == DEFAULT_TOOLCHAIN:
        revision = REPL_REVISION
    else:
        output = run(
            ["git", "ls-remote", "--tags", "--refs", REPL_URL],
            code="repl_revision_lookup_failed",
            remedy="Check access to GitHub, then retry setup.",
        )
        tags = [
            line.rsplit("refs/tags/", 1)[-1] for line in output.splitlines() if "refs/tags/" in line
        ]
        revision = str(resolve_repl_revision(version, tags=tags)["rev"])
        if not revision:
            raise SetupFailure(
                "repl_revision_unavailable",
                f"No REPL release matches Lean {version}.",
                f"Choose a compatible revision from {REPL_URL}/tags, add the repl requirement, and retry.",
            )
    if (root / "lakefile.toml").is_file():
        path = root / "lakefile.toml"
        addition = f'\n\n[[require]]\nname = "repl"\ngit = "{REPL_URL}"\nrev = "{revision}"\n'
    else:
        path = root / "lakefile.lean"
        text = _strip_lean_comments_and_strings(path.read_text(encoding="utf-8"))
        # Only the ordinary declarative Lake DSL is safe to extend automatically.
        # Custom syntax and scopes can change the meaning of an appended command.
        lines = [line for line in text.splitlines() if line.strip() and not line[0].isspace()]
        ordinary = all(
            re.match(r"(?:import|open|package|require|lean_lib|lean_exe)\b|@\[", line)
            for line in lines
        )
        if (
            not ordinary
            or not re.search(r"^import\s+Lake\b", text, re.MULTILINE)
            or not re.search(r"^open\s+Lake\s+DSL\b", text, re.MULTILINE)
            or re.search(r"\b(?:namespace|section|macro|syntax|elab|run_cmd|repl)\b", text)
        ):
            raise SetupFailure(
                "repl_dependency_required",
                "This custom Lake configuration needs an explicit REPL dependency.",
                f'Add `require repl from git "{REPL_URL}" @ "{revision}"` at Lake DSL top level in lakefile.lean, then retry setup.',
            )
        addition = f'\n\nrequire repl from git "{REPL_URL}" @ "{revision}"\n'
    with path.open("a", encoding="utf-8") as handle:
        handle.write(addition)
    if path.name not in changed:
        changed.append(path.name)
    return True


def manifest_revisions(data: bytes | None) -> dict[str, Any]:
    """Extract existing dependency pins for selective-update preservation checks."""
    if data is None:
        return {}
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict) or not isinstance(payload.get("packages"), list):
            raise ValueError("Expected a package manifest mapping")
        return {
            str(item["name"]): item
            for item in payload.get("packages", [])
            if isinstance(item, dict) and item.get("name") != "repl"
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupFailure(
            "invalid_manifest",
            "lake-manifest.json is not a valid dependency manifest.",
            "Repair or restore the project manifest, then retry setup.",
        ) from exc


def protocol_check(root: Path, run: Run, *, mathlib: bool) -> Path:
    """Check one known theorem through the same REPL protocol used by verification."""
    binary = repl_binary(root)
    if binary is None:
        raise SetupFailure(
            "repl_binary_missing",
            "The REPL build finished without an executable.",
            "Review the build log and the repl dependency, then retry setup.",
        )
    code = ("import Mathlib.Data.Nat.Basic\n" if mathlib else "") + "example : True := by trivial\n"
    output = run(
        ["lake", "env", str(binary)],
        input_text=json.dumps({"cmd": code}) + "\n\n",
        stdout_only=True,
        code="verification_failed",
        remedy="Review the verification log, then retry setup.",
    )
    try:
        response = json.loads(output.strip())
    except ValueError as exc:
        raise SetupFailure(
            "verification_protocol_failed",
            "The REPL did not return a valid verification response.",
            "Review the log for toolchain or REPL errors, then retry setup.",
        ) from exc
    if not isinstance(response, dict) or not isinstance(response.get("messages", []), list):
        raise SetupFailure(
            "verification_protocol_failed",
            "The REPL returned a malformed verification response.",
            "Review the REPL version and log, then retry setup.",
        )
    errors = [
        item
        for item in response.get("messages", [])
        if not isinstance(item, dict) or item.get("severity") == "error"
    ]
    if "env" not in response or response.get("sorries") or errors:
        raise SetupFailure(
            "verification_failed",
            "The REPL could not verify the setup check: " + str(errors or response)[:2000],
            "Review the log and confirm that the REPL matches the project's Lean toolchain, then retry setup.",
        )
    return binary
