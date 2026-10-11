"""Exercise private Lemex installation without a network or inference request."""

from __future__ import annotations

import json
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
