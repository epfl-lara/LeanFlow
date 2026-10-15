"""Retain only verified new Lake hash sidecars without widening original setup authority."""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness.isolation import IsolationPolicy
from leanflow_cli.formalization import preparation_hash_cache as caches
from leanflow_cli.formalization.preparation_authorization import PreparationAuthorization
from leanflow_cli.formalization.preparation_inputs import existing_packages, read_configuration
from leanflow_cli.formalization.preparation_lifecycle import PrivateStage
from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.formalization.preparation_stage import preparation_git_reader
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup.cleanup import process_states
from leanflow_cli.workflows.project_setup.execution import (
    SetupExecutionPolicy,
    build_setup_execution_policy,
)
from leanflow_cli.workflows.project_setup.process import run_command

_BINARY = "60224162a80c236f"
_TEXT = "36292790ba69fd38"
_BYTES = b"alpha\r\nbeta\r\n"


def _git(path: Path, args: list[str]) -> str:
    """Read fixture Git state without inherited helpers, signing or credentials."""
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        },
        text=True,
        timeout=10,
    )


@dataclass
class World:
    stage: Path
    canonical: Path
    package: Path
    source: Path
    sidecar: Path
    plan: dict[str, Any]
    authority: PreparationAuthorization
    policy: SetupExecutionPolicy
    lease: PrivateStage

    def recapture(self) -> None:
        self.authority = PreparationAuthorization.capture(
            self.stage, self.plan, check=lambda: None
        ).acquire(self.stage, git=_git, check=lambda: None)

    def verify(self) -> PreparationAuthorization:
        return caches.verify_generated_hash_caches(
            self.stage,
            self.authority,
            self.policy,
            self.lease,
            git=_git,
            log=io.StringIO(),
            cancel=None,
            progress=None,
        )


