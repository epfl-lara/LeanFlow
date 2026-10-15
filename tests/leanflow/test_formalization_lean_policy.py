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


@pytest.mark.parametrize("mode", ["formalization", "dependency"])
@pytest.mark.parametrize("explicit_deadline", [None, 12.5])
def test_inspection_default_respects_explicit_deadlines(
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
        )
    assert not result["accepted"]
    default = 600.0
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
def test_real_existing_reuse_requires_controller_built_local_artifacts(real_project: Path) -> None:
    """Use the exact local artifact map, retaining custom-root source identity."""
    from leanflow_cli.formalization.evidence import digest

    root = real_project
    (root / "src/Main.lean").write_text("theorem unrelated : True := by sorry\n")
    relative = "src/Main/Existing.lean"
    path = root / relative
    path.write_text(
        "namespace Existing\n"
        "def double (n : Nat) : Nat := n + n\n"
        "theorem identity_nat (n : Nat) : n = n := rfl\n"
        "theorem unrelated_pending : True := by sorry\n"
        "end Existing\n"
    )
    before = path.read_bytes()
    layout = lean_layout.inspect_layout(root)
    workspace = root / "build-check"
    workspace.mkdir()
    build = lean_policy.build_project(root, layout, workspace)
    assert build["success"], build
    args = {
        "imports": ["Main.Existing"],
        "allowed_axioms": ("propext", "Classical.choice", "Quot.sound"),
    }
    denied = lean_policy.inspect_dependency(
        root, ["Existing.double", "Existing.identity_nat"], root / "denied", **args
    )
    assert not denied["accepted"], "Ambient build outputs must not authorize library reuse"
    result = lean_policy.inspect_dependency(
        root,
        ["Existing.double", "Existing.identity_nat"],
        root / "checked",
        local_artifacts=build["local_artifacts"],
        **args,
    )
    assert result["accepted"], result
    assert result["obligations"] == [] and path.read_bytes() == before
    assert set(result["profiles"]) == {"Existing.double", "Existing.identity_nat"}
    for row in result["declarations"]:
        assert row["declaration_origin"] == "existing_project"
        assert row["module"] == "Main.Existing" and row["file"] == relative
        assert row["source_sha256"] == digest(before) and row["line_start"] in {2, 3}
    unfinished = lean_policy.inspect_dependency(
        root,
        ["Existing.unrelated_pending"],
        root / "pending-reuse",
        local_artifacts=build["local_artifacts"],
        **args,
    )
    assert not unfinished["accepted"] and "already verified" in unfinished["error"]


@pytest.mark.integration
def test_real_formalization_registry_allows_named_proofs_and_aggregators(
    real_project: Path,
) -> None:
    """Inspect genuine formalization modules through the shared compiled policy."""
    root = real_project
    (root / "src/Main.lean").write_text("def original : Nat := 3\n")
    (root / "src/Main/Paper.lean").write_text(
        "theorem witness : ∃ n : Nat, n = 3 := by sorry\n"
        "noncomputable def selected : Nat := Classical.choose witness\n"
        "opaque complete : Nat := 7\n"
    )
    (root / "src/Main/Export.lean").write_text("import Main.Paper\n")
    result = lean_policy.inspect_draft(
        root,
        ["src/Main/Paper.lean", "src/Main/Export.lean"],
        root / ".leanflow/checks",
        ("propext", "Classical.choice", "Quot.sound"),
    )
    assert result["accepted"], result
    assert result["profiles"]["selected"]["conditional"]
    assert {row["name"] for row in result["obligations"]} == {"witness"}
    assert result["modules"]["Main.Export"] == "src/Main/Export.lean"


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


def _imported_equation_rows() -> list[dict[str, Any]]:
    parent = _row(
        "Main.d",
        module="Main.Base",
        kind="definition",
        is_prop=False,
        body="Nat.add n n",
        direct_sorry=False,
        pending=False,
        axioms=[],
        source_range={"line_start": 1, "column_start": 0, "line_end": 1, "column_end": 30},
    )
    equation = _row(
        "Main.d.eq_1",
        module="Main.Goal",
        body="Eq.refl (d n)",
        direct_sorry=False,
        pending=False,
        axioms=[],
        generated_from=["Main.d"],
        generation_kind="equation",
        source_range=None,
        line_start=None,
        direct_dependencies=["Main.d", "Eq.refl"],
        generated_parent_profiles=[
            {
                key: parent[key]
                for key in ("name", "module", "kind", "type", "levels", "body", "source_range")
            }
        ],
    )
    return [parent, equation]


