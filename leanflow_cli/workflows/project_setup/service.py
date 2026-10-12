"""Drive shared CLI/editor project setup with resumable phases and explicit readiness."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from leanflow_cli.runtime.project_operations import ProjectBusyError, project_setup
from leanflow_cli.workflows.project import (
    LeanFlowProjectError,
    initialize_leanflow_project,
    load_leanflow_project,
)
from leanflow_cli.workflows.project_setup.process import (
    Progress,
    SetupCancelled,
    SetupFailure,
    check_cancel,
    find_executable,
    run_command,
    safe_text,
)
from leanflow_cli.workflows.project_setup.readiness import (
    installed_environment,
    mathlib_cache_present,
    source_diagnostics,
)
from leanflow_cli.workflows.project_setup.scaffold import (
    project_root,
    read_toolchain,
    scaffold,
    validate_lakefile,
    validate_mutable_paths,
)
from leanflow_cli.workflows.project_setup.state import (
    fingerprint,
    inspect_project,
    save_result,
    state_directory,
)
from leanflow_cli.workflows.project_setup.verification import (
    configure_repl,
    dependency_names,
    manifest_revisions,
    protocol_check,
)


def prepare_project(
    path: str | Path,
    *,
    name: str | None = None,
    preset: str = "mathlib",
    source_file: str | Path | None = None,
    template_source: str | None = None,
    repair: bool = False,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Return structured setup errors even when the log or receipt cannot be written."""
    try:
        return _prepare_project(
            path,
            name=name,
            preset=preset,
            source_file=source_file,
            template_source=template_source,
            repair=repair,
            progress=progress,
            cancel=cancel,
        )
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "phase": "setup_state",
            "code": "setup_io_failed",
            "message": safe_text(str(exc)),
            "remediation": "Check project and LeanFlow home permissions and free disk space, then retry setup.",
            "log_path": "",
            "root": str(path),
            "changed_files": [],
            "capabilities": {
                "verification": False,
                "mathlib": False,
                "repl_path": "",
                "source_diagnostics": [],
            },
        }


