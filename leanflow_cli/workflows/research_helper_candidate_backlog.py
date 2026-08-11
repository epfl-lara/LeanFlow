"""Normalize and order the durable checked-helper candidate backlog."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar, cast

CandidateT = TypeVar("CandidateT")
MAX_BACKLOG_CANDIDATES = 32


def normalize(
    raw: object,
    *,
    parse: Callable[[Mapping[str, object]], CandidateT | None],
    candidate_id: Callable[[CandidateT], str],
) -> tuple[CandidateT, ...]:
    """Return a bounded, valid, duplicate-free backlog in promotion order."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    entries: list[CandidateT] = []
    seen: set[str] = set()
    for value in raw:
        candidate = cast(CandidateT, value) if not isinstance(value, Mapping) else parse(value)
        if candidate is None:
            continue
        try:
            identity = candidate_id(candidate)
        except (AttributeError, TypeError):
            identity = ""
        if not identity or identity in seen:
            continue
        seen.add(identity)
        entries.append(candidate)
    return tuple(entries[:MAX_BACKLOG_CANDIDATES])


def prepend(
    displaced: CandidateT,
    backlog: Sequence[CandidateT],
    *,
    candidate_id: Callable[[CandidateT], str],
    exclude_id: str = "",
) -> tuple[CandidateT, ...]:
    """Place a displaced active candidate first without duplicating identities."""
    displaced_id = candidate_id(displaced)
    return tuple(
        [displaced]
        + [
            candidate
            for candidate in backlog
            if candidate_id(candidate) not in {displaced_id, exclude_id}
        ]
    )[:MAX_BACKLOG_CANDIDATES]


def promote(
    backlog: Sequence[CandidateT],
) -> tuple[CandidateT | None, tuple[CandidateT, ...]]:
    """Return the next active candidate and remaining backlog."""
    records = tuple(backlog)
    return (records[0], records[1:]) if records else (None, ())
