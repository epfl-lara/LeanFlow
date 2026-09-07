"""Define read roots and explicit write grants for protected Lean subprocesses."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
from collections.abc import Sequence
from pathlib import Path


class IsolationUnavailable(RuntimeError):
    """Refuse Lean execution when no supported kernel isolation is available."""


def _roots(project: Path, workspace: Path, private: Path) -> list[Path]:
    """Return only runtime, toolchain, project, and OS locations needed for reads."""
    candidates = [
        project,
        project / ".lake" / "packages",
        workspace,
        private,
        Path(__file__).resolve().parents[3],
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.executable).resolve().parent,
        Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))),
        *map(
            Path,
            (
                "/System",
                "/Library/Frameworks",
                "/Library/Developer",
                "/Library/Preferences/.GlobalPreferences.plist",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc",
                "/opt/homebrew",
            ),
        ),
    ]
    return list(dict.fromkeys(path.resolve() for path in candidates if path.exists()))


def _merged_usr_links(prefix: Path = Path("/")) -> list[tuple[str, str]]:
    """Return (target, link) pairs for top-level symlinks into /usr.

    On merged-/usr distributions (Ubuntu 20.04+, Debian 11+, Fedora, Arch) /bin,
    /sbin, /lib and /lib64 are symlinks into /usr. ``_roots`` resolves paths
    before binding, which collapses them to their /usr targets, so the link
    names themselves never exist inside the sandbox. Every dynamically linked
    binary names its ELF interpreter as /lib64/ld-linux-x86-64.so.2, so without
    these links exec fails with a bare "No such file or directory".
    """
    pairs: list[tuple[str, str]] = []
    for name in ("bin", "sbin", "lib", "lib64", "lib32", "libx32"):
        link = prefix / name
        if link.is_symlink():
            pairs.append((os.readlink(link), "/" + name))
    return pairs


def _sandbox_command(
    argv: Sequence[str],
    *,
    project: Path,
    workspace: Path,
    private: Path,
    extra_writable_roots: Sequence[Path] = (),
    network_allowed: bool = False,
) -> list[str]:
    """Wrap a command in seatbelt or a private Bubblewrap mount/PID namespace."""
    readable = _roots(project, workspace, private)
    writable = [workspace, private]
    for root in extra_writable_roots:
        target = root.resolve(strict=True)
        if not target.is_relative_to(project) or target == project:
            raise IsolationUnavailable(
                "Additional writable artifacts must be inside the project, never the project root."
            )
        writable.append(target)
        readable.append(target)
    system = platform.system()
    if system == "Darwin":
        binary = shutil.which("sandbox-exec")
        if not binary:
            raise IsolationUnavailable(
                "Protected Lean checks require macOS sandbox-exec; no unsafe fallback is enabled."
            )

        def rule(path: Path) -> str:
            return f"({'subpath' if path.is_dir() else 'literal'} {json.dumps(str(path))})"

        profile = "\n".join(
            [
                "(version 1)",
                "(deny default)",
                "(allow file-read-metadata)",
                '(allow file-read* (literal "/"))',
                "(allow file-read* " + " ".join(rule(path) for path in readable) + ")",
                '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
                "(allow file-write* "
                + " ".join(rule(path) for path in writable)
                + ' (literal "/dev/null"))',
                "(allow process-exec)",
                "(allow process-fork)",
                "(allow process-info* (target self))",
                "(allow signal (target self))",
                "(allow sysctl-read)",
                '(allow mach-lookup (global-name "com.apple.system.logger") (global-name "com.apple.logd") (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.system.notification_center"))',
                "(allow network*)" if network_allowed else "(deny network*)",
            ]
        )
        return [binary, "-p", profile, *argv]
    if system == "Linux":
        binary = shutil.which("bwrap")
        if not binary:
            raise IsolationUnavailable(
                "Protected Lean checks require Bubblewrap (bwrap) with user namespaces on Linux; install it or use a host with supported isolation."
            )
        command = [
            binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
        if not network_allowed:
            command.append("--unshare-net")
        command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
        for root in sorted(readable, key=lambda path: len(path.parts)):
            command.extend(["--ro-bind", str(root), str(root)])
        # Recreate the merged-/usr symlinks the resolved binds above dropped,
        # otherwise nothing in the sandbox can be executed at all.
        for target, link in _merged_usr_links():
            command.extend(["--symlink", target, link])
        for root in writable:
            command.extend(["--bind", str(root), str(root)])
        return [*command, "--chdir", str(project), "--", *argv]
    raise IsolationUnavailable(
        f"Protected Lean checks are unavailable on {system}; use macOS sandbox-exec or Linux Bubblewrap."
    )


def _environment(private: Path) -> dict[str, str]:
    """Keep toolchain settings while excluding provider credentials and runtime ledgers."""
    keys = (
        "PATH",
        "HOME",
        "ELAN_HOME",
        "ELAN_TOOLCHAIN",
        "LEAN_PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
    )
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(
        {
            "TMPDIR": str(private / "tmp"),
            "TMP": str(private / "tmp"),
            "TEMP": str(private / "tmp"),
            "XDG_CACHE_HOME": str(private / "cache"),
            "LEANFLOW_HOME": str(private / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LEANFLOW_PROJECT_LEAN_ADMISSION": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    for name in ("tmp", "cache", "home"):
        (private / name).mkdir(mode=0o700, exist_ok=True)
    return env


def _validated_paths(project_root: Path, workspace: Path) -> tuple[Path, Path]:
    """Reject broad writable workspaces or a workspace containing the runtime itself."""
    project = project_root.resolve(strict=True)
    work = workspace.resolve(strict=True)
    runtime = Path(__file__).resolve().parents[3]
    if not project.is_dir() or not work.is_dir():
        raise ValueError("Project and checker workspace must be existing directories.")
    if project.is_relative_to(work) or runtime.is_relative_to(work):
        raise ValueError("Checker workspace must not contain the project or LeanFlow runtime.")
    # A pre-existing hardlink could otherwise put a protected inode below an
    # allowed write path. The sandbox also rejects creating new hardlinks.
    for parent, directories, files in os.walk(work, followlinks=False):
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            entry = (Path(parent) / name).lstat()
            if stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1:
                raise ValueError("Checker workspaces cannot contain hardlinked files.")
    return project, work
