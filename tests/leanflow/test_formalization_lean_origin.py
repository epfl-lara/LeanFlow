"""Require exact imported matcher chains without widening authored source or proof authority."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from typing import Any

import pytest

from leanflow_cli.formalization import lean_policy
from leanflow_cli.formalization.lean_origin import imported_origin_owner
from leanflow_cli.formalization.proposal_schema import bind_declarations


def _row(name: str, module: str, **changes: Any) -> dict[str, Any]:
    """Supply complete policy fields while tests vary only provenance relationships."""
    return {
        "name": name,
        "module": module,
        "kind": "definition",
        "type": "Nat → Nat",
        "levels": [],
        "body": "Nat.zero",
        "is_prop": False,
        "unsafe": False,
        "internal": False,
        "generated_from": [],
        "generation_kind": "",
        "generated_parent_profiles": [],
        "source_range": None,
        "line_start": None,
        "type_axioms": [],
        "direct_type_sorry": False,
        "type_depends_on": [],
        "type_untracked_sorry": False,
        "axioms": [],
        "direct_sorry": False,
        "data_sorry": False,
        "pending": False,
        "depends_on": [],
        "untracked_sorry": False,
        "direct_dependencies": [],
        "direct_body_dependencies": [],
        **changes,
    }


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in ("name", "module", "kind", "type", "levels", "body", "source_range")
    }


def _matcher_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mirror the actual child→imported matcher→source definition witness."""
    matcher_name = "Sample.choose.match_1"
    owner = _row(
        "Sample.choose",
        "Main.Base",
        body="matcher n",
        source_range={"line_start": 2, "column_start": 0, "line_end": 2, "column_end": 62},
        direct_dependencies=[matcher_name],
        direct_body_dependencies=[matcher_name],
    )
    matcher = _row(
        matcher_name,
        "Main.Base",
        body="Nat.casesOn n",
        generation_kind="matcher",
        generated_from=[owner["name"]],
        generated_parent_profiles=[_snapshot(owner)],
    )
    child = _row(
        "_private.Main.Goal.0.Sample.choose.match_1.eq_1",
        "Main.Goal",
        kind="theorem",
        is_prop=True,
        type="matcher 0 = 1",
        body="Eq.refl (matcher 0)",
        generation_kind="matcher_equation",
        generated_from=[matcher_name],
        direct_dependencies=[matcher_name],
        generated_parent_profiles=[_snapshot(matcher)],
    )
    splitter = {
        **child,
        "name": child["name"].rsplit(".", 1)[0] + ".splitter",
        "kind": "definition",
        "is_prop": False,
        "body": "fun n => matcher n",
    }
    goal = _row(
        "Sample.goal",
        "Main.Goal",
        kind="theorem",
        is_prop=True,
        source_range=owner["source_range"],
        direct_dependencies=[owner["name"]],
    )
    return [goal, child, splitter], [owner, matcher]


def _bound_matcher() -> tuple[dict[str, Any], dict[str, Any]]:
    rows, parents = _matcher_rows()
    checks = lean_policy.evaluate_policy(rows, (), origin_parents=parents)
    for row in checks["declarations"]:
        row.update(file="src/Main/Goal.lean", declaration_origin="generated")
    for row in checks["origin_parent_profiles"].values():
        row.update(
            file="src/Main/Base.lean", source_sha256="a" * 64, declaration_origin="existing_project"
        )
    checks["admitted_project_sources"] = {"src/Main/Base.lean": "a" * 64}
    record = {
        "units": [
            {
                "id": "goal",
                "required_source_ids": ["source-goal"],
                "declarations": [{"name": "Sample.goal", "file": "src/Main/Goal.lean"}],
            }
        ]
    }
    return record, checks


