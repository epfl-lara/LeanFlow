"""Exercise actual Lake templates and preserve compiler options through private builds."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.formalization.environment import compiler_executable
from leanflow_cli.formalization.lean_build import build_project
from leanflow_cli.formalization.lean_configuration import static_configuration, validate_options
from leanflow_cli.formalization.lean_layout import inspect_layout


@pytest.mark.parametrize(
    "option,value",
    [
        ("autoImplicit", "true"),
        ("relaxedAutoImplicit", "false"),
        ("pp.unicode.fun", "true"),
        ("weak.linter.mathlibStandardSet", "true"),
        ("maxSynthPendingDepth", "3"),
    ],
)
def test_static_template_options_are_accepted_in_both_config_languages(
    tmp_path: Path, option: str, value: str
) -> None:
    static_configuration(
        tmp_path / "lakefile.toml", f'name="paper"\n[leanOptions]\n{option}={value}\n'
    )
    static_configuration(
        tmp_path / "lakefile.lean",
        f'import Lake\nopen Lake DSL\npackage "paper" where\n  version := v!"0.1.0"\n  keywords := #["math"]\n  leanOptions := #[\n    ⟨`{option}, {value}⟩,\n  ]\nrequire "leanprover-community" / "mathlib" @ git "v4.34.1"\n@[default_target]\nlean_lib Paper where\n',
    )


@pytest.mark.parametrize(
    "options",
    [
        {"autoImplicit": 1},
        {"maxSynthPendingDepth": True},
        {"maxHeartbeats": -1},
        {"trustLevel": 0},
        {"Elab.async": True},
    ],
)
def test_unreviewed_options_and_wrong_types_fail_closed(options: dict) -> None:
    with pytest.raises(ValueError, match="Unsupported Lean compiler option"):
        validate_options(options)


@pytest.mark.parametrize(
    "setting",
    [
        "leanOptions := arbitraryCall",
        "leanOptions := #[⟨`autoImplicit, unsafeBool⟩]",
        "version := computeVersion",
        "keywords := fetchKeywords",
        'require "owner" / "dep" @ git (getRevision ())',
        'initialize IO.println "side effect"',
    ],
)
def test_static_dsl_rejects_expressions_disguised_as_template_fields(
    tmp_path: Path, setting: str
) -> None:
    with pytest.raises(ValueError, match="Unsupported|literal"):
        static_configuration(
            tmp_path / "lakefile.lean",
            "import Lake\nopen Lake DSL\npackage paper where\n  " + setting,
        )


@pytest.fixture
def pinned_project(tmp_path: Path) -> Path:
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned compiler integration")
    (tmp_path / "lean-toolchain").write_text(toolchain + "\n")
    return tmp_path


@pytest.mark.integration
@pytest.mark.parametrize(
    "template", ["math.toml", "math.lean", "math-lax.toml", "math-lax.lean", "std.toml", "std.lean"]
)
def test_real_offline_lake_new_templates_have_supported_layouts(
    pinned_project: Path, template: str
) -> None:
    result = subprocess.run(
        [compiler_executable(pinned_project, "lake"), "--offline", "new", "paper", template],
        cwd=pinned_project,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    layout = inspect_layout(pinned_project / "paper")
    assert layout["accepted"], layout
    options = layout["libraries"][0]["lean_options"]
    if template.startswith("math."):
        assert options["relaxedAutoImplicit"] is False
        assert options["maxSynthPendingDepth"] == 3
        assert options["weak.linter.mathlibStandardSet"] is True
    if template.startswith("std."):
        assert layout["executables"][0]["root"] == "Main"
        first = build_project(pinned_project / "paper", layout, pinned_project / "first")
        assert first["success"], first
        second = build_project(pinned_project / "paper", layout, pinned_project / "second")
        assert second["success"], second
        assert first["local_artifacts"] == second["local_artifacts"]
        assert set(first["modules"]) == {"Paper.Basic", "Paper", "Main"}


@pytest.mark.integration
def test_real_direct_compiler_observes_auto_implicit_setting(pinned_project: Path) -> None:
    config = pinned_project / "lakefile.toml"
    config.write_text(
        'name="paper"\n[leanOptions]\nautoImplicit=false\n[[lean_lib]]\nname="Paper"\n'
    )
    (pinned_project / "Paper.lean").write_text("def identity (x : α) := x\n")
    rejected = build_project(
        pinned_project, inspect_layout(pinned_project), pinned_project / "check"
    )
    assert not rejected["success"]
    assert "Unknown identifier" in rejected.get("stdout", "") + rejected.get(
        "stderr", ""
    ) or "unknown identifier" in rejected.get("stdout", "") + rejected.get("stderr", "")
    config.write_text(config.read_text().replace("autoImplicit=false", "autoImplicit=true"))
    accepted = build_project(
        pinned_project, inspect_layout(pinned_project), pinned_project / "check"
    )
    assert accepted["success"], accepted


@pytest.mark.integration
def test_compiled_dependency_tactics_and_equations_have_identical_rebuild_artifacts(
    pinned_project: Path,
) -> None:
    """Bind real dependency bytes and compare draft-closure and whole-build artifacts."""
    import json

    package = pinned_project / ".lake/packages/fixture"
    library = package / ".lake/build/lib/lean"
    library.mkdir(parents=True)
    (package / "lakefile.toml").write_text('name="fixture"\n[[lean_lib]]\nname="Fixture"\n')
    source = package / "Fixture.lean"
    source.write_text(
        "namespace Fixture\ndef increment (n : Nat) : Nat := n + 1\n@[simp] theorem increment_zero : increment 0 = 1 := rfl\nend Fixture\n"
    )
    compiled = subprocess.run(
        [
            compiler_executable(pinned_project, "lean"),
            "-o",
            str(library / "Fixture.olean"),
            str(source),
        ],
        cwd=package,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    (pinned_project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "fixture",
                        "type": "git",
                        "rev": "0123456789abcdef0123456789abcdef01234567",
                    }
                ]
            }
        )
    )
    (pinned_project / "lakefile.toml").write_text('name="paper"\n[[lean_lib]]\nname="Paper"\n')
    (pinned_project / "Paper").mkdir()
    (pinned_project / "Paper/Defs.lean").write_text(
        "import Fixture\nnamespace Paper\ndef f : Nat → Nat\n  | 0 => 0\n  | n+1 => Fixture.increment (f n)\nend Paper\n"
    )
    (pinned_project / "Paper/Results.lean").write_text(
        "import Paper.Defs\nnamespace Paper\ntheorem f_eq (n : Nat) : f n = n := by\n  induction n with\n  | zero => rfl\n  | succ n ih => simp [f, Fixture.increment, ih]\ntheorem f_step (n : Nat) : f (n+1) = n+1 := by simp [f, f_eq, Fixture.increment]\nend Paper\n"
    )
    (pinned_project / "Paper.lean").write_text("import Paper.Results\n")
    layout = inspect_layout(pinned_project)
    assert layout["accepted"], layout
    draft = build_project(
        pinned_project, layout, pinned_project / "draft", files=["Paper/Results.lean"]
    )
    assert draft["success"], draft
    whole = build_project(pinned_project, layout, pinned_project / "whole")
    assert whole["success"], whole
    assert set(draft["modules"]) == {"Paper.Defs", "Paper.Results"}
    assert all(
        whole["local_artifacts"][name] == record
        for name, record in draft["local_artifacts"].items()
    )
