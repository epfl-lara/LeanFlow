"""Preserve executable defaults and package-relative source directories when adding formalization libraries."""

import json
import os
import subprocess
import tomllib

import pytest

from leanflow_cli.formalization.lean_layout import resolve_layout
from leanflow_cli.formalization.preparation_inputs import read_configuration
from leanflow_cli.formalization.preparation_layout import configure_layout
from leanflow_cli.formalization.preparation_plan import default_preparation_plan
from leanflow_cli.formalization.records import create_document


@pytest.mark.parametrize(
    "targets",
    [
        "",
        'defaultTargets = ["app"] # existing executable\n',
        'defaultTargets = [\n "app" # preserve this comment with ] in it\n]\n',
        "'defaultTargets' = [\n 'app', # closing brackets ] are comments\n]\n",
    ],
)
def test_dedicated_toml_library_appends_default_without_rewriting_other_fields(tmp_path, targets):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True holds.\end{theorem}" + "\n")
    prefix = (
        'name = "app"\nsrcDir = "src"\n'
        'keywords = ["literal [table] # text", "defaultTargets = [ignored]"]\n'
    )
    executable = '\n[[lean_exe]]\nname = "app"\nroot = "Main"\n'
    original = prefix + targets + executable
    (tmp_path / "lakefile.toml").write_text(original)
    old = tomllib.loads(original)
    record = create_document(tmp_path, source)
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    baseline = {}
    configure_layout(tmp_path, tmp_path, plan, baseline)
    rewritten = (tmp_path / "lakefile.toml").read_text()
    parsed = tomllib.loads(rewritten)
    assert parsed["defaultTargets"] == [*old.get("defaultTargets", []), "Formalization"]
    assert parsed["lean_exe"] == old["lean_exe"]
    assert parsed["keywords"] == old["keywords"]
    assert rewritten.startswith(prefix) and executable in rewritten
    assert parsed["lean_lib"] == [
        {"name": "Formalization", "srcDir": ".", "roots": ["Formalization"]}
    ]
    assert (tmp_path / "src/Formalization.lean").read_text() == "import Init\n"
    assert not (tmp_path / "src/src").exists()
    assert baseline == {"src/Formalization.lean": None}
    if "# preserve" in targets:
        assert "# preserve this comment with ] in it" in rewritten


def test_dedicated_lean_dsl_library_retains_existing_default_attribute(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True holds.\end{theorem}" + "\n")
    original = (
        "import Lake\nopen Lake DSL\npackage app where\n"
        '  srcDir := "src"\n'
        "@[default_target]\nlean_exe app where\n  root := \x60Main\n"
    )
    (tmp_path / "lakefile.lean").write_text(original)
    record = create_document(tmp_path, source)
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    configure_layout(tmp_path, tmp_path, plan, {})
    assert (tmp_path / "lakefile.lean").read_text().startswith(original)
    configured = read_configuration(tmp_path)
    assert configured["defaultTargets"] == ["app", "Formalization"]
    assert configured["srcDir"] == "src"
    assert configured["lean_lib"][0]["srcDir"] == "."
    assert (tmp_path / "src/Formalization.lean").is_file()


@pytest.mark.integration
@pytest.mark.parametrize("configuration", ["toml", "lean"])
def test_real_executable_default_build_includes_new_package_relative_library(
    tmp_path, configuration
):
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned compiler integration")
    (tmp_path / "lean-toolchain").write_text(toolchain + "\n")
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True holds.\end{theorem}" + "\n")
    if configuration == "toml":
        (tmp_path / "lakefile.toml").write_text(
            'name = "app"\nsrcDir = "src"\ndefaultTargets = [\n "app", # keep executable\n]\n'
            '[[lean_exe]]\nname = "app"\nroot = "Main"\n'
        )
    else:
        (tmp_path / "lakefile.lean").write_text(
            "import Lake\nopen Lake DSL\npackage app where\n"
            '  srcDir := "src"\n'
            "@[default_target]\nlean_exe app where\n  root := \x60Main\n"
        )
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "packages": [],
                "name": "app",
                "lakeDir": ".lake",
            }
        )
    )
    (tmp_path / "src").mkdir()
    main = tmp_path / "src/Main.lean"
    main.write_text("def main : IO Unit := pure ()\n")
    original_main = main.read_bytes()
    record = create_document(tmp_path, source)
    plan = default_preparation_plan(tmp_path, record, preset="lean")
    configure_layout(tmp_path, tmp_path, plan, {})
    layout = resolve_layout(tmp_path, record["document_id"])
    assert layout["accepted"], layout
    assert layout["root_file"] == "src/Formalization.lean"
    assert layout["default_targets"] == ["app", "Formalization"]
    built = subprocess.run(
        ["lake", "build"], cwd=tmp_path, text=True, capture_output=True, timeout=90
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assert (tmp_path / ".lake/build/lib/lean/Formalization.olean").is_file()
    assert (tmp_path / ".lake/build/bin/app").is_file()
    assert main.read_bytes() == original_main
