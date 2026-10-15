"""Pin restart and retention invariants for journals spanning multiple source captures."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import source_mutation, source_transaction
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.source import SourceConflictError
from leanflow_cli.workflows.prover.source_journal import load_journal

BEFORE = b"theorem goal : True := by sorry\n"
MIDDLE = b"-- first planning edit\n" + BEFORE
AFTER = b"-- second planning edit\n" + BEFORE


class _Verifier:
    """Keep the source recovery tests independent of Lean compilation and providers."""

    def capture_signatures(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"accepted": True}


def _runtime(root: Path) -> ProverRuntime:
    """Initialize ordinary durable prover state with one protected source file."""
    (root / "Main.lean").write_bytes(BEFORE)
    (root / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    return ProverRuntime(
        root=root,
        targets=[root / "Main.lean"],
        config=ProverConfig(),
        verifier=_Verifier(),
        session=lambda **kwargs: {},
    )


def test_multiple_writes_restart_after_all_source_rollbacks_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    target = tmp_path / "Main.lean"
    journal = source_transaction.begin_materialization(runtime)
    source_transaction.materialized_write(runtime, journal, target, MIDDLE)
    source_transaction.materialized_write(runtime, journal, target, AFTER)
    persist = runtime._persist
    monkeypatch.setattr(runtime, "_persist", lambda: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        source_transaction.recover_materialization(runtime, journal)
    assert target.read_bytes() == BEFORE
    restarted = load_journal(runtime)
    assert restarted is not None and len(restarted["recovered_mutations"]) == 2
    monkeypatch.setattr(runtime, "_persist", persist)
    source_transaction.recover_materialization(runtime, restarted)
    runtime._assert_sources()
    assert target.read_bytes() == BEFORE
    assert not (runtime.store.directory / "source-transaction.json").exists()


@pytest.mark.parametrize(
    "boundary", ["capture_pending", "captured", "install_pending", "installed"]
)
def test_outer_materialization_recovers_missing_canonical_name_after_capture_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    runtime = _runtime(tmp_path)
    target = tmp_path / "Main.lean"
    journal = source_transaction.begin_materialization(runtime)
    install = source_mutation.install

    def crash_install(root: Path, identifier: str, checkpoint=None) -> None:
        def crash(name: str) -> None:
            if name == boundary:
                raise SystemExit(name)

        install(root, identifier, crash)

    monkeypatch.setattr(source_mutation, "install", crash_install)
    with pytest.raises(SystemExit, match=boundary):
        source_transaction.materialized_write(runtime, journal, target, AFTER)
    monkeypatch.setattr(source_mutation, "install", install)
    source_transaction.recover_materialization(runtime, journal)
    runtime._assert_sources()
    assert target.read_bytes() == BEFORE


def test_materialization_preflight_does_not_roll_back_other_files_over_late_edit(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    first = tmp_path / "Main.lean"
    second = tmp_path / "Helper.lean"
    second.write_bytes(BEFORE)
    descriptor = os.open(first, os.O_RDWR)
    journal = source_transaction.begin_materialization(runtime)
    try:
        source_transaction.materialized_write(runtime, journal, first, AFTER)
        source_transaction.materialized_write(runtime, journal, second, MIDDLE)
        edit = b"-- user edit through old descriptor\n" + BEFORE
        os.write(descriptor, edit)
        os.ftruncate(descriptor, len(edit))
        os.fsync(descriptor)
        with pytest.raises(SourceConflictError, match="late edit retained"):
            source_transaction.recover_materialization(runtime, journal)
    finally:
        os.close(descriptor)
    assert second.read_bytes() == MIDDLE
    retained = source_mutation.directory(tmp_path, journal["mutations"][0]) / "displaced"
    assert retained.read_bytes() == edit


def test_late_descriptor_write_to_rolled_back_candidate_is_retained_and_blocks_future_checks(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    target = tmp_path / "Main.lean"
    journal = source_transaction.begin_materialization(runtime)
    source_transaction.materialized_write(runtime, journal, target, AFTER)
    descriptor = os.open(target, os.O_RDWR)
    try:
        source_transaction.recover_materialization(runtime, journal)
        edit = b"-- user edit through replaced candidate descriptor\n" + AFTER
        os.write(descriptor, edit)
        os.ftruncate(descriptor, len(edit))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert target.read_bytes() == BEFORE
    with pytest.raises(SourceConflictError, match="late editor version retained"):
        runtime._assert_sources()
    directory = source_mutation.directory(tmp_path, journal["mutations"][0])
    assert any(path.is_file() and path.read_bytes() == edit for path in directory.iterdir())


def test_source_journal_rejects_files_beyond_bound_before_capture(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    target = tmp_path / "Main.lean"
    journal = source_transaction.begin_materialization(runtime)
    with pytest.raises(ValueError, match="8 MiB"):
        source_transaction.materialized_write(
            runtime, journal, target, b"x" * (8 * 1024 * 1024 + 1)
        )
    assert target.read_bytes() == BEFORE
    assert "mutations" not in journal
