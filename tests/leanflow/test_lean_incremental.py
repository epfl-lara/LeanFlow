from __future__ import annotations

import re
from pathlib import Path

import pytest

from leanflow_cli.lean import lean_incremental as li


@pytest.fixture(autouse=True)
def _close_sessions():
    li.close_incremental_sessions()
    yield
    li.close_incremental_sessions()


def _write_project(tmp_path: Path, text: str):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    target = module_dir / "Main.lean"
    target.write_text(text, encoding="utf-8")
    return project, target


def test_low_memory_mode_never_starts_leanprobe(monkeypatch, tmp_path):
    project, target = _write_project(tmp_path, "theorem demo : True := by\n  trivial\n")
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "1")
    monkeypatch.setattr(li, "_probe", lambda: pytest.fail("low-memory mode started LeanProbe"))

    capabilities = li.lean_incremental_capabilities(project)
    result = li.lean_incremental_check(
        action="prepare_file",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert capabilities["available"] is False
    assert capabilities["degraded_codes"] == ["low_memory_mode"]
    assert result["error_code"] == "low_memory_mode"


def test_segment_file_keeps_doc_comment_with_declaration():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "/-- First theorem. -/",
                "theorem first : True := by",
                "  trivial",
                "",
                "lemma second : True := by",
                "  trivial",
                "",
            ]
        )
    )

    assert header == "import Mathlib\n"
    assert [segment.name for segment in segments] == ["first", "second"]
    assert segments[0].text.startswith("/-- First theorem. -/")
    assert segments[0].start_line == 3
    assert segments[1].start_line == 7


def test_segment_file_attaches_set_option_wrapper_to_private_theorem():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem first : True := by",
                "  trivial",
                "",
                "set_option maxRecDepth 10000 in",
                "private theorem wrapped : True := by",
                "  trivial",
                "",
                "theorem last : True := by",
                "  trivial",
                "",
            ]
        )
    )

    assert header == "import Mathlib\n"
    assert [segment.name for segment in segments] == ["first", "wrapped", "last"]
    assert "set_option" not in segments[0].text
    assert segments[1].text.startswith("set_option maxRecDepth 10000 in")


def test_segment_file_attaches_variable_wrapper_to_scoped_declaration():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem first : True := by",
                "  trivial",
                "",
                "variable (P : Type) in",
                "abbrev Scoped := P",
                "",
                "theorem last : True := by",
                "  trivial",
                "",
            ]
        )
    )

    assert header == "import Mathlib\n"
    assert [segment.name for segment in segments] == ["first", "Scoped", "last"]
    assert "variable (P : Type) in" not in segments[0].text
    assert segments[1].text.startswith("variable (P : Type) in")
    assert segments[1].declaration_start > segments[1].start


def test_segment_file_ignores_declaration_keywords_inside_comments_and_strings():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "/-- The source says theorem fake : True := by trivial. -/",
                "theorem real : True := by",
                '  have s := "def also_fake := 1"',
                "  trivial",
                "",
                "/-",
                "lemma hidden : True := by trivial",
                "-/",
                "def actual : Nat := 1",
                "",
            ]
        )
    )

    assert header == "import Mathlib\n"
    assert [segment.name for segment in segments] == ["real", "actual"]
    assert segments[0].text.startswith("/-- The source says theorem fake")
    assert "def also_fake" in segments[0].text
    assert segments[0].start_line == 3
    assert segments[1].start_line == 8
    assert segments[1].declaration_start > segments[1].start


def test_segment_file_uses_leanprobe_extended_declaration_parser():
    _header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "@[simp]",
                "noncomputable theorem πLemma.{u} : True := by",
                "  trivial",
                "",
                "abbrev Alias := Nat",
                "",
                "structure Box where",
                "  value : Nat",
                "",
                "axiom trusted_fact : True",
                "",
            ]
        )
    )

    assert [(segment.kind, segment.name) for segment in segments] == [
        ("theorem", "πLemma"),
        ("abbrev", "Alias"),
        ("structure", "Box"),
        ("axiom", "trusted_fact"),
    ]


