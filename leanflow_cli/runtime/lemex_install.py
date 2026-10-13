"""Install an immutable Lemex distribution while leaving its native home untouched.

Existing packaged distributions are copied and pinned by content. Fresh installations
use Lemex's own installer at an exact source commit, with a private npm prefix.
Explicit refresh adopts a local upgrade without changing older runtime objects.
No release binary is assumed to exist, and no runtime credentials are inspected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.home import leanflow_home

LEMEX_SOURCE_URL = "https://github.com/Lemmy00/lemex.git"
LEMEX_SOURCE_COMMIT = "2f0e12c10d0baf7404a83da3b8cb8f04b77822c9"


class LemexInstallError(RuntimeError):
    """Report missing build prerequisites, incompatible CLIs, or damaged installs."""


def _root() -> Path:
    """Return the managed distribution store, independent of LEMEX_HOME."""
    return leanflow_home() / "runtimes" / "lemex"


def _sha256(path: Path) -> str:
    """Hash a file without loading a large native binary into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_identity(root: Path) -> tuple[str, dict[str, str]]:
    """Hash the complete self-contained package and reject escaping symlinks."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LemexInstallError(f"Lemex package contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha256(path)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), files


def probe_lemex(executable: Path) -> str:
    """Check the installed exec protocol without login or any inference request."""
    try:
        version = subprocess.run(
            [str(executable), "--version"], check=True, text=True, capture_output=True, timeout=15
        ).stdout.strip()
        help_text = subprocess.run(
            [str(executable), "exec", "--help"],
            check=True,
            text=True,
            capture_output=True,
            timeout=15,
        ).stdout
        resume = subprocess.run(
            [str(executable), "exec", "resume", "--help"],
            check=True,
            text=True,
            capture_output=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise LemexInstallError(f"Could not inspect Lemex CLI at {executable}: {exc}") from exc
    required = ("--json", "--output-last-message", "--skip-git-repo-check")
    if (
        "lemex" not in version.lower()
        or any(flag not in help_text for flag in required)
        or not resume
    ):
        raise LemexInstallError(
            f"Lemex at {executable} does not support the required exec protocol"
        )
    return version


def _read_install(*, verify: bool = False) -> dict[str, Any] | None:
    """Resolve the atomic install pointer and optionally verify its pinned content."""
    pointer = _root() / "current.json"
    if not pointer.is_file():
        return None
    try:
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
        digest = manifest["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid content identity")
        distribution = _root() / "objects" / digest
        entrypoint = Path(manifest["entrypoint"])
        if entrypoint.is_absolute() or ".." in entrypoint.parts:
            raise ValueError("entrypoint escapes its distribution")
        if distribution.is_symlink():
            raise ValueError("distribution directory is a symlink")
        executable = distribution / entrypoint
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("executable is missing")
        if not executable.resolve().is_relative_to(distribution.resolve()):
            raise ValueError("executable escaped its distribution")
        if verify and _tree_identity(distribution)[0] != digest:
            raise ValueError("distribution content differs from its pinned identity")
        return {**manifest, "executable": str(executable), "distribution_root": str(distribution)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LemexInstallError(
            f"Managed Lemex installation is damaged: {exc}. Inspect the runtime store before reinstalling."
        ) from exc


def resolve_lemex_executable() -> Path | None:
    """Prefer the private managed distribution, then an existing user installation."""
    installed = _read_install()
    if installed:
        return Path(installed["executable"])
    executable = shutil.which("lemex")
    return Path(executable) if executable else None


def managed_distribution_root() -> Path | None:
    """Return companion-resource access required by the managed runtime sandbox."""
    installed = _read_install()
    if installed:
        return Path(installed["distribution_root"])
    executable = shutil.which("lemex")
    if executable:
        try:
            return _package_root(Path(executable))
        except LemexInstallError:
            pass
    return None


def _package_root(executable: Path) -> Path:
    """Find an npm or native source bundle, including its companion executables."""
    resolved = executable.resolve(strict=True)
    package = resolved.parent.parent
    metadata = package / "package.json"
    try:
        if resolved.name not in {"lemex.js", "lemex"}:
            raise ValueError("not a packaged Lemex executable")
        if metadata.is_file():
            if json.loads(metadata.read_text())["name"] != "lemex":
                raise ValueError("not a Lemex package")
            if not (package / "vendor").is_dir():
                raise ValueError("platform companion files are not bundled")
        else:
            marker = json.loads((package / "lemex-install.json").read_text())
            commit = marker.get("source_commit", "") if isinstance(marker, dict) else ""
            with resolved.open("rb") as stream:
                magic = stream.read(4)
            native_magic = {
                b"\x7fELF",
                b"\xcf\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xca\xfe\xba\xbe",
            }
            if (
                not isinstance(commit, str)
                or len(commit) != 40
                or any(c not in "0123456789abcdef" for c in commit)
                or magic not in native_magic
                or not (package / "codex-package.json").is_file()
                or not (package / "codex-path").is_dir()
            ):
                raise ValueError("not a complete native source bundle")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LemexInstallError(
            "Existing Lemex is not a self-contained distribution; build the pinned source instead."
        ) from exc
    return package


@contextmanager
def _install_lock() -> Iterator[None]:
    """Serialize installers while readers continue using the last complete bundle."""
    import fcntl

    _root().mkdir(parents=True, exist_ok=True)
    with (_root() / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publish(package: Path, entrypoint: Path, *, source: str, commit: str | None) -> dict[str, Any]:
    """Copy and verify a whole distribution before atomically selecting it."""
    _tree_identity(package)
    store = _root() / "objects"
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=_root()) as temporary:
        stage = Path(temporary) / "package"
        shutil.copytree(package, stage, symlinks=False)
        version = probe_lemex(stage / entrypoint)
        digest, files = _tree_identity(stage)
        destination = store / digest
        if destination.exists():
            if _tree_identity(destination)[0] != digest:
                raise LemexInstallError("Existing managed distribution failed content verification")
        else:
            stage.rename(destination)
        manifest = {
            "schema": 1,
            "sha256": digest,
            "entrypoint": entrypoint.as_posix(),
            "version": version,
            "source": source,
            "source_commit": commit,
            "files": files,
        }
        pointer = Path(temporary) / "current.json"
        pointer.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        pointer.replace(_root() / "current.json")
    installed = _read_install()
    assert installed is not None
    return installed


def _build_from_source() -> dict[str, Any]:
    """Run Lemex's pinned native installer with a private executable prefix.

    Its own default setup creates only missing native config files. Existing
    LEMEX_HOME content and the user's globally installed executable stay intact.
    """
    missing = [
        name for name in ("git", "node", "npm", "rg", "bash", "python3") if not shutil.which(name)
    ]
    if not (shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")):
        missing.append("C compiler (cc, clang or gcc)")
    if not (shutil.which("cargo") and shutil.which("rustc")) and not shutil.which("curl"):
        missing.append("curl (to install Rust)")
    if missing:
        raise LemexInstallError("Pinned Lemex source installation requires: " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix=".source-", dir=_root()) as temporary:
        work = Path(temporary)
        source = work / "source"
        source.mkdir()
        for args in (
            ["git", "init", "--quiet", str(source)],
            [
                "git",
                "-C",
                str(source),
                "fetch",
                "--quiet",
                "--depth=1",
                LEMEX_SOURCE_URL,
                LEMEX_SOURCE_COMMIT,
            ],
            ["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ):
            subprocess.run(args, check=True)
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if commit != LEMEX_SOURCE_COMMIT:
            raise LemexInstallError("Fetched Lemex source does not match the pinned commit")
        prefix = work / "prefix"
        env = dict(os.environ)
        env["LEMEX_INSTALL_PREFIX"] = str(prefix)
        env["STABLE_GIT_COMMIT"] = commit
        # Remote config imports are user setup actions, not part of installing binaries.
        env.pop("LEMEX_COPY_CONFIG_FROM", None)
        subprocess.run(
            ["bash", str(source / "scripts/install/install_from_source.sh")], env=env, check=True
        )
        executable = prefix / "bin" / "lemex"
        package = _package_root(executable)
        return _publish(
            package,
            executable.resolve().relative_to(package),
            source="pinned-source",
            commit=commit,
        )


def ensure_lemex(*, build: bool = True, refresh: bool = False) -> dict[str, Any]:
    """Verify a content pin, optionally replacing it with a local package or source build.

    Ordinary checks retain the existing pin without discovery or network access.
    Refresh snapshots a compatible external installation before publishing its
    pointer; older objects remain usable by active jobs. Retain a healthy existing
    pin when no compatible external replacement exists, including source-built pins.
    """
    if os.name != "posix":
        raise LemexInstallError("Use LeanFlow through WSL2 on Windows to install Lemex.")
    with _install_lock():
        installed = _read_install(verify=True)
        if installed:
            probe_lemex(Path(installed["executable"]))
            if not refresh:
                return installed
        executable = shutil.which("lemex")
        if executable:
            try:
                package = _package_root(Path(executable))
                # A managed executable on PATH is already a pin, not evidence of
                # a newly installed external version to adopt during refresh.
                if not refresh or not package.is_relative_to((_root() / "objects").resolve()):
                    entrypoint = Path(executable).resolve().relative_to(package)
                    return _publish(package, entrypoint, source="adopted-content-pin", commit=None)
            except (LemexInstallError, OSError, subprocess.SubprocessError):
                if installed is not None:
                    return installed
                if not build:
                    raise
        if installed is not None:
            return installed
        if not build:
            raise LemexInstallError("No compatible Lemex installation found")
        return _build_from_source()


def main() -> int:
    """Install or inspect Lemex without invoking any model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Use existing installations only; keep a healthy pin if refresh has no replacement",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Adopt a compatible local upgrade; build pinned source only when no healthy runtime exists",
    )
    args = parser.parse_args()
    try:
        result = ensure_lemex(build=not args.no_build, refresh=args.refresh)
    except (LemexInstallError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Lemex installation failed: {exc}\n")
    print(f"Lemex ready: {result['executable']} ({result['source']}; {result['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
