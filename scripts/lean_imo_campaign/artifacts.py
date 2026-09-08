"""Freeze source inputs and create fresh, private Lake projects for benchmark cells."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    """Hash a file's exact bytes for the experiment provenance record."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: Any) -> None:
    """Replace a JSON snapshot atomically so the editor never reads a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _clone_commands(source: str, destination: str) -> tuple[list[str], ...]:
    """Copy strategies, cheapest first. Every one yields an independent tree.

    `-R` copies a symlink as a symlink rather than dereferencing it, so no
    strategy can leave the frozen evidence pointing back at a live path.
    """
    if sys.platform == "darwin":
        # APFS copy-on-write: no shared writable blocks, near-zero disk.
        cheap = ["/bin/cp", "-c", "-R", source, destination]
    else:
        # GNU cp: reflink on btrfs/XFS, a full copy on ext4 and elsewhere.
        cheap = ["cp", "--reflink=auto", "-R", source, destination]
    return (cheap, ["cp", "-R", source, destination])


def clone(source: Path, destination: Path) -> None:
    """Copy a tree independently, using copy-on-write where the platform offers it."""
    if destination.exists():
        raise ValueError(f"Refusing to replace existing evidence: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for argv in _clone_commands(str(source), str(destination)):
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0:
            return
        failures.append(f"{' '.join(argv)}: {result.stderr.strip() or result.returncode}")
        # A partial tree would be mistaken for frozen evidence, and a dirty
        # destination would make the next `cp -R` copy INTO it rather than
        # produce an independent copy. Remove it; if cleanup itself fails, stop
        # loudly rather than retry against a dirty path or leave a partial behind.
        if destination.is_symlink() or destination.exists():
            try:
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            except OSError as exc:
                failures.append(f"cleanup of partial {destination} failed: {exc}")
                raise ValueError(
                    f"Could not clone {source} to {destination}: {'; '.join(failures)}"
                ) from exc
    raise ValueError(f"Could not clone {source} to {destination}: {'; '.join(failures)}")


def initialize_lake(root: Path) -> dict[str, Any]:
    """Create Lake's local configuration cache with network disabled and sources protected."""
    from leanflow_cli.workflows.prover.check_process import isolated_command

    workspace = root / ".leanflow/preparation"
    workspace.mkdir(parents=True, exist_ok=True)
    result = isolated_command(
        project_root=root,
        workspace=workspace,
        argv=["lake", "env", "lean", "--version"],
        timeout_s=120,
        extra_writable_roots=[root / ".lake"],
        network_allowed=False,
    )
    save(workspace / "lake.json", result)
    if not result.get("success") or result.get("returncode") != 0:
        raise ValueError(f"Offline Lake preparation failed: {result}")
    return result


