"""Preserve controller setup authority across effectful package acquisition and builds."""

from __future__ import annotations

import copy
import heapq
import json
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.preparation_checkout import (
    GitRead,
    check_checkout,
    checkout_head,
    checkout_sources,
)
from leanflow_cli.formalization.preparation_dependency_pins import (
    authorize_dependency_closure,
    original_requirement_pins,
)
from leanflow_cli.formalization.preparation_fs_inputs import CONFIG, entry_identity
from leanflow_cli.formalization.preparation_inputs import (
    configuration_hashes,
    existing_packages,
    read_configuration,
)
from leanflow_cli.workflows.prover.source import project_sources

_PIN_FIELDS = ("type", "url", "rev", "subDir")


def assert_original_configuration(
    root: Path, plan: dict[str, Any], *, private: bool = False
) -> None:
    """Require unchanged planned inputs before the controller constructs its allowed delta."""
    original_requirement_pins(plan["existing_packages"])
    actual = configuration_hashes(root)
    expected = dict(plan["baseline_config_hashes"])
    if private and expected["lean-toolchain"] is None:
        expected["lean-toolchain"] = actual["lean-toolchain"]
        if (root / "lean-toolchain").read_text().strip() != plan["toolchain"]:
            raise ValueError("Preparation changed the planned compiler before configuration")
    if actual != expected:
        raise ValueError("Original project configuration changed before bounded preparation edits")


def _manifest(root: Path) -> dict[str, Any]:
    """Read a conventional pinned Git manifest without accepting aliased package locations."""
    value = json.loads(contained(root, "lake-manifest.json").read_text())
    if not isinstance(value, dict) or not isinstance(value.get("packages"), list):
        raise ValueError("Preparation dependency manifest is malformed")
    if (
        value.get("packagesDir", ".lake/packages") != ".lake/packages"
        or value.get("lakeDir", ".lake") != ".lake"
    ):
        raise ValueError("Preparation changed the authorized dependency layout")
    for row in value["packages"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*", row["name"])
            or row.get("type") != "git"
            or not isinstance(row.get("url"), str)
            or not isinstance(row.get("rev"), str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", row["rev"])
            or row.get("subDir") not in {None, ""}
        ):
            raise ValueError("Preparation requires conventional full Git package pins")
    # Reuse the requirement reader's exact-name and duplicate checks.
    existing_packages(root, {})
    if set(value) - {"version", "packagesDir", "packages", "name", "lakeDir", "fixedToolchain"}:
        raise ValueError("Preparation dependency manifest has unsupported layout metadata")
    # Lake 1.2.0 manifests record a boolean toolchain policy; Lake reads absence as false.
    if not isinstance(value.get("fixedToolchain", False), bool):
        raise ValueError("Preparation dependency manifest has a malformed fixedToolchain flag")
    return value


def _sources(root: Path, check: Callable[[], None]) -> dict[str, str | None]:
    """Bind project Lean sources allocated by the controller before builds can modify them."""
    return {
        path.relative_to(root).as_posix(): entry_identity(path, check=check)
        for path in project_sources(root)
        if path.name != "lakefile.lean"
    }


def _source_drift_summary(
    before: dict[str, str | None],
    after: dict[str, str | None],
    *,
    check: Callable[[], None],
) -> str:
    """Describe bounded relative-path differences without exposing file contents or weakening pins."""
    counts = {"added": 0, "missing": 0, "changed": 0}

    def differences() -> Iterator[tuple[str, str]]:
        for path, previous in before.items():
            check()
            kind = "missing" if path not in after else "changed"
            if path not in after or after[path] != previous:
                counts[kind] += 1
                yield path, kind
        for path in after:
            check()
            if path not in before:
                counts["added"] += 1
                yield path, "added"

    sample = heapq.nsmallest(10, differences())
    paths = "; ".join(
        kind + " " + json.dumps(path[:160] + ("..." if len(path) > 160 else ""))
        for path, kind in sample
    )
    return (
        "; source identity drift: "
        + ", ".join(f"{kind}={count}" for kind, count in counts.items())
        + f"; first {len(sample)} of {sum(counts.values())} paths: "
        + paths
        + "; changed identities bind bytes, entry type and mode"
    )


@dataclass(frozen=True)
class VerifiedHashCache:
    """Bind a controller-verified derived cache without replacing its original input authority."""

    package: str
    revision: str
    input_path: str
    input_identity: str
    cache_path: str
    cache_identity: str
    hash_value: str
    hash_modes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Expose portable verification evidence without granting receipt data authority."""
        return {
            "package": self.package,
            "revision": self.revision,
            "input_path": self.input_path,
            "input_identity": self.input_identity,
            "cache_path": self.cache_path,
            "cache_identity": self.cache_identity,
            "hash_value": self.hash_value,
            "hash_modes": list(self.hash_modes),
        }


def _hash_input_version(path: Path) -> tuple[int, ...]:
    """Bind one unaliased regular input version throughout a cache admission check."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Verified hash cache requires unaliased regular inputs")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@dataclass(frozen=True)
