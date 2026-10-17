"""Bound owned publication evidence discovery without treating ordinary project files as authority."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .evidence import contained, digest, identifier, identity
from .publication_case_paths import aliased_document_controls
from .publication_io import PublicationConflict, entry_path

MAX_ENTRIES = 16384
MAX_PROBED_ENTRIES = 262144
MAX_CONTROL_BYTES = 32 * 1024**2
MAX_CONTROL_FILE_BYTES = 8 * 1024**2
REGISTRY_RELATIVE = ".leanflow/formalization-publications.json"
INDEX_HISTORY = ".leanflow/formalization-publication-index-history"
EFFECT_HISTORY = ".leanflow/formalization-publication-effect-history"
TRANSACTION_DIRECTORY = ".leanflow/formalization-publication-transactions"


class PublicationAuthorityLoss(PublicationConflict):
    """Refuse acceptance when retained publication history has lost complete index authority."""


class PublicationDiscoveryLimit(PublicationAuthorityLoss):
    """Preserve retained history beyond the supported automatic recovery bounds."""


def discovery_limit(reason: str) -> PublicationDiscoveryLimit:
    """Give an actionable fail-closed limit instead of an impossible acknowledgment loop."""
    return PublicationDiscoveryLimit(
        reason + "; automatic authority-loss recovery is unsupported at this size. "
        "Keep retained history and consult the publication recovery limits in docs/formalization.md; "
        "no acknowledgment can grant authority from a truncated discovery"
    )


def bounded_control_bytes(path: Path, budget: list[int]) -> bytes | str | None:
    """Read regular observations without unbounded allocation or blocking on substituted FIFOs."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return "nonregular"
        if info.st_size > MAX_CONTROL_FILE_BYTES or budget[0] + info.st_size > MAX_CONTROL_BYTES:
            raise discovery_limit("Publication authority-loss discovery exceeds its byte bound")
        data = handle.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise PublicationAuthorityLoss("Publication evidence changed during bounded discovery")
        budget[0] += len(data)
        return data


def _owned_history(path: Path, *, index_history: bool = False) -> bool:
    """Ignore foreign names while preserving every supported retained identity."""
    if index_history and path.suffix != ".json":
        return False
    try:
        identifier(path.stem if index_history else path.name)
    except ValueError:
        return False
    return True


