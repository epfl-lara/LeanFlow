"""Require fresh verification whenever cached submission inputs or trust change."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import lean_environment
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import candidate_feedback
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.submission_cache import import_closure, imported_modules
from tests.leanflow.test_prover_live_progress import make_runtime

SOURCES = {
    "Main.lean": "import Helper\n\ntheorem goal : True := by sorry\n",
    "Helper.lean": "import Base\n\ntheorem helper : True := by trivial\n",
    "Base.lean": "theorem base : True := by trivial\n",
    "Other.lean": "theorem other : True := by trivial\n",
}


@pytest.fixture(autouse=True)
def local_test_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fake verifier tests independent of the host's installed Lean versions."""
    monkeypatch.setattr(lean_environment, "_toolchain_roots", lambda root: [])


def project_runtime(root: Path, sources: dict[str, str] | None = None) -> ProverRuntime:
    """Snapshot a goal with an import chain and one unrelated module."""
    for name, text in (sources or SOURCES).items():
        (root / name).write_text(text)
    (root / "lakefile.toml").write_text('name = "Demo"\n')
    runtime = ProverRuntime(
        root=root,
        targets=[root / "Main.lean"],
        config=ProverConfig(total_api_calls=100, job_api_calls=20),
        session=lambda **_: {},
        verifier=object(),
    )
    helper = Node(
        id="helper",
        name="helper",
        statement="theorem helper : True",
        file="Helper.lean",
        module="Helper",
        status="proved",
    )
    runtime.dag.nodes.append(helper)
    runtime.dag.nodes[0].dependencies = ["helper"]
    return runtime


def rewrite(runtime: ProverRuntime, name: str, text: str) -> None:
    """Change one managed source the way a controller publication would."""
    runtime.documents[name].baseline = text
    (runtime.root / name).write_text(text)