def test_imported_lazy_equation_binds_exact_inspected_source_parent() -> None:
    result = lean_policy.evaluate_policy(_imported_equation_rows(), ())
    assert result["accepted"]
    assert result["profiles"]["Main.d.eq_1"]["origin"] == "compiler_generated"


@pytest.mark.parametrize(
    "fault",
    [
        "body",
        "module",
        "levels",
        "source_range",
        "user_source",
        "no_evidence",
        "wrong_kind",
        "unreferenced",
        "missing_parent",
    ],
)
def test_imported_equation_rejects_forged_or_stale_parent_authority(fault: str) -> None:
    rows = _imported_equation_rows()
    parent, equation = rows
    if fault in {"body", "module", "levels", "source_range"}:
        parent[fault] = [] if fault == "levels" else "changed"
        if fault == "levels":
            parent[fault] = ["u"]
    elif fault == "user_source":
        equation["source_range"] = parent["source_range"]
    elif fault == "no_evidence":
        equation.pop("generated_parent_profiles")
    elif fault == "wrong_kind":
        equation["generation_kind"] = "elaborator_dependency"
    elif fault == "unreferenced":
        equation["direct_dependencies"] = ["Eq.refl"]
    else:
        rows = [equation]
    with pytest.raises(ValueError, match="no inspected parent"):
        lean_policy.evaluate_policy(rows, ())


@pytest.mark.integration
def test_real_lazy_imported_equation_authenticates_parent_and_user_aux_keeps_ownership(
    real_project: Path,
) -> None:
    root = real_project
    base, goal = root / "src/Main/Base.lean", root / "src/Main/Goal.lean"
    base.write_text("namespace Sample\ndef d (n : Nat) : Nat := n + n\nend Sample\n")
    goal.write_text(
        "import Main.Base\nnamespace Sample\ntheorem goal (n : Nat) : d n = 2 * n := by simpa only [d] using (Nat.two_mul n).symm\nend Sample\n"
    )
    result = lean_policy.inspect_draft(
        root, ["src/Main/Base.lean", "src/Main/Goal.lean"], root / "checks", ()
    )
    assert result["accepted"], result
    parent, equation = result["profiles"]["Sample.d"], result["profiles"]["Sample.d.eq_1"]
    assert parent["module"] == "Main.Base" and equation["module"] == "Main.Goal"
    assert equation["generation_kind"] == "equation" and equation["source_range"] is None
    assert equation["generated_parent_profiles"][0]["body"] == parent["body"]
    assert equation["axioms"] == [] and result["obligations"] == []
    imported = lean_policy.inspect_draft(root, ["src/Main/Goal.lean"], root / "imported-checks", ())
    assert imported["accepted"], imported
    assert "Sample.d" not in imported["profiles"]
    assert "Sample.d" not in {row["name"] for row in imported["declarations"]}
    origin_parent = imported["origin_parent_profiles"]["Sample.d"]
    assert origin_parent["file"] == "src/Main/Base.lean"
    assert (
        origin_parent["source_sha256"] == lean_policy.hashlib.sha256(base.read_bytes()).hexdigest()
    )
    assert origin_parent["body_hash"] == parent["body_hash"]
    assert imported["obligations"] == []
    from leanflow_cli.formalization.proposal_schema import bind_declarations

    assert "Sample.d" in imported["profiles"]["Sample.goal"]["direct_dependencies"]
    proposal = {
        "units": [
            {
                "id": "goal",
                "required_source_ids": ["source-goal"],
                "declarations": [{"name": "Sample.goal", "file": "src/Main/Goal.lean"}],
            }
        ]
    }
    bind_declarations(proposal, imported)
    assert {row["name"] for row in proposal["units"][0]["declarations"]} == {
        "Sample.goal",
        "Sample.d.eq_1",
    }
    assert proposal["units"][0]["required_source_ids"] == ["source-goal"]
    assert proposal["units"][0]["declarations"][1]["compiler_owner_names"] == ["Sample.goal"]
    # A user-written auxiliary-looking name retains its source authority and does
    # not receive the compiler-origin exception merely because of its spelling.
    goal.write_text(
        "import Main.Base\nnamespace Sample\ntheorem d.eq_1 (n : Nat) : d n = n + n := by rfl\nend Sample\n"
    )
    user = lean_policy.inspect_draft(
        root, ["src/Main/Base.lean", "src/Main/Goal.lean"], root / "user-checks", ()
    )
    assert not user["accepted"] and "reserved name" in user["build"]["stdout"], user
    goal.write_text(
        "import Main.Base\nnamespace Sample\ntheorem d.user_aux (n : Nat) : d n = n + n := by rfl\nend Sample\n"
    )
    ordinary = lean_policy.inspect_draft(
        root, ["src/Main/Base.lean", "src/Main/Goal.lean"], root / "ordinary-checks", ()
    )
    assert ordinary["accepted"], ordinary
    authored = ordinary["profiles"]["Sample.d.user_aux"]
    assert authored["source_range"] and authored["generated_from"] == []
    assert authored["origin"] == "declaration"


