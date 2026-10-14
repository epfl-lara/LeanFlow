"""Pin independent kernel type fidelity and fresh-artifact axiom checks."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.source import declaration_source, discover
from leanflow_cli.workflows.prover.verification import LeanVerifier


@pytest.mark.parametrize("first_stage_seconds", [7, 11])
def test_skeleton_check_shares_deadline_with_kernel_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_stage_seconds: int
) -> None:
    from leanflow_cli.workflows.prover import check_process, type_profile, verification

    clock = [0.0]
    monkeypatch.setattr(verification.time, "monotonic", lambda: clock[0])
    inspected: list[float] = []
    source = tmp_path / "candidate.lean"
    source.write_text("theorem goal : True := by trivial\n")
    node = Node(
        id="n", name="goal", statement="theorem goal : True", file="Main.lean", module="Main"
    )

    def check(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["timeout_s"] == 10
        clock[0] += first_stage_seconds
        return {
            "success": True,
            "ok": True,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": [],
        }

    def profile(**kwargs: Any) -> dict[str, Any]:
        inspected.append(kwargs["timeout_s"])
        return {"accepted": True, "profiles": {"goal": {"sha256": "checked", "axioms": []}}}

    monkeypatch.setattr(check_process, "check_scratch", check)
    monkeypatch.setattr(type_profile, "compiled_type_profiles", profile)
    result = LeanVerifier(tmp_path, (), timeout_s=10).check(node, source, skeleton=True)
    if first_stage_seconds < 10:
        assert result["accepted"] and inspected == [3]
    else:
        assert not result["accepted"] and inspected == []
        assert result["kernel_profile"]["error_code"] == "check_timeout"


def test_parallel_queue_wait_does_not_consume_active_check_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process, type_profile

    source = tmp_path / "candidate.lean"
    source.write_text("theorem goal : True := by trivial\n")
    node = Node(
        id="n", name="goal", statement="theorem goal : True", file="Main.lean", module="Main"
    )
    verifier = LeanVerifier(tmp_path, ())
    verifier.configured_timeout_s = 0.1
    verifier.remaining_time = lambda: 2.0
    budgets = []

    def check(**kwargs: Any) -> dict[str, Any]:
        budgets.append(kwargs["timeout_s"])
        return {
            "success": True,
            "ok": True,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": [],
        }

    def profile(**kwargs: Any) -> dict[str, Any]:
        budgets.append(kwargs["timeout_s"])
        return {"accepted": True, "profiles": {"goal": {"sha256": "checked", "axioms": []}}}

    monkeypatch.setattr(check_process, "check_scratch", check)
    monkeypatch.setattr(type_profile, "compiled_type_profiles", profile)
    verifier.lock.acquire()
    timer = threading.Timer(0.2, verifier.lock.release)
    timer.start()
    try:
        result = verifier.check(node, source)
        assert result["accepted"], result
        assert result["verifier_queue_wait_s"] >= 0.15
        assert budgets[0] > 0.05
    finally:
        timer.join()


def test_verifier_queue_is_bounded_and_preserves_global_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted

    verifier = LeanVerifier(tmp_path, ())
    deadline = time.monotonic() + 0.02

    def remaining() -> float:
        if time.monotonic() >= deadline:
            raise BudgetExhausted("campaign time exhausted", code="campaign_wall_time")
        return deadline - time.monotonic()

    verifier.remaining_time = remaining
    verifier.lock.acquire()
    monkeypatch.setattr(
        verifier, "_check_locked", lambda *_args, **_kwargs: pytest.fail("No slot was available")
    )
    node = Node(
        id="n", name="goal", statement="theorem goal : True", file="Main.lean", module="Main"
    )
    try:
        with pytest.raises(BudgetExhausted) as error:
            verifier.check(node, tmp_path / "candidate.lean")
        assert error.value.code == "campaign_wall_time"
    finally:
        verifier.lock.release()


def test_final_gate_creates_a_protected_build_root_before_granting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process

    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by trivial\n")

    def command(**kwargs: Any) -> dict[str, Any]:
        build = tmp_path / ".lake" / "build"
        assert build.is_dir(), "sandbox grants require an existing exact artifact root"
        assert build in kwargs["extra_writable_roots"]
        return {"success": True, "returncode": 0}

    monkeypatch.setattr(check_process, "isolated_command", command)
    assert LeanVerifier(tmp_path, ()).final([source])["accepted"]


def test_final_gate_never_follows_a_build_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process

    root = tmp_path / "project"
    root.mkdir()
    (root / ".lake").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".lake" / "build").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(check_process, "isolated_command", lambda **_: pytest.fail("unsafe grant"))
    with pytest.raises(ValueError):
        LeanVerifier(root, ()).final([])


def verdict() -> dict[str, Any]:
    return {"success": True, "ok": True, "axiom_profile_checked": True, "axiom_profile_axioms": []}


@pytest.mark.parametrize(
    "fingerprint,axioms", [("changed", []), ("original", ["sorryAx"]), ("original", ["invented"])]
)
def test_named_target_success_cannot_override_kernel_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fingerprint: str, axioms: list[str]
) -> None:
    from leanflow_cli.workflows.prover import check_process, type_profile

    source = tmp_path / "candidate.lean"
    source.write_text("theorem goal : True := by trivial\n")
    node = Node(
        id="n",
        name="goal",
        statement="theorem goal : True",
        file="Main.lean",
        module="Main",
        original=True,
    )
    node.signature_sha256 = "original"
    monkeypatch.setattr(check_process, "check_scratch", lambda **_: verdict())
    monkeypatch.setattr(
        type_profile,
        "compiled_type_profiles",
        lambda **_: {
            "accepted": True,
            "profiles": {"goal": {"sha256": fingerprint, "axioms": axioms}},
        },
    )
    assert LeanVerifier(tmp_path, ()).check(node, source)["accepted"] is False


@pytest.fixture
def real_project(tmp_path: Path) -> Path:
    """Make an isolated project while reusing only prebuilt dependency artifacts."""
    configured = os.environ.get("LEANFLOW_TEST_LEAN_PROJECT")
    if not configured:
        pytest.skip("Set LEANFLOW_TEST_LEAN_PROJECT to a prebuilt Lean project for integration")
    base = Path(configured).resolve()
    root = tmp_path / "project"
    root.mkdir()
    for name in ("lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain"):
        if (base / name).is_file():
            shutil.copyfile(base / name, root / name)
    (root / ".lake").mkdir()
    (root / ".lake" / "packages").symlink_to(base / ".lake" / "packages", target_is_directory=True)
    # Match project initialization before entering the read-only verifier.
    # Recent Lake versions create .lake/config even for TOML lakefiles.
    subprocess.run(
        ["lake", "env", "lean", "--version"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return root


@pytest.mark.parametrize("through_definition", [False, True])
def test_real_import_cannot_change_the_claim_elaborated_from_frozen_source(
    real_project: Path, through_definition: bool
) -> None:
    root = real_project
    verifier = LeanVerifier(root, ())
    workspace = root / ".leanflow" / "checks"
    workspace.mkdir(parents=True)
    (root / "Spec.lean").write_text(
        "class Selected where\n  P : Prop\n"
        "instance (priority := 100) originalSelection : Selected := ⟨False⟩\n"
    )
    (root / "Alteration.lean").write_text(
        "import Spec\ninstance (priority := 2000) changedSelection : Selected := ⟨True⟩\n"
    )
    assert verifier.compile_module("Spec.lean")["accepted"]
    assert verifier.compile_module("Alteration.lean")["accepted"]
    target = root / "Main.lean"
    target.write_text(
        "import Spec\n"
        + ("def chosen : Prop := Selected.P\n" if through_definition else "")
        + f"theorem goal : {'chosen' if through_definition else 'Selected.P'} := by sorry\n"
    )
    dag, documents = discover(root, [target], fill_definitions=False)
    try:
        baseline = verifier.capture_signatures(dag, documents, workspace)
        assert baseline["accepted"], baseline
        documents["Main.lean"].imports = ["Alteration"]
        candidate = workspace / "Candidate.lean"
        candidate.write_text(
            declaration_source(documents["Main.lean"], dag.nodes[0], candidate=["trivial"])
        )
        result = verifier.check(dag.nodes[0], candidate)
        # The source compiles, but the trusted profile rejects its changed claim.
        assert result["success"] is True and result["ok"] is False, result
        assert result["accepted"] is False and result["type_matches"] is False, result
        assert (
            verifier.capture_signatures(dag, documents, workspace, initialize=False)["accepted"]
            is False
        )
    finally:
        verifier.close(workspace)


def test_real_multiple_holes_custom_axioms_and_fresh_dependency_gate(real_project: Path) -> None:
    root = real_project
    workspace = root / ".leanflow" / "checks"
    workspace.mkdir(parents=True)
    target = root / "Main.lean"
    candidate = workspace / "Candidate.lean"
    for source, proofs, axioms in [
        (
            "import Std\ntheorem goal : True ∧ True := by\n  constructor\n  · sorry\n  · sorry\n",
            ["trivial", "trivial"],
            (),
        ),
        (
            "import Std\naxiom permitted : True\ntheorem goal : True := by sorry\n",
            ["exact permitted"],
            ("permitted",),
        ),
    ]:
        target.write_text(source)
        dag, documents = discover(root, [target], fill_definitions=False)
        verifier = LeanVerifier(root, axioms)
        try:
            assert verifier.capture_signatures(dag, documents, workspace)["accepted"]
            candidate.write_text(
                declaration_source(documents["Main.lean"], dag.nodes[0], candidate=proofs)
            )
            result = verifier.check(dag.nodes[0], candidate)
            assert result["accepted"], result
        finally:
            verifier.close(workspace)

    dependency = root / "Dependency.lean"
    dependency.write_text("theorem dependency : True := by trivial\n")
    verifier = LeanVerifier(root, ())
    assert verifier.compile_module("Dependency.lean")["accepted"]
    target.write_text("import Dependency\ntheorem goal : True := by sorry\n")
    dag, documents = discover(root, [target], fill_definitions=False)
    try:
        assert verifier.capture_signatures(dag, documents, workspace)["accepted"]
        candidate.write_text(
            declaration_source(documents["Main.lean"], dag.nodes[0], candidate=["exact dependency"])
        )
        assert verifier.check(dag.nodes[0], candidate)["accepted"]
        dependency.write_text("theorem dependency : True := by sorry\n")
        assert verifier.compile_module("Dependency.lean")["accepted"]
        result = verifier.check(dag.nodes[0], candidate)
        assert result["accepted"] is False, result
        # The full compile's independent axiom closure must reject even if a
        # warm REPL still carries the previously compiled import environment.
        if result.get("kernel_profile"):
            assert "sorryAx" in result["kernel_profile"]["axioms"], result
    finally:
        verifier.close(workspace)


def test_real_module_scheme_publishes_the_inspected_artifact(real_project: Path) -> None:
    """Compile once under the real module name, inspect it beside published siblings, publish it."""
    root = real_project
    verifier = LeanVerifier(root, (), signature_scheme="module")
    verifier.artifact_root = root / ".leanflow" / "artifacts"
    workspace = root / ".leanflow" / "checks"
    workspace.mkdir(parents=True)
    (root / "Pkg").mkdir()
    (root / "Pkg" / "Base.lean").write_text("theorem base : True := by trivial\n")
    assert verifier.compile_module("Pkg/Base.lean")["accepted"]
    target = root / "Pkg" / "Main.lean"
    target.write_text("import Pkg.Base\ntheorem goal : True := by sorry\n")
    dag, documents = discover(root, [target], fill_definitions=False)
    try:
        baseline = verifier.capture_signatures(dag, documents, workspace)
        assert baseline["accepted"], baseline
        candidate = workspace / "Candidate.lean"
        candidate.write_text(
            declaration_source(documents["Pkg/Main.lean"], dag.nodes[0], candidate=["exact base"])
        )
        result = verifier.check(dag.nodes[0], candidate)
        assert result["accepted"], result
        artifact = Path(result["compiled_artifact"])
        assert artifact.is_file() and artifact.parent == verifier.artifact_root
        installed = verifier.install_module(
            "Pkg/Main.lean", artifact, result["compiled_artifact_sha256"]
        )
        assert installed["accepted"], installed
        assert (root / ".lake" / "build" / "lib" / "lean" / "Pkg" / "Main.olean").is_file()
        (root / "Consumer.lean").write_text("import Pkg.Main\ntheorem consumer : True := goal\n")
        assert verifier.compile_module("Consumer.lean")["accepted"]
        # A recheck beside the now published artifact keeps the same fingerprint.
        again = verifier.check(dag.nodes[0], candidate)
        assert again["accepted"] and again["type_matches"], again
    finally:
        verifier.close(workspace)


def test_real_root_module_profile_with_published_child(real_project: Path) -> None:
    """Inspect a root module beside a published directory sharing its module prefix."""
    root = real_project
    workspace = root / ".leanflow" / "checks"
    workspace.mkdir(parents=True)
    verifier = LeanVerifier(root, (), signature_scheme="module")
    verifier.artifact_root = root / ".leanflow" / "artifacts"
    (root / "Main").mkdir()
    (root / "Main" / "Child.lean").write_text("theorem child : True := by trivial\n")
    assert verifier.compile_module("Main/Child.lean")["accepted"]
    target = root / "Main.lean"
    target.write_text("import Main.Child\ntheorem goal : True := by sorry\n")
    dag, documents = discover(root, [target], fill_definitions=False)
    try:
        baseline = verifier.capture_signatures(dag, documents, workspace)
        assert baseline["accepted"], baseline
        candidate = workspace / "Candidate.lean"
        candidate.write_text(
            declaration_source(documents["Main.lean"], dag.nodes[0], candidate=["exact child"])
        )
        result = verifier.check(dag.nodes[0], candidate)
        assert result["accepted"], result
        assert not any(path.is_symlink() for path in workspace.rglob("*"))
    finally:
        verifier.close(workspace)


def test_scoped_profile_freezes_fresh_parts_and_never_shadows_stale_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from leanflow_cli.workflows.prover import check_process, type_profile

    library = tmp_path / "derived/lib/lean"
    stem = library / "Main/Goal.olean"
    stem.parent.mkdir(parents=True)
    for suffix in (".olean", ".olean.server", ".olean.private", ".ir", ".ir.sig"):
        stem.with_suffix(suffix).write_bytes(b"stale:" + suffix.encode())
    sibling = library / "Main/Base.olean"
    sibling.write_bytes(b"sibling")
    seen = []

    def command(**kwargs: Any) -> dict[str, Any]:
        argv, work = kwargs["argv"], kwargs["workspace"]
        if "--run" not in argv:
            target = Path(argv[argv.index("-o") + 1])
            target.write_bytes(b"fresh-public")
            target.with_suffix(".olean.server").write_bytes(b"fresh-server")
            assert "-j1" in argv
        else:
            current = work / "Main/Goal.olean"
            assert current.read_bytes() == b"fresh-public"
            assert current.with_suffix(".olean.server").read_bytes() == b"fresh-server"
            assert not current.with_suffix(".olean.private").exists()
            assert not current.with_suffix(".ir").exists()
            assert not current.with_suffix(".ir.sig").exists()
            assert (work / "Main/Base.olean").read_bytes() == b"sibling"
            seen.append(True)
            (work / "profile.jsonl").write_text(
                json.dumps(
                    {"name": "goal", "type": "True", "levels": [], "dependencies": [], "axioms": []}
                )
                + "\n"
            )
        return {"success": True}

    monkeypatch.setattr(check_process, "isolated_command", command)
    result = type_profile.compiled_type_profiles(
        root=tmp_path,
        source="module\npublic theorem goal : True := by trivial\n",
        names=["goal"],
        workspace=tmp_path / "work",
        timeout_s=30,
        module="Main.Goal",
        published_library=library,
        freeze_artifact_parts=True,
    )
    assert result["accepted"] and seen == [True]


def test_final_scoped_build_uses_only_authenticated_custom_build_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanflow_cli.workflows.prover import check_process

    calls = []
    monkeypatch.setattr(
        check_process,
        "isolated_command",
        lambda **kwargs: calls.append(kwargs) or {"success": True, "returncode": 0},
    )
    verifier = LeanVerifier(tmp_path, ())
    verifier.scoped_modules = {"src/Main.lean": "Main"}
    with pytest.raises(ValueError, match="authenticated scoped layout"):
        verifier.final([])
    verifier.scoped_build_dir = "derived-cache"
    assert verifier.final([])["accepted"]
    assert calls[0]["extra_writable_roots"] == (tmp_path / "derived-cache",)
    assert not (tmp_path / ".lake/build").exists()


def test_real_scoped_module_header_inspects_all_fresh_split_parts(real_project: Path) -> None:
    from core.file_copy import copy_tree
    from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles

    root = real_project
    packages = root / ".lake/packages"
    dependency_source = packages.resolve()
    packages.unlink()
    copy_tree(dependency_source, packages)
    old = "module\npublic section\ntheorem goal : False := by sorry\n"
    fresh = "module\npublic section\ntheorem goal : True := by trivial\n"
    (root / "Main.lean").write_text(old)
    built = subprocess.run(
        ["lake", "build", "+Main:olean"], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert built.returncode == 0, built.stderr
    library = root / ".lake/build/lib/lean"
    stem = library / "Main.olean"
    prior = {
        path: path.read_bytes()
        for suffix in (".olean", ".olean.server", ".olean.private", ".ir", ".ir.sig")
        if (path := stem.with_suffix(suffix)).is_file()
    }
    assert len(prior) == 5
    checked = compiled_type_profiles(
        root=root,
        source=fresh,
        names=["goal"],
        workspace=root / ".leanflow/checks",
        timeout_s=60,
        module="Main",
        published_library=library,
        freeze_artifact_parts=True,
    )
    assert checked["accepted"] and checked["profiles"]["goal"]["axioms"] == [], checked
    assert all(path.read_bytes() == content for path, content in prior.items())
    assert (root / "Main.lean").read_text() == old
