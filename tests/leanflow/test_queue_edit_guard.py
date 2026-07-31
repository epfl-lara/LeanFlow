"""Tests for the extracted queue_edit_guard helpers + native_runner re-export (Phase 2).

These cover the pure queue-edit-guard machinery moved out of native_runner: the protected
declaration inventory/diff (an out-of-scope edit to a *future queue* declaration is detected,
while an in-scope edit to the assigned theorem's proof body is allowed), the source-text
restoration, the assigned statement signature, and the guard key.
"""

from types import SimpleNamespace

from leanflow_cli.native import native_runner
from leanflow_cli.workflows import queue_edit_guard

FILE = """\
theorem assigned_thm : True := by
  trivial

theorem future_item : 1 + 1 = 2 := by
  rfl
"""


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in queue_edit_guard,
    # so callers (including the two guard functions still living in native_runner) resolve the
    # extracted helpers without a back-import.
    for name in queue_edit_guard.__all__:
        assert getattr(native_runner, name) is getattr(queue_edit_guard, name), name


def test_guard_key_is_target_and_resolved_file():
    key = queue_edit_guard._queue_edit_guard_key("Ns.thm", "/tmp/Main.lean")
    target, _, resolved = key.partition("\0")
    assert target == "Ns.thm"
    assert resolved.endswith("Main.lean")
    # Distinct targets / files yield distinct keys.
    assert key != queue_edit_guard._queue_edit_guard_key("Ns.other", "/tmp/Main.lean")


def test_assigned_statement_signature_drops_proof_body():
    sig = queue_edit_guard._queue_edit_assigned_statement_signature(FILE, "assigned_thm")
    # The signature is the statement up to the `:=` assignment marker, whitespace-normalized,
    # so it excludes the proof body (`by trivial`).
    assert sig == "theorem assigned_thm : True"
    assert "trivial" not in sig


def test_assigned_preamble_tracks_doc_comment_and_multiline_attributes():
    source = (
        "theorem earlier : True := by\n"
        "  trivial\n\n"
        "/-- Documentation owned by the assigned theorem. -/\n"
        "@[category research open,\n"
        "  simp]\n"
        "theorem assigned_thm : True := by\n"
        "  sorry\n"
    )

    assert queue_edit_guard._queue_edit_assigned_preamble(source, "assigned_thm") == (
        "/-- Documentation owned by the assigned theorem. -/\n"
        "@[category research open,\n"
        "  simp]\n"
    )
    assert queue_edit_guard._queue_edit_assigned_preamble(source, "earlier") == ""
    assert queue_edit_guard._queue_edit_assigned_preamble(source, "missing") is None


def test_doc_comment_guard_allows_atomic_move_but_rejects_delete_or_edit():
    doc = "/-- Documentation owned by demo. -/"
    before = f"{doc}\ntheorem demo : True := by\n  trivial\n"
    moved = f"private lemma helper : True := by trivial\n\n{doc}\ntheorem demo : True := by\n  trivial\n"
    deleted = "theorem demo : True := by\n  trivial\n"
    edited = before.replace("owned by demo", "silently changed")

    assert queue_edit_guard._queue_edit_preserves_doc_comments(before, moved) is True
    assert queue_edit_guard._queue_edit_preserves_doc_comments(before, deleted) is False
    assert queue_edit_guard._queue_edit_preserves_doc_comments(before, edited) is False


def test_protected_declarations_exclude_the_assigned_target():
    protected = queue_edit_guard._queue_edit_protected_declarations(FILE, "assigned_thm")
    names = {p["name"] for p in protected}
    # The assigned theorem is NOT protected (the model may edit its proof body); the other,
    # pre-existing future-queue declaration IS protected.
    assert "assigned_thm" not in names
    assert "future_item" in names


def test_in_scope_proof_edit_is_allowed_but_out_of_scope_edit_is_detected_and_restored():
    protected = queue_edit_guard._queue_edit_protected_declarations(FILE, "assigned_thm")

    # In-scope: only the assigned theorem's proof body changed -> no protected declaration moved.
    in_scope = FILE.replace("  trivial", "  exact trivial")
    assert queue_edit_guard._queue_edit_changed_protected_declarations(protected, in_scope) == []

    # Out-of-scope: a protected future-queue declaration was edited -> detected as "changed".
    out_of_scope = FILE.replace("1 + 1 = 2", "1 + 1 = 3")
    changed = queue_edit_guard._queue_edit_changed_protected_declarations(protected, out_of_scope)
    assert len(changed) == 1
    assert changed[0]["reason"] == "changed"
    assert changed[0]["protected"]["name"] == "future_item"

    # Restoring rewrites the protected declaration back to its snapshot text, leaving the
    # in-scope (assigned) region untouched -> we recover the original file verbatim.
    restored = queue_edit_guard._restore_changed_protected_declarations(out_of_scope, changed)
    assert restored == FILE


def test_declaration_local_scope_prefix_is_not_stripped_from_new_helper():
    before = """\
theorem protected_item : True := by
  trivial

theorem assigned_thm : True := by
  sorry
"""
    after = before.replace(
        "theorem assigned_thm",
        """open scoped Classical in
theorem checked_helper : True := by
  trivial

theorem assigned_thm""",
    )
    protected = queue_edit_guard._queue_edit_protected_declarations(before, "assigned_thm")

    assert queue_edit_guard._queue_edit_changed_protected_declarations(protected, after) == []
    delta = queue_edit_guard._queue_edit_declaration_delta(
        before,
        after,
        "assigned_thm",
        protected,
    )
    assert delta.assigned_changed is False
    assert delta.helper_names == ("checked_helper",)


