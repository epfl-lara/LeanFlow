"""Reject plainly false affine arithmetic in advisory orchestrator routes.

The preflight recognizes only a deliberately small fragment: affine integer
expressions, affine equalities presented as identities, and divisibility
claims over an optional residue class.  It fails open on every other shape;
the Lean kernel remains the authority for general mathematics.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

ARITHMETIC_PREFLIGHT_REJECTION_PREFIX = "arithmetic-preflight-rejected:"


@dataclass(frozen=True)
class AffineExpression:
    """Represent a multivariable affine integer expression."""

    coefficients: tuple[tuple[str, int], ...] = ()
    constant: int = 0

    @classmethod
    def from_parts(cls, coefficients: Mapping[str, int], constant: int) -> AffineExpression:
        """Build a normalized expression with zero coefficients removed."""
        return cls(
            coefficients=tuple(
                sorted((name, value) for name, value in coefficients.items() if value)
            ),
            constant=constant,
        )

    def coefficient_map(self) -> dict[str, int]:
        """Return the normalized coefficient mapping."""
        return dict(self.coefficients)


@dataclass(frozen=True)
class ArithmeticPreflightIssue:
    """Describe one deterministic countercheck that refutes a route claim."""

    kind: str
    claim: str
    evidence: str

    def to_mapping(self) -> dict[str, str]:
        """Return a JSON-friendly evidence record."""
        return {"kind": self.kind, "claim": self.claim, "evidence": self.evidence}


@dataclass(frozen=True)
class ArithmeticPreflightReport:
    """Return the conservative result of checking one advisory decision."""

    issues: tuple[ArithmeticPreflightIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        """Return whether no supported arithmetic claim was refuted."""
        return not self.issues

    def rejection_note(self, *, limit: int = 700) -> str:
        """Render bounded durable evidence for fallback and route deduplication."""
        if not self.issues:
            return ""
        issue = self.issues[0]
        note = (
            f"{ARITHMETIC_PREFLIGHT_REJECTION_PREFIX} {issue.kind}; "
            f"claim `{issue.claim}`; {issue.evidence}"
        )
        return note if len(note) <= limit else note[: limit - 3] + "..."

    def evidence(self) -> tuple[dict[str, str], ...]:
        """Return every supported countercheck as structured evidence."""
        return tuple(issue.to_mapping() for issue in self.issues)


_TOKEN_RE = re.compile(r"\d+|[A-Za-z_][A-Za-z0-9_']*|[()+*\-/]")
_EQUALITY_RE = re.compile(r"(?<![:<>!=%])=(?!=)")
_DIVISIBILITY_RE = re.compile(r"(?P<divisor>\d+)\s*(?:\||∣)\s*")
_WORD_DIVISIBILITY_RE = re.compile(r"(?P<divisor>\d+)\s+divides\s+", re.IGNORECASE)
_RESIDUE_PATTERNS = (
    re.compile(
        r"\b(?P<variable>[A-Za-z_][A-Za-z0-9_']*)\s*≡\s*(?P<residue>-?\d+)\s*"
        r"(?:\(\s*)?mod\s*(?P<modulus>\d+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<variable>[A-Za-z_][A-Za-z0-9_']*)\s*%\s*(?P<modulus>\d+)\s*"
        r"=\s*(?P<residue>-?\d+)",
        re.IGNORECASE,
    ),
)
_SPECULATIVE_RE = re.compile(
    r"\b(?:test|check|probe|ask)\s+(?:whether|if)\b|\b(?:candidate|conjectural|hypothetical)\b",
    re.IGNORECASE,
)
_DECLARATION_HEAD_RE = re.compile(
    r"\b(?:private\s+)?(?:lemma|theorem)\s+(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)"
)
_NAT_BINDER_RE = re.compile(
    r"\(\s*(?P<names>[A-Za-z_][A-Za-z0-9_']*(?:\s+[A-Za-z_][A-Za-z0-9_']*)*)" r"\s*:\s*ℕ\s*\)"
)
_TYPE_ASCRIPTION_RE = re.compile(r"\s*:\s*(?:ℕ|ℚ|ℤ)")


class _AffineParser:
    """Parse the arithmetic fragment without evaluating arbitrary input."""

    def __init__(self, tokens: Sequence[str]) -> None:
        self.tokens = list(tokens)
        self.index = 0

    def parse(self) -> AffineExpression | None:
        """Return an affine expression, or ``None`` when the fragment exceeds scope."""
        value = self._expression()
        if value is None or self.index != len(self.tokens):
            return None
        return value

    def _peek(self, value: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index] == value

    def _take(self) -> str:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _expression(self) -> AffineExpression | None:
        value = self._term()
        if value is None:
            return None
        while self._peek("+") or self._peek("-"):
            operator = self._take()
            right = self._term()
            if right is None:
                return None
            value = _add(value, right, sign=1 if operator == "+" else -1)
        return value

    def _term(self) -> AffineExpression | None:
        value = self._atom()
        if value is None:
            return None
        while self._peek("*"):
            self._take()
            right = self._atom()
            if right is None:
                return None
            value = _multiply(value, right)
            if value is None:
                return None
        return value

    def _atom(self) -> AffineExpression | None:
        if self.index >= len(self.tokens):
            return None
        token = self._take()
        if token.isdigit():
            return AffineExpression(constant=int(token))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", token):
            return AffineExpression.from_parts({token: 1}, 0)
        if token == "(":
            value = self._expression()
            if value is None or not self._peek(")"):
                return None
            self._take()
            return value
        return None


class _GroundRationalParser:
    """Evaluate one fully-ground arithmetic expression over exact rationals."""

    def __init__(self, tokens: Sequence[str], values: Mapping[str, int]) -> None:
        self.tokens = list(tokens)
        self.values = dict(values)
        self.index = 0

    def parse(self) -> Fraction | None:
        """Return the exact value, or ``None`` outside the supported fragment."""
        try:
            value = self._expression()
        except ZeroDivisionError:
            return None
        if value is None or self.index != len(self.tokens):
            return None
        return value

    def _peek(self, value: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index] == value

    def _take(self) -> str:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _expression(self) -> Fraction | None:
        value = self._term()
        if value is None:
            return None
        while self._peek("+"):
            self._take()
            right = self._term()
            if right is None:
                return None
            value += right
        return value

    def _term(self) -> Fraction | None:
        value = self._atom()
        if value is None:
            return None
        while self._peek("*") or self._peek("/"):
            operator = self._take()
            right = self._atom()
            if right is None:
                return None
            value = value * right if operator == "*" else value / right
        return value

    def _atom(self) -> Fraction | None:
        if self.index >= len(self.tokens):
            return None
        token = self._take()
        if token.isdigit():
            return Fraction(int(token))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", token):
            bound_value = self.values.get(token)
            return Fraction(bound_value) if bound_value is not None else None
        if token == "(":
            nested = self._expression()
            if nested is None or not self._peek(")"):
                return None
            self._take()
            return nested
        return None


@dataclass(frozen=True)
class _Equality:
    """Keep one parsed equality and its local source excerpt."""

    lhs: AffineExpression
    rhs: AffineExpression
    claim: str
    position: int


def _add(left: AffineExpression, right: AffineExpression, *, sign: int) -> AffineExpression:
    coefficients = left.coefficient_map()
    for name, coefficient in right.coefficients:
        coefficients[name] = coefficients.get(name, 0) + sign * coefficient
    return AffineExpression.from_parts(coefficients, left.constant + sign * right.constant)


def _multiply(left: AffineExpression, right: AffineExpression) -> AffineExpression | None:
    if left.coefficients and right.coefficients:
        return None
    if left.coefficients:
        scale = right.constant
        return AffineExpression.from_parts(
            {name: coefficient * scale for name, coefficient in left.coefficients},
            left.constant * scale,
        )
    if right.coefficients:
        scale = left.constant
        return AffineExpression.from_parts(
            {name: coefficient * scale for name, coefficient in right.coefficients},
            right.constant * scale,
        )
    return AffineExpression(constant=left.constant * right.constant)


def _parse_tokens(tokens: Sequence[str]) -> AffineExpression | None:
    if not tokens:
        return None
    return _AffineParser(tokens).parse()


def _left_expression(fragment: str) -> tuple[AffineExpression, str] | None:
    tokens = _TOKEN_RE.findall(fragment)
    for start in range(len(tokens)):
        candidate = tokens[start:]
        parsed = _parse_tokens(candidate)
        if parsed is not None:
            return parsed, "".join(candidate)
    return None


def _right_expression(fragment: str) -> tuple[AffineExpression, str] | None:
    tokens = _TOKEN_RE.findall(fragment)
    for end in range(len(tokens), 0, -1):
        candidate = tokens[:end]
        parsed = _parse_tokens(candidate)
        if parsed is not None:
            return parsed, "".join(candidate)
    return None


def _bounded_fragment(text: str, position: int, *, left: bool) -> str:
    """Return the nearest sentence-like fragment on one side of a marker."""
    delimiters = ",;:\n."
    if left:
        start = max(text.rfind(delimiter, 0, position) for delimiter in delimiters) + 1
        return text[start:position][-180:]
    candidates = [text.find(delimiter, position) for delimiter in delimiters]
    ends = [candidate for candidate in candidates if candidate >= 0]
    end = min(ends) if ends else len(text)
    return text[position:end][:180]


def _sentence_fragment(text: str, position: int) -> str:
    """Return the bounded sentence containing a claim marker."""
    start = max(text.rfind(delimiter, 0, position) for delimiter in "\n.") + 1
    candidates = [text.find(delimiter, position) for delimiter in "\n."]
    ends = [candidate for candidate in candidates if candidate >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _claim_is_conditional(text: str, position: int) -> bool:
    """Return whether a marker appears in an assumption rather than a claim."""
    prefix = text[max(0, position - 120) : position]
    open_paren = prefix.rfind("(")
    close_paren = prefix.rfind(")")
    if open_paren > close_paren and ":" in prefix[open_paren:]:
        return True
    sentence_start = max(text.rfind(delimiter, 0, position) for delimiter in "\n.") + 1
    sentence_prefix = text[sentence_start:position]
    return bool(
        re.search(
            r"\b(?:if|assuming|suppose|supposing|provided|hypothesis|under the assumption)\b",
            sentence_prefix[-100:],
            re.IGNORECASE,
        )
    )


def _equalities(text: str) -> tuple[_Equality, ...]:
    equalities: list[_Equality] = []
    for match in _EQUALITY_RE.finditer(text):
        left_fragment = _bounded_fragment(text, match.start(), left=True)
        right_fragment = _bounded_fragment(text, match.end(), left=False)
        # ``t % 7 = 0`` is a residue constraint, not an affine equality.
        # The percent token is intentionally outside the expression grammar.
        if "%" in left_fragment:
            continue
        left = _left_expression(left_fragment)
        right = _right_expression(right_fragment)
        if left is None or right is None:
            continue
        claim = f"{left[1]}={right[1]}"
        equalities.append(_Equality(lhs=left[0], rhs=right[0], claim=claim, position=match.start()))
    return tuple(equalities)


def _single_variable(expression: AffineExpression) -> str:
    if expression.constant != 0 or len(expression.coefficients) != 1:
        return ""
    name, coefficient = expression.coefficients[0]
    return name if coefficient == 1 else ""


def _expand(
    expression: AffineExpression,
    aliases: Mapping[str, AffineExpression],
    *,
    trail: frozenset[str] = frozenset(),
) -> AffineExpression:
    result = AffineExpression(constant=expression.constant)
    for name, coefficient in expression.coefficients:
        replacement = aliases.get(name)
        if replacement is None or name in trail:
            replacement = AffineExpression.from_parts({name: 1}, 0)
        else:
            replacement = _expand(replacement, aliases, trail=trail | {name})
        scaled = _multiply(AffineExpression(constant=coefficient), replacement)
        if scaled is not None:
            result = _add(result, scaled, sign=1)
    return result


def _format_affine(expression: AffineExpression) -> str:
    parts: list[str] = []
    for name, coefficient in expression.coefficients:
        if coefficient == 1:
            term = name
        elif coefficient == -1:
            term = f"-{name}"
        else:
            term = f"{coefficient}*{name}"
        parts.append(term)
    if expression.constant or not parts:
        parts.append(str(expression.constant))
    return "+".join(parts).replace("+-", "-")


def _identity_issues(text: str) -> tuple[ArithmeticPreflightIssue, ...]:
    aliases: dict[str, AffineExpression] = {}
    issues: list[ArithmeticPreflightIssue] = []
    speculative = bool(_SPECULATIVE_RE.search(text))
    for equality in _equalities(text):
        left_name = _single_variable(equality.lhs)
        right_name = _single_variable(equality.rhs)
        alias_name = ""
        alias_value: AffineExpression | None = None
        if left_name and left_name not in equality.rhs.coefficient_map():
            alias_name, alias_value = left_name, equality.rhs
        elif right_name and right_name not in equality.lhs.coefficient_map():
            alias_name, alias_value = right_name, equality.lhs

        if alias_name and alias_value is not None and alias_name not in aliases:
            aliases[alias_name] = _expand(alias_value, aliases)
            continue

        if _claim_is_conditional(text, equality.position):
            continue

        lhs = _expand(equality.lhs, aliases)
        rhs = _expand(equality.rhs, aliases)
        if lhs == rhs or speculative:
            continue
        # Different free-variable sets may become equal under an unmodelled
        # hypothesis.  Decline to judge instead of manufacturing a counterexample.
        if set(lhs.coefficient_map()) != set(rhs.coefficient_map()):
            continue
        issues.append(
            ArithmeticPreflightIssue(
                kind="affine-identity",
                claim=equality.claim,
                evidence=(
                    f"normalized affine forms differ: {_format_affine(lhs)} != "
                    f"{_format_affine(rhs)}"
                ),
            )
        )
    return tuple(issues)


def _declaration_identity_parts(text: str) -> tuple[str, tuple[str, ...], str] | None:
    """Return a simple Nat-quantified declaration's name, binders, and proposition."""
    marker = re.search(r":=\s*by\b", text)
    if marker is None:
        return None
    prefix = text[: marker.start()]
    head = _DECLARATION_HEAD_RE.search(prefix)
    if head is None:
        return None
    remainder = prefix[head.end() :]
    depth = 0
    proposition_at = -1
    opening = {"(": ")", "[": "]", "{": "}"}
    closing = set(opening.values())
    stack: list[str] = []
    for index, char in enumerate(remainder):
        if char in opening:
            stack.append(opening[char])
            depth += 1
        elif char in closing:
            if not stack or stack.pop() != char:
                return None
            depth -= 1
        elif char == ":" and depth == 0:
            proposition_at = index
            break
    if proposition_at < 0:
        return None

    binder_text = remainder[:proposition_at]
    variables: list[str] = []
    cursor = 0
    for binder in _NAT_BINDER_RE.finditer(binder_text):
        if binder_text[cursor : binder.start()].strip():
            return None
        variables.extend(binder.group("names").split())
        cursor = binder.end()
    if binder_text[cursor:].strip() or not variables:
        # Hypotheses and implicit/type binders change the domain. Decline to
        # manufacture a counterexample without modelling those assumptions.
        return None
    proposition = remainder[proposition_at + 1 :].strip()
    return head.group("name"), tuple(variables), proposition


