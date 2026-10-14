"""Exercise whole-batch cache rollback, exact retained versions, and durable admission fencing."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
import uuid

import pytest

from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.workflows.prover import scoped_cache as cache
from leanflow_cli.workflows.prover import scoped_cache_barrier as barrier


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "project"
    library = root / "derived-cache/lib/lean"
    library.mkdir(parents=True)
    (root / "A.lean").write_bytes(b"protected source bytes")
    replacements = {}
    for name in ("A", "B"):
        for suffix in (
            ".olean",
            ".olean.server",
            ".olean.private",
            ".ir",
            ".ir.sig",
            ".ilean",
            ".ilean.hash",
            ".trace",
            ".hash",
        ):
            path = library / (name + suffix)
            path.write_bytes(("original " + path.name).encode())
            path.chmod(0o600)
            content = (
                None
                if suffix in {".trace", ".hash", ".ir.sig", ".ilean", ".ilean.hash"}
                else ("compiled " + path.name).encode()
            )
            replacements[path] = content, cache.cache_version(path)
    return root, replacements


def _retained(root):
    return root / ".leanflow/scoped-import-cache" / uuid.uuid4().hex


def _install(root, replacements, retained, **kwargs):
    with publication_lock(root):
        return cache.install_cache_batch(root, replacements, retained, **kwargs)


def _assert_originals(replacements):
    assert all(cache.cache_version(path) == before for path, (_, before) in replacements.items())


def _replace_process(path, held, content):
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; p,h=map(Path,sys.argv[1:3]); p.rename(h); p.write_bytes(sys.argv[3].encode())",
            str(path),
            str(held),
            content,
        ],
        check=True,
    )


def test_success_retires_metadata_and_obsolete_parts_without_losing_original_inodes(batch):
    root, replacements = batch
    retained = _retained(root)
    result = _install(root, replacements, retained)
    assert cache.cache_refresh_needed(root) is None
    cache.assert_cache_ready(root)
    for path, (content, before) in replacements.items():
        assert cache.cache_version(retained / "before" / path.relative_to(root)) == before
        if content is None:
            assert not path.exists()
            assert path.relative_to(root).as_posix() in result["retired_artifacts"]
        else:
            assert path.read_bytes() == content
    assert (root / "A.lean").read_bytes() == b"protected source bytes"
    assert json.loads((retained / "outcome.json").read_text())["status"] == "COMMITTED"


@pytest.mark.parametrize("failure", ["exception", "cancel", "deadline", "source_drift"])
def test_two_module_batch_restores_originals_after_first_publish_or_postguard_fault(batch, failure):
    root, replacements = batch
    retained = _retained(root)
    first = next(iter(replacements)).relative_to(root).as_posix()
    published = False

    def checkpoint(name):
        nonlocal published
        if name == "installed:" + first:
            published = True
            if failure == "exception":
                raise RuntimeError("First module published; later operation failed")

    def after_commit():
        assert published
        if failure == "cancel":
            raise InterruptedError("Author requested Stop")
        if failure == "deadline":
            raise TimeoutError("Campaign deadline reached")
        raise ValueError("Frozen source changed after publication")

    expected = (
        RuntimeError
        if failure == "exception"
        else (
            InterruptedError
            if failure == "cancel"
            else TimeoutError if failure == "deadline" else ValueError
        )
    )
    with pytest.raises(expected) as error:
        _install(root, replacements, retained, checkpoint=checkpoint, after_commit=after_commit)
    _assert_originals(replacements)
    assert cache.cache_refresh_needed(root) is not None
    with pytest.raises(ValueError, match="needs refresh"):
        cache.assert_cache_ready(root)
    assert error.value.scoped_cache_report["refresh_required"] is True
    assert not error.value.scoped_cache_report["preserved_unknown_versions"]
    assert (retained / "rollback" / first).read_bytes() == replacements[root / first][0]
    assert (root / "A.lean").read_bytes() == b"protected source bytes"


@pytest.mark.parametrize("boundary", ["capture", "install", "installed"])
def test_actual_second_process_unknown_replacement_is_never_overwritten_or_deleted(batch, boundary):
    root, replacements = batch
    target = next(iter(replacements))
    relative = target.relative_to(root).as_posix()
    retained = _retained(root)
    held = target.with_name(target.name + ".held")

    def checkpoint(name):
        if name != boundary + ":" + relative:
            return
        if boundary == "install":
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'independent cache version')",
                    str(target),
                ],
                check=True,
            )
        else:
            _replace_process(target, held, "independent cache version")
        if boundary == "installed":
            raise RuntimeError("Stop after independent replacement")

    with pytest.raises((OSError, ValueError, RuntimeError)):
        _install(root, replacements, retained, checkpoint=checkpoint)
    assert target.read_bytes() == b"independent cache version"
    if boundary == "capture":
        assert cache.cache_version(held) == replacements[target][1]
    else:
        assert cache.cache_version(retained / "before" / relative) == replacements[target][1]
    assert cache.cache_refresh_needed(root) is not None
    for path, (_, before) in replacements.items():
        if path != target:
            assert cache.cache_version(path) == before


def test_rollback_capture_verifies_actual_second_process_swap_before_restore(batch, monkeypatch):
    root, replacements = batch
    target = next(iter(replacements))
    relative = target.relative_to(root).as_posix()
    retained = _retained(root)
    held = target.with_name(target.name + ".installed-held")
    rename = cache.rename_exclusive

    def raced(source, destination):
        if source == target and destination == retained / "rollback" / relative:
            _replace_process(target, held, "independent rollback-race version")
        return rename(source, destination)

    monkeypatch.setattr(cache, "rename_exclusive", raced)

    def checkpoint(name):
        if name == "installed:" + relative:
            raise RuntimeError("Trigger whole-batch rollback")

    with pytest.raises(RuntimeError) as error:
        _install(root, replacements, retained, checkpoint=checkpoint)
    assert target.read_bytes() == b"independent rollback-race version"
    assert held.read_bytes() == replacements[target][0]
    assert cache.cache_version(retained / "before" / relative) == replacements[target][1]
    assert error.value.scoped_cache_report["preserved_unknown_versions"]


def test_preflight_cancellation_never_mutates_cache_or_creates_a_barrier(batch):
    root, replacements = batch
    retained = _retained(root)

    def stopped():
        raise InterruptedError("Stopped before cache mutation")

    with pytest.raises(InterruptedError):
        _install(root, replacements, retained, before_commit=stopped)
    _assert_originals(replacements)
    assert not retained.exists() and cache.cache_refresh_needed(root) is None


def test_real_process_exit_leaves_registered_barrier_and_fresh_batch_supersedes_exact_marker(batch):
    root, replacements = batch
    retained = _retained(root)
    script = """
