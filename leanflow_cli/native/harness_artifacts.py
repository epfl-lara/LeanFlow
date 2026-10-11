"""Freeze, independently verify and promote native workflow source proposals."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.workflows.prover.lean_environment import lean_environment_generation
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.source import sorry_spans
from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles, module_name

_SKIP = {
    ".lake",
    ".git",
    ".leanflow",
    ".leanflow-runtime",
    ".leanflow-job",
    ".venv",
    "node_modules",
}
_CONFIG = {"lakefile.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json"}


def source_files(root: Path) -> dict[str, bytes]:
    """Capture project source/config bytes without following source aliases."""
    files: dict[str, bytes] = {}
    for directory, names, entries in os.walk(root, followlinks=False):
        parent = Path(directory)
        names[:] = [
            name for name in names if name not in _SKIP and not (parent / name).is_symlink()
        ]
        for name in entries:
            path = parent / name
            if not (path.suffix == ".lean" or path.name in _CONFIG or path.name == "Blueprint.md"):
                continue
            if path.is_symlink():
                raise ValueError(f"Source snapshots cannot contain file symlinks: {path}")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def declaration_names(source: str) -> list[str]:
    """List LeanProbe's parsed declaration names for independent kernel inspection."""
    from leanflow_cli.lean.lean_incremental import _segment_file

    _, segments = _segment_file(source)
    return list(dict.fromkeys(str(segment.name) for segment in segments if segment.name))


