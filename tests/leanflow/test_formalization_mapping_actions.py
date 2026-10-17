"""Repair many-to-many mappings and direct reuse without widening file ownership."""

import copy

import pytest

from leanflow_cli.formalization.actions import apply_action


def _record() -> dict:
    return {
        "actions": [],
        "inventory": {"audit_complete": True, "items": [{"id": key} for key in ("a", "b", "c")]},
        "units": [
            {
                "id": "shared",
                "required_source_ids": ["a", "b"],
                "depends_on_units": [],
                "declarations": [],
            },
            {
                "id": "other",
                "required_source_ids": ["c"],
                "depends_on_units": ["shared"],
                "declarations": [],
            },
        ],
    }


def test_shared_mapping_repair_requires_complete_sources_and_supports_imported_reuse() -> None:
    record = _record()
    original = copy.deepcopy(record)
    action = {
        "type": "set_mapping",
        "item_id": "a",
        "lean_refs": [
            {"imports": ["Init"], "name": "Nat.add_comm", "declaration_origin": "dependency"}
        ],
        "rationale": "Both source claims use the existing identity",
    }
    with pytest.raises(ValueError, match="all their source items"):
        apply_action(record, action)
    assert record == original
    apply_action(record, {**action, "source_ids": ["a", "b"], "unit_id": "shared"})
    shared = next(unit for unit in record["units"] if unit["id"] == "shared")
    assert shared["required_source_ids"] == ["a", "b"]
    assert shared["declarations"] == action["lean_refs"]
    assert next(unit for unit in record["units"] if unit["id"] == "other")["depends_on_units"] == [
        "shared"
    ]
    assert not record["inventory"]["audit_complete"]


def test_repair_one_of_several_units_for_same_source_keeps_other_unit() -> None:
    record = _record()
    record["units"].append(
        {
            "id": "reuse",
            "required_source_ids": ["a"],
            "depends_on_units": [],
            "declarations": [{"imports": ["Init"], "name": "True"}],
        }
    )
    apply_action(
        record,
        {
            "type": "set_mapping",
            "item_id": "a",
            "unit_id": "reuse",
            "lean_refs": [
                {
                    "file": "lib/MyLib/Existing.lean",
                    "name": "Existing.identity_nat",
                    "declaration_origin": "existing_project",
                }
            ],
            "rationale": "Use the already compiled project theorem",
        },
    )
    assert {unit["id"] for unit in record["units"]} == {"shared", "other", "reuse"}
    reuse = next(unit for unit in record["units"] if unit["id"] == "reuse")
    assert reuse["declarations"][0]["file"] == "lib/MyLib/Existing.lean"
    assert reuse["declarations"][0]["declaration_origin"] == "existing_project"


@pytest.mark.parametrize(
    "ref",
    [
        {"file": "../escape.lean", "name": "t"},
        {"imports": ["../Init"], "name": "t"},
        {"file": "Main.lean", "name": "t", "declaration_origin": "dependency"},
        {"imports": ["Init"], "name": "t", "declaration_origin": "generated"},
        {"imports": ["Init"], "name": "t", "declaration_origin": "trusted"},
    ],
)
def test_mapping_repair_rejects_unsafe_or_inconsistent_origins(ref: dict) -> None:
    record = _record()
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        apply_action(
            record,
            {
                "type": "set_mapping",
                "source_ids": ["a", "b"],
                "unit_id": "shared",
                "lean_refs": [ref],
                "rationale": "Not authorized",
            },
        )
    assert record == before


def test_default_mapping_id_collision_cannot_strand_a_bad_revision() -> None:
    record = _record()
    record["units"][1]["id"] = "unit-a"
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="duplicate"):
        apply_action(
            record,
            {
                "type": "set_mapping",
                "source_ids": ["a", "b"],
                "lean_refs": [{"imports": ["Init"], "name": "True"}],
                "rationale": "Replace shared unit",
            },
        )
    assert record == before


def test_redirecting_dependencies_rejects_new_cycle_before_mutation() -> None:
    record = _record()
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="cycle"):
        apply_action(
            record,
            {
                "type": "set_mapping",
                "source_ids": ["a", "b"],
                "unit_id": "new-shared",
                "depends_on_units": ["other"],
                "lean_refs": [{"imports": ["Init"], "name": "True"}],
                "rationale": "Would redirect other back to its prerequisite",
            },
        )
    assert record == before
