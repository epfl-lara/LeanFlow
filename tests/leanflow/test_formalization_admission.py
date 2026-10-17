"""Prevent literal masking and machine-local settings from weakening durable acceptance."""

from pathlib import Path

import pytest

from leanflow_cli.formalization import environment
from leanflow_cli.formalization.lean_layout import _static_configuration
from leanflow_cli.formalization.lean_policy import validate_source
from leanflow_cli.workflows.prover.source import lean_code_mask, sorry_spans


@pytest.mark.parametrize(
    "source",
    [
        "def q : Char := '\"'\nrun_cmd pure ()\ndef r : Char := '\"'",
        'def q := s!"{(by run_tac pure ())}"',
        "run_meta pure ()",
        "simproc test (_) := fun _ => pure .continue",
        "set_option maxHeartbeats 0 in\ntheorem t : True := by trivial",
        "set_option debug.skipKernelTC true in\ndef t := 1",
    ],
)
def test_effectful_admission_cannot_hide_inside_literals(source):
    with pytest.raises(ValueError):
        validate_source(source)


def test_literals_do_not_hide_later_holes_or_reject_ordinary_eval_names():
    source = 'def c : Char := \'"\'\ndef s := r#"sorry \\" "#\ntheorem t : True := by sorry'
    spans = sorry_spans(source)
    assert len(spans) == 1 and spans[0][0] == source.rfind("sorry")
    assert len(lean_code_mask(source)) == len(source)
    validate_source("def f := Polynomial.eval\nset_option maxHeartbeats 10")


@pytest.mark.parametrize("field", ["plugins", "dynlibs"])
def test_untracked_lake_plugins_are_not_static_inputs(tmp_path, field):
    path = tmp_path / "lakefile.toml"
    with pytest.raises(ValueError, match="Unsupported dynamic Lake setting"):
        _static_configuration(path, f'name = "demo"\n{field} = ["hidden"]\n')


def compiler_fixture(path: Path) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "lib" / "lean").mkdir(parents=True)
    (path / "bin" / "lean").write_bytes(b"same compiler")
    (path / "lib" / "lean" / "Init.olean").write_bytes(b"same library")
    return path


def test_environment_identity_uses_roles_and_content_not_machine_settings(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
    first = compiler_fixture(tmp_path / "machine-a" / "compiler")
    second = compiler_fixture(tmp_path / "machine-b" / "compiler")
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.setattr(environment, "_compiler_root", lambda root: first)
    before = environment.environment_identity(root)
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "unrelated-elan-settings"))
    monkeypatch.setattr(environment, "_compiler_root", lambda root: second)
    assert environment.environment_identity(root) == before
    (second / "lib" / "lean" / "Init.olean").write_bytes(b"changed library")
    assert environment.environment_identity(root) != before
