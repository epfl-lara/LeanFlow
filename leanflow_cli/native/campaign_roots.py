"""Seal deterministic requested-scope roots before native provider work."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.workflows import (
    campaign_root_registry,
    decomposition_provenance,
    negation_promotion,
    plan_state,
)

CampaignRootRegistryAudit = campaign_root_registry.CampaignRootRegistryAudit
audit_campaign_root_registry = campaign_root_registry.audit_campaign_root_registry


@dataclass(frozen=True)
class CampaignRootSetup:
    """Report one fresh-campaign requested-root handshake."""

    ok: bool
    reason: str
    roots: tuple[dict[str, str], ...] = ()
    registered: bool = False
    legacy: bool = False


@dataclass(frozen=True)
class _RootCandidate:
    """Bind one named open theorem to its leased source declaration."""

    theorem: str
    kind: str
    source_path: str
    statement: str
    source_sha256: str


_CAMPAIGN_ROOTS_FIELD = campaign_root_registry.CAMPAIGN_ROOTS_FIELD
_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD = (
    campaign_root_registry.CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD
)


def _canonical_source_paths(
    source_files: Sequence[str | Path], project_root: Path
) -> tuple[Path, ...]:
    """Return unique canonical source identities in global lease order."""
    canonical: set[Path] = set()
    for raw in source_files:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = project_root / path
        canonical.add(path.resolve(strict=True))
    return tuple(sorted(canonical, key=str))


def _named_open_roots(
    operation: decomposition_provenance.SourceOperation,
) -> tuple[_RootCandidate, ...]:
    """Parse named theorem/lemma declarations that still contain ``sorry``."""
    source_bytes = decomposition_provenance.read_source_bytes(operation)
    source_text = source_bytes.decode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    roots: list[_RootCandidate] = []
    seen: set[str] = set()
    for entry in _declaration_line_index_from_text(source_text):
        kind = str(entry.get("kind", "") or "").strip()
        theorem = str(entry.get("name", "") or "").strip()
        if kind not in {"theorem", "lemma"} or not theorem or not entry.get("has_sorry"):
            continue
        if theorem.startswith("[anonymous "):
            continue
        if theorem in seen:
            raise ValueError(
                f"source has ambiguous duplicate named declaration {theorem}: {operation.path}"
            )
        seen.add(theorem)
        roots.append(
            _RootCandidate(
                theorem=theorem,
                kind=kind,
                source_path=str(operation.path),
                statement=str(entry.get("text", "") or ""),
                source_sha256=source_sha256,
            )
        )
    return tuple(roots)


def _node_reaches_source(node_file: str, *, source: Path, project_root: Path) -> bool:
    """Return whether a graph file label resolves to one canonical source."""
    candidate = Path(str(node_file or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        return candidate.resolve(strict=True) == source
    except (OSError, RuntimeError):
        return False


def _materialize_root_nodes(
    candidates: Sequence[_RootCandidate],
    *,
    project_root: Path,
) -> tuple[tuple[plan_state.GraphNode, ...], str]:
    """Create only missing stated queue-sync nodes through graph revision CAS."""
    blueprint = plan_state.load_blueprint()
    updated = blueprint
    created: list[plan_state.GraphNode] = []
    for candidate in candidates:
        source = Path(candidate.source_path)
        matches = [
            node
            for node in updated.nodes
            if node.name == candidate.theorem
            and _node_reaches_source(node.file, source=source, project_root=project_root)
        ]
        if len(matches) > 1:
            return (), f"dependency graph has ambiguous root {candidate.theorem}"
        if matches:
            node = matches[0]
            if node.id != plan_state.node_id_for(node.name, node.file):
                return (), f"dependency graph root id is not deterministic: {candidate.theorem}"
            continue
        node = plan_state.GraphNode(
            id=plan_state.node_id_for(candidate.theorem, candidate.source_path),
            kind=candidate.kind,
            name=candidate.theorem,
            file=candidate.source_path,
            statement=candidate.statement,
            source_sha256=candidate.source_sha256,
            status="stated",
            generated_by="queue-sync",
        )
        updated = updated.replace_node(node)
        created.append(node)
    if updated != blueprint:
        plan_state.save_blueprint(updated)
    return tuple(created), ""


def _rollback_created_nodes(created: Sequence[plan_state.GraphNode]) -> str:
    """Remove only unchanged, still-unreferenced nodes created by this attempt."""
    if not created:
        return ""
    blueprint = plan_state.load_blueprint()
    created_by_id = {node.id: node for node in created}
    removable = {
        node_id
        for node_id, expected in created_by_id.items()
        if blueprint.node_by_id(node_id) == expected
        and not any(edge.source == node_id or edge.target == node_id for edge in blueprint.edges)
    }
    if removable != set(created_by_id):
        return "created root nodes changed before rollback"
    restored = replace(
        blueprint,
        nodes=tuple(node for node in blueprint.nodes if node.id not in removable),
    )
    try:
        plan_state.save_blueprint(restored)
    except Exception as exc:
        return f"created root node rollback failed: {type(exc).__name__}: {exc}"
    return ""


def initialize_campaign_roots(
    *,
    campaign_id: str,
    project_root: str | Path,
    source_files: Sequence[str | Path],
) -> CampaignRootSetup:
    """Enumerate, materialize, and seal a fresh campaign's immutable roots.

    An already sealed or marker-absent legacy campaign returns without reading
    source. Fresh registration holds every canonical source lease from parsing
    through graph materialization and the registry commit. Empty named scopes
    are sealed explicitly so definitions and anonymous examples cannot
    deadlock provider work while retaining zero terminal-disproof authority.
    """
    campaign = plan_state.load_summary().get("campaign")
    if (
        isinstance(campaign, Mapping)
        and campaign.get(_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD) is False
    ):
        audit = audit_campaign_root_registry(campaign)
        if not audit.ok:
            return CampaignRootSetup(False, audit.reason)
    provider_allowed, gate_reason = negation_promotion.campaign_root_provider_gate()
    if provider_allowed:
        return CampaignRootSetup(
            True,
            gate_reason,
            registered="registered" in gate_reason,
            legacy="legacy" in gate_reason,
        )

    root = Path(project_root).expanduser().resolve()
    created: tuple[plan_state.GraphNode, ...] = ()
    try:
        source_paths = _canonical_source_paths(source_files, root)
        with contextlib.ExitStack() as stack:
            operations = [
                stack.enter_context(decomposition_provenance.source_operation(path, canonical=True))
                for path in source_paths
            ]
            candidates = tuple(
                candidate for operation in operations for candidate in _named_open_roots(operation)
            )
            semantic_keys = [(candidate.theorem, candidate.source_path) for candidate in candidates]
            if len(semantic_keys) != len(set(semantic_keys)):
                return CampaignRootSetup(False, "requested root enumeration is ambiguous")
            created, materialization_reason = _materialize_root_nodes(
                candidates,
                project_root=root,
            )
            if materialization_reason:
                return CampaignRootSetup(False, materialization_reason)
            requested = [
                {
                    "target_symbol": candidate.theorem,
                    "active_file": candidate.source_path,
                }
                for candidate in candidates
            ]
            registration = negation_promotion.record_requested_campaign_roots(
                requested,
                campaign_id=campaign_id,
                cwd=str(root),
            )
            if not registration.ok:
                rollback_reason = _rollback_created_nodes(created)
                reason = registration.reason
                if rollback_reason:
                    reason = f"{reason}; {rollback_reason}"
                return CampaignRootSetup(False, reason)
            for node in created:
                # The summary registry commit is the point of no return. A
                # journal outage cannot roll graph nodes back underneath its
                # now-sealed identities; graph/summary remain authoritative
                # and the journal is best-effort observability here.
                with contextlib.suppress(Exception):
                    plan_state.append_journal_event(
                        {
                            "event": "node-created",
                            "node_id": node.id,
                            "name": node.name,
                            "file": node.file,
                            "why": "immutable campaign-root registration",
                        }
                    )
            return CampaignRootSetup(
                True,
                registration.reason,
                roots=tuple(
                    {
                        "target_symbol": candidate.theorem,
                        "active_file": candidate.source_path,
                    }
                    for candidate in candidates
                ),
                registered=True,
            )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        plan_state.PlanStateRevisionConflict,
    ) as exc:
        rollback_reason = _rollback_created_nodes(created)
        reason = f"requested root initialization failed: {str(exc)[:200]}"
        if rollback_reason:
            reason = f"{reason}; {rollback_reason}"
        return CampaignRootSetup(False, reason)


def source_files_for_scope(
    *,
    project_root: str | Path,
    explicit_file: str = "",
    project_files: Sequence[str | Path] = (),
) -> tuple[Path, ...]:
    """Return explicit-file or project-file inputs without queue assignment state."""
    root = Path(project_root).expanduser().resolve()
    if explicit_file:
        path = Path(explicit_file).expanduser()
        if not path.is_absolute():
            path = root / path
        return (path,)
    return tuple(Path(path) for path in project_files)
