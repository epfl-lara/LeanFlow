"""Reject build-created setup authority with clean Git fixtures and real sandboxed Lake targets."""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization.preparation_authorization import (
    PreparationAuthorization,
    VerifiedHashCache,
    assert_original_configuration,
)
from leanflow_cli.formalization.preparation_delta import infrastructure_baseline, managed_delta
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.preparation_inputs import (
    configuration_hashes,
    existing_packages,
    read_configuration,
)
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.formalization.preparation_stage import (
    build_prepared_dependencies,
    copy_stage,
    preparation_git_reader,
)
from leanflow_cli.formalization.preparation_transaction import (
    commit_preparation,
    preparation_directory,
    protected_inputs,
)
from leanflow_cli.workflows.project_setup.execution import build_setup_execution_policy
from leanflow_cli.workflows.project_setup.process import run_command


def _git(checkout: Path, arguments: list[str]) -> str:
    """Run trusted fixture Git operations without user hooks or configuration."""
    result = subprocess.run(
        ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null", *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout


def _fixture(
    root: Path, *, effect: str = "benign", toolchain: str = "leanprover/lean4:v4.33.1"
) -> dict[str, Any]:
    root.mkdir()
    package = root / ".lake/packages/sample"
    package.mkdir(parents=True)
    (root / "lean-toolchain").write_text(toolchain + "\n")
    (root / "Main.lean").write_text("import Sample\n")
    (package / ".gitignore").write_text(".lake/\n")
    (package / "Sample.lean").write_text("def fixtureValue : Nat := 1\n")
    change = {
        "benign": 'IO.FS.writeFile (root / ".lake/fixture-effect") "benign"',
        "pin": 'let path := root / "lake-manifest.json"\n  let text ← IO.FS.readFile path\n  IO.FS.writeFile path (text.replace "AUTHORIZED_REVISION" "'
        + "b" * 40
        + '")',
        "requirement": 'let path := root / "lakefile.toml"\n  let text ← IO.FS.readFile path\n  IO.FS.writeFile path (text.replace "https://example.invalid/sample" "https://example.invalid/replaced")',
    }[effect]
    # The full pin is substituted after committing, without changing the package
    # source: read the actual authorized pin from the root manifest at execution.
    if effect == "pin":
        change = (
            'let path := root / "lake-manifest.json"\n'
            "  let text ← IO.FS.readFile path\n"
            "  let parsed ← IO.ofExcept (Lean.Json.parse text)\n"
            '  let packages ← IO.ofExcept (parsed.getObjValAs? (Array Lean.Json) "packages")\n'
            '  let revision ← IO.ofExcept (packages[0]!.getObjValAs? String "rev")\n'
            '  IO.FS.writeFile path (text.replace revision "' + "b" * 40 + '")'
        )
    (package / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage sample\nlean_lib Sample\n"
        "@[default_target]\ntarget fixtureEffect pkg : Unit := do\n"
        '  let root := pkg.dir / ".." / ".." / ".."\n  ' + change + "\n  return Job.pure ()\n"
    )
    _git(package, ["init", "--quiet"])
    _git(package, ["add", "."])
    _git(
        package,
        [
            "-c",
            "user.name=Preparation Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    )
    revision = _git(package, ["rev-parse", "HEAD"]).strip()
    (root / "lakefile.toml").write_text(
        'name = "fixture"\ndefaultTargets = ["Main"]\n[[lean_lib]]\nname = "Main"\n'
        '[[require]]\nname = "sample"\ngit = "https://example.invalid/sample"\nrev = "'
        + revision
        + '"\n'
    )
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "name": "fixture",
                "lakeDir": ".lake",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "name": "sample",
                        "type": "git",
                        "url": "https://example.invalid/sample",
                        "rev": revision,
                        "inputRev": revision,
                        "subDir": None,
                        "inherited": False,
                        "scope": "",
                        "manifestFile": "lake-manifest.json",
                        "configFile": "lakefile.lean",
                    }
                ],
            }
        )
    )
    return {
        "existing_packages": existing_packages(root, read_configuration(root)),
        "packages": [],
        "catalog_requirements": [],
        "toolchain": toolchain,
    }


