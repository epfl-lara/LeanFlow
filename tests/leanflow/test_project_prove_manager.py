"""Tests for the extracted project_prove_manager helpers + native_runner re-export (Phase 2).

These cover the pure, closed-under-"calls" subset of the ``/prove`` project-manager moved out of
native_runner: the per-file excerpt builders, the theorem/file difficulty heuristics, the import
dependency graph + transitive reachability, and the fallback candidate ordering. The
re-export-identity test pins every moved name to the same object on ``native_runner`` so the
work-queue orchestration still living there resolves the extracted helpers without a back-import.
"""

from pathlib import Path

from leanflow_cli.native import native_runner
from leanflow_cli.workflows import project_prove_manager as ppm


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in project_prove_manager,
    # so the orchestration helpers still living in native_runner resolve them without a back-import.
    for name in ppm.__all__:
        assert getattr(native_runner, name) is getattr(ppm, name), name


def test_manager_active_predicate():
    assert ppm._project_prove_manager_active(None) is False
    assert ppm._project_prove_manager_active({}) is False
    assert ppm._project_prove_manager_active({"project_prove_manager_enabled": False}) is False
    assert ppm._project_prove_manager_active({"project_prove_manager_enabled": True}) is True


def test_bounded_excerpt_truncates_middle():
    # Under the budget: returned verbatim (stripped).
    assert ppm._project_prove_bounded_excerpt("  hello  ", 100) == "hello"
    # max_chars <= 0 short-circuits to the stripped raw.
    assert ppm._project_prove_bounded_excerpt("abc", 0) == "abc"
    # Over budget: head + truncation marker + tail, and the marker is present.
    big = "A" * 500 + "B" * 500
    out = ppm._project_prove_bounded_excerpt(big, 200)
    assert "excerpt truncated; middle omitted" in out
    assert out.startswith("A")
    assert out.endswith("B")


def test_declaration_difficulty_weights_hard_and_easy_tokens():
    base = ppm._project_prove_declaration_difficulty({"name": "foo", "text": ""})
    # A Putnam competition name should score strictly harder than a neutral one.
    hard = ppm._project_prove_declaration_difficulty({"name": "putnam_1999", "text": ""})
    assert hard > base
    # An easy "mathd" token should score strictly easier than neutral.
    easy = ppm._project_prove_declaration_difficulty({"name": "mathd_basic", "text": ""})
    assert easy < base
    # Score never goes negative.
    assert (
        ppm._project_prove_declaration_difficulty({"name": "mathd mathd linear abs", "text": ""})
        >= 0
    )


def test_fallback_order_prefers_downstream_then_difficulty():
    candidates = [
        {"label": "Hard.lean", "candidate_downstream_count": 0, "difficulty_score": 9},
        {"label": "Core.lean", "candidate_downstream_count": 3, "difficulty_score": 5},
        {"label": "Easy.lean", "candidate_downstream_count": 0, "difficulty_score": 1},
    ]
    order = ppm._project_prove_fallback_order(candidates)
    # Highest candidate-downstream count wins outright.
    assert order[0] == "Core.lean"
    # Among the rest, the easier file comes first.
    assert order.index("Easy.lean") < order.index("Hard.lean")


def test_dependency_graph_and_transitive_paths(tmp_path: Path):
    # A.lean imports B; B imports C. module_to_path maps module names to file paths.
    a = tmp_path / "A.lean"
    b = tmp_path / "B.lean"
    c = tmp_path / "C.lean"
    a.write_text("import B\n", encoding="utf-8")
    b.write_text("import C\n", encoding="utf-8")
    c.write_text("-- leaf\n", encoding="utf-8")
    module_to_path = {"A": a, "B": b, "C": c}
    imports_by_path, imported_by_path, import_modules = ppm._project_prove_dependency_graph(
        [a, b, c], module_to_path
    )
    assert b.resolve() in imports_by_path[a.resolve()]
    assert c.resolve() in imports_by_path[b.resolve()]
    assert import_modules[a.resolve()] == ["B"]
    # Transitive imports of A reach both B and C.
    reachable = ppm._project_prove_transitive_paths(a, imports_by_path)
    assert {b.resolve(), c.resolve()} <= reachable


def test_module_name_for_project_path(tmp_path: Path):
    nested = tmp_path / "Foo" / "Bar.lean"
    nested.parent.mkdir(parents=True)
    nested.write_text("", encoding="utf-8")
    assert ppm._module_name_for_project_path(nested, tmp_path) == "Foo.Bar"
    # Non-.lean paths produce an empty module name.
    assert ppm._module_name_for_project_path(tmp_path / "Foo" / "data.txt", tmp_path) == ""
