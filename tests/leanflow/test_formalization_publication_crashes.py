"""Kill subprocesses at each durable publication and rollback checkpoint and recover."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization.publication import (
    assert_no_pending_publication,
    recover_pending_publication,
)

WORKER = r"""
import json
import os
import sys
from pathlib import Path
from leanflow_cli.formalization.publication import freeze_publication_plan, publish_revision, recover_publication
root = Path(sys.argv[1])
mode = sys.argv[2]
stop = int(sys.argv[3])
events = []
def checkpoint(name):
    events.append(name)
    if len(events) == stop or (mode == "prepare" and name == "before_commit"):
        os._exit(91)
if mode == "recover":
    recover_publication(root, root / ".leanflow/transaction", checkpoint=checkpoint)
else:
    before = {"Old.lean": b"old", "New.lean": None, "Nested/Child.lean": None}
    outputs = {"Old.lean": b"new", "New.lean": b"added", "Nested/Child.lean": b"nested"}
    publish_revision(root=root, directory=root / ".leanflow/transaction", revision_id="revision", plan=freeze_publication_plan("revision", before, outputs, owned_paths=outputs), before=before, outputs=outputs, validate=lambda: None, checkpoint=checkpoint)
print(json.dumps(events))
"""


def _spawn(root: Path, mode: str, checkpoint: int = 0):
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), mode, str(checkpoint)],
        capture_output=True,
        text=True,
        timeout=15,
        env=os.environ.copy(),
    )
    assert result.returncode in {0, 91}, result.stderr
    return result


def _project(path: Path):
    path.mkdir()
    (path / "Old.lean").write_bytes(b"old")
    (path / "Old.lean").chmod(0o640)
    return path


def test_real_process_exit_at_every_publish_boundary(tmp_path: Path):
    baseline = _project(tmp_path / "baseline")
    events = json.loads(_spawn(baseline, "publish").stdout)
    assert {
        "registered",
        "written:Old.lean",
        "flushed:Old.lean",
        "captured:Old.lean",
        "installed:Old.lean",
        "journal:COMMITTED",
        "committed",
    }.issubset(events)
    for index, event in enumerate(events, start=1):
        root = _project(tmp_path / str(index))
        assert _spawn(root, "publish", index).returncode == 91
        recovered = recover_pending_publication(root)
        committed = event in {"journal:COMMITTED", "committed"}
        assert (tmp_path / str(index) / "Old.lean").read_bytes() == (
            b"new" if committed else b"old"
        ), (index, event)
        assert (root / "New.lean").exists() == committed, (index, event)
        assert (root / "Nested").exists() == committed, (index, event)
        assert (root / "Old.lean").stat().st_mode & 0o777 == 0o640
        assert recovered is None or recovered["state"] == (
            "COMMITTED" if committed else "ROLLED_BACK"
        )
        assert_no_pending_publication(root)


def test_real_process_exit_at_every_rollback_boundary(tmp_path: Path):
    baseline = _project(tmp_path / "baseline")
    assert _spawn(baseline, "prepare").returncode == 91
    events = json.loads(_spawn(baseline, "recover").stdout)
    assert "recovery_moved:Old.lean" in events
    assert "restored:Old.lean" in events
    for index, event in enumerate(events, start=1):
        root = _project(tmp_path / str(index))
        assert _spawn(root, "prepare").returncode == 91
        assert _spawn(root, "recover", index).returncode == 91
        recovered = recover_pending_publication(root)
        assert recovered["state"] == "ROLLED_BACK", (index, event)
        assert (root / "Old.lean").read_bytes() == b"old", (index, event)
        assert not (root / "New.lean").exists(), (index, event)
        assert not (root / "Nested").exists(), (index, event)
        assert_no_pending_publication(root)


def test_project_guard_serializes_another_process(tmp_path: Path):
    from leanflow_cli.formalization.publication import publication_guard

    root = _project(tmp_path / "project")
    with publication_guard(root):
        process = subprocess.Popen(
            [sys.executable, "-c", WORKER, str(root), "publish", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
        except BaseException:
            process.kill()
            process.wait()
            raise
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    assert "committed" in json.loads(stdout)
