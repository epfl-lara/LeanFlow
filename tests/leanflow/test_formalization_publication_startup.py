"""Recover interrupted publication before either workflow observes canonical source."""

from pathlib import Path

import pytest

from leanflow_cli.formalization.publication import (
    PublicationConflict,
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
    recover_pending_publication,
)
from leanflow_cli.formalization.publication_journal import load_registry
from leanflow_cli.native import harness_workflow
from leanflow_cli.workflows.prover import entrypoint, runtime

ORIGINAL = b"theorem goal : True := by sorry\n"


def _interrupt(root: Path, boundary: str = "before_commit") -> None:
    """Leave an actual registered publication at a process interruption boundary."""
    (root / "Main.lean").write_bytes(ORIGINAL)
    before = {"Main.lean": ORIGINAL, "Added.lean": None}
    outputs = {"Main.lean": b"candidate", "Added.lean": b"added"}

    def checkpoint(point: str) -> None:
        if point == boundary:
            raise SystemExit("interrupted process")

    with pytest.raises(SystemExit):
        publish_revision(
            root=root,
            directory=root / ".leanflow/native-workflows/old-run/publication",
            revision_id="revision",
            before=before,
            outputs=outputs,
            plan=freeze_publication_plan("revision", before, outputs, owned_paths=outputs),
            validate=lambda: None,
            checkpoint=checkpoint,
        )


def _launch(root: Path, monkeypatch: pytest.MonkeyPatch, kind: str, calls: list[str]):
    """Run the public startup boundary while replacing only subsequent model work."""
    if kind == "native":

        def controller(*args, **kwargs):
            calls.append("native")
            assert (root / "Main.lean").read_bytes() == ORIGINAL
            return {"status": "continued"}

        monkeypatch.setattr(harness_workflow, "_run_controller", controller)
        return harness_workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(root),
                "LEANFLOW_WORKFLOW_RUN_ID": "restart",
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            }
        )

    def run(instance):
        calls.append("prover")
        assert (root / "Main.lean").read_bytes() == ORIGINAL
        return {"status": "completed", "metrics": {}}

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(root))
    # Restore the workflow kind written by the real observer at test teardown.
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "restart")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Main.lean")
    monkeypatch.delenv("LEANFLOW_PROVER_RESUME_RUN_ID", raising=False)
    monkeypatch.setattr(runtime.ProverRuntime, "run", run)
    return entrypoint.main()


@pytest.mark.parametrize("kind", ["native", "prover"])
def test_workflow_startup_recovers_before_loading_source(tmp_path, monkeypatch, kind):
    _interrupt(tmp_path)
    with pytest.raises(PublicationConflict, match="requires recovery"):
        assert_no_pending_publication(tmp_path)
    calls = []
    result = _launch(tmp_path, monkeypatch, kind, calls)
    assert result == ({"status": "continued"} if kind == "native" else 0)
    assert calls == [kind]
    assert (tmp_path / "Main.lean").read_bytes() == ORIGINAL
    assert not (tmp_path / "Added.lean").exists()
    assert_no_pending_publication(tmp_path)


@pytest.mark.parametrize("kind", ["native", "prover"])
def test_workflow_startup_preserves_conflicting_user_save(tmp_path, monkeypatch, kind):
    _interrupt(tmp_path)
    source = tmp_path / "Main.lean"
    source.write_bytes(b"user edit after interruption")
    calls = []
    if kind == "native":
        with pytest.raises(PublicationConflict, match="Publication conflict"):
            _launch(tmp_path, monkeypatch, kind, calls)
    else:
        assert _launch(tmp_path, monkeypatch, kind, calls) == 1
    assert calls == []
    assert source.read_bytes() == b"user edit after interruption"
    assert not (tmp_path / "Added.lean").exists()
    with pytest.raises(PublicationConflict):
        recover_pending_publication(tmp_path)
    assert source.read_bytes() == b"user edit after interruption"


def test_restart_settles_durable_commit_before_receipt_loading(tmp_path):
    _interrupt(tmp_path, "journal:COMMITTED")
    assert load_registry(tmp_path)["active"] is not None
    recovered = recover_pending_publication(tmp_path)
    assert recovered["status"] == "COMMITTED"
    registry = load_registry(tmp_path)
    assert registry["active"] is None
    assert registry["selected_revision"]["revision_id"] == "revision"
    assert (tmp_path / "Main.lean").read_bytes() == b"candidate"
    assert (tmp_path / "Added.lean").read_bytes() == b"added"
    assert recover_pending_publication(tmp_path) is None
