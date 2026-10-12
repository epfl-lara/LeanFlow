"""Test transactional local-have extraction tool behavior."""

import json
import re

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

OK_HELPER = {
    "success": True,
    "ok": True,
    "valid_without_sorry": True,
    "has_errors": False,
    "has_sorry": False,
    "timed_out": False,
}
OK_PROFILED_HELPER = {
    **OK_HELPER,
    "axiom_profile_checked": True,
    "axiom_profile_axioms": ["propext"],
    "axiom_profile_blockers": [],
}
SORRY_PREFIX = {
    "success": True,
    "ok": False,
    "has_errors": False,
    "has_sorry": True,
    "timed_out": False,
}
FAILED_CHECK = {"success": True, "ok": False, "has_errors": True, "timed_out": False}


def _context(entries, full=None, levels=()):
    return extraction.ExtractedContext(
        entries=tuple(extraction.ContextEntry(name, kind) for name, kind in entries),
        full=tuple(extraction.ContextEntry(name, kind) for name, kind in (full or entries)),
        levels=tuple(levels),
    )


def _candidate(header, proof, *, indent="  "):
    source = header + "\n" + proof
    return extraction.HaveCandidate(
        name="hstep",
        header=header,
        proof="\n" + proof,
        source=source,
        start=0,
        end=len(source),
        indent=indent,
        line_count=source.count("\n") + 1,
    )


def _probe_payload(statement, *, retained, full=None, levels=""):
    return {
        **SORRY_PREFIX,
        "messages": [
            {"severity": "info", "message": f"LEANFLOW_CTX_ALL {full or retained}"},
            {"severity": "info", "message": f"LEANFLOW_CTX {retained}"},
            {"severity": "info", "message": f"LEANFLOW_LEVELS {levels}".rstrip()},
            {"severity": "info", "message": statement},
        ],
    }


def _fake_checker(calls, probe_for):
    """Route fake LeanProbe calls by action; ``probe_for(helper_name, mode)`` builds probes."""

    def fake(**kwargs):
        calls.append(kwargs)
        replacement = kwargs.get("replacement", "")
        probe = re.search(r"extract_goal( \*)? using ([A-Za-z0-9_]+)", replacement)
        if probe:
            mode = "full_context" if probe.group(1) else "cleanup"
            return probe_for(probe.group(2), mode)
        if kwargs.get("action") == "check_helper":
            return dict(OK_PROFILED_HELPER if kwargs.get("include_axiom_profile") else OK_HELPER)
        return dict(SORRY_PREFIX)

    return fake


def _capture_apply(captured):
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

    return fake_apply


def test_signature_binder_count_handles_binder_and_pi_forms():
    binder_form = (
        "theorem h.{u} (a b : ℕ) {α : Type u} [inst : Fintype α] [DecidableEq α] ⦃c : ℕ⦄ :\n"
        "  let x := (a, b);\n  x.1 = a := sorry"
    )
    pi_form = "theorem h : ∀ (a : ℝ), 0 < a → let x := Real.exp (-a); 0 ≤ x := sorry"

    assert extraction._signature_binder_count(binder_form) == 6
    assert extraction._signature_binder_count(pi_form) == 0
    assert extraction._signature_binder_count("theorem h a : True := sorry") is None


def test_signature_binder_count_counts_quoted_identifiers_once():
    statement = "theorem demo («left value» : Nat) (a «b c» : Nat) : «left value» = a := sorry"

    assert extraction._signature_binder_count(statement) == 3


def test_signature_scanners_ignore_punctuation_inside_quoted_identifiers():
    statement = (
        "theorem demo («a:b» c : Nat) («a)b» : Nat) (d : Nat) [«i:n» : Fintype Nat] : "
        "«a:b» = c := sorry"
    )

    assert extraction._signature_binder_count(statement) == 5
    binders = "(«a:b» c : Nat) («a)b» : Nat) : «a:b» = c"
    assert extraction._top_level_character(binders, ":") == binders.index(" : «a:b»") + 1
    assert extraction._group_end(binders, 0) == binders.index(")")
    assert extraction._group_end("(«a)b» : Nat)", 0) == len("(«a)b» : Nat)") - 1


def test_signature_scanners_ignore_punctuation_inside_character_literals():
    statement = (
        "theorem char_helper (c : Char) (h : c = ')') (h' : c ≠ '\\'') (s : String)"
        ' (hs : s = ")\'") : h = h := sorry'
    )

    assert extraction._signature_binder_count(statement) == 5
    assert extraction._group_end("(h : c = ')')", 0) == len("(h : c = ')')") - 1
    assert extraction._group_end("(h' : c ≠ '\\'')", 0) == len("(h' : c ≠ '\\'')") - 1
    # A quote after an identifier character is part of the name, not a literal.
    assert extraction._top_level_character("(h' : P) : Q", ":") == len("(h' : P) ")


