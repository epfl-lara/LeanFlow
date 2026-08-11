"""Tests for bounded native queue-item source classification."""

from __future__ import annotations

from leanflow_cli.workflows import queue_item_predicates


def test_current_queue_item_indexes_large_source_once(monkeypatch, tmp_path) -> None:
    """Keep queue selection linear in source size, not source size times queue size."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem selected : True := by\n  sorry\n", encoding="utf-8")
    queue = [
        {
            "label": f"missing_{index}",
            "file": str(active),
            "reasons": ["contains sorry"],
        }
        for index in range(200)
    ]
    queue.append(
        {
            "label": "selected",
            "file": str(active),
            "reasons": ["contains sorry"],
        }
    )
    calls = 0

    def declaration_index(_active_file: str):
        nonlocal calls
        calls += 1
        return [{"name": "selected", "line": 1, "end_line": 2, "has_sorry": True}]

    monkeypatch.setattr(
        queue_item_predicates,
        "_declaration_line_index",
        declaration_index,
    )

    selected = queue_item_predicates._current_queue_item(queue, str(active))

    assert selected is not None
    assert selected["label"] == "selected"
    assert calls == 1


def test_current_queue_item_preserves_frontier_and_curriculum_order(monkeypatch, tmp_path) -> None:
    """Preserve manager precedence and tie-break semantics after source indexing."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem first : True := by\n  sorry\n", encoding="utf-8")
    queue = [
        {"label": "first", "file": str(active), "reasons": ["contains sorry"]},
        {"label": "second", "file": str(active), "reasons": ["contains sorry"]},
        {"label": "missing", "file": str(active), "reasons": ["contains sorry"]},
    ]
    monkeypatch.setattr(
        queue_item_predicates,
        "_declaration_line_index",
        lambda _active_file: [{"name": "first"}, {"name": "second"}],
    )

    selected = queue_item_predicates._current_queue_item(
        queue,
        str(active),
        precedence=lambda label: 0 if label in {"second", "missing"} else 1,
        order_key=lambda label: 0 if label == "missing" else 1,
    )

    assert selected is not None
    assert selected["label"] == "second"
