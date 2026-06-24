"""Tests for agent/log_formatting.py and its re-export from run_agent (Phase 1 extraction)."""

import json

import run_agent
from agent.display import log_formatting


def test_run_agent_reexports_match_module():
    # Backwards-compat: run_agent.* must be the same objects as the new module's.
    for name in log_formatting.__all__:
        assert getattr(run_agent, name) is getattr(log_formatting, name), name


def test_wrap_log_text_preserves_blank_and_wraps():
    assert log_formatting._wrap_log_text("") == [""]
    # Wrappable text (spaces) is split to the width.
    words = " ".join(["word"] * 60)
    out = log_formatting._wrap_log_text(words, width=40)
    assert len(out) > 1
    assert all(len(line) <= 40 for line in out)
    # Long unbreakable tokens are NOT split (break_long_words=False): single line preserved.
    assert log_formatting._wrap_log_text("a" * 250, width=100) == ["a" * 250]


def test_truncate_log_lines_keeps_head_and_tail():
    lines = [str(i) for i in range(50)]
    out = log_formatting._truncate_log_lines(lines, head=3, tail=2)
    assert out[:3] == ["0", "1", "2"]
    assert out[-2:] == ["48", "49"]
    assert any("more line(s)" in line for line in out)
    # Short blocks are returned unchanged.
    assert log_formatting._truncate_log_lines(["a", "b"], head=5) == ["a", "b"]


def test_summarize_arg_value_shapes():
    assert log_formatting._summarize_arg_value("x", None) == "null"
    assert log_formatting._summarize_arg_value("x", True) == "true"
    assert log_formatting._summarize_arg_value("x", [1, 2, 3]) == "3 item(s)"
    big = log_formatting._summarize_arg_value("patch", "a\nb\nc")
    assert "chars across 3 line(s)" in big


def test_format_tool_result_for_log_dict_and_plain():
    out = log_formatting._format_tool_result_for_log("t", json.dumps({"success": True, "count": 2}))
    assert any("success" in line for line in out)
    plain = log_formatting._format_tool_result_for_log("t", "just a plain string")
    assert plain == ["just a plain string"]
