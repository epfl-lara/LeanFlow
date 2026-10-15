"""Exercise shared setup for empty, standalone, and existing project folders."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from leanflow_cli.workflows.project_setup import inspect_project, prepare_project, service
from leanflow_cli.workflows.project_setup.scaffold import DEFAULT_TOOLCHAIN, MATHLIB_REVISION
from leanflow_cli.workflows.project_setup.verification import resolve_repl_revision


@pytest.fixture
def fake_tools(monkeypatch, tmp_path):
    """Provide deterministic Lake commands while preserving filesystem semantics."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service, "find_executable", lambda tool: "/tools/" + tool)
    calls = []
    failure = {"command": None, "protocol": None}

    def run(
        command,
        *,
        cwd,
        log,
        cancel,
        progress,
        phase,
        input_text=None,
        stdout_only=False,
        stdout_limit_bytes=None,
        execution_policy=None,
    ):
        calls.append(command)
        if command == failure["command"]:
            return 1, "network unavailable"
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return 0, "123\trefs/tags/v4.20.0\n"
        if command[:2] == ["lake", "update"]:
            path = cwd / "lake-manifest.json"
            manifest = json.loads(path.read_text()) if path.exists() else {"packages": []}
            if not any(item["name"] == "repl" for item in manifest["packages"]):
                manifest["packages"].append({"name": "repl", "rev": "repl-pin"})
            path.write_text(json.dumps(manifest))
        if command == ["lake", "exe", "cache", "get"]:
            library = cwd / ".lake/packages/mathlib/.lake/build/lib/lean"
            for name in ("Mathlib.olean", "Mathlib/Data/Nat/Basic.olean"):
                artifact = library / name
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"cached")
        if command == ["lake", "build", "repl"]:
            binary = cwd / ".lake/packages/repl/.lake/build/bin/repl"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        if command[:2] == ["lake", "env"]:
            assert input_text is not None
            if failure["protocol"] is not None:
                return 0, failure["protocol"]
            payload = json.loads(input_text)
            if "import Missing" in payload["cmd"]:
                return 0, json.dumps(
                    {"messages": [{"severity": "error", "data": "Missing library"}]}
                )
            return 0, '{"env":0,"messages":[],"sorries":[]}\n\n'
        return 0, ""

    monkeypatch.setattr(service, "run_command", run)
    return calls, failure


