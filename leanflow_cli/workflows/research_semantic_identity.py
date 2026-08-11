"""Derive provenance-insensitive identities for model-authored research shapes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_IGNORED_PROVENANCE_KEYS = frozenset(
    {
        "anchor_consumed",
        "compared_against",
        "consumption_key",
        "created_at",
        "finished_at",
        "finding_sha256",
        "job_id",
        "parent_job_id",
        "parent_route_context",
        "route_anchor_consumption_key",
        "route_anchor_finding_sha256",
        "route_anchor_job_id",
        "route_hash",
        "route_signature",
        "started_at",
        "timestamp",
        "ts",
        "updated_at",
    }
)
_DIRECT_SHAPE_KEYS = frozenset(
    {
        "certificate",
        "concrete_countermodel",
        "construction",
        "coverage_delta",
        "formula",
        "identity",
        "invariant",
        "method",
        "obstruction",
        "parameterization",
        "proof_shape",
        "shape",
        "strategy",
    }
)
_DESCRIPTION_PARENT_PARTS = ("counter", "dependency", "obstruction")
_JOB_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9][A-Za-z0-9_-]*\.)*" r"orchestrator\.(?:dc|ds|em|np|pv)-\d+\b",
    flags=re.IGNORECASE,
)
_SHORT_JOB_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(?:dc|ds|em|np|pv)-\d+\b", re.IGNORECASE)
_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    flags=re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    flags=re.IGNORECASE,
)
_HASH_RE = re.compile(
    r"\b(?=[0-9a-f]{16,64}\b)(?=[0-9a-f]*[a-f])[0-9a-f]{16,64}\b",
    flags=re.IGNORECASE,
)
_COUNTER_RE = re.compile(
    r"\b(?:attempt(?:_count)?|cycle|epoch|generation|iteration|job|nonce|refresh|"
    r"route(?:[-_\s]?set)?|worker)\s*(?:[:=#]\s*)?\d+\b",
    flags=re.IGNORECASE,
)
_VOLATILE_PLACEHOLDERS = frozenset(
    {"$counter", "$hash", "$job", "$timestamp", "$uuid", "at", "finding", "route", "source"}
)
_ROUTE_SEMANTIC_TARGET_KEYS = frozenset(
    {
        "counterexample_evidence_reason",
        "focus",
        "frontier",
        "graph_frontier",
        "helper",
        "helpers",
        "hypothesis",
        "lemma",
        "lemmas",
        "mathematical_target",
        "negation_refresh_evidence_key",
        "obstruction",
        "objective",
        "proof_shape",
        "proof_shapes",
        "probes",
        "research_proof_shapes",
        "route_objective",
        "semantic_route_focus",
        "statements_to_state",
        "strategy",
        "target_node",
        "target_hypothesis",
        "verified_counterexample_evidence",
    }
)
_ROUTE_EXACT_TARGET_KEYS = frozenset(
    {
        "frontier",
        "graph_frontier",
        "helper",
        "helpers",
        "lemma",
        "lemmas",
        "research_proof_shapes",
        "target_node",
        "verified_counterexample_evidence",
    }
)
_ROUTE_SYMBOLIC_TARGET_KEYS = frozenset(
    {
        "focus",
        "hypothesis",
        "mathematical_target",
        "objective",
        "obstruction",
        "route_objective",
        "target_hypothesis",
    }
)
_ROUTE_CONTENT_IDENTITY_KEYS = frozenset({"negation_refresh_evidence_key"})
_ROUTE_MECHANISM_PATTERNS = (
    ("algebraic-identity", re.compile(r"\b(?:algebra|factor|identity|polynomial|ring)\w*\b")),
    (
        "decomposition",
        re.compile(r"\b(?:decompos|helper|intermediate\s+lemma|split|sublemma)\w*\b"),
    ),
    (
        "finite-enumeration",
        re.compile(r"\b(?:bounded|case\s+split|enumerat|finite|witness)\w*\b"),
    ),
    ("induction", re.compile(r"\b(?:induct|recurr|strong\s+induction)\w*\b")),
    (
        "inequality",
        re.compile(r"\b(?:bound|convex|inequal|monot|nonnegative|order)\w*\b"),
    ),
    (
        "library-search",
        re.compile(r"\b(?:library|mathlib|retrieve|search|theorem\s+search)\w*\b"),
    ),
    (
        "modular-arithmetic",
        re.compile(r"\b(?:congru|modulo|modulus|residue|zmod)\w*\b"),
    ),
    (
        "negation",
        re.compile(r"\b(?:contradiction|counterexample|disprov|negat|obstruction)\w*\b"),
    ),
    (
        "number-theory",
        re.compile(r"\b(?:coprime|divid|divis|gcd|lcm|prime|valuation)\w*\b"),
    ),
)
_BACKTICKED_ATOM_RE = re.compile(r"`([^`\n]{1,120})`")
_MATHEMATICAL_NUMBER_RE = re.compile(r"(?<![$A-Za-z_])\d+(?![A-Za-z_])")
_SYMBOLIC_IDENTIFIER_RE = re.compile(r"(?<![$A-Za-z0-9_'])[a-z][a-z0-9_'.]*(?![A-Za-z0-9_'])")
_GREEK_IDENTIFIER_NAMES = frozenset(
    {
        "alpha",
        "beta",
        "delta",
        "epsilon",
        "eta",
        "gamma",
        "iota",
        "kappa",
        "lambda",
        "mu",
        "nu",
        "omega",
        "omicron",
        "phi",
        "pi",
        "psi",
        "rho",
        "sigma",
        "tau",
        "theta",
        "upsilon",
        "xi",
        "zeta",
    }
)


@dataclass(frozen=True)
class ProofShapeIdentities:
    """Hold deterministic proof-shape digests and malformed-input status."""

    values: tuple[str, ...] = ()
    malformed: bool = False


@dataclass(frozen=True)
class RouteSemanticIdentity:
    """Identify one foreground route by assignment and mathematical intent."""

    key: str
    family: str
    target_hypothesis: str
    proof_shapes: tuple[str, ...] = ()


def normalize_semantic_text(value: str) -> str:
    """Remove operational provenance while retaining mathematical constants and syntax."""
    text = str(value or "").casefold()
    text = _JOB_ID_RE.sub("$job", text)
    text = _SHORT_JOB_ID_RE.sub("$job", text)
    text = _ISO_TIMESTAMP_RE.sub("$timestamp", text)
    text = _UUID_RE.sub("$uuid", text)
    text = _HASH_RE.sub("$hash", text)
    text = _COUNTER_RE.sub("$counter", text)
    return " ".join(text.split())


def _canonical_value(value: Any) -> tuple[Any, bool]:
    """Canonicalize JSON-like semantics and reject unsupported model values."""
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        malformed = False
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                malformed = True
                continue
            key = raw_key.strip().casefold()
            if key in _IGNORED_PROVENANCE_KEYS:
                continue
            canonical, item_malformed = _canonical_value(item)
            malformed = malformed or item_malformed
            normalized[key] = canonical
        return normalized, malformed
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized_items: list[Any] = []
        malformed = False
        for item in value:
            canonical, item_malformed = _canonical_value(item)
            malformed = malformed or item_malformed
            normalized_items.append(canonical)
        return normalized_items, malformed
    if isinstance(value, str):
        return normalize_semantic_text(value), False
    if value is None or type(value) in {bool, int, float}:
        return value, False
    return None, True


def canonical_semantic_value(value: Any) -> tuple[str, bool]:
    """Return deterministic provenance-free JSON and whether input was malformed."""
    canonical, malformed = _canonical_value(value)
    if isinstance(canonical, str):
        rendered = canonical
    else:
        rendered = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return rendered, malformed


def _shape_field(path: tuple[str, ...], key: str) -> bool:
    """Return whether one scalar field claims a mathematical proof shape."""
    if key in _DIRECT_SHAPE_KEYS or key.endswith("_proof_shape"):
        return True
    return key == "description" and any(
        marker in component for component in path for marker in _DESCRIPTION_PARENT_PARTS
    )


def _has_substance(value: str) -> bool:
    """Return whether normalized text contains content beyond provenance placeholders."""
    tokens = set(re.findall(r"[a-z_][a-z0-9_]*|\d+", value))
    return bool(tokens.difference(_VOLATILE_PLACEHOLDERS))


def proof_shape_identities(deliverable: Any) -> ProofShapeIdentities:
    """Extract stable proof-shape identities from one untrusted deliverable.

    Job identifiers, route hashes, timestamps, and generation counters are
    observational provenance. They cannot make an otherwise repeated shape
    novel. Unsupported values in a shape-bearing field fail closed and mark
    the complete identity result malformed.
    """
    if not isinstance(deliverable, Mapping):
        return ProofShapeIdentities(malformed=deliverable is not None)

    canonical_shapes: set[str] = set()
    malformed = False

    def visit(value: Any, *, path: tuple[str, ...] = ()) -> None:
        nonlocal malformed
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    malformed = True
                    continue
                key = raw_key.strip().casefold()
                if key in _IGNORED_PROVENANCE_KEYS:
                    continue
                if _shape_field(path, key):
                    if not isinstance(item, (str, Mapping, list, tuple)):
                        malformed = True
                    canonical, item_malformed = canonical_semantic_value(item)
                    malformed = malformed or item_malformed
                    if canonical and _has_substance(canonical):
                        canonical_shapes.add(canonical)
                if isinstance(item, (Mapping, list, tuple)):
                    visit(item, path=(*path, key))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                if isinstance(item, (Mapping, list, tuple)):
                    visit(item, path=path)
                elif not isinstance(item, (str, int, float, bool)) and item is not None:
                    malformed = True

    visit(deliverable)
    identities = tuple(
        sorted(
            {hashlib.sha256(value.encode("utf-8")).hexdigest()[:20] for value in canonical_shapes}
        )
    )
    return ProofShapeIdentities(values=identities, malformed=malformed)


def _semantic_text_features(value: str) -> tuple[str, ...]:
    """Return coarse mathematical mechanisms and stable concrete atoms in text."""
    normalized = normalize_semantic_text(value)
    features = {
        family for family, pattern in _ROUTE_MECHANISM_PATTERNS if pattern.search(normalized)
    }
    features.update(
        f"atom:{normalize_semantic_text(match.group(1))}"
        for match in _BACKTICKED_ATOM_RE.finditer(normalized)
        if _has_substance(normalize_semantic_text(match.group(1)))
    )
    features.update(f"constant:{value}" for value in _MATHEMATICAL_NUMBER_RE.findall(normalized))
    return tuple(sorted(features))


def _symbolic_target_features(value: str) -> tuple[str, ...]:
    """Return stable mathematical symbols from one concrete target description.

    Ordinary prose stays represented by the coarse mechanism classifier, so
    rewording cannot manufacture novelty. Single-letter variables, Greek-style
    identifiers, qualified/underscored Lean names, and alphanumeric symbols are
    retained because changing them can identify a genuinely different
    hypothesis even when the surrounding strategy prose is unchanged.
    """
    normalized = normalize_semantic_text(value)
    identifiers: set[str] = set()
    for token in _SYMBOLIC_IDENTIFIER_RE.findall(normalized):
        if token in _VOLATILE_PLACEHOLDERS:
            continue
        if (
            (len(token) == 1 and token not in {"a", "i"})
            or token in _GREEK_IDENTIFIER_NAMES
            or any(marker in token for marker in ("_", ".", "'"))
            or any(character.isdigit() for character in token)
        ):
            identifiers.add(token)
    return tuple(sorted(identifiers))


def _route_target_semantics(target: Mapping[str, Any]) -> dict[str, Any]:
    """Return explicit hypothesis-bearing route target fields without provenance."""
    selected: dict[str, Any] = {}

    def semantic_value(value: Any, *, key: str) -> Any:
        """Collapse prose to mechanisms while retaining exact mathematical anchors."""
        if key in _ROUTE_CONTENT_IDENTITY_KEYS:
            return str(value or "").strip().casefold()
        if key in _ROUTE_EXACT_TARGET_KEYS:
            canonical, malformed = _canonical_value(value)
            return None if malformed else canonical
        if isinstance(value, str):
            features = _semantic_text_features(value)
            symbols = (
                _symbolic_target_features(value)
                if key in _ROUTE_SYMBOLIC_TARGET_KEYS
                or key.endswith("_hypothesis")
                or key.endswith("_objective")
                else ()
            )
            if symbols:
                return {
                    "mechanisms": list(features),
                    "symbols": list(symbols),
                }
            return list(features) if features else ["unclassified-mathematical-text"]
        if isinstance(value, Mapping):
            return {
                str(raw_key)
                .strip()
                .casefold(): semantic_value(
                    item,
                    key=str(raw_key).strip().casefold(),
                )
                for raw_key, item in value.items()
                if isinstance(raw_key, str)
                and str(raw_key).strip().casefold() not in _IGNORED_PROVENANCE_KEYS
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [semantic_value(item, key=key) for item in value]
        if value is None or type(value) in {bool, int, float}:
            return value
        return None

    def visit(value: Any, *, path: tuple[str, ...] = ()) -> None:
        if not isinstance(value, Mapping):
            return
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip().casefold()
            if key in _IGNORED_PROVENANCE_KEYS:
                continue
            child_path = (*path, key)
            if (
                key in _ROUTE_SEMANTIC_TARGET_KEYS
                or key.endswith("_hypothesis")
                or key.endswith("_objective")
                or key.endswith("_proof_shape")
            ):
                semantic = semantic_value(item, key=key)
                canonical = json.dumps(
                    semantic,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if semantic is not None and canonical and _has_substance(canonical):
                    selected[".".join(child_path)] = semantic
            if isinstance(item, Mapping):
                visit(item, path=child_path)

    visit(target)
    return selected


def route_semantic_identity(
    *,
    route: str,
    target_symbol: str,
    active_file: str,
    reason: str = "",
    target: Mapping[str, Any] | None = None,
) -> RouteSemanticIdentity:
    """Return a provenance-insensitive identity for one foreground route.

    Route labels establish a strategy family, while explicit target
    hypotheses, mathematical mechanism features, and proof-shape identities
    establish the work inside that family. Operational prose, counters, job
    ids, timestamps, and route hashes cannot manufacture novelty.
    """
    family = str(route or "unknown").strip().casefold() or "unknown"
    target_map = dict(target or {})
    proof_shapes = proof_shape_identities(target_map).values
    semantic_target = _route_target_semantics(target_map)
    request_reason = str(target_map.get("prover_request_reason", "") or "")
    features = tuple(
        sorted(
            {
                *_semantic_text_features(request_reason),
            }
        )
    )
    hypothesis_payload = {
        "features": features,
        "target": semantic_target,
    }
    if not features and not proof_shapes and not semantic_target:
        target_hypothesis = "assignment-root"
    else:
        rendered_hypothesis = json.dumps(
            hypothesis_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        target_hypothesis = hashlib.sha256(rendered_hypothesis.encode("utf-8")).hexdigest()[:20]
    normalized_file = os.path.realpath(active_file) if active_file else ""
    payload = "\x1f".join(
        (
            family,
            str(target_symbol or "").strip(),
            normalized_file,
            target_hypothesis,
        )
    )
    return RouteSemanticIdentity(
        key=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20],
        family=family,
        target_hypothesis=target_hypothesis,
        proof_shapes=proof_shapes,
    )


def route_record_semantic_identity(record: Mapping[str, Any]) -> RouteSemanticIdentity:
    """Return the stored or reconstructed semantic identity of a route record."""
    identity = route_semantic_identity(
        route=str(record.get("route", "") or ""),
        target_symbol=str(record.get("target_symbol", "") or ""),
        active_file=str(record.get("active_file", "") or ""),
        reason=str(record.get("reason", "") or ""),
        target=(
            dict(record.get("target") or {}) if isinstance(record.get("target"), Mapping) else {}
        ),
    )
    stored = str(record.get("semantic_route_key", "") or "").strip()
    if not stored:
        return identity
    return RouteSemanticIdentity(
        key=stored,
        family=str(record.get("semantic_route_family", "") or identity.family),
        target_hypothesis=str(
            record.get("semantic_target_hypothesis", "") or identity.target_hypothesis
        ),
        proof_shapes=tuple(
            str(value)
            for value in (record.get("semantic_proof_shapes") or identity.proof_shapes)
            if str(value).strip()
        ),
    )
