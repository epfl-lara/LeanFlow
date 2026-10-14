"""Acquire one missing official compiler without changing installed toolchains or native configuration."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent.runtime.harness.isolation import IsolationPolicy
from core.home import leanflow_home
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.publication_io import fcntl
from leanflow_cli.workflows.project_setup.execution import (
    SetupExecutionPolicy,
    build_setup_execution_policy,
)
from leanflow_cli.workflows.project_setup.process import (
    Progress,
    SetupFailure,
    check_cancel,
    run_command,
)
from leanflow_cli.workflows.project_setup.toolchain_publication import publish_compiler


def _compiler_version(
    binary: Path,
    policy: SetupExecutionPolicy,
    toolchain: str,
    cancel: threading.Event | None,
) -> str:
    """Check compiler version under the same cumulative acquisition budget."""
    checker = dataclasses.replace(policy, executables={"lean": str(binary)})
    status, output = run_command(
        ["lean", "--version"],
        cwd=policy.isolation.snapshot_root,
        log=io.StringIO(),
        cancel=cancel,
        progress=None,
        phase="checking_compiler",
        execution_policy=checker,
        stdout_only=True,
    )
    release = toolchain.partition(":v")[2]
    version = output.strip()
    if status or not re.fullmatch(
        r"Lean \(version " + re.escape(release) + r", [^\r\n]+\)", version
    ):
        raise SetupFailure(
            "setup_toolchain_version_mismatch",
            "The selected compiler does not report its exact pinned release.",
            "Retain acquisition evidence and inspect the compiler before preparing a project.",
        )
    return version


@contextlib.contextmanager
def _installation_owner(deadline: float, cancel: threading.Event | None) -> Iterator[None]:
    """Serialize cooperating toolchain acquisition before enumerating protected installations."""
    if fcntl is None:
        raise SetupFailure(
            "setup_isolation_unavailable",
            "Bounded compiler installation requires POSIX ownership.",
            "Use a supported macOS or Linux preparation host.",
        )
    directory = leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        directory / "ownership.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        while True:
            check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise SetupFailure(
                    "setup_deadline",
                    "Compiler acquisition ownership exceeded its deadline.",
                    "Retry after the other bounded acquisition has finished.",
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def _installation_policy(
    stage: Path,
    canonical: Path,
    private_elan: Path,
    executable: Path,
    *,
    deadline: float,
    network_allowed: bool,
) -> SetupExecutionPolicy:
    """Grant a fresh private Elan home while withholding all native configuration/toolchain writes."""
    temporary = stage / ".leanflow-toolchain-private"
    for path in (
        temporary / "home",
        temporary / "tmp",
        temporary / "cache",
        private_elan,
    ):
        if path.is_symlink():
            raise ValueError("Compiler installation cannot use an aliased writable directory")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    system = tuple(
        Path(name).resolve()
        for name in (
            "/System",
            "/Library/Frameworks",
            "/Library/Developer",
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/opt/homebrew",
            "/private/var/db/timezone",
            "/etc/resolv.conf",
        )
        if Path(name).exists()
    )
    reads = tuple(
        dict.fromkeys(
            (
                *system,
                executable.parent,
                Path(sys.prefix).resolve(),
                Path(sys.base_prefix).resolve(),
            )
        )
    )
    return SetupExecutionPolicy(
        isolation=IsolationPolicy(
            stage,
            (temporary,),
            reads,
            (canonical, Path.home().resolve()),
            temporary / "tmp",
            (*system, executable.parent),
            network_allowed=network_allowed,
        ),
        environment={
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(temporary / "home"),
            "ELAN_HOME": str(private_elan),
            "ELAN_NO_PROMPT": "1",
            "LEAN_NUM_THREADS": "1",
            "TMPDIR": str(temporary / "tmp"),
            "TMP": str(temporary / "tmp"),
            "TEMP": str(temporary / "tmp"),
            "XDG_CACHE_HOME": str(temporary / "cache"),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        },
        executables={"elan": str(executable)},
        deadline=deadline,
    )


def ensure_official_toolchain(
    stage: Path,
    *,
    canonical_root: Path,
    toolchain: str,
    deadline: float,
    network_allowed: bool,
    cancel: threading.Event | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Acquire an exact compiler and expose explicit command cleanup evidence on every failure."""
    policies: list[SetupExecutionPolicy] = []
    try:
        return _ensure_official_toolchain(
            stage,
            canonical_root=canonical_root,
            toolchain=toolchain,
            deadline=deadline,
            network_allowed=network_allowed,
            cancel=cancel,
            progress=progress,
            policies=policies,
        )
    except BaseException as error:
        resources: dict[str, Any] = policies[-1].evidence() if policies else {"commands": 0}
        resources["cleanup_verified"] = all(policy.usage.cleanup_verified for policy in policies)
        resources["cleanup_basis"] = (
            "verified_owned_command_shutdown"
            if any(policy.usage.commands for policy in policies)
            else "no_acquisition_command_launched"
        )
        setattr(error, "acquisition_resources", resources)
        raise


