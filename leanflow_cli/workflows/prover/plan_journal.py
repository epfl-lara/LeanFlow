"""Keep a durable record of what planning has already established or ruled out.

Every planning proposal runs in a fresh model context, and the only feedback it
receives is the immediately preceding draft plus the single newest critique. The
plan itself is not a substitute: ``plan_markdown`` is rewritten only when a
proposal is *accepted*, so a planner that has never had one accepted reads the
same opening outline on every attempt.

The observed consequence is a loop. A planner satisfies the newest critique,
receives a critique about something else, satisfies that, and reintroduces the
first violation -- because it has never seen the two complaints together. In one
Lean-IMO-Bench cell this repeated for eleven rounds without a single accepted
decomposition, and nothing bounded it: rejected drafts do not spend the plan
refinement budget, so only the campaign call ceiling and the wall clock ever
stop it.

This journal is the missing memory. It is written on rejection rather than on
acceptance, kept beside ``plan_markdown`` so an accepted plan cannot clobber it,
and handed to every job through the shared context.
"""

from __future__ import annotations

from typing import Any

#: Distinct findings retained. Exact repeats collapse into one entry rather than
#: consuming a slot, so this bounds genuinely different findings, and eviction
#: is by least-recently-seen: a constraint the planner keeps violating stays.
MAX_ENTRIES = 40

#: Per-finding budget. Mechanical constraints ("planned name does not match its
#: declaration") are short and survive whole, which matters because they must be
#: obeyed literally. Long mathematical critiques are truncated; the newest one is
#: always delivered in full through ``planning_critique`` regardless.
MAX_DETAIL = 1000


def record(state: dict[str, Any], kind: str, detail: str, *, at: str) -> dict[str, Any] | None:
    """Add one finding, collapsing an exact repeat into the existing entry.

    Returns the entry written, or None when there was nothing to record. A
    repeat bumps ``count`` and ``last_at`` instead of appending, so the record
    reads "this was rejected three times" rather than filling with one message.
    """
    text = " ".join(str(detail or "").split())
    if not text:
        return None
    text = text[:MAX_DETAIL]
    journal = state.setdefault("plan_journal", [])
    if not isinstance(journal, list):
        journal = []
        state["plan_journal"] = journal
    for entry in journal:
        if entry.get("kind") == kind and entry.get("detail") == text:
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["last_at"] = at
            journal.sort(key=lambda item: str(item.get("last_at", "")))
            return entry
    entry = {"kind": kind, "detail": text, "count": 1, "first_at": at, "last_at": at}
    journal.append(entry)
    journal.sort(key=lambda item: str(item.get("last_at", "")))
    # Evict least-recently-seen, so a repeatedly violated constraint is retained
    # while one-off observations age out.
    del journal[: max(0, len(journal) - MAX_ENTRIES)]
    return entry


def render(journal: Any) -> str:
    """Render the journal as the PLAN.md section a human would read."""
    if not isinstance(journal, list) or not journal:
        return ""
    lines = [
        "## Planning journal",
        "",
        "Findings already established in this run. Do not repropose anything ruled",
        "out here, and keep satisfying every constraint listed, not only the newest.",
        "",
    ]
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        seen = int(entry.get("count", 1))
        repeated = f" (seen {seen}x)" if seen > 1 else ""
        lines.append(f"- **{entry.get('kind', 'note')}**{repeated}: {entry.get('detail', '')}")
    return "\n".join(lines) + "\n"