def existing_project(path: Path, *, lean_file=False):
    path.mkdir()
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    if lean_file:
        (path / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n")
    else:
        (path / "lakefile.toml").write_text('name = "demo"\n')
    return path


def test_empty_folder_defaults_to_pinned_mathlib_and_real_protocol(tmp_path, fake_tools):
    root = tmp_path / "New Project"
    events = []
    result = prepare_project(root, progress=events.append)
    assert result["status"] == "ready"
    assert result["capabilities"]["verification"] is True
    assert result["capabilities"]["mathlib"] is True
    assert (root / "lean-toolchain").read_text().strip() == DEFAULT_TOOLCHAIN
    assert MATHLIB_REVISION in (root / "lakefile.toml").read_text()
    assert "sorry" in (root / "Main.lean").read_text()
    assert ["lake", "exe", "cache", "get"] in fake_tools[0]
    assert any(command[:2] == ["lake", "env"] for command in fake_tools[0])
    assert inspect_project(root)["ready"] is True
    assert events[-1]["phase"] == "checking_readiness"


def test_quoted_scoped_requirements_prepare_mathlib(tmp_path, fake_tools):
    """Download library builds for Lake's quoted scope/name syntax."""
    root = existing_project(tmp_path / "quoted", lean_file=True)
    with (root / "lakefile.lean").open("a") as stream:
        stream.write(
            '\nrequire "leanprover-community" / "mathlib" from git "https://example.org/mathlib"\n'
        )
        stream.write('/- require "ignored" / "comment" -/\n')
        stream.write('def note := "\nrequire fake\n"\n')
    # The executable custom command needs its own REPL requirement; setup may
    # inspect dependencies without guessing how to extend that custom DSL.
    from leanflow_cli.workflows.project_setup.verification import dependency_names

    assert dependency_names(root) == {"mathlib"}
    (root / "lakefile.lean").write_text(
        'import Lake\nopen Lake DSL\npackage demo\nrequire "leanprover-community" / "mathlib" from git "https://example.org/mathlib"\n'
    )
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert result["capabilities"]["mathlib"] is True
    assert ["lake", "exe", "cache", "get"] in fake_tools[0]


def test_standalone_file_is_preserved_byte_for_byte(tmp_path, fake_tools):
    root = tmp_path / "standalone"
    root.mkdir()
    source = b"import Mathlib\r\n\r\ntheorem original : True := by\r\n  sorry\r\n"
    (root / "Problem.lean").write_bytes(source)
    assert inspect_project(root)["kind"] == "standalone"
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert (root / "Problem.lean").read_bytes() == source
    assert not (root / "Main.lean").exists()


def test_existing_project_retains_toolchain_source_and_dependency_pins(tmp_path, fake_tools):
    root = existing_project(tmp_path / "existing")
    source = b"theorem old : True := by sorry\n"
    (root / "Main.lean").write_bytes(source)
    pins = {
        "packages": [{"name": "custom", "rev": "exact-old-sha", "url": "https://example.org/lib"}]
    }
    (root / "lake-manifest.json").write_text(json.dumps(pins))
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert (root / "Main.lean").read_bytes() == source
    assert (root / "lean-toolchain").read_text() == "leanprover/lean4:v4.20.0\n"
    assert (
        json.loads((root / "lake-manifest.json").read_text())["packages"][0] == pins["packages"][0]
    )
    assert ["lake", "update"] not in fake_tools[0]


def test_retry_reuses_scaffold_and_does_not_duplicate_repl(tmp_path, fake_tools):
    root = tmp_path / "retry"
    fake_tools[1]["command"] = ["lake", "build", "repl"]
    failure = prepare_project(root)
    source = (root / "Main.lean").read_bytes()
    config = (root / "lakefile.toml").read_bytes()
    assert failure["status"] == "failed"
    assert failure["phase"] == "verification"
    assert failure["code"] == "repl_build_failed"
    assert failure["remediation"]
    assert Path(failure["log_path"]).is_file()
    assert inspect_project(root)["setup_status"] == "failed"
    fake_tools[1]["command"] = None
    assert prepare_project(root)["status"] == "ready"
    assert (root / "Main.lean").read_bytes() == source
    assert (root / "lakefile.toml").read_bytes() == config
    assert fake_tools[0].count(["lake", "update", "repl"]) == 1


def test_existing_nested_path_uses_actual_root(tmp_path, fake_tools):
    root = existing_project(tmp_path / "existing")
    nested = root / "A" / "B"
    nested.mkdir(parents=True)
    assert prepare_project(nested)["root"] == str(root)
    assert (root / ".leanflow/project.yaml").exists()
    assert not (nested / ".leanflow").exists()


def test_inspection_is_read_only_and_manifest_is_not_readiness(tmp_path, fake_tools):
    root = tmp_path / "absent"
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    assert inspect_project(root)["ready"] is False
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    root = existing_project(tmp_path / "existing")
    (root / ".leanflow").mkdir()
    (root / ".leanflow/project.yaml").write_text("name: registered\n")
    result = inspect_project(root)
    assert result["configured"] is True
    assert result["ready"] is False


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        '{"messages":[]}',
        '{"env":0,"sorries":[{}]}',
        '{"env":0,"messages":[{"severity":"error"}]}',
    ],
)
def test_repl_presence_does_not_bypass_protocol_verification(tmp_path, fake_tools, response):
    root = tmp_path / "bad-protocol"
    fake_tools[1]["protocol"] = response
    result = prepare_project(root)
    assert result["status"] == "failed"
    assert result["code"].startswith("verification_")
    assert not (root / ".leanflow/project.yaml").exists()


def test_blank_toolchain_returns_precise_error_without_clobber(tmp_path, fake_tools):
    root = existing_project(tmp_path / "blank")
    (root / "lean-toolchain").write_text("  \n")
    result = prepare_project(root)
    assert result["code"] == "toolchain_empty"
    assert (root / "lean-toolchain").read_text() == "  \n"
    assert fake_tools[0] == []


def test_missing_lake_explains_install_and_writes_no_project_files(
    tmp_path, fake_tools, monkeypatch
):
    root = tmp_path / "missing"
    monkeypatch.setattr(service, "find_executable", lambda name: None if name == "lake" else "/git")
    result = prepare_project(root)
    assert result["code"] == "lake_missing"
    assert "Elan" in result["remediation"]
    assert not root.exists()


def test_cancel_before_start_creates_no_project(tmp_path, fake_tools):
    cancel = threading.Event()
    cancel.set()
    root = tmp_path / "cancel"
    assert prepare_project(root, cancel=cancel)["status"] == "cancelled"
    assert not root.exists()
    assert fake_tools[0] == []


