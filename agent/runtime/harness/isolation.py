"""Confine native tools to a filtered snapshot and explicitly granted runtime paths."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class IsolationUnavailable(RuntimeError):
    """Refuse native execution without supported kernel filesystem isolation."""


@dataclass(frozen=True)
class IsolationPolicy:
    """Grant snapshot, native state and toolchain paths while excluding controller data."""

    snapshot_root: Path
    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    denied_roots: tuple[Path, ...] = ()
    temp_root: Path | None = None
    readonly_roots: tuple[Path, ...] = ()
    network_allowed: bool = True


def build_runtime_isolation(
    backend: str,
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    writable_roots: Sequence[Path] = (),
    readable_roots: Sequence[Path] = (),
    denied_roots: Sequence[Path] = (),
) -> IsolationPolicy:
    """Prepare scratch and grant the selected runtime's original configuration home.

    Native state remains native: no credentials or configuration are copied. Runtime
    homes contain session history and must not be used to store controller secrets.
    """
    if backend not in {"codex", "claude", "lemex"}:
        raise IsolationUnavailable(f"Unsupported native backend: {backend}")
    environment = {**os.environ, **(env or {})}
    home = Path(environment.get("HOME", str(Path.home())))
    home_key = "CLAUDE_CONFIG_DIR" if backend == "claude" else f"{backend.upper()}_HOME"
    native_home = Path(environment.get(home_key, str(home / f".{backend}"))).resolve()
    native_home.mkdir(parents=True, exist_ok=True)
    temporary = cwd.resolve() / ".leanflow-runtime" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    system_roots = [
        Path(p)
        for p in (
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
        )
    ]
    if platform.system() == "Linux":
        # Ubuntu's resolver file points into /run, outside the /etc bind mount.
        system_roots.append(Path("/etc/resolv.conf"))
    runtime_roots = [
        native_home,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.executable).resolve().parent,
        Path(environment.get("ELAN_HOME", str(home / ".elan"))),
    ]
    if backend == "claude":
        runtime_roots.extend(
            [
                home / ".claude.json",
                home / "Library/Keychains",
                home / ".local/share/claude",
                home / ".local/bin/claude",
            ]
        )
    reads = tuple(
        dict.fromkeys(
            path.resolve()
            for path in [*system_roots, *runtime_roots, *readable_roots]
            if path.exists()
        )
    )
    writes = tuple(
        dict.fromkeys([native_home, temporary, *(path.resolve() for path in writable_roots)])
    )
    project_config = tuple(
        cwd.resolve() / name for name in (".codex", ".lemex", ".claude", ".mcp.json")
    )
    native_config = tuple(
        native_home / name
        for name in (
            "config.toml",
            "settings.json",
            "settings.local.json",
            "AGENTS.md",
            "CLAUDE.md",
            "skills",
            "plugins",
            "rules",
            "prompts",
            "agents",
            "hooks",
            "profiles",
            "worktrees",
        )
    )
    native_config += tuple(native_home.glob("*.config.toml"))
    return IsolationPolicy(
        cwd.resolve(),
        writes,
        reads,
        (*tuple(path.resolve() for path in denied_roots), *project_config),
        temporary,
        (Path(__file__).resolve().parents[3], *native_config),
    )


def _validate_snapshot(root: Path) -> None:
    """Reject links that would expose protected bytes through a writable snapshot."""
    if not root.is_dir() or root == Path(root.anchor):
        raise IsolationUnavailable("The native workspace must be an existing isolated directory.")
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            metadata = path.lstat()
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                raise IsolationUnavailable(f"Snapshot symlink escapes its workspace: {path}")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise IsolationUnavailable(f"Snapshot hardlink is not permitted: {path}")


def _rule(path: Path) -> str:
    """Return a seatbelt path filter without interpolating unescaped paths."""
    return f"({'subpath' if path.is_dir() else 'literal'} {json.dumps(str(path))})"


def _carve_root(root: Path, denies: tuple[Path, ...]) -> list[Path]:
    """Split a Linux bind root around denied descendants, inspecting names only."""
    if any(root == denied or root.is_relative_to(denied) for denied in denies):
        return []
    if not any(denied.is_relative_to(root) for denied in denies):
        return [root]
    if not root.is_dir():
        return []
    result: list[Path] = []
    for child in root.iterdir():
        # Symlinks do not create a second route to the deliberately omitted tree.
        if child.is_symlink():
            target = child.resolve()
            if any(
                target == denied or target.is_relative_to(denied) or denied.is_relative_to(target)
                for denied in denies
            ):
                continue
        result.extend(_carve_root(child, denies))
    return result


def sandbox_command(
    argv: Sequence[str], policy: IsolationPolicy, *, writable: bool = True
) -> list[str]:
    """Wrap an invocation in fail-closed macOS seatbelt or Linux Bubblewrap."""
    root = policy.snapshot_root.resolve(strict=True)
    _validate_snapshot(root)
    reads = tuple(
        dict.fromkeys(
            [
                root,
                *(p.resolve() for p in policy.readable_roots),
                *(p.resolve() for p in policy.writable_roots),
            ]
        )
    )
    writes = tuple(
        dict.fromkeys(
            [*([root] if writable else []), *(p.resolve() for p in policy.writable_roots)]
        )
    )
    denies = tuple(p.resolve() for p in policy.denied_roots)
    for path in writes:
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise IsolationUnavailable("Broad filesystem or user-home write grants are forbidden.")
    # Explicit grants nested below a protected parent are the only exceptions.
    exceptions = tuple(p for p in reads if any(p != d and p.is_relative_to(d) for d in denies))
    write_exceptions = tuple(
        p for p in writes if any(p != d and p.is_relative_to(d) for d in denies)
    )
    readonly = tuple(p.resolve() for p in policy.readonly_roots)
    system = platform.system()
    if system == "Darwin":
        binary = shutil.which("sandbox-exec")
        if not binary:
            raise IsolationUnavailable(
                "Native backends require macOS sandbox-exec; no unsafe fallback is enabled."
            )
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/") (subpath "/dev"))',
            "(allow file-read* " + " ".join(_rule(p) for p in reads) + ")",
            "(allow file-write* " + " ".join(_rule(p) for p in writes) + ' (literal "/dev/null"))',
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow process-info* (target self))",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            '(allow user-preference-read user-preference-write (preference-domain "com.openai.codex"))',
            '(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center") (ipc-posix-name-prefix "apple.cfprefs."))',
        ]
        if policy.network_allowed:
            lines.append("(allow network*)")
        for operation, allowed_exceptions in (
            ("file-read-data", exceptions),
            ("file-write*", write_exceptions),
        ):
            for denied in denies:
                children = [p for p in allowed_exceptions if p.is_relative_to(denied)]
                predicate = _rule(denied)
                if children:
                    predicate = (
                        "(require-all "
                        + predicate
                        + " (require-not (require-any "
                        + " ".join(_rule(p) for p in children)
                        + ")))"
                    )
                lines.append(f"(deny {operation} {predicate})")
        for protected in readonly:
            predicate = _rule(protected)
            if root != protected and root.is_relative_to(protected):
                predicate = f"(require-all {predicate} (require-not {_rule(root)}))"
            lines.append(f"(deny file-write* {predicate})")
        return [binary, "-p", "\n".join(lines), *argv]
    if system == "Linux":
        binary = shutil.which("bwrap")
        if not binary:
            raise IsolationUnavailable(
                "Native backends require Bubblewrap (bwrap) and user namespaces on Linux."
            )
        command = [
            binary,
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if not policy.network_allowed:
            command.append("--unshare-net")
        for paths, option in ((reads, "--ro-bind"), (writes, "--bind")):
            for path in sorted(paths, key=lambda p: len(p.parts)):
                allowed_exceptions = exceptions if option == "--ro-bind" else write_exceptions
                excluded = denies if option == "--ro-bind" else (*denies, *readonly)
                carved = (
                    [path]
                    if path in allowed_exceptions
                    and not (
                        option == "--bind"
                        and any(path.is_relative_to(p) for p in readonly)
                        and not path.is_relative_to(root)
                    )
                    else _carve_root(path, excluded)
                )
                for piece in carved:
                    if piece.exists():
                        command.extend([option, str(piece), str(piece)])
        for name in ("bin", "sbin", "lib", "lib64"):
            path = Path("/") / name
            if path.is_symlink():
                command.extend(["--symlink", os.readlink(path), str(path)])
        return [*command, "--chdir", str(root), "--", *argv]
    raise IsolationUnavailable(f"Native backend isolation requires macOS or Linux; found {system}.")
