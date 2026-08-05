"""Pin deterministic rejection of bare assigned-target self-reference."""

from leanflow_cli.native.direct_self_reference import is_direct_self_reference


def test_detects_bare_direct_self_reference():
    assert is_direct_self_reference(
        "theorem result : True := by\n  exact result",
        "IMO2026P3.result",
    )
    assert is_direct_self_reference(
        "theorem result : True := by\n  simpa using IMO2026P3.result",
        "result",
    )
    assert is_direct_self_reference(
        "theorem result : True := by\n  let rec h : True := h\n  exact h",
        "result",
    )


def test_allows_recursive_call_with_argument_and_other_declaration():
    assert not is_direct_self_reference(
        "theorem result (n : Nat) : True := by\n  exact result (n - 1)",
        "result",
    )
    assert not is_direct_self_reference(
        "theorem result : True := by\n  exact helper",
        "result",
    )