def _has_nat_division(text: str) -> bool:
    """Return whether a slash occurs inside a parenthesized Nat ascription."""
    stack: list[int] = []
    for index, char in enumerate(text):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            stack.pop()
        elif char == ":" and text[index + 1 :].lstrip().startswith("ℕ") and stack:
            if "/" in text[stack[-1] : index]:
                return True
    return False


def _ground_rational_value(text: str, values: Mapping[str, int]) -> Fraction | None:
    """Evaluate the deliberately small casted-rational expression fragment."""
    if "-" in text or _has_nat_division(text):
        # Nat subtraction/division do not share rational semantics after
        # erasing casts, so those expressions remain outside this checker.
        return None
    without_types = _TYPE_ASCRIPTION_RE.sub("", text)
    compact = "".join(without_types.split())
    tokens = _TOKEN_RE.findall(without_types)
    if not tokens or "".join(tokens) != compact:
        return None
    return _GroundRationalParser(tokens, values).parse()


def _ground_rational_identity_issues(text: str) -> tuple[ArithmeticPreflightIssue, ...]:
    """Refute simple universal rational identities with one exact Nat witness.

    This is intentionally narrower than general nonlinear normalization. It
    recognizes only a complete Lean lemma/theorem with plain ``ℕ`` binders,
    no hypotheses, one rational equality, and arithmetic that can be evaluated
    exactly after grounding the binders. Everything else still fails open.
    """
    if "ℚ" not in text or "/" not in text or _SPECULATIVE_RE.search(text):
        return ()
    declaration = _declaration_identity_parts(text)
    if declaration is None:
        return ()
    name, variables, proposition = declaration
    equalities = tuple(_EQUALITY_RE.finditer(proposition))
    if len(equalities) != 1:
        return ()
    equality = equalities[0]
    left_text = proposition[: equality.start()]
    right_text = proposition[equality.end() :]
    assignments = (
        {variable: 0 for variable in variables},
        {variable: 1 for variable in variables},
    )
    for values in assignments:
        left = _ground_rational_value(left_text, values)
        right = _ground_rational_value(right_text, values)
        if left is None or right is None or left == right:
            continue
        witness = ", ".join(f"{variable}={values[variable]}" for variable in variables)
        return (
            ArithmeticPreflightIssue(
                kind="ground-rational-identity",
                claim=name,
                evidence=f"exact counterexample at {witness}: {left} != {right}",
            ),
        )
    return ()


