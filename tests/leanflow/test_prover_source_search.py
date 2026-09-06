"""Keep bounded source retrieval honest about omitted matches and previews."""

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.session_search import search_sources


@pytest.mark.parametrize("count, truncated", [(8, False), (9, True), (24, True)])
def test_per_file_match_limit_reports_only_actual_omission(
    tmp_path: Path, count: int, truncated: bool
) -> None:
    source = tmp_path / "Lemmas.lean"
    source.write_text("".join(f"-- useful_lemma {index}\n" for index in range(count)))

    result = search_sources("useful_lemma", tmp_path, Path)

    assert result["success"]
    assert len(result["results"]) == 8
    assert [match["line"] for match in result["results"]] == list(range(1, 9))
    assert result["truncated"] is truncated


def test_long_matching_line_reports_clipped_preview(tmp_path: Path) -> None:
    source = tmp_path / "Lemmas.lean"
    source.write_text("-- useful_lemma " + "x" * 3000 + "\n")

    result = search_sources("useful_lemma", tmp_path, Path)

    assert result["success"]
    assert len(result["results"]) == 1
    assert len(result["results"][0]["text"]) == 2000
    assert result["truncated"] is True


def test_unreadable_matches_do_not_affect_visible_completeness(tmp_path: Path) -> None:
    hidden = tmp_path / "Hidden.lean"
    hidden.write_text("-- useful_lemma\n" * 9)
    visible = tmp_path / "Visible.lean"
    visible.write_text("-- useful_lemma\n")

    def readable(raw: str) -> Path:
        """Reject a protected file before its search hits enter the result."""
        path = Path(raw)
        if path == hidden:
            raise ValueError("protected source")
        return path

    result = search_sources("useful_lemma", tmp_path, readable)

    assert result["success"]
    assert [match["path"] for match in result["results"]] == [str(visible)]
    assert result["truncated"] is False
