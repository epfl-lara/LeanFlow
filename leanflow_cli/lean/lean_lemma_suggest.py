"""Goal-driven candidate-lemma retriever for the Lean prove worker.

Automates the goal -> query -> search -> rank loop a model otherwise runs by hand: read the
assigned declaration's goal/hypotheses (via ``lean_proof_context`` with a ``lean_inspect`` goal
fallback), derive a few targeted queries from the conclusion head symbol, key operators, and
hypothesis types, run ``lean_search`` across the semantic and type-pattern modes, then dedupe and
rank the returned declarations by overlap with the goal's symbols.

Pure orchestration leaf: it reaches the three backends lazily off ``lean_services`` at call time
(so test monkeypatches on ``lean_services.<name>`` apply) and owns no state. It imports only stdlib
plus ``lean_declarations``; it does NOT import ``lean_services`` at module load, ``native_runner``,
``tools`` or ``leanflow_cli.cli``, so it introduces no import cycle and stays inside the
``leanflow_cli`` layer.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_declarations import _find_declaration_entry

# Query derivation bounds: keep the derived query set small and cheap so the retriever issues a few
# high-signal searches rather than flooding the (rate-limited) semantic providers.
MAX_DERIVED_QUERIES = 4
MAX_CANDIDATES = 12
_PER_QUERY_SEARCH_LIMIT = 8

# Lean-name/operator tokenizers. Identifiers may be namespaced (``List.map``); operators are the
# non-word glyphs that most often anchor a Mathlib lemma name search.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")
_OPERATOR_TOKENS = (
    "≤",
    "≥",
    "<",
    ">",
    "≠",
    "∣",
    "∑",
    "∏",
    "∈",
    "∉",
    "⊆",
    "∩",
    "∪",
    "∀",
    "∃",
    "%",
    "^",
    "√",
)
# Lean keywords / connectives that are never useful as a lemma-search head symbol.
_STOPWORD_IDENTS = frozenset(
    {
        "theorem",
        "lemma",
        "example",
        "def",
        "by",
        "fun",
        "let",
        "in",
        "if",
        "then",
        "else",
        "match",
        "with",
        "do",
        "have",
        "show",
        "from",
        "at",
        "Type",
        "Prop",
        "Sort",
        "sorry",
    }
)


def _proof_context(file_path: str, theorem_id: str, cwd: str | None) -> Mapping[str, Any]:
    """Fetch theorem-local context; resolve the backend lazily so test monkeypatches apply."""
    from leanflow_cli.lean import lean_services

    try:
        payload = lean_services.lean_proof_context(file_path, theorem_id, cwd=cwd)
    except Exception:
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _inspect_goals(file_path: str, theorem_id: str, cwd: str | None) -> str:
    """Return the goal text from ``lean_inspect`` as a fallback when proof-context has none."""
    from leanflow_cli.lean import lean_services

    try:
        inspection = lean_services.lean_inspect(file_path, cwd=cwd, symbol=theorem_id)
    except Exception:
        return ""
    goals = getattr(inspection, "goals", "")
    return str(goals or "").strip()


def _statement_from_disk(file_path: str, theorem_id: str) -> str:
    """Read the declaration's source text directly as a last-resort goal source."""
    entry = _find_declaration_entry(Path(file_path).expanduser(), theorem_id)
    if not entry:
        return ""
    return str(entry.get("text", "") or "").strip()


def _hypothesis_text(hypotheses: Any) -> list[str]:
    """Flatten proof-context hypotheses (strings or ``{name, type}`` maps) to type-bearing text."""
    out: list[str] = []
    if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, str):
        return out
    for item in hypotheses:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, Mapping):
            text = str(item.get("type", "") or item.get("statement", "") or "").strip()
        else:
            text = ""
        if text:
            out.append(text)
    return out


def _conclusion_fragment(goal: str) -> str:
    """Return the target conclusion of a goal/statement so the head symbol comes from it.

    Drops the proof body (``:=`` ...), then takes the text after ``⊢`` for a goal state, or after
    the last top-level ``:`` for a ``theorem``/``lemma`` statement (so the declaration name and
    binders don't get ranked as the head symbol), and finally narrows to the last ``→``/``->``
    conclusion segment.
    """
    snippet = str(goal or "").strip()
    if not snippet:
        return ""
    # Drop the proof body so `:= by ...` never pollutes the conclusion.
    if ":=" in snippet:
        snippet = snippet.split(":=", 1)[0]
    if "⊢" in snippet:
        snippet = snippet.rsplit("⊢", 1)[-1]
    elif ":" in snippet:
        # A statement like `theorem foo (a : T) : Concl` — the conclusion is after the LAST colon,
        # so the theorem name / binders aren't mistaken for the head symbol.
        snippet = snippet.rsplit(":", 1)[-1]
    # Prefer the conclusion of the top-level arrow chain; the last `→`/`->` segment is the target.
    for arrow in ("→", "->"):
        if arrow in snippet:
            snippet = snippet.rsplit(arrow, 1)[-1]
    return snippet.strip()


