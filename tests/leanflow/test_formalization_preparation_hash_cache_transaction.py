"""Publish only exact verified Lake sidecars through the existing no-clobber setup transaction."""

from __future__ import annotations

import multiprocessing
from dataclasses import replace
from pathlib import Path

import pytest

from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_authorization import PreparationAuthorization
from leanflow_cli.formalization.preparation_delta import infrastructure_baseline, managed_delta
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.preparation_stage import copy_stage
from leanflow_cli.formalization.preparation_transaction import (
    commit_preparation,
    load_preparation_transaction,
    protected_inputs,
)
from tests.leanflow.test_formalization_preparation_authorization import (
    _git,
    _verified_cache_fixture,
)


@pytest.fixture
def cache_transaction(tmp_path, monkeypatch):
    """Keep the canonical cache absent while a separately pinned private stage verifies it."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root, stage = tmp_path / "project", tmp_path / "stage"
    original, record, source, sidecar = _verified_cache_fixture(root)
    sidecar.unlink()
    baseline = infrastructure_baseline(root)
    protected = protected_inputs(root)
    copy_stage(root, stage)
    authority = PreparationAuthorization.capture(stage, original.plan, check=lambda: None).acquire(
        stage, git=_git, check=lambda: None
    )
    private = stage / ".lake/packages/sample/Lock.txt.hash"
    private.write_text(record.hash_value)
    record = replace(record, cache_identity=str(entry_identity(private)))
    extended = authority.with_verified_hash_caches(stage, (record,), check=lambda: None)

    def authorize(selected: Path) -> None:
        extended.validate(selected, git=_git, check=lambda: None)

    writes = managed_delta(root, stage, baseline, authorize=authorize)
    relative = ".lake/packages/sample/Lock.txt.hash"
    assert writes == {relative: None}
    assert authority.verified_hash_caches == ()
    assert extended.package_sources is authority.package_sources
    return {
        "root": root,
        "stage": stage,
        "source": source,
        "sidecar": sidecar,
        "relative": relative,
        "writes": writes,
        "protected": protected,
        "record": record,
        "authority": extended,
        "authorize": authorize,
    }


def _commit(case, *, checkpoint=None):
    """Exercise the real infrastructure journal without claiming mathematical acceptance."""
    return commit_preparation(
        case["root"],
        case["stage"],
        managed=case["writes"],
        protected=case["protected"],
        receipt={"verified_hash_caches": [case["record"].to_dict()]},
        validate=lambda: case["authorize"](case["root"]),
        authorize=case["authorize"],
        cancelled=lambda: False,
        checkpoint=checkpoint,
    )


def _concurrent_cache_writer(path, start, finished):
    """Create an independently owned destination at the controller's selected boundary."""
    if not start.wait(10):
        raise RuntimeError("Controller did not reach the cache publication barrier")
    Path(path).write_text("independent cache writer")
    finished.set()


def test_verified_absent_cache_is_committed_as_named_infrastructure(cache_transaction):
    case = cache_transaction
    journal = _commit(case)
    assert journal["state"] == "COMMITTED"
    assert set(journal["writes"]) == {case["relative"]}
    assert journal["writes"][case["relative"]]["before"] is None
    assert journal["plan"]["receipt"]["verified_hash_caches"] == [case["record"].to_dict()]
    assert case["sidecar"].read_text() == case["record"].hash_value
    assert case["source"].read_bytes() == b"alpha\r\nbeta\r\n"
    assert protected_inputs(case["root"]) == case["protected"]
    case["authority"].validate(case["root"], git=_git, check=lambda: None)


def test_failed_cache_transaction_rolls_back_only_its_new_sidecar(cache_transaction):
    case = cache_transaction

    def checkpoint(name):
        if name == "before_commit":
            raise RuntimeError("injected post-install failure")

    with pytest.raises(RuntimeError, match="post-install"):
        _commit(case, checkpoint=checkpoint)
    assert not case["sidecar"].exists()
    assert case["source"].read_bytes() == b"alpha\r\nbeta\r\n"
    assert protected_inputs(case["root"]) == case["protected"]
    assert load_preparation_transaction(case["root"])["state"] == "ROLLED_BACK"


@pytest.mark.parametrize("boundary", ["prepared", "install"])
def test_concurrent_cache_destination_is_preserved_as_a_conflict(cache_transaction, boundary):
    case = cache_transaction
    context = multiprocessing.get_context("spawn")
    start, finished = context.Event(), context.Event()
    writer = context.Process(
        target=_concurrent_cache_writer, args=(str(case["sidecar"]), start, finished)
    )
    writer.start()

    def checkpoint(name):
        if name == ("prepared" if boundary == "prepared" else "install:" + case["relative"]):
            start.set()
            assert finished.wait(5), "Independent writer did not reach its publication barrier"

    try:
        with pytest.raises((ValueError, OSError)):
            _commit(case, checkpoint=checkpoint)
    finally:
        start.set()
        writer.join(5)
        if writer.is_alive():
            writer.kill()
            writer.join(5)
            pytest.fail("Owned concurrent writer did not close")
    assert writer.exitcode == 0
    assert case["sidecar"].read_text() == "independent cache writer"
    assert case["source"].read_bytes() == b"alpha\r\nbeta\r\n"
    assert protected_inputs(case["root"]) == case["protected"]
    assert load_preparation_transaction(case["root"])["state"] in {"ROLLED_BACK", "CONFLICTED"}


@pytest.mark.parametrize("change", ["delete", "corrupt"])
def test_published_cache_changes_the_real_dependency_fingerprint_when_lost_or_changed(
    cache_transaction, change
):
    case = cache_transaction
    _commit(case)
    before = identity(dependency_inputs(case["root"]))
    if change == "delete":
        case["sidecar"].unlink()
    else:
        case["sidecar"].write_text("60224162a80c236f")
    assert identity(dependency_inputs(case["root"])) != before
    with pytest.raises((ValueError, OSError)):
        case["authority"].validate(case["root"], git=_git, check=lambda: None)
