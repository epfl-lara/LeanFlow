"""Bind best-effort completion to bounded saved inputs and actual compiled-name evidence."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from leanflow_cli.formalization import dependency_snapshot
from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.draft_delivery import project_manifest
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.records import create_document
from leanflow_cli.formalization.source_draft_artifacts import MAX_DRAFT_FILE_BYTES
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native import harness_best_effort as best


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Use real source/census/snapshot files with explicitly simulated compiler boundary results."""
    root = tmp_path / "canonical"
    root.mkdir()
    source = root / "paper.tex"
    source.write_text(
        r"\begin{definition}A natural-number object.\end{definition}"
        "\n"
        r"\begin{theorem}Zero equals zero.\end{theorem}"
    )
    (root / "Main.lean").write_text("import Init\ndef existing : Nat := 1\n")
    (root / "lakefile.toml").write_text('name = "paper"\n[[lean_lib]]\nname = "Main"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
    (root / "lake-manifest.json").write_text('{"packages": []}\n')
    record = create_document(root, source)
    item = record["inventory"]["items"][0]
    apply_action(
        record,
        {
            "type": "record_obligation",
            "item_id": item["id"],
            "source_refs": item["statement_spans"],
            "description": "Retain the exact natural-number domain of the source object.",
            "reason": "Keep ambiguity visible without requiring human clarification before drafting.",
        },
    )
    record["environment_id"] = "simulated-compiler-boundary"
    baseline = {name: (root / name).read_bytes() for name in project_manifest(root)}
    snapshot = root / ".leanflow/native-workflows/test-run/project"
    snapshot.mkdir(parents=True)
    for name in [*baseline, "paper.tex"]:
        shutil.copyfile(root / name, snapshot / name)
    file = "Main/Document/Statements.lean"
    (snapshot / file).parent.mkdir(parents=True)
    (snapshot / file).write_text(
        "def Paper.object : Nat := 0\ntheorem Paper.result : 0 = 0 := by sorry\n"
    )
    (snapshot / "Main/Document.lean").write_text("import Main.Document.Statements\n")
    layout = {
        "accepted": True,
        "library": "Main",
        "src_dir": ".",
        "libraries": [{"src_dir": ".", "roots": ["Main"]}],
        "owned_prefix": "Main/Document",
        "module_prefix": "Main.Document",
        "aggregator": "Main/Document.lean",
        "root_file": "Main.lean",
        "build_dir": ".lake/build",
        "lean_lib_dir": ".lake/build/lib/lean",
    }
    proposal = {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "phase": "complete",
        "units": [
            {
                "id": "paper",
                "required_source_ids": [row["id"] for row in record["inventory"]["items"]],
                "declarations": [
                    {"file": file, "name": "Paper.object"},
                    {"file": file, "name": "Paper.result"},
                ],
                "clause_dispositions": [
                    {
                        "obligation_id": next(iter(obligation_registry(record))),
                        "disposition": "unresolved",
                        "declaration_names": ["Paper.object"],
                        "rationale": "The provisional reading remains an optional source clarification.",
                    }
                ],
            }
        ],
        "coverage": [
            {
                "region_id": row["id"],
                "disposition": "unresolved",
                "item_ids": row.get("item_ids", []),
                "reason": "Retained in full for advisory review.",
            }
            for row in record["inventory"]["regions"]
        ],
    }
    (snapshot / ".leanflow-job").mkdir()
    artifact = {"Main.Document.Statements": {"sha256": {"Statements.olean": "fixture-artifact"}}}
    declarations = [
        {"name": "Paper.object", "file": file, "kind": "definition", "type": "Nat"},
        {"name": "Paper.result", "file": file, "kind": "theorem", "type": "0 = 0", "pending": True},
    ]
    checks = {
        "accepted": True,
        "declarations": declarations,
        "profiles": {row["name"]: row for row in declarations},
        "local_artifacts": artifact,
        "library_declarations": [],
    }
    calls: list[str] = []
    on_inspect = []

    def inspect(*args, **kwargs):
        calls.append("inspect")
        for action in on_inspect:
            action()
        return copy.deepcopy(checks)

    def build(*args, **kwargs):
        calls.append("build")
        return {"success": True, "local_artifacts": copy.deepcopy(artifact)}

    monkeypatch.setattr(best, "environment_identity", lambda _: record["environment_id"])
    monkeypatch.setattr(dependency_snapshot, "inspect_layout", lambda *a, **kw: layout)
    monkeypatch.setattr(best, "inspect_draft", inspect)
    monkeypatch.setattr(best, "build_project", build)
    monkeypatch.setattr(
        best, "inspect_dependency", lambda *a, **kw: pytest.fail("Unexpected library replay")
    )
    value = {
        "root": root,
        "snapshot": snapshot,
        "record": record,
        "baseline": baseline,
        "layout": layout,
        "proposal": proposal,
        "checks": checks,
        "calls": calls,
        "on_inspect": on_inspect,
        "directory": tmp_path / "checks",
        "file": file,
    }
    _write_proposal(value)
    return value


def _write_proposal(project: dict) -> None:
    """Write only the simulated author's private proposal."""
    (project["snapshot"] / ".leanflow-job/formalization-proposal.json").write_text(
        json.dumps(project["proposal"])
    )


def _check(project: dict) -> tuple[dict, dict, dict | None]:
    """Run real snapshot protection, bounded census and mapping checks around mocked compiler calls."""
    return best.check_project(
        project["root"],
        project["snapshot"],
        project["record"],
        project["baseline"],
        project["layout"],
        project["directory"],
        ("propext", "Classical.choice", "Quot.sound"),
        deadline=None,
        cancelled=lambda: False,
    )


def test_valid_compiled_names_and_unresolved_clauses_finish_without_changing_originals(project):
    source_before = (project["root"] / "paper.tex").read_bytes()
    report, subject, proposal = _check(project)
    assert report["complete"] and report["lean_accepted"] and report["project_build_passed"]
    assert (
        not report["accepted"]
        and not report["publication_authority"]
        and not report["proof_authority"]
    )
    assert report["census"]["counts"]["unresolved_known_clauses"] == 1
    assert not report["missing_compiled_mappings"] and proposal == project["proposal"]
    assert project["calls"] == ["inspect", "build"]
    assert (project["root"] / "Main.lean").read_bytes() == project["baseline"]["Main.lean"]
    assert (project["snapshot"] / "Main.lean").read_bytes() == b"import Main.Document\n" + project[
        "baseline"
    ]["Main.lean"]
    assert (project["root"] / "paper.tex").read_bytes() == source_before
    assert (project["snapshot"] / "paper.tex").read_bytes() == source_before
    assert subject["lean_files"]["Main.lean"] == digest(
        (project["snapshot"] / "Main.lean").read_bytes()
    )


@pytest.mark.parametrize("mutation", ["nonexistent", "wrong_file"])
def test_plausible_file_mapping_does_not_replace_actual_compiled_names(project, mutation):
    ref = project["proposal"]["units"][0]["declarations"][0]
    if mutation == "nonexistent":
        ref["name"] = "Paper.invented"
        project["proposal"]["units"][0]["clause_dispositions"][0]["declaration_names"] = [
            ref["name"]
        ]
    else:
        ref["file"] = "Main/Document.lean"
    _write_proposal(project)
    report, _, _ = _check(project)
    assert report["census"][
        "complete"
    ], "The regression must pass the purely static authored census"
    assert report["lean_accepted"] and report["project_build_passed"]
    assert not report["complete"]
    assert report["missing_compiled_mappings"] == [{"unit_id": "paper", "reference": ref}]


@pytest.mark.parametrize("replayed", [False, True])
def test_fileless_library_reuse_requires_its_own_successful_compiled_profile(
    project, monkeypatch, replayed
):
    name = "Nat.add_comm"
    project["proposal"]["units"][0]["declarations"].append({"name": name, "imports": ["Init"]})
    _write_proposal(project)
    calls = []

    def inspect(root, names, workspace, **kwargs):
        calls.append((root, names, kwargs))
        row = {
            "name": name,
            "kind": "theorem",
            "type": "∀ a b : Nat, a + b = b + a",
            "declaration_origin": "dependency",
            "pending": False,
        }
        return {
            "accepted": replayed,
            "declarations": [row] if replayed else [],
            "profiles": {name: row} if replayed else {},
            "library_declarations": [],
        }

    monkeypatch.setattr(best, "inspect_dependency", inspect)
    report, _, _ = _check(project)
    assert len(calls) == 1 and calls[0][1] == [name]
    assert calls[0][2]["imports"] == ["Init"]
    assert calls[0][2]["local_artifacts"] == project["checks"]["local_artifacts"]
    assert report["complete"] is replayed
    assert (name in {row["name"] for row in report["checks"]["declarations"]}) is replayed
    assert bool(report["missing_compiled_mappings"]) is not replayed


def test_canonical_baseline_drift_stops_before_freezing_or_checker_work(project):
    (project["root"] / "Main.lean").write_text("def external_change := 7\n")
    with pytest.raises(RepreparationRequired, match="Canonical project sources changed"):
        _check(project)
    assert project["calls"] == [] and not project["directory"].exists()


def test_canonical_drift_during_checking_is_terminal_and_preserves_private_project(project):
    project["on_inspect"].append(
        lambda: (project["root"] / "Main.lean").write_text("def external_change := 8\n")
    )
    with pytest.raises(RepreparationRequired, match="Canonical project sources changed"):
        _check(project)
    assert project["calls"] == ["inspect", "build"]
    assert (project["snapshot"] / project["file"]).is_file()
    assert not (project["directory"] / "report.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "invalid_json",
        "invalid_encoding",
        "array",
        "units_object",
        "unit_scalar",
        "declarations_object",
        "reference_scalar",
        "reference_name_object",
    ],
)
def test_malformed_author_proposals_request_an_actionable_repair(project, mutation):
    proposal = project["proposal"]
    if mutation == "array":
        project["proposal"] = []
    elif mutation == "units_object":
        proposal["units"] = {}
    elif mutation == "unit_scalar":
        proposal["units"] = ["not a translation unit"]
    elif mutation == "declarations_object":
        proposal["units"][0]["declarations"] = {}
    elif mutation == "reference_scalar":
        proposal["units"][0]["declarations"] = ["not a declaration reference"]
    elif mutation == "reference_name_object":
        proposal["units"][0]["declarations"][0]["name"] = {}
    _write_proposal(project)
    path = project["snapshot"] / ".leanflow-job/formalization-proposal.json"
    if mutation == "invalid_json":
        path.write_bytes(b"{unfinished")
    elif mutation == "invalid_encoding":
        path.write_bytes(b"\xff")
    with pytest.raises(best.DraftRepairRequired, match="proposal|authored unit"):
        _check(project)
    assert project["calls"] == [] and not project["directory"].exists()
    assert (project["root"] / "Main.lean").read_bytes() == project["baseline"]["Main.lean"]


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_authored_numbers_request_repair_before_any_checker(project, token):
    raw = json.dumps(project["proposal"])[:-1] + ',"untrusted_number":' + token + "}"
    path = project["snapshot"] / ".leanflow-job/formalization-proposal.json"
    path.write_text(raw)
    with pytest.raises(best.DraftRepairRequired, match="proposal JSON"):
        _check(project)
    assert project["calls"] == [] and not project["directory"].exists()
    assert path.read_text() == raw, "Repair feedback cannot rewrite authored input"


