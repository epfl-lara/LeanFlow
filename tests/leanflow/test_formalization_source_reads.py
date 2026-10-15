"""Reject substituted intake sources without holding document ownership on a blocking read."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization.records import assert_source_current, create_document


@pytest.mark.parametrize("phase", ["initial", "freeze", "currentness"])
def test_source_fifo_races_reject_and_release_document_ownership(
    tmp_path: Path, phase: str
) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    script = r"""
import fcntl, os, sys
from pathlib import Path
from leanflow_cli.formalization import records, source_inventory
from core import regular_files
root = Path(sys.argv[1]); source = root/'paper.tex'; phase = sys.argv[2]
if phase == 'initial':
    original_open = os.open
    def racing_open(path, flags, *args, **kwargs):
        if Path(path) == source:
            source.unlink(); os.mkfifo(source)
        return original_open(path, flags, *args, **kwargs)
    regular_files.os.open = racing_open
elif phase == 'freeze':
    original_inventory = source_inventory.inventory_source
    def raced_inventory(*args, **kwargs):
        inventory = original_inventory(*args, **kwargs)
        source.unlink(); os.mkfifo(source)
        return inventory
    source_inventory.inventory_source = raced_inventory
else:
    record = records.create_document(root, source)
    source.unlink(); os.mkfifo(source)
try:
    if phase == 'currentness': records.assert_source_current(root, record)
    else: records.create_document(root, source)
except (ValueError, OSError): pass
else: raise AssertionError('FIFO source was admitted')
for lock in root.glob('.leanflow/formalizations/*/document.lock'):
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('rejected; document ownership released')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), phase],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected; document ownership released"


def test_source_currentness_uses_frozen_size_and_keeps_changed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    changed = b"changed" * 1024
    source.write_bytes(changed)
    with pytest.raises(ValueError, match="Original source changed"):
        assert_source_current(tmp_path, record)
    assert source.read_bytes() == changed


def test_source_hardlinks_do_not_require_private_artifact_ownership(tmp_path: Path) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    os.link(source, tmp_path / "original-copy.tex")
    record = create_document(tmp_path, source)
    assert_source_current(tmp_path, record)


def test_missing_frozen_size_requires_intake_instead_of_unbounded_read(tmp_path: Path) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    record["inventory"]["files"].pop("paper.tex")
    with pytest.raises(ValueError, match="repeat source intake"):
        assert_source_current(tmp_path, record)
