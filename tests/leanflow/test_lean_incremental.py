from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from leanflow_cli.lean import lean_incremental as li
from leanflow_cli.lean.lean_probe_deadline import (
    LeanProbeDeadlineExceeded,
    call_lean_probe_with_deadline,
)


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


def test_instrumented_target_payload_cannot_verify_production_target():
    payload = li._mark_inspection_only_target_payload(
        {
            "success": True,
            "ok": True,
            "valid_without_sorry": True,
            "target_verified": True,
            "has_errors": False,
        }
    )

    assert payload["ok"] is False
    assert payload["target_verified"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["diagnostic_only"] is True
    assert payload["proof_progress"] is False
    assert payload["status"] == "inspection_only_target"


def test_suggestion_rejection_payload_is_diagnostic_only(tmp_path: Path):
    payload = li._suggestion_rejection_payload(
        action="check_target",
        file_path=tmp_path / "Main.lean",
        theorem_id="demo",
        replacement_metadata={"replacement_matches_target": True},
        requested_timeout_s=60,
        effective_timeout_s=60,
        timeout_adjusted=False,
        timeout_policy="requested",
        timeout_ceiling_s=None,
        operation_started=time.monotonic(),
    )

    assert payload["ok"] is False
    assert payload["target_verified"] is False
    assert payload["diagnostic_only"] is True
    assert payload["proof_progress"] is False
    assert payload["status"] == "suggestion_tactic_diagnostic_only"


def test_probe_outer_deadline_bounds_a_call_that_ignores_its_timeout():
    release = threading.Event()

    class _HangingProbe:
        def check_target(self):
            release.wait()

    started = time.monotonic()
    with pytest.raises(LeanProbeDeadlineExceeded) as captured:
        call_lean_probe_with_deadline(
            _HangingProbe(),
            "check_target",
            deadline_s=0.03,
            shutdown_grace_s=0.01,
        )
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2
    assert captured.value.worker_stopped is False


def test_probe_outer_deadline_terminates_owned_session_and_reaps_worker():
    release = threading.Event()

    class _Server:
        def kill(self):
            release.set()

    class _Session:
        server = _Server()

    class _HangingProbe:
        _sessions = {"target": _Session()}
        _code_sessions = {}
        _scratch_sessions = {}

        def check_target(self):
            release.wait()

    with pytest.raises(LeanProbeDeadlineExceeded) as captured:
        call_lean_probe_with_deadline(
            _HangingProbe(),
            "check_target",
            deadline_s=0.03,
            shutdown_grace_s=0.1,
        )

    assert captured.value.sessions_terminated is True
    assert captured.value.worker_stopped is True


def test_probe_shutdown_accepts_verified_process_death_after_cleanup_race():
    release = threading.Event()

    class _Server:
        alive = True

        def kill(self):
            self.alive = False
            release.set()
            raise ValueError("stream already closed")

        def is_alive(self):
            return self.alive

    class _Session:
        server = _Server()

    class _HangingProbe:
        _sessions = {"target": _Session()}
        _code_sessions = {}
        _scratch_sessions = {}

        def check_target(self):
            release.wait()

    with pytest.raises(LeanProbeDeadlineExceeded) as captured:
        call_lean_probe_with_deadline(
            _HangingProbe(),
            "check_target",
            deadline_s=0.03,
            shutdown_grace_s=0.1,
        )

    assert captured.value.sessions_terminated is True
    assert captured.value.worker_stopped is True


def test_incremental_check_returns_retryable_payload_on_probe_outer_timeout(monkeypatch, tmp_path):
    project, target = _write_project(tmp_path, "theorem demo : True := by\n  trivial\n")

    class _DeadlineProbe:
        def check_target(self, *args, **kwargs):
            raise LeanProbeDeadlineExceeded(
                kwargs["timeout_s"],
                worker_stopped=True,
                sessions_terminated=True,
            )

    fake = _DeadlineProbe()
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(li, "_PROBE", fake)
    monkeypatch.setattr(li, "_PROBE_EVER_STARTED", True)
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(li, "_local_repl_dir", lambda root: root / ".lake" / "build")
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=7,
    )

    assert payload["timed_out"] is True
    assert payload["retryable"] is True
    assert payload["error_code"] == "lean_probe_wall_clock_timeout"
    assert payload["probe_worker_stopped"] is True
    assert payload["resource_admission"]["incremental_session_reclaimed"] is True
    assert li._PROBE is None

    retry = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=7,
    )

    assert retry["effective_timeout_s"] == li.RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S
    assert retry["timeout_policy"] == "research_cold_start_floor"


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


