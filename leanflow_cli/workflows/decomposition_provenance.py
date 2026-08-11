"""Persist exact source ownership for campaign-created helper decompositions.

The decomposer records a pending provenance transaction before it inserts any
helper declarations.  The record keeps the original parent declaration and
exact helper signatures, allowing a later false-helper cleanup to remove only
campaign-owned source and reopen the parent without consulting Git.  Startup
also supports a fail-closed migration for older campaigns whose durable
activity and verified-patch checkpoints predate this ledger.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from core.process_identity import (
    current_process_identity,
    process_identity_details,
)
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    declaration_statement_text,
)
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

logger = logging.getLogger(__name__)

try:  # POSIX advisory locking; same fallback policy as workflow state writes.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_PROVENANCE_CAP = 100
_MAX_LEGACY_ACTIVITY_RECORD_BYTES = 8 * 1024 * 1024
_MAX_LEGACY_HOT_ACTIVITY_FILES = 4_096
_MAX_LEGACY_DECOMPOSER_MATCHES = 1_024
_MAX_LEGACY_EVENT_ID_CHARS = 512
_MAX_LEGACY_TIMESTAMP_CHARS = 128
_MAX_LEGACY_PARENT_CHARS = 1_024
_DECLARATION_TRAILER_RE = re.compile(r"\n\n(?=(?:end\b|namespace\b|section\b|/--|@\[[^\n]*\]))")
_SOURCE_LOCKS_GUARD = threading.Lock()
_SOURCE_THREAD_LOCKS: dict[str, threading.RLock] = {}
_SOURCE_LOCK_LOCAL = threading.local()
_ACTIVE_TRANSACTION_LOCK = threading.Lock()
_ACTIVE_TRANSACTIONS: set[str] = set()
_CANONICAL_INCREMENTAL_FALLBACK_TIMEOUT_S = 300


def _incremental_environment_failure(check: Mapping[str, Any] | None) -> bool:
    """Return whether an incremental check failed before reaching its target."""
    if not check:
        return False
    error_code = str(check.get("error_code", "") or "").strip().lower()
    detail = " ".join(
        str(check.get(key, "") or "") for key in ("error", "output", "message", "hint")
    ).lower()
    return bool(
        error_code
        in {
            "header_failed",
            "prior_decl_failed",
            "prior_declaration_failed",
        }
        or "failed to build env before target" in detail
    )


def canonical_source_fallback_for_incremental_failure(
    check: Mapping[str, Any] | None,
    *,
    source: str,
    cwd: str,
) -> dict[str, Any]:
    """Use exact-project full-source validation after a prefix-build failure.

    LeanProbe may segment valid syntax incorrectly while rebuilding the
    environment preceding a target. Preserve real incremental diagnostics,
    but replace that infrastructure result with one canonical system-temporary
    ``lake env lean`` check of the exact candidate source.
    """
    incremental = dict(check or {})
    if not _incremental_environment_failure(incremental):
        return incremental
    from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check

    canonical = dict(
        lean_ephemeral_source_check(
            source,
            cwd=cwd or ".",
            timeout_s=_CANONICAL_INCREMENTAL_FALLBACK_TIMEOUT_S,
        )
        or {}
    )
    canonical_ok = canonical.get("success") is True and canonical.get("ok") is True
    incremental_detail = str(
        incremental.get("error", "")
        or incremental.get("output", "")
        or incremental.get("message", "")
        or ""
    )
    return {
        **canonical,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_source",
        "has_errors": not canonical_ok,
        "canonical_fallback": True,
        "incremental_fallback_error_code": str(incremental.get("error_code", "") or ""),
        "incremental_fallback_reason": incremental_detail[:1000],
    }


@dataclass(frozen=True)
class DeclarationSlice:
    """Describe one exact declaration and its stable statement identity."""

    name: str
    kind: str
    start: int
    end: int
    metadata_start: int
    text: str
    signature: str
    signature_sha256: str
    declaration_sha256: str


@dataclass(frozen=True)
class SourceOperation:
    """Pin one canonical source identity while its cross-process lease is held."""

    path: Path
    lock_keys: tuple[str, ...]
    parent_fd: int = field(compare=False, repr=False)
    file_name: str
    directory_identities: tuple[tuple[int, int], ...]
    attempts: set[str] = field(default_factory=set, compare=False, repr=False)


@dataclass(frozen=True)
class _SourceSnapshot:
    """Describe the exact regular-file inode read for a source comparison."""

    device: int
    inode: int
    size: int
    modified_ns: int


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    """Return the digest of exact on-disk source bytes."""
    return hashlib.sha256(content).hexdigest()


def _canonical_signature_text(signature: str) -> str:
    """Return the newline-stable declaration signature used by negation evidence."""
    return str(signature or "").replace("\r\n", "\n").replace("\r", "\n")


def signature_sha256(signature: str) -> str:
    """Return the LF-canonical signature digest shared with negation promotion."""
    return _sha256_text(_canonical_signature_text(signature))


def full_declaration_signature_sha256(declaration_text: str) -> str:
    """Return the full statement digest used by authoritative negation evidence.

    Historical decomposition records split a declaration at its first ``:=``.
    Keep that durable identity stable, but reconstruct the full statement here
    when a result type contains top-level dependent ``let`` assignments.
    """
    signature = declaration_statement_text(str(declaration_text or ""))
    return signature_sha256(signature) if signature else ""


def _source_lock_root() -> Path:
    """Return the private global lock root without creating source-tree artifacts."""
    return leanflow_home() / "workflow-state" / "source-locks"


def _thread_source_lock(key: str) -> threading.RLock:
    """Return the process-local lock paired with one cross-process source lease."""
    with _SOURCE_LOCKS_GUARD:
        return _SOURCE_THREAD_LOCKS.setdefault(key, threading.RLock())


def _source_lock_entries() -> dict[str, tuple[int, Any]]:
    """Return re-entrant source-lock entries for the current thread."""
    entries = getattr(_SOURCE_LOCK_LOCAL, "entries", None)
    if not isinstance(entries, dict):
        entries = {}
        _SOURCE_LOCK_LOCAL.entries = entries
    return entries


def _open_source_lock(lock_path: Path) -> Any:
    """Open one private regular lock file without following a final symlink."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(fd, "a+b", closefd=True)
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise OSError(f"source lock is not a regular file: {lock_path}")
    return handle


