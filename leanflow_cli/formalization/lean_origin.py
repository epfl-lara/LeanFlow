"""Authenticate imported compiler helpers through exact kernel profiles and source owners."""

from __future__ import annotations

from typing import Any


def _exact_parent(row: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Compare the importing constant's parent snapshot with independent replay evidence."""
    fields = ("name", "module", "kind", "type", "levels", "body", "source_range")
    return row.get("generated_parent_profiles") == [{field: parent.get(field) for field in fields}]


def _closed(row: dict[str, Any]) -> bool:
    """Require complete replayed helpers without hidden proof or computational holes."""
    return (
        row.get("unsafe") is False
        and row.get("pending") is False
        and row.get("direct_sorry") is False
        and row.get("data_sorry") is False
        and row.get("direct_type_sorry") is False
        and row.get("type_untracked_sorry") is False
        and row.get("untracked_sorry") is False
        and row.get("depends_on") == []
        and "sorryAx" not in row.get("axioms", ["sorryAx"])
        and isinstance(row.get("body"), str)
    )


def _source_definition(row: dict[str, Any]) -> bool:
    """Admit the ultimate ordinary source definition without promoting it into a draft."""
    return (
        _closed(row)
        and row.get("kind") == "definition"
        and isinstance(row.get("source_range"), dict)
        and row.get("generated_from") == []
        and row.get("generation_kind") == ""
    )


def authenticated_imported_equation(row: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Bind an imported lazy equation to its exact independently replayed source definition."""
    return (
        row.get("generation_kind") == "equation"
        and row.get("generated_from") == [parent.get("name")]
        and row.get("kind") == "theorem"
        and row.get("is_prop") is True
        and row.get("source_range") is None
        and row.get("line_start") is None
        and _source_definition(parent)
        and parent.get("name") in row.get("direct_dependencies", [])
        and _exact_parent(row, parent)
    )


def _matcher_owner(
    matcher: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Authenticate one matcher through its source owner's actual kernel body dependency."""
    parents = matcher.get("generated_from", [])
    owner = (
        profiles.get(parents[0])
        if isinstance(parents, list) and len(parents) == 1 and isinstance(parents[0], str)
        else None
    )
    body_dependencies = (owner or {}).get("direct_body_dependencies")
    if (
        owner is None
        or not _source_definition(owner)
        or not _closed(matcher)
        or matcher.get("kind") != "definition"
        or matcher.get("generation_kind") != "matcher"
        or matcher.get("source_range") is not None
        or matcher.get("line_start") is not None
        or matcher.get("module") != owner.get("module")
        or not isinstance(body_dependencies, list)
        or any(not isinstance(name, str) for name in body_dependencies)
        or matcher.get("name") not in body_dependencies
        or not _exact_parent(matcher, owner)
    ):
        return None
    return owner


def provenance_parent_allowed(row: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> bool:
    """Keep separate provenance restricted to ordinary definitions and their exact matchers."""
    return _source_definition(row) or _matcher_owner(row, profiles) is not None


def imported_origin_owner(
    row: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the source owner of an authenticated imported equation or matcher helper.

    Profiles must first pass the current kernel/axiom policy. A matcher chain is
    exactly child→matcher→ordinary source definition; navigation hints and names
    alone grant no ownership, and the parent profiles remain provenance only.
    """
    parents = row.get("generated_from", [])
    parent = (
        profiles.get(parents[0])
        if isinstance(parents, list) and len(parents) == 1 and isinstance(parents[0], str)
        else None
    )
    if parent is None:
        return None
    if authenticated_imported_equation(row, parent):
        return parent
    owner = _matcher_owner(parent, profiles)
    if (
        owner is None
        or row.get("generation_kind") != "matcher_equation"
        or row.get("kind") not in {"theorem", "definition"}
        or (row.get("kind") == "theorem" and row.get("is_prop") is not True)
        or row.get("source_range") is not None
        or row.get("line_start") is not None
        or not _closed(row)
        or parent.get("name") not in row.get("direct_dependencies", [])
        or not _exact_parent(row, parent)
    ):
        return None
    return owner