def test_declaration_local_option_prefix_is_not_stripped_from_new_helper():
    before = """\
theorem protected_item : True := by
  trivial

theorem assigned_thm : True := by
  sorry
"""
    after = before.replace(
        "theorem assigned_thm",
        """set_option maxRecDepth 100000 in
private lemma checked_helper : True := by
  trivial

theorem assigned_thm""",
    )
    protected = queue_edit_guard._queue_edit_protected_declarations(before, "assigned_thm")

    assert queue_edit_guard._queue_edit_changed_protected_declarations(protected, after) == []
    delta = queue_edit_guard._queue_edit_declaration_delta(
        before,
        after,
        "assigned_thm",
        protected,
    )
    assert delta.assigned_changed is False
    assert delta.helper_names == ("checked_helper",)


def test_restore_returns_none_when_protected_declaration_is_missing():
    protected = queue_edit_guard._queue_edit_protected_declarations(FILE, "assigned_thm")
    # Drop the protected declaration entirely -> reported as "missing".
    removed = "theorem assigned_thm : True := by\n  trivial\n"
    changed = queue_edit_guard._queue_edit_changed_protected_declarations(protected, removed)
    assert any(item["reason"] == "missing" for item in changed)
    # An in-place restore cannot recover a removed declaration, so it bails out (None) so the
    # caller falls back to a full pre-tool-state restore.
    assert queue_edit_guard._restore_changed_protected_declarations(removed, changed) is None


def test_removed_generated_assignment_requires_no_remaining_source_reference():
    referenced = "theorem result : True := by\n  exact generated_helper\n"
    unused = "theorem result : True := by\n  trivial\n"

    assert (
        queue_edit_guard._queue_edit_removed_generated_assignment_is_safe(
            referenced,
            "generated_helper",
            removal_authorized=True,
            protected_declarations=[
                {
                    "kind": "theorem",
                    "name": "result",
                    "text": referenced.strip(),
                    "line": 1,
                }
            ],
        )
        is False
    )
    assert (
        queue_edit_guard._queue_edit_removed_generated_assignment_is_safe(
            unused,
            "generated_helper",
            removal_authorized=True,
            protected_declarations=[
                {
                    "kind": "theorem",
                    "name": "result",
                    "text": unused.strip(),
                    "line": 1,
                }
            ],
        )
        is True
    )


def test_initial_declaration_keys_are_cached_on_the_agent():
    agent = SimpleNamespace()
    keys = queue_edit_guard._queue_edit_initial_declaration_keys(agent, "/tmp/Main.lean", FILE)
    assert ("theorem", "assigned_thm") in keys
    assert ("theorem", "future_item") in keys
    # The keys are persisted on the agent and a second call returns the cached set even if the
    # file text later changes (the guard pins the *initial* declaration set).
    cached = queue_edit_guard._queue_edit_initial_declaration_keys(
        agent, "/tmp/Main.lean", "theorem only_now : True := by trivial\n"
    )
    assert cached == keys


def test_axiom_declaration_names_detects_modifiers_and_ignores_comments():
    text = (
        "axiom plain : False\n"
        "@[simp] private axiom decorated : 1 = 2\n"
        "noncomputable axiom nc : Nat\n"
        "-- axiom commented : False\n"
        "/- axiom blocked : True -/\n"
        'def s := "axiom in_a_string : False"\n'
        "def myaxiom := 1\n"  # the word 'axiom' inside an identifier must not match
    )
    names = queue_edit_guard._axiom_declaration_names(text)
    assert names == {"plain", "decorated", "nc"}


def test_axiom_declaration_names_detects_unicode_and_quoted_identifiers():
    # A cheating edit must not bypass the guard via Unicode or guillemet-quoted axiom names.
    text = (
        "axiom α : False\n"
        "noncomputable axiom f₁ : Nat\n"
        "axiom «cheat ax» : False\n"
        "axiom foo.bar : T\n"
        "axiom no_space:False\n"
    )
    names = queue_edit_guard._axiom_declaration_names(text)
    assert names == {"α", "f₁", "«cheat ax»", "foo.bar", "no_space"}


def test_introduced_forbidden_axioms_respects_allowlist():
    before = "theorem t : True := by\n  trivial\n"
    after = before + "axiom sneaky : False\nprivate axiom helper_ax : 1 = 2\n"

    standard = {"propext", "Classical.choice", "Quot.sound"}
    # Declaring new axioms is forbidden even though standard dependency axioms are allowed.
    assert queue_edit_guard._introduced_forbidden_axioms(before, after, standard) == [
        "helper_ax",
        "sneaky",
    ]
    # An explicitly allow-listed name (e.g. via --axioms) is permitted.
    assert queue_edit_guard._introduced_forbidden_axioms(
        before, after, standard | {"helper_ax"}
    ) == ["sneaky"]
    # An axiom already present before the edit is not flagged as "introduced".
    assert queue_edit_guard._introduced_forbidden_axioms(after, after, standard) == []


def test_native_runner_allowed_axioms_reads_env(monkeypatch):
    monkeypatch.delenv("LEANFLOW_NATIVE_ALLOWED_AXIOMS", raising=False)
    assert native_runner._allowed_axioms() == set(native_runner.DEFAULT_ALLOWED_AXIOMS)
    monkeypatch.setenv("LEANFLOW_NATIVE_ALLOWED_AXIOMS", "myAx, other_ax  third.ax")
    allowed = native_runner._allowed_axioms()
    assert {"myAx", "other_ax", "third.ax"} <= allowed
    assert set(native_runner.DEFAULT_ALLOWED_AXIOMS) <= allowed
