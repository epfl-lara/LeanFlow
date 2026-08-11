"""Build credential-safe Python interpreter and provider-package identities."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

_PRIMARY_PROVIDER_PACKAGES = ("openai", "anthropic")
_PYTHON_PATH_ENV_KEYS = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONEXECUTABLE",
    }
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _content_identity(value: str) -> str:
    """Return a printable digest identity without retaining the source text."""
    raw = str(value or "")
    digest = _sha256_bytes(raw.encode("utf-8", errors="surrogatepass"))
    return f"[content-sha256:{digest};chars:{len(raw)}]"


def _stable_path(value: str, *, project_root: Path | None = None) -> tuple[Path, str, str]:
    """Resolve a path and replace volatile project, home, and temp roots."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (project_root or Path.cwd()) / path
    resolved = path.resolve(strict=False)
    roots: list[tuple[str, Path]] = []
    if project_root is not None:
        roots.append(("project-relative", project_root.expanduser().resolve()))
    try:
        roots.append(("home-relative", Path.home().resolve()))
    except (OSError, RuntimeError):
        pass
    try:
        roots.append(("temp-relative", Path(tempfile.gettempdir()).resolve()))
    except (OSError, RuntimeError):
        pass
    for scope, root in roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        marker = f"{scope}/{relative.as_posix()}"
        return resolved, scope, marker
    return resolved, "digest-only", os.path.normcase(str(resolved))


def _path_identity(value: str, *, project_root: Path | None = None) -> dict[str, Any]:
    """Return a path identity without serializing an absolute machine path."""
    raw = str(value or "")
    if not raw:
        return {"scope": "empty", "path_sha256": _sha256_bytes(b""), "status": "empty"}
    try:
        resolved, scope, stable_path = _stable_path(raw, project_root=project_root)
        status = "directory" if resolved.is_dir() else "file" if resolved.is_file() else "missing"
    except (OSError, RuntimeError, ValueError):
        return {
            "scope": "digest-only",
            "path_sha256": _sha256_bytes(raw.encode("utf-8", errors="surrogatepass")),
            "status": "invalid",
        }
    identity: dict[str, Any] = {
        "scope": scope,
        "path_sha256": _sha256_bytes(stable_path.encode("utf-8", errors="surrogatepass")),
        "status": status,
    }
    if scope == "project-relative":
        relative = stable_path.removeprefix("project-relative/")
        identity["project_relative"] = "." if relative in {"", "."} else _content_identity(relative)
    return identity


def _normalize_volatile_text(value: str, *, project_root: Path | None = None) -> str:
    """Replace absolute volatile roots before hashing command-like controls."""
    normalized = str(value or "")
    replacements: list[tuple[str, str]] = []
    if project_root is not None:
        try:
            replacements.append((str(project_root.expanduser().resolve()), "$PROJECT_ROOT"))
        except (OSError, RuntimeError):
            pass
    for marker, candidate in (("$HOME", Path.home()), ("$TMP", Path(tempfile.gettempdir()))):
        try:
            replacements.append((str(candidate.resolve()), marker))
        except (OSError, RuntimeError):
            continue
    for prefix, marker in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if prefix:
            normalized = normalized.replace(prefix, marker)
    return normalized


def _path_list_identity(value: str, *, project_root: Path | None = None) -> str:
    """Hash a path-list after replacing volatile roots with stable markers."""
    records = [
        _path_identity(item or os.getcwd(), project_root=project_root)
        for item in str(value or "").split(os.pathsep)
    ]
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return _content_identity(canonical)


def _executable_identity(
    value: str, *, project_root: Path | None = None
) -> tuple[dict[str, Any], bool]:
    """Hash the resolved interpreter executable while suppressing its path."""
    identity = _path_identity(value, project_root=project_root)
    if not value:
        return identity, False
    try:
        raw = Path(value).expanduser().resolve(strict=True).read_bytes()
    except (OSError, RuntimeError, ValueError):
        identity["content_status"] = "unreadable"
        return identity, False
    identity.update(
        {"content_status": "complete", "content_sha256": _sha256_bytes(raw), "bytes": len(raw)}
    )
    return identity, True


def _is_import_file(path: Path) -> bool:
    """Return whether one direct sys.path file can affect Python imports."""
    return path.suffix in {".py", ".pyi", ".pth"} or path.name.endswith(
        tuple(importlib.machinery.EXTENSION_SUFFIXES)
    )


