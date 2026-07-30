"""Classify helper decompositions that only move their parent's obligation."""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)

DECOMPOSITION_ADMISSION_PROMPT_CONTRACT = (
    "Decomposition admission contract: a helper for a universal or parameterized parent "
    "must not be a closed literal instance of the parent conclusion. Preserve the parent "
    "parameter in a reusable residue/helper declaration, or propose a genuinely distinct "
    "structural lemma. A finite base case is useful only when it states a distinct structural "
    "fact; merely substituting one numeral into the full parent conclusion moves the `sorry` "
    "and is rejected deterministically. A same-premise existential certificate must expose "
    "a strictly smaller obligation than the parent: do not replace the parent's witnesses "
    "with an equally broad or larger bundle of existential witnesses and atomic constraints. "
    "Instead isolate the missing divisor, witness, or coverage fact as the narrower helper. "
    "Never drop a side condition that makes a proposed helper sound. Before proposing each "
    "helper, audit every parent hypothesis that the helper omits: try the smallest boundary "
    "counterexample with that hypothesis removed, and retain the hypothesis whenever the "
    "conclusion is not independently true. Reachability, positivity, nonemptiness, and invariant "
    "helpers must carry the initial-condition hypotheses they use; being reachable through a "
    "`ValidSeq`-style relation does not imply properties that were assumed only of its initial "
    "state. Put the retained assumptions in the Lean skeleton itself, not only in proof hints. "
    "In particular, a helper "
    "relating `playEnds` to `physicalPieces` must retain the `Disjoint` hypothesis required to "
    "prevent overlapping cut points from changing the physical-piece cardinality. "
)

_SLICE_HEADER_RE = re.compile(r"^Assigned declaration slice[^\n]*:\s*\n", re.IGNORECASE)
_LEAN_NAME = r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*"
_DECLARATION_HEAD_RE = re.compile(
    rf"^\s*(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+)\s+)*"
    rf"(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
    rf"(?:theorem|lemma)\s+(?P<name>{_LEAN_NAME})\b",
    flags=re.DOTALL,
)
_IDENTIFIER_RE = re.compile(rf"^{_LEAN_NAME}$")
_TOKEN_RE = re.compile(
    rf"{_LEAN_NAME}|\d+|<->|->|=>|:=|<=|>=|\S",
    flags=re.UNICODE,
)
_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
_GROUPING_TOKENS = frozenset({"(", ")"})
_CANONICAL_TOKENS = {
    "Nat": "ℕ",
    "Rat": "ℚ",
    "->": "→",
    "=>": "⇒",
    "<=": "≤",
    ">=": "≥",
}
_NEAR_IDENTICAL_SIMILARITY = 0.96
_REASON_CODE = "closed_literal_parent_instantiation"
_DROPPED_DISJOINTNESS_REASON_CODE = "dropped_required_disjointness"
_NONREDUCING_WRAPPER_REASON_CODE = "nonreducing_existential_wrapper"
_EXISTENTIAL_BINDER_RE = re.compile(r"(?:∃|\bexists\b)\s+([^,:]+?)\s*:", re.IGNORECASE)
_LOGICAL_CONNECTIVES = frozenset({"∧", "∨"})
_RELATION_TOKENS = frozenset({"=", "≠", "<", ">", "≤", "≥", "∣"})


@dataclass(frozen=True)
class HelperAdmissionAssessment:
    """Describe deterministic admission of one proposed decomposition helper."""

    accepted: bool
    reason_code: str = ""
    reason: str = ""
    instantiated_parameters: tuple[tuple[str, str], ...] = ()
    conclusion_similarity: float = 0.0
    parent_conclusion_sha256: str = ""
    helper_conclusion_sha256: str = ""

    def journal_fields(self) -> dict[str, object]:
        """Return bounded, source-free rejection telemetry for the workflow journal."""
        return {
            "reason_code": self.reason_code,
            "instantiated_parameters": [
                {"name": name, "literal": literal} for name, literal in self.instantiated_parameters
            ],
            "conclusion_similarity": round(self.conclusion_similarity, 4),
            "parent_conclusion_sha256": self.parent_conclusion_sha256,
            "helper_conclusion_sha256": self.helper_conclusion_sha256,
        }


