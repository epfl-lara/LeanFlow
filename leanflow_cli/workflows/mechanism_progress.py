"""Derive parent-scoped proof-mechanism provenance for verified graph helpers.

The plan graph keeps every kernel-verified declaration as mathematical
evidence.  This module separately identifies when two structural helpers use
the same proof mechanism, so the portfolio can diversify after repeated
residue/case constructions.  A newly proved, non-covered theorem remains
mathematical campaign progress even when its mechanism repeats, except for a
negation-route node linked only by an ``evidence`` edge. Statement subsumption
is accounted separately. Signatures come from exact local declaration
references in the helper body, with a normalized proof-body fallback when no
local dependency is present; theorem names and arithmetic moduli are never
used as semantic classifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows import plan_state

MECHANISM_SIGNATURE_VERSION = 1
OPEN_PARENT_STATUSES = frozenset(
    {"conjectured", "stated", "audited", "proving", "blocked", "split"}
)

_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.-]*")
_BODY_TOKEN_RE = re.compile(r"\$LOCAL_DEP|[A-Za-z_][A-Za-z0-9_'.-]*|\d+|:=|=>|<-|->|[^\s]")
_PROOF_LANGUAGE_TOKENS = frozenset(
    {
        "aesop",
        "all_goals",
        "any_goals",
        "apply",
        "assumption",
        "at",
        "by",
        "by_cases",
        "calc",
        "case",
        "cases",
        "change",
        "constructor",
        "contradiction",
        "convert",
        "decide",
        "do",
        "dsimp",
        "else",
        "exact",
        "first",
        "fun",
        "have",
        "if",
        "induction",
        "intro",
        "intros",
        "left",
        "let",
        "linarith",
        "match",
        "native_decide",
        "next",
        "nlinarith",
        "norm_num",
        "obtain",
        "omega",
        "only",
        "positivity",
        "rcases",
        "refine",
        "repeat",
        "right",
        "ring",
        "ring_nf",
        "rw",
        "rwa",
        "set",
        "show",
        "simp",
        "simp_all",
        "simpa",
        "subst",
        "suffices",
        "tauto",
        "then",
        "trivial",
        "unfold",
        "using",
        "with",
    }
)


@dataclass(frozen=True)
class MechanismRecord:
    """Describe one verified helper mechanism under one explicit graph parent."""

    node_id: str
    node_name: str
    node_file: str
    parent_id: str
    parent_name: str
    parent_file: str
    mechanism_signature: str
    local_dependencies: tuple[str, ...]
    local_dependency_ids: tuple[str, ...]
    body_provenance_sha256: str
    body_provenance_excerpt: str

    def to_mapping(self) -> dict[str, object]:
        """Return the compact campaign-ledger representation."""
        return {
            "signature_version": MECHANISM_SIGNATURE_VERSION,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "node_file": self.node_file,
            "parent_id": self.parent_id,
            "parent_name": self.parent_name,
            "parent_file": self.parent_file,
            "mechanism_signature": self.mechanism_signature,
            "local_dependencies": list(self.local_dependencies),
            "local_dependency_ids": list(self.local_dependency_ids),
            "body_provenance_sha256": self.body_provenance_sha256,
            "body_provenance_excerpt": self.body_provenance_excerpt,
        }


@dataclass(frozen=True)
class MechanismBatch:
    """Carry one graph reconciliation's mechanism-accounting inputs."""

    historical_records: tuple[MechanismRecord, ...] = ()
    candidate_records: tuple[MechanismRecord, ...] = ()
    forced_node_ids: tuple[str, ...] = ()
    terminal_parent_node_ids: tuple[str, ...] = ()


def parent_ids_for_node(blueprint: plan_state.Blueprint, node_id: str) -> tuple[str, ...]:
    """Return explicit graph parents for a helper node.

    ``split_of`` is the direct child-to-parent relation.  The reciprocal
    ``depends_on`` edge is accepted too because planner deltas may provide one
    side before the decomposer has repaired the pair.
    """
    parents = {
        edge.target
        for edge in blueprint.edges
        if edge.kind == "split_of" and edge.source == node_id
    }
    parents.update(
        edge.source
        for edge in blueprint.edges
        if edge.kind == "depends_on" and edge.target == node_id
    )
    return tuple(sorted(parent_id for parent_id in parents if blueprint.node_by_id(parent_id)))


