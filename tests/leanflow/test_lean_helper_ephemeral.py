"""Dispatch-worker exact-project helper verification tests."""

from __future__ import annotations

import re

from leanflow_cli.lean import lean_helper_ephemeral as helper_check


def _axiom_output(source: str, axioms: str = "") -> str:
    """Return marked ``#print axioms`` output for one generated harness."""
    begins = re.findall(r"LEANFLOW_AXIOMS_BEGIN_[A-F0-9]+", source)
    ends = re.findall(r"LEANFLOW_AXIOMS_END_[A-F0-9]+", source)
    names = re.findall(r"#print axioms\s+([^\s]+)", source)
    chunks = []
    for begin, end, name in zip(begins, ends, names, strict=True):
        profile = (
            f"'{name}' depends on axioms: [{axioms}]"
            if axioms
            else f"'{name}' does not depend on any axioms"
        )
        chunks.append(f"{begin}\n{profile}\n{end}")
    return "\n".join(chunks)


def _source() -> str:
    return """import Mathlib

namespace Demo

private lemma existing (n : ℕ) : n = n := by rfl

/-- Keep this target preamble attached to the anchor. -/
@[simp]
theorem target (n : ℕ) : n = n := by
  sorry

theorem later : False := by
  sorry

end Demo
"""


def test_helper_check_builds_exact_pre_anchor_harness(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def exact_check(source, *, cwd, timeout_s):
        captured.update(source=source, cwd=cwd, timeout_s=timeout_s)
        return {
            "success": True,
            "ok": True,
            "output": _axiom_output(source),
            "command": ["lake", "env", "lean", "/tmp/helper.lean"],
            "resource_admission": {"enforced": True},
        }

    monkeypatch.setattr(helper_check, "lean_ephemeral_source_check", exact_check)
    helper = "private lemma helper (n : ℕ) : n = n := by\n  exact existing n"

    result = helper_check.check_helper_ephemerally(
        source_text=_source(),
        helper_source=helper,
        theorem_id="target",
        file_path=tmp_path / "Main.lean",
        project_root=tmp_path,
        anchor_skeleton="theorem target (n : ℕ) : n = n := by\n  sorry",
        timeout_s=300,
    )

    harness = str(captured["source"])
    assert harness.startswith("import Mathlib\n")
    assert "private lemma existing" in harness
    assert helper in harness
    assert harness.index(helper) < harness.index("/-- Keep this target preamble")
    assert "@[simp]\ntheorem target (n : ℕ) : n = n := by\n  sorry" in harness
    assert "theorem later" not in harness
    assert captured["cwd"] == tmp_path
    assert captured["timeout_s"] == 300
    assert result["success"] is True
    assert result["ok"] is True
    assert result["valid_without_sorry"] is True
    assert result["has_errors"] is False
    assert result["has_sorry"] is False
    assert result["verification_scope"] == "helper_candidate"
    assert result["replacement_matches_target"] is False
    assert result["replacement_declarations"] == ["helper"]
    assert result["axiom_profile_requested"] is True
    assert result["axiom_profile_checked"] is True
    assert result["axiom_profile_axioms"] == []
    assert result["axiom_profile_blockers"] == []
    assert result["axiom_profile_error"] == ""
    assert result["resource_admission"] == {"enforced": True}


def test_helper_check_rejects_placeholder_before_spawning(monkeypatch, tmp_path):
    monkeypatch.setattr(
        helper_check,
        "lean_ephemeral_source_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("placeholder helper reached Lake")
        ),
    )

    result = helper_check.check_helper_ephemerally(
        source_text=_source(),
        helper_source="private lemma helper : True := by sorry",
        theorem_id="target",
        file_path=tmp_path / "Main.lean",
        project_root=tmp_path,
        anchor_skeleton="theorem target (n : ℕ) : n = n := by\n  sorry",
        timeout_s=300,
    )

    assert result["ok"] is False
    assert result["has_sorry"] is True
    assert result["error_code"] == "helper_placeholder"
    assert result["axiom_profile_requested"] is True
    assert result["axiom_profile_checked"] is False
    assert result["axiom_profile_axioms"] == []


def test_helper_check_requires_named_unambiguous_declarations(monkeypatch, tmp_path):
    monkeypatch.setattr(
        helper_check,
        "lean_ephemeral_source_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid helper reached Lake")
        ),
    )
    common = {
        "source_text": _source(),
        "theorem_id": "target",
        "file_path": tmp_path / "Main.lean",
        "project_root": tmp_path,
        "anchor_skeleton": "theorem target (n : ℕ) : n = n := by\n  sorry",
        "timeout_s": 300,
    }

    missing = helper_check.check_helper_ephemerally(
        helper_source="set_option maxHeartbeats 1000",
        **common,
    )
    colliding = helper_check.check_helper_ephemerally(
        helper_source="private lemma existing (n : ℕ) : n = n := by rfl",
        **common,
    )

    assert missing["error_code"] == "missing_helper_declaration"
    assert colliding["error_code"] == "ambiguous_helper_declaration"


def test_helper_check_rejects_nonstandard_or_missing_axiom_profile(monkeypatch, tmp_path):
    outputs = iter(("nonstandard", "missing"))

    def exact_check(source, **kwargs):
        mode = next(outputs)
        return {
            "success": True,
            "ok": True,
            "output": _axiom_output(source, "sorryAx") if mode == "nonstandard" else "",
        }

    monkeypatch.setattr(helper_check, "lean_ephemeral_source_check", exact_check)
    common = {
        "source_text": _source(),
        "helper_source": "private lemma helper (n : ℕ) : n = n := by rfl",
        "theorem_id": "target",
        "file_path": tmp_path / "Main.lean",
        "project_root": tmp_path,
        "anchor_skeleton": "theorem target (n : ℕ) : n = n := by\n  sorry",
        "timeout_s": 300,
    }

    nonstandard = helper_check.check_helper_ephemerally(**common)
    missing = helper_check.check_helper_ephemerally(**common)

    assert nonstandard["ok"] is False
    assert nonstandard["axiom_profile_checked"] is True
    assert nonstandard["axiom_profile_axioms"] == ["sorryAx"]
    assert nonstandard["axiom_profile_blockers"] == ["sorryAx"]
    assert nonstandard["error_code"] == "helper_axiom_profile"
    assert missing["ok"] is False
    assert missing["axiom_profile_checked"] is False
    assert missing["error_code"] == "helper_axiom_profile_unavailable"


def test_helper_check_respects_explicit_axiom_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_NATIVE_ALLOWED_AXIOMS", "my_axiom")
    monkeypatch.setattr(
        helper_check,
        "lean_ephemeral_source_check",
        lambda source, **kwargs: {
            "success": True,
            "ok": True,
            "output": _axiom_output(source, "Classical.choice, my_axiom"),
        },
    )

    result = helper_check.check_helper_ephemerally(
        source_text=_source(),
        helper_source="private lemma helper (n : ℕ) : n = n := by rfl",
        theorem_id="target",
        file_path=tmp_path / "Main.lean",
        project_root=tmp_path,
        anchor_skeleton="theorem target (n : ℕ) : n = n := by\n  sorry",
        timeout_s=300,
    )

    assert result["ok"] is True
    assert result["axioms"] == ["Classical.choice", "my_axiom"]
    assert result["axiom_profile_axioms"] == ["Classical.choice", "my_axiom"]
    assert result["axiom_profile_blockers"] == []