def test_loose_file_copy_preserves_original_and_conflict_is_actionable(tmp_path, fake_tools):
    source = tmp_path / "Problem.lean"
    source.write_bytes(b"theorem copied : True := by sorry\n")
    root = tmp_path / "destination"
    assert prepare_project(root, source_file=source)["status"] == "ready"
    assert (root / source.name).read_bytes() == source.read_bytes()
    (root / source.name).write_text("different")
    assert prepare_project(root, source_file=source)["code"] == "source_conflict"
    assert (root / source.name).read_text() == "different"


def test_unknown_custom_import_is_source_diagnostic_not_install_failure(tmp_path, fake_tools):
    root = tmp_path / "custom"
    root.mkdir()
    (root / "Problem.lean").write_text("import Missing.Library\ntheorem foo : True := by sorry\n")
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert (
        "Could not locate import Missing.Library" in result["capabilities"]["source_diagnostics"][0]
    )


def test_executable_lakefile_requires_explicit_safe_dependency(tmp_path, fake_tools):
    root = existing_project(tmp_path / "lean", lean_file=True)
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\nnamespace Custom\npackage demo\nend Custom\n"
    )
    original = (root / "lakefile.lean").read_bytes()
    result = prepare_project(root)
    assert result["code"] == "repl_dependency_required"
    assert "require repl" in result["remediation"]
    assert (root / "lakefile.lean").read_bytes() == original


def test_receipt_invalidates_when_toolchain_changes_or_binary_disappears(tmp_path, fake_tools):
    root = tmp_path / "ready"
    result = prepare_project(root)
    assert inspect_project(root)["ready"] is True
    binary = Path(result["capabilities"]["repl_path"])
    binary.unlink()
    assert inspect_project(root)["ready"] is False
    prepare_project(root)
    (root / "lean-toolchain").write_text("new-toolchain\n")
    assert inspect_project(root)["code"] == "readiness_stale"


@pytest.mark.parametrize(
    ("version", "tags", "expected"),
    [
        ("v4.33.1", ["v4.33.0", "v4.33.1"], "v4.33.1"),
        ("v4.33.1", ["v4.33.0", "v4.34.0"], "v4.33.0"),
        ("v4.34.0-rc2", ["v4.34.0-rc1", "v4.34.0"], "v4.34.0-rc1"),
        ("v4.35.0", ["v4.34.0"], ""),
    ],
)
def test_repl_revision_selection(version, tags, expected):
    assert resolve_repl_revision(version, tags=tags)["rev"] == expected


def test_dependency_pin_conflict_restores_original_manifest(tmp_path, fake_tools, monkeypatch):
    root = existing_project(tmp_path / "conflict")
    previous = b'{"packages": [{"name": "custom", "rev": "keep-this"}]}\n'
    (root / "lake-manifest.json").write_bytes(previous)
    original = service.run_command

    def change_pin(command, **kwargs):
        result = original(command, **kwargs)
        if command == ["lake", "update", "repl"]:
            (root / "lake-manifest.json").write_text(
                '{"packages": [{"name": "custom", "rev": "changed"}]}'
            )
        return result

    monkeypatch.setattr(service, "run_command", change_pin)
    result = prepare_project(root)
    assert result["code"] == "dependency_pin_conflict"
    assert (root / "lake-manifest.json").read_bytes() == previous
    assert ["lake", "build", "repl"] not in fake_tools[0]


def test_cancellation_after_download_keeps_files_for_retry(tmp_path, fake_tools, monkeypatch):
    root = tmp_path / "cancel-during"
    cancel = threading.Event()
    original = service.run_command

    def cancel_download(command, **kwargs):
        result = original(command, **kwargs)
        if command == ["lake", "update", "repl"]:
            cancel.set()
        return result

    monkeypatch.setattr(service, "run_command", cancel_download)
    result = prepare_project(root, cancel=cancel)
    assert result["status"] == "cancelled"
    assert (root / "lake-manifest.json").exists()
    assert not (root / ".leanflow/project.yaml").exists()
    assert ["lake", "build", "repl"] not in fake_tools[0]


def test_invalid_existing_manifest_returns_registration_error(tmp_path, fake_tools):
    root = existing_project(tmp_path / "registration")
    (root / ".leanflow").mkdir()
    (root / ".leanflow/project.yaml").write_text("not: a valid project\n")
    result = prepare_project(root)
    assert result["code"] == "registration_failed"
    assert inspect_project(root)["ready"] is False