def linked_helper_node_ids(blueprint: plan_state.Blueprint, node_ids: Iterable[str]) -> set[str]:
    """Return candidate node ids that have at least one explicit graph parent."""
    return {node_id for node_id in node_ids if parent_ids_for_node(blueprint, str(node_id or ""))}


def evidence_only_node_ids(
    blueprint: plan_state.Blueprint,
    node_ids: Iterable[str],
) -> set[str]:
    """Return nodes linked to a target only as non-structural evidence.

    A negation-route declaration may be kernel verified and mathematically
    useful without proving any part of its foreground target.  Its outgoing
    ``evidence`` edge keeps that fact in the graph, while the absence of an
    explicit structural parent excludes it from proof-mechanism accounting.
    """
    candidates = {str(node_id or "") for node_id in node_ids if str(node_id or "")}
    structurally_linked = linked_helper_node_ids(blueprint, candidates)
    evidence_sources = {
        edge.source
        for edge in blueprint.edges
        if edge.kind == "evidence" and edge.source in candidates
    }
    return evidence_sources - structurally_linked


def graph_parent_node_ids(blueprint: plan_state.Blueprint) -> set[str]:
    """Return nodes that own at least one explicit helper/dependency child."""
    return {edge.target for edge in blueprint.edges if edge.kind == "split_of"} | {
        edge.source for edge in blueprint.edges if edge.kind == "depends_on"
    }


def _proof_body(declaration_text: str) -> str:
    marker = _find_assignment_marker_for_statement(str(declaration_text or ""))
    if marker < 0:
        return ""
    return str(declaration_text or "")[marker + 2 :].strip()


