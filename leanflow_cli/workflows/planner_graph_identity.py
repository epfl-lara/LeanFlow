"""Compare exact Lean declaration identities for planner graph admission."""

from __future__ import annotations

import hashlib
import re

from leanflow_cli.lean.lean_parsing import _statement_signature_text
from leanflow_cli.lean.lean_statement_guard import _normalize_statement_signature

_DECLARATION_SIGNATURE_RE = re.compile(
    r"^(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+|private|protected|noncomputable|unsafe|partial|local)\s+)*"
    r"(?:theorem|lemma)\s+(?:«[^»]+»|[^\s:({]+)(?P<core>.*)$",
    re.DOTALL,
)
_LEGACY_CORE_GENERATORS = frozenset(
    {
        "decomposer",
        "prover-edit",
        "prover-edit-backfill",
    }
)


def declaration_signature(statement: str) -> str:
    """Return the proof-insensitive normalized signature for one declaration.

    Comments, whitespace, and the proof body do not authorize a new graph node
    to inherit an existing kernel status. String literals remain part of the
    identity because changing one can materially change the proposition.
    """
    head = _statement_signature_text(str(statement or ""))
    return _normalize_statement_signature(head)


def is_full_declaration_signature(statement: str) -> bool:
    """Return whether ``statement`` carries a named theorem/lemma declaration head."""
    return bool(_DECLARATION_SIGNATURE_RE.fullmatch(declaration_signature(statement)))


def legacy_core_matches_declaration(
    legacy_statement: str,
    proposed_statement: str,
    *,
    generated_by: str,
) -> bool:
    """Return whether a known legacy producer stored this exact declaration core.

    Historical decomposer graph nodes omitted the declaration modifier, kind,
    and name and stored only binders plus the result type. Restrict migration
    to those known producers and require an exact normalized core match; an
    arbitrary non-declaration string never authenticates kernel truth.
    """
    if str(generated_by or "").strip() not in _LEGACY_CORE_GENERATORS:
        return False
    legacy_signature = declaration_signature(legacy_statement)
    if not legacy_signature or is_full_declaration_signature(legacy_statement):
        return False
    proposed_signature = declaration_signature(proposed_statement)
    match = _DECLARATION_SIGNATURE_RE.fullmatch(proposed_signature)
    if match is None:
        return False
    proposed_core = str(match.group("core") or "").strip()
    return bool(proposed_core and legacy_signature == proposed_core)


def signature_sha256(signature: str) -> str:
    """Return the stable digest used when journaling a signature conflict."""
    normalized = str(signature or "")
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