@contextmanager
def _source_write_lock(key: str) -> Iterator[None]:
    """Acquire one stable path-or-inode source lease across LeanFlow processes."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    lock_path = _source_lock_root() / f"{digest}.lock"
    thread_lock = _thread_source_lock(key)
    with thread_lock:
        entries = _source_lock_entries()
        existing = entries.get(key)
        if existing is not None:
            depth, handle = existing
            entries[key] = (depth + 1, handle)
            try:
                yield
            finally:
                current_depth, current_handle = entries[key]
                if current_depth <= 1:
                    entries.pop(key, None)
                else:
                    entries[key] = (current_depth - 1, current_handle)
            return
        handle = _open_source_lock(lock_path)
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            entries[key] = (1, handle)
            yield
        finally:
            entries.pop(key, None)
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


@contextmanager
def _source_write_locks(keys: Sequence[str]) -> Iterator[None]:
    """Acquire path and inode leases in a stable order to cover hardlink aliases."""
    with contextlib.ExitStack() as stack:
        for key in sorted(set(keys)):
            stack.enter_context(_source_write_lock(key))
        yield


def _canonical_path_parts(path: Path) -> tuple[str, ...]:
    """Return normalized absolute path parts without resolving durable identity."""
    if not path.is_absolute() or not path.name:
        raise OSError(f"source identity is not an absolute file path: {path}")
    normalized = Path(os.path.normpath(str(path)))
    if normalized != path or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise OSError(f"source identity is not canonically normalized: {path}")
    return path.parts


def _open_source_parent(path: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Open and identify every ancestor without following component symlinks."""
    parts = _canonical_path_parts(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path.anchor, flags)
    identities: list[tuple[int, int]] = []
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(path.anchor)
        identities.append((int(metadata.st_dev), int(metadata.st_ino)))
        for component in parts[1:-1]:
            child_flags = flags
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            child = os.open(component, child_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise NotADirectoryError(component)
            identities.append((int(metadata.st_dev), int(metadata.st_ino)))
        return descriptor, tuple(identities)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def _source_entry_snapshot(parent_fd: int, file_name: str) -> _SourceSnapshot:
    """Return one no-follow regular source identity relative to a pinned parent."""
    metadata = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"source target is not a regular file: {file_name}")
    return _SourceSnapshot(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
    )


def _operation_path_is_pinned(operation: SourceOperation) -> bool:
    """Return whether the durable path still reaches the pinned parent and file."""
    try:
        current_parent, identities = _open_source_parent(operation.path)
    except OSError:
        return False
    try:
        if identities != operation.directory_identities:
            return False
        pinned_parent = os.fstat(operation.parent_fd)
        current_parent_stat = os.fstat(current_parent)
        if (pinned_parent.st_dev, pinned_parent.st_ino) != (
            current_parent_stat.st_dev,
            current_parent_stat.st_ino,
        ):
            return False
        pinned_source = _source_entry_snapshot(operation.parent_fd, operation.file_name)
        visible_source = _source_entry_snapshot(current_parent, operation.file_name)
        return (pinned_source.device, pinned_source.inode) == (
            visible_source.device,
            visible_source.inode,
        )
    except OSError:
        return False
    finally:
        os.close(current_parent)


@contextmanager
def source_operation(path: Path, *, canonical: bool = False) -> Iterator[SourceOperation]:
    """Pin and lease one source path for a complete mutation lifecycle.

    Ordinary callers may pass an alias, which is resolved exactly once.
    Durable provenance callers pass ``canonical=True`` so a later symlink at
    that stored identity is rejected instead of followed to another target.
    """
    candidate = path if canonical else path.expanduser()
    if canonical:
        resolved = candidate
        _canonical_path_parts(resolved)
    else:
        resolved = candidate.resolve(strict=True)
    parent_fd, directory_identities = _open_source_parent(resolved)
    try:
        initial = _source_entry_snapshot(parent_fd, resolved.name)
    except BaseException:
        os.close(parent_fd)
        raise
    lock_keys = (
        f"path:{resolved}",
        f"inode:{initial.device}:{initial.inode}",
    )
    operation = SourceOperation(
        path=resolved,
        lock_keys=lock_keys,
        parent_fd=parent_fd,
        file_name=resolved.name,
        directory_identities=directory_identities,
    )
    try:
        with _source_write_locks(lock_keys):
            if not _operation_path_is_pinned(operation):
                raise OSError(f"source path ancestry changed before its lease: {resolved}")
            leased = _source_entry_snapshot(parent_fd, resolved.name)
            if (leased.device, leased.inode) != (initial.device, initial.inode):
                raise OSError(f"source identity changed before its lease was acquired: {resolved}")
            yield operation
    finally:
        os.close(parent_fd)
        with _ACTIVE_TRANSACTION_LOCK:
            _ACTIVE_TRANSACTIONS.difference_update(operation.attempts)


def _read_regular_source(operation: SourceOperation) -> tuple[bytes, _SourceSnapshot]:
    """Read exact bytes from one regular file in the pinned parent directory."""
    if not _operation_path_is_pinned(operation):
        raise OSError(f"source path ancestry no longer matches its lease: {operation.path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(operation.file_name, flags, dir_fd=operation.parent_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"source target is not a regular file: {operation.path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
    finally:
        os.close(fd)
    current = _source_entry_snapshot(operation.parent_fd, operation.file_name)
    if (current.device, current.inode) != (metadata.st_dev, metadata.st_ino):
        raise OSError(f"source identity changed while being read: {operation.path}")
    if not _operation_path_is_pinned(operation):
        raise OSError(f"source path ancestry changed while being read: {operation.path}")
    return content, _SourceSnapshot(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
    )


def read_source_bytes(operation: SourceOperation) -> bytes:
    """Read exact bytes through a currently held canonical source operation."""
    content, _snapshot = _read_regular_source(operation)
    return content


def _source_cas_hook(stage: str) -> None:
    """Expose deterministic external-write boundaries for transaction tests."""


def _atomic_write_source_bytes(
    operation: SourceOperation,
    content: bytes,
    *,
    expected_bytes: bytes | None = None,
    expected_snapshot: _SourceSnapshot | None = None,
) -> bool:
    """Replace exact bytes in a pinned parent after final path revalidation."""
    path = operation.path
    current_mode = _source_entry_snapshot(operation.parent_fd, operation.file_name)
    temporary = f".{path.stem}_{uuid.uuid4().hex}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600, dir_fd=operation.parent_fd)
    try:
        with contextlib.suppress(OSError):
            visible = os.stat(
                operation.file_name, dir_fd=operation.parent_fd, follow_symlinks=False
            )
            os.fchmod(fd, stat.S_IMODE(visible.st_mode))
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _source_cas_hook("before-final-revalidation")
        if expected_bytes is not None and expected_snapshot is not None:
            latest, latest_snapshot = _read_regular_source(operation)
            if latest != expected_bytes or latest_snapshot != expected_snapshot:
                os.unlink(temporary, dir_fd=operation.parent_fd)
                return False
        if not _operation_path_is_pinned(operation):
            os.unlink(temporary, dir_fd=operation.parent_fd)
            return False
        # Keep the linter-visible read: a final symlink swap cannot be replaced
        # merely because its inode differs from the initially read source.
        latest_entry = _source_entry_snapshot(operation.parent_fd, operation.file_name)
        if (latest_entry.device, latest_entry.inode) != (
            current_mode.device,
            current_mode.inode,
        ):
            os.unlink(temporary, dir_fd=operation.parent_fd)
            return False
        os.replace(
            temporary,
            operation.file_name,
            src_dir_fd=operation.parent_fd,
            dst_dir_fd=operation.parent_fd,
        )
        with contextlib.suppress(OSError):
            os.fsync(operation.parent_fd)
        persisted, _persisted_snapshot = _read_regular_source(operation)
        if persisted != content:
            raise OSError(f"source changed immediately after atomic replacement: {path}")
        return True
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=operation.parent_fd)
        raise


def compare_and_swap_source(
    path: Path,
    *,
    expected_bytes: bytes,
    replacement_bytes: bytes,
    operation: SourceOperation | None = None,
) -> bool:
    """Replace source bytes when the leased canonical file still matches.

    The private stable lease closes races between LeanFlow writers. Exact
    bytes plus inode metadata are revalidated immediately before replacement,
    which detects observed editor changes, but POSIX advisory locking cannot
    make arbitrary non-cooperating writers participate in an atomic CAS. A
    mismatch returns ``False`` without writing; ambiguity and I/O failures are
    loud so the campaign can pause rather than claim all-or-nothing success.
    """
    if operation is None:
        with source_operation(path) as owned:
            return compare_and_swap_source(
                owned.path,
                expected_bytes=expected_bytes,
                replacement_bytes=replacement_bytes,
                operation=owned,
            )
    current, snapshot = _read_regular_source(operation)
    if current != expected_bytes:
        return False
    if replacement_bytes == current:
        return True
    _source_cas_hook("after-initial-comparison")
    return _atomic_write_source_bytes(
        operation,
        replacement_bytes,
        expected_bytes=expected_bytes,
        expected_snapshot=snapshot,
    )


def atomic_write_source(path: Path, text: str) -> None:
    """Replace one UTF-8 source file atomically under the shared source lock."""
    with source_operation(path) as operation:
        _atomic_write_source_bytes(operation, text.encode("utf-8"))


def canonical_file(file_label: str, project_root: Path) -> str:
    """Return one absolute real-path identity for a source file."""
    path = Path(str(file_label or "").strip()).expanduser()
    if not path.is_absolute():
        path = project_root / path
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path.absolute())