def _significant_idents(text: str) -> list[str]:
    """Extract meaningful (non-keyword) identifiers from a Lean fragment, order-preserving."""
    seen: list[str] = []
    for token in _IDENT_RE.findall(text):
        head = token.split(".")[0]
        if token in _STOPWORD_IDENTS or head in _STOPWORD_IDENTS:
            continue
        if len(token) == 1 and token.islower():
            # Single lowercase letters are bound variables, not searchable symbols.
            continue
        if token not in seen:
            seen.append(token)
    return seen


def _operators_in(text: str) -> list[str]:
    """Return the notable operator glyphs present in a fragment, order-preserving."""
    found: list[str] = []
    for op in _OPERATOR_TOKENS:
        if op in text and op not in found:
            found.append(op)
    return found


def _goal_symbols(*, conclusion: str, hypotheses: list[str]) -> list[str]:
    """Collect the ranked symbol vocabulary of a goal: conclusion idents first, then hypotheses."""
    symbols: list[str] = list(_significant_idents(conclusion))
    for hyp in hypotheses:
        for ident in _significant_idents(hyp):
            if ident not in symbols:
                symbols.append(ident)
    return symbols


def derive_queries(*, goal: str, hypotheses: list[str], statement: str) -> list[str]:
    """Build 2-4 targeted lemma-search queries from a goal: head symbol, operators, hypothesis types.

    The queries are ordered by expected signal — the conclusion head symbol (optionally with its
    key operator) first, then a namespaced/short form, then a hypothesis-type query — so the caller
    can spend its rate-limited search budget on the most promising probes first.
    """
    conclusion = _conclusion_fragment(goal) or _conclusion_fragment(statement)
    concl_idents = _significant_idents(conclusion)
    concl_ops = _operators_in(conclusion)
    queries: list[str] = []

    def _push(query: str) -> None:
        query = query.strip()
        if query and query not in queries and len(queries) < MAX_DERIVED_QUERIES:
            queries.append(query)

    # 1. Conclusion head symbol + its dominant operator — the strongest single probe.
    head = concl_idents[0] if concl_idents else ""
    if head and concl_ops:
        _push(f"{head} {concl_ops[0]}")
    if head:
        _push(head)
        # 2. Namespaced short form so both `List.map` and bare `map` reach name-based providers.
        short = head.split(".")[-1]
        if short != head:
            _push(short)
    # 3. A second conclusion symbol pairs the head with a companion for a more specific probe.
    if len(concl_idents) >= 2:
        _push(f"{concl_idents[0]} {concl_idents[1]}")
    # 4. Hypothesis-type query — the key symbols the target is proved *from*.
    for hyp in hypotheses:
        hyp_idents = _significant_idents(hyp)
        if hyp_idents:
            _push(" ".join(hyp_idents[:2]))
            break
    # Last resort: fall back to raw statement idents so the tool never returns zero queries.
    if not queries:
        for ident in _significant_idents(statement):
            _push(ident)
    return queries


def _candidate_name(match_text: str) -> str:
    """Pull the most likely declaration name (first namespaced identifier) from a search hit."""
    for token in _IDENT_RE.findall(match_text):
        if token not in _STOPWORD_IDENTS and any(ch.isalpha() for ch in token):
            return token
    return ""


def _run_search(query: str, *, mode: str, cwd: str | None, file_path: str) -> list[dict[str, Any]]:
    """Invoke ``lean_search`` for one query/mode; resolve the backend lazily for monkeypatching."""
    from leanflow_cli.lean import lean_services

    try:
        result = lean_services.lean_search(
            query,
            mode=mode,
            cwd=cwd,
            limit=_PER_QUERY_SEARCH_LIMIT,
            file_path=file_path,
        )
    except Exception:
        return []
    raw = getattr(result, "results", None)
    return list(raw) if isinstance(raw, list) else []


