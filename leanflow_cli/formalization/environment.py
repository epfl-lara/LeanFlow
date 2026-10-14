"""Bind durable review evidence to actual compiler and dependency contents."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.workflows.prover.lean_environment import _files

LIBRARY_SEMANTICS = {
    "authority": "independently-kernel-checked-compiled-declarations",
    "check_scope": "generated-or-selected-declarations-and-recursive-type-value-dependencies",
    "unused_library_proofs": "not_certified",
    "source_build_correspondence": "not_claimed",
    "review_basis": "compiled-statement-and-semantic-dependency-surface",
    "trust_assumptions": [
        "The pinned Lean kernel and binary importer are correct, including memory safety.",
        "Source-fidelity review is an independent assessment, not a proved semantic equivalence.",
    ],
}


def _checker_sources(base: Path) -> dict[str, str]:
    """Bind all shipped controller code, including shared sandbox and review helpers."""
    return {
        path.relative_to(base).as_posix(): digest(path.read_bytes())
        for package in (
            "core",
            "agent",
            "leanflow_cli",
            "tools",
            "model_tools",
            "toolsets",
            "utils",
        )
        for path in sorted((base / package).rglob("*.py"))
        if path.is_file()
    }


def _compiler_root(root: Path) -> Path:
    """Resolve the compiler actually invoked for this pinned project, excluding Elan state."""
    result = subprocess.run(
        ["lean", "--print-prefix"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    prefix = Path(result.stdout.strip())
    if not prefix.is_absolute() or not (prefix / "bin" / "lean").is_file():
        raise ValueError("Pinned compiler prefix could not be resolved")
    return prefix.resolve()


def compiler_executable(root: Path, name: str) -> str:
    """Select the actual pinned compiler command instead of a PATH-shadowed launcher."""
    if name not in {"lean", "lake"}:
        raise ValueError("Unsupported pinned compiler executable")
    try:
        compiler = _compiler_root(root)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Pinned compiler executable could not be resolved") from error
    path = compiler / "bin" / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Pinned compiler executable is unavailable: {name}")
    if not path.resolve().is_relative_to(compiler):
        raise ValueError(f"Pinned compiler executable escapes its compiler prefix: {name}")
    return str(path)


def environment_manifest(root: Path) -> dict[str, Any]:
    """Hash pinned external inputs; project source bytes are bound by the proposal.

    Unlike the warm REPL generation, these digests remain meaningful after a
    restart. Build products belonging to the proposed project are checked by
    the declaration inspector and are deliberately not dependency inputs.
    """
    if os.environ.get("LEAN_PATH") or os.environ.get("ELAN_TOOLCHAIN"):
        raise ValueError("Formalization requires the project toolchain without loader overrides")
    config = {}
    for name in ("lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"):
        path = root / name
        if path.is_file():
            config[name] = digest(path.read_bytes())
    if "lean-toolchain" not in config:
        raise ValueError("Formalization requires a pinned Lean toolchain")
    dependencies = dependency_inputs(root)
    compiler = _compiler_root(root)
    inputs = {"compiler/bin": compiler / "bin", "compiler/lib": compiler / "lib" / "lean"}
    contents: dict[str, str] = {}
    for role, directory in sorted(inputs.items()):
        if not directory.exists():
            continue
        for path in _files(directory, set(), include_sources=True):
            if not path.is_file():
                continue
            before = path.stat()
            checksum = hashlib.sha256()
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    checksum.update(block)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("A compiler or dependency input changed while hashing")
            relative = path.relative_to(directory).as_posix() if directory.is_dir() else path.name
            contents[f"{role}/{relative}"] = checksum.hexdigest()
    if not contents:
        raise ValueError("No installed compiler inputs could be fingerprinted")
    policy = {
        name: digest(Path(__file__).with_name(name).read_bytes())
        for name in (
            "lean_inspector.py",
            "lean_policy.py",
            "lean_layout.py",
            "lean_configuration.py",
            "environment.py",
            "dependency_inputs.py",
            "lean_build.py",
            "dependency_files.py",
            "dependency_modules.py",
            "dependency_snapshot.py",
        )
    }
    return {
        "schema_version": 4,
        "configuration": config,
        "contents": contents,
        "policy": policy,
        "checker_sources": _checker_sources(Path(__file__).resolve().parents[2]),
        "library_semantics": LIBRARY_SEMANTICS,
        "dependencies": dependencies,
    }


def environment_identity(root: Path) -> str:
    """Return a persistent content digest independent of temporary checker paths."""
    return identity(environment_manifest(root))
