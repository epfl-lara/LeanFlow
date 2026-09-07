"""Publish the artifact an accepted check compiled instead of compiling the same bytes again."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process, type_profile
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import candidate_feedback
from leanflow_cli.workflows.prover.models import Node, digest
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.verification import LeanVerifier
from tests.leanflow.test_prover_live_progress import make_runtime

PROFILE_ROW = json.dumps(
    {
        "name": "LeanFlowProofs.target",
        "type": "Prop",
        "levels": [],
        "dependencies": [],
        "axioms": [],
    }
)


def published_tree(root: Path) -> Path:
    """Lay out one package root with published siblings and a stale target artifact."""
    published = root / ".lake" / "build" / "lib" / "lean"
    (published / "LeanFlowProofs" / "Nested").mkdir(parents=True)
    (published / "LeanFlowProofs" / "Sibling.olean").write_bytes(b"sibling")
    (published / "LeanFlowProofs" / "Nested" / "Deep.olean").write_bytes(b"deep")
    (published / "LeanFlowProofs" / "Target.olean").write_bytes(b"stale")
    (published / "LeanFlowProofs.olean").write_bytes(b"package root")
    return published


def test_shadow_root_is_created_for_a_root_level_module(tmp_path: Path) -> None:
    published = tmp_path / "published" / "Main"
    published.mkdir(parents=True)
    (published / "Child.olean").write_bytes(b"child")
    shadow = tmp_path / "temporary" / "Main"
    type_profile._shadow_package_root(shadow, published)
    assert (shadow / "Child.olean").read_bytes() == b"child"


def test_module_scheme_compiles_under_the_real_module_and_retains_inspected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    published = published_tree(root)
    workspace = tmp_path / "checks"
    workspace.mkdir()
    artifact = tmp_path / "artifacts" / "target.olean"
    commands: list[list[str]] = []

    def command(**kwargs: Any) -> dict[str, Any]:
        argv = list(kwargs["argv"])
        commands.append(argv)
        directory = Path(kwargs["workspace"])
        assert not directory.is_relative_to(
            workspace
        ), "Ephemeral links must not enter resume evidence"
        if "-o" in argv:
            source = Path(argv[-1])
            assert argv[argv.index("-R") + 1] == str(directory)
            assert source.relative_to(directory) == Path("LeanFlowProofs/Target.lean")
            assert source.read_text() == "theorem target : True := by trivial\n"
            Path(argv[argv.index("-o") + 1]).write_bytes(b"fresh")
            return {"success": True, "returncode": 0}
        assert argv[argv.index("--run") + 2 :][:2] == [str(directory), "LeanFlowProofs.Target"]
        shadow = directory / "LeanFlowProofs"
        assert (shadow / "Target.olean").is_symlink()
        assert (shadow / "Target.olean").read_bytes() == b"fresh"
        assert (shadow / "Sibling.olean").read_bytes() == b"sibling"
        assert (shadow / "Nested" / "Deep.olean").read_bytes() == b"deep"
        assert (directory / "LeanFlowProofs.olean").read_bytes() == b"package root"
        # Inspection reads the retained copy, so publication cannot trust other bytes.
        assert (shadow / "Target.olean").resolve() == artifact.resolve()
        return {"success": True, "returncode": 0, "stdout": PROFILE_ROW + "\n"}

    monkeypatch.setattr(check_process, "isolated_command", command)
    result = type_profile.compiled_type_profiles(
        root=root,
        source="theorem target : True := by trivial\n",
        names=["target"],
        workspace=workspace,
        timeout_s=10,
        module="LeanFlowProofs.Target",
        artifact=artifact,
    )
    assert result["accepted"], result
    assert result["artifact"] == str(artifact)
    assert result["artifact_sha256"] == hashlib.sha256(b"fresh").hexdigest()
    assert artifact.read_bytes() == b"fresh"
    assert (published / "LeanFlowProofs" / "Target.olean").read_bytes() == b"stale"
    assert len(commands) == 2
    assert not list(workspace.iterdir()), "temporary compile roots are removed"


def test_legacy_scheme_keeps_the_fixed_profile_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    published_tree(root)
    workspace = tmp_path / "checks"
    workspace.mkdir()
    modules: list[str] = []

    def command(**kwargs: Any) -> dict[str, Any]:
        argv = list(kwargs["argv"])
        directory = Path(kwargs["workspace"])
        if "-o" in argv:
            assert Path(argv[-1]).relative_to(directory) == Path("LeanFlowTypeProfileInput.lean")
            Path(argv[argv.index("-o") + 1]).write_bytes(b"fresh")
            return {"success": True, "returncode": 0}
        modules.append(argv[argv.index("--run") + 3])
        assert not (directory / "LeanFlowProofs").exists()
        return {"success": True, "returncode": 0, "stdout": PROFILE_ROW + "\n"}

    monkeypatch.setattr(check_process, "isolated_command", command)
    result = type_profile.compiled_type_profiles(
        root=root,
        source="theorem target : True := by trivial\n",
        names=["target"],
        workspace=workspace,
        timeout_s=10,
    )
    assert result["accepted"] and "artifact" not in result
    assert modules == ["LeanFlowTypeProfileInput"]


@pytest.mark.parametrize(
    "scheme,skeleton", [("module", False), ("module", True), ("legacy", False)]
)
def test_check_requests_an_artifact_only_for_closed_module_scheme_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str, skeleton: bool
) -> None:
    source = tmp_path / "Candidate.lean"
    source.write_text("theorem goal : True := by trivial\n")
    node = Node(
        id="n",
        name="goal",
        statement="theorem goal : True",
        file="Pkg/Main.lean",
        module="Pkg.Main",
    )
    received: dict[str, Any] = {}

    def profile(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        retained = (
            {"artifact": str(kwargs["artifact"]), "artifact_sha256": "digest"}
            if kwargs.get("artifact") is not None
            else {}
        )
        return {"accepted": True, "profiles": {"goal": {"sha256": "x", "axioms": []}}, **retained}

    monkeypatch.setattr(
        check_process,
        "check_scratch",
        lambda **_: {
            "success": True,
            "ok": not skeleton,
            "has_sorry": skeleton,
            "has_errors": False,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": [],
            "messages": [],
        },
    )
    monkeypatch.setattr(type_profile, "compiled_type_profiles", profile)
    verifier = LeanVerifier(tmp_path, (), signature_scheme=scheme)
    verifier.artifact_root = tmp_path / "artifacts"
    result = verifier.check(node, source, skeleton=skeleton)
    assert result["accepted"], result
    assert result["checked_source_sha256"] == digest("theorem goal : True := by trivial\n")
    if scheme == "module":
        assert received["module"] == "Pkg.Main"
    else:
        assert received["module"] is None
    if scheme == "module" and not skeleton:
        artifact = Path(result["compiled_artifact"])
        assert artifact.parent == verifier.artifact_root
        assert artifact.name.startswith("n_0_")
        assert result["compiled_artifact_sha256"] == "digest"
    else:
        assert received["artifact"] is None
        assert "compiled_artifact" not in result


def test_install_module_publishes_only_matching_private_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    verifier = LeanVerifier(root, (), signature_scheme="module")
    output = root / ".lake" / "build" / "lib" / "lean" / "Pkg" / "Main.olean"
    assert verifier.install_module("Pkg/Main.lean", tmp_path / "x.olean", "d")["error_code"] == (
        "artifact_unavailable"
    )
    verifier.artifact_root = tmp_path / "artifacts"
    verifier.artifact_root.mkdir()
    outside = tmp_path / "outside.olean"
    outside.write_bytes(b"bytes")
    refused = verifier.install_module(
        "Pkg/Main.lean", outside, hashlib.sha256(b"bytes").hexdigest()
    )
    assert refused["error_code"] == "artifact_unavailable" and not output.exists()
    link = verifier.artifact_root / "link.olean"
    link.symlink_to(outside)
    assert verifier.install_module("Pkg/Main.lean", link, "")["accepted"] is False
    artifact = verifier.artifact_root / "n_0_abc.olean"
    artifact.write_bytes(b"bytes")
    mismatch = verifier.install_module("Pkg/Main.lean", artifact, "0" * 64)
    assert mismatch["error_code"] == "artifact_mismatch" and not output.exists()
    assert artifact.exists()
    closed: list[Path] = []
    monkeypatch.setattr(check_process, "close_check_workers", closed.append)
    verifier.workspaces.add(tmp_path / "warm")
    installed = verifier.install_module(
        "Pkg/Main.lean", artifact, hashlib.sha256(b"bytes").hexdigest()
    )
    assert installed["accepted"], installed
    assert output.read_bytes() == b"bytes"
    assert closed == [tmp_path / "warm"] and not verifier.workspaces
    assert not artifact.exists(), "a consumed artifact is not reusable a second time"
    with pytest.raises(ValueError):
        verifier.install_module("../escape.lean", artifact, "d")


class ArtifactVerifier:
    """Return a check result that carries the artifact of the checked source."""

    def __init__(self, checked_source: str, install: dict[str, Any] | None = None) -> None:
        self.checked_source = checked_source
        self.install = install or {"accepted": True, "artifact": "a", "sha256": "s"}
        self.checks = 0
        self.installed: list[tuple[str, Path, str]] = []
        self.compiled: list[str] = []

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        self.checks += 1
        return {
            "accepted": True,
            "axiom_profile_checked": True,
            "checked_source_sha256": digest(self.checked_source),
            "compiled_artifact": "/private/artifacts/n_0.olean",
            "compiled_artifact_sha256": "s",
        }

    def install_module(self, relative: str, artifact: Path, sha256: str) -> dict[str, Any]:
        self.installed.append((relative, artifact, sha256))
        return dict(self.install)

    def compile_module(self, relative: str) -> dict[str, Any]:
        self.compiled.append(relative)
        return {"accepted": True}


def events(runtime: ProverRuntime, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (runtime.store.directory / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == kind
    ]


def test_accepted_submission_installs_its_own_artifact_without_recompiling(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    verifier = ArtifactVerifier("theorem goal : True := by exact True.intro\n")
    runtime.verifier = verifier
    assert candidate_feedback(runtime, job, json.dumps({"proof": "exact True.intro"}))["accepted"]
    assert runtime._accept(node, ["exact True.intro"], job["id"])
    assert node.status == "proved"
    assert verifier.checks == 1 and verifier.compiled == []
    assert verifier.installed == [("Main.lean", Path("/private/artifacts/n_0.olean"), "s")]
    assert (tmp_path / "Main.lean").read_text() == "theorem goal : True := by exact True.intro\n"
    integrated = events(runtime, "proof_integrated")
    assert integrated and integrated[-1]["reused_checked_artifact"] is True


@pytest.mark.parametrize("reason", ["stale_source", "declined"])
def test_publication_recompiles_unless_the_artifact_matches_installed_bytes(
    tmp_path: Path, reason: str
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    verifier = ArtifactVerifier(
        (
            "theorem goal : True := by trivial\n"
            if reason == "stale_source"
            else "theorem goal : True := by exact True.intro\n"
        ),
        install=(
            {"accepted": False, "error_code": "artifact_mismatch"} if reason == "declined" else None
        ),
    )
    runtime.verifier = verifier
    assert runtime._accept(node, ["exact True.intro"], "controller")
    assert node.status == "proved"
    assert verifier.compiled == ["Main.lean"]
    assert len(verifier.installed) == (0 if reason == "stale_source" else 1)
    assert events(runtime, "proof_integrated")[-1]["reused_checked_artifact"] is False
    assert bool(events(runtime, "checked_artifact_declined")) is (reason == "declined")


def test_signature_scheme_is_fixed_per_run_and_legacy_for_older_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Main.lean").write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    options: dict[str, Any] = {
        "root": tmp_path,
        "targets": [tmp_path / "Main.lean"],
        "config": ProverConfig(),
        "session": lambda **_: {},
    }
    runtime = ProverRuntime(**options)
    assert runtime.state["signature_scheme"] == "module"
    assert isinstance(runtime.verifier, LeanVerifier)
    assert runtime.verifier.signature_scheme == "module"
    assert runtime.verifier.artifact_root == runtime.store.directory / "artifacts"
    resumed = ProverRuntime(**options, run_id=runtime.run_id, resume=True)
    assert resumed.verifier.signature_scheme == "module"
    state_path = runtime.store.directory / "state.json"
    state = json.loads(state_path.read_text())
    del state["signature_scheme"]
    state_path.write_text(json.dumps(state))
    legacy = ProverRuntime(**options, run_id=runtime.run_id, resume=True)
    assert legacy.verifier.signature_scheme == "legacy"
    assert legacy.state.get("signature_scheme") is None
    monkeypatch.setenv("LEANFLOW_PROVER_SIGNATURE_SCHEME", "legacy")
    assert ProverRuntime(**options, run_id="legacy-run").verifier.signature_scheme == "legacy"
    monkeypatch.setenv("LEANFLOW_PROVER_SIGNATURE_SCHEME", "other")
    with pytest.raises(ValueError):
        ProverRuntime(**options, run_id="invalid-run")