def test_check_file_uses_cached_probe_declaration_replay(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def __init__(self):
            self.calls = []

        def check_target(self, file_path, **kwargs):
            self.calls.append((file_path, kwargs))
            return {
                "success": True,
                "ok": False,
                "has_errors": False,
                "has_sorry": True,
                "valid_without_sorry": False,
                "action": "check",
                "cache": {"reused_server": True},
            }

    fake = _FakeProbe()
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_file",
        file_path=str(target),
        cwd=str(project),
        timeout_s=17,
    )

    assert payload["success"] is True
    assert payload["action"] == "check_file"
    assert payload["has_errors"] is False
    assert payload["has_sorry"] is True
    assert payload["cache"]["reused_server"] is True
    assert fake.calls == [
        (
            target,
            {
                "theorem_id": "demo",
                "cwd": project,
                "replacement": "",
                "include_tactics": True,
                "timeout_s": 17,
            },
        )
    ]


def test_check_file_uses_canonical_fallback_after_prefix_build_failure(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem prior : True := by\n  trivial\n\n"
        "theorem open_target : True := by\n  sorry\n",
    )
    checked_sources: list[str] = []

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": False,
                "ok": False,
                "error_code": "prior_decl_failed",
                "error": "failed to build env before target at prior: unexpected end of input",
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    def exact_check(source, **kwargs):
        checked_sources.append(source)
        return {"success": True, "ok": True, "output": "", "messages": []}

    monkeypatch.setattr(li, "lean_ephemeral_source_check", exact_check)

    payload = li.lean_incremental_check(
        action="check_file",
        file_path=str(target),
        cwd=str(project),
    )

    assert checked_sources == [target.read_text(encoding="utf-8")]
    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["has_errors"] is False
    assert payload["has_sorry"] is True
    assert payload["canonical_fallback"] is True
    assert payload["backend"] == "lean_exact_ephemeral"
    assert payload["incremental_fallback_error_code"] == "prior_decl_failed"


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


def test_segment_file_recognizes_same_line_set_option_theorem():
    source = "\n".join(
        [
            "import Mathlib",
            "",
            "set_option maxHeartbeats 1000000 in theorem result : True := by",
            "  trivial",
            "",
        ]
    )

    header, segments = li._segment_file(source)

    assert header == "import Mathlib\n\n"
    assert [segment.name for segment in segments] == ["result"]
    assert segments[0].text.startswith("set_option maxHeartbeats 1000000 in theorem result")
    assert source[segments[0].start : segments[0].end] == segments[0].text
    entries = li._declaration_line_index_from_text(source)
    assert [(entry["kind"], entry["name"], entry["line"]) for entry in entries] == [
        ("theorem", "result", 3)
    ]


def test_segment_file_attaches_stacked_option_wrappers_to_private_theorem():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem first : True := by",
                "  trivial",
                "",
                "set_option maxHeartbeats 50000000 in",
                "set_option maxRecDepth 100000 in",
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
    assert segments[1].text.startswith("set_option maxHeartbeats 50000000 in")
    assert "set_option maxRecDepth 100000 in" in segments[1].text


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


def test_segment_file_attaches_open_scoped_wrapper_to_declaration():
    header, segments = li._segment_file(
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem first : True := by",
                "  trivial",
                "",
                "open scoped Classical in",
                "def wrapped : Nat := 1",
                "",
                "theorem last : True := by",
                "  trivial",
                "",
            ]
        )
    )

    assert header == "import Mathlib\n"
    assert [segment.name for segment in segments] == ["first", "wrapped", "last"]
    assert "open scoped Classical in" not in segments[0].text
    assert segments[1].text.startswith("open scoped Classical in")
    assert segments[1].declaration_start > segments[1].start


def test_segment_file_normalizes_inline_open_scoped_abbrev():
    header, segments = li._segment_file(
        "import Mathlib\n\nopen scoped Classical in abbrev Wrapped := Nat\n"
    )

    assert header == "import Mathlib\n\n"
    assert [segment.name for segment in segments] == ["Wrapped"]
    assert segments[0].text.startswith("open scoped Classical in ")
    assert segments[0].declaration_start > segments[0].start