def test_instrumented_candidate_dumps_context_before_extract_goal():
    candidate = _candidate("  have hstep : True := by", "    trivial")

    cleanup = extraction._instrumented_candidate(candidate, "helper", mode="cleanup")
    full = extraction._instrumented_candidate(candidate, "helper", mode="full_context")

    assert "\n    run_tac do\n" in cleanup
    assert "else g.cleanup" in cleanup
    assert cleanup.index("run_tac") < cleanup.index("extract_goal using helper")
    assert cleanup.endswith("extract_goal using helper\n    trivial")
    assert "else pure g" in full
    assert full.endswith("extract_goal * using helper\n    trivial")


def test_extracted_context_parses_probe_messages():
    payload = _probe_payload(
        "theorem helper : True := sorry",
        retained="a:var x:let inst✝:inst «weird name»:hyp",
        full="a:var b:var x:let inst✝:inst «weird name»:hyp",
        levels="u_1 u_2",
    )

    context = extraction._extracted_context(payload)

    assert context is not None
    assert [(item.name, item.kind) for item in context.entries] == [
        ("a", "var"),
        ("x", "let"),
        ("inst✝", "inst"),
        ("«weird name»", "hyp"),
    ]
    assert [item.name for item in context.full] == ["a", "b", "x", "inst✝", "«weird name»"]
    assert context.levels == ("u_1", "u_2")
    assert (
        extraction._extracted_context({"messages": [{"message": "theorem h : T := sorry"}]}) is None
    )


def test_private_helper_freshens_extract_goal_universe_binders():
    """Avoid redeclaring generated universe names from the active file scope."""
    candidate = _candidate("  have hstep : True := by", "    trivial")
    statement = "theorem extracted.{u_2, u_1} {V : Type u_1} {P : Type u_2} : True := sorry"

    helper = extraction._private_helper(
        statement, candidate, _context([("V", "var"), ("P", "var")], levels=["u_2", "u_1"])
    )

    match = re.search(r"private lemma extracted\.\{([^,]+), ([^}]+)\}", helper)
    assert match is not None
    first, second = match.groups()
    assert first.startswith("leanflow_u_")
    assert second.startswith("leanflow_u_")
    assert first != second
    assert f"P : Type {first}" in helper
    assert f"V : Type {second}" in helper
    assert ".{u_2, u_1}" not in helper
    assert helper.endswith(":= by\n  trivial")


def test_private_helper_reintroduces_pi_form_context_and_declares_levels():
    """A goal starting with ∀ makes extract_goal print a binder-less Pi type."""
    candidate = _candidate(
        "  have hcoord : ∀ S : Finset ℤ, 0 ≤ S.card := by",
        "    intro S\n    exact Nat.zero_le _",
    )
    statement = (
        "theorem extracted : ∀ {α : Type u_1} [inst : DecidableEq α] (m : ℕ) (a : ℝ),\n"
        "  0 < a → let x := Real.exp (-a); 0 ≤ x → ∀ (S : Finset ℤ), 0 ≤ S.card := sorry"
    )
    context = _context(
        [
            ("α", "var"),
            ("inst✝", "inst"),
            ("m", "var"),
            ("a", "var"),
            ("_ha", "hyp"),
            ("x", "let"),
            ("hx0", "hyp"),
        ],
        levels=["u_1"],
    )

    helper = extraction._private_helper(statement, candidate, context, classical=True)

    head = re.match(
        r"private lemma extracted\.\{(leanflow_u_[0-9a-f]+_1)\} : ∀ \{α : Type (\S+)\}", helper
    )
    assert head is not None
    assert head.group(1) == head.group(2)
    assert helper.endswith(":= by\n  classical\n  intro α _ m a _ha x hx0 S\n  exact Nat.zero_le _")


def test_private_helper_reintroduces_result_level_lets():
    """Binder-form output keeps the leading locals as binders and reverts the rest."""
    candidate = _candidate("  have hstep : x = n + 1 := by", "    simpa [x]")
    statement = "theorem extracted (n : Nat) :\n  let x := n + 1;\n  x = n + 1 := sorry"

    helper = extraction._private_helper(
        statement, candidate, _context([("n", "var"), ("x", "let")])
    )

    assert helper.endswith("x = n + 1 := by\n  intro x\n  simpa [x]")


