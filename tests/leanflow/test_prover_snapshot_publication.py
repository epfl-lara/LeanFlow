"""Keep model snapshots useful without importing canonical publication approval authority."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization.publication import (
    assert_no_pending_publication,
    freeze_publication_plan,
    publish_revision,
)
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.formalization.publication_journal import load_registry
from leanflow_cli.formalization.publication_registry_inventory import PublicationAuthorityLoss
from leanflow_cli.workflows.prover import native_snapshot, scoped_imports
from leanflow_cli.workflows.prover.native_snapshot import (
    SnapshotProvenanceError,
    create_snapshot,
    prepare_snapshot,
)
from leanflow_cli.workflows.prover.session_build import build_session_imports
from tests.leanflow.test_prover_scoped_imports import fake_build


def _published(root: Path) -> dict:
    """Create a real publication transaction with compiler-independent source fixtures."""
    (root / "Main").mkdir(parents=True)
    (root / "Main/Base.lean").write_text("def value : Nat := 1\n")
    (root / "Main/Goal.lean").write_text("import Main.Base\ntheorem goal : value = 1 := by sorry\n")
    (root / "lakefile.toml").write_text('name = "snapshot"\n')
    outputs = {
        "formalization/paper/current.json": b'{"revision_id":"reviewed"}',
        "formalization/paper/handoff.json": b'{"revision_id":"reviewed"}',
        "formalization/paper/receipts/reviewed.json": b'{"accepted":true}',
        "formalization/paper/Blueprint.md": b"Reviewed definitions and theorem slots.\n",
    }
    return publish_revision(
        root=root,
        directory=root / ".leanflow/publication",
        revision_id="reviewed",
        plan=freeze_publication_plan(
            "reviewed", dict.fromkeys(outputs), outputs, owned_paths=outputs
        ),
        before=dict.fromkeys(outputs),
        outputs=outputs,
        validate=lambda: None,
    )


def test_canonical_guarded_copy_declines_only_publication_authority(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    published = _published(canonical)
    pointer = canonical / "formalization/paper/current.json"
    original = pointer.read_bytes()
    for name in (
        "revisions/r.json",
        "proof-revisions/p.json",
        "receipt-history/r/r.json",
        "review-cache/r.json",
        "blobs/abc",
    ):
        path = canonical / "formalization/paper" / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b"controller evidence")
    source = canonical / "formalization/paper/revisions/User.lean"
    source.write_text("def userSource : Nat := 1\n")
    library = canonical / "formalization/Library"
    library.mkdir()
    (library / "Library.lean").write_text("def libraryValue : Nat := 2\n")
    (library / "data.json").write_text('{"ordinary_user_data":true}')
    snapshot = create_snapshot(
        canonical, tmp_path / "job/project", excluded_build_dir=".lake/build"
    )
    assert pointer.read_bytes() == original
    assert (
        load_registry(canonical)["selected_revision"]["transaction_id"]
        == published["transaction_id"]
    )
    assert not (snapshot / "formalization/paper/current.json").exists()
    assert not (snapshot / "formalization/paper/handoff.json").exists()
    assert not list((snapshot / "formalization/paper").rglob("*.json"))
    assert not (snapshot / "formalization/paper/blobs/abc").exists()
    assert (snapshot / "formalization/paper/Blueprint.md").read_text().startswith("Reviewed")
    assert (snapshot / source.relative_to(canonical)).read_bytes() == source.read_bytes()
    assert (snapshot / "formalization/Library/data.json").read_bytes() == (
        library / "data.json"
    ).read_bytes()
    assert_no_pending_publication(snapshot)
    assert load_registry(snapshot)["selected_revision"] is None

    def layout(root, **kwargs):
        from leanflow_cli.formalization.evidence import digest

        return {
            "accepted": True,
            "lean_lib_dir": ".lake/build/lib/lean",
            "configuration_sha256": digest((root / "lakefile.toml").read_bytes()),
        }

    def admitted(root, *args, **kwargs):
        return {
            "local": {"Main.Base": root / "Main/Base.lean", "Main.Goal": root / "Main/Goal.lean"},
            "library": root / ".lake/build/lib/lean",
        }

    monkeypatch.setattr(scoped_imports, "inspect_layout", layout)
    monkeypatch.setattr(scoped_imports, "validate_build_inputs", admitted)
    monkeypatch.setattr(scoped_imports, "build_project", fake_build)
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir()
    target = scratch / "Scratch.lean"
    target.write_text("import Main.Base\ntheorem privateGoal : value = 1 := by rfl\n")
    built = build_session_imports(
        project_root=snapshot,
        workspace=scratch,
        source_file=target,
        deadline=None,
        cancelled=None,
        static_bound=True,
    )
    assert built["success"] and built["proof_acceptance"] is False
    assert (
        load_registry(canonical)["selected_revision"]["transaction_id"]
        == published["transaction_id"]
    )


def test_ordinary_portable_copy_and_untrusted_source_cannot_use_snapshot_filter_as_a_bypass(
    tmp_path,
):
    canonical = tmp_path / "canonical"
    _published(canonical)
    copied = tmp_path / "copied"
    shutil.copytree(canonical, copied, ignore=shutil.ignore_patterns(".leanflow"))
    with pytest.raises(PublicationAuthorityLoss):
        assert_no_pending_publication(copied)
    with pytest.raises(PublicationAuthorityLoss):
        create_snapshot(copied, tmp_path / "attempt/project", excluded_build_dir=".lake/build")
    assert not (tmp_path / "attempt/project").exists()


def test_exact_case_ownership_does_not_filter_a_Formalization_user_library(tmp_path):
    canonical = tmp_path / "canonical"
    library = canonical / "Formalization/Library"
    library.mkdir(parents=True)
    (library / "current.json").write_text('{"user_library":true}')
    (library / "User.lean").write_text("def userValue : Nat := 1\n")
    snapshot = create_snapshot(
        canonical, tmp_path / "job/project", excluded_build_dir=".lake/build"
    )
    assert (snapshot / "Formalization/Library/current.json").read_bytes() == (
        library / "current.json"
    ).read_bytes()
    assert (snapshot / "Formalization/Library/User.lean").read_bytes() == (
        library / "User.lean"
    ).read_bytes()


@pytest.mark.parametrize("record_change", ["legacy_schema", "missing_policy", "fifo"])
def test_old_or_changed_snapshot_provenance_requires_fresh_job_without_deleting_progress(
    tmp_path, record_change
):
    canonical = tmp_path / "canonical"
    _published(canonical)
    snapshot = create_snapshot(
        canonical, tmp_path / "job/project", excluded_build_dir=".lake/build"
    )
    progress = snapshot / "Main/Goal.lean"
    progress.write_text("private proof progress\n")
    original = progress.read_bytes()
    record = native_snapshot._exclusion_record(snapshot)
    if record_change == "fifo":
        record.unlink()
        os.mkfifo(record)
    else:
        value = json.loads(record.read_text())
        if record_change == "legacy_schema":
            value["schema_version"] = 1
        else:
            value.pop("excluded_publication_authority")
        record.write_text(json.dumps(value))
    with pytest.raises(SnapshotProvenanceError):
        prepare_snapshot(canonical, snapshot, excluded_build_dir=".lake/build")
    assert progress.read_bytes() == original


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_actual_queued_snapshot_session_stops_before_storage_lock_or_model_call(tmp_path, mode):
    canonical = tmp_path / "canonical"
    _published(canonical)
    script = r"""