def test_probe_uses_scoped_declaration_segment_repairs(monkeypatch):
    source = "\n".join(
        [
            "def first : Nat := 1",
            "",
            "variable (P : Type) in",
            "/-- A scoped alias. -/",
            "abbrev Scoped := P",
            "",
            "theorem last : True := by",
            "  trivial",
            "",
        ]
    )

    class _FakeProbe:
        def __init__(self, *, auto_build):
            assert auto_build is False

    import lean_probe.probe as lean_probe_runtime

    monkeypatch.setattr(li, "_PROBE", None)
    monkeypatch.setattr(li, "LeanProbe", _FakeProbe)
    monkeypatch.setattr(lean_probe_runtime, "segment_file", li._probe_segment_file)

    li._probe()

    _header, segments = lean_probe_runtime.segment_file(source)
    assert [segment.name for segment in segments] == ["first", "Scoped", "last"]
    assert "variable (P : Type) in" not in segments[0].text
    assert segments[1].text.startswith("variable (P : Type) in")


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


def test_profiled_check_target_uses_canonical_fallback_after_prefix_build_failure(
    monkeypatch, tmp_path
):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\n/-- Demo documentation. -/\ntheorem demo : True := by\n  trivial\n",
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
        assert source.count("/-- Demo documentation. -/") == 1
        begin = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_BEGIN_[A-F0-9]+", source)
        end = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_END_[A-F0-9]+", source)
        assert begin is not None and end is not None
        return {
            "success": True,
            "ok": True,
            "output": "\n".join(
                (
                    f'"{begin.group(0)}" : String',
                    "'demo' depends on axioms: [propext]",
                    f'"{end.group(0)}" : String',
                )
            ),
            "messages": [],
        }

    monkeypatch.setattr(li, "lean_ephemeral_source_check", exact_check)

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert checked_sources
    assert payload["success"] is True
    assert payload["ok"] is True
    assert payload["canonical_fallback"] is True
    assert payload["axiom_profile_checked"] is True
    assert payload["axiom_profile_axioms"] == ["propext"]


def test_target_replacement_without_preamble_preserves_original_preamble(tmp_path):
    _project, target = _write_project(
        tmp_path,
        "import Mathlib\n\n/-- Demo documentation. -/\ntheorem demo : True := by\n  sorry\n",
    )
    source = target.read_text(encoding="utf-8")

    replaced = li._target_replaced_source(
        source,
        theorem_id="demo",
        replacement="theorem demo : True := by\n  trivial",
    )

    assert replaced is not None
    integrated, has_placeholder = replaced
    assert integrated.count("/-- Demo documentation. -/") == 1
    assert "theorem demo : True := by\n  trivial" in integrated
    assert has_placeholder is False


def test_profiled_canonical_fallback_retains_exact_error_diagnostic(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

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
        begin = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_BEGIN_[A-F0-9]+", source)
        end = re.search(r"LEANFLOW_INCREMENTAL_AXIOMS_END_[A-F0-9]+", source)
        assert begin is not None and end is not None
        return {
            "success": False,
            "ok": False,
            "timed_out": False,
            "failure_kind": "lean_elaboration",
            "error": "Main.lean:8:2: error(lean.unsolvedGoals): unsolved goals",
            "output": "\n".join(
                (
                    "Main.lean:3:1: warning: earlier warning",
                    "Main.lean:8:2: error(lean.unsolvedGoals): unsolved goals",
                    f'"{begin.group(0)}" : String',
                    "'demo' depends on axioms: [sorryAx]",
                    f'"{end.group(0)}" : String',
                )
            ),
            "messages": [],
        }

    monkeypatch.setattr(li, "lean_ephemeral_source_check", exact_check)

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        include_axiom_profile=True,
    )

    assert payload["ok"] is False
    assert payload["axiom_profile_checked"] is True
    assert payload["messages"][0] == {
        "severity": "warning",
        "message": "earlier warning",
        "line": 3,
    }
    assert payload["messages"][1] == {
        "severity": "error",
        "message": "unsolved goals",
        "line": 8,
    }


