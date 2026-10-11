"""Tests for source-bound verified-patch declaration evidence reuse."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from leanflow_cli.native import verified_patch_batch_reuse as reuse


def _sha256(text: str) -> str:
    """Return the exact UTF-8 source digest used by fixtures."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch_result(active, source: str, *, theorem_id: str = "target") -> dict:
    """Return one authenticated successful file-exact tool result."""
    return {
        "success": True,
        "status": "patch_elaborated",
        "path": str(active),
        "theorem_id": theorem_id,
        "check_mode": "file_exact",
        "check_passed": True,
        "verified_source_revision_sha256": _sha256(source),
        "verification_source_unchanged": True,
        "verification": {
            "ok": True,
            "mode": "file_exact",
            "target": str(active),
            "output": f"{active}:2:3: warning: helper cleanup\n",
        },
    }


def _report(active, target: str, axioms=("Classical.choice",)):
    """Return one complete axiom profile for a requested declaration."""
    return SimpleNamespace(
        target=target,
        file_path=str(active),
        inspection_succeeded=True,
        axioms=list(axioms),
    )


def test_exact_patch_and_complete_batch_build_target_and_helper_checks(tmp_path) -> None:
    """Reuse one full-file compile plus one all-target axiom harness."""
    active = tmp_path / "Main.lean"
    source = (
        "private lemma helper : True := by\n"
        "  trivial\n\n"
        "theorem target : True := by\n"
        "  exact helper\n"
    )
    active.write_text(source, encoding="utf-8")
    calls: list[tuple[tuple[str, ...], str]] = []

    def inspect(targets, path):
        calls.append((tuple(targets), path))
        return {target: _report(active, target) for target in targets}

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("helper", "target"),
        allowed_axioms={"Classical.choice", "propext", "Quot.sound"},
        inspect_axioms_many=inspect,
    )

    assert decision.reusable is True
    assert decision.reason == "exact_verified_patch_batch"
    assert calls == [(("helper", "target"), str(active))]
    assert set(decision.checks) == {"helper", "target"}
    helper = decision.checks["helper"]
    assert helper["ok"] is True
    assert helper["mode"] == "incremental_target"
    assert helper["source_sha256"] == _sha256(source)
    assert helper["axiom_profile_checked"] is True
    assert helper["axiom_profile_axioms"] == ["Classical.choice"]
    assert helper["axiom_profile_blockers"] == []
    assert helper["warnings"] == 1
    assert len(str(helper["declaration_sha256"])) == 64


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("source", "verified_source_changed"),
        ("path", "active_file_changed"),
        ("theorem", "assignment_target_changed"),
        ("mode", "broad_scope_not_file_exact"),
    ],
)
def test_stale_or_tampered_patch_identity_falls_back_without_batch(
    tmp_path,
    mutation: str,
    reason: str,
) -> None:
    """Never profile declarations when broad evidence is not the current edit."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  trivial\n"
    active.write_text(source, encoding="utf-8")
    payload = _patch_result(active, source)
    active_file = str(active)
    assignment_target = "target"
    if mutation == "source":
        active.write_text(source + "\n-- changed\n", encoding="utf-8")
    elif mutation == "path":
        payload["path"] = str(tmp_path / "Other.lean")
    elif mutation == "theorem":
        payload["theorem_id"] = "other"
    elif mutation == "mode":
        payload["check_mode"] = "module"
    calls: list[object] = []

    decision = reuse.build_reusable_checks(
        payload,
        active_file=active_file,
        assignment_target=assignment_target,
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda *_args: calls.append(object()) or {},
    )

    assert decision.reusable is False
    assert decision.reason == reason
    assert decision.checks == {}
    assert decision.axiom_batch_started is False
    assert calls == []


def test_placeholder_declaration_cannot_borrow_broad_file_success(tmp_path) -> None:
    """Reject a broad compile that succeeded only because Lean admits a placeholder."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  admit\n"
    active.write_text(source, encoding="utf-8")
    called = False

    def inspect(_targets, _path):
        nonlocal called
        called = True
        return {}

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=inspect,
    )

    assert decision.reusable is False
    assert decision.reason == "declaration_has_placeholder:target"
    assert called is False


def test_partial_axiom_batch_falls_back_for_every_declaration(tmp_path) -> None:
    """Do not reuse any check from a partial all-or-nothing profile batch."""
    active = tmp_path / "Main.lean"
    source = (
        "private lemma helper : True := by trivial\n" "theorem target : True := by exact helper\n"
    )
    active.write_text(source, encoding="utf-8")

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("helper", "target"),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda _targets, _path: {"helper": _report(active, "helper")},
    )

    assert decision.reusable is False
    assert decision.reason == "axiom_batch_incomplete"
    assert decision.checks == {}
    assert decision.axiom_batch_started is True


def test_disallowed_axiom_preserves_negative_exact_verdict(tmp_path) -> None:
    """Reuse elaboration while rejecting a declaration outside current policy."""
    active = tmp_path / "Main.lean"
    source = "theorem target : True := by\n  trivial\n"
    active.write_text(source, encoding="utf-8")

    decision = reuse.build_reusable_checks(
        _patch_result(active, source),
        active_file=str(active),
        assignment_target="target",
        declaration_targets=("target",),
        allowed_axioms={"Classical.choice"},
        inspect_axioms_many=lambda _targets, _path: {
            "target": _report(active, "target", ("Classical.choice", "Unsafe.custom"))
        },
    )

    assert decision.reusable is True
    check = decision.checks["target"]
    assert check["ok"] is False
    assert check["axiom_profile_checked"] is True
    assert check["axiom_profile_blockers"] == ["Unsafe.custom"]
    assert check["errors"] == 1
