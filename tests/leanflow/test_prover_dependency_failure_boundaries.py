"""Keep pre-effect dependency failures resumable and report canonical cache changes truthfully."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.formalization.publication_io import PublicationUnsupported
from leanflow_cli.workflows.prover import libraries, library_transaction, source_mutation
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from tests.leanflow.library_checkout_fixture import REQUEST
from tests.leanflow.test_prover_library_transaction import _project, _resolved
from tests.leanflow.test_prover_recovery import runtime_at


def test_unsupported_capture_retains_candidate_without_retrying_a_noop_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real proof integration through failure, constructor resume and later acceptance."""
    runtime = runtime_at(tmp_path, session=lambda **_: {})
    original = (tmp_path / "Main.lean").read_bytes()
    original_inode = (tmp_path / "Main.lean").stat().st_ino
    calls = []

    def unsupported(*_: Any) -> None:
        calls.append(True)
        raise PublicationUnsupported("This filesystem has no supported no-replace operation")

    with monkeypatch.context() as patch:
        patch.setattr(source_mutation, "check_filesystem", unsupported)
        with pytest.raises(InfrastructureFailure) as stopped:
            runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
        assert stopped.value.status == "environment_error"
    assert len(calls) == 1
    assert (tmp_path / "Main.lean").read_bytes() == original
    assert (tmp_path / "Main.lean").stat().st_ino == original_inode
    assert not (runtime.store.directory / "source-transaction.json").exists()
    assert runtime.dag.nodes[0].candidate == ["trivial"]
    resumed = runtime_at(tmp_path, session=lambda **_: {}, run_id=runtime.run_id, resume=True)
    assert resumed.dag.nodes[0].candidate == ["trivial"]
    assert resumed._accept(resumed.dag.nodes[0], ["trivial"], "controller")
    assert resumed.dag.nodes[0].status == "proved"


@pytest.mark.parametrize("boundary", ["before_promotion", "after_checkout"])
def test_cache_failure_message_requires_actual_canonical_checkout_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Distinguish an interrupted promotion call from a genuinely retained new package inode."""
    before = _project(tmp_path)
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    )
    monkeypatch.setattr(libraries, "_run_update", lambda stage, *a: _resolved(stage))
    if boundary == "before_promotion":

        def stop_before(*_: Any, **__: Any) -> list[str]:
            raise TimeoutError("Deadline reached before any promotion effect")

        monkeypatch.setattr(library_transaction, "promote", stop_before)
    else:

        def stop_after(*_: Any, **__: Any) -> None:
            raise ValueError("Failure after the new checkout reached canonical cache")

        monkeypatch.setattr(source_mutation, "install", stop_after)
    result = libraries.install_libraries(tmp_path, [REQUEST])
    assert result["accepted"] is False and result["status"] == "rolled_back"
    assert result["dependency_cache_may_have_changed"] is (boundary == "after_checkout")
    assert (tmp_path / ".lake/packages/physlib").exists() is (boundary == "after_checkout")
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert not library_transaction.pending_path(tmp_path).exists()


def test_running_conflict_guidance_explains_pending_library_blocks_fresh_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_at(tmp_path, session=lambda **_: {})
    runtime.state["design_plan_complete"] = True

    def stop() -> None:
        raise InfrastructureFailure(
            "Retained library configuration conflicts", status="source_conflict"
        )

    monkeypatch.setattr(runtime, "_messages", stop)
    result = runtime.run()
    assert result["status"] == "source_conflict"
    assert "pending conflicted library" in result["next_step"].lower()
    assert "blocks fresh runs" in result["next_step"]
