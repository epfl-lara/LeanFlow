"""Tests for the fuzzy matching module."""

from tools.utilities.fuzzy_match import (
    fuzzy_find_and_replace,
    fuzzy_find_and_replace_ex,
)


class TestExactMatch:
    def test_single_replacement(self):
        content = "hello world"
        new, count, err = fuzzy_find_and_replace(content, "hello", "hi")
        assert err is None
        assert count == 1
        assert new == "hi world"

    def test_no_match(self):
        content = "hello world"
        new, count, err = fuzzy_find_and_replace(content, "xyz", "abc")
        assert count == 0
        assert err is not None
        assert new == content

    def test_empty_old_string(self):
        new, count, err = fuzzy_find_and_replace("abc", "", "x")
        assert count == 0
        assert err is not None

    def test_identical_strings(self):
        new, count, err = fuzzy_find_and_replace("abc", "abc", "abc")
        assert count == 0
        assert "identical" in err

    def test_multiline_exact(self):
        content = "line1\nline2\nline3"
        new, count, err = fuzzy_find_and_replace(content, "line1\nline2", "replaced")
        assert err is None
        assert count == 1
        assert new == "replaced\nline3"


class TestWhitespaceDifference:
    def test_extra_spaces_match(self):
        content = "def  foo(  x,  y  ):"
        new, count, err = fuzzy_find_and_replace(content, "def foo( x, y ):", "def bar(x, y):")
        assert count == 1
        assert "bar" in new


class TestIndentDifference:
    def test_different_indentation(self):
        content = "    def foo():\n        pass"
        new, count, err = fuzzy_find_and_replace(
            content, "def foo():\n    pass", "def bar():\n    return 1"
        )
        assert count == 1
        assert "bar" in new


class TestReplaceAll:
    def test_multiple_matches_without_flag_errors(self):
        content = "aaa bbb aaa"
        new, count, err = fuzzy_find_and_replace(content, "aaa", "ccc", replace_all=False)
        assert count == 0
        assert "Found 2 matches" in err

    def test_multiple_matches_with_flag(self):
        content = "aaa bbb aaa"
        new, count, err = fuzzy_find_and_replace(content, "aaa", "ccc", replace_all=True)
        assert err is None
        assert count == 2
        assert new == "ccc bbb ccc"


class TestStrategyObservability:
    """F2: fuzzy_find_and_replace_ex reports which strategy matched + similarity."""

    def test_exact_reports_exact_strategy(self):
        result = fuzzy_find_and_replace_ex("hello world", "hello", "hi")
        assert result.error is None
        assert result.count == 1
        assert result.strategy == "exact"
        assert result.similarity == 1.0

    def test_structural_strategy_is_full_confidence(self):
        # Whitespace differences -> a structural strategy, still similarity 1.0.
        content = "def  foo(  x,  y  ):"
        result = fuzzy_find_and_replace_ex(content, "def foo( x, y ):", "def bar(x, y):")
        assert result.count == 1
        assert result.strategy != "exact"
        assert result.strategy in {
            "line_trimmed",
            "whitespace_normalized",
            "indentation_flexible",
            "trimmed_boundary",
        }
        assert result.similarity == 1.0

    def test_low_similarity_fuzzy_hit_is_visible(self):
        # Anchors (first/last line) match but the middle differs a lot: this lands
        # on a fuzzy strategy with a clearly sub-1.0 similarity, which is exactly
        # the signal that distinguishes a guess from an exact match.
        content = "alpha\nbravo charlie delta echo\nfoxtrot\n"
        old = "alpha\nzulu yankee xray whiskey\nfoxtrot"
        result = fuzzy_find_and_replace_ex(content, old, "alpha\nNEW\nfoxtrot")
        assert result.count == 1
        assert result.strategy in {"block_anchor", "context_aware"}
        assert result.similarity is not None
        assert result.similarity < 0.8

    def test_no_match_has_no_strategy(self):
        result = fuzzy_find_and_replace_ex("hello world", "xyz", "abc")
        assert result.count == 0
        assert result.error is not None
        assert result.strategy is None
        assert result.similarity is None

    def test_thin_wrapper_preserves_three_tuple(self):
        # The legacy 3-tuple API must keep working unchanged.
        new, count, err = fuzzy_find_and_replace("hello world", "hello", "hi")
        assert (new, count, err) == ("hi world", 1, None)