import os, sys
from pathlib import Path
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.workflows.prover import scoped_cache as cache
root, retained = map(Path, sys.argv[1:])
files = sorted((root/'derived-cache/lib/lean').iterdir(), key=lambda path: (path.name != 'A.olean', path.name))
replacements = {path: (None if path.name.endswith(('.trace','.hash','.ir.sig')) else ('compiled '+path.name).encode(), cache.cache_version(path)) for path in files}
def checkpoint(name):
    if name.startswith('installed:'): os._exit(92)
with publication_lock(root):
    cache.install_cache_batch(root, replacements, retained, checkpoint=checkpoint)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), str(retained)])
    assert result.returncode == 92
    old = cache.cache_refresh_needed(root)
    assert old is not None and (retained / "batch.json").is_file()
    with pytest.raises(ValueError, match="needs refresh"):
        cache.assert_cache_ready(root)
    fresh = {
        path: (content, cache.cache_version(path)) for path, (content, _) in replacements.items()
    }
    _install(root, fresh, _retained(root))
    assert cache.cache_refresh_needed(root) is None
    markers = [
        json.loads(path.read_text())
        for path in (root / ".leanflow/scoped-import-cache/barriers").glob("*.json")
    ]
    assert any(item["previous_marker_id"] == old["marker_id"] for item in markers)
    assert any(item["marker_id"] == old["marker_id"] for item in markers)


def test_newer_refresh_marker_is_preserved_and_old_batch_cannot_clear_it(batch):
    root, replacements = batch
    newer = None

    def changed():
        nonlocal newer
        newer = cache.mark_cache_refresh_needed(root, "Source rollback requires another refresh")

    with pytest.raises(ValueError, match="marker changed"):
        _install(root, replacements, _retained(root), after_commit=changed)
    _assert_originals(replacements)
    assert cache.cache_refresh_needed(root)["marker_id"] == newer


def test_unreadable_barrier_remains_fail_closed(batch):
    root, replacements = batch
    with publication_lock(root):
        cache.mark_cache_refresh_needed(root, "Uncertain old cache")
    marker = root / ".leanflow/scoped-import-cache/needs-refresh.json"
    marker.write_bytes(b"unreadable original barrier")
    with pytest.raises(ValueError):
        cache.assert_cache_ready(root)
    with pytest.raises(ValueError):
        _install(root, replacements, _retained(root))
    assert marker.read_bytes() == b"unreadable original barrier"
    _assert_originals(replacements)


