"""Contain one-branch residue research without discarding Lean facts.

Kernel-checked singleton and single-congruence helpers remain usable proof
artifacts. Exact closed ``target_case_k_eq_N`` instances are evidence
immediately; ordinary singleton, audit, and single-congruence helpers become
evidence once an unresolved graph parent already owns several distinct helpers
from that finite-case family. This module supplies the shared deterministic
family identity used by research-result and graph/queue accounting.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
    _text_has_sorry,
)
from leanflow_cli.workflows import plan_state

FINITE_BRANCH_FAMILY = "finite_or_single_congruence_coverage"
SATURATION_MIN_PRIOR_BRANCHES = 4
_JOURNAL_HISTORY_TAIL_BYTES = 4 * 1024 * 1024

_CONGRUENCE_RE = re.compile(r"\bt\s*%\s*(\d+)\s*=\s*(\d+)\b")
_SINGLETON_RE = re.compile(r"\bt\s*=\s*(\d+)\b(?!\s*[*+/\-^])")
_DECLARATION_NAME_RE = re.compile(
    r"^\s*(?:(?:private|protected|noncomputable)\s+)*(?:lemma|theorem)\s+([^\s(:]+)"
)
_AUDIT_HELPER_NAME_RE = re.compile(r"^(?P<stem>[A-Za-z_][A-Za-z0-9_']*)_audit_k(?P<value>\d+)$")
_TARGET_CASE_HELPER_NAME_RE = re.compile(
    r"^(?P<target>[A-Za-z_][A-Za-z0-9_']*)_case_k_eq_(?P<value>\d+)$"
)
_EXISTENTIAL_NAT_RESULT_RE = re.compile(
    r"^\s*∃\s+(?:\([^()]*:\s*(?:ℕ|Nat)\s*\)|[^,:]+:\s*(?:ℕ|Nat))\s*,",
    flags=re.DOTALL,
)
_TYPED_NAT_EXPRESSION_RE = re.compile(r"\(([^()]*)\s*:\s*(?:ℕ|Nat)\s*\)")
_OPEN_PARENT_STATUSES = frozenset(
    {"conjectured", "stated", "audited", "proving", "blocked", "split"}
)
_UNUSABLE_BRIDGE_STATUSES = frozenset({"false", "parked"})
_NAT_BINDER_RE = re.compile(
    r"(?:[({]\s*[^(){}:]+\s*:\s*(?:ℕ|Nat)\s*[)}])|" r"(?:∀\s+[^,.:]+\s*:\s*(?:ℕ|Nat)\b)"
)
_UNIVERSAL_NAT_BINDER_RE = re.compile(
    r"(?:∀|forall)\s+(?:\([^()]*:\s*(?:ℕ|Nat)\s*\)|[^,.:()]+\s*:\s*(?:ℕ|Nat)\b)"
)
_NAMED_NAT_BINDER_RE = re.compile(r"[({]\s*([A-Za-z_][A-Za-z0-9_']*)\s*:\s*(?:ℕ|Nat)\s*[)}]")
_PAREN_BINDER_RE = re.compile(r"\(([^()]*)\)")


@dataclass(frozen=True, order=True)
class FiniteBranch:
    """Identify one singleton or one positive residue class."""

    kind: str
    modulus: int = 0
    residue: int = 0
    value: int = 0
    identity: str = ""

    @property
    def fingerprint(self) -> str:
        """Return the stable branch-family fingerprint."""
        if self.kind == "congruence":
            return f"congruence:t%{self.modulus}={self.residue}"
        if self.kind == "closed_target_case":
            return f"closed-target-case:{self.identity}"
        if self.kind == "closed_singleton":
            return f"closed-singleton:{self.identity}"
        return f"singleton:t={self.value}"


@dataclass(frozen=True, order=True)
class FiniteCaseCoverage:
    """Identify one helper restricted to an explicit finite value set."""

    variable: str
    values: tuple[int, ...]

    @property
    def fingerprint(self) -> str:
        """Return the stable direct-equality coverage fingerprint."""
        rendered = ",".join(str(value) for value in self.values)
        return f"finite-cases:{self.variable}={{{rendered}}}"


@dataclass(frozen=True)
class SaturatedFiniteBranchAssessment:
    """Describe a proved helper retained as finite-case evidence."""

    node_id: str
    node_name: str
    parent_ids: tuple[str, ...]
    branch: FiniteBranch
    prior_node_ids: tuple[str, ...]
    prior_branch_count: int


@dataclass(frozen=True)
class RepeatedSingletonEvidenceAssessment:
    """Describe an unintegrated finite-case edit that must be rolled back."""

    target_symbol: str
    candidate_names: tuple[str, ...]
    prior_names: tuple[str, ...]
    candidate_branches: tuple[str, ...]
    bridge_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvedNodeEvent:
    """Identify one gate-backed graph promotion in durable journal order."""

    node_id: str
    proved_at: datetime


def branch_from_facts(
    witnesses: Sequence[int],
    congruences: Sequence[tuple[int, int]],
) -> FiniteBranch | None:
    """Return the sole finite branch represented by semantic facts, if any."""
    branches = {
        FiniteBranch(kind="singleton", value=int(value)) for value in witnesses if int(value) >= 0
    }
    branches.update(
        FiniteBranch(kind="congruence", modulus=int(modulus), residue=int(residue))
        for modulus, residue in congruences
        if int(modulus) > 0 and 0 <= int(residue) < int(modulus)
    )
    return next(iter(branches)) if len(branches) == 1 else None


def _top_level_result_colon(suffix: str) -> int:
    """Return the declaration result colon outside binder delimiters."""
    depth = 0
    for index, character in enumerate(suffix):
        if character in "([{":
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1
        elif character == ":" and depth == 0:
            return index
    return -1


def _signature_quantifies_over_nat(signature: str) -> bool:
    """Return whether a declaration binds a natural before or inside its result."""
    name_match = _DECLARATION_NAME_RE.match(signature)
    if name_match is None:
        return False
    suffix = signature[name_match.end() :]
    result_colon = _top_level_result_colon(suffix)
    if result_colon < 0:
        return False
    binders = suffix[:result_colon]
    result = suffix[result_colon + 1 :]
    return bool(_NAT_BINDER_RE.search(binders) or _UNIVERSAL_NAT_BINDER_RE.search(result))


def _finite_case_coverage(
    declaration: str,
    *,
    target_symbol: str,
) -> FiniteCaseCoverage | None:
    """Return a target-prefixed finite equality-disjunction hypothesis.

    This intentionally recognizes only the live ``s = 2 ∨ ... ∨ s = 5``
    proof shape.  Congruences, inequalities, universal conclusions, and
    structural helpers fail open so they remain available as proof bridges.
    """
    text = str(declaration or "")
    name_match = _DECLARATION_NAME_RE.match(text)
    if name_match is None:
        return None
    declaration_name = name_match.group(1).rsplit(".", 1)[-1]
    target_name = str(target_symbol or "").strip().rsplit(".", 1)[-1]
    if not target_name or not declaration_name.startswith(target_name + "_at_"):
        return None
    marker = _find_assignment_marker_for_statement(text)
    signature = text[:marker] if marker >= 0 else text
    signature = _strip_lean_comments_and_strings(signature)
    suffix = signature[name_match.end() :]
    result_colon = _top_level_result_colon(suffix)
    if result_colon < 0:
        return None
    binders = suffix[:result_colon]
    nat_variables = tuple(dict.fromkeys(_NAMED_NAT_BINDER_RE.findall(binders)))
    if len(nat_variables) != 1:
        return None
    coverages: set[FiniteCaseCoverage] = set()
    for binder_match in _PAREN_BINDER_RE.finditer(binders):
        binder = binder_match.group(1)
        _name, separator, proposition = binder.partition(":")
        if not separator:
            continue
        proposition = proposition.strip()
        for variable in nat_variables:
            direct_case = rf"{re.escape(variable)}\s*=\s*\d+"
            if re.fullmatch(rf"{direct_case}(?:\s*∨\s*{direct_case})+", proposition) is None:
                continue
            values = tuple(
                sorted(
                    {
                        int(match.group(1))
                        for match in re.finditer(
                            rf"\b{re.escape(variable)}\s*=\s*(\d+)\b",
                            proposition,
                        )
                    }
                )
            )
            if len(values) >= 2:
                coverages.add(FiniteCaseCoverage(variable=variable, values=values))
    return next(iter(coverages)) if len(coverages) == 1 else None


def _closed_singleton_branch(
    declaration: str,
    *,
    target_symbol: str,
) -> FiniteBranch | None:
    """Return a narrow closed ``target_at_case`` singleton identity."""
    text = str(declaration or "")
    name_match = _DECLARATION_NAME_RE.match(text)
    if name_match is None:
        return None
    declaration_name = name_match.group(1).rsplit(".", 1)[-1]
    target_name = str(target_symbol or "").strip().rsplit(".", 1)[-1]
    if not target_name or not declaration_name.startswith(target_name + "_at_"):
        return None
    signature = text
    marker = _find_assignment_marker_for_statement(signature)
    if marker >= 0:
        signature = signature[:marker]
    signature = _strip_lean_comments_and_strings(signature)
    if _signature_quantifies_over_nat(signature):
        return None
    suffix = signature[name_match.end() :].lstrip()
    # This policy is intentionally narrower than "a theorem containing a
    # numeral": the helper must be a closed, target-prefixed case declaration.
    if not suffix.startswith(":") or not re.search(r"\b\d+\b", suffix):
        return None
    normalized = " ".join(signature.split())
    identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return FiniteBranch(kind="closed_singleton", identity=identity)


def _typed_nat_expression_contains_literal(result: str, value: int) -> bool:
    """Return whether a typed natural subexpression contains the exact literal."""
    literal = re.compile(rf"(?<![A-Za-z0-9_']){value}(?![A-Za-z0-9_'])")
    return any(
        literal.search(expression) for expression in _TYPED_NAT_EXPRESSION_RE.findall(result)
    )


def _closed_instantiated_target_branch(
    declaration: str,
    *,
    target_symbol: str,
) -> FiniteBranch | None:
    """Return one conservative closed audit or exact target-case branch.

    These helpers instantiate an open natural parameter inside a target-shaped
    existential proposition. Trust only complete source declarations whose
    name identifies the literal, whose result repeats that literal in a typed
    natural expression, and which bind no input or universal natural.
    """
    text = str(declaration or "")
    name_match = _DECLARATION_NAME_RE.match(text)
    if name_match is None:
        return None
    declaration_name = name_match.group(1).rsplit(".", 1)[-1]
    target_name = str(target_symbol or "").strip().rsplit(".", 1)[-1]
    audit_match = _AUDIT_HELPER_NAME_RE.fullmatch(declaration_name)
    case_match = _TARGET_CASE_HELPER_NAME_RE.fullmatch(declaration_name)
    if audit_match is None and case_match is None:
        return None
    if not target_name:
        return None
    if case_match is not None:
        stem = str(case_match.group("target") or "")
        if stem != target_name:
            return None
        kind = "closed_target_case"
        literal = int(case_match.group("value"))
    else:
        assert audit_match is not None
        stem = str(audit_match.group("stem") or "")
        if target_name != stem and not target_name.startswith(stem + "_"):
            return None
        kind = "closed_singleton"
        literal = int(audit_match.group("value"))

    marker = _find_assignment_marker_for_statement(text)
    if marker < 0 or _text_has_sorry(text):
        return None
    signature = _strip_lean_comments_and_strings(text[:marker])
    if _signature_quantifies_over_nat(signature):
        return None
    suffix = signature[name_match.end() :]
    result_colon = _top_level_result_colon(suffix)
    if result_colon < 0 or suffix[:result_colon].strip():
        return None
    result = suffix[result_colon + 1 :]
    if _EXISTENTIAL_NAT_RESULT_RE.match(result) is None:
        return None
    if not _typed_nat_expression_contains_literal(result, literal):
        return None
    return FiniteBranch(
        kind=kind,
        value=literal,
        identity=f"{stem}:k={literal}",
    )


def branch_from_declaration(
    declaration: str,
    *,
    target_symbol: str = "",
) -> FiniteBranch | None:
    """Return one literal ``t`` branch from a declaration signature only."""
    text = str(declaration or "")
    name_match = _DECLARATION_NAME_RE.match(text)
    declaration_name = name_match.group(1).rsplit(".", 1)[-1] if name_match is not None else ""
    if _AUDIT_HELPER_NAME_RE.fullmatch(declaration_name) or _TARGET_CASE_HELPER_NAME_RE.fullmatch(
        declaration_name
    ):
        # Special instantiated-target names must satisfy the closed-source
        # contract; never let a malformed or parametric lookalike fall through
        # to the generic literal scanner.
        return _closed_instantiated_target_branch(
            declaration,
            target_symbol=target_symbol,
        )
    signature = str(declaration or "")
    marker = _find_assignment_marker_for_statement(signature)
    if marker >= 0:
        signature = signature[:marker]
    signature = _strip_lean_comments_and_strings(signature)
    congruences = {
        (int(match.group(1)), int(match.group(2)))
        for match in _CONGRUENCE_RE.finditer(signature)
        if int(match.group(1)) > 0 and int(match.group(2)) < int(match.group(1))
    }
    witnesses = {int(match.group(1)) for match in _SINGLETON_RE.finditer(signature)}
    literal = branch_from_facts(sorted(witnesses), sorted(congruences))
    if literal is not None:
        return literal
    return _closed_singleton_branch(declaration, target_symbol=target_symbol)


def branch_from_checked_declarations(
    checked_code: Sequence[str],
    *,
    target_symbol: str,
) -> FiniteBranch | None:
    """Return the sole source-backed branch across checked declarations."""
    branches = branches_from_checked_declarations(
        checked_code,
        target_symbol=target_symbol,
    )
    return branches[0] if len(branches) == 1 else None


def branches_from_checked_declarations(
    checked_code: Sequence[str],
    *,
    target_symbol: str,
) -> tuple[FiniteBranch, ...]:
    """Return every distinct source-backed branch across checked declarations."""
    branches = {
        branch
        for declaration in checked_code
        if (branch := branch_from_declaration(declaration, target_symbol=target_symbol)) is not None
    }
    return tuple(sorted(branches, key=lambda branch: branch.fingerprint))


def immediate_evidence_branch(branch: FiniteBranch) -> bool:
    """Return whether a closed target-case helper is evidence before saturation."""
    return branch.kind == "closed_target_case"


def _entry_map(source: str) -> dict[str, dict[str, Any]]:
    """Return the first parsed declaration under each exact and short name."""
    entries: dict[str, dict[str, Any]] = {}
    for raw in _declaration_line_index_from_text(str(source or "")):
        entry = dict(raw)
        name = str(entry.get("name", "") or "").strip()
        if not name:
            continue
        entries.setdefault(name, entry)
        entries.setdefault(name.rsplit(".", 1)[-1], entry)
    return entries


def _same_file(left: str, right: str) -> bool:
    """Return whether two source labels resolve to the same local file."""
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _target_requires_uniform_coverage(entry: Mapping[str, Any] | None) -> bool:
    """Return whether the target declaration quantifies over a natural input."""
    declaration = str(dict(entry or {}).get("text", "") or "")
    marker = _find_assignment_marker_for_statement(declaration)
    signature = declaration[:marker] if marker >= 0 else declaration
    signature = _strip_lean_comments_and_strings(signature)
    return _signature_quantifies_over_nat(signature)


def _branch_for_entry(
    entry: Mapping[str, Any] | None,
    *,
    target_symbol: str,
) -> FiniteBranch | None:
    """Return one finite branch from a parsed declaration entry."""
    if entry is None:
        return None
    return branch_from_declaration(
        str(entry.get("text", "") or ""),
        target_symbol=target_symbol,
    )


def _coverage_for_entry(
    entry: Mapping[str, Any] | None,
    *,
    target_symbol: str,
) -> FiniteCaseCoverage | None:
    """Return explicit finite equality coverage from one parsed entry."""
    if entry is None:
        return None
    return _finite_case_coverage(
        str(entry.get("text", "") or ""),
        target_symbol=target_symbol,
    )


def _finite_fingerprint_for_entry(
    entry: Mapping[str, Any] | None,
    *,
    target_symbol: str,
) -> str:
    """Return one singleton or finite-set fingerprint for edit containment."""
    branch = _branch_for_entry(entry, target_symbol=target_symbol)
    if branch is not None and branch.kind in {
        "singleton",
        "closed_singleton",
        "closed_target_case",
    }:
        return branch.fingerprint
    coverage = _coverage_for_entry(entry, target_symbol=target_symbol)
    return coverage.fingerprint if coverage is not None else ""


def _target_child_ids(blueprint: plan_state.Blueprint, target_id: str) -> set[str]:
    """Return explicit structural children of one target graph node."""
    children = {
        edge.source
        for edge in blueprint.edges
        if edge.kind == "split_of" and edge.target == target_id
    }
    children.update(
        edge.target
        for edge in blueprint.edges
        if edge.kind == "depends_on" and edge.source == target_id
    )
    return children


def assess_repeated_unintegrated_singleton_edit(
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
    before_text: str,
    after_text: str,
    helper_names: Iterable[str],
    evidence_helper_names: Iterable[str],
) -> RepeatedSingletonEvidenceAssessment | None:
    """Reject repeated closed or explicitly grouped cases without a bridge.

    The first unintegrated finite-case helper remains available as base
    evidence.  A later singleton or direct finite equality-disjunction must
    either be referenced by the target in the same edit (and therefore be
    absent from ``evidence_helper_names``) or sit beside an explicit non-finite
    structural child such as an induction step or exhaustive-coverage
    reduction.  Source parsing supplies durable resume behavior; graph
    ambiguity fails open.
    """
    target = str(target_symbol or "").strip()
    file = str(active_file or "").strip()
    if not target or not file:
        return None
    before_entries = _entry_map(before_text)
    after_entries = _entry_map(after_text)
    target_entry = before_entries.get(target) or before_entries.get(target.rsplit(".", 1)[-1])
    if not _target_requires_uniform_coverage(target_entry):
        return None
    after_target_entry = after_entries.get(target) or after_entries.get(target.rsplit(".", 1)[-1])
    if (
        _text_has_sorry(str((target_entry or {}).get("text", "") or ""))
        and after_target_entry is not None
        and not _text_has_sorry(str(after_target_entry.get("text", "") or ""))
    ):
        # A newly closed target belongs at the authoritative target kernel
        # gate. Never discard that proof merely because the same edit also
        # introduced an unused finite observation.
        return None

    requested = {str(name or "").strip() for name in helper_names if str(name or "").strip()}
    evidence = {
        str(name or "").strip()
        for name in evidence_helper_names
        if str(name or "").strip() in requested
    }
    finite_fingerprint_by_name = {
        name: fingerprint
        for name in requested
        if (
            fingerprint := _finite_fingerprint_for_entry(
                after_entries.get(name) or after_entries.get(name.rsplit(".", 1)[-1]),
                target_symbol=target,
            )
        )
    }
    candidate_names = sorted(
        name
        for name in evidence
        if finite_fingerprint_by_name.get(name)
        and not (before_entries.get(name) or before_entries.get(name.rsplit(".", 1)[-1]))
    )
    if not candidate_names:
        return None

    # A non-finite helper integrated in this same edit is already a concrete
    # bridge even though it has not reached the graph transaction yet.
    bridge_names = {
        name for name in requested - evidence if not finite_fingerprint_by_name.get(name)
    }
    target_id = plan_state.node_id_for(target, file)
    for node_id in _target_child_ids(blueprint, target_id):
        node = blueprint.node_by_id(node_id)
        if (
            node is None
            or node.status in _UNUSABLE_BRIDGE_STATUSES
            or not _same_file(node.file, file)
        ):
            continue
        entry = after_entries.get(node.name) or after_entries.get(node.name.rsplit(".", 1)[-1])
        if not _finite_fingerprint_for_entry(entry, target_symbol=target):
            bridge_names.add(node.name)
    if bridge_names:
        return None

    prior_names: set[str] = set()
    target_short = target.rsplit(".", 1)[-1]
    for name, entry in before_entries.items():
        if "." not in name and name != str(entry.get("name", "") or ""):
            continue
        fingerprint = _finite_fingerprint_for_entry(entry, target_symbol=target)
        if not fingerprint:
            continue
        if _text_has_sorry(str(entry.get("text", "") or "")):
            continue
        declaration_name = str(entry.get("name", "") or "").rsplit(".", 1)[-1]
        if declaration_name.startswith(target_short + "_at_"):
            prior_names.add(str(entry.get("name", "") or declaration_name))

    linked_ids = _target_child_ids(blueprint, target_id)
    linked_ids.update(
        edge.source
        for edge in blueprint.edges
        if edge.kind == "evidence" and edge.target == target_id
    )
    for node_id in linked_ids:
        node = blueprint.node_by_id(node_id)
        if node is None or node.status != "proved" or not _same_file(node.file, file):
            continue
        entry = after_entries.get(node.name) or after_entries.get(node.name.rsplit(".", 1)[-1])
        fingerprint = _finite_fingerprint_for_entry(entry, target_symbol=target)
        if fingerprint and not _text_has_sorry(str((entry or {}).get("text", "") or "")):
            prior_names.add(node.name)

    # A singleton integrated in the same edit counts as an existing base for
    # deciding whether another unintegrated sibling is merely accumulation.
    prior_names.update(
        name for name in requested - evidence if finite_fingerprint_by_name.get(name)
    )
    if not prior_names and len(candidate_names) == 1:
        return None
    return RepeatedSingletonEvidenceAssessment(
        target_symbol=target,
        candidate_names=tuple(candidate_names),
        prior_names=tuple(sorted(prior_names)),
        candidate_branches=tuple(finite_fingerprint_by_name[name] for name in candidate_names),
    )


def strictly_broader_than(current: FiniteBranch, prior: FiniteBranch) -> bool:
    """Return whether one congruence strictly contains a prior congruence.

    Singleton-to-congruence scaling is deliberately not broader-family
    progress: it is the live finite-sieve amplifier this policy contains.
    """
    return bool(
        current.kind == prior.kind == "congruence"
        and current != prior
        and current.modulus > 0
        and prior.modulus % current.modulus == 0
        and prior.residue % current.modulus == current.residue
    )


def _parent_ids(blueprint: plan_state.Blueprint, node_id: str) -> tuple[str, ...]:
    """Return explicit structural parents for one helper graph node."""
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
    return tuple(sorted(parent for parent in parents if blueprint.node_by_id(parent)))


def _entry_for_node(
    entries: Sequence[Mapping[str, Any]],
    name: str,
) -> Mapping[str, Any] | None:
    """Return one exact-or-short declaration entry, failing open on ambiguity."""
    wanted = str(name or "").strip()
    aliases = {wanted, wanted.rsplit(".", 1)[-1]}
    matches = [entry for entry in entries if str(entry.get("name", "") or "") in aliases]
    return matches[0] if len(matches) == 1 else None


def _source_entries(
    file: str,
    cache: dict[str, tuple[dict[str, Any], ...]],
) -> tuple[dict[str, Any], ...]:
    """Return cached declaration entries for one source file."""
    if file not in cache:
        try:
            source = Path(file).read_text(encoding="utf-8")
        except OSError:
            cache[file] = ()
        else:
            cache[file] = tuple(dict(entry) for entry in _declaration_line_index_from_text(source))
    return cache[file]


def _node_branch(
    node: plan_state.GraphNode,
    cache: dict[str, tuple[dict[str, Any], ...]],
    *,
    target_symbol: str = "",
) -> FiniteBranch | None:
    """Return one source-backed finite branch for a graph node."""
    if not node.file or not node.name:
        return None
    entry = _entry_for_node(_source_entries(node.file, cache), node.name)
    if entry is None:
        return None
    return branch_from_declaration(
        str(entry.get("text", "") or ""),
        target_symbol=target_symbol,
    )


def assess_saturated_finite_branch_helpers(
    blueprint: plan_state.Blueprint,
    node_ids: Iterable[str],
    *,
    previously_proved_node_ids: Iterable[str] | None = None,
) -> dict[str, SaturatedFiniteBranchAssessment]:
    """Return newly proved one-branch helpers contained as campaign evidence.

    Exact closed target-case instances are contained immediately. Other finite
    branches require a saturated parent family. Source ambiguity fails open as
    ordinary progress. Explicit terminal parent closure remains authoritative
    and is never suppressed here.
    """
    candidates = {str(node_id or "") for node_id in node_ids if str(node_id or "")}
    prior_filter = (
        {str(node_id or "") for node_id in previously_proved_node_ids if str(node_id or "")}
        if previously_proved_node_ids is not None
        else None
    )
    source_cache: dict[str, tuple[dict[str, Any], ...]] = {}
    assessments: dict[str, SaturatedFiniteBranchAssessment] = {}
    for node_id in sorted(candidates):
        node = blueprint.node_by_id(node_id)
        if node is None or node.status != "proved":
            continue
        parent_ids = tuple(
            parent_id
            for parent_id in _parent_ids(blueprint, node_id)
            if (parent := blueprint.node_by_id(parent_id)) is not None
            and parent.status in _OPEN_PARENT_STATUSES
        )
        if not parent_ids:
            continue
        branch = _node_branch(node, source_cache)
        if branch is None:
            for parent_id in parent_ids:
                parent = blueprint.node_by_id(parent_id)
                if parent is None:
                    continue
                branch = _node_branch(
                    node,
                    source_cache,
                    target_symbol=parent.name,
                )
                if branch is not None:
                    break
        if branch is None:
            continue
        prior_nodes: dict[str, FiniteBranch] = {}
        for sibling in blueprint.nodes:
            if (
                sibling.id == node_id
                or sibling.status != "proved"
                or sibling.file != node.file
                or (prior_filter is not None and sibling.id not in prior_filter)
                or not set(parent_ids).intersection(_parent_ids(blueprint, sibling.id))
            ):
                continue
            sibling_branch = _node_branch(sibling, source_cache)
            if sibling_branch is None:
                for parent_id in parent_ids:
                    parent = blueprint.node_by_id(parent_id)
                    if parent is None:
                        continue
                    sibling_branch = _node_branch(
                        sibling,
                        source_cache,
                        target_symbol=parent.name,
                    )
                    if sibling_branch is not None:
                        break
            if sibling_branch is not None:
                prior_nodes[sibling.id] = sibling_branch
        distinct_prior = {value.fingerprint for value in prior_nodes.values()}
        immediate_evidence = immediate_evidence_branch(branch)
        if not immediate_evidence and len(distinct_prior) < SATURATION_MIN_PRIOR_BRANCHES:
            continue
        if not immediate_evidence and any(
            strictly_broader_than(branch, prior) for prior in prior_nodes.values()
        ):
            continue
        assessments[node_id] = SaturatedFiniteBranchAssessment(
            node_id=node_id,
            node_name=node.name,
            parent_ids=parent_ids,
            branch=branch,
            prior_node_ids=tuple(sorted(prior_nodes)),
            prior_branch_count=len(distinct_prior),
        )
    return assessments


def deferred_helper_names(
    blueprint: plan_state.Blueprint,
    helper_names: Iterable[str],
) -> dict[str, SaturatedFiniteBranchAssessment]:
    """Return contained finite-branch assessments keyed by helper name."""
    requested = {str(name or "").strip() for name in helper_names if str(name or "").strip()}
    node_ids = {
        node.id
        for node in blueprint.nodes
        if node.name in requested or node.name.rsplit(".", 1)[-1] in requested
    }
    return {
        assessment.node_name: assessment
        for assessment in assess_saturated_finite_branch_helpers(blueprint, node_ids).values()
        if assessment.node_name
    }


def _parse_timestamp(value: Any) -> datetime | None:
    """Return one normalized UTC timestamp, or None when malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _journal_tail_records() -> tuple[dict[str, Any], ...]:
    """Return a bounded complete-line tail of the append-only plan journal."""
    path = plan_state.plan_state_paths().journal_jsonl
    try:
        size = path.stat().st_size
        start = max(0, size - _JOURNAL_HISTORY_TAIL_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(_JOURNAL_HISTORY_TAIL_BYTES)
    except OSError:
        return ()
    if start:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return ()
    records: list[dict[str, Any]] = []
    for raw_line in payload.splitlines():
        try:
            decoded = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, Mapping):
            records.append(dict(decoded))
    return tuple(records)


