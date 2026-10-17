"""Create deterministic ordinary local Git packages for independent readiness checks."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

SOURCE = b"def value := 1\n"


def _object(kind: str, content: bytes) -> str:
    """Compute the immutable Git object identity independently of local configuration."""
    return hashlib.sha1(
        kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
    ).hexdigest()


_BLOB = _object("blob", SOURCE)
_TREE = _object("tree", b"100644 Physlib.lean\0" + bytes.fromhex(_BLOB))
PIN = _object(
    "commit",
    (
        f"tree {_TREE}\n"
        "author Fixture <fixture@example.invalid> 946684800 +0000\n"
        "committer Fixture <fixture@example.invalid> 946684800 +0000\n"
        "\nFixture package\n"
    ).encode(),
)
REQUEST = {"name": "physlib", "git": "https://github.com/example/physlib", "rev": PIN}


def create_checkout(path: Path) -> None:
    """Commit real package bytes and require the deterministic full revision."""
    if (path / ".git").exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
    )
    commands = [
        ["git", "init", "-q", str(path)],
        ["git", "-C", str(path), "add", "Physlib.lean"],
        [
            "git",
            "-C",
            str(path),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "commit",
            "-qm",
            "Fixture package",
        ],
    ]
    (path / "Physlib.lean").write_bytes(SOURCE)
    for command in commands:
        subprocess.run(command, env=environment, check=True, capture_output=True)
    revision = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert revision == PIN