@pytest.mark.integration
@pytest.mark.parametrize("case", ["valid", "invalid", "sorry", "native_decide", "native_reference"])
def test_real_compiler_compatibility_preserves_kernel_native_reduction_policy(
    real_project: Path, tmp_path: Path, case: str
) -> None:
    """Exercise current Name guards on the selected installed compiler without fake acceptance."""
    import time

    from leanflow_cli.formalization.preparation_resources import supervise_preparation
    from leanflow_cli.workflows.project_setup.execution import build_setup_execution_policy

    root = real_project
    toolchain = (root / "lean-toolchain").read_text()
    source = {
        "valid": "theorem valid : 1 + 1 = 2 := by decide\n",
        "invalid": "theorem invalid : False := by trivial\n",
        "sorry": "theorem pending : True := by sorry\n",
        "native_decide": "theorem nativeProof : 1 + 1 = 2 := by native_decide\n",
        "native_reference": (
            "namespace Lean\ndef reduceBool : Nat := 0\nend Lean\n" if "v4.35" in toolchain else ""
        )
        + "def nativeReference := Lean.reduceBool\n",
    }[case]
    target = root / "src/Main/Goal.lean"
    target.write_text(source)
    stage = tmp_path.parent / (tmp_path.name + "-supervision")
    stage.mkdir()
    policy = build_setup_execution_policy(
        stage,
        canonical_root=root,
        toolchain=toolchain,
        network_allowed=False,
        deadline=time.monotonic() + 120,
        max_owned_rss_bytes=16 * 1024**3,
    )
    with supervise_preparation(policy) as cancelled:
        result = lean_policy.inspect_draft(
            root,
            ["src/Main/Goal.lean"],
            root / ".leanflow/checks",
            ("propext", "Classical.choice", "Quot.sound"),
            deadline=policy.deadline,
            cancelled=cancelled.is_set,
        )
        assert target.read_text() == source
        if case == "valid":
            assert result["accepted"] and result["obligations"] == [], result
        elif case == "sorry":
            assert result["accepted"] and len(result["obligations"]) == 1, result
            assert result["profiles"]["pending"]["pending"]
            assert "sorryAx" in result["profiles"]["pending"]["axioms"]
        elif case == "invalid":
            assert not result["accepted"] and not result["build"]["success"], result
            assert "False" in result["build"]["stdout"]
        else:
            assert (
                not result["accepted"] and result["error"] == "Independent kernel inspection failed"
            ), result
            diagnostic = result["details"]["stderr"]
            assert (
                "unapproved noncompiler axiom" in diagnostic
                if case == "native_decide"
                else "references native reduction" in diagnostic
            ), diagnostic
            assert result["details"]["timed_out"] is False
            assert "unknown constant" not in diagnostic
    assert policy.usage.cleanup_verified