def proved_node_events(
    records: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[ProvedNodeEvent, ...]:
    """Return gate-backed ``node-status -> proved`` events in journal order."""
    events: list[ProvedNodeEvent] = []
    for record in records if records is not None else _journal_tail_records():
        if (
            str(record.get("event", "") or "") != "node-status"
            or str(record.get("to", "") or "") != "proved"
            or not bool(record.get("via_gate"))
        ):
            continue
        node_id = str(record.get("node_id", "") or "").strip()
        proved_at = _parse_timestamp(record.get("ts"))
        if node_id and proved_at is not None:
            events.append(ProvedNodeEvent(node_id=node_id, proved_at=proved_at))
    return tuple(sorted(events, key=lambda event: (event.proved_at, event.node_id)))


def historical_saturated_finite_branch_helpers(
    blueprint: plan_state.Blueprint,
    *,
    epoch_routes: Sequence[Mapping[str, Any]],
    referenced_node_ids: Iterable[str] = (),
    journal_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, SaturatedFiniteBranchAssessment]:
    """Replay promotions and return historically saturated persisted branches.

    Assess each same-timestamp batch against the prior proved set. This preserves
    the first four family branches as genuine historical progress while finding
    every later singleton/residue helper that an older runner falsely counted.
    Current-epoch promotions are always candidates. Persisted progress-anchor or
    mechanism-ledger references are candidates even when a rollover moved them
    outside the current route window. Missing or malformed ordering evidence
    fails open without reclassifying it.
    """
    referenced = {
        str(node_id or "").strip() for node_id in referenced_node_ids if str(node_id or "").strip()
    }
    route_times = [
        decided_at
        for route in epoch_routes
        if (decided_at := _parse_timestamp(route.get("decided_at"))) is not None
    ]
    if not route_times and not referenced:
        return {}
    epoch_started_at = min(route_times) if route_times else None
    current_proved = {node.id for node in blueprint.nodes if node.status == "proved"}
    ordered_events = [
        event for event in proved_node_events(journal_records) if event.node_id in current_proved
    ]
    if not ordered_events:
        return {}
    journaled_node_ids = {event.node_id for event in ordered_events}
    prior_node_ids = current_proved - journaled_node_ids
    assessments: dict[str, SaturatedFiniteBranchAssessment] = {}
    for proved_at, grouped in groupby(ordered_events, key=lambda event: event.proved_at):
        batch_ids = {event.node_id for event in grouped}
        candidate_ids = {
            node_id
            for node_id in batch_ids
            if node_id in referenced
            or (epoch_started_at is not None and proved_at >= epoch_started_at)
        }
        batch = assess_saturated_finite_branch_helpers(
            blueprint,
            candidate_ids,
            previously_proved_node_ids=prior_node_ids,
        )
        for node_id, assessment in batch.items():
            assessments.setdefault(node_id, assessment)
        prior_node_ids.update(batch_ids)
    return assessments