def test_feedback_uses_canonical_fallback_after_prefix_build_failure(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    checked_sources = []

    class _FakeProbe:
        def feedback(self, *args, **kwargs):
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
            "success": False,
            "ok": False,
            "timed_out": False,
            "failure_kind": "lean_elaboration",
            "error": "unsolved goals",
            "output": "unsolved goals\n⊢ True",
            "messages": [],
        }

    monkeypatch.setattr(li, "lean_ephemeral_source_check", exact_check)

    replacement = "theorem demo : True := by\n  exact ?_"
    payload = li.lean_incremental_check(
        action="feedback",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=replacement,
    )

    assert checked_sources
    assert replacement in checked_sources[0]
    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["canonical_fallback"] is True
    assert payload["diagnostic_only"] is True
    assert "⊢ True" in payload["feedback_lean"]
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
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
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


def test_project_admission_wait_consumes_the_end_to_end_probe_deadline(monkeypatch, tmp_path):
    """Do not grant a fresh full probe timeout after waiting for admission."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _Admission:
        def __enter__(self):
            time.sleep(0.08)
            return self

        def __exit__(self, *_args):
            return False

        def to_dict(self):
            return {}

        def retain_until_process_exit(self, _reason):
            return None

    class _FakeProbe:
        timeout_s = 0.0

        def check_target(self, *args, **kwargs):
            self.timeout_s = float(kwargs["timeout_s"])
            return {"success": True, "ok": True, "target": "demo"}

    fake = _FakeProbe()
    monkeypatch.setattr(li, "project_lean_heavy_admission", lambda _root: _Admission())
    monkeypatch.setattr(li, "project_lean_service_reclaim_enabled", lambda: False)
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(li, "_local_repl_dir", lambda root: root / ".lake" / "packages" / "repl")
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=1,
    )

    assert payload["ok"] is True
    assert 0.5 < fake.timeout_s < 0.98


def test_scheduler_scale_admission_jitter_does_not_shave_timeout_floor(monkeypatch, tmp_path):
    """Keep a stable probe timeout when lock setup incurs only tiny jitter."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _Admission:
        def __enter__(self):
            time.sleep(0.02)
            return self

        def __exit__(self, *_args):
            return False

        def to_dict(self):
            return {}

        def retain_until_process_exit(self, _reason):
            return None

    class _FakeProbe:
        timeout_s = 0.0

        def check_target(self, *args, **kwargs):
            self.timeout_s = float(kwargs["timeout_s"])
            return {"success": True, "ok": True, "target": "demo"}

    fake = _FakeProbe()
    monkeypatch.setattr(li, "project_lean_heavy_admission", lambda _root: _Admission())
    monkeypatch.setattr(li, "project_lean_service_reclaim_enabled", lambda: False)
    monkeypatch.setattr(li, "_probe", lambda: fake)
    monkeypatch.setattr(li, "_local_repl_dir", lambda root: root / ".lake" / "packages" / "repl")
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        timeout_s=1,
    )

    assert payload["ok"] is True
    assert fake.timeout_s == 1.0


def test_foreground_incremental_session_stays_warm_between_checks(monkeypatch, tmp_path):
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  trivial\n",
    )

    class _FakeProbe:
        closed = False

        def check_target(self, *args, **kwargs):
            return {
                "success": True,
                "ok": True,
                "target": "demo",
                "cache": {"cache_hit": True},
            }

        def close(self):
            self.closed = True

    fake = _FakeProbe()
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
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
    assert fake.closed is False
    assert payload["resource_admission"]["incremental_session_reclaimed"] is False
    assert payload["cache"]["cache_hit"] is True


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
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
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
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
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
    monkeypatch.setattr(li, "_PROBE", None)
    monkeypatch.setattr(li, "_PROBE_EVER_STARTED", False)
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


def test_foreground_research_honors_requested_timeout_after_probe_warmup(monkeypatch, tmp_path):
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
    monkeypatch.setattr(li, "_PROBE", fake)
    monkeypatch.setattr(li, "_PROBE_EVER_STARTED", True)
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
    )

    assert fake.timeout_s == 60
    assert payload["effective_timeout_s"] == 60
    assert payload["timeout_adjusted"] is False
    assert payload["timeout_policy"] == "requested"


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
    monkeypatch.setattr(li, "_PROBE", None)
    monkeypatch.setattr(li, "_PROBE_EVER_STARTED", False)
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