def _prepare_project(
    path: str | Path,
    *,
    name: str | None = None,
    preset: str = "mathlib",
    source_file: str | Path | None = None,
    template_source: str | None = None,
    repair: bool = False,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Prepare a project under exclusive ownership and return an actionable receipt."""
    root = project_root(path)
    state = state_directory(root)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = state / f"{uuid.uuid4().hex}.log"
    changed: list[str] = []
    result: dict[str, Any] = {
        "status": "failed",
        "phase": "checking",
        "code": "",
        "message": "",
        "remediation": "",
        "log_path": str(log_path),
        "root": str(root),
        "changed_files": changed,
        "capabilities": {
            "verification": False,
            "mathlib": False,
            "repl_path": "",
            "source_diagnostics": [],
        },
    }
    with log_path.open("x", encoding="utf-8") as log:
        log_path.chmod(0o600)

        def phase(value: str, message: str) -> None:
            check_cancel(cancel)
            result["phase"] = value
            log.write(message + "\n")
            log.flush()
            if progress:
                progress(
                    {
                        "type": "progress",
                        "phase": value,
                        "message": message,
                        "log_path": str(log_path),
                    }
                )

        def run(
            command: list[str],
            *,
            code: str,
            remedy: str,
            input_text: str | None = None,
            stdout_only: bool = False,
            cwd: Path | None = None,
        ) -> str:
            exit_code, output = run_command(
                command,
                cwd=cwd or root,
                log=log,
                cancel=cancel,
                progress=progress,
                phase=str(result["phase"]),
                input_text=input_text,
                stdout_only=stdout_only,
            )
            if exit_code:
                detail = safe_text(output[-4000:])
                raise SetupFailure(
                    code, f"{command[0]} failed (exit {exit_code}). {detail}", remedy
                )
            return output

        try:
            with project_setup(root):
                phase("checking", "Checking project and required tools")
                previous_setup = inspect_project(root)
                if (
                    previous_setup["ready"]
                    and previous_setup["configured"]
                    and not repair
                    and not name
                    and not source_file
                    and not template_source
                ):
                    load_leanflow_project(root)
                    message = "Already set up. Existing dependencies and verification results were reused."
                    phase("ready", message)
                    result = {
                        **result,
                        "status": "ready",
                        "phase": "ready",
                        "code": "ready",
                        "message": message,
                        "capabilities": {
                            **previous_setup["capabilities"],
                            "source_diagnostics": source_diagnostics(root),
                        },
                        "fingerprint": fingerprint(root),
                    }
                    save_result(root, result)
                    return result
                reuse_installed = (
                    not repair
                    and installed_environment(root)
                    and previous_setup.get("code") != "readiness_stale"
                )
                if root.exists() and not root.is_dir():
                    raise SetupFailure(
                        "invalid_directory",
                        "The project location is not a folder.",
                        "Choose a project folder.",
                    )
                for executable in ("git", "lake"):
                    if not find_executable(executable):
                        remedy = (
                            "Install Git and ensure it is on PATH, then retry setup."
                            if executable == "git"
                            else "Install Lean's Elan toolchain manager from https://lean-lang.org/install/, then reload VS Code and retry setup."
                        )
                        raise SetupFailure(
                            f"{executable}_missing",
                            f"{executable} is not available to LeanFlow.",
                            remedy,
                        )
                if template_source:
                    if root.exists():
                        validate_mutable_paths(root)
                        if not (root / ".git").exists():
                            raise SetupFailure(
                                "template_destination_exists",
                                "The template destination already exists without its Git checkout.",
                                "Choose a new folder, or prepare this folder without a template.",
                            )
                        origin = run(
                            ["git", "config", "--get", "remote.origin.url"],
                            code="template_origin_unavailable",
                            remedy="Check the existing checkout's origin, or prepare it without a template.",
                        ).strip()
                        if origin != template_source:
                            raise SetupFailure(
                                "template_origin_conflict",
                                "The existing checkout uses a different template origin.",
                                "Choose a new folder, or prepare this checkout without a template.",
                            )
                    else:
                        root.parent.mkdir(parents=True, exist_ok=True)
                        run(
                            ["git", "clone", template_source, str(root)],
                            cwd=root.parent,
                            code="template_clone_failed",
                            remedy="Check the template address and Git access, then retry setup.",
                        )
                        changed.append(".")
                validate_mutable_paths(root)
                phase("preparing", "Preparing project configuration")
                scaffold(root, name=name, preset=preset, source_file=source_file, changed=changed)
                validate_lakefile(root)
                if not read_toolchain(root):
                    raise SetupFailure(
                        "toolchain_missing",
                        "This project has no Lean toolchain version.",
                        "Restore or create lean-toolchain with the version required by this project, then retry setup.",
                    )
                phase("toolchain", "Preparing the project's Lean toolchain")
                run(
                    ["lake", "--version"],
                    code="toolchain_failed",
                    remedy="Check the Lean toolchain version and network access; retry to resume its installation.",
                )
                manifest = root / "lake-manifest.json"
                previous = manifest.read_bytes() if manifest.is_file() else None
                pins = manifest_revisions(previous)
                phase("dependencies", "Preparing pinned project dependencies")
                added = configure_repl(root, run, changed)
                if (
                    added
                    or not manifest.is_file()
                    or "repl" not in manifest.read_text(encoding="utf-8")
                ):
                    toolchain_before = (root / "lean-toolchain").read_bytes()
                    try:
                        run(
                            ["lake", "update", "repl"],
                            code="dependency_download_failed",
                            remedy="Check the dependency address, Git access, and network connection, then retry. Completed downloads are reused.",
                        )
                        current = manifest_revisions(
                            manifest.read_bytes() if manifest.is_file() else None
                        )
                        if any(current.get(key) != value for key, value in pins.items()):
                            raise SetupFailure(
                                "dependency_pin_conflict",
                                "Adding REPL would change existing dependency pins; the original manifest was restored.",
                                "Choose a REPL revision compatible with the existing dependencies, add it to the Lake file, then retry.",
                            )
                        if (root / "lean-toolchain").read_bytes() != toolchain_before:
                            raise SetupFailure(
                                "toolchain_conflict",
                                "Dependency setup tried to change the project's Lean toolchain; the original version was restored.",
                                "Choose dependencies compatible with the existing toolchain, then retry.",
                            )
                    except BaseException:
                        # Lake hooks may rewrite pins before reporting failure or cancellation.
                        # Preserve existing authority even when the command did not succeed.
                        if previous is not None:
                            manifest.write_bytes(previous)
                        (root / "lean-toolchain").write_bytes(toolchain_before)
                        raise
                    if previous != (manifest.read_bytes() if manifest.is_file() else None):
                        changed.append("lake-manifest.json")
                mathlib = "mathlib" in dependency_names(root)
                if mathlib and (repair or not reuse_installed or not mathlib_cache_present(root)):
                    phase(
                        "cache",
                        "Downloading missing Mathlib builds; completed downloads are reused",
                    )
                    run(
                        ["lake", "exe", "cache", "get"],
                        code="mathlib_cache_failed",
                        remedy="Check network access and free disk space, then retry to reuse completed downloads.",
                    )
                if not reuse_installed:
                    phase("verification", "Building Lean verification tools")
                    run(
                        ["lake", "build", "repl"],
                        code="repl_build_failed",
                        remedy="Review the build log and confirm the REPL revision matches Lean, then retry. Completed build steps are reused.",
                    )
                else:
                    phase(
                        "verification",
                        "Existing dependencies and verification tools found; no downloads or builds needed",
                    )
                phase(
                    "checking_readiness",
                    "Testing Lean with a small library import; your proofs are not compiled",
                )
                binary = protocol_check(root, run, mathlib=mathlib)
                diagnostics = source_diagnostics(root)
                initialize_leanflow_project(root, name=name)
                result.update(
                    status="ready",
                    code="ready",
                    message="Project is ready to prove.",
                    phase="ready",
                )
                result["capabilities"] = {
                    "verification": True,
                    "mathlib": mathlib,
                    "repl_path": str(binary),
                    "source_diagnostics": diagnostics,
                }
                result["fingerprint"] = fingerprint(root)
        except ProjectBusyError as exc:
            result.update(
                code="project_busy",
                message=str(exc),
                remediation="Wait for this project's active run or setup to finish, then retry.",
            )
        except (SetupCancelled, KeyboardInterrupt):
            result.update(
                status="cancelled",
                code="cancelled",
                message="Project setup was cancelled.",
                remediation="Retry setup to reuse downloaded dependencies and completed builds.",
            )
        except SetupFailure as exc:
            result.update(code=exc.code, message=safe_text(str(exc)), remediation=exc.remediation)
        except LeanFlowProjectError as exc:
            result.update(
                code="registration_failed",
                message=safe_text(str(exc)),
                remediation="Repair the .leanflow/project.yaml metadata or move it aside, then retry setup.",
            )
        except (OSError, ValueError) as exc:
            result.update(
                code="setup_io_failed",
                message=safe_text(str(exc)),
                remediation="Check folder permissions, configuration, and disk space, then retry setup.",
            )
        log.write(f"\n{result['status']}: {result['message']}\n")
        log.flush()
    # A rejected attempt must not invalidate the readiness receipt of the active run.
    if result["code"] != "project_busy":
        save_result(root, result)
    return result
