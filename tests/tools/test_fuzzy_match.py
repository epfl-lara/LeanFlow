"""Tests for the fuzzy matching module."""

from tools.utilities.fuzzy_match import (
    STRICT_CONFIG,
    FuzzyConfig,
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


# A 5-line block whose anchors (first/last) match but every middle line differs.
# block_anchor middle similarity lands in the old 0.10..0.30 "accept" band while
# context_aware sees only 2/5 similar lines — so the ONLY thing that ever matched this
# was the dangerously-low block_anchor threshold.
_FALSE_MATCH_CONTENT = (
    "def anchor_top():\n"
    "    alpha_value = 111\n"
    "    beta_value = 222\n"
    "    gamma_value = 333\n"
    "    return None\n"
)
_FALSE_MATCH_PATTERN = "def anchor_top():\n    Q\n    W\n    E\n    return None"


class TestRaisedThresholds:
    """D3: the old 0.10 block_anchor / 0.5 context_aware thresholds were unsafe."""

    def test_old_permissive_config_accepts_the_false_match(self):
        # Reconstruct the historical thresholds and confirm they DID accept this block —
        # so the test below is genuinely guarding against a real regression, not a strawman.
        legacy = FuzzyConfig(
            block_anchor_threshold=0.10,
            context_aware_line_fraction=0.5,
            context_aware_min_similarity=0.0,
        )
        result = fuzzy_find_and_replace_ex(
            _FALSE_MATCH_CONTENT,
            _FALSE_MATCH_PATTERN,
            "def anchor_top():\n    return None",
            config=legacy,
        )
        assert result.count == 1
        assert result.strategy == "block_anchor"

    def test_safe_defaults_reject_the_false_match(self):
        # Same inputs, default (safe) config: no strategy should accept it.
        result = fuzzy_find_and_replace_ex(
            _FALSE_MATCH_CONTENT,
            _FALSE_MATCH_PATTERN,
            "def anchor_top():\n    return None",
        )
        assert result.count == 0
        assert result.error is not None
        assert result.strategy is None

    def test_multi_candidate_anchor_needs_strong_middle(self):
        # Two anchor candidates: an ambiguous anchor must not win on a weak middle.
        content = "H\na b c\nT\nH\np q r\nT\n"
        pattern = "H\nz z z z\nT"
        result = fuzzy_find_and_replace_ex(content, pattern, "H\nX\nT")
        # Weak middle across both candidates -> no confident block_anchor hit.
        assert result.strategy != "block_anchor" or result.count == 0


class TestStrictConfig:
    """D3: strict = exact-or-fail (no fuzzy, no whitespace normalization)."""

    def test_strict_rejects_whitespace_only_difference(self):
        content = "def f():\n    return  1\n"  # double space
        result = fuzzy_find_and_replace_ex(
            content, "    return 1", "    return 2", config=STRICT_CONFIG
        )
        assert result.count == 0
        assert result.error is not None

    def test_strict_still_applies_exact_match(self):
        result = fuzzy_find_and_replace_ex("abc def", "abc", "xyz", config=STRICT_CONFIG)
        assert result.count == 1
        assert result.strategy == "exact"
        assert result.content == "xyz def"


class TestNearMissSurfacing:
    """D3: a failed match surfaces the closest region instead of a generic message."""

    def test_failure_carries_near_miss_snippet(self):
        content = "def compute_total(items):\n    return sum(items)\n"
        # Close but not matchable: same shape, different identifier.
        result = fuzzy_find_and_replace_ex(
            content, "def compute_grand_total(rows):", "def x():", config=STRICT_CONFIG
        )
        assert result.count == 0
        assert result.near_miss is not None
        assert result.near_miss.similarity > 0.0
        assert "compute_total" in result.near_miss.snippet
        assert result.near_miss.line_number == 1
        # The similarity + snippet are echoed into the human-readable error too.
        assert "Closest region" in (result.error or "")

    def test_no_near_miss_snippet_for_empty_file(self):
        result = fuzzy_find_and_replace_ex("", "anything", "x")
        assert result.count == 0
        assert result.near_miss is None