@dataclass(frozen=True)
class ObligationProfile:
    """Summarize visible existential and atomic burden in one result type."""

    existential_variables: int = 0
    logical_atoms: int = 0
    relation_atoms: int = 0

    def to_mapping(self) -> dict[str, int]:
        """Return the bounded profile used by graph-progress telemetry."""
        return {
            "existential_variables": self.existential_variables,
            "logical_atoms": self.logical_atoms,
            "relation_atoms": self.relation_atoms,
        }


@dataclass(frozen=True)
class ObligationReductionAssessment:
    """Describe whether a helper visibly reduces its parent's proof obligation."""

    reducing: bool = True
    reason_code: str = ""
    parent_profile: ObligationProfile = ObligationProfile()
    helper_profile: ObligationProfile = ObligationProfile()

    @property
    def nonreducing_wrapper(self) -> bool:
        """Return whether accounting must defer this same-premise wrapper."""
        return not self.reducing and self.reason_code == _NONREDUCING_WRAPPER_REASON_CODE


@dataclass(frozen=True)
class _DeclarationParts:
    """Hold the binder header and conclusion of one parsed declaration signature."""

    binder_groups: tuple[str, ...]
    conclusion: str


def _normalized_binder_groups(parts: _DeclarationParts) -> tuple[tuple[str, ...], ...]:
    """Return canonical binder tokens for exact same-premise comparison."""
    return tuple(_tokens(group, relaxed=True) for group in parts.binder_groups)


def _existential_variable_count(conclusion: str) -> int:
    """Count explicit names in ordinary ``∃ x y : T`` binder groups."""
    count = 0
    for match in _EXISTENTIAL_BINDER_RE.finditer(conclusion):
        names = tuple(
            token for token in _TOKEN_RE.findall(match.group(1)) if _IDENTIFIER_RE.fullmatch(token)
        )
        if not names:
            return 0
        count += len(names)
    return count


def _obligation_profile(conclusion: str) -> ObligationProfile:
    """Return a conservative surface profile for one existential proposition."""
    tokens = _tokens(conclusion)
    connective_count = sum(token in _LOGICAL_CONNECTIVES for token in tokens)
    relation_count = sum(token in _RELATION_TOKENS for token in tokens)
    return ObligationProfile(
        existential_variables=_existential_variable_count(conclusion),
        logical_atoms=(connective_count + 1 if tokens else 0),
        relation_atoms=relation_count,
    )


def _signature_text(text: str) -> str:
    """Return comment-free declaration text before its proof assignment."""
    raw = _SLICE_HEADER_RE.sub("", str(text or "").strip(), count=1).strip()
    sanitized = _strip_lean_comments_and_strings(raw).strip()
    assignment = _find_assignment_marker_for_statement(sanitized)
    if assignment >= 0:
        return sanitized[:assignment].rstrip()
    return sanitized


def _declaration_parts(text: str) -> _DeclarationParts | None:
    """Parse top-level binder groups and the result conclusion from one signature."""
    signature = _signature_text(text)
    head = _DECLARATION_HEAD_RE.match(signature)
    if head is None:
        return None
    groups: list[str] = []
    stack: list[tuple[str, int]] = []
    result_colon = -1
    for index in range(head.end(), len(signature)):
        token = signature[index]
        if token in _OPEN_TO_CLOSE:
            stack.append((token, index))
            continue
        if stack and token == _OPEN_TO_CLOSE[stack[-1][0]]:
            _, start = stack.pop()
            if not stack:
                groups.append(signature[start + 1 : index])
            continue
        if token == ":" and not stack:
            result_colon = index
            break
    if result_colon < 0:
        return None
    conclusion = signature[result_colon + 1 :].strip()
    if not conclusion:
        return None
    return _DeclarationParts(binder_groups=tuple(groups), conclusion=conclusion)


def _nat_binder_names(parts: _DeclarationParts) -> tuple[str, ...]:
    """Return explicit names whose binder type is exactly ``Nat`` or ``ℕ``."""
    names: list[str] = []
    for group in parts.binder_groups:
        if ":" not in group:
            continue
        left, right = group.split(":", 1)
        binder_type = right.split(":=", 1)[0].strip()
        while binder_type.startswith("(") and binder_type.endswith(")"):
            binder_type = binder_type[1:-1].strip()
        if binder_type not in {"Nat", "ℕ"}:
            continue
        candidates = tuple(part for part in left.split() if part)
        if candidates and all(_IDENTIFIER_RE.fullmatch(candidate) for candidate in candidates):
            names.extend(candidates)
    return tuple(dict.fromkeys(names))


