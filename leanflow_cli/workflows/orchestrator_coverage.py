"""Reject orchestrator routes that misuse or repeat durable graph evidence.

The LLM router is advisory.  This module gives it a deterministic novelty
guard over kernel-proved graph facts and failed proof-shape signatures.  The
guard is deliberately conservative: it rejects exact repeats and arithmetic
progression subfamilies whose Lean conclusion is already covered, and leaves
ambiguous semantic comparisons to the next checked proving turn.  It also
prevents campaign-global or statement-incompatible graph nodes from being
presented as dependencies of the current assignment.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Affine:
    """Represent one non-negative single-variable affine expression."""

    variable: str
    coefficient: int
    constant: int


@dataclass(frozen=True)
class _DenominatorFamily:
    """Describe the progression and normalized conclusion around a denominator."""

    affine: _Affine
    conclusion_template: str
    residue_modulus: int = 0
    allowed_residues: frozenset[int] = frozenset()


EXACT_TARGET_CONCLUSION = "exact-target-conclusion"
DIFFERENT_TARGET_CONCLUSION = "different-target-conclusion"
UNVERIFIED_TARGET_CONCLUSION = "unverified-target-conclusion"


_TOKEN_RE = re.compile(r"\s*(?:(\d+)|([A-Za-z_][A-Za-z0-9_']*)|(.))", re.DOTALL)
_DOUBLE_CAST_DENOM_RE = re.compile(
    r"/\s*\(\(\s*(?P<expr>.*?)\s*:\s*ℕ\s*\)\s*:\s*ℚ\s*\)",
    re.DOTALL,
)
_DIRECT_CAST_DENOM_RE = re.compile(
    r"/\s*\(\s*(?P<expr>[^:()]+(?:\([^:]*\)[^:]*)?)\s*:\s*ℚ\s*\)",
    re.DOTALL,
)


class _AffineParser:
    """Parse the small linear-arithmetic fragment used in route statements."""

    def __init__(self, text: str) -> None:
        self.tokens: list[tuple[str, str]] = []
        for match in _TOKEN_RE.finditer(text):
            number, identifier, symbol = match.groups()
            if number is not None:
                self.tokens.append(("number", number))
            elif identifier is not None:
                self.tokens.append(("identifier", identifier))
            elif symbol and not symbol.isspace():
                self.tokens.append((symbol, symbol))
        self.index = 0

    def parse(self) -> tuple[dict[str, int], int] | None:
        """Return linear coefficients and a constant, or ``None`` on doubt."""
        value = self._expression()
        if value is None or self.index != len(self.tokens):
            return None
        return value

    def _peek(self, kind: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index][0] == kind

    def _take(self) -> tuple[str, str]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _expression(self) -> tuple[dict[str, int], int] | None:
        value = self._term()
        if value is None:
            return None
        while self._peek("+") or self._peek("-"):
            operator, _ = self._take()
            right = self._term()
            if right is None:
                return None
            sign = 1 if operator == "+" else -1
            value = _linear_add(value, right, sign=sign)
        return value

    def _term(self) -> tuple[dict[str, int], int] | None:
        value = self._atom()
        if value is None:
            return None
        while self._peek("*"):
            self._take()
            right = self._atom()
            if right is None:
                return None
            value = _linear_multiply(value, right)
            if value is None:
                return None
        return value

    def _atom(self) -> tuple[dict[str, int], int] | None:
        if self._peek("number"):
            _kind, value = self._take()
            return {}, int(value)
        if self._peek("identifier"):
            _kind, value = self._take()
            return {value: 1}, 0
        if self._peek("("):
            self._take()
            parsed_value = self._expression()
            if parsed_value is None or not self._peek(")"):
                return None
            self._take()
            return parsed_value
        return None


def _linear_add(
    left: tuple[dict[str, int], int],
    right: tuple[dict[str, int], int],
    *,
    sign: int,
) -> tuple[dict[str, int], int]:
    coefficients = dict(left[0])
    for name, coefficient in right[0].items():
        coefficients[name] = coefficients.get(name, 0) + sign * coefficient
        if coefficients[name] == 0:
            coefficients.pop(name)
    return coefficients, left[1] + sign * right[1]


def _linear_multiply(
    left: tuple[dict[str, int], int], right: tuple[dict[str, int], int]
) -> tuple[dict[str, int], int] | None:
    if left[0] and right[0]:
        return None
    if left[0]:
        scale = right[1]
        return (
            {name: coefficient * scale for name, coefficient in left[0].items()},
            left[1] * scale,
        )
    if right[0]:
        scale = left[1]
        return (
            {name: coefficient * scale for name, coefficient in right[0].items()},
            right[1] * scale,
        )
    return {}, left[1] * right[1]


def _parse_affine(text: str) -> _Affine | None:
    parsed = _AffineParser(text).parse()
    if parsed is None:
        return None
    coefficients, constant = parsed
    coefficients = {name: value for name, value in coefficients.items() if value}
    if len(coefficients) != 1:
        return None
    variable, coefficient = next(iter(coefficients.items()))
    if coefficient <= 0 or constant < 0:
        return None
    return _Affine(variable=variable, coefficient=coefficient, constant=constant)


def _normalize_statement(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _declaration_signature(text: str) -> str:
    """Return an exact-statement signature that ignores only the declaration name."""
    stripped = _strip_proof(str(text or ""))
    matches = list(re.finditer(r"\b(?:private\s+)?(?:lemma|theorem)\s+[^\s(:]+", stripped))
    if matches:
        match = matches[-1]
        stripped = stripped[match.end() :]
    return _normalize_statement(stripped)


def _strip_proof(text: str) -> str:
    return re.split(r":=\s*by\b|\bby\s+sorry\b", text, maxsplit=1, flags=re.DOTALL)[0]


def _residue_constraints(prefix: str, variable: str) -> tuple[int, frozenset[int]] | None:
    pattern = re.compile(rf"\b{re.escape(variable)}\s*%\s*(\d+)\s*=\s*(\d+)")
    matches = list(pattern.finditer(prefix))
    if not matches:
        return 0, frozenset()
    moduli = {int(match.group(1)) for match in matches}
    if len(moduli) != 1 or 0 in moduli:
        return None
    modulus = next(iter(moduli))
    residues = frozenset(int(match.group(2)) % modulus for match in matches)

    # Multiple alternatives are supported only when they are disjoined.  A
    # conjunction can encode a stronger condition and needs a theorem prover,
    # so the deterministic guard declines to compare it.
    if len(matches) > 1:
        between = "".join(
            prefix[left.end() : right.start()] for left, right in zip(matches, matches[1:])
        )
        if "∧" in between or re.search(r"\band\b", between, flags=re.IGNORECASE):
            return None

    # The verified theorem may not carry any unmodelled assumption.  Reduce a
    # declaration header, the progression binder, and residue-only hypothesis
    # binders; anything else needs a theorem prover and therefore fails open.
    reduced = prefix
    declaration_headers = list(
        re.finditer(r"\b(?:private\s+)?(?:lemma|theorem)\s+[^\s(:]+", reduced)
    )
    if declaration_headers:
        reduced = reduced[declaration_headers[-1].end() :]
    reduced = re.sub(rf"\(\s*{re.escape(variable)}\s*:\s*(?:ℕ|Nat)\s*\)", "", reduced)
    reduced = pattern.sub("$RESIDUE", reduced)
    residue = re.escape("$RESIDUE")
    reduced = re.sub(
        rf"\(\s*[A-Za-z_][A-Za-z0-9_']*\s*:\s*{residue}" rf"(?:\s*(?:∨|\bor\b)\s*{residue})*\s*\)",
        "",
        reduced,
        flags=re.IGNORECASE,
    )
    if re.sub(r"[\s:()]+", "", reduced):
        return None
    return modulus, residues


def _denominator_family(statement: str) -> _DenominatorFamily | None:
    text = _strip_proof(str(statement or ""))
    match = _DOUBLE_CAST_DENOM_RE.search(text) or _DIRECT_CAST_DENOM_RE.search(text)
    if match is None:
        return None
    affine = _parse_affine(match.group("expr"))
    if affine is None:
        return None
    conclusion_start = min(
        (position for marker in ("∃", "∀") if (position := text.find(marker)) >= 0),
        default=-1,
    )
    if conclusion_start < 0 or match.start("expr") < conclusion_start:
        return None
    prefix = text[:conclusion_start]
    constraints = _residue_constraints(prefix, affine.variable)
    if constraints is None:
        return None
    modulus, residues = constraints
    conclusion = text[conclusion_start:]
    relative_start = match.start("expr") - conclusion_start
    relative_end = match.end("expr") - conclusion_start
    conclusion = conclusion[:relative_start] + "$AFFINE" + conclusion[relative_end:]
    conclusion = re.sub(rf"\b{re.escape(affine.variable)}\b", "$PARAM", conclusion)
    template = re.sub(r"\s+", "", conclusion)
    return _DenominatorFamily(
        affine=affine,
        conclusion_template=template,
        residue_modulus=modulus,
        allowed_residues=residues,
    )


def _denominator_conclusion_shape(statement: str) -> tuple[_Affine, str] | None:
    """Return a denominator and conclusion template without trusting hypotheses."""
    text = _strip_proof(str(statement or ""))
    match = _DOUBLE_CAST_DENOM_RE.search(text) or _DIRECT_CAST_DENOM_RE.search(text)
    if match is None:
        return None
    affine = _parse_affine(match.group("expr"))
    if affine is None:
        return None
    conclusion_start = min(
        (position for marker in ("∃", "∀") if (position := text.find(marker)) >= 0),
        default=-1,
    )
    if conclusion_start < 0 or match.start("expr") < conclusion_start:
        return None
    conclusion = text[conclusion_start:]
    relative_start = match.start("expr") - conclusion_start
    relative_end = match.end("expr") - conclusion_start
    conclusion = conclusion[:relative_start] + "$AFFINE" + conclusion[relative_end:]
    conclusion = re.sub(rf"\b{re.escape(affine.variable)}\b", "$PARAM", conclusion)
    return affine, re.sub(r"\s+", "", conclusion)


def statement_shape_compatibility(target_statement: str, fact_statement: str) -> str:
    """Classify only exact, deterministic conclusion-shape evidence.

    The comparison is intentionally one-sided: exact declaration signatures
    and identical arithmetic denominator shapes are reusable.  A parsed but
    different denominator is known-incompatible.  Everything else remains
    unverified rather than inviting semantic guesses from theorem names.
    """
    target_signature = _declaration_signature(target_statement)
    fact_signature = _declaration_signature(fact_statement)
    if target_signature and fact_signature and target_signature == fact_signature:
        return EXACT_TARGET_CONCLUSION

    target_shape = _denominator_conclusion_shape(target_statement)
    fact_shape = _denominator_conclusion_shape(fact_statement)
    if target_shape is None or fact_shape is None:
        return UNVERIFIED_TARGET_CONCLUSION
    target_affine, target_template = target_shape
    fact_affine, fact_template = fact_shape
    if target_template != fact_template:
        return DIFFERENT_TARGET_CONCLUSION
    if (
        target_affine.coefficient == fact_affine.coefficient
        and target_affine.constant == fact_affine.constant
    ):
        return EXACT_TARGET_CONCLUSION
    return DIFFERENT_TARGET_CONCLUSION


def _affine_subfamily_covered(candidate: str, verified: str) -> bool:
    candidate_family = _denominator_family(candidate)
    verified_family = _denominator_family(verified)
    if candidate_family is None or verified_family is None:
        return False
    if candidate_family.conclusion_template != verified_family.conclusion_template:
        return False

    existing = verified_family.affine
    proposed = candidate_family.affine
    coefficient_delta, constant_delta = proposed.coefficient, proposed.constant - existing.constant
    if coefficient_delta % existing.coefficient or constant_delta % existing.coefficient:
        return False
    multiplier = coefficient_delta // existing.coefficient
    offset = constant_delta // existing.coefficient
    if multiplier < 0 or offset < 0:
        return False
    modulus = verified_family.residue_modulus
    if not modulus:
        return True
    orbit_size = modulus // math.gcd(multiplier, modulus)
    residues = {(multiplier * index + offset) % modulus for index in range(orbit_size)}
    return residues.issubset(verified_family.allowed_residues)


def _decision_texts(decision: Mapping[str, Any]) -> tuple[str, ...]:
    texts = [
        str(decision.get("reason", "") or ""),
        str(decision.get("target_node", "") or ""),
    ]
    for entry in decision.get("statements_to_state") or []:
        if isinstance(entry, Mapping):
            texts.extend((str(entry.get("name", "") or ""), str(entry.get("statement", "") or "")))
    for entry in decision.get("probes") or []:
        if isinstance(entry, Mapping):
            texts.append(str(entry.get("objective", "") or ""))
    return tuple(text for text in texts if text.strip())


def _structured_node_references(decision: Mapping[str, Any]) -> tuple[str, ...]:
    """Return graph-node identities the decision asks the runner to act on.

    Free-form rationale and probe objectives may discuss any proved theorem as
    mathematical context.  They are not graph mutations.  ``target_node`` and
    proposed declaration names are different: the runner may route to or state
    them, so those identities must stay within the active assignment instead
    of silently relabelling an unrelated graph node.
    """
    references = [str(decision.get("target_node", "") or "").strip()]
    for entry in decision.get("statements_to_state") or []:
        if isinstance(entry, Mapping):
            references.append(str(entry.get("name", "") or "").strip())
    return tuple(dict.fromkeys(reference for reference in references if reference))


def unsupported_graph_reference_reason(
    decision: Mapping[str, Any],
    *,
    expected_target_symbol: str = "",
    verified_graph_facts: Sequence[Mapping[str, Any]] = (),
    unrelated_frontier: Sequence[str] = (),
) -> str:
    """Return why a structured route identity escapes the active assignment.

    A proved helper normally has a different conclusion from its parent.  Its
    name may therefore appear in rationale, a candidate proof body, or a probe
    without claiming that the helper *is* the target or already closes it.
    Applicability remains a Lean elaboration question.  Reject only identities
    the response asks the runner to act on: a mismatched ``target_node`` or a
    proposed declaration that reuses an incompatible/unrelated graph node.
    Duplicate mathematical coverage is handled separately by
    :func:`covered_route_reason`.
    """
    target_node = str(decision.get("target_node", "") or "").strip()
    expected_target = str(expected_target_symbol or "").strip()
    if target_node and expected_target and target_node != expected_target:
        return (
            f"structured target_node `{target_node}` does not match active target "
            f"`{expected_target}`"
        )

    structured_references = set(_structured_node_references(decision))
    for fact in verified_graph_facts:
        compatibility = str(fact.get("route_compatibility", "") or "")
        if not compatibility or compatibility == EXACT_TARGET_CONCLUSION:
            continue
        name = str(fact.get("name", "") or "").strip()
        if name in structured_references:
            return (
                f"structured route identity `{name}` names a proved graph fact with "
                f"{compatibility}"
            )
    for raw_name in unrelated_frontier:
        name = str(raw_name or "").strip()
        if name in structured_references:
            return (
                f"structured route identity `{name}` is a campaign-global frontier node "
                "without a target dependency edge"
            )
    return ""


def covered_route_reason(
    decision: Mapping[str, Any],
    *,
    verified_graph_facts: Sequence[Mapping[str, Any]] = (),
    failed_route_signatures: Sequence[str] = (),
) -> str:
    """Return why a proposed route is already covered, or ``""`` if novel.

    The check is intentionally one-sided.  A non-empty result is strong
    duplicate evidence; an empty result does not claim mathematical novelty.
    """
    statements = [
        dict(entry)
        for entry in (decision.get("statements_to_state") or [])
        if isinstance(entry, Mapping)
    ]
    candidate_names = {
        str(entry.get("name", "") or "").strip() for entry in statements if entry.get("name")
    }
    target_node = str(decision.get("target_node", "") or "").strip()
    candidate_statements = [str(entry.get("statement", "") or "") for entry in statements]

    for fact in verified_graph_facts:
        name = str(fact.get("name", "") or "").strip()
        statement = str(fact.get("statement", "") or "").strip()
        if name and (name in candidate_names or name == target_node):
            return f"verified graph fact `{name}` is already a completed route target"
        verified_signature = _declaration_signature(statement)
        for candidate in candidate_statements:
            candidate_signature = _declaration_signature(candidate)
            if (
                verified_signature
                and candidate_signature
                and len(verified_signature) >= 40
                and candidate_signature == verified_signature
            ):
                return f"verified graph fact `{name or '[unnamed]'}` already has that statement"
            if statement and _affine_subfamily_covered(candidate, statement):
                return (
                    f"verified graph fact `{name or '[unnamed]'}` already covers the proposed "
                    "arithmetic subfamily"
                )

    candidate_text = "\n".join(_decision_texts(decision))
    normalized_candidate_text = _normalize_statement(candidate_text)
    for signature in failed_route_signatures:
        normalized_signature = _normalize_statement(str(signature or ""))
        if len(normalized_signature) >= 40 and normalized_signature in normalized_candidate_text:
            return "the proposed route repeats a recorded failed proof-shape signature"
    return ""