def test_run_hard_timeout_caps_research_incremental_cold_start(monkeypatch, tmp_path):
    """Apply the explicit run-wide Lean cap to LeanProbe as well as Lake commands."""
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
    monkeypatch.setenv("LEANFLOW_LEAN_COMMAND_HARD_TIMEOUT_S", "600")
    monkeypatch.setattr(li, "_PROBE", None)
    monkeypatch.setattr(li, "_PROBE_EVER_STARTED", False)
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
    )

    assert 599 < fake.timeout_s <= 600
    assert payload["effective_timeout_s"] == 600
    assert payload["timeout_ceiling_s"] == 600
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
    assert payload["error"] == "unsolved goals"
    assert payload["output"] == diagnostics
    assert {"severity": "error", "message": "unsolved goals"} in payload["messages"]
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


@pytest.mark.parametrize(
    "tactic",
    ["exact?", "apply?", "simp?", "rw?", "aesop?", "grind?"],
)
def test_check_helper_never_certifies_suggestion_tactics(monkeypatch, tmp_path, tactic):
    """Keep diagnostic tactic suggestions out of helper-verification truth fields."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: pytest.fail("diagnostic-only helper suggestion started LeanProbe"),
    )

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=f"private lemma helper : True := by\n  {tactic}",
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["has_errors"] is False
    assert payload["has_sorry"] is False
    assert payload["lean_started"] is False
    assert payload["error_code"] == "suggestion_tactic_diagnostic_only"
    assert payload["verification_scope"] == "helper_candidate"


@pytest.mark.parametrize(
    ("helper_name", "setup"),
    [
        ("probe_nat_succ", "  have h := @Nat.succ_eq_add_one\n"),
        (
            "probe_nat_succ",
            "  letI : Inhabited Nat := ⟨0⟩\n  have h : Nat.succ 0 = 1 := by decide\n",
        ),
        ("test_nat_succ", "  have h := @Nat.succ_eq_add_one\n"),
    ],
)
def test_check_helper_marks_dummy_type_probe_as_diagnostic_only(
    monkeypatch, tmp_path, helper_name, setup
):
    """Preserve type diagnostics without certifying a trivial inspection wrapper."""
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
                "messages": [
                    {
                        "severity": "info",
                        "message": "h : Nat.succ 0 = 1\n⊢ True",
                    }
                ],
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=(
            f"private lemma {helper_name} : True := by\n" f"{setup}" "  trace_state\n" "  trivial"
        ),
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["diagnostic_only"] is True
    assert payload["proof_progress"] is False
    assert payload["helper_elaborated"] is True
    assert payload["inspection_completed"] is True
    assert payload["error_code"] == "inspection_only_helper_candidate"
    assert payload["verification_scope"] == "helper_candidate"
    assert payload["messages"][0]["message"].startswith("h :")
    assert "mathematically named helper" in payload["action_required"]
    assert "context compression" in payload["action_required"]


def test_check_helper_marks_failed_bare_term_type_probe_as_diagnostic_only(monkeypatch, tmp_path):
    """Charge inspection wrappers that expose a declaration type via mismatch."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": True,
                "ok": False,
                "has_errors": True,
                "has_sorry": False,
                "target": kwargs["theorem_id"],
                "messages": [
                    {
                        "severity": "error",
                        "message": (
                            "Type mismatch: existing_declaration has type Nat -> Nat "
                            "but is expected to have type True"
                        ),
                    }
                ],
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=(
            "private lemma probe_existing_type {P : Type*} : True := by\n"
            "  exact existing_declaration (P := P)"
        ),
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["diagnostic_only"] is True
    assert payload["proof_progress"] is False
    assert payload["error_code"] == "inspection_only_helper_candidate"


