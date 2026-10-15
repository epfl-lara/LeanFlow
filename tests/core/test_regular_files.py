"""Exercise bounded reads against actual special-file substitutions and source versions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.regular_files import read_regular_bytes


def test_regular_reads_accept_empty_files_and_shared_source_inodes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"")
    assert read_regular_bytes(source, 0) == b""
    source.write_bytes(b"source")
    os.link(source, tmp_path / "alias")
    assert read_regular_bytes(source, 6) == b"source"
    with pytest.raises(ValueError, match="bounded regular"):
        read_regular_bytes(source, 5)
    for limit in (-1, True):
        with pytest.raises(ValueError, match="byte bound"):
            read_regular_bytes(source, limit)


@pytest.mark.parametrize("replacement", ["fifo", "symlink", "growth"])
def test_substitution_between_lstat_and_open_never_blocks(tmp_path: Path, replacement: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    script = r"""
import os, sys
from pathlib import Path
from core import regular_files
source = Path(sys.argv[1]); replacement = sys.argv[2]
original_open = os.open
def racing_open(path, flags, *args, **kwargs):
    source.unlink()
    if replacement == 'fifo': os.mkfifo(source)
    elif replacement == 'symlink': source.symlink_to('/dev/null')
    else: source.write_bytes(b'x' * 4096)
    return original_open(path, flags, *args, **kwargs)
regular_files.os.open = racing_open
try: regular_files.read_regular_bytes(source, 6)
except (ValueError, OSError): pass
else: raise AssertionError('Substituted source was admitted')
print('rejected without waiting')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), replacement],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected without waiting"