def _residue_constraints(text: str) -> dict[str, tuple[tuple[int, int], ...]]:
    constraints: dict[str, list[tuple[int, int]]] = {}
    for pattern in _RESIDUE_PATTERNS:
        for match in pattern.finditer(text):
            modulus = int(match.group("modulus"))
            if modulus <= 0:
                continue
            variable = match.group("variable")
            residue = int(match.group("residue")) % modulus
            constraints.setdefault(variable, []).append((residue, modulus))
    return {name: tuple(dict.fromkeys(values)) for name, values in constraints.items()}


def _divisibility_matches(text: str) -> tuple[tuple[int, int], ...]:
    matches = [
        (match.start(), int(match.group("divisor")))
        for pattern in (_DIVISIBILITY_RE, _WORD_DIVISIBILITY_RE)
        for match in pattern.finditer(text)
    ]
    return tuple(sorted(matches))


def _divisibility_issues(text: str) -> tuple[ArithmeticPreflightIssue, ...]:
    if _SPECULATIVE_RE.search(text):
        return ()
    aliases: dict[str, AffineExpression] = {}
    for equality in _equalities(text):
        left_name = _single_variable(equality.lhs)
        right_name = _single_variable(equality.rhs)
        if (
            left_name
            and left_name not in equality.rhs.coefficient_map()
            and left_name not in aliases
        ):
            aliases[left_name] = _expand(equality.rhs, aliases)
        elif (
            right_name
            and right_name not in equality.lhs.coefficient_map()
            and right_name not in aliases
        ):
            aliases[right_name] = _expand(equality.lhs, aliases)

    issues: list[ArithmeticPreflightIssue] = []
    for position, divisor in _divisibility_matches(text):
        if divisor <= 0:
            continue
        # Re-find the end of the exact marker so parsing starts at its expression.
        marker = next(
            (
                match
                for pattern in (_DIVISIBILITY_RE, _WORD_DIVISIBILITY_RE)
                for match in pattern.finditer(text)
                if match.start() == position
            ),
            None,
        )
        if marker is None:
            continue
        if _claim_is_conditional(text, position):
            continue
        right = _right_expression(_bounded_fragment(text, marker.end(), left=False))
        if right is None:
            continue
        expression = _expand(right[0], aliases)
        coefficients = expression.coefficient_map()
        if len(coefficients) != 1:
            continue
        variable, coefficient = next(iter(coefficients.items()))

        counterexample: int | None = None
        constraints = _residue_constraints(_sentence_fragment(text, position))
        applicable_constraints = constraints.get(variable, ())
        if len(applicable_constraints) > 1:
            # Multiple residue hypotheses may be alternatives or an
            # inconsistent conjunction. Their logic is outside this fragment.
            continue
        if applicable_constraints:
            for residue, modulus in applicable_constraints:
                for step in range(divisor):
                    value = residue + modulus * step
                    if (coefficient * value + expression.constant) % divisor != 0:
                        counterexample = value
                        break
                if counterexample is not None:
                    break
        elif expression.constant % math.gcd(abs(coefficient), divisor):
            # With no stated residue class, reject only when the congruence
            # has no solution at all. A merely non-universal condition may be
            # a sound case split and is outside this preflight's authority.
            for value in range(divisor + 1):
                if (coefficient * value + expression.constant) % divisor != 0:
                    counterexample = value
                    break
        if counterexample is None:
            continue
        remainder = (coefficient * counterexample + expression.constant) % divisor
        issues.append(
            ArithmeticPreflightIssue(
                kind="affine-divisibility",
                claim=f"{divisor}|{right[1]}",
                evidence=(
                    f"after substitutions, {_format_affine(expression)} mod {divisor} is "
                    f"{remainder} at {variable}={counterexample}"
                ),
            )
        )
    return tuple(issues)


def _asserted_decision_texts(decision: Mapping[str, Any]) -> tuple[str, ...]:
    """Return route-authoritative prose while excluding explicit probe questions."""
    texts = [str(decision.get("reason", "") or "")]
    for entry in decision.get("statements_to_state") or []:
        if isinstance(entry, Mapping):
            texts.append(str(entry.get("statement", "") or ""))
    return tuple(text for text in texts if text.strip())


def preflight_route_decision(decision: Mapping[str, Any]) -> ArithmeticPreflightReport:
    """Reject only supported affine claims with a deterministic countercheck.

    Empty evidence means either the route passed this narrow fragment or the
    mathematics was outside it.  It never means the route is generally true.
    """
    issues: list[ArithmeticPreflightIssue] = []
    for text in _asserted_decision_texts(decision):
        issues.extend(_identity_issues(text))
        issues.extend(_divisibility_issues(text))
        issues.extend(_ground_rational_identity_issues(text))
    return ArithmeticPreflightReport(issues=tuple(issues[:4]))