def test_check_helper_marks_traced_nontrivial_failure_probe_as_diagnostic_only(
    monkeypatch, tmp_path
):
    """Charge a traced mismatch wrapper even when its proposition is substantive."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )

    class _FakeProbe:
        def check_target(self, *args, **kwargs):
            return {
                "success": True,
                "ok": False,
                "has_errors": True,
                "has_sorry": False,
                "target": kwargs["theorem_id"],
                "messages": [
                    {
                        "severity": "error",
                        "message": "Type mismatch: h has type True but is expected to have type False",
                    }
                ],
            }

    monkeypatch.setattr(li, "_probe", lambda: _FakeProbe())
    monkeypatch.setattr(
        li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl"
    )
    monkeypatch.setattr(li, "_LEAN_PROBE_IMPORT_ERROR", "")

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement=(
            "private lemma result_hstep_probe (h : True) : False := by\n"
            "  have hrev := h\n"
            "  trace_state\n"
            "  fail_if_success done\n"
            "  exact h\n"
        ),
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["diagnostic_only"] is True
    assert payload["proof_progress"] is False
    assert payload["error_code"] == "inspection_only_helper_candidate"


def test_constructive_traced_helper_is_not_inspection_only():
    """Keep traced helpers constructive without the deliberate failure-probe shape."""
    replacement = (
        "private lemma result_hstep_probe (h : True) : True := by\n" "  trace_state\n" "  exact h\n"
    )

    assert li._is_lean_inspection_only_helper_candidate(replacement) is False


def test_bound_declaration_head_probe_is_inspection_only():
    """Recognize bare declaration-head mismatches under a substantive wrapper."""
    replacement = (
        "private lemma result_hstep_type_probe (h : True) : False := by\n"
        "  have z := @Strategy.Winning (V := V) (P := P)\n"
        "  exact z\n"
    )

    assert li._is_lean_inspection_only_helper_candidate(replacement) is True


def test_assigned_target_bound_declaration_head_probe_is_inspection_only():
    """Do not count a whole-target declaration-signature probe as a proof attempt."""
    replacement = (
        "theorem result (h : True) : False := by\n"
        "  have inspected := @existing_bridge\n"
        "  exact inspected\n"
    )

    assert li._is_lean_inspection_only_target_candidate(replacement) is True


def test_assigned_target_constructive_binding_is_not_inspection_only():
    """Keep an applied local theorem binding eligible as a real target proof."""
    replacement = (
        "theorem result (h : True) : True := by\n"
        "  have resolved := existing_bridge h\n"
        "  exact resolved\n"
    )

    assert li._is_lean_inspection_only_target_candidate(replacement) is False


def test_trivial_binding_probe_is_inspection_only_without_probe_name():
    """Recognize semantic signature wrappers even when their name says ``try``."""
    replacement = (
        "private lemma try_branch_signature (h : True) : True := by\n"
        "  have dependency := h\n"
        "  trivial\n"
    )

    assert li._is_lean_inspection_only_helper_candidate(replacement) is True


def test_nontrivial_true_helper_is_not_inspection_only_without_probe_name():
    """Keep a helper constructive when its body actually uses its hypothesis."""
    replacement = "private lemma preserve_true (h : True) : True := by\n  exact h\n"

    assert li._is_lean_inspection_only_helper_candidate(replacement) is False


@pytest.mark.parametrize(
    "statement",
    [
        "WinsNow t theta ↔ WinsNow t theta",
        "f x = f x",
    ],
)
def test_reflexive_helper_is_nonadvancing_regardless_of_name(statement):
    """Keep reflexive facts out of mandatory helper production."""
    replacement = (
        "private lemma mathematically_named (t theta f x : Nat) : " f"{statement} := by\n  rfl"
    )

    assert li._is_lean_inspection_only_helper_candidate(replacement) is True


def test_check_helper_rejects_broad_print_prefix_before_lean(monkeypatch, tmp_path):
    """Steer exact symbol inspection away from slow environment-prefix dumps."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: pytest.fail("broad print started LeanProbe"),
    )

    payload = li.lean_incremental_check(
        action="check_helper",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="#print prefix List\nprivate lemma inspect : True := by trivial",
    )

    assert payload["ok"] is False
    assert payload["status"] == "bounded_symbol_inspection_required"
    assert payload["error_code"] == "broad_print_prefix_rejected"
    assert payload["lean_started"] is False