def _sys_path_entry(
    path_value: str, *, project_root: Path | None = None
) -> tuple[dict[str, Any], bool]:
    """Hash one import root and only its direct import-routing entries."""
    effective = path_value or os.getcwd()
    identity = _path_identity(effective, project_root=project_root)
    try:
        path = Path(effective).expanduser().resolve(strict=False)
        if not path.exists():
            identity.update({"content_sha256": _sha256_bytes(b"missing"), "import_entry_count": 0})
            return identity, True
        if path.is_file():
            raw = path.read_bytes()
            identity.update({"content_sha256": _sha256_bytes(raw), "import_entry_count": 1})
            return identity, True
        if not path.is_dir():
            raise OSError("import path is not readable as a file or directory")
        records: list[dict[str, Any]] = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            initializer = child / "__init__.py"
            import_file = (child.is_file() or child.is_symlink()) and _is_import_file(child)
            import_package = child.is_dir() and initializer.is_file()
            if not import_file and not import_package:
                continue
            record: dict[str, Any] = {
                "name_sha256": _sha256_bytes(child.name.encode("utf-8", errors="surrogatepass"))
            }
            if import_file:
                raw = child.read_bytes()
                record.update({"kind": "file", "sha256": _sha256_bytes(raw), "bytes": len(raw)})
            else:
                raw = initializer.read_bytes()
                record.update(
                    {
                        "kind": "package",
                        "initializer_sha256": _sha256_bytes(raw),
                        "initializer_bytes": len(raw),
                    }
                )
            records.append(record)
        encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        identity.update(
            {"content_sha256": _sha256_bytes(encoded), "import_entry_count": len(records)}
        )
        return identity, True
    except (OSError, RuntimeError, ValueError):
        identity.update({"content_sha256": _sha256_bytes(b"unreadable"), "import_entry_count": 0})
        return identity, False