def test_compact_proposal_that_exceeds_advisory_serialization_limit_requests_repair_early(project):
    project["proposal"]["notes"] = [""] * 600000
    raw = json.dumps(project["proposal"], separators=(",", ":"))
    assert len(raw.encode()) < MAX_DRAFT_FILE_BYTES
    path = project["snapshot"] / ".leanflow-job/formalization-proposal.json"
    path.write_text(raw)
    with pytest.raises(best.DraftRepairRequired, match="artifact limit"):
        _check(project)
    assert project["calls"] == [] and not project["directory"].exists()
    assert path.read_text() == raw


def test_nonfinite_spellings_in_source_notes_remain_ordinary_text(project):
    project["proposal"]["notes"] = "The source writes NaN, Infinity and -Infinity as text."
    _write_proposal(project)
    report, _, proposal = _check(project)
    assert report["complete"] and proposal["notes"] == project["proposal"]["notes"]


@pytest.mark.parametrize("mutation", ["missing_aggregator", "missing_import", "missing_modules"])
def test_generated_module_and_aggregator_defects_are_actionable_before_checker_work(
    project, mutation
):
    aggregator = project["snapshot"] / project["layout"]["aggregator"]
    if mutation == "missing_import":
        aggregator.write_text("-- The authored module import is missing.\n")
    else:
        aggregator.unlink()
        if mutation == "missing_modules":
            (project["snapshot"] / project["file"]).unlink()
    with pytest.raises(best.DraftRepairRequired, match="aggregator"):
        _check(project)
    assert project["calls"] == []
    assert not project["directory"].exists()