def _check_active(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    """Stop verification and publication when the owning campaign no longer permits work."""
    remaining = remaining_seconds(deadline)
    if operation_cancelled() or (cancelled is not None and cancelled()):
        raise InterruptedError("The campaign was cancelled before source acceptance")
    if remaining is not None and remaining <= 0:
        raise InterruptedError("The global campaign deadline expired before source acceptance")


def _protected_definitions(source: str) -> dict[str, str]:
    """Retain non-proof declaration bytes so type equality cannot hide changed definitions."""
    from leanflow_cli.lean.lean_incremental import _segment_file

    _, segments = _segment_file(source)
    return {
        str(segment.name): str(segment.text).strip()
        for segment in segments
        if segment.name and segment.kind not in {"theorem", "lemma", "example"}
    }


def verify_proposal(
    *,
    root: Path,
    snapshot: Path,
    baseline: dict[str, bytes],
    directory: Path,
    workflow: str,
    active_file: str,
    allowed_axioms: tuple[str, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Verify frozen bytes in a second workspace the native agent cannot mutate."""
    from leanflow_cli.workflows.prover.check_process import isolated_command

    _check_active(deadline, cancelled)
    proposed = source_files(snapshot)
    if any(
        proposed.get(path) != baseline.get(path)
        for path in baseline.keys() | proposed.keys()
        if Path(path).name in _CONFIG
    ):
        return {
            "accepted": False,
            "error": "Native proposals cannot change the toolchain or dependency configuration",
        }
    if baseline.keys() - proposed.keys():
        return {"accepted": False, "error": "Native proposals cannot delete original source files"}
    changed = {path: data for path, data in proposed.items() if data != baseline.get(path)}
    if workflow == "review":
        return {
            "accepted": not changed,
            "error": "Review is read-only" if changed else "",
            "files": {},
        }
    if not changed:
        return {"accepted": False, "error": "No source changes were submitted"}
    statement_draft = workflow in {"draft", "formalize"}
    if any(
        Path(path).suffix != ".lean" and not (statement_draft and Path(path).name == "Blueprint.md")
        for path in changed
    ):
        return {"accepted": False, "error": "Only Lean source changes can be promoted"}
    if active_file and any(
        path in baseline and path != active_file and Path(path).name != "Blueprint.md"
        for path in changed
    ):
        return {
            "accepted": False,
            "error": "An original source outside the assigned file was changed",
        }
    lean_changes = {path: data for path, data in changed.items() if Path(path).suffix == ".lean"}
    if not lean_changes:
        return {"accepted": False, "error": "No Lean source changes were submitted"}
    if any(sorry_spans(data.decode()) for data in lean_changes.values()) and not statement_draft:
        return {"accepted": False, "error": "Submitted Lean sources still contain sorry/admit"}
    for relative, content in lean_changes.items():
        definitions = _protected_definitions(content.decode())
        if statement_draft and any(sorry_spans(value) for value in definitions.values()):
            return {"accepted": False, "error": f"Construction placeholders remain in {relative}"}
        if relative in baseline:
            protected = _protected_definitions(baseline[relative].decode())
            if any(definitions.get(name) != text for name, text in protected.items()):
                return {
                    "accepted": False,
                    "error": f"An original non-proof definition changed in {relative}",
                }
    directory.mkdir(parents=True, exist_ok=True)
    environment_generation = lean_environment_generation(root)
    # A fresh UUID directory avoids sharing accepted bytes with ongoing agent edits.
    with tempfile.TemporaryDirectory(prefix="proposal-", dir=directory) as temporary:
        # Trust dependency artifacts only from the canonical project. Native tools
        # may rewrite every byte in their workspace, including .lake caches.
        frozen = create_snapshot(root, Path(temporary) / "project")
        if source_files(frozen) != baseline or source_files(root) != baseline:
            return {"accepted": False, "error": "Canonical source changed during verification"}
        for relative, content in changed.items():
            target = frozen / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if source_files(frozen) != proposed:
            return {
                "accepted": False,
                "error": "Source changed while the submission was being frozen",
            }
        build = frozen / ".lake" / "build"
        build.mkdir(parents=True, exist_ok=True)
        lake = frozen / ".lake"
        lake.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="leanflow-verify-") as check_tmp:
            checked = isolated_command(
                project_root=frozen,
                workspace=Path(check_tmp),
                argv=["lake", "build"],
                timeout_s=remaining_seconds(deadline),
                cancelled=cancelled,
                extra_writable_roots=(
                    build,
                    *(
                        lake / name
                        for name in (
                            "lakefile.olean",
                            "lakefile.olean.trace",
                            "lakefile.olean.lock",
                        )
                        if (lake / name).exists()
                    ),
                ),
            )
            if not checked.get("success"):
                return {
                    "accepted": False,
                    "error": "Independent project build failed",
                    "build": checked,
                }
            profiles: dict[str, Any] = {}
            for relative, content in lean_changes.items():
                names = declaration_names(content.decode())
                if not names:
                    return {
                        "accepted": False,
                        "error": f"No inspectable declarations in {relative}",
                    }
                result = compiled_type_profiles(
                    root=frozen,
                    source=content.decode(),
                    names=names,
                    workspace=Path(check_tmp),
                    timeout_s=remaining_seconds(deadline),
                    module=module_name(relative),
                    cancelled=cancelled,
                )
                if not result.get("accepted"):
                    return {
                        "accepted": False,
                        "error": f"Kernel inspection failed for {relative}",
                        "details": result,
                    }
                permitted = set(allowed_axioms) | ({"sorryAx"} if statement_draft else set())
                if any(
                    set(profile["axioms"]) - permitted for profile in result["profiles"].values()
                ):
                    return {"accepted": False, "error": f"Unapproved axioms in {relative}"}
                if relative in baseline:
                    original = baseline[relative].decode()
                    original_names = declaration_names(original)
                    if set(original_names) - set(names):
                        return {
                            "accepted": False,
                            "error": f"An original declaration was removed in {relative}",
                        }
                    before = compiled_type_profiles(
                        root=root,
                        source=original,
                        names=original_names,
                        workspace=Path(check_tmp),
                        timeout_s=remaining_seconds(deadline),
                        module=module_name(relative),
                        cancelled=cancelled,
                    )
                    if not before.get("accepted") or any(
                        before["profiles"][name]["sha256"] != result["profiles"][name]["sha256"]
                        for name in original_names
                    ):
                        return {
                            "accepted": False,
                            "error": f"Original kernel declaration types changed or could not be verified in {relative}",
                        }
                profiles[relative] = result["profiles"]
        _check_active(deadline, cancelled)
        if lean_environment_generation(root) != environment_generation:
            return {
                "accepted": False,
                "error": "Canonical Lean dependencies changed during verification",
            }
        return {
            "accepted": True,
            "environment_generation": environment_generation,
            "files": {path: data.decode() for path, data in changed.items()},
            "sha256": {path: hashlib.sha256(data).hexdigest() for path, data in changed.items()},
            "profiles": profiles,
        }


def promote(
    root: Path,
    baseline: dict[str, bytes],
    receipt: dict[str, Any],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Publish only accepted frozen bytes after rechecking canonical source identity."""
    if receipt.get("accepted") is not True:
        raise ValueError("A successful independent receipt is required")
    lock = root / ".leanflow" / "native-promotion.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _check_active(deadline, cancelled)
        if source_files(root) != baseline:
            raise ValueError(
                "Canonical sources changed during the native workflow; proposal retained for review"
            )
        if receipt.get("environment_generation") != lean_environment_generation(root):
            raise ValueError(
                "Canonical Lean dependencies changed or the receipt lacks freshness evidence"
            )
        written: dict[str, bytes] = {}
        try:
            for relative, content in receipt.get("files", {}).items():
                _check_active(deadline, cancelled)
                path = root / relative
                if not path.resolve().is_relative_to(root.resolve()) or any(
                    part.startswith(".") for part in Path(relative).parts
                ):
                    raise ValueError("Proposal path escapes the publishable project source")
                expected = baseline.get(relative)
                current = path.read_bytes() if path.exists() else None
                if current != expected:
                    raise ValueError(f"Canonical source changed before publication: {relative}")
                data = content.encode()
                if hashlib.sha256(data).hexdigest() != receipt["sha256"][relative]:
                    raise ValueError("Accepted source digest does not match the receipt")
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    output.write(data)
                    staged = Path(output.name)
                os.replace(staged, path)
                written[relative] = data
            _check_active(deadline, cancelled)
        except BaseException:
            for relative in reversed(written):
                target = root / relative
                # A user edit after our write belongs to the user, even during rollback.
                if not target.is_file() or target.read_bytes() != written[relative]:
                    continue
                if relative in baseline:
                    (root / relative).write_bytes(baseline[relative])
                else:
                    (root / relative).unlink(missing_ok=True)
            raise
        return list(written)
