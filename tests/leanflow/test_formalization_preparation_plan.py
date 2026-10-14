"""Pin preparation planning to frozen sources, existing pins and explicit acquisition authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization.preparation_plan import (
    CATALOG,
    DEFAULT_TOOLCHAIN,
    REPL_URL,
    configuration_hashes,
    default_preparation_plan,
    plan_identity,
    validate_preparation_plan,
)
from leanflow_cli.formalization.records import create_document


@pytest.fixture
def paper(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "paper.tex"
    path.write_text(
        r"\begin{definition}For natural $n$, define $d(n)=n+n$.\end{definition}"
        "\n"
        r"\begin{theorem}For natural $n$, $d(n)=n+n$.\end{theorem}"
        "\n"
    )
    return tmp_path, create_document(tmp_path, path)


def _project(root: Path, *, toolchain: str = DEFAULT_TOOLCHAIN, packages: bool = True) -> None:
    (root / "lean-toolchain").write_text(toolchain + "\n")
    (root / "lakefile.toml").write_text(
        'name = "existing"\ndefaultTargets = ["Actual"]\n'
        '[[lean_lib]]\nname = "Actual"\nsrcDir = "src"\nroots = ["Main"]\n'
        + (
            '[[require]]\nname = "mathlib"\ngit = "https://example.org/mathlib"\nrev = "'
            + "a" * 40
            + '"\n'
            if packages
            else ""
        )
    )
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": (
                    [
                        {
                            "name": "mathlib",
                            "type": "git",
                            "url": "https://example.org/mathlib",
                            "rev": "a" * 40,
                        }
                    ]
                    if packages
                    else []
                )
            }
        )
    )


def test_source_only_plan_is_readonly_pinned_and_selects_definition_result(paper):
    root, record = paper
    before = configuration_hashes(root)
    plan = default_preparation_plan(root, record)
    assert plan == default_preparation_plan(root, record)
    assert configuration_hashes(root) == before and all(value is None for value in before.values())
    assert {row["name"] for row in plan["packages"]} == {"mathlib", "repl"}
    assert all(
        len(row["revision"]) == 40 and row["authorization"] == "catalog" for row in plan["packages"]
    )
    assert plan["layout"]["mode"] == "new_project" and plan["layout"]["root_file"] == "Main.lean"
    assert plan["slice_source_ids"] == [item["id"] for item in record["inventory"]["items"]]
    assert len(plan["mathematical_prerequisites"]) == 1 and plan["plan_id"] == plan_identity(plan)
    assert not (root / "Main.lean").exists()


def test_lean_preset_preserves_core_setup_without_adding_mathlib(paper):
    root, record = paper
    plan = default_preparation_plan(root, record, preset="lean")
    assert plan["required_imports"] == ["Init"]
    assert [row["name"] for row in plan["packages"]] == ["repl"]


def test_existing_project_reuses_custom_pin_and_source_root(paper):
    root, record = paper
    _project(root, toolchain="leanprover/lean4:v4.34.1")
    before = configuration_hashes(root)
    plan = default_preparation_plan(root, record)
    assert plan["packages"] == [] and not plan["blockers"]
    assert plan["catalog_requirements"] == [
        {
            "name": "repl",
            "url": REPL_URL,
            "toolchain": "leanprover/lean4:v4.34.1",
            "resolution": "matching-release",
        }
    ]
    assert plan["existing_packages"][0]["manifest"]["rev"] == "a" * 40
    assert plan["layout"]["root_file"] == "src/Main.lean" and plan["layout"]["library"] == "Actual"
    assert configuration_hashes(root) == before


def test_missing_mathlib_on_other_compiler_is_explicit_blocker(paper):
    root, record = paper
    _project(root, toolchain="leanprover/lean4:v4.34.1", packages=False)
    plan = default_preparation_plan(root, record)
    assert plan["blockers"] and "compatible full Git pin" in plan["blockers"][0]
    assert not any(row["name"] == "mathlib" for row in plan["packages"])


def test_literal_lean_configuration_plans_without_evaluation(paper):
    root, record = paper
    (root / "lean-toolchain").write_text(DEFAULT_TOOLCHAIN)
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage paper where\n"
        'require mathlib from git "https://example.org/mathlib" @ "' + "a" * 40 + '"\n'
        '@[default_target]\nlean_lib Actual where\n  srcDir := "src"\n  roots := #[`Main]\n'
    )
    plan = default_preparation_plan(root, record)
    assert plan["layout"]["root_file"] == "src/Main.lean"
    assert [row["name"] for row in plan["existing_packages"]] == ["mathlib"]
    assert [row["name"] for row in plan["packages"]] == ["repl"]


def test_executable_only_project_gets_dedicated_library_plan(paper):
    root, record = paper
    (root / "lakefile.toml").write_text('name = "app"\n[[lean_exe]]\nname = "app"\nroot = "Main"\n')
    plan = default_preparation_plan(root, record, preset="lean")
    assert plan["layout"]["mode"] == "dedicated_library"
    assert plan["layout"]["root_module"] == "Formalization"


def test_layout_prefers_existing_default_library_like_native_allocator(paper):
    root, record = paper
    (root / "lakefile.toml").write_text(
        'name = "paper"\ndefaultTargets = ["Results"]\n'
        '[[lean_lib]]\nname = "Aux"\nroots = ["ZZ", "AA"]\n'
        '[[lean_lib]]\nname = "Results"\nsrcDir = "src"\nroots = ["ZZResult", "AAResult"]\n'
    )
    plan = default_preparation_plan(root, record, preset="lean")
    assert (
        plan["layout"]["library"] == "Results"
        and plan["layout"]["root_file"] == "src/AAResult.lean"
    )


@pytest.mark.parametrize(
    "revision,url",
    [
        ("main", "https://example.org/custom"),
        ("b" * 40, "ssh://example.org/custom"),
        ("b" * 40, "https://user:password@example.org/custom"),
        ("b" * 40, "https://example.org/custom?q=token"),
    ],
)
def test_user_sources_need_plain_https_and_full_pins(paper, revision, url):
    root, record = paper
    with pytest.raises(ValueError, match="immutable Git|HTTPS source"):
        default_preparation_plan(
            root,
            record,
            overrides={"packages": [{"name": "custom", "url": url, "revision": revision}]},
        )


def test_explicit_user_pin_is_authorized_but_never_replaces_existing_pin(paper):
    root, record = paper
    package = {
        "name": "custom",
        "url": "https://example.org/custom",
        "revision": "b" * 40,
        "imports": ["Custom.Defs"],
    }
    plan = default_preparation_plan(root, record, overrides={"packages": [package]})
    custom = next(row for row in plan["packages"] if row["name"] == "custom")
    assert (
        custom["authorization"] == "user"
        and custom in plan["network_policy"]["user_authorized_packages"]
    )
    _project(root)
    exact = {"name": "mathlib", "url": "https://example.org/mathlib", "revision": "a" * 40}
    assert not any(
        row["name"] == "mathlib"
        for row in default_preparation_plan(root, record, overrides={"packages": [exact]})[
            "packages"
        ]
    )
    with pytest.raises(ValueError, match="never replace"):
        default_preparation_plan(
            root, record, overrides={"packages": [{**exact, "revision": "b" * 40}]}
        )


@pytest.mark.parametrize(
    "change",
    ["config", "source_revision", "toolchain", "unlisted", "slice", "layout", "repl_import"],
)
def test_plan_validation_rejects_drift_or_broadened_authority(paper, change):
    root, record = paper
    plan = default_preparation_plan(root, record)
    damaged = copy.deepcopy(plan)
    if change == "config":
        (root / "lean-toolchain").write_text(DEFAULT_TOOLCHAIN)
    elif change == "source_revision":
        damaged["source_revision_id"] = "other"
    elif change == "toolchain":
        damaged["toolchain"] = "leanprover/lean4:v4.34.1"
    elif change == "unlisted":
        damaged["packages"].append(
            {"name": "custom", "url": "https://example.org/custom", "revision": "b" * 40}
        )
    elif change == "slice":
        damaged["slice_source_ids"] = [record["inventory"]["items"][1]["id"]]
    elif change == "layout":
        damaged["layout"]["root_file"] = "Other.lean"
    else:
        damaged["required_imports"] = ["REPL"]
    damaged["plan_id"] = plan_identity(damaged)
    with pytest.raises(ValueError):
        validate_preparation_plan(root, record, damaged)


def test_dynamic_lake_configuration_is_never_executed(paper):
    root, record = paper
    (root / "lakefile.lean").write_text('import Lake\nopen Lake DSL\nrun_cmd IO.println "effect"\n')
    with pytest.raises(ValueError, match="dynamic Lake configuration"):
        default_preparation_plan(root, record)


def test_catalog_identity_remains_exact_when_mathematical_notes_change(paper):
    root, record = paper
    plan = default_preparation_plan(root, record)
    changed = copy.deepcopy(plan)
    changed["representation_notes"] = [
        "Use naturals and check that doubling exists in the project."
    ]
    changed["plan_id"] = plan_identity(changed)
    assert changed["plan_id"] != plan["plan_id"]
    assert validate_preparation_plan(root, record, changed)["network_policy"]["catalog"] == CATALOG