def _exact_local_dependencies(
    *,
    entries: Sequence[Mapping[str, Any]],
    helper_entry: Mapping[str, Any],
    proof_body: str,
    active_file: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return exact preceding local declarations referenced by the proof body."""
    sanitized = _strip_lean_comments_and_strings(proof_body)
    tokens = set(_IDENTIFIER_TOKEN_RE.findall(sanitized))
    helper_line = int(helper_entry.get("line", 0) or 0)
    found: list[tuple[str, str]] = []
    for entry in entries:
        name = str(entry.get("name", "") or "").strip()
        line = int(entry.get("line", 0) or 0)
        if not name or name.startswith("[anonymous ") or line <= 0 or line >= helper_line:
            continue
        if name not in tokens:
            continue
        found.append((name, plan_state.node_id_for(name, active_file)))
    found.sort(key=lambda item: item[1])
    return tuple(name for name, _node_id in found), tuple(node_id for _name, node_id in found)


def _replace_exact_dependency_references(proof_body: str, dependency_names: Sequence[str]) -> str:
    text = _strip_lean_comments_and_strings(proof_body)
    for name in sorted({str(name) for name in dependency_names if name}, key=len, reverse=True):
        text = re.sub(
            rf"(?<![A-Za-z0-9_'.-]){re.escape(name)}(?![A-Za-z0-9_'.-])",
            "$LOCAL_DEP",
            text,
        )
    return text


def _normalized_body_provenance(proof_body: str, dependency_names: Sequence[str]) -> str:
    """Return a residue-agnostic proof-body shape for audit and fallback."""
    replaced = _replace_exact_dependency_references(proof_body, dependency_names)
    normalized: list[str] = []
    for token in _BODY_TOKEN_RE.findall(replaced):
        if token == "$LOCAL_DEP":
            normalized.append("$dep")
        elif token.isdigit():
            normalized.append("$num")
        elif _IDENTIFIER_TOKEN_RE.fullmatch(token):
            if token in _PROOF_LANGUAGE_TOKENS or "." in token or token[:1].isupper():
                normalized.append(token)
            else:
                normalized.append("$id")
        else:
            normalized.append(token)
    return " ".join(normalized)


def _mechanism_signature(
    *, local_dependency_ids: Sequence[str], body_provenance_sha256: str
) -> str:
    basis: dict[str, object]
    if local_dependency_ids:
        # Exact local theorem dependencies are the stable strategy identity.
        # Body provenance remains in the ledger for audit, but harmless tactic
        # or residue-specific scaffolding cannot evade mechanism deduplication.
        basis = {
            "version": MECHANISM_SIGNATURE_VERSION,
            "basis": "exact-local-dependencies",
            "dependency_ids": sorted(set(local_dependency_ids)),
        }
    else:
        basis = {
            "version": MECHANISM_SIGNATURE_VERSION,
            "basis": "normalized-proof-body",
            "body_provenance_sha256": body_provenance_sha256,
        }
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_parent_scoped_mechanisms(
    blueprint: plan_state.Blueprint,
    node_ids: Iterable[str],
) -> tuple[MechanismRecord, ...]:
    """Derive mechanisms for graph-linked helpers under unresolved parents.

    Source parsing is fail-open: a missing file, declaration, assignment body,
    or explicit unresolved parent yields no record, so uncertain provenance can
    never suppress genuine graph progress.
    """
    source_cache: dict[str, tuple[dict[str, Any], ...]] = {}
    records: list[MechanismRecord] = []
    for node_id in sorted({str(value or "") for value in node_ids if value}):
        node = blueprint.node_by_id(node_id)
        if node is None or not node.file or not node.name:
            continue
        parent_nodes = [
            parent
            for parent_id in parent_ids_for_node(blueprint, node_id)
            if (parent := blueprint.node_by_id(parent_id)) is not None
            and parent.status in OPEN_PARENT_STATUSES
        ]
        if not parent_nodes:
            continue
        if node.file not in source_cache:
            try:
                content = Path(node.file).read_text(encoding="utf-8")
            except OSError:
                source_cache[node.file] = ()
            else:
                source_cache[node.file] = tuple(
                    dict(entry) for entry in _declaration_line_index_from_text(content)
                )
        entries = source_cache[node.file]
        short_name = node.name.split(".")[-1]
        helper_entry = next(
            (
                entry
                for entry in entries
                if str(entry.get("name", "") or "") in {node.name, short_name}
            ),
            None,
        )
        if helper_entry is None:
            continue
        body = _proof_body(str(helper_entry.get("text", "") or ""))
        if not body:
            continue
        dependency_names, dependency_ids = _exact_local_dependencies(
            entries=entries,
            helper_entry=helper_entry,
            proof_body=body,
            active_file=node.file,
        )
        normalized_body = _normalized_body_provenance(body, dependency_names)
        if not normalized_body:
            continue
        body_hash = hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()
        signature = _mechanism_signature(
            local_dependency_ids=dependency_ids,
            body_provenance_sha256=body_hash,
        )
        for parent in sorted(parent_nodes, key=lambda candidate: candidate.id):
            records.append(
                MechanismRecord(
                    node_id=node.id,
                    node_name=node.name,
                    node_file=node.file,
                    parent_id=parent.id,
                    parent_name=parent.name,
                    parent_file=parent.file,
                    mechanism_signature=signature,
                    local_dependencies=dependency_names,
                    local_dependency_ids=dependency_ids,
                    body_provenance_sha256=body_hash,
                    body_provenance_excerpt=normalized_body[:600],
                )
            )
    return tuple(records)


def related_previously_proved_helpers(
    blueprint: plan_state.Blueprint,
    *,
    parent_ids: Iterable[str],
    previously_proved_node_ids: Iterable[str],
) -> set[str]:
    """Return proved sibling helpers relevant to candidate parent scopes."""
    wanted_parents = {str(parent_id or "") for parent_id in parent_ids if parent_id}
    proved = {str(node_id or "") for node_id in previously_proved_node_ids if node_id}
    if not wanted_parents or not proved:
        return set()
    return {
        node_id
        for node_id in proved
        if wanted_parents.intersection(parent_ids_for_node(blueprint, node_id))
    }


def newly_exhaustive_trigger_nodes(
    before: plan_state.Blueprint,
    after: plan_state.Blueprint,
    *,
    newly_verified_node_ids: Iterable[str],
) -> set[str]:
    """Return verified children that complete an explicit exhaustive split.

    ``split`` is the graph's explicit claim that the listed dependencies form
    the parent's decomposition.  Merely adding another ``depends_on`` edge is
    not treated as exhaustive; every declared dependency must become proved,
    and the transition must be new in this reconciliation.
    """
    newly_verified = {
        str(node_id or "") for node_id in newly_verified_node_ids if str(node_id or "")
    }
    if not newly_verified:
        return set()
    before_by_id = {node.id: node for node in before.nodes}
    after_by_id = {node.id: node for node in after.nodes}
    triggers: set[str] = set()
    for parent in after.nodes:
        before_parent = before_by_id.get(parent.id)
        if parent.status != "split" and (before_parent is None or before_parent.status != "split"):
            continue
        dependency_ids = {
            edge.target
            for edge in after.edges
            if edge.kind == "depends_on" and edge.source == parent.id
        }
        if not dependency_ids or not dependency_ids.intersection(newly_verified):
            continue
        if not all(
            (dependency := after_by_id.get(dependency_id)) is not None
            and dependency.status == "proved"
            for dependency_id in dependency_ids
        ):
            continue
        was_exhaustive = all(
            (dependency := before_by_id.get(dependency_id)) is not None
            and dependency.status == "proved"
            for dependency_id in dependency_ids
        )
        if not was_exhaustive:
            triggers.update(dependency_ids.intersection(newly_verified))
    return triggers


def build_mechanism_batch(
    before: plan_state.Blueprint,
    after: plan_state.Blueprint,
    *,
    previously_proved_node_ids: Iterable[str],
    newly_verified_node_ids: Iterable[str],
    eligible_node_ids: Iterable[str],
) -> MechanismBatch:
    """Build campaign-accounting inputs for newly verified graph nodes.

    Relevant historical siblings are backfilled only for the candidate parent
    scopes.  This preserves cross-restart deduplication without rescanning the
    entire graph on every sync.  Unparseable provenance fails open as ordinary
    progress; helpers whose only parents are already terminal and nodes linked
    only as negation evidence do not.
    """
    previous = {str(node_id or "") for node_id in previously_proved_node_ids if str(node_id or "")}
    newly_verified = {
        str(node_id or "") for node_id in newly_verified_node_ids if str(node_id or "")
    }
    eligible = {str(node_id or "") for node_id in eligible_node_ids if str(node_id or "")}
    linked = linked_helper_node_ids(after, newly_verified)
    candidate_records = derive_parent_scoped_mechanisms(after, newly_verified)
    candidate_record_nodes = {record.node_id for record in candidate_records}
    candidate_parent_ids = {record.parent_id for record in candidate_records}
    historical_ids = related_previously_proved_helpers(
        after,
        parent_ids=candidate_parent_ids,
        previously_proved_node_ids=previous,
    )
    historical_records = derive_parent_scoped_mechanisms(after, historical_ids)

    open_linked: set[str] = set()
    terminal_linked: set[str] = set()
    for node_id in linked:
        parents = [
            parent
            for parent_id in parent_ids_for_node(after, node_id)
            if (parent := after.node_by_id(parent_id)) is not None
        ]
        if any(parent.status in OPEN_PARENT_STATUSES for parent in parents):
            open_linked.add(node_id)
        else:
            terminal_linked.add(node_id)

    evidence_only = evidence_only_node_ids(after, newly_verified)
    unlinked = newly_verified - linked - evidence_only
    unclassified_open = open_linked - candidate_record_nodes
    parent_closures = newly_verified.intersection(graph_parent_node_ids(after))
    exhaustive = newly_exhaustive_trigger_nodes(
        before,
        after,
        newly_verified_node_ids=newly_verified,
    )
    forced = ((unlinked | unclassified_open) & eligible) | parent_closures | exhaustive
    return MechanismBatch(
        historical_records=historical_records,
        candidate_records=candidate_records,
        forced_node_ids=tuple(sorted(forced)),
        terminal_parent_node_ids=tuple(sorted(terminal_linked)),
    )