def test_segment_file_keeps_mutual_block_as_single_context_chunk():
    _header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "mutual",
                "  def evenish : Nat -> Bool",
                "    | 0 => true",
                "    | n + 1 => oddish n",
                "",
                "  def oddish : Nat -> Bool",
                "    | 0 => false",
                "    | n + 1 => evenish n",
                "end",
                "",
                "theorem after : True := by",
                "  trivial",
                "",
            ]
        )
    )

    assert [(segment.kind, segment.name) for segment in segments] == [
        ("mutual", ""),
        ("theorem", "after"),
    ]
    assert "def evenish" in segments[0].text
    assert "def oddish" in segments[0].text


def test_find_segment_does_not_confuse_prefix_related_theorem_names():
    _header, segments = li._segment_file(
        "\n".join(
            [
                "theorem residue_k_mod_455_eq_1 : True := by sorry",
                "",
                "theorem residue_k_mod_455_eq_106 : True := by sorry",
                "",
            ]
        )
    )

    assert li._find_segment(segments, "residue_k_mod_455_eq_1").name == ("residue_k_mod_455_eq_1")
    assert li._find_segment(segments, "residue_k_mod_455_eq_106").name == (
        "residue_k_mod_455_eq_106"
    )


def test_check_target_delegates_to_leanprobe_and_preserves_leanflow_action(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem demo : True := by",
                "  trivial",
                "",
            ]
        ),
    )

    class _FakeProbe:
        def __init__(self):
            self.calls = []

        def check_target(self, *args, **kwargs):
            self.calls.append(("check_target", args, kwargs))
            return {
                "success": True,
                "ok": True,
                "backend": "lean_interact",
                "tool": "lean_probe",
                "action": "check",
                "file": str(target),
                "target": "demo",
                "command": "lean_probe check",
                "cache": {"cache_hit": True},
            }

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : True := by\n  trivial\n",
        include_tactics=True,
        timeout_s=7,
    )

    assert payload["success"] is True
    assert payload["ok"] is True
    assert payload["action"] == "check_target"
    assert payload["command"] == "lean_probe check_target"
    assert payload["replacement_matches_target"] is True
    assert payload["verification_scope"] == "target_candidate"
    assert payload["requested_timeout_s"] == 7
    assert payload["effective_timeout_s"] == 7
    assert payload["timeout_adjusted"] is False
    assert payload["timeout_policy"] == "requested"
    assert payload["resource_admission"]["project_root"] == str(project.resolve())
    assert payload["resource_admission"]["enforced"] is True
    assert fake.calls == [
        (
            "check_target",
            (target.resolve(),),
            {
                "theorem_id": "demo",
                "cwd": project.resolve(),
                "replacement": "theorem demo : True := by\n  trivial\n",
                "include_tactics": True,
                "timeout_s": 7,
            },
        )
    ]


def test_check_target_uses_canonical_fallback_after_prefix_build_failure(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem prior : True := by",
                "  trivial",
                "",
                "theorem demo : True := by",
                "  trivial",
                "",
            ]
        ),
    )
    checked_sources = []

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": False,
                "ok": False,
                "error_code": "prior_decl_failed",
                "error": "failed to build env before target at prior",
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    def exact_check(source, **kwargs):
        checked_sources.append(source)
        return {
            "success": True,
            "ok": True,
            "output": "",
            "messages": [],
        }

    monkeypatch.setattr(li, "lean_ephemeral_source_check", exact_check)

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert checked_sources
    assert "theorem demo : True := by\n  trivial" in checked_sources[0]
    assert payload["success"] is True
    assert payload["ok"] is True
    assert payload["canonical_fallback"] is True
    assert payload["backend"] == "lean_exact_ephemeral"
    assert payload["incremental_fallback_error_code"] == "prior_decl_failed"


