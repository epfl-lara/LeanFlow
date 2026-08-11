"""Tests for source-bound reuse of parent-banked helper verification."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.native import banked_helper_inspection


def _verification() -> dict[str, object]:
    """Return one accepted exact helper verification record."""
    return {
        "ok": True,
        "errors": 0,
        "sorry": 0,
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [],
        "tool": "lean_incremental_check",
    }


def test_exact_unchanged_banked_helper_reuses_parent_gate_without_lean(tmp_path):
    """Replace a redundant symbol inspection with its stronger exact gate."""
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma banked : True := by trivial\n\ntheorem target : True := by sorry\n",
        encoding="utf-8",
    )
    agent = SimpleNamespace()
    assert banked_helper_inspection.remember(
        agent,
        active_file=str(active),
        helper_verifications={"banked": _verification()},
        project_root=str(tmp_path),
    ) == ("banked",)

    reused = banked_helper_inspection.reused_lean_inspection(
        agent,
        "lean_inspect",
        {"target": str(active), "symbol": "banked"},
        project_root=str(tmp_path),
    )

    assert reused is not None
    assert reused["success"] is True
    assert reused["status"] == "parent_kernel_verification_reused"
    assert reused["lean_started"] is False
    assert reused["valid_without_sorry"] is True
    assert reused["axiom_profile_checked"] is True


def test_source_change_invalidates_banked_helper_inspection_reuse(tmp_path):
    """Never reuse helper authority after any source-revision change."""
    active = tmp_path / "Main.lean"
    active.write_text("lemma banked : True := by trivial\n", encoding="utf-8")
    agent = SimpleNamespace()
    banked_helper_inspection.remember(
        agent,
        active_file=str(active),
        helper_verifications={"banked": _verification()},
        project_root=str(tmp_path),
    )
    active.write_text(
        "lemma banked : True := by trivial\nlemma later : True := by trivial\n",
        encoding="utf-8",
    )

    assert (
        banked_helper_inspection.reused_lean_inspection(
            agent,
            "lean_inspect",
            {"target": str(active), "symbol": "banked"},
            project_root=str(tmp_path),
        )
        is None
    )


def test_verified_helper_status_survives_unrelated_source_change(tmp_path):
    """Keep exact helper authority when only the surrounding target changes."""
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma banked : True := by trivial\n\ntheorem target : True := by sorry\n",
        encoding="utf-8",
    )
    agent = SimpleNamespace()
    banked_helper_inspection.remember(
        agent,
        active_file=str(active),
        helper_verifications={"banked": _verification()},
        project_root=str(tmp_path),
    )
    active.write_text(
        "lemma banked : True := by trivial\n\ntheorem target : True := by\n  trivial\n",
        encoding="utf-8",
    )

    assert banked_helper_inspection.current_verified_helper_names(
        agent,
        active_file=str(active),
        project_root=str(tmp_path),
    ) == ("banked",)


def test_file_wide_or_different_symbol_inspection_still_runs_real_lean(tmp_path):
    """Keep the reuse surface exact rather than suppressing broad diagnostics."""
    active = tmp_path / "Main.lean"
    active.write_text("lemma banked : True := by trivial\n", encoding="utf-8")
    agent = SimpleNamespace()
    banked_helper_inspection.remember(
        agent,
        active_file=str(active),
        helper_verifications={"banked": _verification()},
        project_root=str(tmp_path),
    )

    assert (
        banked_helper_inspection.reused_lean_inspection(
            agent,
            "lean_inspect",
            {"target": str(active)},
            project_root=str(tmp_path),
        )
        is None
    )
    assert (
        banked_helper_inspection.reused_lean_inspection(
            agent,
            "lean_inspect",
            {"target": str(active), "symbol": "other"},
            project_root=str(tmp_path),
        )
        is None
    )