def _tokens(conclusion: str, *, relaxed: bool = False) -> tuple[str, ...]:
    """Return canonical syntax tokens for one Lean conclusion."""
    canonical = tuple(
        _CANONICAL_TOKENS.get(token, token) for token in _TOKEN_RE.findall(conclusion)
    )
    if relaxed:
        return tuple(token for token in canonical if token not in _GROUPING_TOKENS)
    return canonical


def _identifier_tokens(tokens: tuple[str, ...]) -> frozenset[str]:
    """Return identifier tokens while excluding numerals and punctuation."""
    return frozenset(token for token in tokens if _IDENTIFIER_RE.fullmatch(token))


def _sha256(text: str) -> str:
    """Return a stable digest for bounded admission telemetry."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assess_obligation_reduction(
    parent_statement: str,
    helper_statement: str,
) -> ObligationReductionAssessment:
    """Defer a same-premise existential wrapper with no visible reduction.

    This is deliberately an accounting classifier, not a source-admission
    verdict.  Logical equivalence is not decidable from Lean surface syntax,
    so the check fails open unless both signatures parse, their explicit
    premises match exactly, and both expose an ordinary existential result.
    A child is non-reducing only when it retains at least the parent's witness,
    connective, and relation burden.  Kernel validity is preserved; callers
    merely withhold campaign-progress credit until the parent uses the helper.
    """
    parent = _declaration_parts(parent_statement)
    helper = _declaration_parts(helper_statement)
    if parent is None or helper is None:
        return ObligationReductionAssessment()
    if _normalized_binder_groups(parent) != _normalized_binder_groups(helper):
        return ObligationReductionAssessment()

    parent_profile = _obligation_profile(parent.conclusion)
    helper_profile = _obligation_profile(helper.conclusion)
    if (
        parent_profile.existential_variables <= 0
        or helper_profile.existential_variables <= 0
        or max(parent_profile.logical_atoms, parent_profile.relation_atoms) < 2
    ):
        return ObligationReductionAssessment(
            parent_profile=parent_profile,
            helper_profile=helper_profile,
        )
    nonreducing = (
        helper_profile.existential_variables >= parent_profile.existential_variables
        and helper_profile.logical_atoms >= parent_profile.logical_atoms
        and helper_profile.relation_atoms >= parent_profile.relation_atoms
    )
    return ObligationReductionAssessment(
        reducing=not nonreducing,
        reason_code=_NONREDUCING_WRAPPER_REASON_CODE if nonreducing else "",
        parent_profile=parent_profile,
        helper_profile=helper_profile,
    )


def bounded_journal_fields(value: object) -> dict[str, object]:
    """Return only bounded admission fields from a serialized advisor result."""
    raw = value if isinstance(value, Mapping) else {}
    reason_code = str(raw.get("reason_code", "") or "")
    if reason_code not in {_REASON_CODE, _DROPPED_DISJOINTNESS_REASON_CODE}:
        reason_code = _REASON_CODE
    parameters: list[dict[str, str]] = []
    raw_parameters = raw.get("instantiated_parameters", [])
    if isinstance(raw_parameters, Sequence) and not isinstance(
        raw_parameters,
        (str, bytes),
    ):
        for item in raw_parameters[:4]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name", "") or "")
            literal = str(item.get("literal", "") or "")
            if _IDENTIFIER_RE.fullmatch(name) and literal.isdecimal() and len(literal) <= 40:
                parameters.append({"name": name, "literal": literal})
    fields: dict[str, object] = {
        "reason_code": reason_code,
        "instantiated_parameters": parameters,
    }
    similarity: Any = raw.get("conclusion_similarity")
    if isinstance(similarity, (int, float)) and not isinstance(similarity, bool):
        fields["conclusion_similarity"] = round(min(1.0, max(0.0, float(similarity))), 4)
    for key in ("parent_conclusion_sha256", "helper_conclusion_sha256"):
        digest = str(raw.get(key, "") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            fields[key] = digest
    return fields


def assess_helper_admission(
    parent_statement: str,
    helper_skeleton: str,
) -> HelperAdmissionAssessment:
    """Reject known unsound or non-reducing decomposition shapes.

    The side-condition guard catches the concrete unsafe bridge from
    ``playEnds`` to ``physicalPieces`` when ``Disjoint`` was discarded. The
    remaining check fails open unless both declarations have an unambiguous
    shape and exactly one explicit natural-number parent parameter occurs in
    the result. This keeps structural decompositions admissible.
    """
    parent = _declaration_parts(parent_statement)
    helper = _declaration_parts(helper_skeleton)
    if parent is None or helper is None:
        return HelperAdmissionAssessment(accepted=True)

    parent_tokens = _tokens(parent.conclusion)
    helper_tokens = _tokens(helper.conclusion)
    helper_signature_tokens = _tokens(_signature_text(helper_skeleton))
    uses_play_ends = any(
        token == "playEnds" or token.endswith(".playEnds") for token in helper_signature_tokens
    )
    if (
        "Disjoint" in parent_tokens
        and "physicalPieces" in parent_tokens
        and "physicalPieces" in helper_tokens
        and uses_play_ends
        and "Disjoint" not in helper_signature_tokens
    ):
        return HelperAdmissionAssessment(
            accepted=False,
            reason_code=_DROPPED_DISJOINTNESS_REASON_CODE,
            reason=(
                "helper drops the Disjoint side condition required when deriving "
                "physicalPieces from playEnds; overlapping cut points can change the "
                "physical-piece cardinality"
            ),
            parent_conclusion_sha256=_sha256(" ".join(parent_tokens)),
            helper_conclusion_sha256=_sha256(" ".join(helper_tokens)),
        )

    parent_nat_names = _nat_binder_names(parent)
    relevant_parameters = tuple(name for name in parent_nat_names if name in parent_tokens)
    if len(relevant_parameters) != 1:
        return HelperAdmissionAssessment(accepted=True)
    parameter = relevant_parameters[0]
    if parameter in helper_tokens:
        return HelperAdmissionAssessment(accepted=True)

    # An explicitly parameterized helper may alpha-rename the parent's index.
    # Its conclusion is reusable and therefore is not a closed literal case.
    helper_nat_names = _nat_binder_names(helper)
    if any(name in helper_tokens for name in helper_nat_names):
        return HelperAdmissionAssessment(accepted=True)

    parent_identifiers = _identifier_tokens(parent_tokens) - {parameter}
    helper_identifiers = _identifier_tokens(helper_tokens)
    if not helper_identifiers.issubset(parent_identifiers):
        return HelperAdmissionAssessment(accepted=True)

    literal_candidates = tuple(dict.fromkeys(token for token in helper_tokens if token.isdecimal()))
    if not literal_candidates:
        return HelperAdmissionAssessment(accepted=True)

    best_literal = ""
    best_similarity = 0.0
    relaxed_helper = _tokens(helper.conclusion, relaxed=True)
    for literal in literal_candidates:
        instantiated = tuple(literal if token == parameter else token for token in parent_tokens)
        if instantiated == helper_tokens:
            best_literal = literal
            best_similarity = 1.0
            break
        relaxed_parent = tuple(token for token in instantiated if token not in _GROUPING_TOKENS)
        similarity = difflib.SequenceMatcher(
            None,
            relaxed_parent,
            relaxed_helper,
            autojunk=False,
        ).ratio()
        if similarity > best_similarity:
            best_literal = literal
            best_similarity = similarity

    if best_similarity < _NEAR_IDENTICAL_SIMILARITY:
        return HelperAdmissionAssessment(accepted=True)

    parent_hash = _sha256(" ".join(parent_tokens))
    helper_hash = _sha256(" ".join(helper_tokens))
    return HelperAdmissionAssessment(
        accepted=False,
        reason_code=_REASON_CODE,
        reason=(
            "helper conclusion is only a closed literal instance of the universal parent; "
            "state a reusable parameterized residue lemma or a distinct structural base fact"
        ),
        instantiated_parameters=((parameter, best_literal),),
        conclusion_similarity=best_similarity,
        parent_conclusion_sha256=parent_hash,
        helper_conclusion_sha256=helper_hash,
    )