@pytest.fixture
def world(tmp_path: Path):
    stage, canonical = tmp_path / "stage", tmp_path / "canonical"
    stage.mkdir()
    canonical.mkdir()
    original = canonical / "paper.tex"
    original.write_bytes(b"Original source must remain unchanged")
    package = stage / ".lake/packages/sample"
    package.mkdir(parents=True)
    (package / ".gitignore").write_text("*.hash\n.lake/\n")
    (package / "lakefile.lean").write_text(
        'import Lake\nopen Lake DSL System\npackage sample\n@[default_target]\ntarget lockFile pkg : FilePath :=\n  Job.async do\n    let path := pkg.dir / "Lock.txt"\n    discard <| fetchFileHash path (text := true)\n    return path\n'
    )
    source = package / "Lock.txt"
    source.write_bytes(_BYTES)
    (package / "Input.txt").write_text("unchanged declared dependency\n")
    _git(package, ["init", "--quiet"])
    _git(package, ["add", "."])
    _git(
        package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "pinned input",
        ],
    )
    rev = _git(package, ["rev-parse", "HEAD"]).strip()
    (stage / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    (stage / "lakefile.toml").write_text(
        'name = "fixture"\n[[require]]\nname = "sample"\ngit = "https://example.invalid/sample"\nrev = "'
        + rev
        + '"\n'
    )
    (stage / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "name": "fixture",
                "packagesDir": ".lake/packages",
                "lakeDir": ".lake",
                "packages": [
                    {
                        "name": "sample",
                        "type": "git",
                        "url": "https://example.invalid/sample",
                        "rev": rev,
                        "inputRev": rev,
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
    plan = {
        "existing_packages": existing_packages(stage, read_configuration(stage)),
        "packages": [],
        "catalog_requirements": [],
        "toolchain": "leanprover/lean4:v4.33.1",
    }
    policy = SetupExecutionPolicy(
        IsolationPolicy(stage),
        {
            "LEAN_PATH": "untrusted",
            "LEAN_SRC_PATH": "untrusted",
            "LEAN_SYSROOT": "untrusted",
            "LEAN_NUM_THREADS": "9",
        },
        {"lean": "/trusted/toolchain/bin/lean"},
        time.monotonic() + 120,
        disk_guard=lambda _: None,
    )
    info = stage.stat()
    current = process_states()[os.getpid()]
    lease = PrivateStage(
        stage,
        tmp_path,
        {
            "pid": os.getpid(),
            "start": current[2],
            "stage_device": info.st_dev,
            "stage_inode": info.st_ino,
            "stage_id": "fixture",
        },
        cleanup_observed=True,
    )
    authority = PreparationAuthorization.capture(stage, plan, check=lambda: None).acquire(
        stage, git=_git, check=lambda: None
    )
    value = World(
        stage, canonical, package, source, package / "Lock.txt.hash", plan, authority, policy, lease
    )
    yield value
    assert original.read_bytes() == b"Original source must remain unchanged"


@pytest.fixture
def query(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return 0, json.dumps([[_BINARY, _TEXT] for _ in command[3:]])

    monkeypatch.setattr(caches, "run_command", run)
    return calls


def test_new_derived_cache_extends_authority_without_changing_original_inputs(world, query):
    world.sidecar.write_text(_TEXT)
    before = dict(world.authority.package_sources["sample"])
    with pytest.raises(ValueError, match="changed pinned dependency source"):
        world.authority.validate(world.stage, git=_git, check=lambda: None)
    extended = world.verify()
    assert world.sidecar.read_text() == _TEXT and world.source.read_bytes() == _BYTES
    extended.validate(world.stage, git=_git, check=lambda: None)
    assert len(extended.verified_hash_caches) == 1
    assert extended.package_sources is world.authority.package_sources
    assert extended.package_sources["sample"] == before
    assert world.authority.verified_hash_caches == () and world.lease.cleanup_verified
    with pytest.raises(ValueError, match="changed pinned dependency source"):
        world.authority.validate(world.stage, git=_git, check=lambda: None)
    command, kwargs = query[0]
    derived = kwargs["execution_policy"]
    assert command[:2] == ["lean", "--run"] and command[3:] == ["input-0"]
    assert kwargs["cwd"].is_relative_to(world.stage / ".leanflow/preparation-checks")
    assert not kwargs["cwd"].is_relative_to(world.package)
    assert kwargs["stdout_only"] is True and kwargs["stdout_limit_bytes"] == 64 * 1024
    assert derived.environment["LEAN_PATH"] == "/trusted/toolchain/lib/lean"
    assert "LEAN_SRC_PATH" not in derived.environment and "LEAN_SYSROOT" not in derived.environment
    assert derived.usage is world.policy.usage and derived.owner is world.policy.owner
    assert derived.isolation is world.policy.isolation and derived.deadline == world.policy.deadline
    assert world.policy.environment["LEAN_PATH"] == "untrusted"


def test_no_new_cache_does_not_launch_query(world, query):
    assert world.verify() is world.authority and query == []


def test_oversized_sparse_manifest_rejects_before_validation_query_or_unlink(
    world, query, monkeypatch
):
    world.sidecar.write_text(_TEXT)
    manifest = world.stage / "lake-manifest.json"
    with manifest.open("wb") as stream:
        stream.truncate(caches.MAX_PROTOCOL_STDOUT_BYTES + 1)

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized metadata must fail before authority validation or cache effects")

    monkeypatch.setattr(PreparationAuthorization, "validate_configuration", forbidden)
    monkeypatch.setattr(caches, "_query_hashes", forbidden)
    monkeypatch.setattr(PreparationAuthorization, "with_verified_hash_caches", forbidden)
    with pytest.raises(ValueError, match="Expected a bounded regular file"):
        world.verify()
    assert world.sidecar.read_text() == _TEXT and query == []
    assert not (world.stage / ".leanflow/preparation-checks").exists()


@pytest.mark.parametrize("value", ["f" * 16, "f" * 15, "f" * 17, "A" * 16, "not-a-Lake-cache"])
def test_invalid_or_nonmatching_cache_is_preserved(world, query, value):
    world.sidecar.write_text(value)
    with pytest.raises(ValueError):
        world.verify()
    assert world.sidecar.read_text() == value


@pytest.mark.parametrize(
    "change", ["bytes", "mode", "remove", "extra", "unknown_base", "pin", "hooks", "tracked_cache"]
)
def test_other_input_changes_reject_without_removing_any_cache(world, query, change):
    world.sidecar.write_text(_TEXT)
    if change == "bytes":
        world.source.write_text("unauthorized replacement")
    elif change == "mode":
        world.source.chmod(world.source.stat().st_mode ^ 0o020)
    elif change == "remove":
        world.source.unlink()
    elif change == "extra":
        (world.package / "Unapproved.lean").write_text("def injected := 1")
    elif change == "unknown_base":
        (world.package / "Unknown.hash").write_text(_TEXT)
    elif change == "pin":
        (world.package / ".git/HEAD").write_text("b" * 40 + "\n")
    elif change == "hooks":
        (world.package / ".git/config").write_text("unapproved Git configuration")
    else:
        _git(world.package, ["add", "--force", "Lock.txt.hash"])
    with pytest.raises(ValueError):
        world.verify()
    assert world.sidecar.read_text() == _TEXT and not query


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_aliased_or_special_sidecars_are_never_opened_or_removed(world, query, kind):
    other = world.stage / "other"
    other.write_text(_TEXT)
    if kind == "symlink":
        world.sidecar.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, world.sidecar)
    else:
        os.mkfifo(world.sidecar)
    started = time.monotonic()
    with pytest.raises(ValueError):
        world.verify()
    assert time.monotonic() - started < 2 and world.sidecar.lstat() and not query
    assert other.read_text() == _TEXT


def test_changed_preexisting_hash_remains_an_input(world, query):
    world.sidecar.write_text(_TEXT)
    world.recapture()
    world.sidecar.write_text(_BINARY)
    with pytest.raises(ValueError, match="previous input"):
        world.verify()
    assert world.sidecar.read_text() == _BINARY and not query


def test_unchanged_preexisting_hash_needs_no_new_authority_or_query(world, query):
    world.sidecar.write_text(_TEXT)
    world.recapture()
    assert world.verify() is world.authority
    assert world.sidecar.read_text() == _TEXT and query == []
    assert world.authority.verified_hash_caches == ()


@pytest.mark.parametrize(
    "response", ["prefix []", "[]", '[[null,"' + _TEXT + '"]]', '[["0000000000000000",null]]']
)
def test_incomplete_or_untrusted_protocol_preserves_sidecar(world, monkeypatch, response):
    world.sidecar.write_text(_TEXT)
    monkeypatch.setattr(caches, "run_command", lambda *a, **kw: (0, response))
    with pytest.raises(ValueError):
        world.verify()
    assert world.sidecar.read_text() == _TEXT


def test_unsupported_pinned_api_cannot_remove_cache(world, monkeypatch):
    world.sidecar.write_text(_TEXT)
    monkeypatch.setattr(caches, "run_command", lambda *a, **kw: (1, "unknown API"))
    with pytest.raises(ValueError, match="pinned Lake hash API"):
        world.verify()
    assert world.sidecar.exists()


@pytest.mark.parametrize("change", ["sidecar_inode", "source", "pin", "tracked", "cleanup"])
def test_query_time_changes_or_unknown_closure_preserve_sidecar(world, monkeypatch, change):
    world.sidecar.write_text(_TEXT)

    def query(*args, **kwargs):
        if change == "sidecar_inode":
            replacement = world.package / "replacement"
            replacement.write_text(_TEXT)
            os.replace(replacement, world.sidecar)
        elif change == "source":
            world.source.write_text("changed during query")
        elif change == "pin":
            (world.package / ".git/HEAD").write_text("b" * 40 + "\n")
        elif change == "tracked":
            _git(world.package, ["add", "--force", "Lock.txt.hash"])
        else:
            world.policy.usage.cleanup_verified = False
        return 0, json.dumps([[_BINARY, _TEXT]])

    monkeypatch.setattr(caches, "run_command", query)
    with pytest.raises((ValueError, RuntimeError)):
        world.verify()
    assert world.sidecar.read_text() == _TEXT
    if change == "cleanup":
        assert not world.lease.cleanup_verified


@pytest.mark.parametrize("change", ["cleanup", "unobserved", "owner", "inode", "policy"])
def test_unverified_stage_ownership_prevents_even_query(world, query, change):
    world.sidecar.write_text(_TEXT)
    if change == "cleanup":
        world.lease.cleanup_verified = False
    elif change == "unobserved":
        world.lease.cleanup_observed = False
    elif change == "owner":
        world.lease.owner["pid"] = -1
    elif change == "inode":
        world.lease.owner["stage_inode"] = -1
    else:
        world.policy.usage.cleanup_verified = False
    with pytest.raises(ValueError):
        world.verify()
    assert world.sidecar.exists() and not query


def test_last_instant_sidecar_replacement_is_not_admitted(world, query, monkeypatch):
    world.sidecar.write_text(_TEXT)
    original = PreparationAuthorization.with_verified_hash_caches

    def replace(authority, root, records, **kwargs):
        changed = world.package / "replacement"
        changed.write_text("retain replacement")
        os.replace(changed, world.sidecar)
        return original(authority, root, records, **kwargs)

    monkeypatch.setattr(PreparationAuthorization, "with_verified_hash_caches", replace)
    with pytest.raises(ValueError):
        world.verify()
    assert world.sidecar.read_text() == "retain replacement"
    assert world.authority.verified_hash_caches == ()


def test_binary_derivation_is_accepted_without_assuming_text_mode(world, query):
    world.sidecar.write_text(_BINARY)
    extended = world.verify()
    assert world.sidecar.read_text() == _BINARY
    assert extended.verified_hash_caches[0].hash_modes == ("binary",)
    extended.validate(world.stage, git=_git, check=lambda: None)


def test_another_stage_cannot_borrow_a_verified_lease(world, query, monkeypatch):
    world.sidecar.write_text(_TEXT)
    monkeypatch.setattr(
        caches, "_inspect", lambda *args: pytest.fail("Foreign stage reached inspection")
    )
    with pytest.raises(ValueError, match="exact leased stage"):
        caches.verify_generated_hash_caches(
            world.canonical,
            world.authority,
            world.policy,
            world.lease,
            git=_git,
            log=io.StringIO(),
            cancel=None,
            progress=None,
        )
    assert world.sidecar.exists() and not query


def test_hardlinked_bound_input_cannot_authorize_generated_cache(world, query):
    world.sidecar.write_text(_TEXT)
    os.link(world.source, world.stage / "input-alias")
    with pytest.raises(ValueError, match="unaliased regular"):
        world.verify()
    assert world.sidecar.exists() and not query


def test_git_tracking_check_treats_brackets_as_literal_names(world, query):
    source = world.package / "Bound[1].txt"
    source.write_bytes(_BYTES)
    _git(world.package, ["--literal-pathspecs", "add", "Bound[1].txt"])
    _git(
        world.package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "literal tracked input",
        ],
    )
    revision = _git(world.package, ["rev-parse", "HEAD"]).strip()
    old = world.plan["existing_packages"][0]["manifest"]["rev"]
    for name in ("lakefile.toml", "lake-manifest.json"):
        path = world.stage / name
        path.write_text(path.read_text().replace(old, revision))
    world.plan["existing_packages"] = existing_packages(
        world.stage, read_configuration(world.stage)
    )
    world.recapture()
    sidecar = world.package / "Bound[1].txt.hash"
    sidecar.write_text(_TEXT)
    _git(world.package, ["--literal-pathspecs", "add", "--force", "Bound[1].txt.hash"])
    with pytest.raises(ValueError, match="Git-tracked hash cache"):
        world.verify()
    assert sidecar.read_text() == _TEXT and not query


def test_all_cache_results_must_match_before_any_are_removed(world, monkeypatch):
    other = world.package / "Other.txt"
    other.write_bytes(_BYTES)
    _git(world.package, ["add", "Other.txt"])
    _git(
        world.package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "second bound input",
        ],
    )
    revision = _git(world.package, ["rev-parse", "HEAD"]).strip()
    config = world.stage / "lakefile.toml"
    old = world.plan["existing_packages"][0]["manifest"]["rev"]
    config.write_text(config.read_text().replace(old, revision))
    manifest = world.stage / "lake-manifest.json"
    manifest.write_text(manifest.read_text().replace(old, revision))
    world.plan["existing_packages"] = existing_packages(
        world.stage, read_configuration(world.stage)
    )
    world.recapture()
    world.sidecar.write_text(_TEXT)
    second = world.package / "Other.txt.hash"
    second.write_text(_TEXT)
    monkeypatch.setattr(
        caches,
        "run_command",
        lambda *a, **kw: (0, json.dumps([[_BINARY, _TEXT], ["0" * 16, None]])),
    )
    with pytest.raises(ValueError, match="does not derive"):
        world.verify()
    assert world.sidecar.read_text() == _TEXT and second.read_text() == _TEXT


def test_matching_bytes_from_replaced_inode_do_not_authorize_verification(world, monkeypatch):
    world.sidecar.write_text(_TEXT)
    original = caches.read_regular_bytes

    def replace(path, limit):
        value = original(path, limit)
        changed = world.package / "replacement"
        changed.write_text(_TEXT)
        os.replace(changed, world.sidecar)
        return value

    monkeypatch.setattr(caches, "read_regular_bytes", replace)
    with pytest.raises(ValueError, match="malformed Lake hash cache"):
        world.verify()
    assert world.sidecar.read_text() == _TEXT
    assert world.authority.verified_hash_caches == ()


def test_input_substituted_with_fifo_before_copy_fails_without_blocking(world, monkeypatch):
    world.sidecar.write_text(_TEXT)
    original = caches.os.open
    original_copy = caches._copy_input

    def substitute(path, flags, *args, **kwargs):
        if path == world.source:
            world.source.unlink()
            os.mkfifo(world.source)
        return original(path, flags, *args, **kwargs)

    def copy(cache, target, check):
        with monkeypatch.context() as patch:
            patch.setattr(caches.os, "open", substitute)
            return original_copy(cache, target, check)

    monkeypatch.setattr(caches, "_copy_input", copy)
    started = time.monotonic()
    with pytest.raises(ValueError, match="input changed before copying"):
        world.verify()
    assert time.monotonic() - started < 2 and world.sidecar.read_text() == _TEXT


def _large_bound_cache_set(world: World) -> list[Path]:
    """Bind more than one query batch without weakening any Git or source fixture authority."""
    paths = [world.package / f"Bulk-{index:03d}.txt" for index in range(257)]
    for path in paths:
        path.write_bytes(_BYTES)
    _git(world.package, ["add", "."])
    _git(
        world.package,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "large bound input set",
        ],
    )
    revision = _git(world.package, ["rev-parse", "HEAD"]).strip()
    old = world.plan["existing_packages"][0]["manifest"]["rev"]
    for name in ("lakefile.toml", "lake-manifest.json"):
        path = world.stage / name
        path.write_text(path.read_text().replace(old, revision))
    world.plan["existing_packages"] = existing_packages(
        world.stage, read_configuration(world.stage)
    )
    world.recapture()
    sidecars = [path.with_name(path.name + ".hash") for path in paths]
    for path in sidecars:
        path.write_text(_TEXT)
    return sidecars


def test_large_cache_set_uses_bounded_queries_and_one_cumulative_budget(world, monkeypatch):
    sidecars = _large_bound_cache_set(world)
    calls = []

    def query(command, **kwargs):
        assert all(
            path.exists() for path in sidecars
        ), "A cache was deleted before all queries passed"
        calls.append((command, kwargs))
        kwargs["execution_policy"].usage.commands += 1
        return 0, json.dumps([[_BINARY, _TEXT] for _ in command[3:]])

    monkeypatch.setattr(caches, "run_command", query)
    extended = world.verify()
    assert len(extended.verified_hash_caches) == 257
    assert all(path.exists() for path in sidecars)
    assert extended.package_sources is world.authority.package_sources
    assert world.authority.verified_hash_caches == ()
    assert [len(command[3:]) for command, _ in calls] == [256, 1]
    assert len({kwargs["cwd"] for _, kwargs in calls}) == 2
    assert world.policy.usage.commands == 2
    for _, kwargs in calls:
        policy = kwargs["execution_policy"]
        assert policy.usage is world.policy.usage and policy.owner is world.policy.owner
        assert (
            policy.deadline == world.policy.deadline and kwargs["stdout_limit_bytes"] == 64 * 1024
        )
    extended.validate(world.stage, git=_git, check=lambda: None)


def test_later_batch_failure_keeps_every_earlier_verified_cache(world, monkeypatch):
    sidecars = _large_bound_cache_set(world)
    sizes = []

    def query(command, **kwargs):
        assert all(path.exists() for path in sidecars)
        sizes.append(len(command[3:]))
        values = [[_BINARY, _TEXT] for _ in command[3:]]
        if len(sizes) == 2:
            values[0] = ["0" * 16, None]
        return 0, json.dumps(values)

    monkeypatch.setattr(caches, "run_command", query)
    monkeypatch.setattr(
        PreparationAuthorization,
        "with_verified_hash_caches",
        lambda *a, **k: pytest.fail("Incomplete query batches cannot extend setup authority"),
    )
    with pytest.raises(ValueError, match="does not derive"):
        world.verify()
    assert sizes == [256, 1] and all(path.read_text() == _TEXT for path in sidecars)
    assert world.authority.verified_hash_caches == ()


@pytest.mark.integration
def test_real_pinned_lake_sidecar_verification_retains_all_sources(world):
    compiler = Path.home() / ".elan/toolchains/leanprover--lean4---v4.33.1/bin/lean"
    sandbox = "sandbox-exec" if platform.system() == "Darwin" else "bwrap"
    if not compiler.is_file() or not shutil.which(sandbox):
        pytest.skip("Pinned compiler and real preparation sandbox required")
    policy = build_setup_execution_policy(
        world.stage,
        canonical_root=world.canonical,
        toolchain=world.plan["toolchain"],
        network_allowed=False,
        deadline=time.monotonic() + 90,
    )
    world.policy = policy
    world.lease.require_cleanup()
    log = io.StringIO()
    try:
        with supervise_preparation(policy):
            code, output = run_command(
                ["lake", "build", "@sample"],
                cwd=world.stage,
                log=log,
                cancel=threading.Event(),
                progress=None,
                phase="fixture_build",
                execution_policy=policy,
            )
            assert code == 0, output
    finally:
        world.lease.verify_cleanup(policy.evidence())
    assert world.sidecar.read_text() == _TEXT
    with pytest.raises(ValueError, match="changed pinned dependency source"):
        world.authority.validate(world.stage, git=_git, check=lambda: None)
    preparation_directory(world.canonical).mkdir(parents=True, exist_ok=True)
    git = preparation_git_reader(world.canonical, policy, cancel=None, progress=None)
    extended = caches.verify_generated_hash_caches(
        world.stage,
        world.authority,
        policy,
        world.lease,
        git=git,
        log=log,
        cancel=None,
        progress=None,
    )
    assert len(extended.verified_hash_caches) == 1
    assert world.sidecar.read_text() == _TEXT and world.source.read_bytes() == _BYTES
    assert extended.package_sources is world.authority.package_sources
    extended.validate(world.stage, git=_git, check=lambda: None)
    assert world.authority.verified_hash_caches == ()
    assert world.lease.cleanup_verified and policy.usage.cleanup_verified


@pytest.mark.integration
def test_real_verified_hash_cache_survives_the_readonly_final_build(world):
    """Retain verified setup infrastructure needed by Lake under final-verifier permissions."""
    from leanflow_cli.formalization.dependency_inputs import dependency_inputs
    from leanflow_cli.workflows.prover.check_process import isolated_command
    from leanflow_cli.workflows.prover.verification import LeanVerifier

    test_real_pinned_lake_sidecar_verification_retains_all_sources(world)
    assert (
        world.sidecar.is_file()
    ), "Verified Lake cache must survive preparation for readonly final builds"
    expected = dependency_inputs(world.stage)
    workspace = world.stage.parent / "readonly-final-check"
    workspace.mkdir()
    build = world.stage / ".lake/build"
    build.mkdir(parents=True, exist_ok=True)
    verifier = LeanVerifier(world.stage, ())
    result = isolated_command(
        project_root=world.stage,
        workspace=workspace,
        argv=["lake", "build", "@sample"],
        timeout_s=30,
        extra_writable_roots=(*verifier._lake_config_cache(), build),
    )
    assert result.get("success") is True and result.get("returncode") == 0, result
    assert dependency_inputs(world.stage) == expected
    assert world.sidecar.read_text() == _TEXT
    assert world.source.read_bytes() == _BYTES
