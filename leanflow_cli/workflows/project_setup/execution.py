"""Bind private setup capabilities and measured, serial process-resource budgets."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.runtime.harness.isolation import IsolationPolicy
from agent.runtime.harness.processes import ProcessTree
from leanflow_cli.runtime.storage import StoragePressure, storage_policy
from leanflow_cli.workflows.project_setup.process import SetupFailure, check_cancel


def _disk_reserve(root: Path) -> None:
    """Check the configured reserve without performing cache cleanup in a supervision loop."""
    free = shutil.disk_usage(root).free
    required = storage_policy().min_free_bytes
    if free < required:
        raise StoragePressure(free, required)


def setup_memory_limit_bytes() -> int:
    """Resolve the user-selected 1–20 GiB setup ceiling, defaulting to 16 GiB."""
    raw = os.environ.get("LEANFLOW_SETUP_MAX_RSS_GIB", "16")
    try:
        gib = int(raw)
    except ValueError:
        gib = 0
    if not 1 <= gib <= 20:
        raise SetupFailure(
            "setup_memory_setting",
            "Setup memory must be a whole number from 1 to 20 GiB.",
            "Set LEANFLOW_SETUP_MAX_RSS_GIB to 16, or choose 20 for a larger project.",
        )
    return gib * 1024**3


@dataclass
class SetupResourceUsage:
    """Record observed peaks and cumulative output without claiming unsampled process peaks."""

    peak_owned_rss_bytes: int = 0
    output_bytes: int = 0
    commands: int = 0
    peak_disk_growth_bytes: int = 0
    initial_free_bytes: int | None = None
    cleanup_verified: bool = True


@dataclass(frozen=True)
class SetupExecutionPolicy:
    """Constrain one serial private preparation campaign to deliberately granted paths."""

    isolation: IsolationPolicy
    environment: dict[str, str]
    executables: dict[str, str]
    deadline: float
    toolchain: str | None = None
    max_output_bytes: int = 4 * 1024**2
    max_owned_rss_bytes: int = field(default_factory=setup_memory_limit_bytes)
    max_disk_growth_bytes: int = 12 * 1024**3
    disk_guard: Callable[[Path], object] = _disk_reserve
    usage: SetupResourceUsage = field(default_factory=SetupResourceUsage, compare=False)
    owner: threading.Lock = field(default_factory=threading.Lock, compare=False)
    usage_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline) or any(
            type(value) is not int or value <= 0
            for value in (
                self.max_output_bytes,
                self.max_owned_rss_bytes,
                self.max_disk_growth_bytes,
            )
        ):
            raise ValueError(
                "Setup execution requires a finite deadline and positive resource budgets"
            )

    def check(self, cancel: threading.Event | None = None) -> None:
        """Stop at cancellation, campaign expiration, or unavailable disk reserve."""
        check_cancel(cancel)
        marker = self.isolation.snapshot_root / "lean-toolchain"
        if self.toolchain and marker.is_file() and marker.read_text().strip() != self.toolchain:
            raise SetupFailure(
                "setup_toolchain_changed",
                "Preparation no longer uses the explicitly bound toolchain.",
                "Prepare a new plan matching the existing pinned toolchain.",
            )
        if time.monotonic() >= self.deadline:
            raise SetupFailure(
                "setup_deadline",
                "Preparation exceeded its wall-clock deadline.",
                "Retry preparation with an adequate bounded time budget.",
            )
        self.disk_guard(self.isolation.snapshot_root)

    def evidence(self) -> dict[str, Any]:
        """Return public measured usage and explicit capability limits without environment values."""
        return {
            "peak_owned_rss_bytes": self.usage.peak_owned_rss_bytes,
            "output_bytes": self.usage.output_bytes,
            "commands": self.usage.commands,
            "peak_disk_growth_bytes": self.usage.peak_disk_growth_bytes,
            "cleanup_verified": self.usage.cleanup_verified,
            "max_owned_rss_bytes": self.max_owned_rss_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_disk_growth_bytes": self.max_disk_growth_bytes,
            "network_allowed": self.isolation.network_allowed,
            "rss_measurement": "conservative-sum-of-sampled-owned-process-rss",
            "disk_measurement": "conservative-filesystem-free-space-decrease",
        }

    def observe_rss(self, value: int) -> None:
        """Record a monotonic shared peak and enforce the sampled aggregate subprocess limit."""
        with self.usage_lock:
            self.usage.peak_owned_rss_bytes = max(self.usage.peak_owned_rss_bytes, value)
        if value > self.max_owned_rss_bytes:
            raise SetupFailure(
                "setup_memory_limit",
                "Preparation exceeded its aggregate owned-process memory budget.",
                "Reduce build parallelism or use a host with an adequate bounded memory budget.",
            )

    def observe_disk(self) -> None:
        """Track conservative filesystem expansion across the whole preparation campaign."""
        info = os.statvfs(self.isolation.snapshot_root)
        free = info.f_bavail * info.f_frsize
        with self.usage_lock:
            if self.usage.initial_free_bytes is None:
                self.usage.initial_free_bytes = free
            growth = max(0, self.usage.initial_free_bytes - free)
            self.usage.peak_disk_growth_bytes = max(self.usage.peak_disk_growth_bytes, growth)
        if growth > self.max_disk_growth_bytes:
            raise SetupFailure(
                "setup_disk_growth_limit",
                "Preparation exceeded its disk-expansion budget.",
                "Increase the bounded disk budget only after checking available storage.",
            )


def build_setup_execution_policy(
    stage: Path,
    *,
    canonical_root: Path,
    toolchain: str,
    network_allowed: bool,
    deadline: float,
    max_output_bytes: int = 4 * 1024**2,
    max_owned_rss_bytes: int | None = None,
    max_disk_growth_bytes: int = 12 * 1024**3,
) -> SetupExecutionPolicy:
    """Grant a private stage and installed official compiler while denying canonical/home data.

    Missing toolchains are an explicit preparation blocker. Automatic acquisition
    must use a separate controller-owned installation operation; this capability
    never makes existing Elan toolchains writable or runs a mutating Elan shim.
    """
    stage, canonical = stage.resolve(), canonical_root.resolve()
    if not stage.is_dir() or stage.is_relative_to(canonical) or canonical.is_relative_to(stage):
        raise ValueError("Private setup stage must be disjoint from the canonical project")
    if not re.fullmatch(r"leanprover/lean4:v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?", toolchain):
        raise SetupFailure(
            "setup_toolchain_unavailable",
            "Bounded preparation requires an installed official pinned Lean release.",
            "Install the requested official toolchain through project setup, then retry preparation.",
        )
    real_home = Path.home().resolve()
    elan_home = Path(os.environ.get("ELAN_HOME", str(real_home / ".elan"))).expanduser().resolve()
    compiler = elan_home / "toolchains" / toolchain.replace("/", "--").replace(":", "---")
    binaries = {name: compiler / "bin" / name for name in ("lean", "lake")}
    if not compiler.is_dir() or any(
        not path.is_file() or not os.access(path, os.X_OK) for path in binaries.values()
    ):
        raise SetupFailure(
            "setup_toolchain_unavailable",
            f"The pinned toolchain {toolchain} is not installed for bounded preparation.",
            "Install this toolchain through project setup, then retry. Existing installed toolchains will remain unchanged.",
        )
    if compiler.is_symlink() or any(
        not path.resolve().is_relative_to(compiler) for path in binaries.values()
    ):
        raise ValueError("Preparation toolchain cannot alias another installed authority")
    temporary = stage / ".leanflow-setup-private"
    for leaf in (temporary / "home", temporary / "tmp", temporary / "cache"):
        leaf.mkdir(parents=True, exist_ok=True, mode=0o700)
    system = [
        Path(path)
        for path in (
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
    ]
    reads = tuple(
        dict.fromkeys(
            path.resolve()
            for path in (
                *system,
                compiler,
                Path(sys.prefix),
                Path(sys.base_prefix),
                Path(sys.executable).resolve().parent,
            )
            if path.exists()
        )
    )
    path_value = os.pathsep.join(
        str(path)
        for path in (compiler / "bin", Path("/usr/bin"), Path("/bin"), Path("/opt/homebrew/bin"))
        if path.is_dir()
    )
    environment = {
        "PATH": path_value,
        "HOME": str(temporary / "home"),
        "TMPDIR": str(temporary / "tmp"),
        "TMP": str(temporary / "tmp"),
        "TEMP": str(temporary / "tmp"),
        "XDG_CACHE_HOME": str(temporary / "cache"),
        "ELAN_HOME": str(temporary / "elan"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "/usr/bin/false",
        "ELAN_NO_PROMPT": "1",
        "LEAN_NUM_THREADS": "1",
        "LANG": "C.UTF-8",
    }
    executables = {name: str(path) for name, path in binaries.items()}
    git = shutil.which("git", path=path_value)
    if sys.platform == "darwin" and git == "/usr/bin/git":
        try:
            selected = subprocess.run(
                ["/usr/bin/xcrun", "--find", "git"],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(temporary / "home"),
                    "TMPDIR": str(temporary / "tmp"),
                },
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            actual = Path(selected)
            if (
                not actual.is_absolute()
                or not actual.is_file()
                or not any(actual.resolve().is_relative_to(root) for root in reads)
            ):
                raise ValueError("Selected Darwin Git escapes granted system tooling")
            git = str(actual)
            environment["PATH"] = str(actual.parent) + os.pathsep + path_value
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise SetupFailure(
                "setup_git_unavailable",
                "The actual Darwin Git executable could not be selected safely.",
                "Install or select the command line developer tools, then retry preparation.",
            ) from error
    if git:
        executables["git"] = git
    return SetupExecutionPolicy(
        isolation=IsolationPolicy(
            stage,
            (temporary,),
            reads,
            (canonical, real_home),
            temporary / "tmp",
            (compiler, *reads),
            network_allowed=network_allowed,
        ),
        environment=environment,
        executables=executables,
        deadline=deadline,
        toolchain=toolchain,
        max_output_bytes=max_output_bytes,
        max_owned_rss_bytes=(
            setup_memory_limit_bytes() if max_owned_rss_bytes is None else max_owned_rss_bytes
        ),
        max_disk_growth_bytes=max_disk_growth_bytes,
    )


def owned_rss_bytes(process: subprocess.Popen[bytes], tree: ProcessTree) -> int:
    """Conservatively sum sampled owned RSS, including shared pages once per process."""
    tree.refresh()
    table = subprocess.run(
        ["/bin/ps", "-axo", "pid=,lstart=,rss="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    total = 0
    for line in table.stdout.splitlines():
        parts = line.split()
        if len(parts) != 7 or not parts[0].isdigit() or not parts[-1].isdigit():
            continue
        pid = int(parts[0])
        start = " ".join(parts[1:6])
        remembered = " ".join(tree.descendants.get(pid, "").split())
        if (pid == process.pid and process.poll() is None) or remembered == start:
            total += int(parts[-1]) * 1024
    return total
