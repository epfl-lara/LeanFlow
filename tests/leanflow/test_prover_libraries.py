"""Verify pinned library admission, source-preserving updates, and exact rollback."""

from __future__ import annotations

import json
import sys
import tomllib
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import libraries

PIN = "a" * 40
REQUEST = {"name": "physlib", "git": "https://github.com/example/physlib", "rev": PIN}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "lakefile.toml").write_bytes(b'name = "demo"\r\n# user comment\r\n')
    (tmp_path / "lean-toolchain").write_bytes(b"leanprover/lean4:v4.30.0\n")
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"version": "1.2.0", "packages": []}))
    monkeypatch.delenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", raising=False)
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    )
    return tmp_path


def _success(root, paths, names, timeout_s):
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text())
    payload["packages"].append(
        {"name": "physlib", "url": REQUEST["git"], "rev": PIN, "type": "git"}
    )
    manifest.write_text(json.dumps(payload))
    return {"success": True, "returncode": 0, "stdout": "installed"}


def test_toml_install_appends_only_and_returns_lock_evidence(project, monkeypatch):
    original = (project / "lakefile.toml").read_bytes()
    monkeypatch.setattr(libraries, "_run_update", _success)
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] and result["status"] == "installed"
    updated = (project / "lakefile.toml").read_bytes()
    assert updated.startswith(original)
    assert tomllib.loads(updated.decode())["require"] == [REQUEST]
    assert {item["path"] for item in result["changes"]} == {
        str(project / "lakefile.toml"),
        str(project / "lake-manifest.json"),
    }
    assert result["libraries"][0]["rev"] == PIN
    assert len(result["manifest_sha256"]) == 64
    assert (project / "lean-toolchain").read_bytes() == b"leanprover/lean4:v4.30.0\n"


def test_same_pinned_install_is_idempotent(project, monkeypatch):
    monkeypatch.setattr(libraries, "_run_update", _success)
    assert libraries.install_libraries(project, [REQUEST])["accepted"]
    before = (project / "lakefile.toml").read_bytes()
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("unexpected update"))
    result = libraries.install_libraries(project, [REQUEST])
    assert result["status"] == "already_installed" and not result["changes"]
    assert (project / "lakefile.toml").read_bytes() == before


def test_helper_library_registration_is_additive_and_idempotent(project):
    lakefile = project / "lakefile.toml"
    original = lakefile.read_bytes()
    result = libraries.ensure_helper_library(project)
    assert result["accepted"] and result["status"] == "registered"
    assert lakefile.read_bytes().startswith(original)
    assert tomllib.loads(lakefile.read_text())["lean_lib"] == [{"name": "LeanFlowProofs"}]
    updated = lakefile.read_bytes()
    assert libraries.ensure_helper_library(project)["status"] == "already_registered"
    assert lakefile.read_bytes() == updated


def test_helper_lean_library_registration_preserves_original_commands(project):
    (project / "lakefile.toml").unlink()
    lakefile = project / "lakefile.lean"
    original = "import Lake\nopen Lake DSL\npackage demo\nlean_lib Demo\n"
    lakefile.write_text(original)
    result = libraries.ensure_helper_library(project)
    assert result["accepted"] and lakefile.read_text().startswith(original)
    assert lakefile.read_text().endswith("lean_lib LeanFlowProofs\n")
    assert libraries.ensure_helper_library(project)["status"] == "already_registered"


def test_incompatible_existing_helper_library_is_rejected_without_edits(project):
    lakefile = project / "lakefile.toml"
    original = 'name="demo"\n[[lean_lib]]\nname="LeanFlowProofs"\nroots=["Other"]\n'
    lakefile.write_text(original)
    assert not libraries.ensure_helper_library(project)["accepted"]
    assert lakefile.read_text() == original


def test_existing_quoted_helper_library_name_is_not_duplicated(project):
    (project / "lakefile.toml").unlink()
    lakefile = project / "lakefile.lean"
    original = "import Lake\nopen Lake DSL\npackage demo\nlean_lib «LeanFlowProofs»\n"
    lakefile.write_text(original)
    assert libraries.ensure_helper_library(project)["status"] == "already_registered"
    assert lakefile.read_text() == original


def test_lean_lakefile_uses_quoted_names_and_preserves_original_code(project, monkeypatch):
    (project / "lakefile.toml").unlink()
    original = b"import Lake\nopen Lake DSL\npackage demo\n-- original statement stays\n"
    path = project / "lakefile.lean"
    path.write_bytes(original)
    monkeypatch.setattr(libraries, "_run_update", _success)
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"]
    assert path.read_bytes().startswith(original)
    assert f'require «physlib» from git "{REQUEST["git"]}" @ "{PIN}"' in path.read_text()
    assert libraries._configuration(project)[2]["physlib"] == REQUEST


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "--evil"},
        {"name": "a/b"},
        {"rev": "main"},
        {"rev": "abc123"},
        {"git": "file:///tmp/repo"},
        {"git": "http://example.org/repo"},
        {"git": "https://u:p@example.org/repo"},
        {"git": "https://example.org/repo?x=option"},
        {"git": "https://example.org/a/../repo"},
        {"git": "https://example.org:8080/repo"},
        {"path": "../repo"},
    ],
)
def test_unsafe_or_unpinned_entries_are_rejected_without_mutation(project, monkeypatch, patch):
    original = (project / "lakefile.toml").read_bytes()
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("update"))
    result = libraries.install_libraries(project, [{**REQUEST, **patch}])
    assert not result["accepted"]
    assert (project / "lakefile.toml").read_bytes() == original


