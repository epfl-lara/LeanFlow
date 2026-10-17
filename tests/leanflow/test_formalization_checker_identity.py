"""Invalidate formalization authority when shared checker or reviewer code changes."""

import os
from pathlib import Path

from leanflow_cli.formalization.environment import _checker_sources


def test_checker_identity_includes_shared_transport_and_review_code(tmp_path: Path):
    paths = [
        "leanflow_cli/workflows/prover/check_sandbox.py",
        "leanflow_cli/workflows/prover/check_process.py",
        "leanflow_cli/formalization/review_dossier.py",
        "core/file_copy.py",
        "agent/runtime/harness/processes.py",
        "tools/implementations/document_tool.py",
        "utils/compatibility.py",
    ]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    before = _checker_sources(tmp_path)
    assert set(before) == set(paths)
    path = tmp_path / paths[0]
    original = path.stat()
    path.write_text("value = 2\n")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = _checker_sources(tmp_path)
    assert before[paths[0]] != after[paths[0]]
    assert {key: value for key, value in before.items() if key != paths[0]} == {
        key: value for key, value in after.items() if key != paths[0]
    }
