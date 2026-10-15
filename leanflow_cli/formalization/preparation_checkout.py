"""Check pinned Git checkout identity and retain source inputs across effectful builds."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity

GitRead = Callable[[Path, list[str]], str]


def checkout_sources(root: Path, check: Callable[[], None]) -> dict[str, str | None]:
    """Bind every checkout input outside Git metadata and conventional Lake outputs."""
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in {".git", ".lake"})
        links = [name for name in directories if (Path(current) / name).is_symlink()]
        directories[:] = [name for name in directories if name not in links]
        for name in sorted([*files, *links]):
            if name == ".git":
                continue
            path = Path(current) / name
            result[path.relative_to(root).as_posix()] = entry_identity(
                path, check=check, link_root=root
            )
    return result


def check_checkout(root: Path, package: dict[str, Any], git: GitRead) -> Path:
    """Require the actual package checkout and clean source at its authorized full commit."""
    checkout = contained(root, ".lake/packages/" + package["name"])
    if (
        not checkout.is_dir()
        or not (checkout / ".git").is_dir()
        or (checkout / ".git").is_symlink()
    ):
        raise ValueError(
            "Prepared dependency is not an independent Git checkout: " + package["name"]
        )
    if Path(git(checkout, ["rev-parse", "--show-toplevel"]).strip()).resolve() != checkout:
        raise ValueError("Prepared dependency Git worktree escaped its package directory")
    if git(checkout, ["rev-parse", "HEAD"]).strip() != package["rev"]:
        raise ValueError("Prepared checkout differs from its authorized pin: " + package["name"])
    if any(
        line and (line[0].islower() or line[0] == "S")
        for line in git(checkout, ["ls-files", "-v"]).splitlines()
    ):
        raise ValueError("Prepared dependency hides tracked source from cleanliness checks")
    if git(checkout, ["status", "--porcelain", "--untracked-files=all"]).strip():
        raise ValueError("Prepared dependency has unbound source edits: " + package["name"])
    return checkout


def checkout_head(root: Path, package: dict[str, Any]) -> Path:
    """Read the pinned HEAD without running package-configured Git helpers at commitment."""
    checkout = contained(root, ".lake/packages/" + package["name"])
    metadata = contained(checkout, ".git")
    if not metadata.is_dir():
        raise ValueError("Prepared dependency lost its independent Git metadata")
    value = contained(metadata, "HEAD").read_text().strip()
    if value.startswith("ref: "):
        reference = value.removeprefix("ref: ")
        if not reference.startswith("refs/"):
            raise ValueError("Prepared dependency has unsupported Git HEAD authority")
        path = contained(metadata, reference)
        if path.is_file():
            value = path.read_text().strip()
        else:
            packed = contained(metadata, "packed-refs")
            values = (
                [
                    line.split()[0]
                    for line in packed.read_text().splitlines()
                    if len(line.split()) == 2 and line.split()[1] == reference
                ]
                if packed.is_file()
                else []
            )
            if len(values) != 1:
                raise ValueError("Prepared dependency Git HEAD is unresolved")
            value = values[0]
    if value != package["rev"]:
        raise ValueError("Prepared checkout differs from its authorized pin: " + package["name"])
    return checkout
