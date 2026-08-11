"""Hold source and dependency-graph leases across terminal outcome commits."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from leanflow_cli.runtime import file_locks
from leanflow_cli.workflows import decomposition_provenance, plan_state


@dataclass(frozen=True)
class TerminalAuthoritySnapshot:
    """Describe the leased source bytes and graph revision at commit entry."""

    operations: tuple[decomposition_provenance.SourceOperation, ...]
    source_bytes: Mapping[str, bytes]
    blueprint_revision: int

    @property
    def source_paths(self) -> tuple[str, ...]:
        """Return leased source identities in global acquisition order."""
        return tuple(str(operation.path) for operation in self.operations)


def _canonical_source_paths(source_paths: Sequence[str | Path]) -> tuple[Path, ...]:
    """Return unique strict-resolved source identities in global lock order."""
    normalized: set[Path] = set()
    for raw_path in source_paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ValueError(f"terminal source identity is not absolute: {path}")
        lexical = Path(os.path.normpath(str(path)))
        if lexical != path or any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise ValueError(f"terminal source identity is not canonically normalized: {path}")
        # Runtime file tools reserve strict-resolved identities. Resolve here
        # as well so an absolute symlink cannot split the registry lease from
        # the source-operation lease on its real target.
        normalized.add(lexical.resolve(strict=True))
    return tuple(sorted(normalized, key=str))


def _raise_on_failed_release(result: Mapping[str, object], *, identity: Path) -> None:
    """Reject a lease release the registry could not commit authoritatively."""
    if result.get("success") is not True:
        detail = str(result.get("error", "runtime reservation release failed") or "")
        raise RuntimeError(f"terminal runtime reservation release failed for {identity}: {detail}")


@contextlib.contextmanager
def terminal_namespace_guard(
    namespace_paths: Sequence[str | Path],
    *,
    runtime_owner_id: str = "",
) -> Iterator[tuple[Path, ...]]:
    """Hold strict project namespace reservations through terminal persistence."""
    namespaces = _canonical_source_paths(namespace_paths)
    owner_id = str(runtime_owner_id or "").strip() or (
        f"terminal-authority:{os.getpid()}:{threading.get_ident()}"
    )
    with contextlib.ExitStack() as stack:
        for namespace in namespaces:
            if not namespace.is_dir():
                raise RuntimeError(f"terminal namespace is not a directory: {namespace}")
            reservation = file_locks.acquire_namespace_lock(
                str(namespace),
                owner_id=owner_id,
                purpose="terminal mathematical outcome",
                strict=True,
            )
            if reservation.get("success") is not True:
                detail = str(reservation.get("error", "namespace reservation unavailable") or "")
                raise RuntimeError(
                    f"terminal namespace reservation failed for {namespace}: {detail}"
                )

            def release(path: Path = namespace) -> None:
                result = file_locks.release_namespace_lock(
                    str(path),
                    owner_id=owner_id,
                    strict=True,
                )
                _raise_on_failed_release(result, identity=path)

            stack.callback(release)
        yield namespaces


@contextlib.contextmanager
def terminal_authority_guard(
    source_paths: Sequence[str | Path],
    *,
    runtime_owner_id: str = "",
) -> Iterator[TerminalAuthoritySnapshot]:
    """Lease runtime reservations, requested sources, and the terminal graph.

    Ordinary file tools honor the runtime reservation registry, while
    decomposition, promotion, and false-cleanup transactions honor the stronger
    source-operation leases.  Acquire both families in canonical path order,
    followed by the dependency graph, and retain all three through terminal
    persistence. Existing reconciliation code may reacquire the source and graph
    leases on this thread without releasing their outer cross-process handles.
    """
    canonical_paths = _canonical_source_paths(source_paths)
    owner_id = str(runtime_owner_id or "").strip() or (
        f"terminal-authority:{os.getpid()}:{threading.get_ident()}"
    )
    with contextlib.ExitStack() as stack:
        for path in canonical_paths:
            # Use the non-forceable namespace kind even for an exact source
            # identity. Ordinary model tools may force-replace an exact file
            # reservation for recovery, but must never evict terminal authority.
            reservation = file_locks.acquire_namespace_lock(
                str(path),
                owner_id=owner_id,
                purpose="terminal mathematical outcome",
                strict=True,
            )
            if reservation.get("success") is not True:
                detail = str(reservation.get("error", "runtime reservation unavailable") or "")
                raise RuntimeError(f"terminal runtime reservation failed for {path}: {detail}")

            def release(identity: Path = path) -> None:
                result = file_locks.release_namespace_lock(
                    str(identity),
                    owner_id=owner_id,
                    strict=True,
                )
                _raise_on_failed_release(result, identity=identity)

            stack.callback(release)
        operations = tuple(
            stack.enter_context(decomposition_provenance.source_operation(path, canonical=True))
            for path in canonical_paths
        )
        stack.enter_context(plan_state.blueprint_commit_guard())
        snapshots = {
            str(operation.path): decomposition_provenance.read_source_bytes(operation)
            for operation in operations
        }
        yield TerminalAuthoritySnapshot(
            operations=operations,
            source_bytes=MappingProxyType(snapshots),
            blueprint_revision=plan_state.load_blueprint().revision,
        )
