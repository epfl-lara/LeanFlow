"""Pin compiled draft policy, honest layout failure, and exact declaration inspection."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import lean_layout, lean_policy


def _row(name: str = "pending", **changes: Any) -> dict[str, Any]:
    return {
        "name": name,
        "module": "Main.Results",
        "kind": "theorem",
        "type": "Expr.const `True []",
        "levels": [],
        "body": "Expr.app sorryAx True",
        "is_prop": True,
        "unsafe": False,
        "internal": False,
        "generated_from": [],
        "generation_kind": "",
        "type_axioms": [],
        "direct_type_sorry": False,
        "type_depends_on": [],
        "type_untracked_sorry": False,
        "axioms": ["sorryAx"],
        "direct_sorry": True,
        "data_sorry": False,
        "pending": True,
        "depends_on": [],
        "untracked_sorry": False,
        "direct_dependencies": ["sorryAx"],
        **changes,
    }


def test_named_obligations_and_conditional_definitions() -> None:
    rows = [
        _row(),
        _row(
            "constructed",
            kind="definition",
            is_prop=False,
            direct_sorry=False,
            pending=False,
            depends_on=["pending"],
            body="Classical.choose pending",
        ),
        _row(
            "opaque",
            kind="opaque",
            is_prop=False,
            direct_sorry=False,
            pending=False,
            axioms=[],
            body="Nat.zero",
        ),
    ]
    result = lean_policy.evaluate_policy(rows, ())
    assert result["accepted"]
    assert [row["name"] for row in result["obligations"]] == ["pending"]
    assert result["profiles"]["constructed"]["conditional"]
    assert not result["profiles"]["opaque"]["conditional"]
    assert result["profiles"]["pending"]["type_hash"]


def test_obligation_dependencies_include_transitive_proof_slots() -> None:
    result = lean_policy.evaluate_policy(
        [
            _row("first"),
            _row("second", depends_on=["first"]),
            _row(
                "constructed",
                kind="definition",
                is_prop=False,
                direct_sorry=False,
                pending=False,
                depends_on=["second"],
            ),
        ],
        (),
    )
    assert result["profiles"]["constructed"]["direct_obligations"] == ["second"]
    assert result["profiles"]["constructed"]["depends_on"] == ["first", "second"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"kind": "axiom"}, "New axioms"),
        ({"unsafe": True}, "unsafe"),
        ({"data_sorry": True}, "Computational data"),
        ({"type_axioms": ["sorryAx"]}, "Declaration type"),
        (
            {"type_axioms": ["sorryAx"], "direct_type_sorry": True, "type_depends_on": ["pending"]},
            "Declaration type",
        ),
        ({"kind": "definition", "is_prop": False, "pending": False}, "proof slot"),
        ({"pending": False}, "proof slot"),
        ({"untracked_sorry": True}, "Untracked placeholder"),
        ({"axioms": ["invented_axiom"]}, "Unapproved axiom"),
        ({"depends_on": ["not-inspected"]}, "escape"),
        ({"data_sorry": None}, "Incomplete"),
    ],
)
def test_fail_closed_policy(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        lean_policy.evaluate_policy([_row(**changes)], ("sorryAx",))


def test_empty_aggregator_and_no_new_library_mapping() -> None:
    assert lean_policy.evaluate_policy([], ())["accepted"]
    result = lean_policy.evaluate_policy(
        [_row("existing", direct_sorry=False, pending=False, axioms=[], body="True.intro")],
        (),
        dependency=True,
    )
    assert result["accepted"] and not result["obligations"]
    with pytest.raises(ValueError, match="already verified"):
        lean_policy.evaluate_policy([_row()], (), dependency=True)


@pytest.mark.parametrize("mode", ["formalization", "dependency", "ordinary_draft"])
@pytest.mark.parametrize("explicit_deadline", [None, 12.5])
def test_inspection_default_respects_explicit_and_ordinary_deadlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    explicit_deadline: float | None,
) -> None:
    durations: list[float] = []
    deadlines: list[float | None] = []

    def after(seconds: float) -> float:
        durations.append(seconds)
        return seconds + 1000.0

    def layout(root: Path, *, deadline: float | None, cancelled: Any) -> dict[str, Any]:
        deadlines.append(deadline)
        return {"accepted": False, "error": "controlled setup boundary"}

    monkeypatch.setattr(lean_policy, "deadline_after", after)
    monkeypatch.setattr(lean_policy, "_admitted_project_sources", lambda *args, **kwargs: {})
    monkeypatch.setattr(lean_policy, "inspect_layout", layout)
    if mode == "dependency":
        result = lean_policy.inspect_dependency(
            tmp_path, ["existing"], tmp_path, imports=["Main"], deadline=explicit_deadline
        )
    else:
        result = lean_policy.inspect_draft(
            tmp_path,
            ["Main.lean"],
            tmp_path,
            (),
            deadline=explicit_deadline,
            require_closed_inputs=mode == "formalization",
        )
    assert not result["accepted"]
    default = 300.0 if mode == "ordinary_draft" else 600.0
    assert durations == ([default] if explicit_deadline is None else [])
    assert deadlines == [default + 1000.0 if explicit_deadline is None else explicit_deadline]


@pytest.mark.parametrize(
    "source",
    [
        'run_elab IO.println "outside input"',
        "initialize registerSomething",
        "theorem p : True := by run_tac pure ()",
        "example : True := by sorry",
        "set_option debug.skipKernelTC true\ntheorem p : True := by sorry",
        "@[implemented_by anything] def f := 1",
        "macro_rules | `(x) => pure x",
    ],
)
def test_effectful_admission_is_visible(source: str) -> None:
    with pytest.raises(ValueError):
        lean_policy.validate_source(source)


def test_source_notes_and_resource_options_are_not_executable() -> None:
    lean_policy.validate_source(
        "/- IO /- nested -/ run_tac -/\nset_option maxHeartbeats 1000\ntheorem p : True := by sorry"
    )


def test_imported_local_elaborator_blocks_before_compilation(tmp_path, monkeypatch) -> None:
    (tmp_path / "Hidden.lean").write_text('macro \\"load_file\\" : term => pure default\n')
    (tmp_path / "Main.lean").write_text("import Hidden\ntheorem p : True := by sorry\n")
    monkeypatch.setattr(
        lean_policy,
        "inspect_layout",
        lambda *args, **kwargs: {
            "accepted": True,
            "libraries": [{"roots": ["Main", "Hidden"], "src_dir": "."}],
        },
    )
    monkeypatch.setattr(
        lean_policy,
        "build_project",
        lambda *args, **kwargs: pytest.fail("must reject before build"),
    )
    result = lean_policy.inspect_draft(tmp_path, ["Main.lean"], tmp_path / "checks", ())
    assert not result["accepted"] and "Hidden.lean" in result["error"]


def test_layout_uses_evaluated_roots_and_srcdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evaluated = {
        "accepted": True,
        "default_targets": ["Library"],
        "libraries": [
            {"name": "Library", "src_dir": "src", "roots": ["Main"], "roots_buildable": [True]}
        ],
    }
    monkeypatch.setattr(lean_layout, "inspect_layout", lambda root: evaluated)
    result = lean_layout.resolve_layout(tmp_path, "paper-1")
    assert result["library"] == "Library"
    assert result["module_prefix"].startswith("Main.Formalization.")
    assert result["aggregator"].startswith("src/Main/")
    assert lean_layout.module_for_file(result, "src/Main/Paper.lean") == "Main.Paper"
    with pytest.raises(ValueError, match="membership"):
        lean_layout.module_for_file(result, "Other.lean")


def test_dynamic_layout_blocks_without_evaluating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.34.1")
    (tmp_path / "lakefile.lean").write_text(
        'import Lake\nopen Lake DSL\nrun_io IO.println "secret"'
    )
    monkeypatch.setattr(
        lean_layout, "isolated_command", lambda **kwargs: pytest.fail("must not evaluate")
    )
    result = lean_layout.resolve_layout(tmp_path, "paper")
    assert not result["accepted"] and "dynamic" in result["error"]


def test_static_lean_dsl_screen(tmp_path: Path) -> None:
    config = tmp_path / "lakefile.lean"
    config.write_text(
        'import Lake\nopen Lake DSL\npackage paper where\n@[default_target]\nlean_lib Actual where\n  srcDir := "src"\n  roots := #[`Main]\n'
    )
    lean_layout._static_configuration(config)


@pytest.fixture
def real_project(tmp_path: Path) -> Path:
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned compiler integration")
    assert toolchain is not None
    (tmp_path / "lean-toolchain").write_text(toolchain)
    (tmp_path / "lakefile.toml").write_text(
        'name = "different_package"\ndefaultTargets = ["Actual"]\n[[lean_lib]]\nname = "Actual"\nsrcDir = "src"\nroots = ["Main"]\n'
    )
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "packages": [],
                "name": "different_package",
                "lakeDir": ".lake",
            }
        )
    )
    (tmp_path / "src/Main").mkdir(parents=True)
    return tmp_path


@pytest.mark.integration
def test_real_plain_draft_shares_compiled_acceptance(real_project: Path) -> None:
    from leanflow_cli.native import harness_artifacts

    root = real_project
    (root / "src/Main.lean").write_text("def original : Nat := 3\n")
    baseline = harness_artifacts.source_files(root)
    snapshot = harness_artifacts.create_snapshot(root, root / ".leanflow/agent")
    (snapshot / "src/Main/Paper.lean").write_text(
        "theorem witness : ∃ n : Nat, n = 3 := by sorry\n"
        "noncomputable def selected : Nat := Classical.choose witness\n"
        "opaque complete : Nat := 7\n"
    )
    (snapshot / "src/Main/Export.lean").write_text("import Main.Paper\n")
    result = harness_artifacts.verify_proposal(
        root=root,
        snapshot=snapshot,
        baseline=baseline,
        directory=root / ".leanflow/checks",
        workflow="draft",
        active_file="",
        allowed_axioms=("propext", "Classical.choice", "Quot.sound"),
        deadline=None,
    )
    assert result["accepted"], result
    assert result["compiled_draft"]["profiles"]["selected"]["conditional"]
    assert result["profiles"]["src/Main/Export.lean"] == {}
    assert harness_artifacts.source_files(root) == baseline


@pytest.mark.integration
def test_real_moved_pending_declaration_cannot_reroll_review(
    real_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import review
    from leanflow_cli.formalization.evidence import put_blob
    from leanflow_cli.formalization.proposal_schema import bind_declarations
    from leanflow_cli.formalization.records import create_document
    from tests.leanflow.test_formalization_revision_review import _review_transport

    root = real_project
    paper = root / "paper.tex"
    paper.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(root, paper)
    record.update(layout={"library": "Actual"}, environment_id="pinned-fixture")
    path = "src/Main.lean"
    calls, options = _review_transport(record, root, monkeypatch, decision="BLOCK")
    hashes, lines = [], []
    for index, code in enumerate(
        [
            "theorem t : True := by sorry\n",
            "theorem t : True := by /- note -/ sorry\n",
            "-- shifted declaration\ntheorem t : True := by /- note -/ sorry\n",
            "-- shifted declaration\ntheorem t : True := by /- longer inline note -/ sorry\n",
            "-- shifted declaration\ntheorem t : False := by /- longer inline note -/ sorry\n",
        ]
    ):
        (root / path).write_text(code)
        checks = lean_policy.inspect_draft(root, [path], root / "checks", ())
        assert checks["accepted"], checks
        record.update(
            checks=checks,
            lean_files={path: put_blob(Path(record["_directory"]) / "blobs", code.encode())},
            units=[
                {
                    "id": "t-unit",
                    "required_source_ids": [record["inventory"]["items"][0]["id"]],
                    "declarations": [{"name": "t", "file": path}],
                    "representation_note": "The source assertion",
                    "depends_on_units": [],
                }
            ],
        )
        bind_declarations(record, checks)
        reference = record["units"][0]["declarations"][0]
        hashes.append(reference["body_hash"])
        lines.append(reference["line_start"])
        result = review.review_revision(record, **options)
        assert not result["accepted"]
        assert len(calls) == (2 if index == 4 else 1)
        if index in {1, 2, 3}:
            assert result["reviews"][0]["cache_reused"]
    assert lines[:4] == [1, 1, 2, 2]
    assert len(set(hashes[:4])) == 4  # Real sorry labels encode both line and column.


@pytest.mark.integration
def test_real_compiled_registry_and_layout(real_project: Path) -> None:
    from leanflow_cli.formalization.proposal_schema import bind_declarations

    root = real_project
    (root / "src/Main/Defs.lean").write_text(
        "theorem exists_n : ∃ n : Nat, n = 3 := by sorry\nnoncomputable def selected : Nat := Classical.choose exists_n\nopaque complete : Nat := 7\nstructure Data where\n  n : Nat\n  property : n = n\ntheorem property (n : Nat) : n = n := by sorry\ndef constructed (n : Nat) : Data := ⟨n, property n⟩\n"
    )
    (root / "src/Main/Results.lean").write_text(
        "import Main.Defs\ntheorem downstream : ∃ n : Nat, n = 3 := exists_n\n"
        "theorem selected_spec : selected = 3 := Classical.choose_spec exists_n\n"
        "namespace Data\ndef ordinary := 4\nprivate def hidden := 5\nend Data\n"
        "def choose (n : Nat) : Nat := match n with | 0 => 1 | k+1 => k\n"
        "theorem choose_zero : choose 0 = 1 := by simp [choose]\n"
    )
    (root / "src/Main.lean").write_text("import Main.Results\n")
    layout = lean_layout.resolve_layout(root, "paper")
    assert layout["accepted"], layout
    assert layout["library"] == "Actual" and layout["root_module"] == "Main"
    assert layout["src_dir"] == "src"
    result = lean_policy.inspect_draft(
        root,
        ["src/Main/Defs.lean", "src/Main/Results.lean", "src/Main.lean"],
        root / "checks",
        ("propext", "Classical.choice", "Quot.sound"),
    )
    assert result["accepted"], result
    profiles = result["profiles"]
    assert {"exists_n", "property"} == {row["name"] for row in result["obligations"]}
    assert profiles["selected"]["depends_on"] == ["exists_n"]
    assert profiles["downstream"]["conditional"]
    assert profiles["selected_spec"]["conditional"]
    assert profiles["selected_spec"]["depends_on"] == ["exists_n"]
    assert profiles["selected_spec"]["type_depends_on"] == ["exists_n"]
    assert not profiles["selected_spec"]["direct_type_sorry"]
    assert profiles["constructed"]["depends_on"] == ["property"]
    assert profiles["complete"]["kind"] == "opaque"
    assert "Data.mk" in profiles and "Data.rec" in profiles
    assert profiles["Data.mk"]["generated_from"] == ["Data"]
    assert profiles["Data.n"]["generated_from"] == ["Data"]
    assert profiles["choose.match_1"]["generated_from"] == ["choose"]
    assert profiles["Data.ordinary"]["generated_from"] == []
    hidden = next(name for name in profiles if name.endswith(".Data.hidden"))
    assert profiles[hidden]["generated_from"] == []
    assert "Main.Defs" in result["kernel_replay"]["modules"]
    assert profiles["exists_n"]["file"] == "src/Main/Defs.lean"
    record: dict[str, Any] = {
        "units": [
            {
                "required_source_ids": ["fixture-source"],
                "declarations": [
                    {"name": name, "file": profiles[name]["file"]}
                    for name in (
                        "exists_n",
                        "selected",
                        "complete",
                        "Data",
                        "property",
                        "constructed",
                        "downstream",
                        "selected_spec",
                        "choose",
                        "choose_zero",
                    )
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="Unmapped compiled declarations") as error:
        bind_declarations(record, result)
    assert "Data.ordinary" in str(error.value) and hidden in str(error.value)
    record["units"][0]["declarations"].extend(
        {"name": name, "file": profiles[name]["file"]} for name in ("Data.ordinary", hidden)
    )
    bind_declarations(record, result)
    assert {reference["name"] for reference in record["units"][0]["declarations"]} == set(profiles)
    direct = lean_policy.inspect_dependency(
        root, ["Nat.add_comm"], root / "checks", imports=["Init"]
    )
    assert direct["accepted"] and not direct["obligations"], direct
    assert all("line_start" not in row for row in direct["declarations"])
    final = subprocess.run(["lake", "build"], cwd=root, capture_output=True, text=True)
    assert final.returncode == 0, final.stderr + final.stdout


@pytest.mark.integration
@pytest.mark.parametrize(
    "source,reason",
    [
        ("def broken : Nat := sorry", "Computational data"),
        (
            "theorem p : True := by\n  have n : Nat := sorry\n  exact True.intro",
            "Computational data",
        ),
        ("axiom invented : False", "unapproved noncompiler axiom"),
        ("theorem p : (sorry : Prop) := by sorry", "Declaration type"),
        ("def broken : { n : Nat // n = n } := ⟨3, sorry⟩", "proof slot"),
    ],
)
def test_real_compiled_rejections(real_project: Path, source: str, reason: str) -> None:
    (real_project / "src/Main.lean").write_text(source)
    result = lean_policy.inspect_draft(
        real_project,
        ["src/Main.lean"],
        real_project / "checks",
        ("propext", "Classical.choice", "Quot.sound"),
    )
    assert not result["accepted"] and reason in json.dumps(result), result


@pytest.mark.integration
def test_kernel_replay_rejects_forged_artifacts_even_if_admission_is_bypassed(
    real_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (real_project / "src/Main.lean").write_text(
        "import Lean\nopen Lean Elab Command\n"
        "set_option debug.skipKernelTC true in\n"
        "run_elab do\n"
        "  addDecl <| .thmDecl {\n"
        "    name := `forged, levelParams := [],\n"
        "    type := mkConst ``False, value := mkConst ``True.intro }\n"
    )
    # Deliberately remove the first defense to exercise independent kernel replay.
    monkeypatch.setattr(lean_policy, "validate_source", lambda source: None)
    result = lean_policy.inspect_draft(real_project, ["src/Main.lean"], real_project / "checks", ())
    assert (
        not result["accepted"] and result["error"] == "Independent kernel inspection failed"
    ), result
    assert "forged" in result["details"]["stderr"], result


@pytest.mark.integration
def test_compiler_ranges_bound_mutual_namespace_holes(real_project: Path) -> None:
    from leanflow_cli.formalization.evidence import digest
    from leanflow_cli.formalization.handoff import authorized_holes, build_handoff
    from leanflow_cli.workflows.prover.source import discover

    source = (
        "namespace Paper\nmutual\n"
        "theorem one : True := by sorry\n"
        "theorem two : True := by sorry\nend\n"
        'local notation "placeholder" => (sorry : True)\n'
        "theorem three : True := by sorry\nend Paper\n"
    )
    (real_project / "src/Main.lean").write_text(source)
    registry = lean_policy.inspect_draft(
        real_project, ["src/Main.lean"], real_project / "checks", ()
    )
    assert registry["accepted"], registry
    handoff = build_handoff(
        real_project,
        document_id="paper",
        revision_id="rev-fixture",
        registry=registry,
        files={"src/Main.lean": digest(source)},
        environment_id="fixture",
        allowed_axioms=(),
    )
    assert {row["declaration"]: row["holes"] for row in handoff["obligations"]} == {
        "Paper.one": [0],
        "Paper.two": [1],
        "Paper.three": [3],
    }
    dag, _ = discover(
        real_project,
        [real_project / "src/Main.lean"],
        fill_definitions=False,
        authorized_holes=authorized_holes(handoff),
    )
    assert {node.name: node.holes for node in dag.nodes} == {
        "Paper.one": [0],
        "Paper.two": [1],
        "Paper.three": [3],
    }


@pytest.mark.integration
def test_module_system_inspects_private_bodies_and_public_proof_holes(real_project: Path) -> None:
    (real_project / "src/Main.lean").write_text(
        "module\npublic theorem public_pending : True := by sorry\n" "private def hidden := 5\n"
    )
    result = lean_policy.inspect_draft(real_project, ["src/Main.lean"], real_project / "checks", ())
    assert result["accepted"], result
    assert result["profiles"]["public_pending"]["pending"]
    hidden = next(row for row in result["declarations"] if row["name"].endswith(".hidden"))
    assert hidden["body"] is not None and hidden["generated_from"] == []


@pytest.mark.integration
@pytest.mark.parametrize("all_authorized", [False, True])
def test_real_handoff_proof_certificate_preserves_unrelated_hole(
    real_project: Path, monkeypatch: pytest.MonkeyPatch, all_authorized: bool
) -> None:
    from leanflow_cli.formalization import handoff
    from leanflow_cli.formalization.evidence import digest, durable_json
    from leanflow_cli.formalization.records import document_directory, save_receipt, save_revision
    from leanflow_cli.workflows.prover.source import SourceDocument

    root = real_project
    source = "namespace Paper\ntheorem target : True := by sorry\ntheorem unrelated : True := by sorry\nend Paper\n"
    path = root / "src/Main.lean"
    path.write_text(source)
    axioms = ("propext", "Classical.choice", "Quot.sound")
    result = lean_policy.inspect_draft(root, ["src/Main.lean"], root / "checks", axioms)
    assert result["accepted"], result
    if not all_authorized:
        result["obligations"] = [
            row for row in result["obligations"] if row["name"] == "Paper.target"
        ]
    directory = document_directory(root, "paper", published=True)
    record = save_revision(
        directory,
        {
            "schema_version": 1,
            "document_id": "paper",
            "checks": result,
            "project_source_hashes": {"src/Main.lean": digest(source)},
            "environment_id": "fixture-environment",
            "source_files": {},
            "allowed_axioms": list(axioms),
        },
    )
    projection = handoff.build_handoff(
        root,
        document_id="paper",
        revision_id=record["revision_id"],
        registry=result,
        files={"src/Main.lean": digest(source)},
        environment_id="fixture-environment",
    )
    durable_json(directory / "handoff.json", projection)
    save_receipt(
        directory,
        {
            "status": "published",
            "revision_id": record["revision_id"],
            "handoff": projection,
        },
    )
    monkeypatch.setattr(handoff, "environment_identity", lambda root: "fixture-environment")
    document = SourceDocument("src/Main.lean", source, replacements={"0": "trivial"})
    path.write_text(document.render())
    if all_authorized:
        with pytest.raises(ValueError, match="obligation remains conditional"):
            handoff.proof_preservation_certificate(
                root, projection, {"src/Main.lean": document}, root / "checks", axioms
            )
        assert not (directory / "proof-revisions").exists()
        return
    certificate = handoff.proof_preservation_certificate(
        root, projection, {"src/Main.lean": document}, root / "checks", axioms
    )
    assert certificate["checks"]["profiles"]["Paper.target"]["axioms"] == []
    assert certificate["checks"]["profiles"]["Paper.unrelated"]["pending"]
    assert "unrelated : True := by sorry" in path.read_text()


def test_mathematical_source_admission_excludes_executable_and_unrelated_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Main.lean").write_text('def main : IO Unit := IO.println "hello"\n')
    (tmp_path / "Script.lean").write_text('#eval IO.println "unrelated"\n')
    (tmp_path / "Math.lean").write_text("theorem p : True := by sorry\n")
    monkeypatch.setattr(lean_policy, "dependency_inputs", lambda root: {})
    layout = {
        "libraries": [{"roots": ["Math"], "src_dir": "."}],
        "executables": [{"root": "Main", "src_dir": "."}],
    }
    admitted = lean_policy._admitted_project_sources(tmp_path, layout=layout)
    assert set(admitted) == {"Math.lean"}