def _lake_fixture(
    root: Path,
    *,
    root_metadata: dict[str, Any] | None = None,
    dependency_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-pin the fixture to Lake v4.33.1 manifests, including a tracked dependency manifest."""
    plan = _fixture(root)
    package = root / ".lake/packages/sample"
    (package / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [],
                "name": "sample",
                "lakeDir": ".lake",
                "fixedToolchain": False,
                **(dependency_metadata or {}),
            }
        )
    )
    _git(package, ["add", "lake-manifest.json"])
    _git(
        package,
        [
            "-c",
            "user.name=Preparation Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "manifest",
        ],
    )
    revision = _git(package, ["rev-parse", "HEAD"]).strip()
    previous = plan["existing_packages"][0]["manifest"]["rev"]
    config = root / "lakefile.toml"
    config.write_text(config.read_text().replace(previous, revision))
    manifest = json.loads((root / "lake-manifest.json").read_text())
    manifest["packages"][0].update(rev=revision, inputRev=revision)
    manifest.update({"version": "1.2.0", "fixedToolchain": False, **(root_metadata or {})})
    (root / "lake-manifest.json").write_text(json.dumps(manifest))
    return {**plan, "existing_packages": existing_packages(root, read_configuration(root))}


def _verified_cache_fixture(root: Path):
    """Bind a real pinned Git input before constructing a separate portable cache record."""
    plan = _lake_fixture(root)
    package = root / ".lake/packages/sample"
    (package / ".gitignore").write_text(".lake/\n*.hash\n")
    source = package / "Lock.txt"
    source.write_bytes(b"alpha\r\nbeta\r\n")
    _git(package, ["add", "."])
    _git(
        package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "bind cache input",
        ],
    )
    revision = _git(package, ["rev-parse", "HEAD"]).strip()
    previous = plan["existing_packages"][0]["manifest"]["rev"]
    for name in ("lakefile.toml", "lake-manifest.json"):
        path = root / name
        path.write_text(path.read_text().replace(previous, revision))
    plan["existing_packages"] = existing_packages(root, read_configuration(root))
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    sidecar = package / "Lock.txt.hash"
    sidecar.write_text("36292790ba69fd38")
    assert authority.package_sources is not None
    record = VerifiedHashCache(
        package="sample",
        revision=revision,
        input_path="Lock.txt",
        input_identity=str(authority.package_sources["sample"]["Lock.txt"]),
        cache_path="Lock.txt.hash",
        cache_identity=str(entry_identity(sidecar)),
        hash_value="36292790ba69fd38",
        hash_modes=("text",),
    )
    return authority, record, source, sidecar


def test_verified_hash_cache_extends_only_named_infrastructure_without_rewriting_sources(tmp_path):
    root = tmp_path / "project"
    authority, record, source, sidecar = _verified_cache_fixture(root)
    original = json.loads(json.dumps(authority.package_sources))
    with pytest.raises(ValueError, match="changed pinned dependency source"):
        authority.validate(root, git=_git, check=lambda: None)
    extended = authority.with_verified_hash_caches(root, (record,), check=lambda: None)
    assert extended is not authority and extended.package_sources is authority.package_sources
    assert authority.package_sources == original and authority.verified_hash_caches == ()
    assert extended.verified_hash_caches == (record,)
    assert json.loads(json.dumps(record.to_dict()))["cache_path"] == "Lock.txt.hash"
    extended.validate(root, git=_git, check=lambda: None)
    assert sidecar.read_text() == record.hash_value
    assert source.read_bytes() == b"alpha\r\nbeta\r\n"


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "overlap",
        "foreign_package",
        "foreign_pin",
        "unknown_input",
        "input_identity",
        "cache_identity",
        "cache_value",
        "no_mode",
        "unknown_mode",
        "duplicate_mode",
        "escaping_cache",
    ],
)
def test_hash_cache_extension_rejects_records_outside_exact_original_inputs(tmp_path, change):
    root = tmp_path / "project"
    authority, record, _, sidecar = _verified_cache_fixture(root)
    modifications = {
        "overlap": {"cache_path": "Lock.txt"},
        "foreign_package": {"package": "unlisted"},
        "foreign_pin": {"revision": "f" * 40},
        "unknown_input": {"input_path": "Unknown.txt", "cache_path": "Unknown.txt.hash"},
        "input_identity": {"input_identity": "0" * 64},
        "cache_identity": {"cache_identity": "0" * 64},
        "cache_value": {"hash_value": "0" * 16},
        "no_mode": {"hash_modes": ()},
        "unknown_mode": {"hash_modes": ("unverified",)},
        "duplicate_mode": {"hash_modes": ("text", "text")},
        "escaping_cache": {"cache_path": "../Lock.txt.hash"},
    }
    records = (
        (record, record) if change == "duplicate" else (replace(record, **modifications[change]),)
    )
    with pytest.raises(ValueError):
        authority.with_verified_hash_caches(root, records, check=lambda: None)
    assert authority.verified_hash_caches == () and sidecar.read_text() == "36292790ba69fd38"


@pytest.mark.parametrize(
    "change",
    [
        "input_bytes",
        "input_mode",
        "cache_bytes",
        "cache_mode",
        "cache_missing",
        "pin",
        "configuration",
        "git_hooks",
        "extra_input",
    ],
)
def test_extended_authority_still_rejects_every_original_or_verified_cache_change(tmp_path, change):
    root = tmp_path / "project"
    authority, record, source, sidecar = _verified_cache_fixture(root)
    extended = authority.with_verified_hash_caches(root, (record,), check=lambda: None)
    if change == "input_bytes":
        source.write_bytes(b"changed input")
    elif change == "input_mode":
        source.chmod(source.stat().st_mode ^ 0o020)
    elif change == "cache_bytes":
        sidecar.write_text("60224162a80c236f")
    elif change == "cache_mode":
        sidecar.chmod(sidecar.stat().st_mode ^ 0o020)
    elif change == "cache_missing":
        sidecar.unlink()
    elif change == "pin":
        (source.parent / ".git/HEAD").write_text("b" * 40 + "\n")
    elif change == "configuration":
        (root / "lakefile.toml").write_text('name = "changed"\n')
    elif change == "git_hooks":
        (source.parent / ".git/hooks/pre-commit").write_text("unexpected hook")
    else:
        (source.parent / "Extra.hash").write_text(record.hash_value)
    with pytest.raises((ValueError, OSError)):
        extended.validate(root, git=_git, check=lambda: None)
    assert authority.verified_hash_caches == ()


def test_verified_cache_extension_cannot_be_repeated_or_merged_from_old_records(tmp_path):
    root = tmp_path / "project"
    authority, record, _, _ = _verified_cache_fixture(root)
    extended = authority.with_verified_hash_caches(root, (record,), check=lambda: None)
    with pytest.raises(ValueError):
        extended.with_verified_hash_caches(root, (record,), check=lambda: None)


@pytest.mark.parametrize("aliased", ["cache", "input"])
def test_hash_cache_admission_rejects_a_late_outside_hardlink(tmp_path, monkeypatch, aliased):
    """Keep initial single-link evidence valid through all cache and input reads."""
    from leanflow_cli.formalization import preparation_authorization as authorization

    root = tmp_path / "project"
    authority, record, source, sidecar = _verified_cache_fixture(root)
    alias = tmp_path / "independent-alias"
    original = authorization.read_regular_bytes

    def link_before_read(path, limit):
        if path == sidecar and not alias.exists():
            os.link(sidecar if aliased == "cache" else source, alias)
        return original(path, limit)

    monkeypatch.setattr(authorization, "read_regular_bytes", link_before_read)
    with pytest.raises((ValueError, OSError)):
        authority.with_verified_hash_caches(root, (record,), check=lambda: None)
    assert alias.exists() and authority.verified_hash_caches == ()
    assert source.read_bytes() == b"alpha\r\nbeta\r\n"
    assert sidecar.read_text() == record.hash_value


def test_preexisting_cache_cannot_be_reclassified_as_new_verified_infrastructure(tmp_path):
    root = tmp_path / "project"
    authority, record, _, _ = _verified_cache_fixture(root)
    captured = PreparationAuthorization.capture(root, authority.plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    with pytest.raises(ValueError):
        captured.with_verified_hash_caches(root, (record,), check=lambda: None)


def test_original_planned_inputs_are_rechecked_after_ownership_and_copy(tmp_path):
    root = tmp_path / "project"
    plan = _fixture(root)
    plan["baseline_config_hashes"] = configuration_hashes(root)
    assert_original_configuration(root, plan)
    (root / "lakefile.toml").write_text('name = "changed"\n')
    with pytest.raises(ValueError, match="Original project configuration changed"):
        assert_original_configuration(root, plan)


def test_acquisition_preserves_a_real_root_override_of_a_pinned_parent(tmp_path):
    root = tmp_path / "project"
    _lake_fixture(root)
    shared = root / ".lake/packages/shared"
    shared.mkdir()
    (shared / "lakefile.toml").write_text('name = "shared"\n')
    (shared / "Shared.lean").write_text("def sharedValue : Nat := 2\n")
    _git(shared, ["init", "--quiet"])
    _git(shared, ["add", "."])
    _git(
        shared,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "shared",
        ],
    )
    revision = _git(shared, ["rev-parse", "HEAD"]).strip()
    package = root / ".lake/packages/sample"
    parent_manifest = package / "lake-manifest.json"
    parent = json.loads(parent_manifest.read_text())
    pin = {
        "name": "shared",
        "type": "git",
        "url": "https://example.invalid/shared",
        "rev": "f" * 40,
        "subDir": None,
    }
    parent["packages"] = [pin]
    parent_manifest.write_text(json.dumps(parent))
    _git(package, ["add", "lake-manifest.json"])
    _git(
        package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "parent",
        ],
    )
    parent_revision = _git(package, ["rev-parse", "HEAD"]).strip()
    config = root / "lakefile.toml"
    old = json.loads((root / "lake-manifest.json").read_text())
    config.write_text(
        config.read_text().replace(old["packages"][0]["rev"], parent_revision)
        + '\n[[require]]\nname = "shared"\ngit = "https://example.invalid/shared"\nrev = "'
        + revision
        + '"\n'
    )
    old["packages"][0].update(rev=parent_revision, inputRev=parent_revision)
    old["packages"].append({**pin, "rev": revision})
    manifest = root / "lake-manifest.json"
    manifest.write_text(json.dumps(old))
    plan = {
        "existing_packages": existing_packages(root, read_configuration(root)),
        "packages": [],
        "catalog_requirements": [],
        "toolchain": "leanprover/lean4:v4.33.1",
    }
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None)
    acquired = authority.acquire(root, git=_git, check=lambda: None)
    acquired.validate(root, git=_git, check=lambda: None)
    old["packages"][1]["rev"] = "f" * 40
    manifest.write_text(json.dumps(old))
    with pytest.raises(ValueError, match="existing dependency pin"):
        authority.acquire(root, git=_git, check=lambda: None)


@pytest.mark.parametrize("change", [None, "url", "revision", "extra"])
def test_added_requirements_match_authorized_plan_before_freezing_resolved_outputs(
    tmp_path, change
):
    root = tmp_path / "project"
    plan = _fixture(root)
    package = plan["existing_packages"][0]["manifest"]
    plan["existing_packages"] = []
    plan["packages"] = [{"name": "sample", "url": package["url"], "revision": package["rev"]}]
    path = root / "lakefile.toml"
    if change == "url":
        path.write_text(
            path.read_text().replace(package["url"], "https://example.invalid/replaced")
        )
    elif change == "revision":
        path.write_text(path.read_text().replace(package["rev"], "b" * 40))
    elif change == "extra":
        path.write_text(
            path.read_text()
            + '\n[[require]]\nname = "extra"\ngit = "https://example.invalid/extra"\nrev = "'
            + "b" * 40
            + '"\n'
        )
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None)
    if change is None:
        authority.acquire(root, git=_git, check=lambda: None).validate(root, check=lambda: None)
    else:
        with pytest.raises(ValueError):
            authority.acquire(root, git=_git, check=lambda: None)


@pytest.mark.parametrize("change", [None, "url", "revision", "input_ref", "subdir"])
def test_missing_manifest_existing_requirement_retains_original_git_authority(tmp_path, change):
    root = tmp_path / "project"
    _lake_fixture(root)
    path = root / "lake-manifest.json"
    acquired_manifest = json.loads(path.read_text())
    path.unlink()
    plan = {
        "existing_packages": existing_packages(root, read_configuration(root)),
        "packages": [],
        "catalog_requirements": [],
        "toolchain": "leanprover/lean4:v4.33.1",
    }
    assert plan["existing_packages"][0]["manifest"] is None
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None)
    package = acquired_manifest["packages"][0]
    if change == "url":
        package["url"] = "https://example.invalid/unapproved"
    elif change == "revision":
        checkout = root / ".lake/packages/sample"
        (checkout / "Sample.lean").write_text("def fixtureValue : Nat := 7\n")
        _git(checkout, ["add", "Sample.lean"])
        _git(
            checkout,
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "unapproved acquisition",
            ],
        )
        package["rev"] = _git(checkout, ["rev-parse", "HEAD"]).strip()
    elif change == "input_ref":
        package["inputRev"] = "moving-ref"
    elif change == "subdir":
        package["subDir"] = "alternate"
    path.write_text(json.dumps(acquired_manifest))
    if change is None:
        authority.acquire(root, git=_git, check=lambda: None).validate(
            root, git=_git, check=lambda: None
        )
    else:
        with pytest.raises(ValueError, match="original immutable"):
            authority.acquire(root, git=_git, check=lambda: None)


@pytest.mark.parametrize("revision", ["main", "v4.33.1", "", "abcdef"])
def test_missing_manifest_symbolic_existing_requirement_fails_before_acquisition(
    tmp_path, revision
):
    root = tmp_path / "project"
    _lake_fixture(root)
    path = root / "lake-manifest.json"
    manifest = json.loads(path.read_text())
    path.unlink()
    configuration = root / "lakefile.toml"
    configuration.write_text(
        configuration.read_text().replace(manifest["packages"][0]["rev"], revision)
    )
    plan = {
        "existing_packages": existing_packages(root, read_configuration(root)),
        "packages": [],
        "catalog_requirements": [],
        "toolchain": "leanprover/lean4:v4.33.1",
        "baseline_config_hashes": configuration_hashes(root),
    }
    before = configuration.read_bytes()
    with pytest.raises(ValueError, match="before preparation.*Symbolic revisions"):
        assert_original_configuration(root, plan)
    with pytest.raises(ValueError, match="original immutable Git authority"):
        PreparationAuthorization.capture(root, plan, check=lambda: None)
    assert configuration.read_bytes() == before and not path.exists()


@pytest.mark.parametrize(
    "change",
    [
        "pin",
        "requirement",
        "toolchain",
        "layout",
        "source",
        "hidden_dirty_source",
        "git_hooks",
        "head",
        "ignored_source",
        "unlisted_package",
    ],
)
def test_frozen_setup_authority_rejects_post_build_drift(tmp_path, change):
    root = tmp_path / "project"
    plan = _fixture(root)
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    package = root / ".lake/packages/sample"
    if change == "pin":
        manifest = json.loads((root / "lake-manifest.json").read_text())
        manifest["packages"][0]["rev"] = "b" * 40
        (root / "lake-manifest.json").write_text(json.dumps(manifest))
    elif change == "requirement":
        path = root / "lakefile.toml"
        path.write_text(
            path.read_text().replace("example.invalid/sample", "example.invalid/replaced")
        )
    elif change == "toolchain":
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc1\n")
    elif change == "layout":
        path = root / "lakefile.toml"
        path.write_text(path.read_text().replace('["Main"]', "[]"))
    elif change == "source":
        (package / "Sample.lean").write_text("def fixtureValue : Nat := 2\n")
    elif change == "hidden_dirty_source":
        _git(package, ["update-index", "--assume-unchanged", "Sample.lean"])
        (package / "Sample.lean").write_text("def fixtureValue : Nat := 2\n")
        assert _git(package, ["status", "--porcelain"]).strip() == ""
    elif change == "git_hooks":
        (package / ".git/hooks/post-checkout").write_text("unapproved command")
    elif change == "head":
        (package / ".git/HEAD").write_text("b" * 40 + "\n")
    elif change == "ignored_source":
        (package / "untracked-input").write_text("unapproved source")
    else:
        (root / ".lake/packages/unlisted").mkdir()
    with pytest.raises(ValueError):
        authority.validate(root, check=lambda: None)


@pytest.mark.parametrize(
    ("change", "kind"),
    [("added", "added"), ("removed", "missing"), ("bytes", "changed"), ("mode", "changed")],
)
def test_pinned_source_drift_names_ignored_paths_without_revealing_contents(tmp_path, change, kind):
    root = tmp_path / "project"
    plan = _fixture(root)
    package = root / ".lake/packages/sample"
    (package / ".git/info/exclude").write_text("generated/\n")
    source = package / "generated/input.bin"
    source.parent.mkdir()
    if change != "added":
        source.write_text("private original file content")
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    assert authority.package_sources is not None
    captured = dict(authority.package_sources["sample"])
    if change == "removed":
        source.unlink()
    elif change == "mode":
        before = source.read_bytes()
        source.chmod(source.stat().st_mode ^ 0o020)
        assert source.read_bytes() == before
    else:
        source.write_text("private changed file content")
    assert not _git(package, ["status", "--porcelain", "--untracked-files=all"]).strip()
    with pytest.raises(ValueError) as rejected:
        authority.validate(root, git=_git, check=lambda: None)
    message = str(rejected.value)
    assert message.startswith("Preparation builds changed pinned dependency source: sample;")
    assert f'{kind} "generated/input.bin"' in message
    assert f"{kind}=1" in message and "first 1 of 1 paths" in message
    assert "changed identities bind bytes, entry type and mode" in message
    assert "private original" not in message and "private changed" not in message
    assert str(root) not in message
    assert authority.package_sources["sample"] == captured


def test_pinned_source_drift_bounds_sorted_paths_and_escapes_control_characters(tmp_path):
    root = tmp_path / "project"
    plan = _fixture(root)
    package = root / ".lake/packages/sample"
    (package / ".git/info/exclude").write_text("generated/\n")
    directory = package / "generated"
    directory.mkdir()
    changed, missing = directory / "a-changed", directory / "b-missing"
    changed.write_text("private prior bytes")
    missing.write_text("private missing bytes")
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    changed.write_text("private replacement bytes")
    missing.unlink()
    for index in reversed(range(12)):
        name = f"c-added-{index:02d}" + ("\nline" if index == 0 else "")
        (directory / name).write_text("private added bytes")
    assert not _git(package, ["status", "--porcelain", "--untracked-files=all"]).strip()
    with pytest.raises(ValueError) as rejected:
        authority.validate(root, git=_git, check=lambda: None)
    message = str(rejected.value)
    assert "added=12, missing=1, changed=1" in message and "first 10 of 14 paths" in message
    assert message.index('changed "generated/a-changed"') < message.index(
        'missing "generated/b-missing"'
    )
    assert 'added "generated/c-added-00\\nline"' in message and "\n" not in message
    assert "c-added-07" in message and "c-added-08" not in message
    assert "private" not in message and len(message) < 1500


def test_pinned_source_drift_truncates_long_relative_names_without_accepting_them(tmp_path):
    root = tmp_path / "project"
    plan = _fixture(root)
    package = root / ".lake/packages/sample"
    (package / ".git/info/exclude").write_text("generated/\n")
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    directory = package / "generated" / ("x" * 100)
    directory.mkdir(parents=True)
    path = directory / ("y" * 100)
    path.write_text("private file content")
    relative = path.relative_to(package).as_posix()
    with pytest.raises(ValueError) as rejected:
        authority.validate(root, git=_git, check=lambda: None)
    message = str(rejected.value)
    assert json.dumps(relative[:160] + "...") in message and relative not in message
    assert "added=1" in message and len(message) < 600
    assert "private file" not in message


@pytest.mark.parametrize("boundary", ["before_delta", "before_commit"])
def test_commit_repeats_authorization_instead_of_accepting_a_final_hash(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root, stage = tmp_path / "project", tmp_path / "stage"
    plan = _fixture(root)
    baseline = infrastructure_baseline(root)
    copy_stage(root, stage)
    authority = PreparationAuthorization.capture(stage, plan, check=lambda: None).acquire(
        stage, git=_git, check=lambda: None
    )
    (stage / ".lake/build").mkdir()
    (stage / ".lake/build/new-output").write_text("checked output")

    def authorize(selected):
        authority.validate(selected, check=lambda: None)

    def mutate(selected):
        path = selected / "lakefile.toml"
        path.write_text(
            path.read_text().replace("example.invalid/sample", "example.invalid/replaced")
        )

    original = (root / "lakefile.toml").read_bytes()
    if boundary == "before_delta":
        mutate(stage)
        with pytest.raises(ValueError, match="authorized configuration"):
            managed_delta(root, stage, baseline, authorize=authorize)
    else:
        selected = managed_delta(root, stage, baseline, authorize=authorize)
        with pytest.raises(ValueError, match="authorized configuration"):
            commit_preparation(
                root,
                stage,
                managed=selected,
                protected=protected_inputs(root),
                receipt={},
                validate=lambda: None,
                authorize=authorize,
                cancelled=lambda: False,
                checkpoint=lambda name: mutate(root) if name == "before_commit" else None,
            )
        assert "replaced" in (root / "lakefile.toml").read_text()
        assert not (root / ".lake/build/new-output").exists()
    if boundary == "before_delta":
        assert (root / "lakefile.toml").read_bytes() == original


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"fixedToolchain": "false"}, "malformed fixedToolchain"),
        ({"fixedToolchain": 0}, "malformed fixedToolchain"),
        ({"fixedToolchain": None}, "malformed fixedToolchain"),
        ({"toolchain": "leanprover/lean4:v4.35.0"}, "unsupported layout metadata"),
    ],
)
@pytest.mark.parametrize("location", ["root", "dependency"])
def test_lake_manifests_admit_only_typed_fixed_toolchain_metadata(
    tmp_path, location, metadata, message
):
    root = tmp_path / "project"
    if location == "root":
        plan = _lake_fixture(root, root_metadata=metadata)
        with pytest.raises(ValueError, match=message):
            PreparationAuthorization.capture(root, plan, check=lambda: None)
        return
    plan = _lake_fixture(root, dependency_metadata=metadata)
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None)
    with pytest.raises(ValueError, match=message):
        authority.acquire(root, git=_git, check=lambda: None)


@pytest.mark.parametrize("acquired", [False, True])
@pytest.mark.parametrize("baseline", [False, True, None])
def test_acquisition_keeps_the_baseline_or_lake_default_fixed_toolchain_flag(
    tmp_path, baseline, acquired
):
    root = tmp_path / "project"
    plan = _lake_fixture(root, root_metadata={"fixedToolchain": bool(baseline)})
    path = root / "lake-manifest.json"
    manifest = json.loads(path.read_text())
    if baseline is None:
        path.unlink()
        plan["existing_packages"] = existing_packages(root, read_configuration(root))
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None)
    assert (authority.baseline_manifest is None) is (baseline is None)
    # Model `lake update` writing the acquired root flag after the baseline was frozen.
    path.write_text(json.dumps({**manifest, "fixedToolchain": acquired}))
    if acquired is bool(baseline):
        authority.acquire(root, git=_git, check=lambda: None).validate(root, check=lambda: None)
        return
    message = "unauthorized fixedToolchain" if baseline is None else "existing manifest"
    with pytest.raises(ValueError, match=message):
        authority.acquire(root, git=_git, check=lambda: None)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("flip", "authorized resolved package manifest"),
        ("remove", "authorized resolved package manifest"),
        ("retype", "malformed fixedToolchain"),
    ],
)
@pytest.mark.parametrize("fixed", [False, True])
def test_post_build_fixed_toolchain_changes_are_never_blessed(tmp_path, fixed, change, message):
    root = tmp_path / "project"
    plan = _lake_fixture(root, root_metadata={"fixedToolchain": fixed})
    authority = PreparationAuthorization.capture(root, plan, check=lambda: None).acquire(
        root, git=_git, check=lambda: None
    )
    authority.validate(root, check=lambda: None)
    path = root / "lake-manifest.json"
    manifest = json.loads(path.read_text())
    if change == "flip":
        manifest["fixedToolchain"] = not fixed
    elif change == "remove":
        # Lake reads absence as false, yet a build that rewrites the manifest still drifted.
        del manifest["fixedToolchain"]
    else:
        manifest["fixedToolchain"] = json.dumps(fixed)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=message):
        authority.validate(root, check=lambda: None)


@pytest.mark.parametrize("flipped", [False, True])
@pytest.mark.parametrize("fixed", [False, True])
def test_commit_keeps_the_lake_fixed_toolchain_flag_and_refuses_a_flipped_one(
    tmp_path, monkeypatch, fixed, flipped
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root, stage = tmp_path / "project", tmp_path / "stage"
    flag = {"fixedToolchain": fixed}
    plan = _lake_fixture(root, root_metadata=flag, dependency_metadata=flag)
    original = (root / "lake-manifest.json").read_bytes()
    baseline = infrastructure_baseline(root)
    copy_stage(root, stage)
    authority = PreparationAuthorization.capture(stage, plan, check=lambda: None).acquire(
        stage, git=_git, check=lambda: None
    )
    assert authority.manifest is not None and authority.manifest["fixedToolchain"] is fixed
    (stage / ".lake/build").mkdir()
    (stage / ".lake/build/new-output").write_text("checked output")

    def authorize(selected):
        authority.validate(selected, check=lambda: None)

    def checkpoint(name):
        if flipped and name == "before_commit":
            path = root / "lake-manifest.json"
            manifest = json.loads(path.read_text())
            path.write_text(json.dumps({**manifest, "fixedToolchain": not fixed}))

    def commit():
        return commit_preparation(
            root,
            stage,
            managed=managed_delta(root, stage, baseline, authorize=authorize),
            protected=protected_inputs(root),
            receipt={},
            validate=lambda: None,
            authorize=authorize,
            cancelled=lambda: False,
            checkpoint=checkpoint,
        )

    if flipped:
        with pytest.raises(ValueError, match="authorized resolved package manifest"):
            commit()
        assert not (root / ".lake/build/new-output").exists()
    else:
        assert commit()["state"] == "COMMITTED"
        assert (root / ".lake/build/new-output").read_text() == "checked output"
        assert (root / "lake-manifest.json").read_bytes() == original


@pytest.mark.integration
@pytest.mark.parametrize("effect", ["benign", "pin", "requirement"])
def test_real_sandboxed_lake_build_cannot_authorize_its_mutations(tmp_path, monkeypatch, effect):
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Select an installed compiler for real sandboxed preparation builds")
    sandbox = "sandbox-exec" if platform.system() == "Darwin" else "bwrap"
    if not shutil.which(sandbox):
        pytest.skip("Real preparation sandbox unavailable")
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root, stage = tmp_path / "project", tmp_path / "stage"
    plan = _fixture(root, effect=effect, toolchain=toolchain)
    preparation_directory(root).mkdir(parents=True)
    baseline = infrastructure_baseline(root)
    original = {
        name: (root / name).read_bytes()
        for name in ("lakefile.toml", "lake-manifest.json", "Main.lean")
    }
    copy_stage(root, stage)
    policy = build_setup_execution_policy(
        stage,
        canonical_root=root,
        toolchain=toolchain,
        network_allowed=False,
        deadline=time.monotonic() + 90,
    )
    cancel = threading.Event()
    with supervise_preparation(policy, cancel=cancel):
        git = preparation_git_reader(root, policy, cancel=cancel, progress=None)
        authority = PreparationAuthorization.capture(
            stage, plan, check=lambda: policy.check(cancel)
        ).acquire(stage, git=git, check=lambda: policy.check(cancel))
        code, output = run_command(
            ["lake", "build", "@sample"],
            cwd=stage,
            log=io.StringIO(),
            cancel=cancel,
            progress=None,
            phase="building_dependencies",
            execution_policy=policy,
        )
        assert code == 0, output
    assert policy.usage.cleanup_verified
    assert all((root / name).read_bytes() == value for name, value in original.items())

    def authorize(selected):
        authority.validate(selected, check=lambda: policy.check(cancel))

    if effect == "benign":
        assert (stage / ".lake/fixture-effect").read_text() == "benign"
        build_prepared_dependencies(
            root,
            stage,
            plan,
            {row["name"]: row for row in plan["existing_packages"]},
            policy,
            cancel=cancel,
            progress=None,
        )
        selected = managed_delta(root, stage, baseline, authorize=authorize)
        transaction = commit_preparation(
            root,
            stage,
            managed=selected,
            protected=protected_inputs(root),
            receipt={},
            validate=lambda: None,
            authorize=authorize,
            cancelled=lambda: False,
        )
        assert transaction["state"] == "COMMITTED"
        assert (root / ".lake/fixture-effect").read_text() == "benign"
    else:
        changed = "lake-manifest.json" if effect == "pin" else "lakefile.toml"
        assert (stage / changed).read_bytes() != original[changed]
        with pytest.raises(ValueError):
            managed_delta(root, stage, baseline, authorize=authorize)
        assert all((root / name).read_bytes() == value for name, value in original.items())
        assert not (root / ".lake/fixture-effect").exists()