def _trim_parser_trailer(text: str) -> str:
    """Remove a following top-level command accidentally captured by the parser."""
    match = _DECLARATION_TRAILER_RE.search(text)
    return (text[: match.start()] if match else text).rstrip()


def _metadata_line_index(lines: list[str], declaration_index: int) -> int:
    """Return the first contiguous doc-comment/attribute line for a declaration."""
    index = declaration_index
    while index > 0:
        previous = lines[index - 1].strip()
        if previous.startswith("@["):
            index -= 1
            continue
        if previous.endswith("-/"):
            cursor = index - 1
            while cursor >= 0 and not lines[cursor].lstrip().startswith(("/--", "/-")):
                cursor -= 1
            if cursor < 0:
                break
            index = cursor
            continue
        break
    return index


def declaration_slice(source: str, name: str) -> DeclarationSlice | None:
    """Return the exact declaration slice for ``name`` from one source revision."""
    target = str(name or "").strip()
    if not target:
        return None
    entries = _declaration_line_index_from_text(source)
    entry = next(
        (item for item in entries if str(item.get("name", "") or "").strip() == target),
        None,
    )
    if entry is None:
        return None
    raw_text = _trim_parser_trailer(str(entry.get("text", "") or ""))
    if not raw_text:
        return None
    lines = source.splitlines(keepends=True)
    line_index = max(0, int(entry.get("line", 1) or 1) - 1)
    line_offset = sum(len(line) for line in lines[:line_index])
    # The shared declaration parser intentionally normalizes all line endings
    # to LF. Re-slice the same number of lines from the original source so the
    # ownership ledger preserves CRLF and every other raw UTF-8 byte.
    raw_line_count = max(1, len(raw_text.splitlines()))
    region = "".join(lines[line_index : line_index + raw_line_count])
    exact_text = region.strip()
    if "\n".join(exact_text.splitlines()) != raw_text:
        return None
    start = line_offset + len(region) - len(region.lstrip())
    metadata_index = _metadata_line_index(lines, line_index)
    metadata_start = sum(len(line) for line in lines[:metadata_index])
    # Preserve the legacy decomposition-ledger identity. Promotion cleanup
    # separately reconstructs the full statement for dependent-let results.
    signature = exact_text.split(":=", 1)[0].rstrip()
    if not signature or signature == exact_text:
        return None
    return DeclarationSlice(
        name=target,
        kind=str(entry.get("kind", "") or "").strip().lower(),
        start=start,
        end=start + len(exact_text),
        metadata_start=metadata_start,
        text=exact_text,
        signature=signature,
        signature_sha256=signature_sha256(signature),
        declaration_sha256=_sha256_text(exact_text),
    )