def test_lean_preset_does_not_download_mathlib(tmp_path, fake_tools):
    root = tmp_path / "lean-only"
    result = prepare_project(root, preset="lean")
    assert result["status"] == "ready"
    assert result["capabilities"]["mathlib"] is False
    assert ["lake", "exe", "cache", "get"] not in fake_tools[0]


def test_failed_mathlib_cache_step_is_actionable_and_retryable(tmp_path, fake_tools):
    fake_tools[1]["command"] = ["lake", "exe", "cache", "get"]
    result = prepare_project(tmp_path / "cache")
    assert result["code"] == "mathlib_cache_failed"
    assert result["phase"] == "cache"
    assert "retry" in result["remediation"]
    assert ["lake", "build", "repl"] not in fake_tools[0]


@pytest.mark.parametrize("failure", ["command", "cancel", "toolchain"])
def test_update_failure_restores_original_authority(tmp_path, fake_tools, monkeypatch, failure):
    root = existing_project(tmp_path / failure)
    manifest = b'{"packages": [{"name": "keep", "rev": "old"}]}'
    toolchain = (root / "lean-toolchain").read_bytes()
    (root / "lake-manifest.json").write_bytes(manifest)
    original = service.run_command

    def fail_update(command, **kwargs):
        if command == ["lake", "update", "repl"]:
            (root / "lean-toolchain").write_text("new-version")
            if failure != "toolchain":
                (root / "lake-manifest.json").write_text('{"packages": []}')
            if failure == "cancel":
                raise service.SetupCancelled()
            return (1 if failure == "command" else 0), "update failed"
        return original(command, **kwargs)

    monkeypatch.setattr(service, "run_command", fail_update)
    result = prepare_project(root)
    assert result["status"] == ("cancelled" if failure == "cancel" else "failed")
    assert (root / "lean-toolchain").read_bytes() == toolchain
    assert (root / "lake-manifest.json").read_bytes() == manifest


@pytest.mark.parametrize(
    "name", ["lakefile.toml", "lean-toolchain", "lake-manifest.json", ".lake", ".leanflow"]
)
def test_setup_rejects_shared_mutable_paths(tmp_path, fake_tools, name):
    root = existing_project(tmp_path / "shared")
    sibling = tmp_path / "sibling"
    if name.startswith("."):
        sibling.mkdir()
    else:
        sibling.write_text('name = "sibling"')
    target = root / name
    target.unlink(missing_ok=True)
    target.symlink_to(sibling, target_is_directory=name.startswith("."))
    result = prepare_project(root)
    assert result["code"] == "shared_setup_path"
    assert fake_tools[0] == []
    assert (
        list(sibling.iterdir()) == []
        if sibling.is_dir()
        else sibling.read_text() == 'name = "sibling"'
    )


def test_inspection_invalidates_deleted_dependency_artifacts(tmp_path, fake_tools):
    root = tmp_path / "dependency-gone"
    artifact = root / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"cached")
    prepare_project(root)
    assert inspect_project(root)["ready"] is True
    artifact.unlink()
    assert inspect_project(root)["ready"] is False


def test_retry_matching_template_reuses_checkout(tmp_path, fake_tools, monkeypatch):
    root = existing_project(tmp_path / "template")
    (root / ".git").mkdir()
    original = service.run_command

    def origin(command, **kwargs):
        if command == ["git", "config", "--get", "remote.origin.url"]:
            return 0, "https://example.com/template.git\n"
        return original(command, **kwargs)

    monkeypatch.setattr(service, "run_command", origin)
    result = prepare_project(root, template_source="https://example.com/template.git")
    assert result["status"] == "ready"
    assert not any(command[:2] == ["git", "clone"] for command in fake_tools[0])


def test_unreadable_source_encoding_is_diagnostic_only(tmp_path, fake_tools):
    root = tmp_path / "bad-encoding"
    root.mkdir()
    source = b"\xffnot utf8"
    (root / "Problem.lean").write_bytes(source)
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert "Cannot inspect imports" in result["capabilities"]["source_diagnostics"][0]
    assert (root / "Problem.lean").read_bytes() == source


