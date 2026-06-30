"""Tests for D2 read-before-edit freshness + F2 strategy observability.

These drive the real patch path through a LocalShellEnv (no mocks), so they
verify the end-to-end tool JSON the agent actually sees: that a patch reports
which fuzzy strategy matched, and that editing a file whose on-disk content
changed since the last read is rejected while read-then-edit succeeds.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.implementations.file_operations import ShellFileOperations
from tools.implementations.file_tools import (
    clear_read_tracker,
    patch_tool,
    read_file_tool,
    write_file_tool,
)
from tools.utilities.read_freshness import (
    _normalize,
    check_freshness,
    clear_freshness,
    record_read,
)


class LocalShellEnv:
    """Minimal real-shell backend rooted at a temp dir (mirrors file-ops tests)."""

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


@pytest.fixture
def local_ops(tmp_path):
    """A real ShellFileOperations over a temp dir, with trackers cleared."""
    clear_read_tracker()  # also clears freshness
    ops = ShellFileOperations(LocalShellEnv(tmp_path), cwd=str(tmp_path))
    with patch("tools.implementations.file_tools._get_file_ops", return_value=ops):
        yield ops
    clear_read_tracker()


# ---------------------------------------------------------------------------
# D2 — freshness tracker unit behavior
# ---------------------------------------------------------------------------


class TestFreshnessTracker:
    def setup_method(self):
        clear_freshness()

    def test_never_read_is_soft(self):
        v = check_freshness("t", "/some/file", "content")
        assert v.status == "never_read"
        assert v.message

    def test_read_then_same_content_is_fresh(self):
        record_read("t", "/some/file", "content")
        assert check_freshness("t", "/some/file", "content").status == "fresh"

    def test_changed_content_is_stale(self):
        record_read("t", "/some/file", "content")
        v = check_freshness("t", "/some/file", "content CHANGED")
        assert v.status == "stale"
        assert "changed on disk" in v.message

    def test_clear_for_one_task_only(self):
        record_read("a", "/f", "x")
        record_read("b", "/f", "x")
        clear_freshness("a")
        assert check_freshness("a", "/f", "x").status == "never_read"
        assert check_freshness("b", "/f", "x").status == "fresh"

    def test_relative_and_absolute_paths_share_a_key(self):
        # A read by relative path and a patch by the resolved absolute path (what
        # ShellFileOperations uses) must hash to the same key — else stale edits fail open.
        import os

        rel = "sub/dir/file.txt"
        assert _normalize(rel) == _normalize(os.path.abspath(rel))
        record_read("t", os.path.abspath(rel), "X")
        assert check_freshness("t", rel, "X").status == "fresh"
        assert check_freshness("t", rel, "X CHANGED").status == "stale"


# ---------------------------------------------------------------------------
# D2 — freshness contract through the real patch_tool
# ---------------------------------------------------------------------------


class TestPatchFreshnessGuard:
    def test_read_then_edit_succeeds(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        Path(path).write_text("hello world\n", encoding="utf-8")

        read_file_tool(path)  # establishes freshness
        raw = patch_tool(mode="replace", path=path, old_string="hello", new_string="hi")
        result = json.loads(raw)

        assert result["success"] is True
        assert "freshness_warning" not in result
        assert Path(path).read_text(encoding="utf-8") == "hi world\n"

    def test_edit_after_external_change_is_rejected(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        Path(path).write_text("hello world\n", encoding="utf-8")

        read_file_tool(path)  # agent's view: "hello world"
        # Something else changes the file on disk after the read.
        Path(path).write_text("hello brave world\n", encoding="utf-8")

        raw = patch_tool(mode="replace", path=path, old_string="hello", new_string="hi")
        result = json.loads(raw)

        assert result["success"] is False
        assert result.get("stale") is True
        assert "changed on disk" in result["error"]
        # The stale edit must NOT have been applied.
        assert Path(path).read_text(encoding="utf-8") == "hello brave world\n"

    def test_never_read_warns_but_applies(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        Path(path).write_text("hello world\n", encoding="utf-8")

        # No prior read at all.
        raw = patch_tool(mode="replace", path=path, old_string="hello", new_string="hi")
        result = json.loads(raw)

        assert result["success"] is True
        assert "freshness_warning" in result
        assert "without having read it" in result["freshness_warning"]
        # Soft case: the edit still applies.
        assert Path(path).read_text(encoding="utf-8") == "hi world\n"

    def test_consecutive_edits_after_one_read_are_not_stale(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        Path(path).write_text("a b c\n", encoding="utf-8")

        read_file_tool(path)
        first = json.loads(patch_tool(mode="replace", path=path, old_string="a", new_string="A"))
        assert first["success"] is True
        # The tool refreshed its own view, so a follow-up edit isn't flagged stale.
        second = json.loads(patch_tool(mode="replace", path=path, old_string="b", new_string="B"))
        assert second["success"] is True
        assert "freshness_warning" not in second
        assert Path(path).read_text(encoding="utf-8") == "A B c\n"

    def test_write_file_then_patch_is_not_stale(self, local_ops, tmp_path):
        # write_file records the write, so patching the agent's own just-written
        # content is not hard-rejected as stale (Codex review: write_file note_write).
        path = str(tmp_path / "f.txt")
        write_file_tool(path, "fresh content\n")
        result = json.loads(
            patch_tool(mode="replace", path=path, old_string="fresh", new_string="brand new")
        )
        assert result["success"] is True
        assert "freshness_warning" not in result
        assert Path(path).read_text(encoding="utf-8") == "brand new content\n"

    def test_v4a_patch_after_external_change_is_rejected(self, local_ops, tmp_path):
        # V4A patch mode is guarded too (Codex review: mode="patch" bypassed freshness).
        path = str(tmp_path / "f.txt")
        Path(path).write_text("line one\nline two\n", encoding="utf-8")
        read_file_tool(path)
        Path(path).write_text("line one CHANGED\nline two\n", encoding="utf-8")

        v4a = (
            "*** Begin Patch\n"
            f"*** Update File: {path}\n"
            "@@\n"
            "-line two\n"
            "+line TWO\n"
            "*** End Patch\n"
        )
        result = json.loads(patch_tool(mode="patch", patch=v4a))
        assert result["success"] is False
        assert result.get("stale") is True
        # The stale V4A edit must NOT have been applied.
        assert Path(path).read_text(encoding="utf-8") == "line one CHANGED\nline two\n"


# ---------------------------------------------------------------------------
# F2 — strategy observability through the real patch_tool
# ---------------------------------------------------------------------------


class TestPatchStrategyObservability:
    def test_exact_match_reports_full_confidence(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        Path(path).write_text("hello world\n", encoding="utf-8")
        read_file_tool(path)

        result = json.loads(
            patch_tool(mode="replace", path=path, old_string="hello", new_string="hi")
        )
        assert result["success"] is True
        assert result["matched_via"] == "exact"
        assert result["similarity"] == 1.0

    def test_fuzzy_match_reports_strategy_and_low_similarity(self, local_ops, tmp_path):
        path = str(tmp_path / "f.txt")
        # First/last lines anchor; the middle line differs enough to land on a
        # fuzzy strategy with a clearly sub-1.0 similarity.
        Path(path).write_text("alpha\nbravo charlie delta echo\nfoxtrot\n", encoding="utf-8")
        read_file_tool(path)

        old = "alpha\nzulu yankee xray whiskey\nfoxtrot"
        result = json.loads(
            patch_tool(mode="replace", path=path, old_string=old, new_string="alpha\nNEW\nfoxtrot")
        )
        assert result["success"] is True
        assert result["matched_via"] in {"block_anchor", "context_aware"}
        assert result["similarity"] < 0.8
