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


def test_private_helper_freshens_extract_goal_universe_binders():
    """Avoid redeclaring generated universe names from the active file scope."""
    candidate = extraction.HaveCandidate(
        name="hstep",
        header="  have hstep : True := by",
        proof="    trivial",
        source="  have hstep : True := by\n    trivial",
        start=0,
        end=43,
        indent="  ",
        line_count=2,
    )
    statement = "theorem extracted.{u_2, u_1} {V : Type u_1} {P : Type u_2} : True := sorry"

    helper = extraction._private_helper(statement, candidate)

    match = extraction.re.search(r"private lemma extracted\.\{([^,]+), ([^}]+)\}", helper)
    assert match is not None
    first, second = match.groups()
    assert first.startswith("leanflow_u_")
    assert second.startswith("leanflow_u_")
    assert first != second
    assert f"P : Type {first}" in helper
    assert f"V : Type {second}" in helper
    assert ".{u_2, u_1}" not in helper


def test_private_helper_recreates_result_level_let_for_original_proof():
    """Keep local let names available after ``extract_goal`` reverts context."""
    candidate = extraction.HaveCandidate(
        name="hstep",
        header="  have hstep : x = n + 1 := by",
        proof="    simpa [x]",
        source="  have hstep : x = n + 1 := by\n    simpa [x]",
        start=0,
        end=49,
        indent="  ",
        line_count=2,
    )
    statement = "theorem extracted (n : Nat) :\n" "  let x := n + 1;\n" "  x = n + 1 := sorry"

    helper = extraction._private_helper(statement, candidate)

    assert ":= by\n  let x := n + 1" in helper
    assert "\n  change x = n + 1\n" in helper
    assert helper.endswith("  simpa [x]")


def test_private_helper_reuses_typed_source_let_declaration():
    """Preserve a local let's expected type when its value is ambiguous alone."""
    candidate = extraction.HaveCandidate(
        name="hstep",
        header="  have hstep : Box x := by",
        proof="    simpa [x]",
        source="  have hstep : Box x := by\n    simpa [x]",
        start=0,
        end=43,
        indent="  ",
        line_count=2,
    )
    statement = "theorem extracted (a : Nat) :\n" "  let x := { value := a };\n" "  Box x := sorry"
    context = "  let x : Container Nat := { value := a }\n"

    helper = extraction._private_helper(
        statement,
        candidate,
        context_prefix=context,
    )

    assert "\n  let x : Container Nat := { value := a }\n" in helper
    assert "\n  change Box x\n" in helper


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


def test_inventory_is_comment_safe_and_provider_free(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        """import Mathlib

theorem demo : True := by
  /- have historical : True := by
    trivial
  -/
  have active : True := by
    trivial
    trivial
  exact active
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        extraction,
        "lean_incremental_check",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("inventory must not start Lean")),
    )

    payload = json.loads(
        extraction.lean_extract_have_tool(
            "demo",
            str(target),
            cwd=str(tmp_path),
            action="inventory",
            minimum_lines=2,
        )
    )

    assert payload["success"] is True
    assert [item["have_name"] for item in payload["candidates"]] == ["active"]
    assert payload["candidates"][0]["suggested_helper_name"] == "leanflow_demo_active"


def test_tool_extracts_named_batch_with_semantic_names_in_one_patch(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        """import Mathlib

theorem demo (a : Nat) : a = a := by
  have first : a = a := by
    rfl
    rfl
  have second : a = a := by
    exact first
    exact first
  exact second
""",
        encoding="utf-8",
    )
    captured = {}
    verified_edit_authority.clear_for_tests()

    def fake_check(**kwargs):
        replacement = kwargs.get("replacement", "")
        match = extraction.re.search(r"extract_goal using ([A-Za-z0-9_]+)", replacement)
        if match:
            helper_name = match.group(1)
            return {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "timed_out": False,
                "messages": [
                    {
                        "severity": "info",
                        "message": f"theorem {helper_name} (a : ℕ) : a = a := sorry",
                    }
                ],
            }
        if kwargs.get("action") == "check_helper":
            return {
                "success": True,
                "ok": True,
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
                "timed_out": False,
                "axiom_profile_checked": True,
                "axiom_profile_axioms": ["propext"],
                "axiom_profile_blockers": [],
            }
        return {
            "success": True,
            "ok": False,
            "has_errors": False,
            "has_sorry": True,
            "timed_out": False,
        }

    monkeypatch.setattr(extraction, "lean_incremental_check", fake_check)

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
            have_names=["first", "second"],
            helper_names={"first": "demo_reflexive_base", "second": "demo_reflexive_finish"},
            minimum_lines=2,
        )
    )

    assert payload["success"] is True
    assert payload["extraction"]["helper_count"] == 2
    assert payload["extraction"]["transactional_batch"] is True
    assert "private lemma demo_reflexive_base" in captured["patch"]
    assert "private lemma demo_reflexive_finish" in captured["patch"]
    assert "solve_by_elim [demo_reflexive_base]" in captured["patch"]
    assert "solve_by_elim [demo_reflexive_finish]" in captured["patch"]