def test_imported_matcher_chain_stays_separate_from_authored_profiles_and_obligations():
    record, checks = _bound_matcher()
    bind_declarations(record, checks)
    assert set(checks["origin_parent_profiles"]) == {"Sample.choose", "Sample.choose.match_1"}
    assert not set(checks["origin_parent_profiles"]) & set(checks["profiles"])
    assert checks["obligations"] == []
    assert len(record["units"][0]["declarations"]) == 3
    assert all(
        row.get("compiler_owner_names") == ["Sample.goal"]
        for row in record["units"][0]["declarations"][1:]
    )
    assert record["units"][0]["required_source_ids"] == ["source-goal"]


@pytest.mark.parametrize(
    "fault",
    [
        "matcher_body",
        "matcher_type",
        "matcher_levels",
        "matcher_module",
        "matcher_origin",
        "matcher_user_range",
        "matcher_ordinary",
        "owner_body",
        "owner_type",
        "owner_range",
        "owner_module",
        "owner_body_edge",
        "child_user_range",
        "child_parent_snapshot",
        "child_untracked",
        "child_unsafe",
        "child_data_hole",
        "unknown_helper",
        "missing_matcher",
    ],
)
def test_matcher_origin_rejects_copied_changed_unowned_or_unsafe_chain(fault):
    rows, parents = _matcher_rows()
    owner, matcher = parents
    child = rows[1]
    if fault.startswith("matcher_") and fault.split("_", 1)[1] in {
        "body",
        "type",
        "levels",
        "module",
    }:
        field = fault.split("_", 1)[1]
        matcher[field] = ["u"] if field == "levels" else "changed"
    elif fault == "matcher_origin":
        matcher["generated_from"] = ["unknown"]
    elif fault == "matcher_user_range":
        matcher["source_range"] = owner["source_range"]
    elif fault == "matcher_ordinary":
        matcher.update(generated_from=[], generation_kind="", source_range=owner["source_range"])
    elif fault.startswith("owner_") and fault.split("_", 1)[1] in {
        "body",
        "type",
        "range",
        "module",
    }:
        owner[{"range": "source_range"}.get(fault[6:], fault[6:])] = "changed"
    elif fault == "owner_body_edge":
        owner["direct_body_dependencies"] = []
    elif fault == "child_user_range":
        child["source_range"] = owner["source_range"]
    elif fault == "child_parent_snapshot":
        child["generated_parent_profiles"][0]["body"] = "changed"
    elif fault == "child_untracked":
        child["untracked_sorry"] = True
    elif fault == "child_unsafe":
        child["unsafe"] = True
    elif fault == "child_data_hole":
        rows[2]["data_sorry"] = True
    elif fault == "unknown_helper":
        child["generation_kind"] = "unknown_helper"
    else:
        parents = [owner]
    with pytest.raises(ValueError):
        lean_policy.evaluate_policy(rows, (), origin_parents=parents)


@pytest.mark.parametrize(
    "fault", ["matcher_source", "matcher_file", "owner_source", "consumer", "module", "policy"]
)
def test_matcher_binding_requires_current_source_identity_and_exact_mapped_consumer(fault):
    record, checks = _bound_matcher()
    matcher, owner = (
        checks["origin_parent_profiles"][name]
        for name in ("Sample.choose.match_1", "Sample.choose")
    )
    if fault == "matcher_source":
        matcher["source_sha256"] = "b" * 64
    elif fault == "matcher_file":
        matcher["file"] = "src/Main/Other.lean"
    elif fault == "owner_source":
        owner["source_sha256"] = "b" * 64
    elif fault == "consumer":
        checks["profiles"]["Sample.goal"]["direct_dependencies"] = []
    elif fault == "module":
        checks["profiles"]["Sample.goal"]["module"] = "Main.Other"
    else:
        checks["policy_id"] = "old-policy"
    with pytest.raises(ValueError, match="owner|consumer"):
        bind_declarations(record, checks)


@pytest.mark.parametrize(
    "dependencies", [None, "Sample.choose.match_1", {"Sample.choose.match_1": True}, [1]]
)
def test_matcher_owner_requires_exact_body_dependency_list_shape(dependencies):
    rows, parents = _matcher_rows()
    parents[0]["direct_body_dependencies"] = dependencies
    with pytest.raises(ValueError):
        lean_policy.evaluate_policy(rows, (), origin_parents=parents)