import json, sys, time
from contextlib import contextmanager
from pathlib import Path
from leanflow_cli.workflows.prover import native_snapshot
from leanflow_cli.workflows.prover.agent_session import run_session
root, work, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
@contextmanager
def forbidden_storage():
    (work / 'storage-entered').write_text('unexpected')
    raise AssertionError('storage acquired before queued canonical ownership')
    yield
native_snapshot.storage_lock = forbidden_storage
work.mkdir(parents=True)
def cancelled():
    return (work / 'cancelled').exists()
(work / 'ready').write_text('ready')
result = run_session(role='prover', prompt='never launched', project_root=root,
    workspace=work / 'job', config={'provider':'codex','_cancelled':cancelled,
        'wall_time_s':0.2 if mode=='deadline' else None}, api_budget=1,
    log_path=work/'events.jsonl', context={'formalization_proof_scope':'checked',
        'formalization_build_dir':'.lake/build'})
print(json.dumps(result))
"""
    work = tmp_path / "queued"
    with publication_lock(canonical):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(canonical), str(work), mode],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            import time

            until = time.monotonic() + 5
            while not (work / "ready").exists() and process.poll() is None:
                assert time.monotonic() < until
                time.sleep(0.01)
            if mode == "cancel":
                (work / "cancelled").write_text("stop")
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["status"] == ("interrupted" if mode == "cancel" else "timeout")
    assert result["api_calls"] == result["agent_calls"] == 0
    assert not (work / "storage-entered").exists()
    assert not (work / "job/project").exists()