@pytest.mark.parametrize(
    "changed",
    [
        "none",
        "candidate",
        "unrelated",
        "unrelated_during_check",
        "imported",
        "transitive",
        "imported_during_check",
        "dependency_trust",
        "manifest",
    ],
)
def test_reuse_requires_same_independently_checked_inputs(tmp_path: Path, changed: str) -> None:
    runtime = project_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    checks: list[str] = []
    edits = {
        "unrelated": ("Other.lean", "theorem other : True := by exact True.intro\n"),
        "imported": ("Helper.lean", "import Base\n\ntheorem helper : True := by exact base\n"),
        "transitive": ("Base.lean", "theorem base : True := by exact True.intro\n"),
    }
    edits["unrelated_during_check"] = edits["unrelated"]
    edits["imported_during_check"] = edits["imported"]

    class Verifier:
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            checks.append(file.read_text())
            if changed.endswith("_during_check") and len(checks) == 1:
                rewrite(runtime, *edits[changed])
            return {"accepted": True, "axiom_profile_checked": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": True}

    runtime.verifier = Verifier()
    report = candidate_feedback(runtime, job, json.dumps({"proof": "exact True.intro"}))
    if changed == "imported_during_check":
        assert report["accepted"] is False
        assert "changed during verification" in report["error"]
    else:
        assert report["accepted"] and not report["conditional"]
    if changed in {"unrelated", "imported", "transitive"}:
        rewrite(runtime, *edits[changed])
    if changed == "dependency_trust":
        runtime.dag.by_id()["helper"].status = "candidate"
    if changed == "manifest":
        (tmp_path / "lake-manifest.json").write_text('{"packages": []}')
    candidate = "trivial" if changed == "candidate" else "exact True.intro"
    assert runtime._accept(node, [candidate], job["id"])
    reused = changed in {"none", "unrelated", "unrelated_during_check"}
    assert len(checks) == (1 if reused else 2), checks
    assert node.status == "proved"


def test_conditional_submission_is_rechecked_when_dependencies_close(tmp_path: Path) -> None:
    """A top-down candidate checked against sorry helpers never counts as closed."""
    runtime = project_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    runtime.dag.by_id()["helper"].status = "pending"
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    skeletons: list[bool] = []

    class Verifier:
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            skeletons.append(bool(kwargs.get("skeleton")))
            return {"accepted": True, "axiom_profile_checked": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": True}

    runtime.verifier = Verifier()
    report = candidate_feedback(runtime, job, json.dumps({"proof": "exact True.intro"}))
    assert report["accepted"] and report["conditional"]
    runtime.dag.by_id()["helper"].status = "proved"
    assert runtime._accept(node, ["exact True.intro"], job["id"])
    assert skeletons == [True, False]


def test_unparseable_import_header_fails_closed_to_every_source(tmp_path: Path) -> None:
    sources = {**SOURCES, "Helper.lean": "/- unterminated\ntheorem helper : True := by trivial\n"}
    runtime = project_runtime(tmp_path, sources)
    closure = import_closure(runtime, "Main.lean")
    assert set(closure) == {"Helper.lean", "Base.lean", "Other.lean"}
    runtime = project_runtime(tmp_path)
    assert set(import_closure(runtime, "Main.lean")) == {"Helper.lean", "Base.lean"}


def test_imported_modules_reads_only_real_import_commands() -> None:
    source = (
        "import Mathlib.Tactic\n"
        "-- import Commented\n"
        "/- import Blocked -/\n"
        "import «Odd Name».Sub\n"
        "\n"
        'theorem t : "import Quoted" = "import Quoted" := rfl\n'
    )
    assert imported_modules(source) == ["Mathlib.Tactic", "Odd Name.Sub"]
    with pytest.raises(ValueError):
        imported_modules("import «Broken\n")


def test_failed_or_conditional_results_never_populate_cache(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    cache = runtime.submission_cache
    cache.remember(node, "same", "same", {"accepted": False}, conditional=False)
    assert cache.take(node, "same") is None
    cache.remember(node, "same", "same", {"accepted": True}, conditional=True)
    assert cache.take(node, "same") is None
    cache.remember(node, "before", "after", {"accepted": True}, conditional=False)
    assert cache.take(node, "after") is None


@pytest.mark.parametrize(
    "header",
    [
        "import /- explanation -/ Helper\n",
        "import\n Helper\n",
        "module\npublic import Helper\n",
        "module\nmeta import Helper\n",
        "module\nimport all Helper\n",
    ],
)
def test_unhandled_lean_import_syntax_never_omits_managed_dependencies(
    tmp_path: Path, header: str
) -> None:
    sources = {**SOURCES, "Main.lean": header + "theorem goal : True := by sorry\n"}
    runtime = project_runtime(tmp_path, sources)
    assert {"Helper.lean", "Base.lean"} <= set(import_closure(runtime, "Main.lean"))


@pytest.mark.parametrize("location", ["local", "package", "unmanaged", "lean_path"])
@pytest.mark.parametrize("during_check", [False, True])
def test_dependency_changes_invalidate_acceptance_even_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, during_check: bool
) -> None:
    """A dependency rebuild must invalidate proof reuse without managed source edits."""
    root = tmp_path / "project"
    root.mkdir()
    runtime = project_runtime(root)
    inputs = {
        "local": root / ".lake/build/lib/lean/Base.olean",
        "package": root / ".lake/packages/library/.lake/build/lib/lean/Library.olean",
        "unmanaged": root / "Unmanaged.lean",
        "lean_path": tmp_path / "external/Library.olean",
    }
    dependency = inputs[location]
    dependency.parent.mkdir(parents=True, exist_ok=True)
    dependency.write_bytes(b"before")
    if location == "lean_path":
        monkeypatch.setenv("LEAN_PATH", str(dependency.parent))
    checks: list[Path] = []

    def mutate() -> None:
        original = dependency.stat()
        dependency.write_bytes(b"after!")
        os.utime(dependency, ns=(original.st_atime_ns, original.st_mtime_ns))

    class Verifier:
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            checks.append(file)
            if during_check and len(checks) == 1:
                mutate()
            return {"accepted": True, "axiom_profile_checked": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": True}

    runtime.verifier = Verifier()
    node = runtime.dag.nodes[0]
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    report = candidate_feedback(runtime, job, json.dumps({"proof": "exact True.intro"}))
    assert report["accepted"] is not during_check
    if not during_check:
        mutate()
    assert runtime._accept(node, ["exact True.intro"], job["id"])
    assert len(checks) == 2


def test_direct_acceptance_rejects_dependencies_changed_during_kernel_check(tmp_path: Path) -> None:
    runtime = project_runtime(tmp_path)
    dependency = tmp_path / ".lake/build/lib/lean/Base.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"before")
    before = (tmp_path / "Main.lean").read_bytes()

    class Verifier:
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            dependency.write_bytes(b"changed")
            return {"accepted": True}

    runtime.verifier = Verifier()
    assert not runtime._accept(runtime.dag.nodes[0], ["exact True.intro"], "job")
    assert (tmp_path / "Main.lean").read_bytes() == before


def test_model_changes_do_not_change_deterministic_kernel_acceptance_keys(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.submission_cache import submission_key

    runtime = project_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    before = submission_key(runtime, node, "exact source")
    runtime.config = ProverConfig(provider="claude", model="another-model", reasoning_effort="high")
    assert submission_key(runtime, node, "exact source") == before
    runtime.config = replace(runtime.config, allowed_axioms=())
    assert submission_key(runtime, node, "exact source") != before


def test_resume_does_not_restore_cached_acceptance_from_saved_receipts(tmp_path: Path) -> None:
    """Retained candidate text survives resume, while controller memory trust does not."""
    runtime = project_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    runtime.submission_cache.remember(node, "same", "same", {"accepted": True}, conditional=False)
    runtime.state["saved_submission_receipt"] = {"accepted": True, "input_hash": "same"}
    runtime._persist()
    reopened = ProverRuntime(
        root=tmp_path,
        targets=[tmp_path / "Main.lean"],
        config=runtime.config,
        run_id=runtime.run_id,
        resume=True,
        verifier=object(),
        session=lambda **_: {},
    )
    assert reopened.state["saved_submission_receipt"]["accepted"]
    assert reopened.submission_cache.take(node, "same") is None
