"""Lean companion-module policy tests."""

from leanflow_cli.native import companion_module_policy


def test_small_active_file_has_no_companion_advice(tmp_path, monkeypatch):
    active = tmp_path / "IMO2026" / "P4.lean"
    active.parent.mkdir()
    active.write_text("import Mathlib\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_LINE_THRESHOLD", "10")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_BYTE_THRESHOLD", "1000")

    assert (
        companion_module_policy.companion_module_advice(
            str(active),
            project_root=str(tmp_path),
        )
        == ""
    )


def test_large_active_file_names_dependency_safe_companion(tmp_path, monkeypatch):
    active = tmp_path / "IMO2026" / "P4.lean"
    active.parent.mkdir()
    active.write_text("\n".join(f"lemma h{i} : True := by trivial" for i in range(12)))
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_LINE_THRESHOLD", "10")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_BYTE_THRESHOLD", "100000")

    advice = companion_module_policy.companion_module_advice(
        str(active),
        project_root=str(tmp_path),
    )

    assert "IMO2026/P4Helpers.lean" in advice
    assert "`IMO2026.P4Helpers`" in advice
    assert "private declarations cannot be imported" in advice
    assert "verify the imported active module" in advice
    assert "mandatory placement decision" in advice
    assert "companion status: missing" in advice
    assert "active import status: missing" in advice
    assert "import IMO2026.P4Helpers" in advice


def test_existing_import_is_reported(tmp_path, monkeypatch):
    active = tmp_path / "IMO2026" / "P4.lean"
    active.parent.mkdir()
    active.write_text(
        "import IMO2026.P4Helpers\n"
        + "\n".join(f"lemma h{i} : True := by trivial" for i in range(12)),
        encoding="utf-8",
    )
    companion = active.with_name("P4Helpers.lean")
    companion.write_text("import Mathlib\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_LINE_THRESHOLD", "10")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_BYTE_THRESHOLD", "100000")

    advice = companion_module_policy.companion_module_advice(
        str(active),
        project_root=str(tmp_path),
    )

    assert "companion status: exists" in advice
    assert "active import status: present" in advice


def test_reverse_import_companion_is_flagged_as_stale_source_risk(tmp_path, monkeypatch):
    active = tmp_path / "IMO2026" / "P4.lean"
    active.parent.mkdir()
    active.write_text("\n".join(f"lemma h{i} : True := by trivial" for i in range(12)))
    companion = active.with_name("P4Helpers.lean")
    companion.write_text("import IMO2026.P4\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_LINE_THRESHOLD", "10")
    monkeypatch.setenv("LEANFLOW_COMPANION_MODULE_BYTE_THRESHOLD", "100000")

    advice = companion_module_policy.companion_module_advice(
        str(active),
        project_root=str(tmp_path),
    )

    assert "unsafe reverse import detected" in advice
    assert "stale compiled active module" in advice


def test_imports_active_module_uses_project_relative_name(tmp_path):
    active = tmp_path / "IMO2026" / "P4.lean"
    active.parent.mkdir()

    assert companion_module_policy.imports_active_module(
        "import Mathlib\nimport IMO2026.P4\n",
        str(active),
        project_root=str(tmp_path),
    )
    assert not companion_module_policy.imports_active_module(
        "import Mathlib\n",
        str(active),
        project_root=str(tmp_path),
    )
