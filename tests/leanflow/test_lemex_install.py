"""Exercise private Lemex installation without a network or inference request."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from leanflow_cli.runtime import lemex_install as installer


@pytest.fixture
def installed_package(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "leanflow"))
    native_home = tmp_path / "native-lemex"
    native_home.mkdir()
    (native_home / "config.toml").write_text('model = "user-model"\n')
    (native_home / "auth.json").write_text('{"secret": "untouched"}')
    monkeypatch.setenv("LEMEX_HOME", str(native_home))
    package = tmp_path / "global" / "lemex"
    (package / "bin").mkdir(parents=True)
    (package / "vendor").mkdir()
    (package / "package.json").write_text(json.dumps({"name": "lemex"}))
    (package / "vendor" / "companion").write_text("native-resource")
    executable = package / "bin" / "lemex"
    executable.write_text(
        '#!/bin/sh\ncase "$*" in\n --version) echo lemex-0.0.0;;\n *) echo "--json --output-last-message --skip-git-repo-check resume";;\nesac\n'
    )
    executable.chmod(0o755)
    original_which = installer.shutil.which
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: str(executable) if name == "lemex" else original_which(name),
    )
    return package, executable, native_home


def test_adoption_pins_whole_distribution_and_preserves_native_setup(installed_package):
    package, global_executable, native_home = installed_package
    before = {path.name: path.read_bytes() for path in native_home.iterdir()}
    result = installer.ensure_lemex(build=False)
    managed = Path(result["executable"])
    assert managed != global_executable
    assert managed.read_bytes() == global_executable.read_bytes()
    assert (
        Path(result["distribution_root"]).joinpath("vendor/companion").read_text()
        == "native-resource"
    )
    assert result["source"] == "adopted-content-pin"
    assert (
        result["source_commit"] is None
    )  # An installed version is not evidence of a source commit.
    assert installer.resolve_lemex_executable() == managed
    assert installer.managed_distribution_root() == Path(result["distribution_root"])
    assert {path.name: path.read_bytes() for path in native_home.iterdir()} == before
    assert installer.ensure_lemex(build=False)["sha256"] == result["sha256"]
    (package / "vendor/companion").write_text("upgraded-global-resource")
    assert installer.ensure_lemex(build=False)["sha256"] == result["sha256"]


def test_damaged_companion_is_detected_on_installer_verification(installed_package):
    result = installer.ensure_lemex(build=False)
    Path(result["distribution_root"]).joinpath("vendor/companion").write_text("modified")
    with pytest.raises(installer.LemexInstallError, match="pinned identity"):
        installer.ensure_lemex(build=False)


def test_existing_pin_does_not_rediscover_or_build_a_runtime(installed_package, monkeypatch):
    """Keep ordinary installation checks offline and stable after user upgrades."""
    result = installer.ensure_lemex(build=False)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("An existing pin must not discover or build another runtime")

    monkeypatch.setattr(installer.shutil, "which", unexpected)
    monkeypatch.setattr(installer, "_build_from_source", unexpected)
    assert installer.ensure_lemex() == result


def test_failed_publication_leaves_the_current_pin_and_object_intact(installed_package):
    """Leave readers on the complete existing bundle when a replacement fails."""
    package, executable, _native_home = installed_package
    result = installer.ensure_lemex(build=False)
    pointer = installer._root() / "current.json"
    before = pointer.read_bytes()
    old_executable = Path(result["executable"])
    old_content = old_executable.read_bytes()
    executable.write_text("#!/bin/sh\necho incompatible\n")
    with pytest.raises(installer.LemexInstallError, match="exec protocol"):
        installer._publish(
            package,
            executable.relative_to(package),
            source="adopted-content-pin",
            commit=None,
        )
    assert pointer.read_bytes() == before
    assert old_executable.read_bytes() == old_content
    assert installer.resolve_lemex_executable() == old_executable


def test_refresh_adopts_local_upgrade_without_changing_old_objects_or_native_home(
    installed_package,
):
    package, executable, native_home = installed_package
    old = installer.ensure_lemex(build=False)
    native_before = {path.name: path.read_bytes() for path in native_home.iterdir()}
    old_executable = Path(old["executable"])
    old_content = old_executable.read_bytes()
    (package / "vendor/companion").write_text("updated-native-resource")
    executable.write_text(executable.read_text().replace("lemex-0.0.0", "lemex-0.1.0"))
    new = installer.ensure_lemex(build=False, refresh=True)
    assert new["sha256"] != old["sha256"]
    assert new["version"] == "lemex-0.1.0"
    assert new["source"] == "adopted-content-pin"
    assert new["source_commit"] is None
    assert installer.resolve_lemex_executable() == Path(new["executable"])
    assert old_executable.read_bytes() == old_content
    assert (
        Path(old["distribution_root"]).joinpath("vendor/companion").read_text() == "native-resource"
    )
    assert (
        Path(new["distribution_root"]).joinpath("vendor/companion").read_text()
        == "updated-native-resource"
    )
    assert {path.name: path.read_bytes() for path in native_home.iterdir()} == native_before
    assert installer.ensure_lemex(build=False) == new


@pytest.mark.parametrize("replacement", ["missing", "incompatible", "managed", "symlink"])
def test_no_build_refresh_retains_healthy_pin_when_no_external_upgrade_is_usable(
    installed_package, monkeypatch, tmp_path, replacement
):
    package, executable, _native_home = installed_package
    old = installer.ensure_lemex(build=False)
    pointer = installer._root() / "current.json"
    before = pointer.read_bytes()
    if replacement == "missing":
        monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    elif replacement == "managed":
        monkeypatch.setattr(installer.shutil, "which", lambda _name: old["executable"])
    elif replacement == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("private")
        (package / "vendor/outside").symlink_to(outside)
    else:
        executable.write_text("#!/bin/sh\necho incompatible\n")
    assert installer.ensure_lemex(build=False, refresh=True) == old
    assert pointer.read_bytes() == before


def test_refresh_builds_pinned_source_when_only_managed_runtime_is_on_path(
    installed_package, monkeypatch
):
    old = installer.ensure_lemex(build=False)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: old["executable"])
    rebuilt = {"source": "pinned-source", "sha256": "new-source"}
    monkeypatch.setattr(installer, "_build_from_source", lambda: rebuilt)
    assert installer.ensure_lemex(refresh=True) == rebuilt


def test_refresh_without_a_runtime_or_build_permission_reports_missing_install(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "leanflow"))
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    with pytest.raises(installer.LemexInstallError, match="No compatible"):
        installer.ensure_lemex(build=False, refresh=True)


def test_refresh_verifies_new_bundle_before_atomic_pointer_switch(installed_package, monkeypatch):
    package, _executable, _native_home = installed_package
    old = installer.ensure_lemex(build=False)
    (package / "vendor/companion").write_text("updated-native-resource")
    probe = installer.probe_lemex
    inspected = []

    def check(executable):
        inspected.append(executable)
        assert installer.resolve_lemex_executable() == Path(old["executable"])
        return probe(executable)

    monkeypatch.setattr(installer, "probe_lemex", check)
    new = installer.ensure_lemex(build=False, refresh=True)
    assert len(inspected) == 2
    assert inspected[0] == Path(old["executable"])
    assert ".staging-" in str(inspected[1])
    assert installer.resolve_lemex_executable() == Path(new["executable"])


def test_cli_refresh_and_no_build_are_forwarded_explicitly(monkeypatch, capsys):
    calls = []

    def ensure(**kwargs):
        calls.append(kwargs)
        return {
            "executable": "/managed/lemex",
            "source": "adopted-content-pin",
            "version": "lemex-0.1.0",
        }

    monkeypatch.setattr(installer, "ensure_lemex", ensure)
    monkeypatch.setattr(sys, "argv", ["lemex_install", "--refresh", "--no-build"])
    assert installer.main() == 0
    assert calls == [{"build": False, "refresh": True}]
    assert "lemex-0.1.0" in capsys.readouterr().out


def test_native_source_bundle_is_adopted_with_companions(installed_package, monkeypatch):
    """Keep an existing native installation usable without rebuilding or copying auth."""
    package, executable, native_home = installed_package
    (package / "package.json").unlink()
    (package / "lemex-install.json").write_text(json.dumps({"source_commit": "a" * 40}))
    (package / "codex-package.json").write_text('{"version":"0.0.0"}')
    (package / "codex-path").mkdir()
    (package / "codex-path/rg").write_bytes(b"search companion")
    executable.write_bytes(b"\x7fELFtest-native-executable")
    monkeypatch.setattr(installer, "probe_lemex", lambda path: "lemex 0.0.0")
    before = (native_home / "auth.json").read_bytes()
    result = installer.ensure_lemex(build=False)
    assert result["source"] == "adopted-content-pin"
    assert result["source_commit"] is None
    assert (
        Path(result["distribution_root"]).joinpath("codex-path/rg").read_bytes()
        == b"search companion"
    )
    assert Path(result["executable"]).read_bytes() == executable.read_bytes()
    assert (native_home / "auth.json").read_bytes() == before


def test_failed_compatibility_check_does_not_publish_a_pointer(installed_package):
    _package, executable, _native_home = installed_package
    executable.write_text("#!/bin/sh\necho incompatible\n")
    with pytest.raises(installer.LemexInstallError, match="exec protocol"):
        installer.ensure_lemex(build=False)
    assert not (installer._root() / "current.json").exists()


def test_adoption_rejects_symlinks_into_unrelated_files(installed_package, tmp_path):
    package, _executable, _native_home = installed_package
    outside = tmp_path / "outside"
    outside.write_text("private")
    (package / "vendor" / "outside").symlink_to(outside)
    with pytest.raises(installer.LemexInstallError, match="symlink"):
        installer.ensure_lemex(build=False)
    assert not (installer._root() / "current.json").exists()


def test_manifest_cannot_escape_distribution(installed_package):
    installer.ensure_lemex(build=False)
    pointer = installer._root() / "current.json"
    manifest = json.loads(pointer.read_text())
    manifest["entrypoint"] = "../../outside"
    pointer.write_text(json.dumps(manifest))
    with pytest.raises(installer.LemexInstallError, match="escapes"):
        installer.resolve_lemex_executable()


def test_pinned_source_build_checks_commit_and_keeps_private_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "leanflow"))
    monkeypatch.setenv("LEMEX_HOME", str(tmp_path / "native-lemex"))
    monkeypatch.setenv("LEMEX_API_KEY", "inherited-native-key")
    monkeypatch.setenv("LEMEX_COPY_CONFIG_FROM", "must-not-import")
    monkeypatch.setattr(
        installer.shutil, "which", lambda name: None if name == "lemex" else f"/bin/{name}"
    )
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(
        installer.subprocess,
        "check_output",
        lambda args, **kwargs: installer.LEMEX_SOURCE_COMMIT + "\n",
    )
    package = tmp_path / "package"
    package.mkdir()
    fake_exec = package / "lemex"
    fake_exec.touch()
    monkeypatch.setattr(installer, "_package_root", lambda executable: package)
    monkeypatch.setattr(
        Path, "resolve", lambda self, **kwargs: fake_exec if self.name == "lemex" else self
    )
    monkeypatch.setattr(installer, "_publish", lambda package, entrypoint, **kwargs: kwargs)
    result = installer.ensure_lemex()
    assert result["commit"] == installer.LEMEX_SOURCE_COMMIT
    fetch = next(args for args, kwargs in calls if "fetch" in args)
    assert fetch[-2:] == [installer.LEMEX_SOURCE_URL, installer.LEMEX_SOURCE_COMMIT]
    build_env = calls[-1][1]["env"]
    assert str(installer._root()) in build_env["LEMEX_INSTALL_PREFIX"]
    assert build_env["STABLE_GIT_COMMIT"] == installer.LEMEX_SOURCE_COMMIT
    assert build_env["LEMEX_HOME"] == str(tmp_path / "native-lemex")
    assert build_env["LEMEX_API_KEY"] == "inherited-native-key"
    assert "LEMEX_COPY_CONFIG_FROM" not in build_env
