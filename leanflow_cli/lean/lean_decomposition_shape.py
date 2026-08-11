"""Inspect one model-proposed Lean helper declaration without elaborating it."""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)

_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b", flags=re.IGNORECASE)
_DECLARATION_HEAD_RE = re.compile(
    r"^\s*(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+)\s+)*"
    r"(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
    r"(?P<kind>theorem|lemma)\s+(?P<name>[A-Za-z_«][A-Za-z0-9_'.»]*)\b",
    flags=re.DOTALL,
)
_DECLARATION_KEYWORD_RE = re.compile(
    r"\b(?:theorem|lemma|example|def|abbrev|axiom|instance|structure|class|" r"inductive|opaque)\b"
)
_EXTRA_COMMAND_RE = re.compile(
    r"(?m)^\s*(?:namespace|section|end|open|export|attribute|variable|include|omit|"
    r"set_option)\b"
)


@dataclass(frozen=True)
class HelperSkeletonShape:
    """Describe the deterministic shape of one proposed helper declaration."""

    valid: bool
    declared_name: str = ""
    kind: str = ""
    signature: str = ""
    has_placeholder: bool = False
    exact_sorry_stub: bool = False
    reason: str = ""


def inspect_helper_skeleton(
    skeleton: str,
    *,
    expected_name: str = "",
) -> HelperSkeletonShape:
    """Require exactly one named theorem/lemma and report its proof-body shape."""
    text = str(skeleton or "").strip()
    sanitized = _strip_lean_comments_and_strings(text).strip()
    has_placeholder = bool(_PLACEHOLDER_RE.search(sanitized))
    if not sanitized:
        return HelperSkeletonShape(
            valid=False,
            has_placeholder=has_placeholder,
            reason="missing lean_skeleton",
        )
    entries = _declaration_line_index_from_text(sanitized)
    if len(entries) != 1:
        return HelperSkeletonShape(
            valid=False,
            has_placeholder=has_placeholder,
            reason="helper skeleton must contain exactly one declaration",
        )
    entry = entries[0]
    kind = str(entry.get("kind", "") or "").strip()
    declared_name = str(entry.get("name", "") or "").strip()
    head = _DECLARATION_HEAD_RE.match(sanitized)
    if kind not in {"theorem", "lemma"} or head is None:
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            has_placeholder=has_placeholder,
            reason="helper skeleton must be one theorem or lemma declaration",
        )
    parsed_name = str(head.group("name") or "").strip()
    if parsed_name != declared_name:
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            has_placeholder=has_placeholder,
            reason="helper skeleton declaration identity is ambiguous",
        )
    wanted = str(expected_name or "").strip().removeprefix("_root_.")
    actual = declared_name.removeprefix("_root_.")
    if wanted and actual != wanted:
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            has_placeholder=has_placeholder,
            reason=(
                f"helper metadata name {wanted!r} does not match skeleton declaration "
                f"{actual!r}"
            ),
        )
    if len(_DECLARATION_KEYWORD_RE.findall(sanitized)) != 1:
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            has_placeholder=has_placeholder,
            reason="helper skeleton contains an extra declaration command",
        )
    marker = _find_assignment_marker_for_statement(sanitized)
    if marker < 0:
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            has_placeholder=has_placeholder,
            reason="helper skeleton is missing a top-level := proof assignment",
        )
    signature = sanitized[:marker].rstrip()
    body = sanitized[marker + 2 :].strip()
    if _EXTRA_COMMAND_RE.search(body):
        return HelperSkeletonShape(
            valid=False,
            declared_name=declared_name,
            kind=kind,
            signature=signature,
            has_placeholder=has_placeholder,
            reason="helper skeleton contains an extra top-level command",
        )
    exact_sorry_stub = bool(re.fullmatch(r"by\s+sorry", body))
    return HelperSkeletonShape(
        valid=True,
        declared_name=declared_name,
        kind=kind,
        signature=signature,
        has_placeholder=has_placeholder,
        exact_sorry_stub=exact_sorry_stub,
    )


def exact_sorry_stub_shape_ok(skeleton: str, *, expected_name: str = "") -> bool:
    """Return whether text is one exact, identity-matching ``by sorry`` stub."""
    shape = inspect_helper_skeleton(skeleton, expected_name=expected_name)
    return shape.valid and shape.exact_sorry_stub
