"""Tests for the extracted formalization_document_runner helpers + native_runner re-export (Phase 2).

These cover the cleanly-pure ``/formalize`` document-formalization helpers moved out of
native_runner: the blueprint manifest/bullet text-parsers (bullet value extraction, the
"missing"/"unresolved" fidelity predicates, the checklist parser) and the workflow-phase
predicates that key off the live-state snapshot. The re-export-identity test pins every moved name
to the same object on ``native_runner`` so existing callers resolve them without a back-import.
"""

from leanflow_cli.formalization import formalization_document_runner as fdr
from leanflow_cli.native import native_runner


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in
    # formalization_document_runner, so callers (including the entangled formalize helpers still
    # living in native_runner) resolve the extracted helpers without a back-import.
    for name in fdr.__all__:
        assert getattr(native_runner, name) is getattr(fdr, name), name


def test_blueprint_bullet_value_extracts_labeled_field():
    entry = "- Statement: theorem foo : True\n- Source: doc.tex\n"
    assert fdr._blueprint_bullet_value(entry, "Statement") == "theorem foo : True"
    assert fdr._blueprint_bullet_value(entry, "Source") == "doc.tex"
    # A label not present yields the empty string, not an error.
    assert fdr._blueprint_bullet_value(entry, "Missing") == ""


def test_blueprint_value_and_block_missing_treat_placeholders_as_missing():
    # Empty / placeholder values count as missing.
    for placeholder in ("", "  ", "_pending_", "pending", "TODO", "tbd"):
        assert fdr._blueprint_value_missing(placeholder) is True
    assert fdr._blueprint_value_missing("doc.tex") is False
    # A real value is not missing; a trailing placeholder after a colon is.
    assert fdr._blueprint_block_missing("Statement: theorem foo") is False
    assert fdr._blueprint_block_missing("Statement: _pending_") is True


def test_blueprint_fidelity_field_unresolved_flags_unresolved_markers():
    assert fdr._blueprint_fidelity_field_unresolved("fully covered, verified") is False
    # Any of the unresolved markers (case-insensitive) flips it to True.
    assert fdr._blueprint_fidelity_field_unresolved("statement is UNVERIFIED") is True
    assert fdr._blueprint_fidelity_field_unresolved("omitted for now") is True
    # An empty/placeholder value is unresolved via the block-missing path.
    assert fdr._blueprint_fidelity_field_unresolved("_pending_") is True


def test_blueprint_checklist_item_checked_tristate():
    text = "- [x] statement verified\n- [ ] proof complete\n"
    assert fdr._blueprint_checklist_item_checked(text, "statement verified") is True
    assert fdr._blueprint_checklist_item_checked(text, "proof complete") is False
    # An item that isn't in the checklist at all returns None (distinct from False).
    assert fdr._blueprint_checklist_item_checked(text, "missing item") is None
