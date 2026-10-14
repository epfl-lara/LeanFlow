"""Verify cold scoped imports, safe cache refresh and evaluated custom source membership."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization.evidence import digest
from leanflow_cli.workflows.prover import scoped_imports
from leanflow_cli.workflows.prover.source import discover
from leanflow_cli.workflows.prover.verification import LeanVerifier


@pytest.fixture
def fake_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / "Main").mkdir(parents=True)
    (root / "Main/Base.lean").write_text("def value : Nat := 1\n")
    (root / "Main/Goal.lean").write_text("import Main.Base\ntheorem goal : value = 1 := by sorry\n")
    (root / "lakefile.toml").write_text('name = "test"\n')
    layout = {
        "accepted": True,
        "lean_lib_dir": ".lake/build/lib/lean",
        "configuration_sha256": digest((root / "lakefile.toml").read_bytes()),
    }
    local = {"Main.Base": root / "Main/Base.lean", "Main.Goal": root / "Main/Goal.lean"}
    monkeypatch.setattr(scoped_imports, "inspect_layout", lambda *_, **__: layout)
    monkeypatch.setattr(
        scoped_imports,
        "validate_build_inputs",
        lambda *_, **__: {"local": local, "library": root / ".lake/build/lib/lean"},
    )
    return root, {
        path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in local.values()
    }


def fake_build(
    root: Path, layout: dict[str, Any], workspace: Path, **kwargs: Any
) -> dict[str, Any]:
    """Emit controlled private public/server/IR parts without a canonical write grant."""
    output = kwargs["output_library"] / "Main/Base.olean"
    output.parent.mkdir(parents=True)
    paths = (output, Path(str(output) + ".server"), output.with_suffix(".ir"))
    for path in paths:
        path.write_bytes(("new:" + path.name).encode())
    return {
        "success": True,
        "modules": ["Main.Base"],
        "source_sha256": {
            file: digest((root / file).read_bytes())
            for file in ("Main/Base.lean", "Main/Goal.lean")
        },
        "local_artifacts": {
            "Main.Base": {"sha256": {str(path): digest(path.read_bytes()) for path in paths}}
        },
    }


def test_refresh_preserves_observed_cache_inode_and_all_source_bytes(fake_scope, monkeypatch):
    root, sources = fake_scope
    old = root / ".lake/build/lib/lean/Main/Base.olean"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"previous checked proof")
    inode = old.stat().st_ino
    config = (root / "lakefile.toml").read_bytes()
    monkeypatch.setattr(scoped_imports, "build_project", fake_build)

    result = scoped_imports.prepare_scoped_imports(
        project_root=root,
        workspace=root / ".leanflow/checks",
        files=["Main/Goal.lean"],
        expected_modules={"Main/Goal.lean": "Main.Goal"},
        expected_sources=sources,
    )

    assert result["success"] and result["proof_acceptance"] is False
    assert len(result["artifacts"]) == 3
    saved = Path(result["retained_before_artifacts"][0])
    assert saved.read_bytes() == b"previous checked proof" and saved.stat().st_ino == inode
    assert old.read_bytes() == b"new:Base.olean"
    assert (root / "lakefile.toml").read_bytes() == config
    assert all(digest((root / file).read_bytes()) == sha for file, sha in sources.items())


def test_source_drift_blocks_all_cache_publication(fake_scope, monkeypatch):
    root, _ = fake_scope

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = fake_build(*args, **kwargs)
        (root / "Main/Base.lean").write_text("def value : Nat := 2\n")
        return result

    monkeypatch.setattr(scoped_imports, "build_project", build)
    with pytest.raises(ValueError, match="inputs changed"):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
        )
    assert not (root / ".lake/build").exists()


def test_concurrent_unknown_cache_version_is_preserved(fake_scope, monkeypatch):
    root, _ = fake_scope
    target = root / ".lake/build/lib/lean/Main/Base.olean"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = fake_build(*args, **kwargs)
        target.write_bytes(b"concurrent unknown build")
        return result

    monkeypatch.setattr(scoped_imports, "build_project", build)
    with pytest.raises(ValueError, match="changed during compilation"):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
        )
    assert target.read_bytes() == b"concurrent unknown build"


def test_linked_output_ancestor_never_reaches_an_external_cache(fake_scope, tmp_path, monkeypatch):
    root, _ = fake_scope
    external = tmp_path / "external"
    external.mkdir()
    (root / ".lake/build/lib/lean").mkdir(parents=True)
    (root / ".lake/build/lib/lean/Main").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        scoped_imports, "build_project", lambda *_args, **_kwargs: pytest.fail("unsafe output")
    )
    with pytest.raises(ValueError):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
        )
    assert not list(external.iterdir())


def test_obsolete_sidecars_are_retained_without_mixing_generations(fake_scope, monkeypatch):
    root, _ = fake_scope
    obsolete = root / ".lake/build/lib/lean/Main/Base.ir.sig"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_bytes(b"old interface")
    inode = obsolete.stat().st_ino
    monkeypatch.setattr(scoped_imports, "build_project", fake_build)
    result = scoped_imports.prepare_scoped_imports(
        project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
    )
    assert result["success"] and not obsolete.exists()
    assert obsolete.relative_to(root).as_posix() in result["retired_artifacts"]
    saved = next(
        Path(path) for path in result["retained_before_artifacts"] if path.endswith(".ir.sig")
    )
    assert saved.read_bytes() == b"old interface" and saved.stat().st_ino == inode


def test_dependency_drift_refuses_cache_readiness(fake_scope, monkeypatch):
    root, _ = fake_scope
    observed = iter([{"pins": "before"}, {"pins": "changed"}])
    monkeypatch.setattr(scoped_imports, "dependency_inputs", lambda _: next(observed))
    monkeypatch.setattr(scoped_imports, "build_project", fake_build)
    with pytest.raises(ValueError, match="dependency inputs changed"):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
        )
    assert not list((root / ".lake/build").rglob("*.olean"))
    from leanflow_cli.workflows.prover.scoped_cache import cache_refresh_needed

    assert cache_refresh_needed(root) is not None


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_expired_campaign_never_launches_private_compilation(fake_scope, monkeypatch, stop):
    root, _ = fake_scope
    monkeypatch.setattr(
        scoped_imports, "build_project", lambda *_, **__: pytest.fail("expired compilation")
    )
    with pytest.raises((TimeoutError, InterruptedError)):
        scoped_imports.prepare_scoped_imports(
            project_root=root,
            workspace=root / ".leanflow/checks",
            files=["Main/Goal.lean"],
            deadline=0.0 if stop == "deadline" else None,
            cancelled=lambda: stop == "cancelled",
        )


def test_runtime_restores_imports_with_checkpointed_kernel_signatures(tmp_path, monkeypatch):
    from tests.leanflow.test_prover_live_progress import make_runtime

    controller = make_runtime(tmp_path)
    controller.verifier = LeanVerifier(tmp_path, ())
    controller.verifier.scoped_modules = {"Main.lean": "Main"}
    controller.dag.nodes[0].signature_sha256 = "original-kernel-type"
    controller.state["phase"] = "resume"
    restored = []
    monkeypatch.setattr(controller.verifier, "preflight", lambda _: {"accepted": True})
    monkeypatch.setattr(
        controller.verifier,
        "capture_signatures",
        lambda *_, **__: pytest.fail("recaptured signatures"),
    )

    def imports(dag, documents, workspace):
        restored.append((dag, documents, workspace))
        return {"accepted": False, "error": "exact readiness stopped"}

    monkeypatch.setattr(controller.verifier, "prepare_imports", imports)
    result = controller.run()
    assert result["status"] == "environment_error"
    assert len(restored) == 1 and restored[0][0] is controller.dag
    assert result["metrics"]["api_calls"] == 0
    assert (tmp_path / "Main.lean").read_text() == "theorem goal : True := by sorry\n"


@pytest.mark.parametrize("scoped", [False, True])
def test_custom_build_snapshot_exclusion_preserves_source_and_dependencies(tmp_path, scoped):
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot

    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    source = root / "src/Main.lean"
    source.write_text("theorem pending : True := by sorry\n")
    (root / "lakefile.toml").write_text('name = "custom"\nbuildDir = "outputs"\n')
    artifact = root / "outputs/lib/lean/Unrequested.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"unrequested filtered proof cache")
    dependency = root / ".lake/packages/library/.lake/build/lib/lean/Library.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"pinned dependency")
    before = artifact.stat().st_ino
    snapshot = create_snapshot(
        root, tmp_path / "snapshot", excluded_build_dir="outputs" if scoped else None
    )
    assert (snapshot / "outputs").exists() is (not scoped)
    assert (snapshot / "src/Main.lean").read_bytes() == source.read_bytes()
    assert (snapshot / dependency.relative_to(root)).read_bytes() == b"pinned dependency"
    assert artifact.read_bytes() == b"unrequested filtered proof cache"
    assert artifact.stat().st_ino == before


def test_resumed_view_without_exclusion_provenance_preserves_unknown_cached_inputs(tmp_path):
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot, prepare_snapshot

    root = tmp_path / "project"
    (root / "artifacts/lib/lean").mkdir(parents=True)
    hidden = root / "artifacts/lib/lean/Hidden.olean"
    hidden.write_bytes(b"unknown filtered declaration cache")
    (root / "Main.lean").write_text("theorem goal : True := by sorry\n")
    snapshot = create_snapshot(root, tmp_path / "snapshot")
    copied = snapshot / hidden.relative_to(root)
    before = copied.stat().st_ino
    with pytest.raises(ValueError, match="lacks exact build-exclusion provenance"):
        prepare_snapshot(root, snapshot, excluded_build_dir="artifacts")
    assert copied.read_bytes() == b"unknown filtered declaration cache"
    assert copied.stat().st_ino == before
    assert prepare_snapshot(root, snapshot) == snapshot


def test_scoped_snapshot_resume_requires_same_directory_and_evaluated_exclusion(tmp_path):
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot, prepare_snapshot

    root = tmp_path / "project"
    root.mkdir()
    (root / "Main.lean").write_text("theorem goal : True := by sorry\n")
    snapshot = create_snapshot(root, tmp_path / "snapshot", excluded_build_dir="artifacts")
    (snapshot / "artifacts").mkdir()
    checked = snapshot / "artifacts/checked.olean"
    checked.write_bytes(b"later private controller build")
    assert prepare_snapshot(root, snapshot, excluded_build_dir="artifacts") == snapshot
    with pytest.raises(ValueError, match="lacks exact build-exclusion provenance"):
        prepare_snapshot(root, snapshot, excluded_build_dir="other-build")
    assert checked.read_bytes() == b"later private controller build"


@pytest.mark.parametrize(
    "source_dir,build_dir", [(".", ".lake/build"), ("src", ".lake/build"), ("src", "derived-cache")]
)
def test_real_cold_imports_signatures_probe_and_accepted_proof_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_dir: str, build_dir: str
) -> None:
    """Exercise the actual protected compiler and probe with no prebuilt local modules."""
    configured = os.environ.get("LEANFLOW_TEST_LEAN_PROJECT")
    if not configured:
        pytest.skip("Set LEANFLOW_TEST_LEAN_PROJECT to a prebuilt core project")
    from core.file_copy import copy_tree
    from leanflow_cli.workflows.prover.check_process import check_scratch, close_check_workers
    from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
    from leanflow_cli.workflows.prover.session_build import build_session_imports

    base = Path(configured).resolve()
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    shutil.copyfile(base / "lean-toolchain", root / "lean-toolchain")
    shutil.copyfile(base / "lake-manifest.json", root / "lake-manifest.json")
    copy_tree(
        base / ".lake/packages",
        root / ".lake/packages",
        ignore=lambda directory, names: [
            name
            for name in names
            if name in {".codex", ".claude", ".env", "credentials"}
            or (Path(directory).name == ".git" and name in {"config", "hooks"})
        ],
    )
    (root / "lakefile.toml").write_text(
        f'name = "scoped_test"\ndefaultTargets = ["Actual"]\nbuildDir = "{build_dir}"\n'
        f'[[lean_lib]]\nname = "Actual"\nsrcDir = "{source_dir}"\nroots = ["Main"]\n'
        '[[require]]\nname = "repl"\ngit = "https://github.com/leanprover-community/repl"\n'
        'rev = "bbeedf38e0898869fc3b7c009e1ea877b46204e4"\n'
    )
    prefix = "" if source_dir == "." else source_dir + "/"
    base_file, goal_file = prefix + "Main/Base.lean", prefix + "Main/Goal.lean"
    (root / base_file).parent.mkdir(parents=True)
    (root / base_file).write_text(
        "namespace Sample\ndef double (n : Nat) : Nat := n + n\n"
        "theorem earlier (n : Nat) : double n = n + n := by sorry\nend Sample\n"
    )
    (root / goal_file).write_text(
        "import Main.Base\nnamespace Sample\n"
        "theorem later (n : Nat) : double n = n + n := by sorry\nend Sample\n"
    )
    (root / (prefix + "Main.lean")).write_text("import Main.Goal\n")
    subprocess.run(
        ["lake", "env", "lean", "--version"], cwd=root, check=True, capture_output=True, timeout=30
    )
    library = root / build_dir / "lib/lean"
    assert not (library / "Main/Base.olean").exists()
    original = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*.lean")
        if ".lake" not in path.parts
    }
    config = (root / "lakefile.toml").read_bytes()
    dag, documents = discover(root, [root / goal_file, root / base_file], fill_definitions=False)
    verifier = LeanVerifier(root, (), signature_scheme="module")
    verifier.scoped_modules = {base_file: "Main.Base", goal_file: "Main.Goal"}
    verifier.artifact_root = root / ".leanflow/artifacts"
    workspace = root / ".leanflow/checks"
    try:
        captured = verifier.capture_signatures(dag, documents, workspace / "signatures")
        assert captured["accepted"], captured
        assert verifier.profile_module(base_file) == "Main.Base"
        assert all((root / file).read_bytes() == value for file, value in original.items())
        assert (root / "lakefile.toml").read_bytes() == config
        earlier = next(node for node in dag.nodes if node.name.endswith("earlier"))
        candidate = workspace / "Earlier.lean"
        candidate.write_text((root / base_file).read_text().replace("by sorry", "by rfl"))
        checked = verifier.check(earlier, candidate)
        assert checked["accepted"], checked
        (root / base_file).write_bytes(candidate.read_bytes())
        installed = verifier.install_module(
            base_file, Path(checked["compiled_artifact"]), checked["compiled_artifact_sha256"]
        )
        assert installed["accepted"], installed
        assert (library / "Main/Base.olean").is_file()
        assert not (library / "src").exists()
        updated_dag, updated_documents = discover(
            root, [root / goal_file, root / base_file], fill_definitions=False
        )
        # A fresh controller restores imports while retaining checkpointed types.
        for node in updated_dag.nodes:
            previous = next(item for item in dag.nodes if item.name == node.name)
            node.signature_sha256 = previous.signature_sha256
            node.signature_mutable_names = previous.signature_mutable_names
        resumed = LeanVerifier(root, (), signature_scheme="module")
        resumed.scoped_modules = dict(verifier.scoped_modules)
        resumed.artifact_root = verifier.artifact_root
        assert resumed.scoped_library is None
        restored = resumed.prepare_imports(updated_dag, updated_documents, workspace / "resumed")
        assert restored["accepted"], restored
        later = next(node for node in updated_dag.nodes if node.name.endswith("later"))
        later_candidate = workspace / "Later.lean"
        later_candidate.write_text(
            (root / goal_file).read_text().replace("by sorry", "by exact earlier n")
        )
        checked_later = resumed.check(later, later_candidate)
        assert (
            checked_later["accepted"] and checked_later["axiom_profile_axioms"] == []
        ), checked_later
        (root / goal_file).write_bytes(later_candidate.read_bytes())
        integrated = resumed.install_module(
            goal_file,
            Path(checked_later["compiled_artifact"]),
            checked_later["compiled_artifact_sha256"],
        )
        assert integrated["accepted"], integrated
        probe = workspace / "Probe.lean"
        probe.write_text(
            "import Main.Goal\ntheorem valid (n : Nat) : Sample.double n = n + n := Sample.later n\n"
        )
        actual = check_scratch(
            project_root=root,
            workspace=workspace,
            file=probe,
            declaration="valid",
            include_axiom_profile=True,
            timeout_s=60,
        )
        assert actual["success"] and actual["ok"] and actual["axiom_profile_checked"], actual
        assert actual["axiom_profile_axioms"] == []
        close_check_workers(workspace)
        final = resumed.final([root / base_file, root / goal_file])
        assert final["accepted"], final
        snapshot = create_snapshot(root, tmp_path / "private-project", excluded_build_dir=build_dir)
        assert not (snapshot / build_dir).exists()
        private_work = snapshot / ".leanflow-job"
        private_work.mkdir()
        private_probe = private_work / "Scratch.lean"
        private_probe.write_bytes(probe.read_bytes())
        prerequisites = build_session_imports(
            project_root=snapshot,
            workspace=private_work,
            source_file=private_probe,
            deadline=None,
            cancelled=None,
            static_bound=True,
        )
        assert (
            prerequisites["success"] and prerequisites["proof_acceptance"] is False
        ), prerequisites
        assert prerequisites["modules"] == ["Main.Base", "Main.Goal"]
        private = check_scratch(
            project_root=snapshot,
            workspace=private_work,
            file=private_probe,
            declaration="valid",
            include_axiom_profile=True,
            timeout_s=60,
        )
        assert private["success"] and private["ok"] and private["axiom_profile_checked"], private
        assert private["axiom_profile_axioms"] == []
        assert (root / goal_file).read_bytes() == later_candidate.read_bytes()
        assert (root / base_file).read_bytes() == candidate.read_bytes()
        assert (root / "lakefile.toml").read_bytes() == config
    finally:
        verifier.close(candidate.parent if "candidate" in locals() else workspace)
        close_check_workers(workspace)
        if "private_work" in locals():
            close_check_workers(private_work)


def test_scoped_configuration_drift_is_bound_before_layout_evaluation(fake_scope, monkeypatch):
    root, _ = fake_scope

    def layout(*args: Any, **kwargs: Any) -> dict[str, Any]:
        (root / "lakefile.toml").write_text('name = "changed"\n')
        return {
            "accepted": True,
            "configuration_sha256": digest((root / "lakefile.toml").read_bytes()),
            "lean_lib_dir": "derived/lib/lean",
        }

    monkeypatch.setattr(scoped_imports, "inspect_layout", layout)
    monkeypatch.setattr(
        scoped_imports,
        "build_project",
        lambda *args, **kwargs: pytest.fail("changed configuration reached compiler"),
    )
    with pytest.raises(ValueError, match="frozen configuration"):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
        )


@pytest.mark.parametrize("suffix", [".trace", ".ilean", ".ilean.hash", ".olean.hash"])
def test_scoped_batch_reuses_one_dependency_inventory_and_retires_lake_metadata(
    fake_scope, monkeypatch, suffix
):
    root, _ = fake_scope
    census = []
    inventory = {"packages": {}}
    monkeypatch.setattr(
        scoped_imports, "dependency_inputs", lambda _root: census.append(True) or inventory
    )

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["inventory"] is inventory
        return fake_build(*args, **kwargs)

    monkeypatch.setattr(scoped_imports, "build_project", build)
    trace = root / (".lake/build/lib/lean/Main/Base" + suffix)
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b"stale lake trace")
    inode = trace.stat().st_ino
    result = scoped_imports.prepare_scoped_imports(
        project_root=root, workspace=root / ".leanflow/checks", files=["Main/Goal.lean"]
    )
    assert result["success"] and len(census) == 2
    assert not trace.exists()
    saved = next(
        Path(path) for path in result["retained_before_artifacts"] if path.endswith(suffix)
    )
    assert saved.read_bytes() == b"stale lake trace" and saved.stat().st_ino == inode


@pytest.mark.integration
def test_real_lake_metadata_is_retired_before_custom_cache_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned Lake integration")
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    (root / "lean-toolchain").write_text(toolchain)
    (root / "lakefile.toml").write_text(
        'name = "actual_metadata"\nbuildDir = "derived-cache"\n'
        'defaultTargets = ["Actual"]\n[[lean_lib]]\nname = "Actual"\n'
        'srcDir = "src"\nroots = ["Support"]\n'
    )
    (root / "lake-manifest.json").write_text(
        '{"version":"1.1.0","packagesDir":".lake/packages","packages":[],"name":"actual_metadata","lakeDir":".lake"}'
    )
    source = root / "src/Support.lean"
    source.write_text("def value : Nat := 1\n")
    environment = {**os.environ, "LEAN_NUM_THREADS": "1"}
    subprocess.run(
        ["lake", "build"], cwd=root, env=environment, check=True, capture_output=True, timeout=60
    )
    library = root / "derived-cache/lib/lean"
    metadata = [library / ("Support" + suffix) for suffix in (".trace", ".ilean", ".ilean.hash")]
    assert all(path.is_file() for path in metadata)
    before = {path: (path.read_bytes(), path.stat().st_ino) for path in metadata}
    source.write_text("def value : Nat := 2\n")
    protected = {path: path.read_bytes() for path in (source, root / "lakefile.toml")}
    result = scoped_imports.prepare_scoped_imports(
        project_root=root, workspace=root / ".leanflow/checks", files=["src/Support.lean"]
    )
    assert result["success"], result
    retained = {Path(path).name: Path(path) for path in result["retained_before_artifacts"]}
    for path, (content, inode) in before.items():
        assert not path.exists()
        assert path.relative_to(root).as_posix() in result["retired_artifacts"]
        saved = retained[path.name]
        assert saved.read_bytes() == content and saved.stat().st_ino == inode
    assert all(path.read_bytes() == content for path, content in protected.items())
    subprocess.run(
        ["lake", "build"], cwd=root, env=environment, check=True, capture_output=True, timeout=60
    )
    assert all(path.is_file() for path in metadata)
    assert all(path.read_bytes() == content for path, content in protected.items())


def test_scoped_source_rollback_preserves_custom_cache_and_blocks_next_check(tmp_path: Path):
    from types import SimpleNamespace

    from leanflow_cli.workflows.prover import source_transaction
    from leanflow_cli.workflows.prover.scoped_cache import cache_refresh_needed

    root = tmp_path / "project"
    target = root / "src/Main/Goal.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem goal : True := by sorry\n")
    dag, documents = discover(root, [target], fill_definitions=False)
    before = documents["src/Main/Goal.lean"]
    state = {"changes": []}
    store = root / ".leanflow/run"
    store.mkdir(parents=True)
    verifier = LeanVerifier(root, (), signature_scheme="module")
    verifier.scoped_modules = {"src/Main/Goal.lean": "Main.Goal"}
    runtime = SimpleNamespace(
        root=root,
        documents=documents,
        dag=dag,
        state=state,
        store=SimpleNamespace(directory=store),
        verifier=verifier,
        _persist=lambda: None,
    )
    source_transaction.begin_proof(
        runtime, dag.nodes[0], ["trivial"], before, before.render().replace("sorry", "trivial")
    )
    target.write_text(before.render().replace("sorry", "trivial"))
    artifact = root / "derived-cache/lib/lean/Main/Goal.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"unknown concurrent cache")
    inode = artifact.stat().st_ino
    source_transaction.recover_proof(runtime)
    assert target.read_text() == before.render()
    assert artifact.read_bytes() == b"unknown concurrent cache" and artifact.stat().st_ino == inode
    assert cache_refresh_needed(root) is not None
    result = verifier.check(dag.nodes[0], target)
    assert result["accepted"] is False and result["error_code"] == "check_setup_failed"
    assert "needs refresh" in result["error"]


def test_private_static_imports_reports_publication_unsupported(fake_scope, monkeypatch):
    from leanflow_cli.formalization.publication import PublicationUnsupported
    from leanflow_cli.workflows.prover import session_build

    root, _ = fake_scope
    workspace = root / ".leanflow-job"
    workspace.mkdir()
    source = workspace / "Scratch.lean"
    source.write_text("import Main.Goal\n")
    monkeypatch.setattr(
        scoped_imports,
        "prepare_scoped_imports",
        lambda **kwargs: (_ for _ in ()).throw(
            PublicationUnsupported("exclusive publication unavailable")
        ),
    )
    result = session_build.build_session_imports(
        project_root=root,
        workspace=workspace,
        source_file=source,
        deadline=None,
        cancelled=None,
        static_bound=True,
    )
    assert result["success"] is False and result["error_code"] == "check_setup_failed"
    assert "exclusive publication unavailable" in result["error"]


def test_global_dirty_cache_forces_all_local_modules_before_readiness(fake_scope, monkeypatch):
    from leanflow_cli.formalization.publication import publication_guard
    from leanflow_cli.workflows.prover.scoped_cache import (
        cache_refresh_needed,
        mark_cache_refresh_needed,
    )

    root, _ = fake_scope
    with publication_guard(root):
        mark_cache_refresh_needed(root, "A previous wider proof-source install rolled back")
    seen = []

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.extend(kwargs["files"])
        result = fake_build(*args, **kwargs)
        goal = kwargs["output_library"] / "Main/Goal.olean"
        goal.write_bytes(b"new-goal")
        result["modules"].append("Main.Goal")
        result["local_artifacts"]["Main.Goal"] = {"sha256": {str(goal): digest(goal.read_bytes())}}
        return result

    monkeypatch.setattr(scoped_imports, "build_project", build)
    result = scoped_imports.prepare_scoped_imports(
        project_root=root, workspace=root / ".leanflow/checks", files=["Main/Base.lean"]
    )
    assert result["success"] and set(seen) == {"Main/Base.lean", "Main/Goal.lean"}
    assert cache_refresh_needed(root) is None


def test_new_refresh_marker_during_compilation_refuses_narrow_publication(fake_scope, monkeypatch):
    from leanflow_cli.formalization.publication import publication_guard
    from leanflow_cli.workflows.prover.scoped_cache import (
        cache_refresh_needed,
        mark_cache_refresh_needed,
    )

    root, _ = fake_scope

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = fake_build(*args, **kwargs)
        with publication_guard(root):
            mark_cache_refresh_needed(root, "Another controller stopped a wider build")
        return result

    monkeypatch.setattr(scoped_imports, "build_project", build)
    with pytest.raises(ValueError, match="refresh authority changed"):
        scoped_imports.prepare_scoped_imports(
            project_root=root, workspace=root / ".leanflow/checks", files=["Main/Base.lean"]
        )
    assert not (root / ".lake/build").exists()
    assert cache_refresh_needed(root) is not None


def test_scoped_compile_reuses_visible_verifier_owned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "Main.lean").write_text("theorem goal : True := by trivial\n")
    workspaces = []

    def prepare(**kwargs: Any) -> dict[str, Any]:
        workspaces.append(kwargs["workspace"])
        return {
            "success": True,
            "layout": {"lean_lib_dir": "derived-cache/lib/lean", "build_dir": "derived-cache"},
        }

    monkeypatch.setattr(scoped_imports, "prepare_scoped_imports", prepare)
    verifier = LeanVerifier(root, (), signature_scheme="module")
    verifier.scoped_modules = {"Main.lean": "Main"}
    assert verifier.compile_module("Main.lean")["accepted"]
    assert verifier.compile_module("Main.lean")["accepted"]
    assert workspaces[0] == workspaces[1] == verifier.scoped_workspace
    assert workspaces[0].is_relative_to(root / ".leanflow/scoped-import-workspaces")