def test_source_target_remains_protected_when_no_other_barrier_exists(batch):
    root, _ = batch
    source = root / "A.lean"
    with pytest.raises(ValueError, match="cannot write source"):
        _install(root, {source: (b"unknown source", cache.cache_version(source))}, _retained(root))
    assert source.read_bytes() == b"protected source bytes"


def test_partial_refresh_cannot_clear_prior_two_module_dirty_paths(batch):
    root, replacements = batch
    first = next(iter(replacements)).relative_to(root).as_posix()

    def interrupted(name):
        if name == "installed:" + first:
            raise InterruptedError("Stop after first publish")

    with pytest.raises(InterruptedError):
        _install(root, replacements, _retained(root), checkpoint=interrupted)
    prior = cache.cache_refresh_needed(root)
    partial = {path: value for path, value in replacements.items() if path.name.startswith("A.")}
    with pytest.raises(ValueError, match="complete refresh"):
        _install(root, partial, _retained(root))
    _assert_originals(replacements)
    assert cache.cache_refresh_needed(root) == prior
    _install(root, replacements, _retained(root))
    cache.assert_cache_ready(root)


def test_generic_source_rollback_barrier_requires_explicit_full_refresh(batch):
    root, replacements = batch
    with publication_lock(root):
        cache.mark_cache_refresh_needed(root, "Canonical source rolled back")
    with pytest.raises(ValueError, match="complete refresh"):
        _install(root, replacements, _retained(root))
    _assert_originals(replacements)
    _install(root, replacements, _retained(root), full_refresh=True)
    cache.assert_cache_ready(root)


@pytest.mark.parametrize("operation", ["replace", "clear"])
def test_actual_second_process_refresh_pointer_swap_is_preserved(batch, monkeypatch, operation):
    root, replacements = batch
    pointer = root / ".leanflow/scoped-import-cache/needs-refresh.json"
    if operation == "replace":
        with publication_lock(root):
            cache.mark_cache_refresh_needed(root, "Previous observed blocker")
    held = pointer.with_name(pointer.name + ".held")
    rename = barrier.rename_exclusive
    raced = False

    def replace(source, destination):
        nonlocal raced
        if source == pointer and not raced:
            raced = True
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; p,h=map(Path,sys.argv[1:]); p.rename(h); p.write_bytes(b'independent unreadable blocker')",
                    str(pointer),
                    str(held),
                ],
                check=True,
            )
        return rename(source, destination)

    monkeypatch.setattr(barrier, "rename_exclusive", replace)
    with pytest.raises(ValueError, match="pointer preserved"):
        _install(root, replacements, _retained(root), full_refresh=True)
    assert raced and pointer.read_bytes() == b"independent unreadable blocker"
    assert held.is_file()
    _assert_originals(replacements)
    with pytest.raises(ValueError):
        cache.assert_cache_ready(root)