def test_check_target_never_accepts_suggestion_tactic_candidate(monkeypatch, tmp_path):
    """Require a concrete term after suggestion discovery before target acceptance."""
    project, target = _write_project(
        tmp_path,
        "import Mathlib\n\ntheorem demo : True := by\n  sorry\n",
    )
    monkeypatch.setattr(
        li,
        "_probe",
        lambda: pytest.fail("diagnostic-only target suggestion started LeanProbe"),
    )

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : True := by\n  apply?",
    )

    assert payload["ok"] is False
    assert payload["valid_without_sorry"] is False
    assert payload["lean_started"] is False
    assert payload["error_code"] == "suggestion_tactic_diagnostic_only"


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
    assert payload["target_verified"] is False
    assert payload["local_elaboration_only"] is True
    assert payload["elaborated_with_placeholders"] is True
    assert payload["has_sorry"] is True
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


def test_normalize_payload_marks_diagnostic_heartbeat_timeout():
    payload = li._normalize_payload(
        {
            "success": True,
            "ok": False,
            "timed_out": False,
            "output": (
                "error: (deterministic) timeout at `whnf`, maximum number of "
                "heartbeats (200000) has been reached"
            ),
            "messages": [
                {
                    "severity": "error",
                    "message": "maximum number of heartbeats has been reached",
                }
            ],
        },
        "feedback",
    )

    assert payload["timed_out"] is True
    assert payload["timed_out_inferred_from_diagnostics"] is True


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


def test_failed_check_payload_keeps_late_error_before_earlier_warnings():
    payload = {
        "ok": False,
        "action": "check_target",
        "messages": [
            *[{"severity": "warning", "message": f"warning {index}"} for index in range(12)],
            {"severity": "error", "message": "actual blocker"},
        ],
        "output": "x" * 30_000,
    }

    bounded = li._bound_failed_check_payload(payload, max_chars=4_000)

    assert bounded["messages"][0] == {
        "severity": "error",
        "message": "actual blocker",
    }
    assert bounded["messages_truncated"]["total"] == 13
    assert bounded["error"] == "actual blocker"


def test_successful_check_keeps_complete_evidence():
    payload = {
        "ok": True,
        "action": "check_helper",
        "feedback_lean": "verified helper source",
    }

    assert li._bound_failed_check_payload(payload, max_chars=10) == payload


def test_successful_check_projection_drops_tactic_trace_but_keeps_audit_identity():
    payload = {
        "success": True,
        "ok": True,
        "action": "check_helper",
        "target": "demo",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "replacement_declarations": ["checked_helper"],
        "leanflow_timing": {"total_s": 0.5},
        "tactics": [{"goal": "x" * 2000} for _ in range(80)],
        "feedback_lean": "verified\n" + "trace" * 4000,
    }

    projected = li.compact_successful_check_payload(payload, max_chars=4000)

    assert projected["ok"] is True
    assert projected["replacement_declarations"] == ["checked_helper"]
    assert "tactics" not in projected
    assert projected["tactics_truncated"] == {"kept": 0, "total": 80}
    assert projected["provider_context_projected"] is True
    assert projected["audit_payload_preserved"] is True
    assert projected["audit_payload_chars"] > 100_000
    assert len(projected["audit_payload_sha256"]) == 64
    assert len(json.dumps(projected, ensure_ascii=False)) < 4000


def test_failed_check_projection_is_error_first_and_drops_replayed_source():
    import json

    payload = {
        "success": True,
        "ok": False,
        "action": "check_helper",
        "target": "demo",
        "valid_without_sorry": False,
        "has_errors": True,
        "has_sorry": False,
        "messages": [
            {"severity": "warning", "message": "unrelated warning"},
            {"severity": "error", "message": "unsolved goals\n" + "goal " * 1000},
        ],
        "tactics": [
            {"goals": "first useful goal", "proof_state": "trace" * 10_000},
            {"goals": "second useful goal", "proof_state": "trace" * 10_000},
        ],
        "feedback_lean": "complete repeated helper source\n" * 10_000,
        "resource_admission": {"large": "metadata" * 10_000},
    }

    projected = li.compact_check_payload(payload, max_chars=4000)

    assert projected["verification_status"] == "not_verified"
    assert projected["messages"][0]["severity"] == "error"
    assert projected["actionable_error"].startswith("unsolved goals")
    assert projected["relevant_goals"] == ["first useful goal", "second useful goal"]
    assert projected["tactics_truncated"] == {"kept": 0, "total": 2}
    assert "tactics" not in projected
    assert "feedback_lean" not in projected
    assert "resource_admission" not in projected
    assert projected["audit_payload_preserved"] is True
    assert len(json.dumps(projected, ensure_ascii=False)) <= 4000
