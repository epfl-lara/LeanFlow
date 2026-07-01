#!/usr/bin/env python3
"""
Fuzzy Matching Module for File Operations

Implements a multi-strategy matching chain to robustly find and replace text,
accommodating variations in whitespace, indentation, and escaping common
in LLM-generated code.

The 8-strategy matching chain (inspired by OpenCode), tried in order:
1. Exact match - Direct string comparison
2. Line-trimmed - Strip leading/trailing whitespace per line
3. Whitespace normalized - Collapse multiple spaces/tabs to single space
4. Indentation flexible - Ignore indentation differences entirely
5. Escape normalized - Convert \\n literals to actual newlines
6. Trimmed boundary - Trim first/last line whitespace only
7. Block anchor - Match first+last lines, use similarity for middle
8. Context-aware - line similarity fraction threshold

(The `replace_all` flag is handled as a separate multi-occurrence path, not a chain strategy.)

The two similarity-guessing strategies (block_anchor, context_aware) are gated by a
`FuzzyConfig` knob. Their historical thresholds (block_anchor 0.10, context_aware 0.5
line-fraction) were dangerously permissive — a 0.10 middle-similarity anchor hit accepts
a block that shares almost nothing with the pattern. The defaults now sit at safe values
(see `FuzzyConfig`); `strict=True` disables all fuzziness entirely (exact-or-fail) for
high-risk edits. Structural strategies (exact/whitespace/indentation/...) are unaffected.

`fuzzy_find_and_replace_ex` additionally reports which strategy matched and a
similarity score, so a low-confidence fuzzy hit is observable in tool results.

Usage:
    from tools.utilities.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, error = fuzzy_find_and_replace(
        content="def foo():\\n    pass",
        old_string="def foo():",
        new_string="def bar():",
        replace_all=False
    )

    # Observable variant — also returns the winning strategy + similarity:
    from tools.utilities.fuzzy_match import fuzzy_find_and_replace_ex
    result = fuzzy_find_and_replace_ex(content, old, new)
    result.strategy    # e.g. "context_aware"
    result.similarity  # e.g. 0.5
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

# Strategies that match the text structurally (exact / whitespace / indentation),
# i.e. with no similarity-threshold guessing. A hit from one of these is reported
# as similarity 1.0 — it is, by construction, a faithful match. The two fuzzy
# strategies (block_anchor, context_aware) report a real measured similarity so a
# low-confidence hit is visible in results and logs rather than indistinguishable
# from an exact match.
_STRUCTURAL_STRATEGIES = frozenset(
    {
        "exact",
        "line_trimmed",
        "whitespace_normalized",
        "indentation_flexible",
        "escape_normalized",
        "trimmed_boundary",
    }
)


@dataclass
class FuzzyMatchResult:
    """Structured result of a fuzzy find-and-replace, carrying which strategy won.

    strategy/similarity are populated only on a successful replacement; on error
    or no-match they stay None so callers can branch on `error` exactly as before.
    `near_miss` carries the closest rejected region (snippet + similarity) so a
    failure surfaces a concrete "did you mean" instead of a generic message.
    """

    content: str
    count: int
    error: str | None = None
    strategy: str | None = None
    similarity: float | None = None
    near_miss: "NearMiss | None" = None


@dataclass
class NearMiss:
    """Closest region that failed to match, for actionable failure messages."""

    similarity: float
    snippet: str
    line_number: int  # 1-indexed start line of the snippet in the source


@dataclass(frozen=True)
class FuzzyConfig:
    """Strictness knob for the two similarity-guessing strategies.

    Structural strategies (exact/whitespace/indentation/escape/boundary) always run and
    are unaffected by this config — they are faithful by construction. Only the two
    guessing strategies (block_anchor, context_aware) are gated here.

    - block_anchor_threshold: min middle-line similarity for a *unique* anchor candidate.
      The historical 0.10 accepted blocks sharing ~nothing with the pattern; the safe
      default is 0.5. (Multi-candidate anchors always use max(this, 0.6) to disambiguate.)
    - context_aware_line_fraction: fraction of pattern lines that must be highly similar.
      Historically 0.5 (half the lines could be wrong); default tightened to 0.66.
    - context_aware_min_similarity: additional whole-span similarity floor for a
      context_aware hit, so a block passing the per-line fraction still has to resemble
      the pattern overall. 0.0 disables the floor.
    - allow_fuzzy: when False, the two guessing strategies are dropped (structural
      normalization still runs — a safe middle rung of the ladder).
    - allow_structural: when False, ONLY byte-exact match runs (top rung: exact-or-fail).
      Implies no fuzzy.
    """

    block_anchor_threshold: float = 0.5
    context_aware_line_fraction: float = 0.66
    context_aware_min_similarity: float = 0.4
    allow_fuzzy: bool = True
    allow_structural: bool = True


# Backward-compatible defaults used when no config is threaded through.
DEFAULT_CONFIG = FuzzyConfig()
# Exact-or-fail: high-risk edits that must not be relocated OR reshaped by any normalization.
STRICT_CONFIG = FuzzyConfig(allow_fuzzy=False, allow_structural=False)


UNICODE_MAP = {
    "\u201c": '"',
    "\u201d": '"',  # smart double quotes
    "\u2018": "'",
    "\u2019": "'",  # smart single quotes
    "\u2014": "--",
    "\u2013": "-",  # em/en dashes
    "\u2026": "...",
    "\u00a0": " ",  # ellipsis and non-breaking space
}


def _unicode_normalize(text: str) -> str:
    """Normalizes Unicode characters to their standard ASCII equivalents."""
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def fuzzy_find_and_replace(
    content: str, old_string: str, new_string: str, replace_all: bool = False
) -> tuple[str, int, str | None]:
    """
    Find and replace text using a chain of increasingly fuzzy matching strategies.

    Thin 3-tuple wrapper over `fuzzy_find_and_replace_ex` for callers that only
    need (content, count, error). New code that wants to know *which* strategy
    matched (and how confidently) should call `fuzzy_find_and_replace_ex`.

    Args:
        content: The file content to search in
        old_string: The text to find
        new_string: The replacement text
        replace_all: If True, replace all occurrences; if False, require uniqueness

    Returns:
        Tuple of (new_content, match_count, error_message)
        - If successful: (modified_content, number_of_replacements, None)
        - If failed: (original_content, 0, error_description)
    """
    result = fuzzy_find_and_replace_ex(content, old_string, new_string, replace_all)
    return result.content, result.count, result.error


def fuzzy_find_and_replace_ex(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    config: FuzzyConfig | None = None,
) -> FuzzyMatchResult:
    """
    Find and replace text, also reporting which strategy matched and its similarity.

    Adds observability over `fuzzy_find_and_replace`: on success the result carries the
    winning strategy name and a similarity score (1.0 for structural strategies; the
    measured ratio for the two fuzzy strategies). On failure it carries a `near_miss`
    (closest rejected region + similarity) so the caller can surface a concrete snippet.

    `config` tunes the two similarity-guessing strategies (default `DEFAULT_CONFIG`;
    `STRICT_CONFIG` disables fuzziness for exact-or-fail edits). Structural strategies
    always run regardless of config.
    """
    cfg = config or DEFAULT_CONFIG

    if not old_string:
        return FuzzyMatchResult(content, 0, "old_string cannot be empty")

    if old_string == new_string:
        return FuzzyMatchResult(content, 0, "old_string and new_string are identical")

    # Strictness ladder. Byte-exact always runs. Structural normalizers (faithful by
    # construction) run unless `allow_structural` is off (top rung: exact-or-fail). The
    # two similarity-guessing strategies run only when `allow_fuzzy` is on. No separate
    # code path — the chain is just trimmed per config.
    strategies: list[tuple[str, Callable]] = [("exact", _strategy_exact)]
    if cfg.allow_structural:
        strategies += [
            ("line_trimmed", _strategy_line_trimmed),
            ("whitespace_normalized", _strategy_whitespace_normalized),
            ("indentation_flexible", _strategy_indentation_flexible),
            ("escape_normalized", _strategy_escape_normalized),
            ("trimmed_boundary", _strategy_trimmed_boundary),
        ]
    if cfg.allow_fuzzy and cfg.allow_structural:
        strategies += [
            ("block_anchor", lambda c, p: _strategy_block_anchor(c, p, cfg)),
            ("context_aware", lambda c, p: _strategy_context_aware(c, p, cfg)),
        ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)

        if matches:
            # Found matches with this strategy
            if len(matches) > 1 and not replace_all:
                return FuzzyMatchResult(
                    content,
                    0,
                    (
                        f"Found {len(matches)} matches for old_string. "
                        f"Provide more context to make it unique, or use replace_all=True."
                    ),
                )

            # Perform replacement
            new_content = _apply_replacements(content, matches, new_string)
            similarity = _match_similarity(content, old_string, matches, strategy_name)
            return FuzzyMatchResult(
                new_content,
                len(matches),
                None,
                strategy=strategy_name,
                similarity=similarity,
            )

    # No strategy matched: surface the closest region so the failure is actionable.
    near_miss = _closest_region(content, old_string)
    suffix = ""
    if near_miss is not None:
        suffix = (
            f" Closest region (similarity {near_miss.similarity}) at line "
            f"{near_miss.line_number}:\n{near_miss.snippet}"
        )
    return FuzzyMatchResult(
        content,
        0,
        f"Could not find a match for old_string in the file.{suffix}",
        near_miss=near_miss,
    )


def _closest_region(content: str, pattern: str, max_snippet_lines: int = 12) -> NearMiss | None:
    """Return the file window most similar to `pattern` (a "did you mean" hint).

    Slides a pattern-sized line window across the file and keeps the best
    SequenceMatcher ratio. Bounded snippet length keeps the surfaced error compact.
    """
    content_lines = content.split("\n")
    pattern_lines = pattern.split("\n")
    window = max(1, len(pattern_lines))
    if not content_lines or not any(content_lines):
        return None

    pattern_norm = pattern.strip()
    best_ratio = -1.0
    best_start = 0
    # When the pattern is longer than the file, still compare against the whole file.
    last_start = max(0, len(content_lines) - window)
    for i in range(last_start + 1):
        block = "\n".join(content_lines[i : i + window])
        ratio = SequenceMatcher(None, pattern_norm, block.strip()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    snippet_lines = content_lines[best_start : best_start + min(window, max_snippet_lines)]
    snippet = "\n".join(snippet_lines)
    if len(snippet) > 800:  # hard cap so a huge single line can't bloat the message
        snippet = snippet[:800] + " …"
    return NearMiss(
        similarity=round(max(best_ratio, 0.0), 3),
        snippet=snippet,
        line_number=best_start + 1,
    )


def _match_similarity(
    content: str, pattern: str, matches: list[tuple[int, int]], strategy_name: str
) -> float:
    """Return a confidence score for the winning match.

    Structural strategies are faithful by construction -> 1.0. The two fuzzy
    strategies guess, so we report the worst (minimum) measured ratio across the
    matched span(s): that is the number that should make a wrong-location hit
    look suspicious in logs/results.
    """
    if strategy_name in _STRUCTURAL_STRATEGIES:
        return 1.0

    worst = 1.0
    for start, end in matches:
        ratio = SequenceMatcher(None, pattern.strip(), content[start:end].strip()).ratio()
        worst = min(worst, ratio)
    # Round so the surfaced value stays compact ("similarity": 0.5) in tool JSON.
    return round(worst, 3)


def _apply_replacements(content: str, matches: list[tuple[int, int]], new_string: str) -> str:
    """
    Apply replacements at the given positions.

    Args:
        content: Original content
        matches: List of (start, end) positions to replace
        new_string: Replacement text

    Returns:
        Content with replacements applied
    """
    # Sort matches by position (descending) to replace from end to start
    # This preserves positions of earlier matches
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)

    result = content
    for start, end in sorted_matches:
        result = result[:start] + new_string + result[end:]

    return result


# =============================================================================
# Matching Strategies
# =============================================================================


def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 1: Exact string match."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    """
    Strategy 2: Match with line-by-line whitespace trimming.

    Strips leading/trailing whitespace from each line before matching.
    """
    # Normalize pattern and content by trimming each line
    pattern_lines = [line.strip() for line in pattern.split("\n")]
    pattern_normalized = "\n".join(pattern_lines)

    content_lines = content.split("\n")
    content_normalized_lines = [line.strip() for line in content_lines]

    # Build mapping from normalized positions back to original positions
    return _find_normalized_matches(
        content, content_lines, content_normalized_lines, pattern, pattern_normalized
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """
    Strategy 3: Collapse multiple whitespace to single space.
    """

    def normalize(s):
        # Collapse multiple spaces/tabs to single space, preserve newlines
        return re.sub(r"[ \t]+", " ", s)

    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)

    # Find in normalized, map back to original
    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)

    if not matches_in_normalized:
        return []

    # Map positions back to original content
    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[tuple[int, int]]:
    """
    Strategy 4: Ignore indentation differences entirely.

    Strips all leading whitespace from lines before matching.
    """

    def strip_indent(s):
        return "\n".join(line.lstrip() for line in s.split("\n"))

    pattern_stripped = strip_indent(pattern)

    content_lines = content.split("\n")
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split("\n")]

    return _find_normalized_matches(
        content, content_lines, content_stripped_lines, pattern, "\n".join(pattern_lines)
    )


def _strategy_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """
    Strategy 5: Convert escape sequences to actual characters.

    Handles \\n -> newline, \\t -> tab, etc.
    """

    def unescape(s):
        # Convert common escape sequences
        return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

    pattern_unescaped = unescape(pattern)

    if pattern_unescaped == pattern:
        # No escapes to convert, skip this strategy
        return []

    return _strategy_exact(content, pattern_unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[tuple[int, int]]:
    """
    Strategy 6: Trim whitespace from first and last lines only.

    Useful when the pattern boundaries have whitespace differences.
    """
    pattern_lines = pattern.split("\n")
    if not pattern_lines:
        return []

    # Trim only first and last lines
    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()

    modified_pattern = "\n".join(pattern_lines)

    content_lines = content.split("\n")

    # Search through content for matching block
    matches = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i : i + pattern_line_count]

        # Trim first and last of this block
        check_lines = block_lines.copy()
        check_lines[0] = check_lines[0].strip()
        if len(check_lines) > 1:
            check_lines[-1] = check_lines[-1].strip()

        if "\n".join(check_lines) == modified_pattern:
            # Found match - calculate original positions
            start_pos = sum(len(line) + 1 for line in content_lines[:i])
            end_pos = sum(len(line) + 1 for line in content_lines[: i + pattern_line_count]) - 1
            if end_pos >= len(content):
                end_pos = len(content)
            matches.append((start_pos, end_pos))

    return matches


def _strategy_block_anchor(
    content: str, pattern: str, config: FuzzyConfig | None = None
) -> list[tuple[int, int]]:
    """
    Strategy 7: Match by anchoring on first and last lines.

    Anchors on the first/last line, then gates the middle by measured similarity.
    The threshold comes from `config` (default `DEFAULT_CONFIG`): the old hard-coded
    0.10 accepted a block whose middle shared almost nothing with the pattern.
    """
    cfg = config or DEFAULT_CONFIG
    # Normalize both strings for comparison while keeping original content for offset calculation
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)

    pattern_lines = norm_pattern.split("\n")
    if len(pattern_lines) < 2:
        return []

    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()

    # Use normalized lines for matching logic
    norm_content_lines = norm_content.split("\n")
    # BUT use original lines for calculating start/end positions to prevent index shift
    orig_content_lines = content.split("\n")

    pattern_line_count = len(pattern_lines)

    potential_matches = []
    for i in range(len(norm_content_lines) - pattern_line_count + 1):
        if (
            norm_content_lines[i].strip() == first_line
            and norm_content_lines[i + pattern_line_count - 1].strip() == last_line
        ):
            potential_matches.append(i)

    matches = []
    candidate_count = len(potential_matches)

    # Thresholding: a unique anchor candidate uses the configured floor; multiple
    # candidates raise it (>= 0.6) so an ambiguous anchor demands a strong middle.
    # The old 0.10/0.30 pair let a near-empty-overlap block win a unique anchor.
    threshold = (
        cfg.block_anchor_threshold if candidate_count == 1 else max(cfg.block_anchor_threshold, 0.6)
    )

    for i in potential_matches:
        if pattern_line_count <= 2:
            similarity = 1.0
        else:
            # Compare normalized middle sections
            content_middle = "\n".join(norm_content_lines[i + 1 : i + pattern_line_count - 1])
            pattern_middle = "\n".join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()

        if similarity >= threshold:
            # Calculate positions using ORIGINAL lines to ensure correct character offsets in the file
            start_pos = sum(len(line) + 1 for line in orig_content_lines[:i])
            end_pos = (
                sum(len(line) + 1 for line in orig_content_lines[: i + pattern_line_count]) - 1
            )
            matches.append((start_pos, min(end_pos, len(content))))

    return matches


def _strategy_context_aware(
    content: str, pattern: str, config: FuzzyConfig | None = None
) -> list[tuple[int, int]]:
    """
    Strategy 8: Line-by-line similarity with a configurable line fraction + span floor.

    Accepts a block where at least `context_aware_line_fraction` of pattern lines are
    highly similar AND (when set) the whole span resembles the pattern above
    `context_aware_min_similarity`. The old fixed 0.5 fraction let half the lines be
    wrong with no whole-span sanity check.
    """
    cfg = config or DEFAULT_CONFIG
    pattern_lines = pattern.split("\n")
    content_lines = content.split("\n")

    if not pattern_lines:
        return []

    matches = []
    pattern_line_count = len(pattern_lines)
    pattern_norm = pattern.strip()

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i : i + pattern_line_count]

        # Calculate line-by-line similarity
        high_similarity_count = 0
        for p_line, c_line in zip(pattern_lines, block_lines):
            sim = SequenceMatcher(None, p_line.strip(), c_line.strip()).ratio()
            if sim >= 0.80:
                high_similarity_count += 1

        if high_similarity_count < pattern_line_count * cfg.context_aware_line_fraction:
            continue

        # Whole-span floor: a block can clear the per-line fraction yet still be a poor
        # overall match (e.g. matching lines interleaved with junk). Reject those.
        if cfg.context_aware_min_similarity > 0.0:
            span = "\n".join(block_lines).strip()
            if SequenceMatcher(None, pattern_norm, span).ratio() < cfg.context_aware_min_similarity:
                continue

        start_pos = sum(len(line) + 1 for line in content_lines[:i])
        end_pos = sum(len(line) + 1 for line in content_lines[: i + pattern_line_count]) - 1
        if end_pos >= len(content):
            end_pos = len(content)
        matches.append((start_pos, end_pos))

    return matches


# =============================================================================
# Helper Functions
# =============================================================================


def _find_normalized_matches(
    content: str,
    content_lines: list[str],
    content_normalized_lines: list[str],
    pattern: str,
    pattern_normalized: str,
) -> list[tuple[int, int]]:
    """
    Find matches in normalized content and map back to original positions.

    Args:
        content: Original content string
        content_lines: Original content split by lines
        content_normalized_lines: Normalized content lines
        pattern: Original pattern
        pattern_normalized: Normalized pattern

    Returns:
        List of (start, end) positions in the original content
    """
    pattern_norm_lines = pattern_normalized.split("\n")
    num_pattern_lines = len(pattern_norm_lines)

    matches = []

    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        # Check if this block matches
        block = "\n".join(content_normalized_lines[i : i + num_pattern_lines])

        if block == pattern_normalized:
            # Found a match - calculate original positions
            start_pos = sum(len(line) + 1 for line in content_lines[:i])
            end_pos = sum(len(line) + 1 for line in content_lines[: i + num_pattern_lines]) - 1

            # Handle case where end is past content
            if end_pos >= len(content):
                end_pos = len(content)

            matches.append((start_pos, end_pos))

    return matches


def _map_normalized_positions(
    original: str, normalized: str, normalized_matches: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """
    Map positions from normalized string back to original.

    This is a best-effort mapping that works for whitespace normalization.
    """
    if not normalized_matches:
        return []

    # Build character mapping from normalized to original
    orig_to_norm = []  # orig_to_norm[i] = position in normalized

    orig_idx = 0
    norm_idx = 0

    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in " \t" and normalized[norm_idx] == " ":
            # Original has space/tab, normalized collapsed to space
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            # Don't advance norm_idx yet - wait until all whitespace consumed
            if orig_idx < len(original) and original[orig_idx] not in " \t":
                norm_idx += 1
        elif original[orig_idx] in " \t":
            # Extra whitespace in original
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            # Mismatch - shouldn't happen with our normalization
            orig_to_norm.append(norm_idx)
            orig_idx += 1

    # Fill remaining
    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1

    # Reverse mapping: for each normalized position, find original range
    norm_to_orig_start = {}
    norm_to_orig_end = {}

    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos

    # Map matches
    original_matches = []
    for norm_start, norm_end in normalized_matches:
        # Find original start
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            # Find nearest
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)

        # Find original end
        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)

        # Expand to include trailing whitespace that was normalized
        while orig_end < len(original) and original[orig_end] in " \t":
            orig_end += 1

        original_matches.append((orig_start, min(orig_end, len(original))))

    return original_matches
