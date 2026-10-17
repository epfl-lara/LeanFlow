"""Verify pinned library admission, source-preserving updates, and exact rollback."""

from __future__ import annotations

import json
import sys
import tomllib
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import libraries
from leanflow_cli.workflows.prover.config import ProverConfig
from tests.leanflow.library_checkout_fixture import PIN, REQUEST, create_checkout


def test_configuration_preserves_scoped_multiline_lake_dependencies(tmp_path):
    source = (
        "import Lake\nopen Lake DSL\npackage demo\n"
        'require "leanprover-community" / "mathlib"\n'
        '  from git "https://github.com/leanprover-community/mathlib4.git" @ "' + PIN + '"\n'
        'require repl from git "https://github.com/leanprover-community/repl" @ "v4.35.0-rc1"\n'
    )
    path = tmp_path / "lakefile.lean"
    path.write_text(source)
    parsed, original, known = libraries._configuration(tmp_path)
    assert parsed == path and original == source.encode()
    assert known["mathlib"] == {
        "name": "mathlib",
        "git": "https://github.com/leanprover-community/mathlib4.git",
        "rev": PIN,
    }
    assert known["repl"]["rev"] == "v4.35.0-rc1"


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
    create_checkout(root / ".lake/packages/physlib")
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


@pytest.mark.parametrize("timeout_s", [ProverConfig().timeout_s, 3600])
def test_installer_accepts_controller_timeout_without_shortening_it(
    project, monkeypatch, timeout_s
):
    """Pass the controller's actual limit through to the isolated dependency update."""
    observed = []

    def update(root, paths, names, timeout):
        observed.append(timeout)
        return _success(root, paths, names, timeout)

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST], timeout_s=timeout_s)
    assert result["accepted"], result
    assert len(observed) == 1
    if timeout_s is None:
        assert observed[0] is None
    else:
        assert timeout_s - 10 <= observed[0] <= timeout_s


def test_accepted_online_plan_installs_with_real_controller_defaults(project, monkeypatch):
    """Cross the review-to-installer boundary with only the model and Lake process mocked."""
    from leanflow_cli.workflows.prover import planning_controller
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

    source = project / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    calls, timeouts = [], []

    def session(**kwargs):
        calls.append(kwargs["role"])
        if len(calls) > 3:
            pytest.fail("Accepted plan returned to planning instead of installing")
        report = (
            {"accepted": True}
            if kwargs["role"] == "review"
            else {"plan": "Use the pinned library", "nodes": [], "libraries": [REQUEST]}
        )
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    def update(root, paths, names, timeout):
        timeouts.append(timeout)
        return _success(root, paths, names, timeout)

    monkeypatch.setattr(libraries, "_run_update", update)
    monkeypatch.setattr(planning_controller, "materialize", lambda *_: None)
    owner = ProverRuntime(
        root=project, targets=[source], config=ProverConfig(mode="research"), session=session
    )
    assert owner._research_plan("Plan with a new dependency")
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 86400
    assert owner.state["proposal_status"] == "accepted"
    assert owner.state["libraries"][0]["status"] == "installed"


@pytest.mark.parametrize("timeout_s", [-1, True, "1200", float("inf"), float("nan")])
def test_invalid_installer_timeout_is_a_configuration_error(project, monkeypatch, timeout_s):
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("update invoked"))
    result = libraries.install_libraries(project, [REQUEST], timeout_s=timeout_s)
    assert not result["accepted"]
    assert result.get("status") == "configuration_error"
    assert result.get("error_code") == "invalid_timeout"


@pytest.mark.parametrize("name", ["lake-manifest.json", "lean-toolchain"])
def test_oversized_library_configuration_fails_before_effects(project, monkeypatch, name):
    """Bound configuration snapshots before JSON parsing or private Lake admission."""
    path = project / name
    with path.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    inode = path.stat().st_ino
    monkeypatch.setattr(libraries, "_run_update", lambda *_: pytest.fail("effectful Lake started"))
    result = libraries.install_libraries(project, [REQUEST])
    assert result["accepted"] is False and "bounded" in result["error"]
    assert path.stat().st_size == 8 * 1024 * 1024 + 1 and path.stat().st_ino == inode
    assert not (project / ".lake").exists()


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
            private_manifest = root / "lake-manifest.json"
            document = json.loads(private_manifest.read_text())
            document["packages"][-1 if failure == "wrong_pin" else 0]["rev"] = "d" * 40
            private_manifest.write_text(json.dumps(document))
        return {
            "success": failure not in {"nonzero", "timeout"},
            "returncode": 1 if failure == "nonzero" else 0,
            "timed_out": failure == "timeout",
        }

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and result["status"] == "rolled_back"
    assert result["dependency_cache_may_have_changed"] is False
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
        packages.mkdir(parents=True, exist_ok=True)
        (packages / "escaped").symlink_to(external)
        return result

    monkeypatch.setattr(libraries, "_run_update", update)
    result = libraries.install_libraries(project, [REQUEST])
    assert not result["accepted"] and result["status"] == "rolled_back"
    assert result["dependency_cache_may_have_changed"] is False
    assert (project / "lakefile.toml").read_bytes() == original


def test_dependency_hook_cannot_redirect_manifest(project, monkeypatch, tmp_path_factory):
    manifest = project / "lake-manifest.json"
    original = manifest.read_bytes()
    external = tmp_path_factory.mktemp("external-manifest") / "manifest.json"

    def update(root, paths, names, timeout_s):
        result = _success(root, paths, names, timeout_s)
        private_manifest = root / "lake-manifest.json"
        external.write_bytes(private_manifest.read_bytes())
        private_manifest.unlink()
        private_manifest.symlink_to(external)
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
    private_root = captured["project_root"]
    assert private_root != project and private_root.is_relative_to(project / ".leanflow")
    assert set(captured["extra_writable_roots"]) == {
        private_root / "lakefile.toml",
        private_root / "lake-manifest.json",
        private_root / "lean-toolchain",
        private_root / ".lake",
    }
    assert (project / "lakefile.toml").read_bytes() == original