def write_launcher(directory: Path, name: str, module: str, arguments: list[str]) -> None:
    """Pin module resolution ahead of editable installs even when launched from the source repo."""
    bootstrap = f"import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module({module!r},run_name='__main__')"
    argv = [
        str(directory / "python-env/bin/python"),
        "-I",
        "-B",
        "-u",
        "-c",
        bootstrap,
        str(directory / "runtime"),
        *arguments,
    ]
    path = directory / name
    path.write_text("#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n')
    path.chmod(0o755)


def freeze(repo: Path, benchmark: Path, directory: Path) -> dict[str, Any]:
    """Snapshot the current working runtime, Python packages and statement-only fixture."""
    from leanflow_cli.config import get_config_path

    initial_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    runtime = directory / "runtime"
    runtime.mkdir(parents=True)
    files = list(repo.glob("*.py")) + [repo / "pyproject.toml", repo / "scripts/__init__.py"]
    for package in (
        "core",
        "agent",
        "tools",
        "leanflow_cli",
        "leanflow_skills",
        "leanflow_specs",
        "scripts/lean_imo_campaign",
    ):
        files += [
            p
            for p in (repo / package).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix in {".py", ".md", ".json", ".yaml", ".toml", ".txt", ".lean"}
        ]
    identities = {}
    for source in sorted(set(files)):
        relative = source.relative_to(repo)
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        identities[str(relative)] = digest(target)
    clone(repo / ".leanflow-venv", directory / "python-env")
    fixture = directory / "baseline"
    fixture.mkdir()
    for name in ("manifest.json", "lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        shutil.copy2(benchmark / name, fixture / name)
    manifest = json.loads((fixture / "manifest.json").read_text())
    problems = [p for p in manifest["problems"] if p["leap_solved"] is False]
    if len(problems) != 18 or len({p["id"] for p in problems}) != 18:
        raise ValueError("Expected exactly 18 distinct LEAP-unsolved problems")
    source_hashes = {}
    for problem in problems:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", problem["id"]):
            raise ValueError("Benchmark problem IDs must be bounded safe identifiers")
        source = benchmark / problem["file"]
        target = fixture / problem["file"]
        if not source.resolve().is_relative_to(benchmark) or not target.resolve().is_relative_to(
            fixture
        ):
            raise ValueError("Manifest source path escapes fixture")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        source_hashes[problem["file"]] = digest(target)
    clone(benchmark / ".lake/packages", fixture / ".lake/packages")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=repo, text=True
    ).strip()
    if commit != initial_commit or any(
        digest(repo / relative) != expected for relative, expected in identities.items()
    ):
        raise ValueError(
            "Working runtime changed while copying; create a fresh consistent snapshot"
        )
    identity = {
        "repository": str(repo),
        "commit": commit,
        "branch": branch,
        "working_tree_included": True,
        "runtime_files": identities,
        "runtime_sha256": hashlib.sha256(
            json.dumps(identities, sort_keys=True).encode()
        ).hexdigest(),
        "statement_files": source_hashes,
        "toolchain": (fixture / "lean-toolchain").read_text().strip(),
        "lake_manifest_sha256": digest(fixture / "lake-manifest.json"),
        "python": str(directory / "python-env/bin/python"),
        "external_inputs": {str(get_config_path()): digest(get_config_path())},
        "comparison_note": "LEAP used Lean/Mathlib 4.27.0; these unchanged statements were retargeted to 4.33.1. Report each condition separately; best-of-four is a separate aggregate.",
    }
    save(directory / "provenance.json", identity)
    write_launcher(directory, "leanflow-frozen", "leanflow_cli.main", [])
    write_launcher(
        directory, "run-campaign", "scripts.lean_imo_campaign.runner", ["run", str(directory)]
    )
    return identity


def prepare(directory: Path, cell: dict[str, Any]) -> Path:
    """Give a cell only its unsolved statement, pinned libraries and frozen skill contracts."""
    baseline = directory / "baseline"
    root = directory / "cells" / cell["id"]
    root.mkdir(parents=True, exist_ok=False)
    for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        shutil.copy2(baseline / name, root / name)
    relative = cell["problem"]["file"]
    target = root / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(baseline / relative, target)
    identity = json.loads((directory / "provenance.json").read_text())
    if digest(target) != identity["statement_files"][relative]:
        raise ValueError("Frozen benchmark statement hash changed")
    clone(baseline / ".lake/packages", root / ".lake/packages")
    flow = root / ".leanflow"
    flow.mkdir()
    (flow / "project.yaml").write_text(
        f"schema_version: 1\nname: {cell['id']}\nkind: lean4\nlean_root: .\n"
    )
    for name in ("lean-bounded-prover", "lean-prover-orchestrator"):
        from scripts.lean_imo_campaign.runtime_versions import runtime_directory

        source = runtime_directory(directory, cell) / "runtime/leanflow_skills" / name / "SKILL.md"
        target_skill = flow / "skills" / name / "SKILL.md"
        target_skill.parent.mkdir(parents=True)
        shutil.copy2(source, target_skill)
    save(directory / "cell-configs" / f"{cell['id']}.json", cell)
    initialize_lake(root)
    return root


def environment(directory: Path) -> dict[str, str]:
    """Keep credentials available while removing inherited workflow switches and Python paths."""
    from core.home import leanflow_home

    permitted = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "ELAN_HOME",
        "CODEX_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
        "__CF_USER_TEXT_ENCODING",
    }
    env = {k: v for k, v in os.environ.items() if k in permitted}
    env["LEANFLOW_HOME"] = str(leanflow_home())
    env["PYTHONPATH"] = str(directory / "runtime")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LEANFLOW_DISABLE_MCP"] = "1"
    env["LEANFLOW_DISABLE_SOLUTION_RESEARCH"] = "1"
    env["LEANFLOW_DISABLE_REPOSITORY_RESEARCH"] = "1"
    env["LEANFLOW_RESEARCH_LOCAL_LOOGLE"] = "0"
    env["LEANFLOW_DISPATCH_LOCAL_LOOGLE"] = "0"
    return env