def _rank_candidates(
    raw_hits: list[tuple[str, str, dict[str, Any]]],
    *,
    goal_symbols: list[str],
) -> list[dict[str, Any]]:
    """Dedupe search hits by declaration name and rank them by goal-symbol overlap.

    Relevance = number of distinct goal symbols the candidate's match text mentions, with the
    conclusion head symbol (first in ``goal_symbols``) weighted highest. Ties fall back to the
    number of queries that surfaced the candidate, then to first-seen order for stability.
    """
    symbol_weight = {sym: (len(goal_symbols) - idx) for idx, sym in enumerate(goal_symbols)}
    aggregated: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for query, match_text, hit in raw_hits:
        name = str(hit.get("name") or "").strip() or _candidate_name(match_text)
        key = name or match_text[:80]
        record = aggregated.get(key)
        if record is None:
            record = {
                "name": name,
                "signature": match_text.strip()[:400],
                "provider": str(hit.get("provider", "") or ""),
                "_queries": set(),
                "_first_seen": len(order),
            }
            aggregated[key] = record
            order.append(key)
        record["_queries"].add(query)

    ranked: list[dict[str, Any]] = []
    for key in order:
        record = aggregated[key]
        haystack = f"{record['name']} {record['signature']}"
        matched = [sym for sym in goal_symbols if sym and sym in haystack]
        relevance = sum(symbol_weight[sym] for sym in matched)
        record["_relevance"] = relevance
        record["_matched"] = matched
        ranked.append(record)

    ranked.sort(
        key=lambda r: (-r["_relevance"], -len(r["_queries"]), r["_first_seen"]),
    )

    out: list[dict[str, Any]] = []
    for record in ranked[:MAX_CANDIDATES]:
        matched = record["_matched"]
        if matched:
            why = "shares goal symbols: " + ", ".join(matched[:4])
        else:
            why = "surfaced by query: " + "; ".join(sorted(record["_queries"])[:2])
        out.append(
            {
                "name": record["name"],
                "signature": record["signature"],
                "provider": record["provider"],
                "why_relevant": why,
            }
        )
    return out


def lean_lemma_suggest(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    """Suggest ranked candidate lemmas for the assigned declaration's current goal.

    Reads the goal/hypotheses, derives targeted queries, searches semantic + type-pattern modes,
    then returns a compact ranked candidate list plus the queries and any degraded reasons so the
    caller can see how the suggestions were produced.
    """
    file_path = str(file_path or "").strip()
    theorem_id = str(theorem_id or "").strip()
    cwd_text = str(cwd) if cwd not in (None, "") else None
    if not file_path or not theorem_id:
        return {
            "success": False,
            "file_path": file_path,
            "theorem_id": theorem_id,
            "queries": [],
            "candidates": [],
            "degraded_reasons": ["file_path and theorem_id are required"],
        }

    context = _proof_context(file_path, theorem_id, cwd_text)
    degraded: list[str] = [str(r) for r in context.get("degraded_reasons", []) or []]
    statement = str(context.get("theorem_statement", "") or "").strip()
    if not statement:
        statement = _statement_from_disk(file_path, theorem_id)
    hypotheses = _hypothesis_text(context.get("hypotheses"))
    goal = str(context.get("goals", "") or context.get("goal", "") or "").strip()
    if not goal:
        goal = _inspect_goals(file_path, theorem_id, cwd_text)
    if not goal:
        goal = statement

    queries = derive_queries(goal=goal, hypotheses=hypotheses, statement=statement)
    if not queries:
        degraded.append("could not derive any search query from the goal")
        return {
            "success": False,
            "file_path": file_path,
            "theorem_id": theorem_id,
            "queries": [],
            "candidates": [],
            "degraded_reasons": list(dict.fromkeys(degraded)),
        }

    goal_symbols = _goal_symbols(
        conclusion=_conclusion_fragment(goal) or _conclusion_fragment(statement),
        hypotheses=hypotheses,
    )
    raw_hits: list[tuple[str, str, dict[str, Any]]] = []
    for query in queries:
        for mode in ("semantic", "type-pattern"):
            for hit in _run_search(query, mode=mode, cwd=cwd_text, file_path=file_path):
                if not isinstance(hit, Mapping):
                    continue
                match_text = str(hit.get("match", "") or hit.get("preview", "") or "")
                raw_hits.append((query, match_text, dict(hit)))

    if not raw_hits:
        degraded.append("no candidate lemmas found for the derived queries")

    candidates = _rank_candidates(raw_hits, goal_symbols=goal_symbols)
    limit = max(1, int(max_candidates or MAX_CANDIDATES))
    candidates = candidates[:limit]
    return {
        "success": bool(candidates),
        "file_path": file_path,
        "theorem_id": theorem_id,
        "queries": queries,
        "goal_symbols": goal_symbols[:12],
        "candidates": candidates,
        "degraded_reasons": list(dict.fromkeys(degraded)),
    }
