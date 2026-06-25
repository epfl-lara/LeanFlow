"""Tests for the extracted native_config env readers + native_runner re-export (Phase 2)."""

from leanflow_cli.native import native_config, native_runner


def test_native_runner_reexports_are_identical():
    for name in native_config.__all__:
        assert getattr(native_runner, name) is getattr(native_config, name), name


def test_read_native_env(monkeypatch):
    monkeypatch.delenv("LEANFLOW_NATIVE_FOO", raising=False)
    assert native_config._read_native_env("FOO", "dflt") == "dflt"
    monkeypatch.setenv("LEANFLOW_NATIVE_FOO", "primary")
    assert native_config._read_native_env("FOO", "dflt") == "primary"


def test_read_int_env_clamps_and_defaults(monkeypatch):
    monkeypatch.delenv("X_INT", raising=False)
    assert native_config._read_int_env("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "not-an-int")
    assert native_config._read_int_env("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "0")
    assert native_config._read_int_env("X_INT", 7, minimum=2) == 2


def test_workflow_kind_lowercases(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "  PROVE ")
    assert native_config._workflow_kind() == "prove"
