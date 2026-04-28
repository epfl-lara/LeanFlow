from __future__ import annotations

from types import SimpleNamespace

import pytest

from epflemma_cli import lean_incremental as li


@pytest.fixture(autouse=True)
def _close_sessions():
    li.close_incremental_sessions()
    yield
    li.close_incremental_sessions()


def _write_project(tmp_path, text: str):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    target = module_dir / "Main.lean"
    target.write_text(text, encoding="utf-8")
    return project, target


def _install_fake_lean_interact(monkeypatch):
    servers = []

    class _Command:
        def __init__(self, *, cmd: str, all_tactics: bool = False, env=None):
            self.cmd = cmd
            self.all_tactics = all_tactics
            self.env = env

        def model_copy(self, *, update):
            copied = _Command(cmd=self.cmd, all_tactics=self.all_tactics, env=self.env)
            for key, value in update.items():
                setattr(copied, key, value)
            return copied

    class _Response:
        def __init__(self, *, env: int, errors: bool = False, tactics=None):
            self.env = env
            self.messages = []
            self.sorries = []
            self.tactics = tactics or []
            self._errors = errors
            if errors:
                pos = SimpleNamespace(line=1, column=7)
                self.messages.append(
                    SimpleNamespace(
                        severity="error",
                        data="unexpected token",
                        start_pos=pos,
                        end_pos=pos,
                    )
                )

        def has_errors(self):
            return self._errors

        def lean_code_is_valid(self, *, allow_sorry: bool = False):
            return not self._errors

    class _Server:
        def __init__(self, config):
            self.config = config
            self.runs = []
            servers.append(self)

        def run(self, request, timeout=None):
            self.runs.append(
                {
                    "cmd": request.cmd,
                    "env": request.env,
                    "all_tactics": request.all_tactics,
                    "timeout": timeout,
                }
            )
            if "bad" in request.cmd:
                return _Response(env=900 + len(self.runs), errors=True)
            tactics = []
            if request.all_tactics:
                pos = SimpleNamespace(line=1, column=0)
                tactics.append(
                    SimpleNamespace(
                        tactic="trivial",
                        goals="⊢ True",
                        proof_state="⊢ True",
                        start_pos=pos,
                        end_pos=pos,
                        used_constants=["True.intro"],
                    )
                )
            return _Response(env=100 + len(self.runs), tactics=tactics)

        def kill(self):
            pass

    class _Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Project:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(li, "_import_lean_interact", lambda: (_Command, _Config, _Server, _Project, ""))
    monkeypatch.setattr(li, "_local_repl_dir", lambda project_root: project_root / ".lake" / "packages" / "repl")
    return servers


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
                "  have s := \"def also_fake := 1\"",
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
    assert segments[1].start_line == 11


def test_check_target_reuses_header_and_prior_declaration_env(monkeypatch, tmp_path):
    servers = _install_fake_lean_interact(monkeypatch)
    project, target = _write_project(
        tmp_path,
        "\n".join(
            [
                "import Mathlib",
                "",
                "theorem first : True := by",
                "  trivial",
                "",
                "theorem second : True := by",
                "  trivial",
                "",
            ]
        ),
    )

    first = li.lean_incremental_check(action="check_target", file_path=str(target), theorem_id="second", cwd=str(project))
    second = li.lean_incremental_check(action="check_target", file_path=str(target), theorem_id="second", cwd=str(project))

    assert first["ok"] is True
    assert first["cache"]["cache_hit"] is False
    assert second["ok"] is True
    assert second["cache"]["cache_hit"] is True
    assert [run["cmd"].strip().splitlines()[0] for run in servers[0].runs] == [
        "import Mathlib",
        "theorem first : True := by",
        "theorem second : True := by",
        "theorem second : True := by",
    ]
    assert servers[0].runs[2]["env"] == 102
    assert servers[0].runs[3]["env"] == 102


def test_check_target_reports_chunk_and_file_locations_on_failure(monkeypatch, tmp_path):
    servers = _install_fake_lean_interact(monkeypatch)
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

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
        replacement="theorem demo : True := by\n  bad\n",
    )

    assert payload["success"] is True
    assert payload["ok"] is False
    assert payload["has_errors"] is True
    assert payload["messages"][0]["start"] == {"line": 1, "column": 7}
    assert payload["messages"][0]["file_start"] == {"line": 3, "column": 7}
    assert "/- <feedback>" in payload["feedback_lean"]
    assert servers[0].runs[-1]["all_tactics"] is True


def test_check_target_restarts_dead_lean_server_once(monkeypatch, tmp_path):
    servers = _install_fake_lean_interact(monkeypatch)
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

    original_run = servers
    del original_run
    failures = {"remaining": 1}

    class _DeadOnce:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def run(self, request, timeout=None):
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise RuntimeError("The Lean server is not running.")
            return self._wrapped.run(request, timeout=timeout)

    original_new_session = li._new_session

    def _new_session(project_root, file_path, repl_dir):
        session, error = original_new_session(project_root, file_path, repl_dir)
        if session is not None:
            session.server = _DeadOnce(session.server)
        return session, error

    monkeypatch.setattr(li, "_new_session", _new_session)

    payload = li.lean_incremental_check(
        action="check_target",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(project),
    )

    assert payload["success"] is True
    assert payload["ok"] is True
    assert failures["remaining"] == 0
