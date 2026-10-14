"""Exercise narrow official compiler acquisition without modifying existing Elan authority."""

from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_lifecycle import private_stage
from leanflow_cli.formalization.preparation_run import prepare_private_stage
from leanflow_cli.workflows.project_setup import toolchain_install
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure

PIN = "leanprover/lean4:v4.99.0"
DIRECTORY = "leanprover--lean4---v4.99.0"


@pytest.fixture
def acquisition(tmp_path, monkeypatch):
    stage, canonical, elan = (tmp_path / name for name in ("stage", "canonical", "elan"))
    stage.mkdir()
    canonical.mkdir()
    (canonical / "source.tex").write_text("Untouched source")
    (elan / "bin").mkdir(parents=True)
    (elan / "toolchains/old/bin").mkdir(parents=True)
    (elan / "toolchains/old/bin/lean").write_text("existing compiler")
    (elan / "settings.toml").write_text("default_toolchain = 'old'\n")
    monkeypatch.setenv("ELAN_HOME", str(elan))
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "native-secret")
    return stage, canonical, elan


def _ensure(acquisition, **kwargs):
    stage, canonical, _ = acquisition
    return toolchain_install.ensure_official_toolchain(
        stage,
        canonical_root=canonical,
        toolchain=kwargs.pop("toolchain", PIN),
        deadline=kwargs.pop("deadline", time.monotonic() + 20),
        network_allowed=kwargs.pop("network_allowed", True),
        **kwargs,
    )


def _installer(elan: Path, body: str) -> None:
    binary = elan / "bin/elan"
    binary.write_text("#!/bin/sh\nset -eu\n" + body)
    binary.chmod(0o755)


def _require_sandbox() -> None:
    binary = "sandbox-exec" if platform.system() == "Darwin" else "bwrap"
    if platform.system() not in {"Darwin", "Linux"} or not shutil.which(binary):
        pytest.skip("Real compiler acquisition sandbox unavailable")


def test_existing_complete_compiler_is_reused_without_network_or_installer(acquisition):
    _, _, elan = acquisition
    compiler = elan / "toolchains" / DIRECTORY / "bin"
    compiler.mkdir(parents=True)
    for name in ("lean", "lake"):
        binary = compiler / name
        binary.write_text('#!/bin/sh\nprintf "Lean (version 4.99.0, fake compiler, Release)\\n"\n')
        binary.chmod(0o755)
    before = (elan / "settings.toml").read_bytes()
    result = _ensure(acquisition, network_allowed=False)
    assert result["status"] == "reused" and result["resources"]["commands"] == 1
    assert (elan / "settings.toml").read_bytes() == before
    assert not (elan / "bin/elan").exists()


def test_offline_missing_pin_and_invalid_model_versions_block_without_replacement(acquisition):
    _, _, elan = acquisition
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition, network_allowed=False)
    assert error.value.code == "setup_toolchain_network_required"
    assert not (elan / "toolchains" / DIRECTORY).exists()
    for pin in ("nightly", "leanprover/lean4:stable", "someone/lean:v4.99.0"):
        with pytest.raises(ValueError, match="exact official"):
            _ensure(acquisition, toolchain=pin)


def test_existing_incomplete_or_aliased_compiler_is_never_patched(acquisition):
    _, _, elan = acquisition
    target = elan / "toolchains" / DIRECTORY
    target.mkdir()
    marker = target / "unfinished"
    marker.write_text("preserve")
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition)
    assert error.value.code == "setup_toolchain_incomplete"
    assert marker.read_text() == "preserve"
    marker.unlink()
    target.rmdir()
    target.symlink_to(elan / "toolchains/old", target_is_directory=True)
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition)
    assert error.value.code == "setup_toolchain_incomplete" and target.is_symlink()


def test_real_installer_only_creates_new_pin_and_cannot_mutate_old_authority(acquisition):
    _require_sandbox()
    _, canonical, elan = acquisition
    _installer(
        elan,
        f"""
test "$#" = 3
test "$1" = toolchain
test "$2" = install
test "$3" = '{PIN}'
test -z "${{OPENAI_API_KEY-}}"
if printf changed > '{elan}/settings.toml'; then exit 20; fi
if printf changed > '{elan}/toolchains/old/bin/lean'; then exit 21; fi
if mv '{elan}/toolchains/old' '{elan}/toolchains/old-moved'; then exit 24; fi
if cat '{canonical}/source.tex'; then exit 22; fi
if printf changed > '{canonical}/source.tex'; then exit 23; fi
target="$ELAN_HOME/toolchains/{DIRECTORY}/bin"
mkdir -p "$target"
for name in lean lake; do
    printf '#!/bin/sh\\nprintf "Lean (version 4.99.0, fake compiler, Release)\\\\n"\\n' > "$target/$name"
    chmod 755 "$target/$name"
done
printf 'installed exact official pin\\n'
""",
    )
    result = _ensure(acquisition)
    assert result["status"] == "installed", result
    assert result["resources"]["cleanup_verified"]
    assert result["resources"]["commands"] == 3
    assert result["compiler_version"] == "Lean (version 4.99.0, fake compiler, Release)"
    assert (elan / "settings.toml").read_text() == "default_toolchain = 'old'\n"
    assert (elan / "toolchains/old/bin/lean").read_text() == "existing compiler"
    assert (canonical / "source.tex").read_text() == "Untouched source"
    assert not (elan / "known-projects").exists()
    assert os.access(elan / "toolchains" / DIRECTORY / "bin/lean", os.X_OK)


