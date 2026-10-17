"""Preserve compiled meaning when the same library declaration has multiple roles."""

import copy
from typing import Any

import pytest

from leanflow_cli.formalization.compiled_evidence import (
    assert_rebuild_artifacts,
    assert_reuse_sources_unchanged,
    merge_library_evidence,
    merge_reused_declarations,
)
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.proposal_schema import bind_declarations


@pytest.mark.parametrize("reference", ["file", "fileless"])
def test_existing_root_reuse_cannot_bind_controller_rewritten_source(reference):
    baseline = {"src/Main.lean": b"def existing : Nat := 42\n"}
    changed = {"src/Main.lean": b"import Main.Paper\n" + baseline["src/Main.lean"]}
    row = {
        "name": "existing",
        "file": "src/Main.lean",
        "declaration_origin": "existing_project",
        "source_sha256": digest(changed["src/Main.lean"]),
    }
    if reference == "fileless":
        # The controller restores this file from the compiled local artifact,
        # so file-less author refs must receive the same immutable-source guard.
        row["source_sha256"] = digest(baseline["src/Main.lean"])
    with pytest.raises(ValueError, match="unchanged original source bytes"):
        assert_reuse_sources_unchanged([row], baseline, changed)
    row["source_sha256"] = digest(baseline["src/Main.lean"])
    assert_reuse_sources_unchanged([row], baseline, {})


def library_row(*, root: bool) -> dict[str, Any]:
    return {
        "name": "Library.fact",
        "module": "Library",
        "kind": "theorem",
        "levels": [],
        "type": "True",
        "type_hash": "type",
        "axioms": [],
        "authority": "kernel-checked",
        "pretty_type": "True",
        "body": "True.intro" if root else None,
        "body_hash": "proof" if root else None,
        "body_omitted": not root,
        "pretty_body": "True.intro" if root else None,
        "semantic_surface": root,
        "direct_dependencies": ["True", "True.intro"] if root else ["True"],
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_draft_proof_dependency_and_explicit_reuse_keep_full_body(reverse: bool):
    rows = [library_row(root=False), library_row(root=True)]
    before = copy.deepcopy(rows)
    if reverse:
        rows.reverse()
    merged = merge_library_evidence(rows[:1], rows[1:])
    assert merged == [library_row(root=True)]
    assert sorted(rows, key=lambda row: row["semantic_surface"]) == before


def test_proof_only_definition_becomes_required_semantic_evidence():
    old = {**library_row(root=True), "kind": "definition", "semantic_surface": False}
    current = {**old, "semantic_surface": True, "pretty_type": "@True"}
    result = merge_library_evidence([old], [current])
    assert result[0]["semantic_surface"] and result[0]["body"] == old["body"]


@pytest.mark.parametrize(
    "field",
    [
        "module",
        "kind",
        "levels",
        "axioms",
        "role",
        "type",
        "type_hash",
        "body",
        "body_hash",
        "authority",
    ],
)
def test_actual_compiled_changes_are_rejected(field: str):
    old = library_row(root=True)
    changed = {**old, field: "changed"}
    with pytest.raises(ValueError, match="differs between inspections"):
        merge_library_evidence([old], [changed])


def test_reuse_cannot_replace_a_generated_file_binding():
    root = library_row(root=True)
    checks = {"declarations": [{**root, "file": "Main/Draft.lean"}], "profiles": {}}
    with pytest.raises(ValueError, match="cannot be mapped as library reuse"):
        merge_reused_declarations(checks, {"declarations": [root], "profiles": {}})
    assert checks["declarations"][0]["file"] == "Main/Draft.lean"


def test_repeated_reuse_with_different_import_groups_has_one_registry_entry():
    row = library_row(root=True)
    checks: dict[str, Any] = {"declarations": [], "profiles": {}, "library_declarations": []}
    result = {"declarations": [row], "profiles": {row["name"]: row}, "library_declarations": [row]}
    merge_reused_declarations(checks, result)
    merge_reused_declarations(checks, copy.deepcopy(result))
    assert checks["declarations"] == [row]
    assert checks["library_declarations"] == [row]


def test_existing_project_reuse_retains_readonly_source_and_no_proof_ownership():
    row = {
        **library_row(root=True),
        "declaration_origin": "existing_project",
        "file": "src/Main/Existing.lean",
        "source_sha256": "source-bytes",
        "line_start": 4,
        "pending": False,
    }
    checks: dict[str, Any] = {"declarations": [], "profiles": {}, "obligations": []}
    result = {"declarations": [row], "profiles": {row["name"]: row}}
    merge_reused_declarations(checks, result)
    merge_reused_declarations(checks, copy.deepcopy(result))
    assert checks["declarations"] == [row]
    assert checks["profiles"][row["name"]]["file"] == "src/Main/Existing.lean"
    assert checks["obligations"] == []


@pytest.mark.parametrize("changes", [{"source_sha256": ""}, {"file": ""}, {"pending": True}])
def test_existing_project_reuse_requires_verified_frozen_source(changes):
    row = {
        **library_row(root=True),
        "declaration_origin": "existing_project",
        "file": "src/Main/Existing.lean",
        "source_sha256": "source-bytes",
        "pending": False,
        **changes,
    }
    with pytest.raises(ValueError, match="immutable verified source evidence"):
        merge_reused_declarations({"declarations": [], "profiles": {}}, {"declarations": [row]})


def test_binding_rejects_duplicate_registry_instead_of_last_row_winning():
    row = library_row(root=True)
    with pytest.raises(ValueError, match="Duplicate compiled"):
        bind_declarations({"units": []}, {"declarations": [row, {**row, "file": "Main.lean"}]})


def test_mapping_must_retain_the_project_file():
    row = {**library_row(root=True), "file": "Main.lean"}
    record = {"units": [{"declarations": [{"name": row["name"], "imports": ["Main"]}]}]}
    with pytest.raises(ValueError, match="does not identify a compiled declaration"):
        bind_declarations(record, {"declarations": [row]})


@pytest.mark.parametrize("change", ["digest", "missing", "parts", "absent_evidence"])
def test_whole_build_cannot_replace_inspected_compiled_meaning(change: str):
    before = {"Paper": {"parts": [["/lib/Paper.olean"], []], "sha256": {"/lib/Paper.olean": "a"}}}
    after = copy.deepcopy(before)
    if change == "digest":
        after["Paper"]["sha256"]["/lib/Paper.olean"] = "b"
    elif change == "missing":
        after.clear()
    elif change == "parts":
        after["Paper"]["parts"] = [["/other/Paper.olean"], []]
    else:
        before["Paper"].pop("sha256")
    with pytest.raises(ValueError, match="changed an inspected artifact"):
        assert_rebuild_artifacts(before, after)


def test_whole_build_may_add_other_modules_without_changing_checked_ones():
    artifact = {"parts": [["/lib/Paper.olean"], []], "sha256": {"/lib/Paper.olean": "a"}}
    assert_rebuild_artifacts({"Paper": artifact}, {"Paper": artifact, "Main": {}})
