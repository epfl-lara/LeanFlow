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


def test_default_search_sees_lean_sources_only(tmp_path: Path) -> None:
    """lean_search's offline fallback is a lemma finder; prose would be noise there."""
    (tmp_path / "Lemmas.lean").write_text("-- deque_bound lemma\n")
    (tmp_path / "NOTES.md").write_text("deque_bound discussion\n")

    result = search_sources("deque_bound", tmp_path, Path)

    assert [Path(m["path"]).name for m in result["results"]] == ["Lemmas.lean"]


def test_project_search_also_reaches_the_projects_documentation(tmp_path: Path) -> None:
    """A project's notes, extracted papers and blueprints were unreachable.

    read_file needs a path and nothing could discover one: search_project grepped
    *.lean only, so 130 KB of formalization notes sitting next to the target were
    invisible to the prover for an entire run.
    """
    from leanflow_cli.workflows.prover.session_search import PROJECT_GLOBS

    (tmp_path / "Lemmas.lean").write_text("-- deque_bound lemma\n")
    (tmp_path / "NOTES_deque.md").write_text("deque_bound: what was tried\n")
    (tmp_path / "paper.txt").write_text("deque_bound proof sketch\n")
    (tmp_path / "blueprint.tex").write_text("\\section{deque_bound}\n")
    (tmp_path / "refs.bib").write_text("@article{deque_bound}\n")
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 deque_bound binary\n")

    result = search_sources("deque_bound", tmp_path, Path, globs=PROJECT_GLOBS)

    found = sorted(Path(m["path"]).name for m in result["results"])
    assert found == ["Lemmas.lean", "NOTES_deque.md", "blueprint.tex", "paper.txt", "refs.bib"]
    # Binaries stay out even when their bytes happen to contain the query.
    assert "paper.pdf" not in found
