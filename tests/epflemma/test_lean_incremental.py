from __future__ import annotations

from pathlib import Path

import pytest

from epflemma_cli.lean import lean_incremental as li


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

    assert [(segment.kind, segment.name) for segment in segments] == [("mutual", ""), ("theorem", "after")]
    assert "def evenish" in segments[0].text
    assert "def oddish" in segments[0].text


def test_check_target_delegates_to_leanprobe_and_preserves_epflemma_action(monkeypatch, tmp_path):
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
    monkeypatch.setattr(li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl")
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
    monkeypatch.setattr(li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl")
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
    assert "epflemma project init" in payload["error"]


def test_capabilities_keep_epflemma_strict_local_repl_semantics(monkeypatch, tmp_path):
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
    assert payload["active_sessions"] == [{"project_root": str(project.resolve()), "file": "Demo/Main.lean"}]
    assert "local_repl_missing" in payload["degraded_codes"]
