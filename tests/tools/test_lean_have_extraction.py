"""Test transactional local-have extraction tool behavior."""

import json

from core import verified_edit_authority
from tools.implementations import lean_have_extraction as extraction

SOURCE = """import Mathlib

theorem demo (a b : Nat) (h : a = b) : a + 3 = b + 3 := by
  have hstep : a + 1 = b + 1 := by
    calc
      a + 1 = b + 1 := by omega
      _ = b + 1 := rfl
      _ = b + 1 := by rfl
      _ = b + 1 := by omega
      _ = b + 1 := rfl
      _ = b + 1 := by rfl
  omega
"""


def _successful_checks():
    """Return the three LeanProbe payloads used by a successful extraction."""
    return iter(
        [
            {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "timed_out": False,
                "messages": [
                    {
                        "severity": "info",
                        "message": (
                            "theorem leanflow_demo_hstep (a b : ℕ) (h : a = b) : "
                            "a + 1 = b + 1 := sorry"
                        ),
                    }
                ],
            },
            {
                "success": True,
                "ok": True,
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
                "timed_out": False,
                "axiom_profile_checked": True,
                "axiom_profile_axioms": ["propext"],
                "axiom_profile_blockers": [],
            },
            {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "timed_out": False,
            },
        ]
    )


def test_tool_verifies_helper_and_switch_before_applying(monkeypatch, tmp_path):
    """Commit only after independent helper and rewritten-prefix checks pass."""
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    checks = _successful_checks()
    captured = {}
    verified_edit_authority.clear_for_tests()
    monkeypatch.setattr(extraction, "lean_incremental_check", lambda **kwargs: next(checks))

    def fake_apply(path, patch, **kwargs):
        captured.update({"path": path, "patch": patch, **kwargs})
        return json.dumps(
            {
                "success": True,
                "status": "patch_elaborated",
                "check_passed": True,
                "patch_applied": True,
            }
        )

    monkeypatch.setattr(extraction, "apply_verified_patch_tool", fake_apply)

    payload = json.loads(
        extraction.lean_extract_have_tool(
            "demo",
            str(target),
            cwd=str(tmp_path),
            timeout_s=30,
        )
    )

    assert payload["success"] is True
    assert payload["extraction"]["have_name"] == "hstep"
    assert "private lemma leanflow_demo_hstep" in captured["patch"]
    assert "solve_by_elim [leanflow_demo_hstep]" in captured["patch"]
    assert captured["theorem_id"] == "demo"
    assert captured["verified_edit_authority_token"]


def test_tool_leaves_source_unchanged_when_switch_does_not_elaborate(monkeypatch, tmp_path):
    """Reject an extracted call-site failure before invoking the patch transaction."""
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    checks = _successful_checks()
    third = next(checks), next(checks)
    responses = iter([*third, {"success": False, "has_errors": True, "timed_out": False}])
    monkeypatch.setattr(extraction, "lean_incremental_check", lambda **kwargs: next(responses))
    monkeypatch.setattr(
        extraction,
        "apply_verified_patch_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("patch must not run")),
    )

    payload = json.loads(extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path)))

    assert payload["status"] == "helper_switch_failed"
    assert target.read_text(encoding="utf-8") == SOURCE