def _ensure_official_toolchain(
    stage: Path,
    *,
    canonical_root: Path,
    toolchain: str,
    deadline: float,
    network_allowed: bool,
    cancel: threading.Event | None,
    progress: Progress | None,
    policies: list[SetupExecutionPolicy],
) -> dict[str, Any]:
    """Install a missing exact official release, or reuse an unchanged valid existing compiler.

    Run trusted Elan in a fresh private home, then exclusively move only the checked
    compiler directory into the missing native destination. Existing incomplete or
    aliased destinations require explicit repair. Native configuration, credentials,
    defaults and overrides are inaccessible to the installer.
    """
    if not re.fullmatch(r"leanprover/lean4:v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?", toolchain):
        raise ValueError("Compiler acquisition requires an exact official Lean release")
    stage, canonical = stage.resolve(), canonical_root.resolve()
    if not stage.is_dir() or stage.is_relative_to(canonical) or canonical.is_relative_to(stage):
        raise ValueError("Compiler acquisition requires a disjoint private preparation stage")
    configured_home = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))).expanduser()
    if configured_home.is_symlink():
        raise ValueError("Compiler acquisition cannot modify an aliased Elan home")
    elan_home = configured_home.resolve()
    compiler = elan_home / "toolchains" / toolchain.replace("/", "--").replace(":", "---")
    with _installation_owner(deadline, cancel):
        from leanflow_cli.workflows.project_setup.toolchain_recovery import (
            recover_compiler_publications,
        )

        recovery_error: SetupFailure | None = None
        try:
            recover_compiler_publications(compiler, deadline=deadline, cancel=cancel)
        except SetupFailure as error:
            recovery_error = error
        if compiler.exists() or compiler.is_symlink():
            if compiler.is_symlink() or any(
                not (compiler / "bin" / name).is_file()
                or not os.access(compiler / "bin" / name, os.X_OK)
                for name in ("lean", "lake")
            ):
                raise SetupFailure(
                    "setup_toolchain_incomplete",
                    "The requested compiler location already exists and is incomplete or aliased.",
                    "Inspect and repair this existing Elan installation explicitly; preparation will not replace it.",
                )
            reused_policy = build_setup_execution_policy(
                stage,
                canonical_root=canonical,
                toolchain=toolchain,
                deadline=deadline,
                network_allowed=False,
            )
            policies.append(reused_policy)
            version = _compiler_version(compiler / "bin/lean", reused_policy, toolchain, cancel)
            return {
                "status": "reused",
                "toolchain": toolchain,
                "compiler_root": str(compiler),
                "resources": reused_policy.evidence(),
                "compiler_version": version,
                "recovery": (
                    {
                        "status": "retained",
                        "reason": str(recovery_error),
                        "remediation": recovery_error.remediation,
                    }
                    if recovery_error
                    else {"status": "checked"}
                ),
            }
        if recovery_error is not None:
            raise recovery_error
        if not network_allowed:
            raise SetupFailure(
                "setup_toolchain_network_required",
                "The pinned compiler is missing and network acquisition is disabled.",
                "Allow official compiler acquisition or install the exact pinned release before retrying.",
            )
        native = elan_home / "bin/elan"
        if not native.is_file() or not os.access(native, os.X_OK):
            located = shutil.which("elan")
            if located is None:
                raise SetupFailure(
                    "elan_missing",
                    "Lean's Elan installer is unavailable.",
                    "Install Elan first, then retry preparation.",
                )
            native = Path(located).resolve()
        existing = (
            tuple(sorted((elan_home / "toolchains").iterdir()))
            if (elan_home / "toolchains").is_dir()
            else ()
        )
        metadata = tuple(
            elan_home / name for name in ("settings.toml", "env", "known-projects", "update-hashes")
        )
        private_elan = stage / ".leanflow-toolchain-private" / ("elan-" + uuid.uuid4().hex)
        policy = _installation_policy(
            stage,
            canonical,
            private_elan,
            native,
            deadline=deadline,
            network_allowed=True,
        )
        policies.append(policy)
        policy.check(cancel)
        protected = {}
        for path in (*existing, *metadata):
            policy.check(cancel)
            protected[str(path)] = entry_identity(path)
        policy.check(cancel)
        log_path = (
            leanflow_home()
            / "toolchain-installations"
            / (toolchain.replace("/", "--").replace(":", "---") + "-" + uuid.uuid4().hex + ".log")
        )
        with log_path.open("x", encoding="utf-8") as log:
            log_path.chmod(0o600)
            try:
                status, output = run_command(
                    ["elan", "toolchain", "install", toolchain],
                    cwd=stage,
                    log=log,
                    cancel=cancel,
                    progress=progress,
                    phase="acquiring_toolchain",
                    execution_policy=policy,
                )
                if status:
                    raise SetupFailure(
                        "setup_toolchain_download_failed",
                        "Official compiler acquisition failed: " + output[-4000:],
                        "Inspect the bounded installer log, check network access, and retry without removing existing toolchains.",
                    )
            finally:
                if any(
                    entry_identity(Path(path)) != checksum for path, checksum in protected.items()
                ):
                    raise SetupFailure(
                        "setup_toolchain_protected_changed",
                        "An existing toolchain or Elan configuration changed during acquisition.",
                        "Retain the installer evidence and reconcile the external change before preparing again.",
                    )
                if (elan_home / "toolchains").is_dir() and set(
                    (elan_home / "toolchains").iterdir()
                ) != set(existing):
                    raise SetupFailure(
                        "setup_toolchain_inventory_changed",
                        "Another compiler entry appeared during the bounded acquisition.",
                        "Inspect the retained installer evidence before preparing again.",
                    )
        policy.check(cancel)
        downloaded = private_elan / "toolchains" / compiler.name
        if downloaded.is_symlink() or any(
            not (downloaded / "bin" / name).is_file()
            or not os.access(downloaded / "bin" / name, os.X_OK)
            or not (downloaded / "bin" / name).resolve().is_relative_to(downloaded)
            for name in ("lean", "lake")
        ):
            raise SetupFailure(
                "setup_toolchain_incomplete",
                "Acquisition did not produce a complete pinned compiler.",
                "Inspect the retained installation evidence before explicitly repairing this compiler location.",
            )
        private_version = _compiler_version(downloaded / "bin/lean", policy, toolchain, cancel)
        publication = publish_compiler(
            downloaded, compiler, toolchain=toolchain, policy=policy, cancel=cancel
        )
        relocated = build_setup_execution_policy(
            stage,
            canonical_root=canonical,
            toolchain=toolchain,
            deadline=deadline,
            network_allowed=False,
        )
        relocated = dataclasses.replace(
            relocated, usage=policy.usage, owner=policy.owner, usage_lock=policy.usage_lock
        )
        policies.append(relocated)
        relocated_version = _compiler_version(compiler / "bin/lean", relocated, toolchain, cancel)
        if relocated_version != private_version:
            raise SetupFailure(
                "setup_toolchain_relocation_changed",
                "The compiler identity changed after relocation.",
                "Inspect the retained compiler and publication evidence before preparing a project.",
            )
        return {
            "status": "installed",
            "toolchain": toolchain,
            "compiler_root": str(compiler),
            "resources": policy.evidence(),
            "log_path": str(log_path),
            "publication": publication,
            "compiler_version": relocated_version,
        }