def test_cancelled_or_expired_acquisition_never_executes_installer(acquisition):
    _, _, elan = acquisition
    _installer(elan, 'printf launched > "$ELAN_HOME/downloads/launched"\n')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SetupCancelled):
        _ensure(acquisition, cancel=cancel)
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition, deadline=time.monotonic() - 1)
    assert error.value.code == "setup_deadline"
    assert not (elan / "downloads/launched").exists()


def test_acquisition_log_is_per_attempt_and_failed_private_pin_never_appears_native(acquisition):
    _require_sandbox()
    _, _, elan = acquisition
    _installer(elan, f'mkdir -p "$ELAN_HOME/toolchains/{DIRECTORY}"\nprintf partial\nexit 1\n')
    with pytest.raises(SetupFailure) as error:
        _ensure(acquisition)
    assert error.value.code == "setup_toolchain_download_failed"
    assert not (elan / "toolchains" / DIRECTORY).exists()
    stage = acquisition[0]
    assert list((stage / ".leanflow-toolchain-private").glob(f"elan-*/toolchains/{DIRECTORY}"))
    logs = list((toolchain_install.leanflow_home() / "toolchain-installations").glob("*.log"))
    assert len(logs) == 1 and "partial" in logs[0].read_text()
    with pytest.raises(SetupFailure):
        _ensure(acquisition)
    assert (
        len(list((toolchain_install.leanflow_home() / "toolchain-installations").glob("*.log")))
        == 2
    )


@pytest.mark.parametrize("failure", ["offline", "cancelled", "recovery", "download", "unverified"])
def test_acquisition_failure_disposes_only_verified_private_stage(
    acquisition, monkeypatch, failure
):
    _, canonical, elan = acquisition
    event = threading.Event()
    if failure == "cancelled":
        event.set()
    elif failure == "recovery":
        directory = toolchain_install.leanflow_home() / "toolchain-installations"
        directory.mkdir(parents=True)
        (directory / "legacy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": str(elan / "toolchains" / DIRECTORY),
                    "state": "PREPARED",
                    "after": "old-checksum",
                }
            )
        )
    elif failure in {"download", "unverified"}:
        _require_sandbox()
        _installer(elan, "printf failed-download\nexit 1\n")
    if failure == "unverified":

        def uncertain(command, **kwargs):
            kwargs["execution_policy"].usage.cleanup_verified = False
            raise SetupFailure(
                "setup_cleanup_unverified", "Owned child may remain", "Retain inputs"
            )

        monkeypatch.setattr(toolchain_install, "run_command", uncertain)
    before = (elan / "settings.toml").read_bytes()
    with pytest.raises((SetupFailure, SetupCancelled)):
        with private_stage(canonical) as lease:
            prepare_private_stage(
                canonical,
                {},
                {"toolchain": PIN},
                lease=lease,
                progress=None,
                cancel=event,
                deadline=time.monotonic() + 15,
                cancelled=event.is_set,
                network_allowed=failure != "offline",
                budget_path=None,
                checkpoint=None,
            )
    assert lease.directory.exists() is (failure == "unverified")
    assert (elan / "settings.toml").read_bytes() == before
    assert (elan / "toolchains/old/bin/lean").read_text() == "existing compiler"
    assert not (elan / "toolchains" / DIRECTORY).exists()


def test_legacy_journal_cannot_block_verified_existing_compiler_reuse(acquisition):
    _, _, elan = acquisition
    compiler = elan / "toolchains" / DIRECTORY / "bin"
    compiler.mkdir(parents=True)
    for name in ("lean", "lake"):
        binary = compiler / name
        binary.write_text(
            '#!/bin/sh\nprintf "Lean (version 4.99.0, preserved compiler, Release)\\n"\n'
        )
        binary.chmod(0o755)
    directory = toolchain_install.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    journal = {
        "schema_version": 1,
        "target": str(compiler.parent),
        "state": "INSTALLED",
        "after": "legacy",
    }
    journal["record_id"] = identity(journal)
    path = directory / "legacy.json"
    path.write_text(json.dumps(journal))
    before = path.read_bytes()
    result = _ensure(acquisition, network_allowed=False)
    assert result["status"] == "reused" and result["recovery"]["status"] == "retained"
    assert "Legacy compiler journal" in result["recovery"]["reason"]
    assert path.read_bytes() == before and not (elan / "bin/elan").exists()