class _InventoryPaths:
    """Share bounded literal ownership enumeration across publication loss observations."""

    def __init__(self, root: Path, evidence: dict[str, str | None]) -> None:
        self.root = root
        self.evidence = evidence
        self.entries = 0
        self.probes = 0

    def probe(self) -> None:
        """Bound filesystem enumeration independently of the retained-control count."""
        self.probes += 1
        if self.probes > MAX_PROBED_ENTRIES:
            raise discovery_limit("Publication authority-loss discovery exceeds its probe bound")

    def exact_entry(self, path: Path) -> bool:
        """Check portable spelling on case-folding hosts without following an alias."""
        try:
            with os.scandir(path.parent) as children:
                for child in children:
                    self.probe()
                    if child.name == path.name:
                        return True
        except FileNotFoundError:
            return False
        return False

    def directory(
        self, relative: str, candidate: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        """Collect owned candidates under separate enumeration and evidence limits."""
        path = entry_path(self.root, relative)
        if not path.exists() or (relative == "formalization" and not self.exact_entry(path)):
            return []
        if path.is_symlink() or not path.is_dir():
            if relative != "formalization":
                self.evidence[relative] = "nonregular"
            return []
        result = []
        with os.scandir(path) as children:
            for child in children:
                self.probe()
                child_path = path / child.name
                if candidate is not None and not candidate(child_path):
                    continue
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    raise discovery_limit(
                        "Publication authority-loss discovery exceeds its entry bound"
                    )
                result.append(child_path)
        return sorted(result)

    def owned_document(self, path: Path) -> bool:
        """Identify literal document directories containing LeanFlow control surfaces."""
        try:
            identifier(path.name)
        except ValueError:
            return False
        if path.is_symlink() or not path.is_dir():
            return False
        with os.scandir(path) as children:
            for child in children:
                self.probe()
                if child.name in {
                    "current.json",
                    "handoff.json",
                    "proof-current.json",
                    "publication.json",
                }:
                    return True
                if child.name in {"receipts", "proof-revisions"} and child.is_dir(
                    follow_symlinks=False
                ):
                    with os.scandir(path / child.name) as records:
                        for record in records:
                            self.probe()
                            if record.name.endswith(".json"):
                                return True
        return False


def discover_current_authority(root: Path) -> dict[str, Any]:
    """Bound current-pointer scope using the same literal document ownership as index discovery."""
    evidence: dict[str, str | None] = {}
    inventory = _InventoryPaths(root, evidence)
    revisions: set[str] = set()
    budget = [0]
    for document in inventory.directory("formalization", inventory.owned_document):
        path = document / "current.json"
        if not inventory.exact_entry(path):
            continue
        relative = path.relative_to(root).as_posix()
        data = "nonregular" if path.is_symlink() else bounded_control_bytes(path, budget)
        evidence[relative] = digest(data) if isinstance(data, bytes) else data
        if not isinstance(data, bytes):
            continue
        try:
            value = json.loads(data)
            revisions.add(identifier(value.get("revision_id", "")))
        except (ValueError, UnicodeError, AttributeError):
            continue
    return {"evidence": evidence, "revision_ids": sorted(revisions)}


def owned_document_paths(root: Path) -> list[Path]:
    """Enumerate literal portable document owners without importing their acceptance."""
    inventory = _InventoryPaths(root, {})
    return inventory.directory("formalization", inventory.owned_document)


def discover_authority(root: Path) -> dict[str, Any]:
    """Inventory bounded owned publication controls, preserving absence and invalid identities.

    Portable projections are evidence of prior acceptance, not an implicit trust
    import. Private intake and ordinary source files alone are a genuinely fresh
    project. Never follow symlinks or recurse through dependency/build caches.
    """
    evidence: dict[str, str | None] = {}
    transactions: dict[str, str] = {}
    revisions: set[str] = set()
    write_paths: set[str] = set()
    control_bytes = [0]
    inventory = _InventoryPaths(root, evidence)
    controls: dict[str, dict[str, Any]] = {}

    def revision(value: Any) -> None:
        """Retain valid historical IDs without importing their acceptance."""
        if isinstance(value, str):
            try:
                valid = identifier(value)
            except ValueError:
                return
            revisions.add(valid)
            if len(revisions) > MAX_ENTRIES:
                raise discovery_limit("Publication revision discovery exceeds its entry bound")

    def control(path: Path) -> dict[str, Any]:
        """Freeze each regular control's exact bytes once per observation."""
        relative = path.relative_to(root).as_posix()
        if relative in controls:
            return controls[relative]
        location = entry_path(root, relative)
        if not location.exists() and not location.is_symlink():
            evidence[relative] = None
            return {}
        if not location.is_file() or location.is_symlink():
            evidence[relative] = "nonregular"
            return {}
        data = bounded_control_bytes(location, control_bytes)
        if not isinstance(data, bytes):
            evidence[relative] = data
            return {}
        evidence[relative] = digest(data)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        revision(value.get("revision_id"))
        revision(value.get("source_revision_id"))
        revision(value.get("reviewed_revision_id"))
        controls[relative] = value
        return value

    def transaction(key: Any, relative: Any) -> None:
        """Bind owned journal paths and canonical write destinations."""
        if not isinstance(key, str) or not isinstance(relative, str):
            return
        try:
            identifier(key)
            path = contained(root, relative)
        except (ValueError, OSError):
            return
        if not relative.startswith(".leanflow/") or not relative.endswith("/publication.json"):
            return
        if key in transactions and transactions[key] != relative:
            raise PublicationAuthorityLoss(
                "Publication transaction identities have conflicting locations"
            )
        transactions[key] = relative
        if len(transactions) > MAX_ENTRIES:
            raise discovery_limit("Publication transaction discovery exceeds its entry bound")
        value = control(path)
        writes = value.get("writes", {})
        if isinstance(writes, dict):
            for name in writes:
                try:
                    contained(root, name)
                except (ValueError, TypeError):
                    continue
                write_paths.add(name)
                if len(write_paths) > MAX_ENTRIES:
                    raise discovery_limit(
                        "Publication destination discovery exceeds its entry bound"
                    )
        for item in (
            value.get("resolutions", []) if isinstance(value.get("resolutions"), list) else []
        ):
            if isinstance(item, dict):
                revision(item.get("revision_id"))

    def registry_control(path: Path) -> None:
        """Retain old index locations and revocations while rejecting selected approval."""
        value = control(path)
        selected = value.get("selected_revision")
        if isinstance(selected, dict):
            revision(selected.get("revision_id"))
        for row in (
            value.get("revoked_revisions", [])
            if isinstance(value.get("revoked_revisions"), list)
            else []
        ):
            if isinstance(row, dict):
                revision(row.get("revision_id"))
        rows = value.get("transactions", {})
        if isinstance(rows, dict):
            for key, row in rows.items():
                if isinstance(row, dict):
                    revision(row.get("revision_id"))
                    loss = row.get("loss")
                    if isinstance(loss, dict) and isinstance(loss.get("revision_ids"), list):
                        for previous in loss["revision_ids"]:
                            revision(previous)
                    transaction(key, row.get("journal"))

    registry_path = entry_path(root, REGISTRY_RELATIVE)
    if registry_path.exists() or registry_path.is_symlink():
        registry_control(registry_path)
    for path in inventory.directory(
        INDEX_HISTORY, lambda path: _owned_history(path, index_history=True)
    ):
        registry_control(path)
    for path in inventory.directory(
        EFFECT_HISTORY, lambda path: _owned_history(path, index_history=True)
    ):
        control(path)
        transaction(path.stem, f"{TRANSACTION_DIRECTORY}/{path.stem}/publication.json")

    for path in inventory.directory(TRANSACTION_DIRECTORY, _owned_history):
        relative = path.relative_to(root).as_posix()
        if not path.is_dir() or path.is_symlink():
            control(path)
            continue
        transaction(path.name, relative + "/publication.json")
        # Even a malformed or empty owned directory is surviving history.
        evidence.setdefault(relative + "/publication.json", None)

    for document in inventory.directory("formalization", inventory.owned_document):
        relative = document.relative_to(root).as_posix()
        if not document.is_dir() or document.is_symlink():
            continue
        for name in ("current.json", "handoff.json", "proof-current.json", "publication.json"):
            path = document / name
            if inventory.exact_entry(path):
                value = control(path)
                if name == "publication.json":
                    transaction(value.get("transaction_id"), value.get("journal"))
        for name in ("receipts", "proof-revisions"):
            for path in inventory.directory(relative + "/" + name):
                if path.name.endswith(".json") or path.is_symlink():
                    control(path)

    for path in aliased_document_controls(
        root, inventory.probe, lambda path: bounded_control_bytes(path, control_bytes)
    ):
        control(path)

    # Legacy run-owned pointers still identify journals after an author run ended.
    def owns_pointer(path: Path) -> bool:
        """Exclude native runs which retain no publication pointer."""
        return (
            path.is_dir()
            and not path.is_symlink()
            and (
                (path / "publication/publication.json").exists()
                or (path / "publication/publication.json").is_symlink()
            )
        )

    for run in inventory.directory(".leanflow/native-workflows", owns_pointer):
        if not run.is_dir() or run.is_symlink():
            continue
        pointer = run / "publication/publication.json"
        if pointer.exists() or pointer.is_symlink():
            value = control(pointer)
            location = value.get("journal")
            if value.get("journal_scope") != "project" and isinstance(location, str):
                location = (pointer.parent.relative_to(root) / location).as_posix()
            transaction(value.get("transaction_id"), location)
    payload = {
        "schema_version": 1,
        "evidence": evidence,
        "transactions": transactions,
        "revision_ids": sorted(revisions),
        "write_paths": sorted(write_paths),
    }
    return {**payload, "discovery_id": identity(payload)}


def assert_registry_covers_retained(root: Path, registry: Mapping[str, Any]) -> None:
    """Refuse restored older indexes which omit owned transactions or retired index history.

    Scan directory entries rather than hashing every journal a second time on
    healthy reads. The normal registry reader independently validates registered
    journal contents and their resolution records.
    """
    acknowledgment = registry.get("registry_loss_acknowledgment") or {}
    observed = acknowledgment.get("observation", {}).get("retained_slots", {})
    retired = acknowledgment.get("retired_registry") or {}
    case_inventory = _InventoryPaths(root, {})
    case_budget = [0]
    for path in aliased_document_controls(
        root, case_inventory.probe, lambda path: bounded_control_bytes(path, case_budget)
    ):
        if path.relative_to(root).as_posix() not in observed:
            raise PublicationAuthorityLoss(
                "Publication registry is incomplete for case-renamed publication controls; "
                "preserve the history, restore literal control spelling and inspect publication recovery"
            )

    def children(relative: str) -> Iterator[Path]:
        """Stream owned history with a budget proportional to the known index."""
        path = entry_path(root, relative)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise PublicationAuthorityLoss(
                "Publication registry is incomplete for nonregular retained history"
            )
        count = 0
        probes = 0
        limit = MAX_ENTRIES + len(registry["transactions"]) + len(observed)
        with os.scandir(path) as entries:
            for child in entries:
                probes += 1
                if probes > MAX_PROBED_ENTRIES + len(registry["transactions"]) + len(observed):
                    raise discovery_limit(
                        "Publication retained-history index check exceeds its probe bound"
                    )
                child_path = path / child.name
                if not _owned_history(
                    child_path, index_history=relative in {INDEX_HISTORY, EFFECT_HISTORY}
                ):
                    continue
                count += 1
                if count > limit:
                    raise discovery_limit(
                        "Publication retained-history index check exceeds its entry bound"
                    )
                yield child_path

    for path in children(TRANSACTION_DIRECTORY):
        relative = path.relative_to(root).as_posix()
        journal = (
            relative + "/publication.json" if path.is_dir() and not path.is_symlink() else relative
        )
        row = registry["transactions"].get(path.name)
        if isinstance(row, dict) and (
            row.get("journal") == journal
            or (
                path.is_dir() and not path.is_symlink() and not (path / "publication.json").exists()
            )
        ):
            continue
        if journal in observed:
            continue
        raise PublicationAuthorityLoss(
            "Publication registry is incomplete; unindexed retained transaction history must be acknowledged before fresh acceptance"
        )
    for path in children(INDEX_HISTORY):
        relative = path.relative_to(root).as_posix()
        if relative not in observed and relative != retired.get("path"):
            raise PublicationAuthorityLoss(
                "Publication registry is incomplete; unindexed retired registry history requires explicit loss acknowledgment"
            )
    for path in children(EFFECT_HISTORY):
        if (
            path.stem not in registry["transactions"]
            and path.relative_to(root).as_posix() not in observed
        ):
            raise PublicationAuthorityLoss(
                "Publication registry is incomplete; unindexed effect-retirement history requires explicit loss acknowledgment"
            )