def test_private_helper_rejects_signature_with_more_binders_than_context():
    candidate = _candidate("  have hstep : True := by", "    trivial")
    statement = "theorem extracted (n : Nat) (m : Nat) : True := sorry"

    assert extraction._private_helper(statement, candidate, _context([("n", "var")])) == ""


def test_switched_candidate_applies_helper_explicitly():
    candidate = _candidate("  have hstep : P := by", "    trivial")
    context = _context(
        [
            ("α", "var"),
            ("inst✝", "inst"),
            ("m", "var"),
            ("h✝", "hyp"),
            ("n✝", "var"),
            ("h", "hyp"),
            ("x", "let"),
            ("h", "hyp"),
            ("hx", "hyp"),
        ]
    )

    switched = extraction._switched_candidate(candidate, "helper", context)

    # The first `h` is shadowed by the later `h`, so only `assumption` can name it.
    assert switched == (
        "  have hstep : P := by\n"
        "    exact @helper α (by infer_instance) m (by assumption) _ (by assumption) h hx"
    )


def test_tool_treats_check_infrastructure_failure_as_switch_failure(monkeypatch, tmp_path):
    """A REPL or project failure during the call-site check must not pass the gate."""
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    responses = iter(
        [
            _probe_payload(
                "theorem leanflow_demo_hstep (a b : ℕ) (h : a = b) : a + 1 = b + 1 := sorry",
                retained="a:var b:var h:hyp",
            ),
            dict(OK_HELPER),
            {"success": False, "error": "REPL crashed", "error_code": "repl_failed"},
        ]
    )
    monkeypatch.setattr(extraction, "lean_incremental_check", lambda **kwargs: next(responses))
    monkeypatch.setattr(
        extraction,
        "apply_verified_patch_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("patch must not run")),
    )

    payload = json.loads(extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path)))

    assert payload["status"] == "helper_switch_failed"
    assert payload["diagnostics"]["error_code"] == "repl_failed"
    assert target.read_text(encoding="utf-8") == SOURCE


def test_tool_verifies_helper_and_switch_before_applying(monkeypatch, tmp_path):
    """Commit only after the warm helper, call-site, and independent axiom checks pass."""
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    calls = []
    captured = {}
    verified_edit_authority.clear_for_tests()
    monkeypatch.setattr(
        extraction,
        "lean_incremental_check",
        _fake_checker(
            calls,
            lambda helper_name, mode: _probe_payload(
                f"theorem {helper_name} (a b : ℕ) (h : a = b) : a + 1 = b + 1 := sorry",
                retained="a:var b:var h:hyp",
            ),
        ),
    )
    monkeypatch.setattr(extraction, "apply_verified_patch_tool", _capture_apply(captured))

    payload = json.loads(
        extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path), timeout_s=30)
    )

    assert payload["success"] is True
    assert payload["extraction"]["have_name"] == "hstep"
    assert payload["extraction"]["extraction_modes"] == ["cleanup"]
    assert "private lemma leanflow_demo_hstep (a b : ℕ) (h : a = b) : a + 1 = b + 1 := by" in (
        captured["patch"]
    )
    assert "+    exact @leanflow_demo_hstep a b h" in captured["patch"]
    assert "solve_by_elim" not in captured["patch"]
    assert captured["theorem_id"] == "demo"
    assert captured["verified_edit_authority_token"]
    assert [call["action"] for call in calls] == [
        "check_target",
        "check_helper",
        "check_target",
        "check_helper",
    ]
    assert calls[0]["preserve_diagnostics"] is True
    assert calls[1].get("include_axiom_profile") is not True
    assert calls[3]["include_axiom_profile"] is True


def test_tool_leaves_source_unchanged_when_switch_does_not_elaborate(monkeypatch, tmp_path):
    """Reject an extracted call-site failure before invoking the patch transaction."""
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    responses = iter(
        [
            _probe_payload(
                "theorem leanflow_demo_hstep (a b : ℕ) (h : a = b) : a + 1 = b + 1 := sorry",
                retained="a:var b:var h:hyp",
            ),
            dict(OK_HELPER),
            dict(FAILED_CHECK),
        ]
    )
    monkeypatch.setattr(extraction, "lean_incremental_check", lambda **kwargs: next(responses))
    monkeypatch.setattr(
        extraction,
        "apply_verified_patch_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("patch must not run")),
    )

    payload = json.loads(extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path)))

    assert payload["status"] == "helper_switch_failed"
    assert target.read_text(encoding="utf-8") == SOURCE