def _sys_path_identity(
    project_root: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bind import-root order and safe content identities without publishing paths."""
    records: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, value in enumerate(sys.path):
        record, complete = _sys_path_entry(str(value or ""), project_root=project_root)
        records.append(record)
        if not complete:
            issues.append(f"sys-path-{index}-unreadable")
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "sha256": _sha256_bytes(encoded),
        "entry_count": len(records),
        "content_complete": not issues,
    }, issues


def _provider_package_identity(
    package: str,
    *,
    project_root: Path | None,
    content_records: Callable[..., tuple[list[dict[str, Any]], list[str]]],
) -> tuple[dict[str, Any], list[str]]:
    """Hash every readable file in one resolved primary-provider package."""
    issues: list[str] = []
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, AttributeError, ValueError):
        spec = None
    if spec is None:
        return {
            "status": "missing",
            "resolution_scope": "unresolved",
            "sha256": _sha256_bytes(b"missing"),
            "file_count": 0,
            "location_count": 0,
        }, [f"primary-provider-{package}-missing"]
    targets: list[tuple[str, str]] = [
        ("package", str(location)) for location in (spec.submodule_search_locations or ())
    ]
    if not targets and spec.origin and spec.origin not in {"built-in", "frozen"}:
        targets = [("single-module", str(spec.origin))]
    if not targets:
        return {
            "status": "incomplete",
            "resolution_scope": "unresolved",
            "sha256": _sha256_bytes(b"unhashable"),
            "file_count": 0,
            "location_count": 0,
        }, [f"primary-provider-{package}-unhashable"]
    locations: list[dict[str, Any]] = []
    file_count = 0
    for index, (scope, raw_target) in enumerate(targets):
        try:
            target = Path(raw_target).expanduser().resolve(strict=True)
            if scope == "single-module":
                if not target.is_file():
                    raise OSError("provider module origin is not a file")
                root = target.parent
                candidates = [target]
            else:
                if not target.is_dir():
                    raise OSError("provider location is not a directory")
                root = target
                candidates = [
                    path
                    for path in root.rglob("*")
                    if (path.is_file() or path.is_symlink())
                    and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pyo"}
                ]
        except (OSError, RuntimeError, ValueError):
            issues.append(f"primary-provider-{package}-location-{index}-unreadable")
            continue
        records, record_issues = content_records(root, candidates)
        if record_issues or not records:
            issues.extend(
                f"primary-provider-{package}-location-{index}-{issue}"
                for issue in (record_issues or ["empty"])
            )
            continue
        file_count += len(records)
        locations.append(
            {
                "index": index,
                "scope": scope,
                "root_sha256": _path_identity(str(root), project_root=project_root)["path_sha256"],
                "files_sha256": _sha256_bytes(
                    json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ),
                "file_count": len(records),
            }
        )
    encoded = json.dumps(locations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    resolution_scope = (
        targets[0][0]
        if len(targets) == 1
        else (
            "multi-location-package"
            if targets and all(scope == "package" for scope, _target in targets)
            else "mixed"
        )
    )
    return {
        "status": "complete" if not issues and locations else "incomplete",
        "resolution_scope": resolution_scope,
        "sha256": _sha256_bytes(encoded),
        "file_count": file_count,
        "location_count": len(locations),
    }, issues


def _python_flags_identity() -> dict[str, Any]:
    """Return every scalar flag exposed by the active interpreter."""
    flags: dict[str, Any] = {}
    for name in dir(sys.flags):
        if name.startswith("_"):
            continue
        try:
            value = getattr(sys.flags, name)
        except AttributeError:
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            flags[name] = value
    return flags


def _python_environment_identity(project_root: Path | None = None) -> dict[str, str]:
    """Hash every configured Python control after volatile-root normalization."""
    identity: dict[str, str] = {}
    for key, value in sorted(os.environ.items()):
        if not key.upper().startswith("PYTHON"):
            continue
        if key.upper() == "PYTHONPATH":
            identity[key] = _path_list_identity(value, project_root=project_root)
        elif key.upper() in _PYTHON_PATH_ENV_KEYS:
            canonical = json.dumps(
                _path_identity(value, project_root=project_root),
                sort_keys=True,
                separators=(",", ":"),
            )
            identity[key] = _content_identity(canonical)
        else:
            identity[key] = _content_identity(
                _normalize_volatile_text(value, project_root=project_root)
            )
    return identity


def collect_python_runtime_identity(
    *,
    distribution_provider: Callable[[], Iterable[Any]],
    content_records: Callable[..., tuple[list[dict[str, Any]], list[str]]],
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Bind interpreter state, import routing, packages, and distribution versions."""
    implementation_version: Sequence[Any] = sys.implementation.version
    executable, executable_complete = _executable_identity(
        sys.executable, project_root=project_root
    )
    base_executable, base_executable_complete = _executable_identity(
        str(getattr(sys, "_base_executable", "") or ""), project_root=project_root
    )
    sys_path, sys_path_issues = _sys_path_identity(project_root)
    runtime: dict[str, Any] = {
        "implementation": sys.implementation.name,
        "implementation_version": ".".join(str(part) for part in implementation_version[:3]),
        "cache_tag": str(sys.implementation.cache_tag or ""),
        "python_version": platform.python_version(),
        "python_full_version": sys.version,
        "compiler": platform.python_compiler(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "flags": _python_flags_identity(),
        "executable": executable,
        "base_executable": base_executable,
        "prefix": _path_identity(sys.prefix, project_root=project_root),
        "base_prefix": _path_identity(sys.base_prefix, project_root=project_root),
        "exec_prefix": _path_identity(sys.exec_prefix, project_root=project_root),
        "base_exec_prefix": _path_identity(sys.base_exec_prefix, project_root=project_root),
        "sys_path": sys_path,
        "python_environment": _python_environment_identity(project_root),
    }
    issues = list(sys_path_issues)
    if not executable_complete:
        issues.append("python-executable-unreadable")
    if not base_executable_complete:
        issues.append("python-base-executable-unreadable")
    distributions: dict[str, str] = {}
    try:
        installed = list(distribution_provider())
    except Exception:
        installed = []
        issues.append("distribution-inventory-unavailable")
    for index, distribution in enumerate(installed):
        try:
            name = str(distribution.metadata["Name"] or "").strip().casefold()
            version = str(distribution.version or "").strip()
        except Exception:
            issues.append(f"distribution-{index}-unreadable")
            continue
        if not name or not version:
            issues.append(f"distribution-{index}-identity-missing")
            continue
        previous = distributions.get(name)
        if previous is not None and previous != version:
            issues.append(f"distribution-{index}-duplicate-version")
            continue
        distributions[name] = version
    providers: dict[str, dict[str, Any]] = {}
    for package in _PRIMARY_PROVIDER_PACKAGES:
        provider, provider_issues = _provider_package_identity(
            package, project_root=project_root, content_records=content_records
        )
        providers[package] = provider
        issues.extend(provider_issues)
    runtime["primary_provider_packages"] = providers
    inventory = dict(sorted(distributions.items()))
    identity = {"python_runtime": runtime, "installed_distributions": inventory}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        **identity,
        "python_runtime_sha256": _sha256_bytes(encoded),
        "python_runtime_complete": not issues and bool(inventory),
        "python_runtime_issues": issues,
    }
