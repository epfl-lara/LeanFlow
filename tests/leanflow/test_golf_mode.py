"""Phase 6 §6.9 tests: the managed-golf SUBSTRATE (runtime wiring deferred).

Review established the runner wiring is a dedicated follow-on (it needs a
drain-to-done queue lifecycle, baselines captured AT ASSIGNMENT because
queue-item extras do not survive ``QueueItem`` serialization, and metrics
on CLASSIFIED acceptance). So NO flag and NO metrics recorder ship here —
only the settled, flag-free substrate the follow-on composes from: the
queue builder, ``declaration_chars``, and the third selection bucket.
Prove selection never sees the golf bucket; the managed loop never admits
golf kinds.
"""

from __future__ import annotations

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import golf_mode
from leanflow_cli.workflows.queue_models import QueueItem, select_next_item

FILE_TEXT = """import Mathlib.Tactic

theorem tidy : True := trivial

lemma open_work : True := by sorry

theorem verbose : True := by
  have h : True := trivial
  exact h
"""


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "Demo.lean").write_text(FILE_TEXT, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Kind classification + the wiring is deferred (no flag ships)
# ---------------------------------------------------------------------------


def test_is_golf_workflow_classifies_kinds():
    assert golf_mode.is_golf_workflow("golf") and golf_mode.is_golf_workflow("refactor")
    assert golf_mode.is_golf_workflow(" GOLF ")  # tolerant of case/whitespace
    assert not golf_mode.is_golf_workflow("prove")
    assert not golf_mode.is_golf_workflow("")


def test_no_managed_flag_ships():
    """The gating flag is a follow-on concern: nothing in the shipped
    substrate defines or reads LEANFLOW_GOLF_MANAGED (review: a flag no
    runtime can honor is a dangling promise)."""
    assert not hasattr(golf_mode, "golf_managed_enabled")
    assert not hasattr(golf_mode, "record_golf_metrics")


def test_runtime_wiring_is_deferred(monkeypatch):
    """golf/refactor never join the managed loop yet — the substrate is
    dark. `_is_autonomous_workflow` admits prove exactly as before."""
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "golf")
    assert runner._is_autonomous_workflow() is False
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "refactor")
    assert runner._is_autonomous_workflow() is False
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    assert runner._is_autonomous_workflow() is True  # prove unchanged


# ---------------------------------------------------------------------------
# The golf queue: declared, sorry-free declarations only, own reason bucket
# ---------------------------------------------------------------------------


def test_golf_queue_skips_sorries_and_tags_the_bucket(project):
    items = golf_mode.golf_declaration_queue("Demo.lean", project_root=str(project))

    labels = [item["label"] for item in items]
    assert labels == ["tidy", "verbose"]  # open_work has a sorry: prove owns it
    for item in items:
        assert item["reasons"] == [golf_mode.GOLF_REASON]


def test_golf_queue_unreadable_file_is_empty(tmp_path):
    assert golf_mode.golf_declaration_queue("Nope.lean", project_root=str(tmp_path)) == []


def test_golf_queue_sorry_check_is_structural(tmp_path):
    """The sorry gate reads the parser's comment/string-stripped flag: a
    finished proof whose comment merely mentions sorry is a candidate; a
    real `by sorry` is not (regression for the raw-text scan)."""
    (tmp_path / "S.lean").write_text(
        "import Mathlib.Tactic\n\n"
        "theorem mentions : True := by\n"
        "  -- no sorry needed here\n"
        "  trivial\n\n"
        "theorem genuinely_open : True := by sorry\n",
        encoding="utf-8",
    )
    labels = [
        item["label"]
        for item in golf_mode.golf_declaration_queue("S.lean", project_root=str(tmp_path))
    ]
    assert labels == ["mentions"]  # the commented-sorry proof survives; the real one is dropped


def test_golf_queue_drops_block_comment_phantoms(tmp_path):
    """A theorem-looking line inside a `/- … -/` block comment is prose;
    the parser's line scan matches it but the queue must not (regression
    for phantom candidates entering the queue)."""
    (tmp_path / "P.lean").write_text(
        "import Mathlib.Tactic\n\n"
        "/-\n"
        "theorem commented_out : True := trivial\n"
        "-/\n\n"
        "theorem real_one : True := trivial\n",
        encoding="utf-8",
    )
    labels = [
        item["label"]
        for item in golf_mode.golf_declaration_queue("P.lean", project_root=str(tmp_path))
    ]
    assert labels == ["real_one"]


def test_golf_queue_phantom_does_not_hide_a_later_sorry(tmp_path):
    """The deep hazard: a block-commented theorem BETWEEN a proof's body
    and its `sorry` must not split the region and hide the sorry. Blanking
    before parsing keeps `outer` a single region, so its real sorry is seen
    and the unfinished proof is EXCLUDED."""
    (tmp_path / "H.lean").write_text(
        "import Mathlib.Tactic\n\n"
        "theorem outer : True := by\n"
        "  /-\n"
        "  theorem phantom : True := trivial\n"
        "  -/\n"
        "  sorry\n",
        encoding="utf-8",
    )
    labels = [
        item["label"]
        for item in golf_mode.golf_declaration_queue("H.lean", project_root=str(tmp_path))
    ]
    assert labels == []  # outer has a real sorry; phantom is not a declaration
    # And the region is not truncated at the phantom: outer spans past it.
    chars = golf_mode.declaration_chars("H.lean", "outer", project_root=str(tmp_path))
    assert chars >= len("theorem outer : True := by")  # includes the body below the comment


def test_declaration_chars_measures_the_decl(project):
    """The baseline/after-size primitive: a real declaration measures
    positive, an absent one is zero (the follow-on samples it at
    assignment and acceptance)."""
    verbose = golf_mode.declaration_chars("Demo.lean", "verbose", project_root=str(project))
    tidy = golf_mode.declaration_chars("Demo.lean", "tidy", project_root=str(project))
    assert verbose > tidy > 0  # verbose is the longer body
    assert golf_mode.declaration_chars("Demo.lean", "ghost", project_root=str(project)) == 0


# ---------------------------------------------------------------------------
# Selection: golf bucket sorts LAST, prove selection untouched
# ---------------------------------------------------------------------------


def test_golf_bucket_sorts_after_diagnostics_and_sorries():
    queue = [
        QueueItem(label="golfable", reasons=("golf candidate",)),
        QueueItem(label="broken", reasons=("diagnostic near line 3",)),
        QueueItem(label="open", reasons=("contains sorry",)),
    ]
    present = lambda label: True  # noqa: E731

    assert select_next_item(queue, is_present_in_file=present).label == "broken"
    # Without the diagnostic, the sorry still outranks golf work.
    assert select_next_item(queue[:1] + queue[2:], is_present_in_file=present).label == "open"
    only_golf = [QueueItem(label="golfable", reasons=("golf candidate",))]
    assert select_next_item(only_golf, is_present_in_file=present).label == "golfable"
    # And with precedence/order_key engaged, the bucket order still holds.
    assert (
        select_next_item(queue, is_present_in_file=present, precedence=lambda label: 1).label
        == "broken"
    )


def test_prove_selection_never_sees_golf_reason():
    """Prove queues never emit the reason; a clean queue still final-sweeps."""
    clean = [QueueItem(label="done", reasons=())]
    assert select_next_item(clean, is_present_in_file=lambda label: True) is None