def test_protected_configuration_edit_remains_a_controller_error(project):
    (project["snapshot"] / "lakefile.toml").write_text('name = "changed-by-author"\n')
    with pytest.raises(ValueError, match="changed protected") as error:
        _check(project)
    assert not isinstance(error.value, best.DraftRepairRequired)
    assert project["calls"] == []


def test_retained_author_files_cannot_change_after_the_checker_started(project):
    project["on_inspect"].append(
        lambda: (project["snapshot"] / project["file"]).write_text("def changed := 8\n")
    )
    with pytest.raises(RepreparationRequired, match="Retained author project changed"):
        _check(project)
    assert project["calls"] == ["inspect", "build"]


def test_proposal_census_cannot_change_after_validating_compiled_mappings(project):
    def change_proposal():
        project["proposal"]["notes"] = "A foreign edit appeared during independent checking."
        _write_proposal(project)

    project["on_inspect"].append(change_proposal)
    with pytest.raises(RepreparationRequired, match="Draft proposal changed"):
        _check(project)
    assert project["calls"] == ["inspect", "build"]
    assert not (project["directory"] / "report.json").exists()


def test_proposal_edit_during_dependency_replay_cannot_pass_the_final_project_check(
    project, monkeypatch
):
    name = "Nat.add_comm"
    project["proposal"]["units"][0]["declarations"].append({"name": name, "imports": ["Init"]})
    _write_proposal(project)
    replay_calls = []

    def replay(root, names, workspace, **kwargs):
        assert project["calls"] == ["inspect", "build"]
        replay_calls.append(names)
        project["proposal"]["notes"] = "Foreign proposal edit during dependency replay."
        _write_proposal(project)
        row = {
            "name": name,
            "kind": "theorem",
            "type": "True",
            "declaration_origin": "dependency",
            "pending": False,
        }
        return {
            "accepted": True,
            "declarations": [row],
            "profiles": {name: row},
            "library_declarations": [],
        }

    monkeypatch.setattr(best, "inspect_dependency", replay)
    with pytest.raises(RepreparationRequired, match="Draft proposal changed"):
        _check(project)
    assert replay_calls == [[name]]
    assert not (project["directory"] / "report.json").exists()


@pytest.mark.parametrize("where", ["snapshot", "proposal"])
def test_oversized_private_inputs_are_rejected_by_size_before_reading(project, monkeypatch, where):
    path = project["snapshot"] / (
        project["file"] if where == "snapshot" else ".leanflow-job/formalization-proposal.json"
    )
    with path.open("wb") as stream:
        stream.truncate(MAX_DRAFT_FILE_BYTES + 1)
    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self == path:
            pytest.fail("An oversized file must be rejected before allocating its contents")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(ValueError, match="bounded private regular file"):
        _check(project)
    assert project["calls"] == []
    assert (project["root"] / "Main.lean").read_bytes() == project["baseline"]["Main.lean"]
