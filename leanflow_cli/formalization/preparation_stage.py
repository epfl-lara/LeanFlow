"""Copy private setup inputs and build dependencies without extending acquisition authority."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.file_copy import copy_file, copy_tree
from leanflow_cli.formalization.preparation_layout import configure_layout  # noqa: F401
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import Progress, run_command
from leanflow_cli.workflows.prover.source import project_sources

_CONFIG = ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")


def copy_stage(root: Path, stage: Path, *, check: Callable[[], None] | None = None) -> None:
    """Copy compiler inputs and dependencies privately, excluding runtime homes and credential files."""
    stage.mkdir(parents=True, exist_ok=True)
    for name in _CONFIG:
        if check:
            check()
        original = root / name
        if original.is_file():
            if name == "lean-toolchain" and (stage / name).is_file():
                if (stage / name).read_bytes() != original.read_bytes():
                    raise ValueError("Project toolchain changed while copying private inputs")
                continue
            copy_file(original, stage / name, check=check)
    for source in project_sources(root):
        if check:
            check()
        if source.name == "lakefile.lean":
            continue
        target = stage / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source, target, check=check)
    dependencies = root / ".lake"
    if dependencies.is_dir():

        def ignore(directory: str, names: list[str]) -> list[str]:
            private = {
                ".codex",
                ".claude",
                ".lemex",
                ".leanflow",
                ".mcp.json",
                ".env",
                ".netrc",
                "credentials",
            }
            if Path(directory).name == ".git":
                private |= {"config", "hooks"}
            return [name for name in names if name in private]

        copy_tree(dependencies, stage / ".lake", ignore=ignore, check=check)
    project = root / ".leanflow/project.yaml"
    if project.is_file():
        (stage / ".leanflow").mkdir(exist_ok=True)
        copy_file(project, stage / ".leanflow/project.yaml", check=check)


def add_packages(stage: Path, plan: dict[str, Any]) -> None:
    """Append only approved absent pinned requirements to a screened private configuration."""
    from leanflow_cli.workflows.project_setup.verification import dependency_names

    existing = dependency_names(stage)
    additions = [item for item in plan["packages"] if item["name"] not in existing]
    if not additions:
        return
    config = stage / ("lakefile.lean" if (stage / "lakefile.lean").is_file() else "lakefile.toml")
    with config.open("a", encoding="utf-8") as stream:
        for item in additions:
            if config.suffix == ".toml":
                stream.write(
                    "\n[[require]]\n"
                    + f'name = {json.dumps(item["name"])}\n'
                    + f'git = {json.dumps(item["url"])}\nrev = {json.dumps(item["revision"])}\n'
                )
            else:
                stream.write(
                    f'\nrequire {item["name"]} from git {json.dumps(item["url"])} @ {json.dumps(item["revision"])}\n'
                )


def build_prepared_dependencies(
    root: Path,
    stage: Path,
    plan: dict[str, Any],
    before_packages: dict[str, Any],
    policy: SetupExecutionPolicy,
    *,
    cancel: threading.Event | None,
    progress: Progress | None,
) -> None:
    """Reject pin/source drift and build every unavailable dependency library privately."""
    log_path = preparation_directory(root) / (uuid.uuid4().hex + ".log")
    with log_path.open("x", encoding="utf-8") as log:
        packages = json.loads((stage / "lake-manifest.json").read_text())["packages"]
        resolved = {item["name"]: item for item in packages}
        for name, previous in before_packages.items():
            previous = previous.get("manifest")
            if previous is None:
                continue
            if name not in resolved or any(
                resolved[name].get(key) != previous.get(key)
                for key in ("type", "url", "rev", "subDir")
            ):
                raise ValueError("Preparation tried to change an existing dependency pin: " + name)
        for requested in plan["packages"]:
            if resolved.get(requested["name"], {}).get("rev") != requested["revision"]:
                raise ValueError(
                    "Prepared dependency differs from the authorized revision: " + requested["name"]
                )
        for package in packages:
            if package.get("type") != "git":
                raise ValueError("Formalization preparation requires pinned Git dependencies")
            checkout = stage / ".lake/packages" / package["name"]
            code, head = run_command(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                cwd=stage,
                log=log,
                cancel=cancel,
                progress=progress,
                phase="checking_pins",
                execution_policy=policy,
                stdout_only=True,
            )
            if code or head.strip() != package["rev"]:
                raise ValueError(f"Prepared checkout differs from its pin: {package['name']}")
            code, dirty = run_command(
                ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
                cwd=stage,
                log=log,
                cancel=cancel,
                progress=progress,
                phase="checking_pins",
                execution_policy=policy,
                stdout_only=True,
            )
            if code or dirty.strip():
                raise ValueError(f"Prepared dependency has unbound source edits: {package['name']}")
            library = checkout / ".lake/build/lib/lean"
            if package["name"] != "repl" and not (
                library.is_dir() and any(library.rglob("*.olean"))
            ):
                code, output = run_command(
                    ["lake", "build", "@" + package["name"]],
                    cwd=stage,
                    log=log,
                    cancel=cancel,
                    progress=progress,
                    phase="building_dependencies",
                    execution_policy=policy,
                )
                if code:
                    raise ValueError("Dependency library build failed: " + output[-4000:])
        # Build missing non-Mathlib libraries too; REPL readiness alone is insufficient.
        code, output = run_command(
            ["lake", "build"],
            cwd=stage,
            log=log,
            cancel=cancel,
            progress=progress,
            phase="building_project",
            execution_policy=policy,
        )
        if code:
            raise ValueError("Prepared project build failed: " + output[-4000:])
