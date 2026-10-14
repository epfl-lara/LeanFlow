"""Share compiled named-proof policy between drafting and source-backed formalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.native import harness_artifacts as artifacts
from leanflow_cli.native import harness_draft


@pytest.fixture
def proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "Main.lean").write_text("theorem original : True := by sorry\n")
    baseline = artifacts.source_files(root)
    snapshot = artifacts.create_snapshot(root, tmp_path / "agent")
    (snapshot / "Main/Paper").mkdir(parents=True)
    (snapshot / "Main/Paper/Definitions.lean").write_text(
        "theorem witness : ∃ n : Nat, n = 3 := by sorry\n"
        "noncomputable def selected : Nat := Classical.choose witness\n"
        "opaque complete : Nat := 7\n"
    )
    (snapshot / "Main/Paper.lean").write_text("import Main.Paper.Definitions\n")
    monkeypatch.setattr(artifacts, "lean_environment_generation", lambda root: "environment")
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.check_process.isolated_command",
        lambda **kwargs: {"success": True},
    )
    return dict(
        root=root,
        snapshot=snapshot,
        baseline=baseline,
        directory=tmp_path / "checks",
        workflow="draft",
        active_file="",
        allowed_axioms=("Classical.choice",),
        deadline=None,
    )


def test_draft_acceptance_uses_full_registry_and_allows_aggregator(proposal, monkeypatch):
    observed = []

    def inspect(root, modules, workspace, allowed_axioms, **kwargs):
        assert kwargs["require_closed_inputs"] is False
        observed.extend(modules)
        return {
            "accepted": True,
            "layout": {"build_dir": ".lake/build"},
            "modules": {path.removesuffix(".lean").replace("/", "."): path for path in modules},
            "declarations": [
                {"name": "Data.mk", "file": "Main/Paper/Definitions.lean", "kind": "constructor"},
                {"name": "witness", "file": "Main/Paper/Definitions.lean", "pending": True},
            ],
            "obligations": [{"name": "witness"}],
        }

    monkeypatch.setattr(harness_draft, "inspect_draft", inspect)
    result = artifacts.verify_proposal(**proposal)
    assert result["accepted"], result
    assert set(observed) == {"Main/Paper.lean", "Main/Paper/Definitions.lean"}
    assert result["profiles"]["Main/Paper.lean"] == {}
    assert "Data.mk" in result["profiles"]["Main/Paper/Definitions.lean"]
    assert result["compiled_draft"]["obligations"] == [{"name": "witness"}]


def test_draft_rejects_compiled_construction_holes(proposal, monkeypatch):
    monkeypatch.setattr(
        harness_draft,
        "inspect_draft",
        lambda *args, **kwargs: {"accepted": False, "error": "Computational data contains a hole"},
    )
    result = artifacts.verify_proposal(**proposal)
    assert not result["accepted"] and "Computational data" in result["error"]


@pytest.mark.parametrize("changed", [False, True])
def test_draft_retains_original_kernel_type_protection(proposal, monkeypatch, changed):
    (proposal["snapshot"] / "Main.lean").write_text("theorem original : True := by trivial\n")
    monkeypatch.setattr(
        harness_draft,
        "inspect_draft",
        lambda root, modules, *args, **kwargs: {
            "accepted": True,
            "layout": {"build_dir": ".lake/build"},
            "declarations": [],
            "modules": {path.removesuffix(".lean").replace("/", "."): path for path in modules},
        },
    )
    calls = []

    def profile(**kwargs):
        calls.append(kwargs)
        return {
            "accepted": True,
            "profiles": {"original": {"sha256": str(changed and len(calls) == 2)}},
        }

    monkeypatch.setattr(harness_draft, "compiled_type_profiles", profile)
    result = artifacts.verify_proposal(**proposal)
    assert result["accepted"] is not changed, result
    assert len(calls) == 2 and all(call["module"] == "Main" for call in calls)


def test_draft_still_protects_original_definition_bytes(proposal):
    original = "def data : Nat := 1\ntheorem original : True := by sorry\n"
    (proposal["root"] / "Main.lean").write_text(original)
    proposal["baseline"] = artifacts.source_files(proposal["root"])
    (proposal["snapshot"] / "Main.lean").write_text(original.replace(":= 1", ":= 2"))
    result = artifacts.verify_proposal(**proposal)
    assert not result["accepted"] and "non-proof definition" in result["error"]
