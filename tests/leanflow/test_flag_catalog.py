"""Contract tests for the declared ``LEANFLOW_*`` knob catalog.

The catalog is documentation that other tools trust, so the failure mode worth
guarding against is silent drift: a knob renamed in the runtime, a default
changed, or a research-profile key that never made it into the catalog. These
tests read the actual source rather than a second copy of the same list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.flags import (
    FLAG_CATALOG,
    catalog_payload,
    effective_flag_values,
    lookup_flag,
    profile_payload,
    resolve_profile,
)
from leanflow_cli.flags.resolve import diff_profiles, load_profiles, save_profile
from leanflow_cli.flags.spec import FlagProfile

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories holding runtime code the catalog documents.
_SOURCE_DIRS = ("core", "agent", "tools", "leanflow_cli")

#: Top-level modules that also read knobs.
_SOURCE_FILES = ("run_agent.py",)


def _runtime_source_text() -> str:
    """Concatenate the non-test runtime source the catalog claims to describe."""
    chunks: list[str] = []
    for directory in _SOURCE_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    for name in _SOURCE_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


@pytest.fixture(scope="module")
def runtime_source() -> str:
    return _runtime_source_text()


def test_every_entry_declares_a_valid_kind_and_value_type() -> None:
    """``Literal`` is erased at runtime, so a typo here type-checks but misrenders."""
    kinds = {"feature", "tuning", "runtime", "internal"}
    value_types = {"bool", "int", "float", "string", "enum", "path", "csv"}
    for spec in FLAG_CATALOG:
        assert spec.kind in kinds, f"{spec.name} has invalid kind {spec.kind!r}"
        assert (
            spec.value_type in value_types
        ), f"{spec.name} has invalid value_type {spec.value_type!r}"


def test_constructing_a_spec_with_a_bad_kind_is_rejected() -> None:
    from leanflow_cli.flags.spec import FlagSpec

    with pytest.raises(ValueError, match="kind must be one of"):
        FlagSpec(
            name="LEANFLOW_X",
            kind="enum",  # type: ignore[arg-type]
            value_type="enum",
            default="a",
            group="g",
            summary="s",
            choices=("a",),
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["flags", "list"],
        ["flags", "list", "--ablatable"],
        ["flags", "list", "--kind", "feature"],
        ["flags", "profiles"],
        ["flags", "diff", "default", "research"],
        ["flags", "effective", "--changed"],
    ],
)
def test_every_rendered_subcommand_runs(argv: list[str], capsys) -> None:
    """Rendering must not be able to abort the command."""
    from leanflow_cli.main import main

    assert main(argv) == 0
    assert capsys.readouterr().out


def test_show_renders_every_catalogued_knob(capsys) -> None:
    """One bad style lookup used to raise on a whole class of knobs."""
    from leanflow_cli.main import main

    for spec in FLAG_CATALOG:
        assert main(["flags", "show", spec.name]) == 0, f"rendering {spec.name} failed"
    assert capsys.readouterr().out


def test_catalog_names_are_unique() -> None:
    names = [spec.name for spec in FLAG_CATALOG]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate catalog entries: {duplicates}"


def test_every_catalogued_knob_is_read_by_the_runtime(runtime_source: str) -> None:
    """A catalogued name the runtime never mentions is a typo or a dead entry."""
    missing = [spec.name for spec in FLAG_CATALOG if spec.name not in runtime_source]
    assert not missing, (
        "catalogued knobs that no runtime module reads (renamed or removed?): " f"{sorted(missing)}"
    )


def test_research_profile_keys_are_catalogued() -> None:
    """--research must not set a knob the catalog cannot explain."""
    from leanflow_cli.workflows.research_mode import research_profile_env

    uncatalogued = sorted(name for name in research_profile_env() if lookup_flag(name) is None)
    assert not uncatalogued, f"research profile sets uncatalogued knobs: {uncatalogued}"


def test_enum_defaults_are_valid_choices() -> None:
    for spec in FLAG_CATALOG:
        if spec.value_type != "enum":
            continue
        assert (
            spec.default in spec.choices
        ), f"{spec.name} default {spec.default!r} is not among {spec.choices}"


def test_numeric_defaults_parse_and_respect_declared_bounds() -> None:
    for spec in FLAG_CATALOG:
        if spec.value_type not in {"int", "float"}:
            continue
        if not spec.default.strip():
            # An empty default means "unset"; the reading module supplies its own.
            continue
        value = spec.coerce(spec.default)
        assert value is not None, f"{spec.name} default {spec.default!r} does not parse"
        if spec.minimum is not None:
            assert value >= spec.minimum, f"{spec.name} default is below its declared minimum"
        if spec.maximum is not None:
            assert value <= spec.maximum, f"{spec.name} default is above its declared maximum"


def test_bool_defaults_are_recognizable() -> None:
    recognized = {"0", "1", "true", "false", "yes", "no", "on", "off", ""}
    for spec in FLAG_CATALOG:
        if spec.value_type != "bool":
            continue
        assert (
            spec.default.strip().lower() in recognized
        ), f"{spec.name} bool default {spec.default!r} is not a recognizable boolean"


def test_catalog_payload_is_json_serializable() -> None:
    payload = catalog_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert json.loads(encoded)["count"] == len(FLAG_CATALOG)
    covered = sum(len(group["flags"]) for group in payload["groups"])
    assert covered == len(FLAG_CATALOG), "grouping dropped or duplicated catalog entries"


def test_internal_knobs_are_not_offered_as_editable() -> None:
    for spec in FLAG_CATALOG:
        if spec.kind == "internal":
            assert not spec.editable
            assert not spec.extension_editable
            assert not spec.ablatable


def test_sensitive_knobs_are_terminal_only() -> None:
    """The editor host must not expose credential or authority boundaries."""
    expected = {
        "LEANFLOW_PLAN_STATE_DIR",
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "LEANFLOW_REDACT_SECRETS",
        "LEANFLOW_DUMP_REQUESTS",
        "LEANFLOW_DUMP_REQUEST_STDOUT",
        "LEANFLOW_YOLO_MODE",
        "LEANFLOW_HOME",
        "LEANFLOW_SANDBOX_BASE_IMAGE",
        "LEANFLOW_PROVER_ALLOWED_AXIOMS",
        "LEANFLOW_PROVER_RESUME_RUN_ID",
    }
    actual = {spec.name for spec in FLAG_CATALOG if spec.sensitive}
    assert actual == expected
    for spec in FLAG_CATALOG:
        assert spec.extension_editable == (spec.editable and not spec.sensitive)
        payload = spec.to_payload()
        assert payload["sensitive"] is spec.sensitive
        assert payload["extension_editable"] is spec.extension_editable


def test_ablatable_knobs_exist_for_every_major_subsystem() -> None:
    """An ablation matrix is only useful if the interesting knobs are marked."""
    groups = {spec.group for spec in FLAG_CATALOG if spec.ablatable}
    for expected in ("Research campaign", "Orchestration", "Planner", "Queue and verification"):
        assert expected in groups, f"no ablatable knob in {expected}"


def test_environment_beats_profile_in_effective_resolution(monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "0")
    profile = FlagProfile(name="t", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "1"})
    rows = {row["name"]: row for row in effective_flag_values(profile=profile)}
    row = rows["LEANFLOW_NEGATION_PROBE"]
    assert row["source"] == "environment"
    assert row["value"] is False


def test_profile_value_is_used_when_environment_is_silent(monkeypatch) -> None:
    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    profile = FlagProfile(name="t", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "1"})
    rows = {row["name"]: row for row in effective_flag_values(profile=profile)}
    row = rows["LEANFLOW_NEGATION_PROBE"]
    assert row["source"] == "profile"
    assert row["value"] is True
    assert row["is_default"] is False


def test_effective_values_hide_internal_plumbing_by_default() -> None:
    visible = {row["name"] for row in effective_flag_values(env={})}
    assert "LEANFLOW_PROJECT_ROOT" not in visible
    with_internal = {row["name"] for row in effective_flag_values(env={}, include_internal=True)}
    assert "LEANFLOW_PROJECT_ROOT" in with_internal


def test_builtin_research_profile_tracks_the_runtime() -> None:
    from leanflow_cli.workflows.research_mode import research_profile_env

    profile = resolve_profile("research")
    for name, value in research_profile_env().items():
        assert profile.overrides[name] == value
    assert profile.overrides["LEANFLOW_RESEARCH_MODE"] == "1"


def test_diff_ignores_knobs_that_resolve_to_the_same_value() -> None:
    left = FlagProfile(name="l", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "1"})
    right = FlagProfile(name="r", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "true"})
    assert diff_profiles(left, right) == []


def test_diff_reports_a_real_difference() -> None:
    left = FlagProfile(name="l", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "1"})
    right = FlagProfile(name="r", summary="", overrides={"LEANFLOW_NEGATION_PROBE": "0"})
    (row,) = diff_profiles(left, right)
    assert row["name"] == "LEANFLOW_NEGATION_PROBE"
    assert row["known"] is True


def test_saving_a_profile_rejects_unknown_knobs(tmp_path: Path) -> None:
    profile = FlagProfile(name="bad", summary="", overrides={"LEANFLOW_NOT_A_REAL_KNOB": "1"})
    with pytest.raises(ValueError, match="unknown flags"):
        save_profile(profile, project_root=tmp_path)


def test_saved_profile_round_trips_and_shadows_nothing_unexpected(tmp_path: Path) -> None:
    (tmp_path / ".leanflow").mkdir(parents=True, exist_ok=True)
    profile = FlagProfile(
        name="ablate-negation",
        summary="research minus the negation probe",
        overrides={"LEANFLOW_RESEARCH_MODE": "1", "LEANFLOW_NEGATION_PROBE": "0"},
    )
    path = save_profile(profile, project_root=tmp_path)
    assert path.is_file()

    loaded = load_profiles(tmp_path)
    assert "ablate-negation" in loaded
    assert loaded["ablate-negation"].overrides == profile.overrides
    assert loaded["ablate-negation"].builtin is False
    # Built-ins remain available alongside project-local profiles.
    assert "research" in loaded and loaded["research"].builtin is True


def test_profile_payload_lists_search_paths(tmp_path: Path) -> None:
    payload = profile_payload(tmp_path)
    assert payload["search_paths"][0].endswith("flag-profiles")
    assert payload["count"] >= 3


@pytest.mark.parametrize(
    "name",
    ["../escape", "a/b", "..", ".", "", "with space", "x" * 65, "/abs", "sub\\dir"],
)
def test_profile_names_that_could_escape_the_directory_are_rejected(
    name: str, tmp_path: Path
) -> None:
    """The name becomes a filename, so traversal must fail before any write."""
    profile = FlagProfile(name=name, summary="", overrides={"LEANFLOW_RESEARCH_MODE": "1"})
    with pytest.raises(ValueError, match="invalid profile name"):
        save_profile(profile, project_root=tmp_path)


def test_saved_profile_records_the_validated_name(tmp_path: Path) -> None:
    profile = FlagProfile(
        name="  ablate-negation  ",
        summary="",
        overrides={"LEANFLOW_NEGATION_PROBE": "0"},
    )
    path = save_profile(profile, project_root=tmp_path)
    assert path.name == "ablate-negation.json"
    assert json.loads(path.read_text())["name"] == "ablate-negation"