def test_standard_lean_lakefile_is_extended_without_rewriting_original(tmp_path, fake_tools):
    root = existing_project(tmp_path / "ordinary", lean_file=True)
    original = (root / "lakefile.lean").read_bytes()
    assert prepare_project(root)["status"] == "ready"
    assert (root / "lakefile.lean").read_bytes().startswith(original)
    assert "require repl from git" in (root / "lakefile.lean").read_text()
    assert prepare_project(root)["status"] == "ready"
    assert (root / "lakefile.lean").read_text().count("require repl from git") == 1


def test_unwritable_home_returns_structured_error(tmp_path, fake_tools, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a state directory")
    monkeypatch.setenv("LEANFLOW_HOME", str(blocked))
    result = prepare_project(tmp_path / "destination")
    assert result["status"] == "failed"
    assert result["code"] == "setup_io_failed"
    assert not (tmp_path / "destination").exists()


def test_only_lightweight_readiness_query_starts_a_lean_environment(
    tmp_path, fake_tools, monkeypatch
):
    root = tmp_path / "protocol-streams"
    root.mkdir()
    (root / "Problem.lean").write_text("import Custom.Library\ntheorem foo : True := by sorry\n")
    original = service.run_command
    requests = []

    def check_streams(command, **kwargs):
        if command[:2] == ["lake", "env"]:
            requests.append(kwargs.get("stdout_only"))
            assert "import Mathlib\n" not in kwargs["input_text"]
        return original(command, **kwargs)

    monkeypatch.setattr(service, "run_command", check_streams)
    assert prepare_project(root)["status"] == "ready"
    assert requests == [True]


def test_unchanged_verified_setup_starts_no_processes(tmp_path, fake_tools):
    root = tmp_path / "already-ready"
    assert prepare_project(root)["status"] == "ready"
    before = (root / "lake-manifest.json").read_bytes()
    fake_tools[0].clear()
    result = prepare_project(root)
    assert result["status"] == "ready"
    assert result["changed_files"] == []
    assert fake_tools[0] == []
    assert "Already set up" in result["message"]
    assert inspect_project(root)["log_path"] == result["log_path"]
    assert (root / "lake-manifest.json").read_bytes() == before


def test_legacy_installed_project_uses_lightweight_check_without_downloads_or_builds(
    tmp_path, fake_tools
):
    from leanflow_cli.workflows.project_setup.state import state_directory

    root = tmp_path / "legacy"
    assert prepare_project(root)["status"] == "ready"
    (state_directory(root.resolve()) / "result.json").unlink()
    report = inspect_project(root)
    assert report["setup_status"] == "detected"
    assert not report["ready"]
    fake_tools[0].clear()
    assert prepare_project(root)["status"] == "ready"
    assert ["lake", "exe", "cache", "get"] not in fake_tools[0]
    assert ["lake", "build", "repl"] not in fake_tools[0]
    assert len([cmd for cmd in fake_tools[0] if cmd[:2] == ["lake", "env"]]) == 1


def test_explicit_repair_bypasses_cached_readiness(tmp_path, fake_tools):
    root = tmp_path / "repair"
    assert prepare_project(root)["status"] == "ready"
    fake_tools[0].clear()
    assert prepare_project(root, repair=True)["status"] == "ready"
    assert ["lake", "exe", "cache", "get"] in fake_tools[0]
    assert ["lake", "build", "repl"] in fake_tools[0]


def test_missing_small_mathlib_artifact_invalidates_ready_cache(tmp_path, fake_tools):
    root = tmp_path / "missing-artifact"
    assert prepare_project(root)["status"] == "ready"
    (root / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib/Data/Nat/Basic.olean").unlink()
    assert inspect_project(root)["code"] == "readiness_stale"
    fake_tools[0].clear()
    assert prepare_project(root)["status"] == "ready"
    assert ["lake", "exe", "cache", "get"] in fake_tools[0]


def test_cached_setup_still_checks_registration_and_active_owner(tmp_path, fake_tools):
    from leanflow_cli.runtime.project_operations import project_activity

    root = tmp_path / "guarded"
    assert prepare_project(root)["status"] == "ready"
    fake_tools[0].clear()
    with project_activity(root):
        assert prepare_project(root)["code"] == "project_busy"
    (root / ".leanflow/project.yaml").write_text("bad: registration")
    assert prepare_project(root)["code"] == "registration_failed"
    assert fake_tools[0] == []


def test_progress_reports_live_log_path(tmp_path, fake_tools):
    events = []
    result = prepare_project(tmp_path / "live-log", progress=events.append)
    assert events
    assert all(
        event["log_path"] == result["log_path"] for event in events if event["type"] == "progress"
    )