@pytest.mark.parametrize("level", ["child", "matcher"])
@pytest.mark.parametrize("origin", [None, "x", {"x": True}, [1], ["x", 1]])
def test_public_imported_origin_refuses_malformed_parent_chain(level, origin):
    rows, parents = _matcher_rows()
    target = rows[1] if level == "child" else parents[1]
    target["generated_from"] = origin
    assert imported_origin_owner(rows[1], {row["name"]: row for row in parents}) is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "definition,statement",
    [
        (
            "def choose (n : Nat) : Nat := match n with | 0 => 1 | k+1 => k",
            "theorem goal : choose 0 = 1",
        ),
        (
            "def choose (n : Option Nat) : Nat := match n with | none => 0 | some k => k",
            "theorem goal (n : Nat) : choose (some n) = n",
        ),
    ],
)
def test_real_imported_matcher_simp_goal_only_binding_and_frozen_final_check(
    tmp_path, monkeypatch, definition, statement
):
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for real kernel provenance")
    from leanflow_cli.formalization.environment import environment_identity
    from leanflow_cli.formalization.evidence import digest, durable_json
    from leanflow_cli.formalization.handoff import build_handoff, proof_preservation_certificate
    from leanflow_cli.formalization.preparation_resources import supervise_preparation
    from leanflow_cli.formalization.proof_snapshot import inspect_frozen_proof
    from leanflow_cli.formalization.records import document_directory, save_receipt, save_revision
    from leanflow_cli.native.harness_artifacts import source_files
    from leanflow_cli.workflows.project_setup.execution import build_setup_execution_policy
    from leanflow_cli.workflows.prover.source import SourceDocument

    root = tmp_path / "project"
    (root / "src/Main").mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    (root / "lean-toolchain").write_text(toolchain)
    (root / "lakefile.toml").write_text(
        'name = "matcher"\ndefaultTargets = ["Actual"]\n[[lean_lib]]\nname = "Actual"\nsrcDir = "src"\nroots = ["Main"]\n'
    )
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "packages": [],
                "name": "matcher",
                "lakeDir": ".lake",
            }
        )
    )
    base, goal = root / "src/Main/Base.lean", root / "src/Main/Goal.lean"
    base.write_text("namespace Sample\n" + definition + "\nend Sample\n")
    goal.write_text(
        "import Main.Base\nnamespace Sample\n" + statement + " := by simp [choose]\nend Sample\n"
    )
    before = source_files(root)
    axioms = ("propext", "Classical.choice", "Quot.sound")
    stage = tmp_path / "supervision"
    stage.mkdir()
    policy = build_setup_execution_policy(
        stage,
        canonical_root=root,
        toolchain=toolchain,
        network_allowed=False,
        deadline=time.monotonic() + 180,
        max_owned_rss_bytes=16 * 1024**3,
    )
    # Repeated real compiler inventories exceed the generic unit-test alarm.
    # Keep this optional fixture bounded and supervised without changing policy.
    signal.alarm(180)
    with supervise_preparation(policy) as cancelled:
        result = lean_policy.inspect_draft(
            root,
            ["src/Main/Goal.lean"],
            root / ".leanflow/checks",
            axioms,
            deadline=policy.deadline,
            cancelled=cancelled.is_set,
        )
        assert result["accepted"], result
        units = [
            {
                "id": "goal",
                "required_source_ids": ["source-goal"],
                "declarations": [{"name": "Sample.goal", "file": "src/Main/Goal.lean"}],
            }
        ]
        record = {"units": units}
        bind_declarations(record, result)
        assert len(units[0]["declarations"]) == 6
        assert set(result["origin_parent_profiles"]) == {"Sample.choose", "Sample.choose.match_1"}
        assert result["obligations"] == []
        for child in result["profiles"].values():
            if child["generation_kind"] == "matcher_equation":
                owner = imported_origin_owner(child, result["origin_parent_profiles"])
                assert owner and owner["name"] == "Sample.choose"
                assert owner["source_sha256"] == hashlib.sha256(base.read_bytes()).hexdigest()
        frozen = inspect_frozen_proof(
            root,
            ["src/Main/Goal.lean"],
            {file: digest(data) for file, data in before.items() if file.endswith(".lean")},
            environment_identity(root),
            axioms,
            inspect=lean_policy.inspect_draft,
            environment=environment_identity,
            deadline=policy.deadline,
            cancelled=cancelled.is_set,
        )
        assert frozen["accepted"] and frozen["obligations"] == [], frozen
        assert (
            frozen["profiles"]["Sample.goal"]["type_hash"]
            == result["profiles"]["Sample.goal"]["type_hash"]
        )
        assert source_files(root) == before

        # Seed only the test's accepted-review boundary. Compilation, exact hole
        # authorization, frozen final checking and immutable certificate are real;
        # this fixture grants no native source-review or publication evidence.
        pending = "import Main.Base\nnamespace Sample\n" + statement + " := by sorry\nend Sample\n"
        goal.write_text(pending)
        baseline = lean_policy.inspect_draft(
            root,
            ["src/Main/Goal.lean"],
            root / ".leanflow/baseline",
            axioms,
            deadline=policy.deadline,
            cancelled=cancelled.is_set,
        )
        assert baseline["accepted"] and len(baseline["obligations"]) == 1, baseline
        environment = environment_identity(root)
        files = {
            file: digest(data)
            for file, data in source_files(root).items()
            if file.endswith(".lean")
        }
        directory = document_directory(root, "matcher-fixture", published=True)
        accepted = save_revision(
            directory,
            {
                "schema_version": 1,
                "document_id": "matcher-fixture",
                "checks": baseline,
                "project_source_hashes": files,
                "environment_id": environment,
                "source_files": {},
                "allowed_axioms": list(axioms),
                "lean_files": {"src/Main/Goal.lean": files["src/Main/Goal.lean"]},
                "inventory": {"items": [], "regions": [], "diagnostics": []},
                "units": [
                    {
                        "id": "goal",
                        "required_source_ids": [],
                        "declarations": [{"name": "Sample.goal", "file": "src/Main/Goal.lean"}],
                    }
                ],
            },
        )
        handoff = build_handoff(
            root,
            document_id="matcher-fixture",
            revision_id=accepted["revision_id"],
            registry=baseline,
            files=files,
            environment_id=environment,
            allowed_axioms=axioms,
        )
        durable_json(directory / "handoff.json", handoff)
        save_receipt(
            directory,
            {
                "status": "published",
                "revision_id": accepted["revision_id"],
                "handoff": handoff,
                "reviews": [],
            },
        )
        handoff_before = (directory / "handoff.json").read_bytes()
        document = SourceDocument(
            "src/Main/Goal.lean",
            pending,
            replacements={str(handoff["obligations"][0]["holes"][0]): "simp [choose]"},
        )
        goal.write_text(document.render())
        certificate = proof_preservation_certificate(
            root,
            handoff,
            {document.path: document},
            root / ".leanflow/certificate",
            axioms,
            deadline=policy.deadline,
            cancelled=cancelled.is_set,
        )
        assert certificate["checks"]["accepted"] and certificate["checks"]["obligations"] == []
        assert len(certificate["checks"]["profiles"]) == 6
        assert certificate["before"] != certificate["after"]
        assert (
            certificate["checks"]["profiles"]["Sample.goal"]["type_hash"]
            == baseline["profiles"]["Sample.goal"]["type_hash"]
        )
        assert (directory / "handoff.json").read_bytes() == handoff_before
        assert base.read_bytes() == before["src/Main/Base.lean"]
    assert policy.usage.cleanup_verified
    assert 0 < policy.usage.peak_owned_rss_bytes <= 16 * 1024**3
