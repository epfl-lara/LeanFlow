"""Restrict reviewed source options to resource limits and explicit binder discipline."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.formalization import freshness, handoff, lean_policy
from leanflow_cli.formalization.records import load_document, load_receipt
from tests.leanflow import test_formalization_handoff as handoff_tests

reviewed_project = handoff_tests.reviewed


@pytest.mark.parametrize(
    "source",
    [
        "set_option autoImplicit false\ntheorem explicit (n : Nat) : n = n := by sorry",
        "set_option autoImplicit false in\ntheorem explicit (n : Nat) : n = n := by sorry",
        "section\nset_option autoImplicit false\nend\ntheorem explicit : True := by sorry",
        "set_option /- outer /- inner -/ -/ autoImplicit false -- explicit names\n"
        "theorem explicit : True := by sorry",
        "set_option autoImplicit false in\nset_option autoImplicit false in\n"
        "theorem explicit : True := by sorry",
        "set_option autoImplicit false",
    ],
)
def test_only_literal_false_can_disable_auto_implicit_binders(source: str) -> None:
    lean_policy.validate_source(source)


@pytest.mark.parametrize(
    "value",
    [
        "true",
        "False",
        "falseValue",
        "false'",
        "false?",
        "false!",
        "false.foo",
        "false₁",
        "false\u0301",
        "(false)",
        '"false"',
        "Bool.false",
        "0",
        "!true",
        "by exact false",
    ],
)
def test_auto_implicit_option_does_not_accept_other_values(value: str) -> None:
    with pytest.raises(ValueError, match="supported in reviewed drafts"):
        lean_policy.validate_source(f"set_option autoImplicit {value}\n")


@pytest.mark.parametrize(
    "body",
    [
        "set_option autoImplicit true in\ntheorem p : True := by sorry",
        "set_option debug.skipKernelTC true in\ntheorem p : True := by sorry",
        "set_option relaxedAutoImplicit false in\ntheorem p : True := by sorry",
        "#eval IO.println 1",
        "theorem p : True := by run_tac pure ()",
        "initialize registerSomething",
    ],
)
def test_scoped_false_does_not_hide_unsupported_inner_commands(body: str) -> None:
    with pytest.raises(ValueError):
        lean_policy.validate_source(f"set_option autoImplicit false in\n{body}\n")


def test_source_notes_and_print_axioms_keep_existing_nonexecuting_policy() -> None:
    lean_policy.validate_source(
        "/- set_option autoImplicit true /- IO -/ -/\n"
        'def description : String := "set_option autoImplicit true IO"\n'
        "set_option maxHeartbeats 1000 in\n"
        "set_option maxRecDepth 1000 in\n"
        "theorem p : True := by sorry\n#print axioms p\n"
    )


@pytest.mark.parametrize("value", ["true", "falseValue", "false.foo", "(false)"])
def test_unsupported_source_option_stops_before_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    (tmp_path / "Main.lean").write_text(
        f"set_option autoImplicit {value}\ntheorem p : True := by sorry\n"
    )
    monkeypatch.setattr(lean_policy, "dependency_inputs", lambda root: {})
    monkeypatch.setattr(
        lean_policy,
        "inspect_layout",
        lambda *args, **kwargs: {
            "accepted": True,
            "libraries": [{"roots": ["Main"], "src_dir": "."}],
        },
    )
    monkeypatch.setattr(
        lean_policy,
        "build_project",
        lambda *args, **kwargs: pytest.fail("Unsupported options reached the compiler"),
    )
    result = lean_policy.inspect_draft(tmp_path, ["Main.lean"], tmp_path / "checks", ())
    assert not result["accepted"]
    assert "supported in reviewed drafts" in result["error"]


def test_previous_source_admission_policy_receipt_stays_stale(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_policy = "formalization-exact-artifact-kernel-proofs-v5"
    with monkeypatch.context() as previous:
        previous.setattr(handoff, "POLICY_VERSION", old_policy)
        root, _ = request.getfixturevalue("reviewed_project")
    record = load_document(root, "paper")
    receipt = load_receipt(record)
    assert receipt["handoff"]["acceptance_policy"]["policy_id"] == old_policy
    assert lean_policy.POLICY_VERSION == "formalization-exact-artifact-kernel-proofs-v6"
    monkeypatch.setattr(
        freshness,
        "environment_identity",
        lambda root: pytest.fail("Stale policy must fail before environment checking"),
    )
    result = freshness.published_acceptance_freshness(root, record, receipt)
    assert result["current"] is False and result["status"] == "checker_stale"
    assert "policy or Lean checker changed" in result["reason"]
    assert load_receipt(record) == receipt