@pytest.mark.parametrize("lost_action", [False, True])
def test_real_exit_during_pointer_capture_blocks_admission_until_full_refresh(batch, lost_action):
    root, replacements = batch
    with publication_lock(root):
        cache.mark_cache_refresh_needed(root, "Source rollback requires full refresh")
    retained = _retained(root)
    script = """
import os, sys
from pathlib import Path
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.workflows.prover import scoped_cache as cache, scoped_cache_barrier as barrier
root, retained = map(Path, sys.argv[1:])
replacements = {path: (('compiled '+path.name).encode(), cache.cache_version(path)) for path in (root/'derived-cache/lib/lean').iterdir()}
rename = barrier.rename_exclusive
def interrupted(source, destination):
    rename(source, destination)
    if source.name == 'needs-refresh.json': os._exit(93)
barrier.rename_exclusive = interrupted
with publication_lock(root):
    cache.install_cache_batch(root, replacements, retained, full_refresh=True)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), str(retained)])
    assert result.returncode == 93
    captured = next((root / ".leanflow/scoped-import-cache/refresh-pending").glob("*.json"))
    if lost_action:
        action = root / ".leanflow/scoped-import-cache/refresh-actions" / captured.name
        (root.parent / "preserved-action.json").write_bytes(action.read_bytes())
        action.unlink()
    assert not (root / ".leanflow/scoped-import-cache/needs-refresh.json").exists()
    observed = cache.cache_refresh_needed(root)
    assert observed["kind"] == "interrupted_refresh_capture"
    if lost_action:
        assert observed["pending_assets"][captured.stem]["action"] is None
    with pytest.raises(ValueError, match="needs refresh"):
        cache.assert_cache_ready(root)
    _assert_originals(replacements)
    _install(root, replacements, _retained(root), full_refresh=True)
    cache.assert_cache_ready(root)
    assert list((root / ".leanflow/scoped-import-cache/refresh-pending").glob("*.json"))
    captured.write_bytes(b"later independent captured pointer bytes")
    with pytest.raises(ValueError, match="needs refresh"):
        cache.assert_cache_ready(root)


def test_committed_outcome_io_failure_rolls_back_only_while_barrier_remains(batch, monkeypatch):
    root, replacements = batch
    retained = _retained(root)
    durable = cache.durable_json

    def unavailable(path, value):
        if path.name == "outcome.json" and value["status"] == "COMMITTED":
            assert cache.cache_refresh_needed(root) is not None
            raise OSError(errno.EIO, "Outcome write interrupted")
        return durable(path, value)

    monkeypatch.setattr(cache, "durable_json", unavailable)
    with pytest.raises(OSError) as error:
        _install(root, replacements, retained)
    assert error.value.errno == errno.EIO
    _assert_originals(replacements)
    assert cache.cache_refresh_needed(root) is not None
    assert json.loads((retained / "outcome.json").read_text())["status"] == "ROLLED_BACK"


@pytest.mark.parametrize("failure", ["outcome", "after_clear"])
def test_real_exit_during_compensating_rollback_keeps_admission_blocker(batch, failure):
    root, replacements = batch
    retained = _retained(root)
    script = """
import errno, os, sys
from pathlib import Path
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.workflows.prover import scoped_cache as cache
root, retained = map(Path, sys.argv[1:3])
failure = sys.argv[3]
replacements = {path: (None if path.name.endswith(('.trace','.hash','.ir.sig')) else ('compiled '+path.name).encode(), cache.cache_version(path)) for path in (root/'derived-cache/lib/lean').iterdir()}
durable, clear, rename = cache.durable_json, cache.clear_cache_refresh, cache.rename_exclusive
def unavailable(path, value):
    if failure == 'outcome' and path.name == 'outcome.json' and value['status'] == 'COMMITTED':
        raise OSError(errno.EIO, 'Outcome write failed')
    return durable(path, value)
def cleared(root, marker_id):
    clear(root, marker_id)
    if failure == 'after_clear': raise OSError(errno.EIO, 'Failure after barrier was cleared')
def interrupted(source, target):
    rename(source, target)
    if '/before/' in source.as_posix(): os._exit(94)
cache.durable_json, cache.clear_cache_refresh, cache.rename_exclusive = unavailable, cleared, interrupted
with publication_lock(root):
    cache.install_cache_batch(root, replacements, retained)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), str(retained), failure])
    assert result.returncode == 94
    assert cache.cache_refresh_needed(root) is not None
    with pytest.raises(ValueError, match="needs refresh"):
        cache.assert_cache_ready(root)
    observed = [cache.cache_version(path) == before for path, (_, before) in replacements.items()]
    assert any(observed) and not all(observed)
    for path, (_, before) in replacements.items():
        assert (
            cache.cache_version(path) == before
            or cache.cache_version(retained / "before" / path.relative_to(root)) == before
        )


def test_unverifiable_rollback_fence_retains_coherent_outputs_instead_of_mutating(
    batch, monkeypatch
):
    root, replacements = batch
    retained = _retained(root)
    clear, mark = cache.clear_cache_refresh, cache.mark_cache_refresh_needed
    calls = 0

    def unavailable(root, reason, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError(errno.EIO, "Unable to establish rollback blocker")
        return mark(root, reason, **kwargs)

    def failed_after_clear(root, marker_id):
        clear(root, marker_id)
        raise OSError(errno.EIO, "Failure after barrier was cleared")

    monkeypatch.setattr(cache, "mark_cache_refresh_needed", unavailable)
    monkeypatch.setattr(cache, "clear_cache_refresh", failed_after_clear)
    with pytest.raises(OSError) as error:
        _install(root, replacements, retained)
    assert error.value.scoped_cache_report["rollback_skipped"] is True
    for path, (content, before) in replacements.items():
        assert cache.cache_version(retained / "before" / path.relative_to(root)) == before
        assert (path.read_bytes() if path.exists() else None) == content
    assert json.loads((retained / "outcome.json").read_text())["status"] == "RETAINED"
