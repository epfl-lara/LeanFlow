"""Preserve root overrides without letting acquisition or deeper manifests choose new pins."""

from __future__ import annotations

from copy import deepcopy

import pytest

from leanflow_cli.formalization.preparation_dependency_pins import authorize_dependency_closure


def _pin(name: str, revision: str) -> dict[str, str | None]:
    """Create a concrete, fully pinned Git dependency record."""
    return {
        "name": name,
        "type": "git",
        "url": "https://example.invalid/" + name,
        "rev": revision * 40,
        "subDir": None,
    }


def test_explicit_preserved_root_override_wins_over_deeper_manifest():
    packages = {"parent": _pin("parent", "a"), "shared": _pin("shared", "b")}
    authorized = authorize_dependency_closure(
        packages,
        {"parent": [_pin("shared", "c")]},
        {"parent", "shared"},
        ["parent", "shared"],
        check=lambda: None,
    )
    assert authorized == {"parent", "shared"}


@pytest.mark.parametrize(
    "requirements, expected", [(["first", "last"], "b"), (["last", "first"], "a")]
)
def test_later_root_requirements_select_the_first_authoritative_manifest(requirements, expected):
    packages = {name: _pin(name, "f") for name in ("first", "last")}
    packages["shared"] = _pin("shared", expected)
    parents = {"first": [_pin("shared", "a")], "last": [_pin("shared", "b")]}
    assert authorize_dependency_closure(
        packages, parents, {"first", "last"}, requirements, check=lambda: None
    ) == set(packages)
    packages["shared"]["rev"] = "c" * 40
    with pytest.raises(ValueError, match="differs from its pinned parent"):
        authorize_dependency_closure(
            packages, parents, {"first", "last"}, requirements, check=lambda: None
        )


def test_first_parent_flattened_pin_wins_over_deeper_pin_but_unbound_edits_fail():
    packages = {
        name: _pin(name, revision)
        for name, revision in (("root", "a"), ("middle", "b"), ("leaf", "c"))
    }
    parents = {
        "root": [_pin("middle", "b"), _pin("leaf", "c")],
        "middle": [_pin("leaf", "d")],
    }
    assert authorize_dependency_closure(
        packages, parents, {"root"}, ["root"], check=lambda: None
    ) == set(packages)
    tampered = deepcopy(packages)
    tampered["leaf"]["rev"] = "d" * 40
    with pytest.raises(ValueError, match="differs from its pinned parent"):
        authorize_dependency_closure(tampered, parents, {"root"}, ["root"], check=lambda: None)


@pytest.mark.parametrize("change", ["missing", "revision", "url", "extra", "unauthorized_root"])
def test_unapproved_acquisition_changes_remain_blocked(change):
    packages = {"parent": _pin("parent", "a"), "child": _pin("child", "b")}
    requirements = ["parent"]
    if change == "missing":
        packages.pop("child")
    elif change == "revision":
        packages["child"]["rev"] = "c" * 40
    elif change == "url":
        packages["child"]["url"] = "https://example.invalid/replaced"
    elif change == "extra":
        packages["extra"] = _pin("extra", "d")
    else:
        requirements.append("child")
    with pytest.raises(ValueError):
        authorize_dependency_closure(
            packages, {"parent": [_pin("child", "b")]}, {"parent"}, requirements, check=lambda: None
        )


def test_cyclic_pinned_parent_references_are_bounded_and_cancellation_propagates():
    packages = {"parent": _pin("parent", "a"), "child": _pin("child", "b")}
    parents = {"parent": [_pin("child", "b")], "child": [_pin("parent", "a")]}
    checks = []
    assert authorize_dependency_closure(
        packages, parents, {"parent"}, ["parent"], check=lambda: checks.append(True)
    ) == set(packages)
    assert len(checks) <= 2 * (len(packages) + sum(map(len, parents.values())))

    def cancelled():
        raise InterruptedError("stop")

    with pytest.raises(InterruptedError, match="stop"):
        authorize_dependency_closure(packages, parents, {"parent"}, ["parent"], check=cancelled)


@pytest.mark.parametrize("existing_inherited", [False, True])
@pytest.mark.parametrize("selected", ["c", "d"])
def test_incomplete_flattened_manifest_cannot_guess_deeper_reverse_dependency_priority(
    existing_inherited, selected
):
    packages = {
        name: _pin(name, revision)
        for name, revision in (("root", "a"), ("first", "b"), ("last", "e"), ("shared", selected))
    }
    parents = {
        "root": [_pin("first", "b"), _pin("last", "e")],
        "first": [_pin("shared", "c")],
        "last": [_pin("shared", "d")],
    }
    roots = {"root", "first", "last"} if existing_inherited else {"root"}
    with pytest.raises(ValueError, match="incomplete or stale flattened manifest"):
        authorize_dependency_closure(packages, parents, roots, ["root"], check=lambda: None)


def test_unused_baseline_package_cannot_grant_new_inherited_descendants():
    packages = {
        name: _pin(name, revision)
        for name, revision in (("root", "a"), ("unused", "b"), ("unrequested", "c"))
    }
    parents = {"unused": [_pin("unrequested", "c")]}
    with pytest.raises(ValueError, match="outside the authorized dependency closure"):
        authorize_dependency_closure(
            packages, parents, {"root", "unused"}, ["root"], check=lambda: None
        )


@pytest.mark.parametrize("requirements", [["root", "override"], ["override", "root"]])
def test_explicit_global_root_override_owns_its_new_descendant_closure(requirements):
    packages = {
        name: _pin(name, revision)
        for name, revision in (("root", "a"), ("override", "b"), ("new_child", "c"))
    }
    parents = {"root": [_pin("override", "d")], "override": [_pin("new_child", "c")]}
    assert authorize_dependency_closure(
        packages, parents, {"root", "override"}, requirements, check=lambda: None
    ) == set(packages)