def test_tool_falls_back_to_full_context_when_cleanup_drops_a_used_name(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        """import Mathlib

theorem demo (a b : Nat) (h : a = b) (hb : 0 ≤ b) : a + 3 = b + 3 := by
  have hstep : a + 1 = b + 1 := by
    have _ := hb
    omega
  omega
""",
        encoding="utf-8",
    )
    calls = []
    captured = {}
    verified_edit_authority.clear_for_tests()

    def probe_for(helper_name, mode):
        if mode == "cleanup":
            return _probe_payload(
                f"theorem {helper_name} (a b : ℕ) (h : a = b) : a + 1 = b + 1 := sorry",
                retained="a:var b:var h:hyp",
                full="a:var b:var h:hyp hb:hyp",
            )
        return _probe_payload(
            f"theorem {helper_name} (a b : ℕ) (h : a = b) (hb : 0 ≤ b) : a + 1 = b + 1 := sorry",
            retained="a:var b:var h:hyp hb:hyp",
        )

    monkeypatch.setattr(extraction, "lean_incremental_check", _fake_checker(calls, probe_for))
    monkeypatch.setattr(extraction, "apply_verified_patch_tool", _capture_apply(captured))

    payload = json.loads(
        extraction.lean_extract_have_tool(
            "demo", str(target), cwd=str(tmp_path), have_names=["hstep"], minimum_lines=2
        )
    )

    assert payload["success"] is True
    assert payload["extraction"]["extraction_modes"] == ["full_context"]
    report = payload["extraction"]["helpers"][0]
    assert report["attempts"][0]["status"] == "cleanup_dropped_used_names"
    assert report["attempts"][0]["dropped_names"] == ["hb"]
    probes = [call for call in calls if "extract_goal" in call.get("replacement", "")]
    assert len(probes) == 2
    assert "extract_goal * using leanflow_demo_hstep" in probes[1]["replacement"]
    assert "+    exact @leanflow_demo_hstep a b h hb" in captured["patch"]


def test_tool_falls_back_to_full_context_when_minimal_helper_fails(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    calls = []
    captured = {}
    verified_edit_authority.clear_for_tests()
    warm_helper_results = iter([dict(FAILED_CHECK), dict(OK_HELPER)])

    def fake(**kwargs):
        calls.append(kwargs)
        replacement = kwargs.get("replacement", "")
        probe = re.search(r"extract_goal( \*)? using ([A-Za-z0-9_]+)", replacement)
        if probe:
            return _probe_payload(
                f"theorem {probe.group(2)} (a b : ℕ) (h : a = b) : a + 1 = b + 1 := sorry",
                retained="a:var b:var h:hyp",
            )
        if kwargs.get("action") == "check_helper" and not kwargs.get("include_axiom_profile"):
            return next(warm_helper_results)
        if kwargs.get("action") == "check_helper":
            return dict(OK_PROFILED_HELPER)
        return dict(SORRY_PREFIX)

    monkeypatch.setattr(extraction, "lean_incremental_check", fake)
    monkeypatch.setattr(extraction, "apply_verified_patch_tool", _capture_apply(captured))

    payload = json.loads(extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path)))

    assert payload["success"] is True
    assert payload["extraction"]["extraction_modes"] == ["full_context"]
    attempts = payload["extraction"]["helpers"][0]["attempts"]
    assert [attempt["status"] for attempt in attempts] == ["helper_elaboration_failed"]
    assert [call["action"] for call in calls] == [
        "check_target",
        "check_helper",
        "check_target",
        "check_helper",
        "check_target",
        "check_helper",
    ]


def test_tool_fails_closed_when_probe_lacks_context(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr(
        extraction,
        "lean_incremental_check",
        lambda **kwargs: {
            **SORRY_PREFIX,
            "messages": [
                {
                    "severity": "info",
                    "message": "theorem leanflow_demo_hstep (a b : ℕ) : a + 1 = b + 1 := sorry",
                }
            ],
        },
    )
    monkeypatch.setattr(
        extraction,
        "apply_verified_patch_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("patch must not run")),
    )

    payload = json.loads(extraction.lean_extract_have_tool("demo", str(target), cwd=str(tmp_path)))

    assert payload["status"] == "goal_extraction_failed"
    assert payload["extraction_mode"] == "cleanup"
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
    calls = []
    captured = {}
    verified_edit_authority.clear_for_tests()
    monkeypatch.setattr(
        extraction,
        "lean_incremental_check",
        _fake_checker(
            calls,
            lambda helper_name, mode: _probe_payload(
                f"theorem {helper_name} (a : ℕ) : a = a := sorry", retained="a:var"
            ),
        ),
    )
    monkeypatch.setattr(extraction, "apply_verified_patch_tool", _capture_apply(captured))

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
    assert "exact @demo_reflexive_base a" in captured["patch"]
    assert "exact @demo_reflexive_finish a" in captured["patch"]
