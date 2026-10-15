"""Preserve supplied bibliography originals without inventing a rendered job name."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from leanflow_cli.formalization import source_tex
from leanflow_cli.formalization.source_bibliography import (
    BIBLIOGRAPHY_MAX_BYTES,
    discover_bibliography_support,
)
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import digest


def test_supplied_root_jobname_bbl_is_frozen_without_a_database(tmp_path: Path) -> None:
    source = tmp_path / "foo.tex"
    source.write_text(r"\begin{document}\bibliography{bar}\end{document}")
    bibliography = tmp_path / "foo.bbl"
    bibliography.write_text(r"\begin{thebibliography}{1}\bibitem{x}Reference\end{thebibliography}")

    result = inventory_source(tmp_path, source)

    assert result["files"].get("foo.bbl", {}).get("sha256") == digest(bibliography.read_bytes())
    assert not any(row["code"] == "missing_support_file" for row in result["diagnostics"])
    missing = [
        row for row in result["diagnostics"] if row["code"] == "missing_bibliography_database"
    ]
    assert len(missing) == 1 and missing[0]["blocking"] is False
    assert missing[0]["support_target"] == "bar"
    assert source.read_text() == r"\begin{document}\bibliography{bar}\end{document}"


def test_databases_and_supplied_jobname_output_are_both_frozen(tmp_path: Path) -> None:
    source = tmp_path / "foo.tex"
    source.write_text(r"\bibliography{bar}")
    (tmp_path / "bar.bib").write_text("@article{x,title={Original database}}")
    bibliography = tmp_path / "foo.bbl"
    bibliography.write_text("Original rendered bibliography")

    first = inventory_source(tmp_path, source)
    assert set(first["files"]) == {"foo.tex", "foo.bbl", "bar.bib"}
    assert not any(
        row["code"]
        in {"missing_support_file", "missing_bibliography_database", "ambiguous_bibliography"}
        for row in first["diagnostics"]
    )
    assert any(row["blocking"] for row in first["diagnostics"])

    bibliography.write_text("Changed rendered bibliography")
    second = inventory_source(tmp_path, source)
    assert first["bundle_hash"] != second["bundle_hash"]
    assert first["files"]["bar.bib"] == second["files"]["bar.bib"]


def test_nested_selected_root_uses_its_own_jobname(tmp_path: Path) -> None:
    selected = tmp_path / "sources" / "foo.tex"
    selected.parent.mkdir()
    selected.write_text(r"\bibliography{bar}")
    bibliography = selected.with_suffix(".bbl")
    bibliography.write_text("Nested root bibliography")

    result = inventory_source(tmp_path, selected)

    assert result["files"]["sources/foo.bbl"]["sha256"] == digest(bibliography.read_bytes())
    assert not any(row["code"] == "missing_support_file" for row in result["diagnostics"])
    assert result["extraction_status"] == "blocked"


def test_included_bibliography_uses_selected_root_not_chapter_jobname(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\input{parts/chapter}")
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "chapter.tex").write_text(r"\bibliography{bar}")
    (parts / "chapter.bbl").write_text("An unrelated chapter job")
    (tmp_path / "foo.bbl").write_text("Root job bibliography")

    result = inventory_source(tmp_path, selected)

    assert "foo.bbl" in result["files"]
    assert "parts/chapter.bbl" not in result["files"]
    assert not any(row["code"] == "missing_support_file" for row in result["diagnostics"])


def test_distinct_root_jobname_locations_remain_blocking_ambiguity(tmp_path: Path) -> None:
    selected = tmp_path / "sources" / "foo.tex"
    selected.parent.mkdir()
    selected.write_text(r"\bibliography{bar}")
    selected.with_suffix(".bbl").write_text("Selected-directory bibliography")
    (tmp_path / "foo.bbl").write_text("Different working-directory bibliography")

    result = inventory_source(tmp_path, selected)

    assert {"sources/foo.bbl", "foo.bbl"} <= result["files"].keys()
    ambiguity = [row for row in result["diagnostics"] if row["code"] == "ambiguous_bibliography"]
    assert len(ambiguity) == 1 and ambiguity[0]["blocking"] is True
    assert ambiguity[0]["support_candidates"] == ["sources/foo.bbl", "foo.bbl"]
    assert result["extraction_status"] == "blocked"


def test_wrong_jobname_bbl_is_preserved_as_context_without_satisfying_rendering(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}")
    bibliography = tmp_path / "refs.bbl"
    bibliography.write_text("Previously supported explicit bibliography")

    result = inventory_source(tmp_path, selected)

    assert result["files"]["refs.bbl"]["sha256"] == digest(bibliography.read_bytes())
    missing = [row for row in result["diagnostics"] if row["code"] == "missing_support_file"]
    assert len(missing) == 1 and missing[0]["blocking"] is True
    assert missing[0]["support_role"] == "rendered_bibliography"
    assert missing[0]["support_target"] == "foo.bbl"


def test_missing_database_and_missing_jobname_remain_unresolved(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{missing}")
    (tmp_path / "unrelated.bbl").write_text("Do not guess this output")

    result = inventory_source(tmp_path, selected)

    assert set(result["files"]) == {"foo.tex"}
    missing = [row for row in result["diagnostics"] if row["code"] == "missing_support_file"]
    assert len(missing) == 1 and missing[0]["blocking"] is True


def test_addbibresource_cannot_use_unrelated_root_jobname_output(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\addbibresource{missing.bib}")
    (tmp_path / "foo.bbl").write_text("Not an explicit BibLaTeX source")

    result = inventory_source(tmp_path, selected)

    assert "foo.bbl" not in result["files"]
    missing = [row for row in result["diagnostics"] if row["code"] == "missing_support_file"]
    assert len(missing) == 1 and missing[0]["blocking"] is True


@pytest.mark.parametrize("kind", ["inside_symlink", "outside_symlink", "directory", "fifo"])
def test_nonregular_or_aliased_jobname_output_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    selected = root / "foo.tex"
    selected.write_text(r"\bibliography{missing}")
    candidate = root / "foo.bbl"
    target = (root if kind == "inside_symlink" else tmp_path) / "original.bbl"
    target.write_text("Do not freeze a source through an alias")
    if kind.endswith("symlink"):
        candidate.symlink_to(target)
    elif kind == "directory":
        candidate.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("A real FIFO requires POSIX")
        os.mkfifo(candidate)
    original_reader = source_tex.read_regular_bytes
    reads = []

    def read(path: Path, limit: int) -> bytes:
        reads.append(path)
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    result = inventory_source(root, selected)

    assert candidate not in reads and target not in reads
    assert set(result["files"]) == {"foo.tex"}
    assert any(
        row["code"] == "missing_support_file" and row["blocking"] for row in result["diagnostics"]
    )


def test_outside_database_is_not_read_to_resolve_a_missing_support(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    selected = root / "foo.tex"
    selected.write_text(r"\bibliography{../outside}")
    (tmp_path / "outside.bib").write_text("Outside the frozen source bundle")

    result = inventory_source(root, selected)

    assert set(result["files"]) == {"foo.tex"}
    assert any(
        row["code"] == "missing_support_file" and row["blocking"] for row in result["diagnostics"]
    )


def test_dynamic_bibliography_argument_keeps_its_blocker_even_with_jobname_output(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{\dynamic}")
    (tmp_path / "foo.bbl").write_text("Frozen candidate is not macro-execution authority")

    result = inventory_source(tmp_path, selected)

    assert "foo.bbl" in result["files"]
    assert any(
        row["code"] == "missing_support_file" and row["blocking"] for row in result["diagnostics"]
    )


def test_support_discovery_rejects_an_outside_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.tex"
    outside.write_text("Outside root")
    with pytest.raises(ValueError, match="inside its original bundle"):
        discover_bibliography_support(
            root, outside, root / "chapter.tex", "refs", lambda *args: None
        )


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_replaced_support_is_rejected_by_the_original_bounded_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    if replacement == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("A real FIFO requires POSIX")
    root = tmp_path / "project"
    root.mkdir()
    selected = root / "foo.tex"
    selected.write_text(r"\bibliography{bar}")
    bibliography = root / "foo.bbl"
    bibliography.write_text("Original bibliography")
    outside = tmp_path / "outside.bbl"
    outside.write_text("Do not read outside the original bundle")
    original_reader = source_tex.read_regular_bytes

    def read(path: Path, limit: int) -> bytes:
        if path == bibliography:
            path.unlink()
            if replacement == "fifo":
                os.mkfifo(path)
            else:
                path.symlink_to(outside)
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    with pytest.raises((OSError, ValueError)):
        inventory_source(root, selected)
    assert outside.read_text() == "Do not read outside the original bundle"


@pytest.mark.parametrize("database", ["refs", "foo"])
def test_case_variant_jobname_is_frozen_as_ambiguous_context(tmp_path: Path, database: str) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{" + database + "}")
    case_variant = tmp_path / "Foo.bbl"
    case_variant.write_text("Do not equate a case alias with the exact jobname")

    result = inventory_source(tmp_path, selected)

    assert "Foo.bbl" in result["files"] and "foo.bbl" not in result["files"]
    ambiguous = [row for row in result["diagnostics"] if row["code"] == "ambiguous_bibliography"]
    assert len(ambiguous) == 1 and ambiguous[0]["blocking"] is True
    assert ambiguous[0]["support_case_aliases"] == ["Foo.bbl"]
    assert any(
        row["code"] == "missing_support_file" and row.get("support_role") == "rendered_bibliography"
        for row in result["diagnostics"]
    )


@pytest.mark.parametrize("role", ["root_bbl", "database", "literal_bbl", "addbibresource"])
def test_oversized_bibliography_is_reported_before_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    selected = tmp_path / "foo.tex"
    command = r"\addbibresource{refs.bib}" if role == "addbibresource" else r"\bibliography{refs}"
    selected.write_text(command)
    name = "foo.bbl" if role == "root_bbl" else "refs.bbl" if role == "literal_bbl" else "refs.bib"
    bibliography = tmp_path / name
    with bibliography.open("wb") as stream:
        stream.truncate(BIBLIOGRAPHY_MAX_BYTES + 1)
    original_reader = source_tex.read_regular_bytes
    observed = []

    def read(path: Path, limit: int) -> bytes:
        if path == bibliography:
            observed.append(limit)
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)

    result = inventory_source(tmp_path, selected)

    assert observed == [] and name not in result["files"]
    oversized = [
        row
        for row in result["diagnostics"]
        if row.get("support_unavailable_reason") == "size_limit"
    ]
    assert len(oversized) == 1 and oversized[0]["blocking"] is True
    assert oversized[0]["support_file"] == name
    assert oversized[0]["support_size_bytes"] == BIBLIOGRAPHY_MAX_BYTES + 1
    assert oversized[0]["support_limit_bytes"] == BIBLIOGRAPHY_MAX_BYTES
    if role in {"database", "literal_bbl"}:
        assert any(
            row["code"] == "missing_support_file"
            and row.get("support_role") == "rendered_bibliography"
            and row["blocking"]
            for row in result["diagnostics"]
        )


def test_oversized_database_is_a_nonblocking_note_when_rendered_support_is_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}\begin{lemma}A\end{lemma}")
    rendered = tmp_path / "foo.bbl"
    rendered.write_text("Usable original rendered bibliography")
    database = tmp_path / "refs.bib"
    with database.open("wb") as stream:
        stream.truncate(5 * 1024 * 1024)
    original_reader = source_tex.read_regular_bytes

    def read(path: Path, limit: int) -> bytes:
        assert path != database, "An unavailable database must never be read"
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    result = inventory_source(tmp_path, selected)

    assert result["files"]["foo.bbl"]["sha256"] == digest(rendered.read_bytes())
    assert "refs.bib" not in result["files"]
    unavailable = [
        row for row in result["diagnostics"] if row["code"] == "missing_bibliography_database"
    ]
    assert len(unavailable) == 1 and unavailable[0]["blocking"] is False
    assert unavailable[0]["support_target"] == "refs"
    assert unavailable[0]["support_file"] == "refs.bib"
    assert unavailable[0]["support_size_bytes"] == 5 * 1024 * 1024
    assert unavailable[0]["support_limit_bytes"] == BIBLIOGRAPHY_MAX_BYTES
    assert not any(row["code"] == "missing_support_file" for row in result["diagnostics"])
    assert any(
        row["code"] == "unsupported_tex_effect" and row["blocking"] for row in result["diagnostics"]
    )
    assert len(result["items"]) == 1 and result["items"][0]["scope"] == "candidate"


@pytest.mark.parametrize("command", [r"\bibliography{refs}", r"\addbibresource{refs.bib}"])
def test_bibliography_growth_after_preflight_keeps_the_fixed_reader_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(command)
    (tmp_path / "foo.bbl").write_text("Usable rendered original")
    database = tmp_path / "refs.bib"
    database.write_text("Initially bounded database")
    original_reader = source_tex.read_regular_bytes
    observed = []

    def read(path: Path, limit: int) -> bytes:
        if path == database:
            observed.append(limit)
            with path.open("r+b") as stream:
                stream.truncate(BIBLIOGRAPHY_MAX_BYTES + 1)
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    with pytest.raises(ValueError, match="bounded regular file"):
        inventory_source(tmp_path, selected)
    assert observed == [BIBLIOGRAPHY_MAX_BYTES]


@pytest.mark.parametrize("command", [r"\bibliography{refs}", r"\addbibresource{refs.bib}"])
def test_unrelated_bibliography_reader_errors_are_not_size_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(command)
    (tmp_path / "foo.bbl").write_text("Usable rendered original")
    database = tmp_path / "refs.bib"
    database.write_text("Original database")
    original_reader = source_tex.read_regular_bytes

    def read(path: Path, limit: int) -> bytes:
        if path == database:
            raise ValueError("Regular-file identity changed during this read")
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    with pytest.raises(ValueError, match="identity changed"):
        inventory_source(tmp_path, selected)


def test_oversized_second_jobname_preserves_ambiguity_without_granting_database_fallback(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "sources" / "foo.tex"
    selected.parent.mkdir()
    selected.write_text(r"\bibliography{refs}")
    selected.with_suffix(".bbl").write_text("Usable selected-directory rendering")
    for candidate in [tmp_path / "foo.bbl", tmp_path / "refs.bib"]:
        with candidate.open("wb") as stream:
            stream.truncate(BIBLIOGRAPHY_MAX_BYTES + 1)

    result = inventory_source(tmp_path, selected)

    assert "sources/foo.bbl" in result["files"]
    assert not {"foo.bbl", "refs.bib"} & result["files"].keys()
    assert any(
        row["code"] == "ambiguous_bibliography" and row["blocking"] for row in result["diagnostics"]
    )
    unavailable = [
        row
        for row in result["diagnostics"]
        if row.get("support_unavailable_reason") == "size_limit"
    ]
    assert len(unavailable) == 2 and all(row["blocking"] for row in unavailable)
    assert not any(row["code"] == "missing_bibliography_database" for row in result["diagnostics"])


def test_all_named_databases_are_complete_support_without_a_generated_bbl(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{first,second.bib}")
    (tmp_path / "first.bib").write_text("@article{first}")
    (tmp_path / "second.bib").write_text("@article{second}")

    result = inventory_source(tmp_path, selected)

    assert set(result["files"]) == {"foo.tex", "first.bib", "second.bib"}
    assert not any(row["code"] == "missing_support_file" for row in result["diagnostics"])
    assert any(
        row["code"] == "unsupported_tex_effect" and row["blocking"] for row in result["diagnostics"]
    )


def test_one_missing_database_without_rendered_jobname_stays_blocking(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{first,second}")
    (tmp_path / "first.bib").write_text("@article{first}")
    (tmp_path / "second.bbl").write_text("Context from a different render")

    result = inventory_source(tmp_path, selected)

    assert {"first.bib", "second.bbl"} <= result["files"].keys()
    assert any(
        row["code"] == "missing_support_file"
        and row.get("support_role") == "rendered_bibliography"
        and row["blocking"]
        for row in result["diagnostics"]
    )


def test_support_reader_uses_fixed_limit_for_small_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}")
    (tmp_path / "foo.bbl").write_text("Rendered original")
    (tmp_path / "refs.bib").write_text("@article{x}")
    original_reader = source_tex.read_regular_bytes
    observed = {}

    def read(path: Path, limit: int) -> bytes:
        observed[path.name] = limit
        return original_reader(path, limit)

    monkeypatch.setattr(source_tex, "read_regular_bytes", read)
    inventory_source(tmp_path, selected)

    assert observed["foo.bbl"] == observed["refs.bib"] == 4 * 1024 * 1024
    assert observed["foo.tex"] == selected.stat().st_size


def test_bibliography_context_cannot_grant_structure_execution_authority(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}\begin{lemma}A\end{lemma}")
    (tmp_path / "foo.bbl").write_text("Rendered original is unexecuted context")

    result = inventory_source(tmp_path, selected)

    assert any(
        row["code"] == "unsupported_tex_effect" and row["blocking"] for row in result["diagnostics"]
    )
    assert len(result["items"]) == 1 and result["items"][0]["scope"] == "candidate"
    assert result["extraction_status"] == "blocked"


def test_nonregular_case_alias_is_recorded_without_reading_it(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}")
    (tmp_path / "refs.bib").write_text("@article{x}")
    (tmp_path / "Foo.bbl").mkdir()

    result = inventory_source(tmp_path, selected)

    assert "Foo.bbl" not in result["files"]
    assert any(
        row["code"] == "ambiguous_bibliography"
        and row.get("support_case_aliases") == ["Foo.bbl"]
        and row["blocking"]
        for row in result["diagnostics"]
    )


def test_support_discovery_refuses_lexical_escape_before_directory_lookup(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(ValueError, match="inside its original bundle"):
        discover_bibliography_support(
            root, root / "../outside.tex", root / "foo.tex", "refs", lambda *args: None
        )


def test_exact_support_byte_limit_is_accepted_without_truncation(tmp_path: Path) -> None:
    selected = tmp_path / "foo.tex"
    selected.write_text(r"\bibliography{refs}")
    bibliography = tmp_path / "foo.bbl"
    with bibliography.open("wb") as stream:
        stream.truncate(BIBLIOGRAPHY_MAX_BYTES)

    result = inventory_source(tmp_path, selected)

    assert result["files"]["foo.bbl"]["size"] == BIBLIOGRAPHY_MAX_BYTES
    assert result["files"]["foo.bbl"]["sha256"] == digest(bibliography.read_bytes())


def test_existing_frozen_support_cannot_bypass_the_fixed_byte_limit(tmp_path: Path) -> None:
    bibliography = tmp_path / "foo.bbl"
    bibliography.write_text("A previously recorded support input")
    bundle = source_tex.TexBundle(tmp_path, tmp_path / "foo.tex", "doc-test")
    bundle.files["foo.bbl"] = {"size": BIBLIOGRAPHY_MAX_BYTES + 1}

    with pytest.raises(ValueError, match="bounded regular file"):
        bundle._binary(bibliography)