def test_check_target_can_return_complete_inline_axiom_profile(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        replacement = ""

        def check_target(self, *args, **kwargs):
            self.replacement = str(kwargs["replacement"])
            begin = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_BEGIN_[A-F0-9]+", self.replacement)
            end = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_END_[A-F0-9]+", self.replacement)
            assert begin is not None and end is not None
            return {
                "success": True,
                "ok": True,
                "backend": "lean_interact",
                "tool": "lean_probe",
                "action": "check",
                "file": str(target),
                "target": "demo",
                "messages": [
                    {"severity": "warning", "message": "ordinary proof warning"},
                    {"severity": "information", "message": begin.group(0)},
                    {
                        "severity": "information",
                        "message": "'demo' depends on axioms: [propext, Classical.choice]",
                    },
                    {"severity": "information", "message": end.group(0)},
                ],
            }

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert "#print axioms demo" in fake.replacement
    assert payload["ok"] is True
    assert payload["axiom_profile_checked"] is True
    assert payload["axiom_profile_axioms"] == ["Classical.choice", "propext"]
    assert payload["axiom_profile_target"] == "demo"
    assert payload["axiom_profile_requested_target"] == "demo"
    assert len(payload["axiom_profile_declaration_sha256"]) == 64
    assert payload["messages"] == [{"severity": "warning", "message": "ordinary proof warning"}]
    assert payload["output"] == "warning: ordinary proof warning"


def test_check_target_profiles_dotted_last_declaration_inside_namespace(monkeypatch, tmp_path):
    """A final namespaced theorem must not be queried after its closing ``end``."""
    target_name = "erdos_242.variants.schinzel_generalization"
    project, target = _write_project(
        tmp_path,
        "\n".join(
            (
                "import Mathlib",
                "",
                "namespace Erdos242",
                "",
                f"theorem {target_name} : True := by",
                "  trivial",
                "",
                "end Erdos242",
                "",
            )
        ),
    )

    class _FakeProbe:
        replacement = ""

        def check_target(self, *args, **kwargs):
            self.replacement = str(kwargs["replacement"])
            begin = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_BEGIN_[A-F0-9]+", self.replacement)
            end = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_END_[A-F0-9]+", self.replacement)
            assert begin is not None and end is not None
            assert self.replacement.index(f"#print axioms {target_name}") < (
                self.replacement.index("end Erdos242")
            )
            return {
                "success": True,
                "ok": True,
                "backend": "lean_interact",
                "tool": "lean_probe",
                "action": "check",
                "file": str(target),
                "target": target_name,
                "messages": [
                    {"severity": "information", "message": begin.group(0)},
                    {
                        "severity": "information",
                        "message": (
                            "'Erdos242.erdos_242.variants.schinzel_generalization' "
                            "does not depend on any axioms"
                        ),
                    },
                    {"severity": "information", "message": end.group(0)},
                ],
            }

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id=target_name,
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert payload["ok"] is True
    assert payload["axiom_profile_checked"] is True
    assert payload["axiom_profile_axioms"] == []
    assert payload["axiom_profile_target"] == target_name
    assert payload["axiom_profile_requested_target"] == target_name


def test_check_target_marks_incomplete_inline_axiom_profile_unavailable(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": True,
                "ok": True,
                "backend": "lean_interact",
                "tool": "lean_probe",
                "action": "check",
                "file": str(target),
                "target": "demo",
                "messages": [],
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert payload["ok"] is True
    assert payload["axiom_profile_checked"] is False
    assert payload["axiom_profile_axioms"] == []
    assert "missing or ambiguous" in payload["axiom_profile_error"]


def test_project_admission_reclaims_incremental_session_before_releasing_slot(
    monkeypatch, tmp_path
):
    """A research worker cannot leave its LSP resident after an admitted check."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        closed = False

        def check_target(self, *args, **kwargs):
            return {"success": True, "ok": True, "target": "demo"}

        def close(self):
            self.closed = True

    fake = _FakeProbe()
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(li, "_PROBE", fake)
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert payload["ok"] is True
    assert fake.closed is True
    assert payload["resource_admission"]["incremental_session_reclaimed"] is True
    assert set(payload["leanflow_timing"]) == {
        "total_s",
        "admission_wait_s",
        "probe_call_s",
        "session_reclaim_s",
        "postprocess_s",
    }
    assert all(value >= 0 for value in payload["leanflow_timing"].values())


def test_repeated_research_scratch_checks_recreate_closed_probe(monkeypatch, tmp_path):
    """Negation-like scratch probes must not retain a warm Lean child between calls."""
    project, _target = _write_project(tmp_path, "theorem demo : True := by trivial\n")
    probes = []

    class _FakeProbe:
        def __init__(self, *, auto_build=False):
            self.closed = False
            probes.append(self)

        def check_code(self, code, **kwargs):
            return {"success": True, "ok": True, "output": code}

        def close(self):
            self.closed = True

    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(li, "LeanProbe", _FakeProbe)
    monkeypatch.setattr(li, "_PROBE", None)

    first = li.lean_scratch_check("example : True := by trivial", cwd=str(project))
    second = li.lean_scratch_check("example : 1 = 1 := by rfl", cwd=str(project))

    assert first["ok"] is True and second["ok"] is True
    assert len(probes) == 2
    assert all(probe.closed for probe in probes)
    assert first["resource_admission"]["incremental_session_reclaimed"] is True
    assert second["resource_admission"]["incremental_session_reclaimed"] is True


def test_failed_scratch_close_retains_project_slot_truthfully(monkeypatch, tmp_path):
    """A failed LeanProbe close cannot be reported as reclaimed or unlock the slot."""
    project, _target = _write_project(tmp_path, "theorem demo : True := by trivial\n")

    class _FailingProbe:
        def check_code(self, code, **kwargs):
            return {"success": True, "ok": True}

        def close(self):
            raise RuntimeError("close failed")

    fake = _FailingProbe()
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(li, "_PROBE", fake)

    payload = li.lean_scratch_check("example : True := by trivial", cwd=str(project))

    resource = payload["resource_admission"]
    assert resource["incremental_session_reclaimed"] is False
    assert resource["retained_until_process_exit"] is True
    assert "close failed" in resource["retention_reason"]
    assert li._PROBE is fake


@pytest.mark.parametrize(
    ("requested_timeout_s", "expected_timeout_s", "adjusted"),
    [
        (60, li.DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S, True),
        (1200, 1200, False),
    ],
)
def test_dispatch_worker_applies_cold_start_timeout_floor(
    monkeypatch,
    tmp_path,
    requested_timeout_s,
    expected_timeout_s,
    adjusted,
):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        def __init__(self):
            self.timeout_s = 0

        def prepare_file(self, *args, **kwargs):
            self.timeout_s = kwargs["timeout_s"]
            return {"success": True, "ok": True, "action": "prepare_file"}

    fake = _FakeProbe()
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="prepare_file",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=requested_timeout_s,
    )

    assert fake.timeout_s == expected_timeout_s
    assert payload["requested_timeout_s"] == requested_timeout_s
    assert payload["effective_timeout_s"] == expected_timeout_s
    assert payload["timeout_adjusted"] is adjusted
    assert payload["timeout_policy"] == (
        "dispatch_worker_cold_start_floor" if adjusted else "requested"
    )


@pytest.mark.parametrize(
    ("requested_timeout_s", "expected_timeout_s", "adjusted"),
    [
        (60, li.RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S, True),
        (1200, 1200, False),
    ],
)
def test_foreground_research_applies_cold_start_timeout_floor(
    monkeypatch,
    tmp_path,
    requested_timeout_s,
    expected_timeout_s,
    adjusted,
):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        def __init__(self):
            self.timeout_s = 0

        def prepare_file(self, *args, **kwargs):
            self.timeout_s = kwargs["timeout_s"]
            return {"success": True, "ok": True, "action": "prepare_file"}

    fake = _FakeProbe()
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="prepare_file",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=requested_timeout_s,
    )

    assert fake.timeout_s == expected_timeout_s
    assert payload["requested_timeout_s"] == requested_timeout_s
    assert payload["effective_timeout_s"] == expected_timeout_s
    assert payload["timeout_adjusted"] is adjusted
    assert payload["timeout_policy"] == ("research_cold_start_floor" if adjusted else "requested")


def test_authoritative_timeout_ceiling_caps_research_cold_start_floor(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        timeout_s = 0

        def prepare_file(self, *args, **kwargs):
            self.timeout_s = kwargs["timeout_s"]
            return {"success": True, "ok": True, "action": "prepare_file"}

    fake = _FakeProbe()
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="prepare_file",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=60,
        timeout_ceiling_s=17,
    )

    assert fake.timeout_s == 17
    assert payload["requested_timeout_s"] == 60
    assert payload["effective_timeout_s"] == 17
    assert payload["timeout_adjusted"] is True
    assert payload["timeout_ceiling_s"] == 17
    assert payload["timeout_policy"] == "research_cold_start_floor_capped_by_deadline"


def test_check_target_labels_unrelated_declaration_as_scratch_replacement(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {"success": True, "ok": True, "target": "demo"}

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="private lemma helper : True := by\n  trivial\n",
    )

    assert payload["ok"] is True
    assert payload["replacement_matches_target"] is False
    assert payload["verification_scope"] == "scratch_replacement"
    assert payload["replacement_declarations"] == ["helper"]
    assert payload["replacement_mismatch_reason"] == (
        "replacement does not declare the assigned target"
    )


def test_check_helper_uses_existing_target_as_non_authoritative_anchor(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def __init__(self):
            self.kwargs = {}

        def check_target(self, *args, **kwargs):
            self.kwargs = kwargs
            return {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "target": "demo",
                "messages": [
                    {
                        "severity": "warning",
                        "message": "declaration uses 'sorry'",
                    }
                ],
            }

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li,
        "check_helper_ephemerally",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary foreground helper used the exact profile backend")
        ),
    )
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    helper = "private lemma helper : True := by\n  trivial"
    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=helper,
    )

    assert fake.kwargs["theorem_id"] == "demo"
    assert fake.kwargs["replacement"] == (helper + "\n\ntheorem demo : True := by\n  sorry")
    assert payload["action"] == "check_helper"
    assert payload["ok"] is True
    assert payload["valid_without_sorry"] is True
    assert payload["has_sorry"] is False
    assert payload["replacement_matches_target"] is False
    assert payload["replacement_declarations"] == ["helper"]
    assert payload["verification_scope"] == "helper_candidate"
    assert payload["anchor_target"] == "demo"
    assert payload["anchor_temporary_sorry"] is True
    assert payload["messages"] == []
    assert payload["anchor_messages"][0]["message"] == "declaration uses 'sorry'"


def test_dispatch_check_helper_uses_ephemeral_backend_without_repl(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    observed: dict[str, object] = {}

    def ephemeral(**kwargs):
        observed.update(kwargs)
        return {
            "success": True,
            "ok": True,
            "backend": "lean_exact_ephemeral",
            "tool": "lake_env_lean",
            "action": "check_helper",
            "file": str(target),
            "target": "demo",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": ["helper"],
            "axiom_profile_requested": True,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": [],
            "axiom_profile_blockers": [],
            "axiom_profile_error": "",
            "resource_admission": {"enforced": True},
        }

    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "1")
    monkeypatch.setattr(li, "check_helper_ephemerally", ephemeral)
    monkeypatch.setattr(
        li,
        "_local_repl_dir",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dispatch helper required REPL")),
    )
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: (_ for _ in ()).throw(AssertionError("dispatch helper started LeanProbe")),
    )

    helper = "private lemma helper : True := by\n  trivial"
    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=helper,
        timeout_s=60,
    )

    assert payload["ok"] is True
    assert payload["backend"] == "lean_exact_ephemeral"
    assert payload["axiom_profile_requested"] is True
    assert payload["axiom_profile_checked"] is True
    assert payload["axiom_profile_axioms"] == []
    assert payload["axiom_profile_blockers"] == []
    assert payload["effective_timeout_s"] == li.DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S
    assert observed["source_text"] == target.read_text(encoding="utf-8")
    assert observed["helper_source"] == helper
    assert observed["theorem_id"] == "demo"
    assert observed["file_path"] == target.resolve()
    assert observed["project_root"] == project.resolve()
    assert "theorem demo : True := by\n  sorry" in str(observed["anchor_skeleton"])


def test_foreground_profiled_check_helper_uses_exact_ephemeral_backend(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    observed: dict[str, object] = {}

    def ephemeral(**kwargs):
        observed.update(kwargs)
        return {
            "success": True,
            "ok": True,
            "backend": "lean_exact_ephemeral",
            "tool": "lake_env_lean",
            "action": "check_helper",
            "file": str(target),
            "target": "demo",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": ["helper"],
            "axiom_profile_requested": True,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": ["Classical.choice"],
            "axiom_profile_blockers": [],
            "axiom_profile_error": "",
        }

    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    monkeypatch.setattr(li, "check_helper_ephemerally", ephemeral)
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: (_ for _ in ()).throw(AssertionError("profiled helper started LeanProbe")),
    )
    monkeypatch.setattr(
        li,
        "_local_repl_dir",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("profiled helper required a project-local REPL")
        ),
    )

    helper = "private lemma helper : True := by\n  trivial"
    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=helper,
        include_axiom_profile=True,
        timeout_s=60,
    )

    assert payload["ok"] is True
    assert payload["backend"] == "lean_exact_ephemeral"
    assert payload["axiom_profile_requested"] is True
    assert payload["axiom_profile_checked"] is True
    assert payload["axiom_profile_axioms"] == ["Classical.choice"]
    assert payload["axiom_profile_blockers"] == []
    assert payload["replacement_matches_target"] is False
    assert payload["replacement_mismatch_reason"] == ""
    assert payload["effective_timeout_s"] == li.PROFILED_HELPER_TIMEOUT_FLOOR_S
    assert payload["timeout_policy"] == "profiled_helper_cold_start_floor"
    assert observed["helper_source"] == helper
    assert observed["theorem_id"] == "demo"


@pytest.mark.parametrize("output_truncated", [False, True])
def test_profiled_check_helper_preserves_elaboration_diagnostics(
    monkeypatch,
    tmp_path,
    output_truncated,
):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    diagnostics = (
        "warning: unrelated source warning\n"
        + ("context line\n" * 80)
        + "error: unsolved goals\ncase helper\n\u22a2 False"
    )
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.setattr(
        li,
        "check_helper_ephemerally",
        lambda **kwargs: {
            "success": False,
            "ok": False,
            "failure_kind": "lean_elaboration",
            "error_code": "helper_elaboration_failed",
            "error": diagnostics[:500],
            "output": diagnostics,
            "output_truncated": output_truncated,
            "action": "check_helper",
            "valid_without_sorry": False,
            "has_errors": True,
            "has_sorry": False,
            "axiom_profile_checked": False,
            "axiom_profile_axioms": [],
            "axiom_profile_blockers": [],
            "axiom_profile_error": "helper candidate has no auditable axiom result",
        },
    )

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="private lemma helper : False := by\n  contradiction",
        include_axiom_profile=True,
    )

    assert payload["ok"] is False
    assert payload["failure_kind"] == "lean_elaboration"
    assert payload["error_code"] == "helper_elaboration_failed"
    assert payload["error"] == diagnostics[:500]
    assert payload["output"] == diagnostics
    assert payload["output_truncated"] is output_truncated
    assert payload["replacement_matches_target"] is False
    assert payload["replacement_mismatch_reason"] == ""


def test_profiled_check_helper_fails_closed_on_incomplete_profile(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.setattr(
        li,
        "check_helper_ephemerally",
        lambda **kwargs: {
            "success": True,
            "ok": True,
            "action": "check_helper",
            "valid_without_sorry": True,
            "axiom_profile_checked": True,
            "axiom_profile_blockers": [],
        },
    )

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="private lemma helper : True := by\n  trivial",
        include_axiom_profile=True,
    )

    assert payload["ok"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["axiom_profile_requested"] is True
    assert payload["axiom_profile_checked"] is False
    assert payload["axiom_profile_axioms"] == []
    assert payload["error_code"] == "helper_axiom_profile_unavailable"


@pytest.mark.parametrize("action", ["prepare_file", "feedback"])
def test_axiom_profile_remains_unsupported_for_nonchecking_actions(monkeypatch, tmp_path, action):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: (_ for _ in ()).throw(AssertionError("unsupported profile started LeanProbe")),
    )

    payload = li.lean_incremental_check(
        action=action,
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert payload["ok"] is False
    assert payload["error_code"] == "inline_axiom_profile_unsupported_action"
    assert payload["error"] == ("axiom profiles require action=check_target or action=check_helper")


def test_dispatch_check_target_still_uses_leanprobe(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {"success": True, "ok": True, "target": "demo"}

    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.delenv("LEANFLOW_LOW_MEMORY", raising=False)
    monkeypatch.setattr(
        li,
        "check_helper_ephemerally",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("check_target routed to helper backend")
        ),
    )
    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(li, "_local_repl_dir", lambda root: root / ".lake" / "packages" / "repl")
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert payload["ok"] is True
    assert payload["backend"] == "lean_interact"


def test_check_helper_rejects_placeholder_and_missing_anchor(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "target": kwargs["theorem_id"],
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    placeholder = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="private lemma helper : True := by\n  sorry",
    )
    missing_anchor = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="missing",
        cwd=str(project),
        replacement="private lemma helper : True := by\n  trivial",
    )

    assert placeholder["ok"] is False
    assert placeholder["error_code"] == "helper_placeholder"
    assert placeholder["verification_scope"] == "helper_candidate"
    assert placeholder["lean_started"] is False
    assert missing_anchor["ok"] is False
    assert missing_anchor["error_code"] == "anchor_target_not_found"


def test_check_target_rejects_placeholder_before_starting_lean(monkeypatch, tmp_path):
    """Do not spend a full-source compile on an acceptance candidate with sorry."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    monkeypatch.setattr(li, "_probe", lambda: pytest.fail("placeholder candidate started Lean"))

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=(
            "theorem demo : True := by\n"
            '  have note : String := "sorry in a string is harmless"\n'
            "  sorry\n"
        ),
        include_axiom_profile=True,
        timeout_s=120,
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["error_code"] == "target_placeholder"
    assert payload["has_sorry"] is True
    assert payload["has_errors"] is False
    assert payload["replacement_matches_target"] is True
    assert payload["verification_scope"] == "target_candidate"
    assert payload["axiom_profile_requested"] is True
    assert payload["axiom_profile_checked"] is False
    assert payload["lean_started"] is False
    assert payload["leanflow_timing"]["probe_call_s"] == 0.0


def test_check_target_can_elaborate_placeholder_template_for_decomposition(monkeypatch, tmp_path):
    """Let internal decomposition parse templates without weakening acceptance."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    calls: list[dict[str, object]] = []

    class FakeProbe:
        def check_target(self, *args, **kwargs):
            kwargs["args"] = args
            calls.append(kwargs)
            return {
                "success": True,
                "ok": False,
                "errors": 0,
                "sorry": 1,
                "tool": "lean_probe",
                "action": "check_target",
                "file": str(target.resolve()),
                "target": "demo",
            }

    monkeypatch.setattr(li, "_probe", FakeProbe)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : True := by\n  sorry",
        allow_placeholders_for_elaboration=True,
        timeout_s=120,
    )

    assert len(calls) == 1
    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["replacement_matches_target"] is True
    assert payload["verification_scope"] == "target_candidate"


def test_check_target_placeholder_scan_ignores_comments_and_strings(monkeypatch, tmp_path):
    """Preserve valid candidates that only mention placeholder words as data."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {"success": True, "ok": True, "target": "demo"}

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=(
            "theorem demo : True := by\n"
            "  -- sorry is discussed here only\n"
            '  have note : String := "admit and sorryAx are text"\n'
            "  trivial\n"
        ),
    )

    assert payload["ok"] is True
    assert payload.get("lean_started") is not False


def test_check_target_rejects_same_name_with_a_different_statement(monkeypatch, tmp_path):
    """A checked scratch proposition cannot impersonate the assigned theorem."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {"success": True, "ok": True, "target": "demo"}

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : 1 = 1 := by\n  rfl\n",
    )

    assert payload["ok"] is True
    assert payload["replacement_matches_target"] is False
    assert payload["verification_scope"] == "scratch_replacement"
    assert payload["replacement_declarations"] == ["demo"]
    assert payload["replacement_mismatch_reason"] == (
        "replacement changes the assigned target statement"
    )


def test_prepare_and_feedback_delegate_to_matching_leanprobe_methods(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem demo : True := by",
                "  trivial",
                "",
            ]
        ),
    )

    class _FakeProbe:
        def __init__(self):
            self.calls = []

        def prepare_file(self, *args, **kwargs):
            self.calls.append(("prepare_file", args, kwargs))
            return {"success": True, "ok": True, "action": "prepare"}

        def feedback(self, *args, **kwargs):
            self.calls.append(("feedback", args, kwargs))
            return {"success": True, "ok": False, "action": "feedback", "tactics": []}

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    prepare = li.lean_incremental_check(
        action="prepare_file",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )
    feedback = li.lean_incremental_check(
        action="feedback",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : True := by\n  sorry\n",
    )

    assert prepare["action"] == "prepare_file"
    assert feedback["action"] == "feedback"
    assert [call[0] for call in fake.calls] == ["prepare_file", "feedback"]
    assert fake.calls[1][2]["replacement"] == "theorem demo : True := by\n  sorry\n"


def test_missing_local_repl_rejects_before_probe_call(monkeypatch, tmp_path):
    project, target = _write_project(tmp_path, "theorem demo : True := by\n  trivial\n")

    def _unexpected_probe():
        raise AssertionError("LeanProbe should not be called without project-local repl")

    monkeypatch.setattr(li, "_probe", _unexpected_probe)
    monkeypatch.setattr(li, "_local_repl_dir", lambda project_root: None)
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert payload["success"] is False
    assert payload["ok"] is False
    assert payload["error_code"] == "local_repl_missing"
    assert "leanflow project init" in payload["error"]


def test_capabilities_keep_leanflow_strict_local_repl_semantics(monkeypatch, tmp_path):
    project, _target = _write_project(tmp_path, "theorem demo : True := by\n  trivial\n")

    class _FakeProbe:
        def capabilities(self, cwd):
            return {
                "available": True,
                "active_sessions": [{"project_root": str(cwd), "file": "Demo/Main.lean"}],
                "code_sessions": ["proof-state"],
                "max_code_sessions": 16,
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(li, "_local_repl_dir", lambda project_root: None)
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_capabilities(project)

    assert payload["available"] is False
    assert payload["project_root"] == str(project.resolve())
    assert payload["active_sessions"] == [
        {"project_root": str(project.resolve()), "file": "Demo/Main.lean"}
    ]
    assert "local_repl_missing" in payload["degraded_codes"]


def test_bound_feedback_payload_trims_oversized_tactics():
    import json

    big = [{"goals": "x" * 400, "proof_state": "y" * 400} for _ in range(200)]
    bounded = li._bound_feedback_payload({"ok": False, "tactics": big}, max_chars=4000)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 4000
    assert bounded["tactics_truncated"]["total"] == 200
    assert 0 < len(bounded["tactics"]) < 200


def test_bound_feedback_payload_leaves_small_untouched():
    small = {"ok": True, "tactics": [{"goals": "g"}]}
    assert li._bound_feedback_payload(small, max_chars=16000) == small


def test_normalize_payload_only_bounds_feedback(monkeypatch):
    monkeypatch.setenv("LEANFLOW_INCREMENTAL_FEEDBACK_MAX_CHARS", "3000")
    big = [{"g": "x" * 400} for _ in range(50)]
    feedback = li._normalize_payload({"ok": False, "tactics": list(big)}, "feedback")
    assert "tactics_truncated" in feedback
    # Non-feedback actions are never trimmed.
    check = li._normalize_payload({"ok": False, "tactics": list(big)}, "check_target")
    assert "tactics_truncated" not in check


def test_failed_helper_check_bounds_replayed_diagnostics():
    import json

    payload = {
        "ok": False,
        "action": "check_helper",
        "messages": [{"severity": "error", "message": "m" * 5000} for _ in range(20)],
        "tactics": [{"goals": "g" * 3000, "proof_state": "p" * 3000} for _ in range(30)],
        "feedback_lean": "source\n" * 8000,
        "output": "output\n" * 2000,
    }

    bounded = li._bound_failed_check_payload(payload, max_chars=12_000)

    assert len(json.dumps(bounded, ensure_ascii=False)) <= 12_000
    assert bounded["diagnostic_payload_truncated"] is True
    assert bounded["messages_truncated"] == {"kept": 4, "total": 20}
    assert bounded["tactics_truncated"]["total"] == 30
    assert "diagnostic text truncated" in bounded["feedback_lean"]


def test_successful_check_keeps_complete_evidence():
    payload = {
        "ok": True,
        "action": "check_helper",
        "feedback_lean": "verified helper source",
    }

    assert li._bound_failed_check_payload(payload, max_chars=10) == payload
