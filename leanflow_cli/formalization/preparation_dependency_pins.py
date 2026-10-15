"""Authorize transitive pins using preserved roots and Lake's root-first dependency priority."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

PIN_FIELDS = ("type", "url", "rev", "subDir")


def original_requirement_pins(existing: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bind unresolved existing roots to original immutable requirements before any acquisition."""
    result = {}
    for previous in existing:
        if previous.get("manifest") is not None:
            continue
        requirement = previous.get("requirement", {})
        if (
            not previous.get("declared")
            or not isinstance(requirement.get("git"), str)
            or not requirement["git"]
            or not isinstance(requirement.get("rev"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", requirement["rev"]) is None
            or requirement.get("subDir") not in {None, ""}
            or requirement.get("path") is not None
            or requirement.get("scope") is not None
        ):
            raise ValueError(
                "Existing dependency has no original immutable Git authority: "
                + previous["name"]
                + "; restore its original pinned manifest or provide a conventional explicit Git URL "
                "and full 40-character commit before preparation. Symbolic revisions cannot be "
                "authorized from acquisition output"
            )
        result[previous["name"]] = {
            "name": previous["name"],
            "type": "git",
            "url": requirement["git"],
            "rev": requirement["rev"],
            "subDir": requirement.get("subDir"),
        }
    return result


def authorize_dependency_closure(
    packages: Mapping[str, dict[str, Any]],
    dependency_pins: Mapping[str, list[dict[str, Any]]],
    authorized_roots: set[str],
    root_requirements: Sequence[str],
    *,
    check: Callable[[], None],
) -> set[str]:
    """Require first-priority parent pins while preserving independently checked root overrides.

    The caller must first validate every authorized root against its original pin,
    planned full revision or checked catalog release. Parent manifests must come
    from clean, committed, pinned checkouts. Acquired output alone is not authority.
    Lake loads direct roots in reverse order and locks their manifest entries
    before resolving deeper requirements. Support only complete flattened root
    manifests: new descendants cannot gain authority from a guessed package order
    or an unused baseline package. A declared root override owns its own closure.
    """
    if authorized_roots - packages.keys():
        raise ValueError("Preparation lost an authorized dependency root")
    direct = list(dict.fromkeys(reversed(root_requirements)))
    if set(direct) - authorized_roots:
        raise ValueError("Preparation has an unauthorized root dependency requirement")
    authorized = set(authorized_roots)
    global_roots = set(direct)
    for root in direct:
        check()
        pins = dependency_pins.get(root, [])
        flattened = {pin["name"] for pin in pins}
        permitted = flattened | authorized_roots
        # Authorize only direct-root lock entries, in Lake's root priority order.
        for pin in pins:
            check()
            name = pin["name"]
            if name in authorized:
                # The independently checked root or an earlier pinned parent wins.
                continue
            selected = packages.get(name)
            if selected is None or any(selected.get(key) != pin.get(key) for key in PIN_FIELDS):
                raise ValueError("Resolved transitive dependency differs from its pinned parent")
            authorized.add(name)
        pending = deque([root])
        visited: set[str] = set()
        while pending:
            check()
            parent = pending.popleft()
            if parent in visited:
                continue
            visited.add(parent)
            for pin in dependency_pins.get(parent, []):
                check()
                name = pin["name"]
                if name in global_roots and name != root:
                    continue
                if name not in permitted or name not in packages:
                    raise ValueError(
                        "Dependency root has an incomplete or stale flattened manifest: "
                        + root
                        + "; pin every inherited descendant in that root's committed manifest "
                        "or independently authorize an explicit root dependency before preparation"
                    )
                if name not in visited:
                    pending.append(name)
    if packages.keys() - authorized:
        raise ValueError("Preparation acquired packages outside the authorized dependency closure")
    return authorized
