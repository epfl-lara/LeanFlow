"""Exercise shared deterministic workflow helpers independently of retired engines."""

from __future__ import annotations

from leanflow_cli.native import source_only_startup


def test_source_revision_guard_rejects_even_same_path_after_rewrite(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    revision = source_only_startup.capture_source_revision(str(active))
    assert revision is not None
    assert source_only_startup.source_revision_is_current(revision)

    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    assert not source_only_startup.source_revision_is_current(revision)