class PreparationAuthorization:
    """Retain trusted configuration separately from checked acquisition and build outputs."""

    configuration: dict[str, str | None]
    project_sources: dict[str, str | None]
    plan: dict[str, Any]
    baseline_manifest: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    package_sources: dict[str, dict[str, str | None]] | None = None
    checkout_settings: dict[str, dict[str, str | None]] | None = None
    acquisition_root: str | None = None
    verified_hash_caches: tuple[VerifiedHashCache, ...] = ()

    @classmethod
    def capture(
        cls, root: Path, plan: dict[str, Any], *, check: Callable[[], None]
    ) -> PreparationAuthorization:
        """Freeze the bounded controller delta before any effectful package command runs."""
        original_requirement_pins(plan["existing_packages"])
        return cls(
            configuration={
                name: entry_identity(root / name, check=check)
                for name in sorted((CONFIG - {"lake-manifest.json"}) | {".leanflow/project.yaml"})
            },
            project_sources=_sources(root, check),
            plan=copy.deepcopy(plan),
            baseline_manifest=(
                copy.deepcopy(_manifest(root)) if (root / "lake-manifest.json").is_file() else None
            ),
        )

    def validate_configuration(self, root: Path, *, check: Callable[[], None]) -> None:
        """Reject configuration, toolchain, requirement or source changes outside the frozen delta."""
        for name, expected in self.configuration.items():
            if entry_identity(root / name, check=check) != expected:
                raise ValueError("Preparation changed controller-authorized configuration: " + name)
        if _sources(root, check) != self.project_sources:
            raise ValueError("Preparation builds changed controller-authorized project source")
        config = read_configuration(root)
        current = {row["name"]: row for row in existing_packages(root, config)}
        declared = {name for name, row in current.items() if row.get("declared")}
        allowed = (
            {row["name"] for row in self.plan["existing_packages"] if row.get("declared")}
            | {row["name"] for row in self.plan["packages"]}
            | {row["name"] for row in self.plan["catalog_requirements"]}
        )
        if declared != allowed:
            raise ValueError("Preparation changed the authorized dependency requirements")
        original_pins = original_requirement_pins(self.plan["existing_packages"])
        for previous in self.plan["existing_packages"]:
            selected = current.get(previous["name"])
            if selected is None or (
                previous.get("declared") and selected.get("requirement") != previous["requirement"]
            ):
                raise ValueError(
                    "Preparation changed an existing dependency requirement: " + previous["name"]
                )

            pin = previous.get("manifest") or original_pins.get(previous["name"])
            if pin is not None and any(
                (selected.get("manifest") or {}).get(key) != pin.get(key) for key in _PIN_FIELDS
            ):
                raise ValueError(
                    (
                        "Preparation changed an existing dependency pin: "
                        if previous.get("manifest") is not None
                        else "Prepared dependency differs from its original immutable requirement: "
                    )
                    + previous["name"]
                )
            if (
                previous.get("manifest") is None
                and (selected.get("manifest") or {}).get("inputRev")
                != previous["requirement"]["rev"]
            ):
                raise ValueError(
                    "Prepared dependency changed its original immutable input revision: "
                    + previous["name"]
                )
        for requested in self.plan["packages"]:
            selected = current.get(requested["name"], {})
            requirement = selected.get("requirement", {})
            pin = selected.get("manifest") or {}
            if requirement != {
                "name": requested["name"],
                "git": requested["url"],
                "rev": requested["revision"],
            } or any(
                pin.get(key) != value
                for key, value in (
                    ("type", "git"),
                    ("url", requested["url"]),
                    ("rev", requested["revision"]),
                )
            ):
                raise ValueError(
                    "Prepared dependency differs from its authorized plan: " + requested["name"]
                )

    def with_verified_hash_caches(
        self,
        root: Path,
        records: tuple[VerifiedHashCache, ...],
        *,
        check: Callable[[], None],
    ) -> PreparationAuthorization:
        """Admit one independently verified cache set while retaining every original source identity.

        Only the stopped private hash verifier calls this controller-only boundary.
        Receipt or model data is never decoded into preparation authorization.
        """
        if (
            self.verified_hash_caches
            or not isinstance(records, tuple)
            or str(root.resolve()) != self.acquisition_root
        ):
            raise ValueError("Preparation hash caches require one complete verification")
        extended = replace(self, verified_hash_caches=records)
        extended.validate(root, check=check)
        return extended

    def _verified_cache_sources(
        self, root: Path, *, check: Callable[[], None]
    ) -> dict[str, dict[str, str]]:
        """Recheck portable cache and input identities in both staged and published checkouts."""
        if not self.verified_hash_caches:
            return {}
        if self.manifest is None or self.package_sources is None:
            raise ValueError("Verified hash caches lost their original package authority")
        pins = {package["name"]: package["rev"] for package in self.manifest["packages"]}
        result: dict[str, dict[str, str]] = {}
        for cache in self.verified_hash_caches:
            check()
            if (
                not isinstance(cache, VerifiedHashCache)
                or not isinstance(cache.package, str)
                or cache.package not in pins
                or cache.revision != pins[cache.package]
                or not isinstance(cache.hash_value, str)
                or re.fullmatch(r"[0-9a-f]{16}", cache.hash_value) is None
                or not isinstance(cache.hash_modes, tuple)
                or not cache.hash_modes
                or any(
                    not isinstance(mode, str) or mode not in {"binary", "text"}
                    for mode in cache.hash_modes
                )
                or len(cache.hash_modes) != len(set(cache.hash_modes))
                or not isinstance(cache.cache_path, str)
                or not isinstance(cache.input_path, str)
                or cache.cache_path != cache.input_path + ".hash"
                or not isinstance(cache.cache_identity, str)
                or re.fullmatch(r"[0-9a-f]{64}", cache.cache_identity) is None
            ):
                raise ValueError("Invalid controller-verified Lake hash cache")
            original = self.package_sources[cache.package]
            expected = original.get(cache.input_path)
            additions = result.setdefault(cache.package, {})
            if (
                expected is None
                or cache.input_identity != expected
                or cache.cache_path in original
                or cache.cache_path in additions
            ):
                raise ValueError("Verified hash cache cannot replace or extend an unbound input")
            checkout = contained(root, ".lake/packages/" + cache.package)
            source = contained(checkout, cache.input_path)
            path = contained(checkout, cache.cache_path)
            versions = {entry: _hash_input_version(entry) for entry in (source, path)}
            if (
                read_regular_bytes(path, 16).decode("ascii") != cache.hash_value
                or entry_identity(path, check=check) != cache.cache_identity
                or entry_identity(source, check=check) != cache.input_identity
            ):
                raise ValueError("Verified hash cache or its original input changed")
            if any(_hash_input_version(entry) != before for entry, before in versions.items()):
                raise ValueError("Verified hash cache inputs changed while checking")
            additions[cache.cache_path] = cache.cache_identity
        return result

    def acquire(
        self, root: Path, *, git: GitRead, check: Callable[[], None]
    ) -> PreparationAuthorization:
        """Authorize resolved pins through preserved baseline and clean pinned dependency manifests."""
        self.validate_configuration(root, check=check)
        manifest = _manifest(root)
        if self.baseline_manifest is not None and {
            key: value for key, value in manifest.items() if key != "packages"
        } != {key: value for key, value in self.baseline_manifest.items() if key != "packages"}:
            raise ValueError("Preparation acquisition changed existing manifest configuration")
        # Lake writes the root's configured flag, which screened configurations leave false.
        if self.baseline_manifest is None and manifest.get("fixedToolchain", False):
            raise ValueError("Preparation acquisition set an unauthorized fixedToolchain flag")
        if manifest.get("name", read_configuration(root).get("name")) != read_configuration(
            root
        ).get("name"):
            raise ValueError("Preparation manifest changed the authorized project name")
        packages = {row["name"]: row for row in manifest["packages"]}
        inputs = {}
        settings = {}
        authorized = {row["name"] for row in self.plan["existing_packages"]} | {
            row["name"] for row in self.plan["packages"]
        }
        for request in self.plan["catalog_requirements"]:
            name = request["name"]
            requirement: dict[str, Any] = next(
                (row for row in read_configuration(root).get("require", []) if row["name"] == name),
                {},
            )
            package = packages.get(name, {})
            if requirement.get("git") != request["url"] or package.get("url") != request["url"]:
                raise ValueError("Prepared catalog dependency changed its authorized source")
            revision = requirement.get("rev", "")
            from leanflow_cli.workflows.project_setup.verification import resolve_repl_revision

            if (
                not isinstance(revision, str)
                or not revision
                or resolve_repl_revision(
                    self.plan["toolchain"].rsplit(":", 1)[-1], tags=[revision]
                )["rev"]
                != revision
            ):
                raise ValueError("Prepared catalog dependency has an incompatible release")
            checkout = check_checkout(root, package, git)
            if git(checkout, ["rev-parse", revision + "^{commit}"]).strip() != package["rev"]:
                raise ValueError("Prepared catalog checkout differs from its authorized release")
            authorized.add(name)
        dependency_pins: dict[str, list[dict[str, Any]]] = {}
        for name, package in packages.items():
            check()
            checkout = check_checkout(root, package, git)
            inputs[name] = checkout_sources(checkout, check)
            settings[name] = {
                relative: entry_identity(checkout / relative, check=check)
                for relative in (".git/config", ".git/hooks")
            }
            path = checkout / "lake-manifest.json"
            if path.is_file():
                git(checkout, ["ls-files", "--error-unmatch", "lake-manifest.json"])
            dependency_pins[name] = _manifest(checkout)["packages"] if path.is_file() else []
        authorize_dependency_closure(
            packages,
            dependency_pins,
            authorized,
            [row["name"] for row in read_configuration(root).get("require", [])],
            check=check,
        )
        return PreparationAuthorization(
            self.configuration,
            self.project_sources,
            self.plan,
            self.baseline_manifest,
            copy.deepcopy(manifest),
            inputs,
            settings,
            str(root.resolve()),
        )

    def validate(
        self, root: Path, *, check: Callable[[], None], git: GitRead | None = None
    ) -> None:
        """Repeat independent authority checks after builds, verified cleanup and at commitment."""
        self.validate_configuration(root, check=check)
        if (
            self.manifest is None
            or self.package_sources is None
            or _manifest(root) != self.manifest
        ):
            raise ValueError("Preparation changed the authorized resolved package manifest")
        directory = contained(root, ".lake/packages")
        if directory.is_dir() and {path.name for path in directory.iterdir()} != set(
            self.package_sources
        ):
            raise ValueError("Preparation changed the authorized package directory membership")
        verified_caches = self._verified_cache_sources(root, check=check)
        for package in self.manifest["packages"]:
            checkout = (
                check_checkout(root, package, git)
                if git is not None
                else checkout_head(root, package)
            )
            current_sources = checkout_sources(checkout, check)
            expected_sources = {
                **self.package_sources[package["name"]],
                **verified_caches.get(package["name"], {}),
            }
            if current_sources != expected_sources:
                raise ValueError(
                    "Preparation builds changed pinned dependency source: "
                    + package["name"]
                    + _source_drift_summary(expected_sources, current_sources, check=check)
                )
            existing = {row["name"] for row in self.plan["existing_packages"]}
            if str(root.resolve()) == self.acquisition_root or package["name"] not in existing:
                if self.checkout_settings is None or any(
                    entry_identity(checkout / relative, check=check) != expected
                    for relative, expected in self.checkout_settings[package["name"]].items()
                ):
                    raise ValueError(
                        "Preparation builds changed package Git configuration or hooks"
                    )