def _record_identity(record: Mapping[str, Any]) -> str:
    """Return a deterministic id for one exact source insertion."""
    identity = {
        "file": str(record.get("file", "") or ""),
        "parent": str(record.get("parent", "") or ""),
        "before_source_sha256": str(record.get("before_source_sha256", "") or ""),
        "after_source_sha256": str(record.get("after_source_sha256", "") or ""),
        "helpers": [
            {
                "name": str(item.get("name", "") or ""),
                "signature_sha256": str(item.get("signature_sha256", "") or ""),
                "dependencies": [
                    str(dependency or "") for dependency in (item.get("dependencies") or [])
                ],
            }
            for item in (record.get("helpers") or [])
            if isinstance(item, Mapping)
        ],
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(payload)


def _retained_provenance_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain every live transaction plus bounded newest terminal history."""
    live = [
        item
        for item in records
        if str(item.get("state", "") or "") not in {"committed", "reverted"}
    ]
    if len(live) > _PROVENANCE_CAP:
        raise ValueError("too many live decomposition provenance transactions")
    history = [
        item for item in records if str(item.get("state", "") or "") in {"committed", "reverted"}
    ]
    return [*history[-_PROVENANCE_CAP:], *live]


def _upsert_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one immutable transaction, returning an exact idempotent match."""
    stored = dict(record)
    transaction_id = str(stored.get("transaction_id", "") or "")

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        records = [
            dict(item)
            for item in (summary.get("decomposition_provenance") or [])
            if isinstance(item, Mapping)
        ]
        for item in records:
            if str(item.get("transaction_id", "") or "") != transaction_id:
                continue
            if item == stored:
                return item
            raise ValueError("decomposition transaction id collision")
        records.append(stored)
        summary["decomposition_provenance"] = _retained_provenance_records(records)
        return stored

    return dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def begin_decomposition(
    *,
    active_file: str,
    target_symbol: str,
    skeletons: Sequence[str],
    before_text: str,
    after_text: str,
    before_bytes: bytes | None = None,
    after_bytes: bytes | None = None,
    helper_dependencies: Mapping[str, Sequence[str]] | None = None,
    cwd: str = "",
    operation: SourceOperation | None = None,
) -> dict[str, Any]:
    """Persist exact parent/helper ownership before writing decomposed source."""
    if not plan_state.plan_state_enabled():
        return {}
    project_root = Path(cwd or ".").expanduser().resolve()
    file_identity = canonical_file(active_file, project_root)
    if operation is None or str(operation.path) != file_identity:
        raise ValueError("decomposition provenance requires the pinned source operation")
    before_source = before_text.encode("utf-8") if before_bytes is None else before_bytes
    after_source = after_text.encode("utf-8") if after_bytes is None else after_bytes
    try:
        before_round_trip = before_source.decode("utf-8")
        after_round_trip = after_source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("cannot record decomposition: source is not valid UTF-8") from exc
    if before_round_trip != before_text or after_round_trip != after_text:
        raise ValueError("cannot record decomposition: source text does not match exact bytes")
    parent = declaration_slice(before_text, target_symbol)
    if parent is None:
        raise ValueError(f"cannot record decomposition: parent {target_symbol!r} is absent")
    helpers: list[dict[str, Any]] = []
    for skeleton in skeletons:
        entries = _declaration_line_index_from_text(str(skeleton or ""))
        if len(entries) != 1:
            raise ValueError("cannot record decomposition: helper skeleton is not one declaration")
        helper_name = str(entries[0].get("name", "") or "").strip()
        helper = declaration_slice(str(skeleton or "").strip(), helper_name)
        if helper is None:
            raise ValueError(f"cannot record decomposition helper {helper_name!r}")
        helpers.append(
            {
                "name": helper.name,
                "kind": helper.kind,
                "inserted_declaration": helper.text,
                "declaration_sha256": helper.declaration_sha256,
                "signature_sha256": helper.signature_sha256,
                "dependencies": [
                    dependency
                    for raw_dependency in (helper_dependencies or {}).get(helper.name, ())
                    if (dependency := str(raw_dependency or "").strip())
                    and dependency != helper.name
                ],
            }
        )
    record: dict[str, Any] = {
        "version": 1,
        "state": "pending",
        "prepared_at": _now_iso(),
        "provenance_kind": "decomposer-transaction",
        "file": file_identity,
        "parent": parent.name,
        "parent_before_declaration": parent.text,
        "parent_before_declaration_sha256": parent.declaration_sha256,
        "parent_signature_sha256": parent.signature_sha256,
        "source_hash_kind": "sha256-raw-utf8-bytes",
        "before_source_sha256": _sha256_bytes(before_source),
        "after_source_sha256": _sha256_bytes(after_source),
        "helpers": helpers,
        **process_identity_details(current_process_identity()),
    }
    record["insertion_fingerprint"] = _record_identity(record)
    attempt_nonce = uuid.uuid4().hex
    record["attempt_nonce"] = attempt_nonce
    record["transaction_id"] = _sha256_text(f"{record['insertion_fingerprint']}\0{attempt_nonce}")
    stored = _upsert_provenance(record)
    transaction_id = str(stored.get("transaction_id", "") or "")
    operation.attempts.add(transaction_id)
    with _ACTIVE_TRANSACTION_LOCK:
        _ACTIVE_TRANSACTIONS.add(transaction_id)
    return stored


def finish_decomposition(transaction_id: str, *, state: str, reason: str = "") -> bool:
    """Conditionally advance one matching pending attempt to a terminal state."""
    if not transaction_id or not plan_state.plan_state_enabled():
        return False
    if state not in {"committed", "reverted", "quarantined"}:
        raise ValueError(f"unsupported decomposition provenance state {state!r}")

    def mutate(summary: dict[str, Any]) -> bool:
        records = [
            dict(item)
            for item in (summary.get("decomposition_provenance") or [])
            if isinstance(item, Mapping)
        ]
        for item in records:
            if str(item.get("transaction_id", "") or "") != transaction_id:
                continue
            if str(item.get("state", "") or "") != "pending":
                return False
            item["state"] = state
            item[f"{state}_at"] = _now_iso()
            if reason:
                item["reason"] = reason
            summary["decomposition_provenance"] = _retained_provenance_records(records)
            return True
        summary["decomposition_provenance"] = _retained_provenance_records(records)
        return False

    try:
        return bool(update_json_file(plan_state.plan_state_paths().summary_json, mutate))
    finally:
        with _ACTIVE_TRANSACTION_LOCK:
            _ACTIVE_TRANSACTIONS.discard(transaction_id)


def _ensure_pending_decomposition_graph(record: Mapping[str, Any]) -> None:
    """Materialize and verify the exact split graph for a source-persisted attempt."""
    from leanflow_cli.workflows import decomposer

    parent = str(record.get("parent", "") or "").strip()
    active_file = str(record.get("file", "") or "").strip()
    skeletons = {
        str(item.get("name", "") or "").strip(): str(item.get("inserted_declaration", "") or "")
        for item in (record.get("helpers") or [])
        if isinstance(item, Mapping)
        and str(item.get("name", "") or "").strip()
        and str(item.get("inserted_declaration", "") or "").strip()
    }
    if not parent or not active_file or not skeletons:
        raise ValueError("pending decomposition lacks exact graph recovery payload")
    recorded = decomposer._record_split_in_graph(
        target_symbol=parent,
        active_file=active_file,
        placed=tuple(skeletons),
        skeletons=skeletons,
        helper_dependencies={
            str(item.get("name", "") or ""): tuple(
                str(dependency or "") for dependency in (item.get("dependencies") or [])
            )
            for item in (record.get("helpers") or [])
            if isinstance(item, Mapping)
        },
    )
    if set(recorded) != set(skeletons):
        raise ValueError("pending decomposition graph recovery was incomplete")


def _stored_canonical_source_path(record: Mapping[str, Any]) -> Path:
    """Return an exact durable source identity without resolving stored components."""
    raw = str(record.get("file", "") or "").strip()
    if not raw:
        raise OSError("decomposition provenance has no durable source identity")
    path = Path(raw)
    _canonical_path_parts(path)
    return path


def _exact_pending_helper_names(
    record: Mapping[str, Any],
    current_source: str,
) -> tuple[str, ...]:
    """Verify every recorded helper is the exact declaration present in source."""
    raw_helpers = record.get("helpers")
    if not isinstance(raw_helpers, list) or not raw_helpers:
        raise ValueError("pending decomposition has no exact helper payload")
    names: list[str] = []
    for raw_helper in raw_helpers:
        if not isinstance(raw_helper, Mapping):
            raise ValueError("pending decomposition helper payload is malformed")
        name = str(raw_helper.get("name", "") or "").strip()
        inserted = str(raw_helper.get("inserted_declaration", "") or "")
        if not name or name in names or not inserted:
            raise ValueError("pending decomposition helper identity is missing or duplicated")
        helper = declaration_slice(current_source, name)
        if helper is None:
            raise ValueError(f"pending decomposition helper {name!r} is absent")
        if (
            helper.text != inserted
            or helper.declaration_sha256 != str(raw_helper.get("declaration_sha256", "") or "")
            or helper.signature_sha256 != str(raw_helper.get("signature_sha256", "") or "")
        ):
            raise ValueError(f"pending decomposition helper {name!r} lost exact identity")
        names.append(name)
    return tuple(names)


def _validate_pending_helpers_in_place(
    record: Mapping[str, Any],
    *,
    operation: SourceOperation,
    current_bytes: bytes,
    cwd: str,
) -> tuple[str, ...]:
    """Rerun Lean validation for every exact helper in an after-hash revision."""
    try:
        current_source = current_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("pending decomposition source is not valid UTF-8") from exc
    names = _exact_pending_helper_names(record, current_source)
    from leanflow_cli.lean.lean_incremental import lean_incremental_check

    for name in names:
        if not _operation_path_is_pinned(operation):
            raise OSError("source path ancestry changed before pending helper validation")
        check = lean_incremental_check(
            action="check_target",
            file_path=str(operation.path),
            theorem_id=name,
            cwd=cwd,
        )
        check = canonical_source_fallback_for_incremental_failure(
            check,
            source=current_source,
            cwd=cwd,
        )
        if not check.get("success", False) or check.get("has_errors"):
            raise ValueError(f"pending helper {name} failed in-place validation")
        if read_source_bytes(operation) != current_bytes:
            raise OSError("source changed during pending helper validation")
        if check.get("canonical_fallback") is True:
            # A clean canonical elaboration validates the whole exact
            # after-source, including every recorded helper, in one pass.
            return names
    return names


def _pending_graph_has_helper_state(record: Mapping[str, Any]) -> bool:
    """Return whether rolling source back could strand an existing helper graph."""
    active_file = str(record.get("file", "") or "").strip()
    try:
        blueprint = plan_state.load_blueprint()
    except Exception:
        return True
    for raw_helper in record.get("helpers") or []:
        if not isinstance(raw_helper, Mapping):
            return True
        name = str(raw_helper.get("name", "") or "").strip()
        if not name:
            return True
        helper_id = plan_state.node_id_for(name, active_file)
        if blueprint.node_by_id(helper_id) is not None:
            return True
        if any(edge.source == helper_id or edge.target == helper_id for edge in blueprint.edges):
            return True
    return False


def _rollback_restored_decomposition_graph(record: Mapping[str, Any]) -> tuple[bool, str]:
    """Remove exact ghost split state after source truth proves helpers absent."""
    from leanflow_cli.workflows import decomposer

    parent = str(record.get("parent", "") or "").strip()
    active_file = str(record.get("file", "") or "").strip()
    helpers: list[str] = []
    for raw_helper in record.get("helpers") or []:
        if not isinstance(raw_helper, Mapping):
            return False, "decomposition graph rollback helper payload is malformed"
        name = str(raw_helper.get("name", "") or "").strip()
        if not name or name in helpers:
            return False, "decomposition graph rollback helper identity is missing or duplicated"
        helpers.append(name)
    outcome = decomposer.rollback_decomposition_graph(
        target_symbol=parent,
        active_file=active_file,
        helper_names=helpers,
    )
    return outcome.ok, outcome.reason


def _reconstruct_before_source(
    record: Mapping[str, Any],
    current_source: str,
) -> bytes | None:
    """Remove the exact contiguous inserted helper block and verify its before hash."""
    parent_name = str(record.get("parent", "") or "").strip()
    parent = declaration_slice(current_source, parent_name)
    if parent is None:
        return None
    if (
        parent.text != str(record.get("parent_before_declaration", "") or "")
        or parent.declaration_sha256
        != str(record.get("parent_before_declaration_sha256", "") or "")
        or parent.signature_sha256 != str(record.get("parent_signature_sha256", "") or "")
    ):
        return None
    inserted = [
        str(item.get("inserted_declaration", "") or "")
        for item in (record.get("helpers") or [])
        if isinstance(item, Mapping)
    ]
    if not inserted or any(not declaration for declaration in inserted):
        return None
    newline_match = re.search(r"\r\n|\n|\r", "".join(inserted)) or re.search(
        r"\r\n|\n|\r", current_source
    )
    newline = newline_match.group(0) if newline_match else "\n"
    block = (newline * 2).join(inserted) + (newline * 2)
    block_end = parent.metadata_start
    block_start = block_end - len(block)
    if block_start < 0 or current_source[block_start:block_end] != block:
        return None
    before = (current_source[:block_start] + current_source[block_end:]).encode("utf-8")
    if _sha256_bytes(before) != str(record.get("before_source_sha256", "") or ""):
        return None
    return before


def _rollback_failed_pending_validation(
    record: Mapping[str, Any],
    *,
    operation: SourceOperation,
    current_bytes: bytes,
    reason: str,
) -> str:
    """Safely revert one exact ungraphed insertion, otherwise quarantine it."""
    transaction_id = str(record.get("transaction_id", "") or "")
    try:
        current_source = current_bytes.decode("utf-8")
    except UnicodeDecodeError:
        current_source = ""
    before_bytes = _reconstruct_before_source(record, current_source) if current_source else None
    if before_bytes is None or _pending_graph_has_helper_state(record):
        finish_decomposition(
            transaction_id,
            state="quarantined",
            reason=f"{reason}; exact rollback could not be authorized",
        )
        return "quarantined"
    try:
        reverted = compare_and_swap_source(
            operation.path,
            expected_bytes=current_bytes,
            replacement_bytes=before_bytes,
            operation=operation,
        )
    except OSError as exc:
        finish_decomposition(
            transaction_id,
            state="quarantined",
            reason=f"{reason}; safe rollback failed: {str(exc)[:160]}",
        )
        return "quarantined"
    if not reverted:
        finish_decomposition(
            transaction_id,
            state="quarantined",
            reason=f"{reason}; source changed before safe rollback",
        )
        return "quarantined"
    graph_reverted, graph_reason = _rollback_restored_decomposition_graph(record)
    if not graph_reverted:
        finish_decomposition(
            transaction_id,
            state="quarantined",
            reason=(
                f"{reason}; source restored but dependency graph rollback failed: "
                f"{graph_reason}"
            ),
        )
        return "quarantined"
    transitioned = finish_decomposition(
        transaction_id,
        state="reverted",
        reason=reason,
    )
    return "reverted" if transitioned else "quarantined"


def recover_pending_decompositions(*, cwd: str = "") -> dict[str, int]:
    """Resolve interrupted insertion records from the exact current source hash."""
    result = {"committed": 0, "reverted": 0, "quarantined": 0}
    if not plan_state.plan_state_enabled():
        return result
    records = [
        dict(item)
        for item in (plan_state.load_summary().get("decomposition_provenance") or [])
        if isinstance(item, Mapping) and str(item.get("state", "") or "") == "pending"
    ]
    for record in records:
        transaction_id = str(record.get("transaction_id", "") or "")
        with _ACTIVE_TRANSACTION_LOCK:
            if transaction_id in _ACTIVE_TRANSACTIONS:
                continue
        try:
            path = _stored_canonical_source_path(record)
            with source_operation(path, canonical=True) as operation:
                current_bytes = read_source_bytes(operation)
                current_hash = hashlib.sha256(current_bytes).hexdigest()
                if current_hash == str(record.get("after_source_sha256", "") or ""):
                    try:
                        current_source = current_bytes.decode("utf-8")
                        _exact_pending_helper_names(record, current_source)
                    except (UnicodeDecodeError, ValueError) as exc:
                        changed = finish_decomposition(
                            transaction_id,
                            state="quarantined",
                            reason=f"exact pending helper identity failed: {str(exc)[:160]}",
                        )
                        result["quarantined"] += int(changed)
                        continue
                    try:
                        _validate_pending_helpers_in_place(
                            record,
                            operation=operation,
                            current_bytes=current_bytes,
                            cwd=cwd,
                        )
                    except Exception as exc:
                        outcome = _rollback_failed_pending_validation(
                            record,
                            operation=operation,
                            current_bytes=current_bytes,
                            reason=f"pending helper validation failed: {str(exc)[:160]}",
                        )
                        result[outcome] += 1
                        continue
                    try:
                        if read_source_bytes(operation) != current_bytes:
                            raise OSError("source changed after pending helper validation")
                        _ensure_pending_decomposition_graph(record)
                        if read_source_bytes(operation) != current_bytes:
                            raise OSError("source changed during pending graph recovery")
                    except Exception as exc:
                        changed = finish_decomposition(
                            transaction_id,
                            state="quarantined",
                            reason=f"dependency graph recovery failed: {str(exc)[:160]}",
                        )
                        result["quarantined"] += int(changed)
                    else:
                        changed = finish_decomposition(transaction_id, state="committed")
                        result["committed"] += int(changed)
                elif current_hash == str(record.get("before_source_sha256", "") or ""):
                    graph_reverted, graph_reason = _rollback_restored_decomposition_graph(record)
                    if not graph_reverted:
                        changed = finish_decomposition(
                            transaction_id,
                            state="quarantined",
                            reason=(
                                "source is restored but dependency graph rollback failed: "
                                f"{graph_reason}"
                            ),
                        )
                        result["quarantined"] += int(changed)
                    else:
                        changed = finish_decomposition(transaction_id, state="reverted")
                        result["reverted"] += int(changed)
                else:
                    changed = finish_decomposition(
                        transaction_id,
                        state="quarantined",
                        reason="source changed across an interrupted decomposition insertion",
                    )
                    result["quarantined"] += int(changed)
        except OSError:
            changed = finish_decomposition(
                transaction_id,
                state="quarantined",
                reason="source unavailable while recovering decomposition insertion",
            )
            result["quarantined"] += int(changed)
    return result


def _resolve_quarantined_decomposition(transaction_id: str, *, reason: str) -> bool:
    """Resolve one quarantined insertion after source truth proves it absent."""

    def mutate(summary: dict[str, Any]) -> bool:
        records = [
            dict(item)
            for item in (summary.get("decomposition_provenance") or [])
            if isinstance(item, Mapping)
        ]
        for item in records:
            if str(item.get("transaction_id", "") or "") != transaction_id:
                continue
            if str(item.get("state", "") or "") != "quarantined":
                return False
            item["state"] = "reverted"
            item["reverted_at"] = _now_iso()
            item["reason"] = reason
            item["quarantine_reconciled"] = True
            summary["decomposition_provenance"] = _retained_provenance_records(records)
            return True
        return False

    return bool(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def reconcile_quarantined_decompositions(*, cwd: str = "") -> dict[str, Any]:
    """Resolve safe source rollbacks and report quarantines that require a pause."""
    result: dict[str, Any] = {"active": 0, "resolved": 0, "reasons": []}
    if not plan_state.plan_state_enabled():
        return result
    records = [
        dict(item)
        for item in (plan_state.load_summary().get("decomposition_provenance") or [])
        if isinstance(item, Mapping) and str(item.get("state", "") or "") == "quarantined"
    ]
    for record in records:
        transaction_id = str(record.get("transaction_id", "") or "")
        path_label = str(record.get("file", "") or "[missing source identity]")[:500]
        try:
            path = _stored_canonical_source_path(record)
            with source_operation(path, canonical=True) as operation:
                current_bytes = read_source_bytes(operation)
                current_text = current_bytes.decode("utf-8")
                current_hash = _sha256_bytes(current_bytes)
                before_hash = str(record.get("before_source_sha256", "") or "")
                helpers = [
                    str(item.get("name", "") or "").strip()
                    for item in (record.get("helpers") or [])
                    if isinstance(item, Mapping) and str(item.get("name", "") or "").strip()
                ]
                parent_name = str(record.get("parent", "") or "").strip()
                parent = declaration_slice(current_text, parent_name)
                parent_matches = bool(
                    parent is not None
                    and parent.signature_sha256
                    == str(record.get("parent_signature_sha256", "") or "")
                )
                helpers_absent = bool(helpers) and all(
                    declaration_slice(current_text, helper_name) is None for helper_name in helpers
                )
                if current_hash == before_hash or (helpers_absent and parent_matches):
                    if read_source_bytes(operation) != current_bytes:
                        raise OSError("source changed before quarantined graph rollback")
                    graph_reverted, graph_reason = _rollback_restored_decomposition_graph(record)
                    if not graph_reverted:
                        result["active"] += 1
                        result["reasons"].append(
                            f"{path}: source is restored but dependency graph rollback failed: "
                            f"{graph_reason}"
                        )
                        continue
                    if read_source_bytes(operation) != current_bytes:
                        result["active"] += 1
                        result["reasons"].append(
                            f"{path}: source changed during quarantined graph rollback"
                        )
                        continue
                    changed = _resolve_quarantined_decomposition(
                        transaction_id,
                        reason=(
                            "source restored to exact pre-insertion revision"
                            if current_hash == before_hash
                            else (
                                "source no longer contains inserted helpers and parent identity "
                                "is intact"
                            )
                        ),
                    )
                    result["resolved"] += int(changed)
                    continue
        except (OSError, UnicodeDecodeError) as exc:
            result["active"] += 1
            result["reasons"].append(f"{path_label}: {str(exc)[:160]}")
            continue
        result["active"] += 1
        result["reasons"].append(
            f"{path}: quarantined helper insertion remains or source identity is ambiguous"
        )
    result["reasons"] = list(result["reasons"][:20])
    return result


@dataclass(frozen=True)
class _HotActivitySnapshot:
    """Pin one bounded hot-run directory generation and its regular files."""

    directory_identity: tuple[int, int] | None
    files: tuple[tuple[str, int, int, int, int, str], ...] = ()


@contextmanager
def _open_state_directory(state_root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    """Open a state subdirectory while rejecting symlinked components."""
    root = state_root.expanduser().resolve(strict=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(root, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(str(root))
        for component in parts:
            child_flags = flags
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            child = os.open(component, child_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise NotADirectoryError(component)
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


@contextmanager
def _open_hot_activity_file(directory: int, name: str) -> Iterator[Any]:
    """Open one no-follow, nonblocking regular hot activity file."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"hot workflow activity is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _strict_hot_activity_events(
    state_root: Path,
    *,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> tuple[_HotActivitySnapshot | None, str]:
    """Stream every hot run under a cap and return its exact file snapshot."""
    try:
        directory_context = _open_state_directory(state_root, ("activity", "runs"))
        directory = directory_context.__enter__()
    except FileNotFoundError:
        return _HotActivitySnapshot(directory_identity=None), ""
    except OSError as exc:
        return None, f"unreadable hot workflow activity root: {str(exc)[:160]}"

    snapshots: list[tuple[str, int, int, int, int, str]] = []
    try:
        directory_stat = os.fstat(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".jsonl"):
                    continue
                if len(snapshots) >= _MAX_LEGACY_HOT_ACTIVITY_FILES:
                    return None, "too many hot workflow activity files to audit safely"
                try:
                    file_context = _open_hot_activity_file(directory, name)
                    handle = file_context.__enter__()
                except OSError as exc:
                    return None, f"unreadable hot workflow activity {name}: {str(exc)[:160]}"
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                    metadata = os.fstat(handle.fileno())
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        record = handle.readline(_MAX_LEGACY_ACTIVITY_RECORD_BYTES + 1)
                        if not record:
                            break
                        digest.update(record)
                        size += len(record)
                        if len(record) > _MAX_LEGACY_ACTIVITY_RECORD_BYTES:
                            return None, f"oversized hot workflow activity record in {name}"
                        try:
                            payload = json.loads(record)
                        except Exception:
                            return None, f"malformed hot workflow activity record in {name}"
                        if not isinstance(payload, dict):
                            return None, f"non-object hot workflow activity record in {name}"
                        if on_event is not None:
                            on_event(payload)
                    final_metadata = os.fstat(handle.fileno())
                    if final_metadata.st_size != size or (
                        final_metadata.st_dev,
                        final_metadata.st_ino,
                    ) != (metadata.st_dev, metadata.st_ino):
                        return None, f"hot workflow activity changed while reading {name}"
                    snapshots.append(
                        (
                            name,
                            int(metadata.st_dev),
                            int(metadata.st_ino),
                            size,
                            int(final_metadata.st_mtime_ns),
                            digest.hexdigest(),
                        )
                    )
                finally:
                    file_context.__exit__(None, None, None)
    except OSError as exc:
        return None, f"unreadable hot workflow activity root: {str(exc)[:160]}"
    finally:
        directory_context.__exit__(None, None, None)
    return (
        _HotActivitySnapshot(
            directory_identity=(int(directory_stat.st_dev), int(directory_stat.st_ino)),
            files=tuple(sorted(snapshots)),
        ),
        "",
    )


def _state_directory_is_empty(state_root: Path, parts: tuple[str, ...]) -> bool:
    """Return whether one no-follow state directory is absent or has no entries."""
    try:
        with _open_state_directory(state_root, parts) as directory:
            with os.scandir(directory) as entries:
                return next(entries, None) is None
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _legacy_retained_evidence_roots_empty(state_root: Path) -> bool:
    """Return whether a missing catalog cannot be hiding retained evidence."""
    if not all(
        _state_directory_is_empty(state_root, parts)
        for parts in (
            ("activity", "archive", "runs"),
            ("activity", "archive", "agents"),
            ("activity", "historical-runs"),
        )
    ):
        return False
    try:
        with _open_state_directory(state_root, ("activity", "archive")) as directory:
            with os.scandir(directory) as entries:
                return all(entry.name in {"runs", "agents"} for entry in entries)
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _normalized_legacy_timestamp(value: str) -> str:
    """Return one timezone-aware ISO timestamp normalized to UTC, or empty."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ""
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _legacy_decomposer_event(
    *, state_root: Path, helper_name: str, file_identity: str
) -> tuple[dict[str, Any] | None, str]:
    """Return globally newest placement only from integrity-complete evidence."""

    matches: dict[str, dict[str, Any]] = {}
    matches_overflowed = False
    evidence_error = ""

    def matching_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
        if str(event.get("type", "") or "") != "decomposer":
            return None
        details = event.get("details")
        if not isinstance(details, Mapping) or not bool(details.get("ok")):
            return None
        placed = {str(item or "").strip() for item in (details.get("placed") or [])}
        if helper_name not in placed:
            return None
        event_file = str(details.get("active_file", "") or details.get("file", "") or "")
        if not event_file:
            return None
        project_root = Path(str(details.get("project_root", "") or ".")).expanduser().resolve()
        if canonical_file(event_file, project_root) != file_identity:
            return None
        target = str(details.get("target_symbol", "") or "").strip()
        timestamp = str(event.get("timestamp", "") or "")
        if not target:
            return None
        nonlocal evidence_error
        if len(target) > _MAX_LEGACY_PARENT_CHARS:
            evidence_error = "matching decomposer evidence has an oversized parent name"
            return None
        if len(timestamp) > _MAX_LEGACY_TIMESTAMP_CHARS:
            evidence_error = "matching decomposer evidence has an oversized timestamp"
            return None
        normalized_timestamp = _normalized_legacy_timestamp(timestamp)
        if not normalized_timestamp:
            evidence_error = "matching decomposer evidence has an invalid timestamp"
            return None
        event_id = str(event.get("event_id", "") or "").strip()
        if len(event_id) > _MAX_LEGACY_EVENT_ID_CHARS:
            event_id = f"sha256:{hashlib.sha256(event_id.encode()).hexdigest()}"
        # Never retain the full activity payload. One valid record may be 8 MiB;
        # ownership selection needs only these three bounded fields.
        return {
            "event_id": event_id,
            "timestamp": timestamp,
            "timestamp_utc": normalized_timestamp,
            "parent": target,
        }

    def consider(event: Mapping[str, Any]) -> None:
        nonlocal evidence_error, matches_overflowed
        match = matching_event(event)
        if match is None:
            return
        event_id = str(match.get("event_id", "") or "").strip()
        if not event_id:
            encoded = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str).encode()
            event_id = hashlib.sha256(encoded).hexdigest()
            match["event_id"] = event_id
        existing = matches.get(event_id)
        if existing is not None:
            if existing != match:
                evidence_error = "conflicting decomposer events reuse one event id"
            return
        if len(matches) >= _MAX_LEGACY_DECOMPOSER_MATCHES:
            matches_overflowed = True
            return
        matches[event_id] = match

    hot_snapshot, hot_reason = _strict_hot_activity_events(state_root, on_event=consider)
    if hot_snapshot is None:
        return None, hot_reason
    if evidence_error:
        return None, evidence_error

    # Retention intentionally keeps normal status reads off gzip. Legacy
    # ownership is a rare recovery path, so require the strict cold audit.
    try:
        from leanflow_cli.workflows.workflow_activity_retention import (
            audit_retained_run_events,
        )

        audit = audit_retained_run_events(
            state_root,
            event_types={"decomposer"},
            on_event=consider,
        )
    except Exception as exc:
        return None, f"retained workflow activity audit failed: {str(exc)[:160]}"
    cold_complete = audit.complete or (
        audit.catalog_status == "missing"
        and audit.catalog_runs == 0
        and _legacy_retained_evidence_roots_empty(state_root)
    )
    if not cold_complete:
        issue_text = ", ".join(f"{code}={count}" for code, count in audit.issue_counts)
        return None, (
            "retained workflow activity evidence is incomplete"
            f" ({audit.catalog_status}{'; ' + issue_text if issue_text else ''})"
        )
    if evidence_error:
        return None, evidence_error
    final_hot_snapshot, final_hot_reason = _strict_hot_activity_events(
        state_root,
        on_event=None,
    )
    if final_hot_snapshot is None:
        return None, final_hot_reason
    if final_hot_snapshot != hot_snapshot:
        return None, "hot workflow activity changed during legacy ownership audit"
    if matches_overflowed:
        return None, "too many matching decomposer events to resolve ownership safely"
    if not matches:
        return None, "no durable successful decomposer event owns the false helper"
    newest_timestamp = max(str(item.get("timestamp_utc", "") or "") for item in matches.values())
    newest: list[dict[str, Any]] = [
        item
        for item in matches.values()
        if str(item.get("timestamp_utc", "") or "") == newest_timestamp
    ]
    parents = {str(item.get("parent", "") or "").strip() for item in newest}
    if len(parents) != 1:
        return None, "same-timestamp decomposer evidence names different parents"
    selected_match = max(newest, key=lambda item: str(item.get("event_id", "") or ""))
    selected = dict(selected_match)
    selected.pop("timestamp_utc", None)
    return selected, ""


def _checkpoint_roots(state_root: Path) -> tuple[Path, ...]:
    """Return candidate verified-patch checkpoint roots without relying on Git."""
    roots = [state_root / "verified-patch-checkpoints"]
    try:
        from leanflow_cli.workflows.workflow_state import workflow_verified_patch_checkpoint_root

        roots.append(workflow_verified_patch_checkpoint_root())
    except Exception:
        pass
    return tuple(dict.fromkeys(root for root in roots if root.is_dir()))


def _legacy_parent_checkpoint(
    *,
    state_root: Path,
    file_identity: str,
    helper_name: str,
    parent_name: str,
    parent_signature_sha256: str,
    before_timestamp: str,
) -> tuple[dict[str, Any], DeclarationSlice] | None:
    """Find the newest verified snapshot proving helper absence and parent fidelity."""
    candidates: list[tuple[str, dict[str, Any], DeclarationSlice]] = []
    for root in _checkpoint_roots(state_root):
        for path in root.glob("*.json"):
            payload = read_json_file(path)
            created_at = str(payload.get("created_at", "") or "")
            if before_timestamp and created_at and created_at > before_timestamp:
                continue
            checkpoint_root = Path(str(payload.get("cwd", "") or ".")).expanduser().resolve()
            checkpoint_file = str(payload.get("file_path", "") or "")
            if (
                not checkpoint_file
                or canonical_file(checkpoint_file, checkpoint_root) != file_identity
            ):
                continue
            before_content = str(payload.get("before_content", "") or "")
            if not before_content or _sha256_text(before_content) != str(
                payload.get("before_sha256", "") or ""
            ):
                continue
            if declaration_slice(before_content, helper_name) is not None:
                continue
            parent = declaration_slice(before_content, parent_name)
            if parent is None or parent.signature_sha256 != parent_signature_sha256:
                continue
            candidates.append((created_at, {**payload, "snapshot_path": str(path)}, parent))
    if not candidates:
        return None
    _created_at, payload, parent = max(candidates, key=lambda item: item[0])
    return payload, parent


def _recorded_helper_matches_promotion(
    raw_helper: Mapping[str, Any],
    *,
    helper_name: str,
    current_helper: DeclarationSlice,
    promoted_signature_sha256: str,
) -> bool:
    """Authenticate one legacy helper row against a full promotion statement.

    The ordinary path preserves the historical signature hash comparison. For
    a dependent-let declaration, additionally require the exact inserted
    declaration payload and its stored hashes to reconstruct both the current
    full statement and the promotion identity. This admits only the known
    legacy parser mismatch, not a statement edited after decomposition.
    """
    if str(raw_helper.get("name", "") or "") != helper_name:
        return False
    recorded_signature = str(raw_helper.get("signature_sha256", "") or "")
    if recorded_signature != current_helper.signature_sha256:
        return False
    if recorded_signature == promoted_signature_sha256:
        return True
    inserted_text = str(raw_helper.get("inserted_declaration", "") or "")
    if not inserted_text or _sha256_text(inserted_text) != str(
        raw_helper.get("declaration_sha256", "") or ""
    ):
        return False
    inserted = declaration_slice(inserted_text, helper_name)
    if (
        inserted is None
        or inserted.declaration_sha256 != str(raw_helper.get("declaration_sha256", "") or "")
        or inserted.signature_sha256 != recorded_signature
    ):
        return False
    return (
        full_declaration_signature_sha256(current_helper.text) == promoted_signature_sha256
        and full_declaration_signature_sha256(inserted.text) == promoted_signature_sha256
    )


def _matching_committed_record(
    *,
    helper_name: str,
    file_identity: str,
    current_helper: DeclarationSlice,
    promoted_signature_sha256: str,
) -> dict[str, Any] | None:
    """Return exact committed provenance for a helper promotion identity."""
    records = [
        dict(item)
        for item in (plan_state.load_summary().get("decomposition_provenance") or [])
        if isinstance(item, Mapping)
        and str(item.get("state", "") or "") == "committed"
        and str(item.get("file", "") or "") == file_identity
    ]
    for record in reversed(records):
        for helper in record.get("helpers") or []:
            if not isinstance(helper, Mapping):
                continue
            if _recorded_helper_matches_promotion(
                helper,
                helper_name=helper_name,
                current_helper=current_helper,
                promoted_signature_sha256=promoted_signature_sha256,
            ):
                return record
    return None


def resolve_helper_provenance(
    *,
    helper_name: str,
    file_label: str,
    promotion_signature_sha256: str,
    current_source: str,
    cwd: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """Resolve exact helper ownership, migrating legacy durable evidence if needed."""
    if not plan_state.plan_state_enabled():
        return None, "plan-state provenance is disabled"
    project_root = Path(cwd or ".").expanduser().resolve()
    file_identity = canonical_file(file_label, project_root)
    recover_pending_decompositions(cwd=str(project_root))
    current_helper = declaration_slice(current_source, helper_name)
    if current_helper is None:
        return None, "current false helper declaration is absent"
    current_full_signature = full_declaration_signature_sha256(current_helper.text)
    if current_full_signature != promotion_signature_sha256:
        return None, "current false helper signature hash differs from promoted evidence"
    stored = _matching_committed_record(
        helper_name=helper_name,
        file_identity=file_identity,
        current_helper=current_helper,
        promoted_signature_sha256=promotion_signature_sha256,
    )
    if stored is not None:
        return stored, ""

    # A legacy activity/checkpoint migration has no exact inserted declaration
    # payload from which to reconstruct the full dependent-let statement. Keep
    # that weaker path fail-closed when the historical prefix digest differs.
    if current_helper.signature_sha256 != promotion_signature_sha256:
        return (
            None,
            "dependent-let helper lacks exact committed insertion provenance",
        )

    state_root = plan_state.plan_state_paths().summary_json.parent
    event, event_reason = _legacy_decomposer_event(
        state_root=state_root,
        helper_name=helper_name,
        file_identity=file_identity,
    )
    if event is None:
        return None, event_reason or "no durable successful decomposer event owns the false helper"
    parent_name = str(event.get("parent", "") or "").strip()
    current_parent = declaration_slice(current_source, parent_name)
    if current_parent is None:
        return None, "decomposer parent declaration is absent from current source"
    checkpoint = _legacy_parent_checkpoint(
        state_root=state_root,
        file_identity=file_identity,
        helper_name=helper_name,
        parent_name=parent_name,
        parent_signature_sha256=current_parent.signature_sha256,
        before_timestamp=str(event.get("timestamp", "") or ""),
    )
    if checkpoint is None:
        return None, "no verified pre-edit checkpoint proves helper absence and parent fidelity"
    checkpoint_payload, parent_before = checkpoint
    record = {
        "version": 1,
        "state": "committed",
        "committed_at": _now_iso(),
        "prepared_at": str(event.get("timestamp", "") or ""),
        "provenance_kind": "legacy-activity-and-verified-checkpoint",
        "file": file_identity,
        "parent": parent_name,
        "parent_before_declaration": parent_before.text,
        "parent_before_declaration_sha256": parent_before.declaration_sha256,
        "parent_signature_sha256": parent_before.signature_sha256,
        "before_source_sha256": str(checkpoint_payload.get("before_sha256", "") or ""),
        "after_source_sha256": "",
        "helpers": [
            {
                "name": helper_name,
                "kind": current_helper.kind,
                "inserted_declaration": "",
                "declaration_sha256": "",
                "signature_sha256": current_helper.signature_sha256,
            }
        ],
        "legacy_decomposer_event_id": str(event.get("event_id", "") or ""),
        "legacy_checkpoint_id": str(checkpoint_payload.get("checkpoint_id", "") or ""),
        "legacy_checkpoint_path": str(checkpoint_payload.get("snapshot_path", "") or ""),
    }
    record["transaction_id"] = _record_identity(record)
    return _upsert_provenance(record), ""