def test_imported_equation_origin_only_parent_never_becomes_an_authored_declaration() -> None:
    parent, equation = _imported_equation_rows()
    result = lean_policy.evaluate_policy([equation], (), origin_parents=[parent])
    assert set(result["profiles"]) == {"Main.d.eq_1"}
    assert [row["name"] for row in result["declarations"]] == ["Main.d.eq_1"]
    assert result["obligations"] == []
    assert set(result["origin_parent_profiles"]) == {"Main.d"}


@pytest.mark.parametrize(
    "fault",
    [
        {"unsafe": True},
        {"data_sorry": True},
        {"axioms": ["sorryAx"]},
        {"source_range": None},
        {"type_axioms": ["sorryAx"]},
    ],
)
def test_origin_only_parent_keeps_axiom_placeholder_and_source_admission(
    fault: dict[str, Any],
) -> None:
    parent, equation = _imported_equation_rows()
    parent.update(fault)
    with pytest.raises(ValueError):
        lean_policy.evaluate_policy([equation], (), origin_parents=[parent])


def _origin_only_proposal() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a policy-admitted equation with a separate frozen source parent."""
    parent, equation = _imported_equation_rows()
    consumer = _row(
        "Main.goal",
        module=equation["module"],
        direct_sorry=False,
        pending=False,
        axioms=[],
        body="Eq.refl (d n)",
        source_range=parent["source_range"],
        direct_dependencies=[parent["name"]],
    )
    checks = lean_policy.evaluate_policy([consumer, equation], (), origin_parents=[parent])
    for row in checks["declarations"]:
        row.update(file="Main/Goal.lean", declaration_origin="generated")
    checks["origin_parent_profiles"][parent["name"]].update(
        file="Main/Base.lean", source_sha256="a" * 64, declaration_origin="existing_project"
    )
    checks["admitted_project_sources"] = {"Main/Base.lean": "a" * 64}
    record = {
        "units": [
            {
                "id": "goal",
                "required_source_ids": ["source-goal"],
                "declarations": [{"name": consumer["name"], "file": "Main/Goal.lean"}],
            }
        ]
    }
    return record, checks


@pytest.mark.parametrize("dependency", ["Main.d", "Main.d.eq_1"])
def test_origin_only_equation_binding_keeps_borrowed_parent_out_of_authored_units(dependency):
    from leanflow_cli.formalization.proposal_schema import bind_declarations

    record, checks = _origin_only_proposal()
    checks["profiles"]["Main.goal"]["direct_dependencies"] = [dependency]
    bind_declarations(record, checks)
    references = record["units"][0]["declarations"]
    assert {row["name"] for row in references} == {"Main.goal", "Main.d.eq_1"}
    assert references[1]["compiler_owner_names"] == ["Main.goal"]
    assert record["units"][0]["required_source_ids"] == ["source-goal"]
    assert "Main.d" not in checks["profiles"] and checks["obligations"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "unconsumed",
        "consumer_file",
        "consumer_module",
        "consumer_source",
        "child_source",
        "parent_body",
        "parent_source_hash",
        "ordinary_auxiliary",
        "unknown_matcher",
    ],
)
def test_origin_only_equation_binding_refuses_missing_or_changed_source_authority(fault):
    from leanflow_cli.formalization.proposal_schema import bind_declarations

    record, checks = _origin_only_proposal()
    consumer, equation = checks["declarations"]
    parent = checks["origin_parent_profiles"]["Main.d"]
    if fault == "unconsumed":
        consumer["direct_dependencies"] = []
    elif fault == "consumer_file":
        consumer["file"] = record["units"][0]["declarations"][0]["file"] = "Main/Other.lean"
    elif fault == "consumer_module":
        consumer["module"] = "Main.Other"
    elif fault == "consumer_source":
        consumer["source_range"] = None
    elif fault == "child_source":
        equation["source_range"] = consumer["source_range"]
    elif fault == "parent_body":
        parent["body"] = "Nat.zero"
    elif fault == "parent_source_hash":
        parent["source_sha256"] = "b" * 64
    elif fault == "ordinary_auxiliary":
        equation.update(
            generated_from=[], generation_kind="", source_range=consumer["source_range"]
        )
    else:
        equation["generation_kind"] = "matcher"
    with pytest.raises(ValueError, match="owner|consumer|Unmapped"):
        bind_declarations(record, checks)
