"""Tests for tools/file_operations.py — deny list, result dataclasses, helpers."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from leanflow_cli.lean.lean_statement_guard import ALLOW_STATEMENT_EDITS_ENV
from tools.implementations.file_operations import (
    MAX_LINE_LENGTH,
    LintResult,
    PatchResult,
    ReadResult,
    SearchMatch,
    SearchResult,
    ShellFileOperations,
    WriteResult,
    _explicit_package_dependency_search,
    _is_write_denied,
)
from tools.utilities.workflow_artifact_guard import WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV

# =========================================================================
# Write deny list
# =========================================================================


class TestIsWriteDenied:
    def test_ssh_authorized_keys_denied(self):
        path = os.path.join(str(Path.home()), ".ssh", "authorized_keys")
        assert _is_write_denied(path) is True

    def test_ssh_id_rsa_denied(self):
        path = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        assert _is_write_denied(path) is True

    def test_netrc_denied(self):
        path = os.path.join(str(Path.home()), ".netrc")
        assert _is_write_denied(path) is True

    def test_aws_prefix_denied(self):
        path = os.path.join(str(Path.home()), ".aws", "credentials")
        assert _is_write_denied(path) is True

    def test_kube_prefix_denied(self):
        path = os.path.join(str(Path.home()), ".kube", "config")
        assert _is_write_denied(path) is True

    def test_normal_file_allowed(self, tmp_path):
        path = str(tmp_path / "safe_file.txt")
        assert _is_write_denied(path) is False

    def test_project_file_allowed(self):
        assert _is_write_denied("/tmp/project/main.py") is False

    def test_tilde_expansion(self):
        assert _is_write_denied("~/.ssh/authorized_keys") is True


def test_explicit_package_dependency_search():
    assert _explicit_package_dependency_search(".lake/packages/mathlib") is True
    assert _explicit_package_dependency_search("/tmp/Demo/.lake/packages/mathlib/Mathlib") is True
    assert _explicit_package_dependency_search(".lake/build/lib") is False
    assert _explicit_package_dependency_search(".") is False


# =========================================================================
# Result dataclasses
# =========================================================================


class TestReadResult:
    def test_to_dict_omits_defaults(self):
        r = ReadResult()
        d = r.to_dict()
        assert "error" not in d  # None omitted
        assert "similar_files" not in d  # empty list omitted

    def test_to_dict_preserves_empty_content(self):
        """Empty file should still have content key in the dict."""
        r = ReadResult(content="", total_lines=0, file_size=0)
        d = r.to_dict()
        assert "content" in d
        assert d["content"] == ""
        assert d["total_lines"] == 0
        assert d["file_size"] == 0

    def test_to_dict_includes_values(self):
        r = ReadResult(content="hello", total_lines=10, file_size=50, truncated=True)
        d = r.to_dict()
        assert d["content"] == "hello"
        assert d["total_lines"] == 10
        assert d["truncated"] is True

    def test_binary_fields(self):
        r = ReadResult(is_binary=True, is_image=True)
        d = r.to_dict()
        assert d["is_binary"] is True
        assert d["is_image"] is True


class TestWriteResult:
    def test_to_dict_omits_none(self):
        r = WriteResult(bytes_written=100)
        d = r.to_dict()
        assert d["bytes_written"] == 100
        assert "error" not in d
        assert "warning" not in d

    def test_to_dict_includes_error(self):
        r = WriteResult(error="Permission denied")
        d = r.to_dict()
        assert d["error"] == "Permission denied"


class TestPatchResult:
    def test_to_dict_success(self):
        r = PatchResult(success=True, diff="--- a\n+++ b", files_modified=["a.py"])
        d = r.to_dict()
        assert d["success"] is True
        assert d["diff"] == "--- a\n+++ b"
        assert d["files_modified"] == ["a.py"]

    def test_to_dict_error(self):
        r = PatchResult(error="File not found")
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "File not found"


class TestSearchResult:
    def test_to_dict_with_matches(self):
        m = SearchMatch(path="a.py", line_number=10, content="hello")
        r = SearchResult(matches=[m], total_count=1)
        d = r.to_dict()
        assert d["total_count"] == 1
        assert len(d["matches"]) == 1
        assert d["matches"][0]["path"] == "a.py"

    def test_to_dict_empty(self):
        r = SearchResult()
        d = r.to_dict()
        assert d["total_count"] == 0
        assert "matches" not in d

    def test_to_dict_files_mode(self):
        r = SearchResult(files=["a.py", "b.py"], total_count=2)
        d = r.to_dict()
        assert d["files"] == ["a.py", "b.py"]

    def test_to_dict_count_mode(self):
        r = SearchResult(counts={"a.py": 3, "b.py": 1}, total_count=4)
        d = r.to_dict()
        assert d["counts"]["a.py"] == 3

    def test_truncated_flag(self):
        r = SearchResult(total_count=100, truncated=True)
        d = r.to_dict()
        assert d["truncated"] is True


class TestLintResult:
    def test_skipped(self):
        r = LintResult(skipped=True, message="No linter for .md files")
        d = r.to_dict()
        assert d["status"] == "skipped"
        assert d["message"] == "No linter for .md files"

    def test_success(self):
        r = LintResult(success=True, output="")
        d = r.to_dict()
        assert d["status"] == "ok"

    def test_error(self):
        r = LintResult(success=False, output="SyntaxError line 5")
        d = r.to_dict()
        assert d["status"] == "error"
        assert "SyntaxError" in d["output"]


# =========================================================================
# ShellFileOperations helpers
# =========================================================================


@pytest.fixture()
def mock_env():
    """Create a mock terminal environment."""
    env = MagicMock()
    env.cwd = "/tmp/test"
    env.execute.return_value = {"output": "", "returncode": 0}
    return env


@pytest.fixture()
def file_ops(mock_env):
    return ShellFileOperations(mock_env)


class LocalShellEnv:
    def __init__(self, cwd: Path):
        self.cwd = str(cwd)

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd or self.cwd,
            input=stdin_data,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {"output": completed.stdout, "returncode": completed.returncode}


class TestShellFileOpsHelpers:
    def test_escape_shell_arg_simple(self, file_ops):
        assert file_ops._escape_shell_arg("hello") == "'hello'"

    def test_escape_shell_arg_with_quotes(self, file_ops):
        result = file_ops._escape_shell_arg("it's")
        assert "'" in result
        # Should be safely escaped
        assert result.count("'") >= 4  # wrapping + escaping

    def test_is_likely_binary_by_extension(self, file_ops):
        assert file_ops._is_likely_binary("photo.png") is True
        assert file_ops._is_likely_binary("data.db") is True
        assert file_ops._is_likely_binary("code.py") is False
        assert file_ops._is_likely_binary("readme.md") is False

    def test_is_likely_binary_by_content(self, file_ops):
        # High ratio of non-printable chars -> binary
        binary_content = "\x00\x01\x02\x03" * 250
        assert file_ops._is_likely_binary("unknown", binary_content) is True

        # Normal text -> not binary
        assert file_ops._is_likely_binary("unknown", "Hello world\nLine 2\n") is False

    def test_is_image(self, file_ops):
        assert file_ops._is_image("photo.png") is True
        assert file_ops._is_image("pic.jpg") is True
        assert file_ops._is_image("icon.ico") is True
        assert file_ops._is_image("data.pdf") is False
        assert file_ops._is_image("code.py") is False

    def test_add_line_numbers(self, file_ops):
        content = "line one\nline two\nline three"
        result = file_ops._add_line_numbers(content)
        assert "     1|line one" in result
        assert "     2|line two" in result
        assert "     3|line three" in result

    def test_add_line_numbers_with_offset(self, file_ops):
        content = "continued\nmore"
        result = file_ops._add_line_numbers(content, start_line=50)
        assert "    50|continued" in result
        assert "    51|more" in result

    def test_add_line_numbers_truncates_long_lines(self, file_ops):
        long_line = "x" * (MAX_LINE_LENGTH + 100)
        result = file_ops._add_line_numbers(long_line)
        assert "[truncated]" in result

    def test_unified_diff(self, file_ops):
        old = "line1\nline2\nline3\n"
        new = "line1\nchanged\nline3\n"
        diff = file_ops._unified_diff(old, new, "test.py")
        assert "-line2" in diff
        assert "+changed" in diff
        assert "test.py" in diff

    def test_cwd_from_env(self, mock_env):
        mock_env.cwd = "/custom/path"
        ops = ShellFileOperations(mock_env)
        assert ops.cwd == "/custom/path"

    def test_cwd_fallback_to_slash(self):
        env = MagicMock(spec=[])  # no cwd attribute
        ops = ShellFileOperations(env)
        assert ops.cwd == "/"


class TestSearchPathValidation:
    """Test that search() returns an error for non-existent paths."""

    def test_search_nonexistent_path_returns_error(self, mock_env):
        """search() should return an error when the path doesn't exist."""

        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "not_found", "returncode": 1}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/nonexistent/path")
        assert result.error is not None
        assert "not found" in result.error.lower() or "Path not found" in result.error

    def test_search_nonexistent_path_files_mode(self, mock_env):
        """search(target='files') should also return error for bad paths."""

        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "not_found", "returncode": 1}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("*.py", path="/nonexistent/path", target="files")
        assert result.error is not None
        assert "not found" in result.error.lower() or "Path not found" in result.error

    def test_search_existing_path_proceeds(self, mock_env):
        """search() should proceed normally when the path exists."""

        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "exists", "returncode": 0}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            # rg returns exit 1 (no matches) with empty output
            return {"output": "", "returncode": 1}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/existing/path")
        assert result.error is None
        assert result.total_count == 0  # No matches but no error

    def test_search_rg_error_exit_code(self, mock_env):
        """search() should report error when rg returns exit code 2."""
        call_count = {"n": 0}

        def side_effect(command, **kwargs):
            call_count["n"] += 1
            if "test -e" in command:
                return {"output": "exists", "returncode": 0}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            # rg returns exit 2 (error) with empty output
            return {"output": "", "returncode": 2}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/some/path")
        assert result.error is not None
        assert "search failed" in result.error.lower() or "Search error" in result.error

    def test_search_context_preserves_hyphenated_numeric_path(self, tmp_path):
        """Keep context paths intact when checkout names contain ``-<digits>-`` segments."""
        checkout = tmp_path / "leanflow-acceptance-20260714-imomath3"
        checkout.mkdir()
        source = checkout / "Example.lean"
        source.write_text("before\nneedle\nafter\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search("needle", path=str(checkout), context=1)

        assert result.error is None
        assert [(match.path, match.line_number) for match in result.matches] == [
            (str(source), 1),
            (str(source), 2),
            (str(source), 3),
        ]

    def test_repository_content_search_excludes_workflow_state(self, tmp_path, monkeypatch):
        """Do not return a live transcript match from an otherwise broad source search."""
        monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
        source = tmp_path / "Source.lean"
        source.write_text("recursive-marker\n", encoding="utf-8")
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        (state / "latest-run.log").write_text("recursive-marker\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search("recursive-marker", path=".")

        assert result.error is None
        assert [Path(match.path).name for match in result.matches] == ["Source.lean"]

    def test_explicit_lake_package_search_overrides_project_ignore(self, tmp_path):
        packages = tmp_path / ".lake" / "packages" / "mathlib" / "Mathlib"
        packages.mkdir(parents=True)
        source = packages / "Independent.lean"
        source.write_text(
            "lemma eq_zero_of_affineCombination_mem_affineSpan : True := by trivial\n",
            encoding="utf-8",
        )
        (tmp_path / ".gitignore").write_text(".lake/\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search(
            "eq_zero_of_affineCombination_mem_affineSpan",
            path=str(packages),
            file_glob="*.lean",
        )

        assert result.error is None
        assert [Path(match.path).name for match in result.matches] == ["Independent.lean"]

    def test_repository_file_search_excludes_workflow_state(self, tmp_path, monkeypatch):
        """Prune .leanflow when the agent uses search_files as a recursive file listing."""
        monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
        (tmp_path / "ordinary.log").write_text("source log\n", encoding="utf-8")
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        (state / "latest-run.log").write_text("agent transcript\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search("*.log", path=".", target="files")

        assert result.error is None
        assert [Path(path).name for path in result.files] == ["ordinary.log"]

    def test_repository_search_includes_cloned_research_sources(self, tmp_path, monkeypatch):
        monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
        checkout = tmp_path / ".leanflow" / "workspace" / "repos" / "proofs"
        checkout.mkdir(parents=True)
        source = checkout / "solution.lean"
        source.write_text("theorem cloned : True := by trivial\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        files = ops.search("*.lean", path=str(tmp_path), target="files")
        content = ops.search(
            "theorem cloned",
            path=str(tmp_path),
            file_glob="*.lean",
        )

        assert files.error is None
        assert source.resolve() in {Path(path).resolve() for path in files.files}
        assert content.error is None
        assert source.resolve() in {Path(match.path).resolve() for match in content.matches}

    def test_explicit_workflow_state_search_is_blocked(self, tmp_path, monkeypatch):
        monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search("proof", path=str(state))

        assert result.error is not None
        assert "cannot search managed workflow-state" in result.error

    def test_explicit_diagnostic_mode_can_search_workflow_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "1")
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        log = state / "latest-run.log"
        log.write_text("diagnostic-marker\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.search("diagnostic-marker", path=str(state))

        assert result.error is None
        assert [Path(match.path).name for match in result.matches] == ["latest-run.log"]


class TestWorkflowTranscriptReadGuard:
    def test_direct_log_read_is_blocked(self, tmp_path, monkeypatch):
        monkeypatch.delenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, raising=False)
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        log = state / "latest-run.log"
        log.write_text("do not ingest me\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.read_file(str(log))

        assert result.error is not None
        assert "own prior output" in result.error
        assert result.content == ""

    def test_explicit_diagnostic_mode_can_read_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv(WORKFLOW_DIAGNOSTIC_FILE_ACCESS_ENV, "true")
        state = tmp_path / ".leanflow" / "workflow-state"
        state.mkdir(parents=True)
        log = state / "latest-run.log"
        log.write_text("operator diagnostic\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.read_file(str(log))

        assert result.error is None
        assert "operator diagnostic" in result.content


class TestShellFileOpsWriteDenied:
    def test_write_file_denied_path(self, file_ops):
        result = file_ops.write_file("~/.ssh/authorized_keys", "evil key")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_patch_replace_denied_path(self, file_ops):
        result = file_ops.patch_replace("~/.ssh/authorized_keys", "old", "new")
        assert result.error is not None
        assert "denied" in result.error.lower()


class TestAtomicLocalWrites:
    """Exercise the LocalEnvironment old-or-new write capability."""

    def test_interrupt_before_replace_preserves_old_bytes(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        path = tmp_path / "state.md"
        old = b"old managed bytes\n"
        path.write_bytes(old)
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        with patch(
            "tools.environments.local.is_interrupted",
            side_effect=[False, True],
        ):
            result = ops.write_file(str(path), "new staged bytes\n")

        assert result.error is not None
        assert "interrupted" in result.error.lower()
        assert path.read_bytes() == old
        assert list(tmp_path.glob(".*.leanflow-tmp")) == []

    def test_transactional_write_commits_complete_bytes_during_interrupt(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        path = tmp_path / "state.md"
        path.write_text("transient edit\n", encoding="utf-8")
        intended = "restored café\n"
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        with patch("tools.environments.local.is_interrupted", return_value=True):
            result = ops.write_file_transactional(str(path), intended)

        assert result.error is None
        assert result.bytes_written == len(intended.encode("utf-8"))
        assert path.read_bytes() == intended.encode("utf-8")
        assert list(tmp_path.glob(".*.leanflow-tmp")) == []

    def test_new_file_mode_honors_process_umask(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        reference = tmp_path / "reference.txt"
        reference_fd = os.open(reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        os.close(reference_fd)
        path = tmp_path / "created.txt"
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        result = ops.write_file(str(path), "new bytes\n")

        assert result.error is None
        assert path.stat().st_mode & 0o777 == reference.stat().st_mode & 0o777

    def test_existing_executable_mode_is_preserved(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        path = tmp_path / "script.sh"
        path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        path.chmod(0o751)
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        result = ops.write_file(str(path), "#!/bin/sh\nexit 0\n")

        assert result.error is None
        assert path.stat().st_mode & 0o777 == 0o751

    def test_read_only_target_is_not_replaced_via_directory_permission(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        path = tmp_path / "read-only.txt"
        original = b"protected bytes\n"
        path.write_bytes(original)
        path.chmod(0o444)
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        result = ops.write_file(str(path), "replacement\n")

        assert result.error is not None
        assert "permission denied" in result.error.lower()
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o444
        assert list(tmp_path.glob(".*.leanflow-tmp")) == []

    def test_relative_path_uses_file_operations_cwd(self, tmp_path, monkeypatch):
        from tools.environments.local import LocalEnvironment

        process_cwd = tmp_path / "process-cwd"
        operation_cwd = tmp_path / "operation-cwd"
        process_cwd.mkdir()
        operation_cwd.mkdir()
        monkeypatch.chdir(process_cwd)
        env = LocalEnvironment(cwd=str(operation_cwd))
        ops = ShellFileOperations(env, cwd=str(operation_cwd))

        result = ops.write_file("relative.txt", "cwd-owned\n")

        assert result.error is None
        assert (operation_cwd / "relative.txt").read_text(encoding="utf-8") == "cwd-owned\n"
        assert not (process_cwd / "relative.txt").exists()

    def test_symlink_path_keeps_link_and_replaces_its_target(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        target = tmp_path / "target.txt"
        target.write_text("old\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target.name)
        env = LocalEnvironment(cwd=str(tmp_path))
        ops = ShellFileOperations(env, cwd=str(tmp_path))

        result = ops.write_file(str(link), "new\n")

        assert result.error is None
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "new\n"


class TestPatchReplaceStrictAndNearMiss:
    """D3: strict exact-or-fail + actionable near-miss on failure (non-Lean files)."""

    def test_strict_rejects_whitespace_only_match(self, tmp_path):
        path = tmp_path / "mod.py"
        path.write_text("def f():\n    return  1\n", encoding="utf-8")  # double space
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        # Non-strict tolerates the whitespace difference.
        loose = ops.patch_replace(str(path), "    return 1", "    return 2")
        assert loose.success is True
        path.write_text("def f():\n    return  1\n", encoding="utf-8")  # reset

        # Strict refuses it and leaves the file untouched.
        strict = ops.patch_replace(str(path), "    return 1", "    return 2", strict=True)
        assert strict.success is False
        assert strict.error is not None
        assert path.read_text(encoding="utf-8") == "def f():\n    return  1\n"

    def test_failure_surfaces_near_miss_snippet(self, tmp_path):
        path = tmp_path / "mod.py"
        path.write_text("def compute_total(items):\n    return sum(items)\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.patch_replace(
            str(path), "def compute_grand_total(rows):", "def x():", strict=True
        )

        assert result.success is False
        assert result.error is not None
        # Concrete closest-region hint, not a generic "could not find".
        assert "Closest region" in result.error
        assert "compute_total" in result.error


class TestLeanStatementGuardedWrites:
    def test_write_file_blocks_lean_statement_change(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_STATEMENT_EDITS_ENV, raising=False)
        path = tmp_path / "Demo.lean"
        original = "theorem demo : True := by\n  trivial\n"
        path.write_text(original, encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.write_file(str(path), "theorem demo : False := by\n  trivial\n")

        assert result.error is not None
        assert "Lean statement guard blocked this edit" in result.error
        assert "LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS" not in result.error
        assert path.read_text(encoding="utf-8") == original

    def test_patch_replace_blocks_lean_statement_change(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_STATEMENT_EDITS_ENV, raising=False)
        path = tmp_path / "Demo.lean"
        path.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.patch_replace(str(path), "theorem demo : True", "theorem demo : False")

        assert result.success is False
        assert result.error is not None
        assert "Lean statement guard blocked this edit" in result.error

    def test_patch_v4a_blocks_lean_file_delete_with_theorem(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_STATEMENT_EDITS_ENV, raising=False)
        path = tmp_path / "Demo.lean"
        path.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.patch_v4a("""\
*** Begin Patch
*** Delete File: Demo.lean
*** End Patch""")

        assert result.success is False
        assert result.error is not None
        assert "Lean statement guard blocked this edit" in result.error
        assert path.exists()

    def test_patch_v4a_blocks_lean_file_move_with_theorem(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_STATEMENT_EDITS_ENV, raising=False)
        path = tmp_path / "Demo.lean"
        moved = tmp_path / "Moved.lean"
        path.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
        ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))

        result = ops.patch_v4a("""\
*** Begin Patch
*** Move File: Demo.lean -> Moved.lean
*** End Patch""")

        assert result.success is False
        assert result.error is not None
        assert "Lean statement guard blocked this move" in result.error
        assert path.exists()
        assert not moved.exists()
