"""Freeze bounded regular private draft files without assigning source or proof authority."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, identity, put_blob

MAX_DRAFT_FILES = 128
MAX_DRAFT_FILE_BYTES = 2 * 1024 * 1024
MAX_DRAFT_BYTES = 8 * 1024 * 1024
MAX_MODEL_REPORT_BYTES = 64 * 1024
_NOTES = (".leanflow-job/setup-notes.md", ".leanflow-job/blockers.md")


def read_regular(path: Path, limit: int) -> bytes:
    """Reject aliases, special files and concurrent substitutions before bounded reading."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError(f"Draft artifact must be a bounded private regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)

        def version(value: os.stat_result) -> tuple[int, ...]:
            """Ignore read access time while retaining every mutation-relevant inode property."""
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if version(opened) != version(before):
            raise ValueError("Draft artifact changed before reading")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if (
            len(data) > limit
            or version(os.fstat(fd)) != version(opened)
            or version(path.lstat()) != version(before)
        ):
            raise ValueError("Draft artifact changed while reading")
        return data
    finally:
        os.close(fd)


def freeze_artifacts(
    snapshot: Path, blobs: Path, layout: dict[str, Any], run_id: str, model_report: str
) -> tuple[list[dict[str, Any]], str]:
    """Copy only allocated Lean files and explicitly labelled draft reports into immutable blobs."""
    prefix = contained(snapshot, layout["owned_prefix"])
    aggregator = contained(snapshot, layout["aggregator"])
    files: list[Path] = []
    if prefix.exists():
        if not prefix.is_dir():
            raise ValueError("Draft module subtree must be a directory")
        visited = 0
        for parent, directories, names in os.walk(prefix, followlinks=False):
            visited += len(directories) + len(names)
            if visited > 4096:
                raise ValueError("Draft module subtree exceeds its bounded inventory")
            for name in directories:
                child = Path(parent) / name
                if child.is_symlink():
                    raise ValueError("Draft module subtree traverses an alias")
            files.extend(Path(parent) / name for name in names if name.endswith(".lean"))
    if aggregator.exists() or aggregator.is_symlink():
        files.append(aggregator)
    for name in _NOTES:
        path = contained(snapshot, name)
        if path.exists() or path.is_symlink():
            files.append(path)
    if len(files) + 1 > MAX_DRAFT_FILES:
        raise ValueError("Draft artifact count exceeds its bounded inventory")
    report = model_report.encode("utf-8")
    report_metadata: dict[str, Any] = {}
    if len(report) > MAX_MODEL_REPORT_BYTES:
        report_metadata = {
            "truncated": True,
            "original_size_bytes": len(report),
            "original_sha256": hashlib.sha256(report).hexdigest(),
        }
        marker = (
            f"\n\n[Model report truncated; original size {len(report)} bytes; "
            f"SHA-256 {report_metadata['original_sha256']}]\n"
        ).encode()
        report = (
            report[: MAX_MODEL_REPORT_BYTES - len(marker) - 256]
            .decode("utf-8", errors="ignore")
            .encode()
            + marker
        )
    label = b"# Unaccepted source-uncertain draft\n\nThis model report is not a translation, coverage, publication or proof approval.\n\n"
    contents = [(f"draft/{run_id}/Model-report.md", label + report)]
    total = len(contents[0][1])
    for path in sorted(files):
        relative = path.relative_to(snapshot).as_posix()
        data = read_regular(contained(snapshot, relative), MAX_DRAFT_FILE_BYTES)
        if relative in _NOTES:
            data = label + data
        total += len(data)
        if total > MAX_DRAFT_BYTES:
            raise ValueError("Draft artifacts exceed their bounded total size")
        contents.append((relative, data))
    artifacts = []
    for relative, data in contents:
        key = put_blob(blobs, data)
        artifacts.append(
            {
                "id": "draft-" + identity({"run_id": run_id, "path": relative, "sha256": key}),
                "path": relative,
                "kind": "lean" if relative.endswith(".lean") else "source",
                "sha256": key,
                "title": "Unaccepted draft: " + Path(relative).name,
            }
        )
    artifacts[0].update(report_metadata)
    return artifacts, artifacts[0]["id"]
