"""Exercise concurrent editor selection and protocol-two author submission without deadlocks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from leanflow_cli.formalization import proposal
from leanflow_cli.formalization.publication_io import PublicationConflict
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    load_document,
    save_revision,
)

_COMMON = """
import json, sys, time
from pathlib import Path
payload = json.load(sys.stdin)
root = Path(payload['root'])
def wait(name):
    deadline = time.monotonic() + 6
    while not (root / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        time.sleep(0.01)
"""

_EDITOR = _COMMON + """
from leanflow_cli.formalization.publication import publication_guard
from leanflow_cli.formalization.service import act_document
with publication_guard(root):
    (root / 'editor-project-owned').touch()
    wait('author-started')
    time.sleep(0.25)
    result = act_document(root, payload['document_id'], payload['revision_id'],
                         {'type':'select_revision', 'revision_id':payload['revision_id']})
(root / 'editor-result.json').write_text(json.dumps({'completed': True}))
"""

_AUTHOR = _COMMON + """
from contextlib import contextmanager
from leanflow_cli.formalization import preparation, proposal
from leanflow_cli.formalization.preparation_transaction import assert_no_pending_preparation
from leanflow_cli.formalization.preparation_journal import load_registry, save_registry
save_registry(root, load_registry(root))
original = proposal.document_lock
@contextmanager
def observed_document_lock(*args, **kwargs):
    with original(*args, **kwargs) as directory:
        (root / 'author-document-owned').touch()
        yield directory
proposal.document_lock = observed_document_lock
def readiness(*args, **kwargs):
    # Exercise the real project lock before stopping short of Lean setup.
    assert_no_pending_preparation(root)
    raise RuntimeError('No compiler is needed by this lock-order regression')
preparation.assert_preparation_current = readiness
wait('editor-project-owned')
(root / 'author-started').touch()
result = proposal.check_proposal(root=root, snapshot=root, baseline={},
          directory=root / '.leanflow/checks', document_id=payload['document_id'],
          base_revision_id=payload['revision_id'], selection_token=payload['selection_token'],
          allowed_axioms=())
(root / 'author-result.json').write_text(json.dumps(result))
"""


def test_editor_selection_and_author_submission_use_consistent_lock_order(
    tmp_path: Path,
) -> None:
    """Let a real concurrent selection finish and reject the stale author token."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(root, root / "paper.tex")
    record.update(
        workflow_protocol=2,
        preparation_id="preparation-fixture",
        preparation_plan_id="plan-fixture",
        environment_id="environment-fixture",
        slice_source_ids=[record["inventory"]["items"][0]["id"]],
    )
    record = save_revision(document_directory(root, record["document_id"]), record)
    (root / ".leanflow-job").mkdir()
    (root / ".leanflow-job/formalization-proposal.json").write_text(
        json.dumps({"phase": "representative_slice"})
    )
    payload = json.dumps(
        {
            "root": str(root),
            "document_id": record["document_id"],
            "revision_id": record["revision_id"],
            "selection_token": current_selection_token(root, record["document_id"]),
        }
    )
    environment = {**os.environ, "LEANFLOW_HOME": str(tmp_path / "state")}
    processes: list[subprocess.Popen[str]] = []
    started = time.monotonic()
    try:
        for script in (_EDITOR, _AUTHOR):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            processes.append(process)
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
            process.stdin = None
        for process in processes:
            stdout, stderr = process.communicate(timeout=max(0.1, 8 - (time.monotonic() - started)))
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    assert json.loads((root / "editor-result.json").read_text())["completed"] is True
    result = json.loads((root / "author-result.json").read_text())
    assert result["accepted"] is False and result["error_code"] == "selection_changed"
    assert load_document(root, record["document_id"])["revision_id"] == record["revision_id"]


def test_pending_publication_stops_author_without_creating_a_correctable_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep recovery failures terminal before selecting another author attempt."""
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    token = current_selection_token(tmp_path, record["document_id"])

    @contextmanager
    def blocked(root: Path) -> Iterator[None]:
        raise PublicationConflict("Preserved publication versions require recovery")
        yield

    monkeypatch.setattr(proposal, "publication_guard", blocked)
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=tmp_path,
        baseline={},
        directory=tmp_path / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=token,
        allowed_axioms=(),
    )
    assert result["status"] == "check_error" and result["error_code"] == "needs_reprepare"
    assert load_document(tmp_path, record["document_id"])["revision_id"] == record["revision_id"]
    assert current_selection_token(tmp_path, record["document_id"]) == token


_PREPARING = _COMMON + """
import argparse
from leanflow_cli.cli import formalization_preparation as command
from leanflow_cli.formalization.publication import recover_pending_publication
from leanflow_cli.runtime.project_operations import project_setup
def prepare(root, record, **kwargs):
    with project_setup(root):
        (root / 'prepare-started').touch()
        wait('editor-project-owned')
        (root / 'prepare-recovery-started').touch()
        recover_pending_publication(root)
        return {'status': 'prepared', 'accepted': True}
command.prepare_document_project = prepare
args = argparse.Namespace(document_id=payload['document_id'],
       expected_revision=payload['revision_id'], plan_json=None, preset='lean',
       formalization_command='prepare', timeout_s=30, offline=True)
try:
    result = command.prepare_operation(root, args)
except ValueError as error:
    result = {'error': str(error)}
(root / 'prepare-result.json').write_text(json.dumps(result))
"""

_PREPARATION_EDITOR = _COMMON + """
from leanflow_cli.formalization.publication import publication_guard
from leanflow_cli.formalization.service import act_document
wait('prepare-started')
with publication_guard(root):
    (root / 'editor-project-owned').touch()
    wait('prepare-recovery-started')
    result = act_document(root, payload['document_id'], payload['revision_id'],
                         payload['action'])
(root / 'editor-result.json').write_text(json.dumps(result))
"""


@pytest.mark.parametrize("change_revision", [False, True])
def test_cli_preparation_releases_document_before_publication_recovery(
    tmp_path: Path, change_revision: bool
) -> None:
    """Complete a real editor repair during setup and reject a changed source selection."""
    root = tmp_path / "project"
    root.mkdir()
    source = root / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    original_source = source.read_bytes()
    record = create_document(root, source)
    action = (
        {
            "type": "set_scope",
            "item_id": record["inventory"]["items"][0]["id"],
            "scope": "included",
            "reason": "Include this statement while project preparation is running",
        }
        if change_revision
        else {"type": "select_revision", "revision_id": record["revision_id"]}
    )
    payload = json.dumps(
        {
            "root": str(root),
            "document_id": record["document_id"],
            "revision_id": record["revision_id"],
            "action": action,
        }
    )
    environment = {**os.environ, "LEANFLOW_HOME": str(tmp_path / "state")}
    processes: list[subprocess.Popen[str]] = []
    started = time.monotonic()
    try:
        for script in (_PREPARING, _PREPARATION_EDITOR):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            processes.append(process)
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
            process.stdin = None
        for process in processes:
            stdout, stderr = process.communicate(timeout=max(0.1, 8 - (time.monotonic() - started)))
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    prepared = json.loads((root / "prepare-result.json").read_text())
    edited = json.loads((root / "editor-result.json").read_text())
    if change_revision:
        assert edited["revision_id"] != record["revision_id"]
        assert prepared == {
            "error": "Source selection changed during preparation; refresh and restart"
        }
    else:
        assert prepared["revision_id"] == edited["revision_id"] == record["revision_id"]
        assert prepared["preparation_result"] == {"status": "prepared", "accepted": True}
    assert load_document(root, record["document_id"])["revision_id"] == edited["revision_id"]
    assert source.read_bytes() == original_source