def test_private_git_host_denial_is_propagated(project, monkeypatch):
    def private(*_):
        raise ValueError("private network address")

    monkeypatch.setattr(libraries, "_public_connection", private)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and "private" in result["error"]


def test_conflicting_duplicate_request_is_rejected(project):
    result = libraries.install_libraries(project, [REQUEST, {**REQUEST, "rev": "b" * 40}])
    assert not result["accepted"] and "Conflicting" in result["error"]


def test_existing_requirement_cannot_be_replaced(project, monkeypatch):
    with (project / "lakefile.toml").open("a") as handle:
        handle.write(
            '\n[[require]]\nname="physlib"\ngit="https://github.com/example/physlib"\nrev="main"\n'
        )
    original = (project / "lakefile.toml").read_bytes()
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and "Cannot replace" in result["error"]
    assert (project / "lakefile.toml").read_bytes() == original


@pytest.mark.parametrize(
    "failure", ["nonzero", "wrong_pin", "toolchain", "config", "timeout", "existing_lock"]
)
def test_failed_update_restores_all_configuration_bytes(project, monkeypatch, failure):
    manifest = project / "lake-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "mathlib",
                        "rev": "c" * 40,
                        "url": "https://github.com/leanprover-community/mathlib4",
                        "type": "git",
                    }
                ]
            }
        )
    )
    paths = [project / name for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain")]
    originals = {path: path.read_bytes() for path in paths}

    def update(root, paths, names, timeout_s):
        _success(root, paths, names, timeout_s)
        if failure == "toolchain":
            (root / "lean-toolchain").write_text("wrong version")
        if failure == "config":
            (root / "lakefile.toml").write_text("wrong configuration")
        if failure in {"wrong_pin", "existing_lock"}:
            document = json.loads(manifest.read_text())
            document["packages"][-1 if failure == "wrong_pin" else 0]["rev"] = "d" * 40
            manifest.write_text(json.dumps(document))
        return {
            "success": failure not in {"nonzero", "timeout"},
            "returncode": 1 if failure == "nonzero" else 0,
            "timed_out": failure == "timeout",
        }

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and result["status"] == "rolled_back"
    assert result["dependency_cache_may_have_changed"]
    assert {path: path.read_bytes() for path in paths} == originals


def test_absent_manifest_is_removed_on_failure(project, monkeypatch):
    manifest = project / "lake-manifest.json"
    manifest.unlink()
    monkeypatch.setattr(libraries, "_run_update", lambda *_: {"success": False})
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and not manifest.exists()


def test_clean_room_blocks_library_git_operations(project, monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("update"))
    assert not libraries.install_libraries(project, [REQUEST])["accepted"]


def test_package_symlink_escape_is_rejected(project, monkeypatch, tmp_path_factory):
    packages = project / ".lake/packages"
    packages.mkdir(parents=True)
    (packages / "shared").symlink_to(tmp_path_factory.mktemp("external-library"))
    assert not libraries.install_libraries(project, [REQUEST])["accepted"]


def test_dependency_hook_cannot_leave_an_external_package_link(
    project, monkeypatch, tmp_path_factory
):
    original = (project / "lakefile.toml").read_bytes()
    external = tmp_path_factory.mktemp("external-post-update-library")

    def update(root, paths, names, timeout_s):
        result = _success(root, paths, names, timeout_s)
        packages = root / ".lake/packages"
        packages.mkdir(parents=True)
        (packages / "escaped").symlink_to(external)
        return result

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and result["status"] == "rolled_back"
    assert result["dependency_cache_may_have_changed"]
    assert (project / "lakefile.toml").read_bytes() == original


def test_dependency_hook_cannot_redirect_manifest(project, monkeypatch, tmp_path_factory):
    manifest = project / "lake-manifest.json"
    original = manifest.read_bytes()
    external = tmp_path_factory.mktemp("external-manifest") / "manifest.json"

    def update(root, paths, names, timeout_s):
        result = _success(root, paths, names, timeout_s)
        external.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(external)
        return result

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and result["status"] == "rolled_back"
    assert not manifest.is_symlink() and manifest.read_bytes() == original


def test_installer_requires_real_isolation_and_constrains_update_arguments(project, monkeypatch):
    captured = {}

    def isolated_command(**kwargs):
        captured.update(kwargs)
        return {"success": False, "error_code": "sandbox_unavailable"}

    monkeypatch.setitem(
        sys.modules,
        "leanflow_cli.workflows.prover.check_process",
        SimpleNamespace(isolated_command=isolated_command),
    )
    original = (project / "lakefile.toml").read_bytes()
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"]
    assert captured["network_allowed"] is True
    assert captured["argv"][-4:] == ["lake", "--keep-toolchain", "update", "physlib"]
    assert "GIT_ALLOW_PROTOCOL=https" in captured["argv"]
    assert set(captured["extra_writable_roots"]) == {
        project / "lakefile.toml",
        project / "lake-manifest.json",
        project / "lean-toolchain",
        project / ".lake",
    }
    assert (project / "lakefile.toml").read_bytes() == original
