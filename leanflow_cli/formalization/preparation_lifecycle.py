"""Dispose exclusively owned private inputs only after verified preparation process cleanup."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent.runtime.harness.processes import start_identity_matches
from leanflow_cli.formalization.evidence import durable_json, identity
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup.cleanup import process_states


@dataclass
class PrivateStage:
    """Retain private paths while any process cleanup is unverified."""

    path: Path
    directory: Path
    owner: dict[str, int | str]
    cleanup_verified: bool = True
    cleanup_observed: bool = False

    def require_cleanup(self) -> None:
        """Withhold disposal before starting an owned acquisition or check."""
        self.cleanup_verified = False
        self.cleanup_observed = False

    def verify_cleanup(self, evidence: dict[str, object]) -> None:
        """Accept only explicit controller process-cleanup evidence."""
        self.cleanup_verified = evidence.get("cleanup_verified") is True
        self.cleanup_observed = self.cleanup_verified

    def verify_acquisition_failure(self, error: BaseException) -> None:
        """Dispose failed acquisition inputs only with its explicit controller cleanup evidence."""
        evidence = getattr(error, "acquisition_resources", None)
        self.verify_cleanup(evidence if isinstance(evidence, dict) else {})

    def precommit_cleanup(self) -> dict[str, object]:
        """Bind a stopped private stage to its original live controller before commitment."""
        current = process_states().get(os.getpid())
        if (
            not self.cleanup_verified
            or not self.cleanup_observed
            or self.owner["pid"] != os.getpid()
            or current is None
            or not start_identity_matches(current[2], str(self.owner["start"]))
        ):
            raise ValueError("Preparation commitment requires its verified original controller")
        stage = self.path.lstat()
        if self.path.is_symlink() or (stage.st_dev, stage.st_ino) != (
            self.owner["stage_device"],
            self.owner["stage_inode"],
        ):
            raise ValueError("Private preparation stage changed before commitment")
        marker = {
            "schema_version": 1,
            "phase": "PRECOMMIT",
            "cleanup_verified": True,
            "owner": {"pid": self.owner["pid"], "start": self.owner["start"]},
            "stage_id": self.owner["stage_id"],
            "stage": {"path": str(self.path), "device": stage.st_dev, "inode": stage.st_ino},
            "scope": "private acquisition and independent preparation checks",
        }
        return {**marker, "marker_id": identity(marker)}

    def dispose_compiler_inputs(self) -> None:
        """Release checked private compiler copies before dependency acquisition."""
        inputs = self.path / ".leanflow-toolchain-private"
        if not inputs.exists() and not inputs.is_symlink():
            return
        if not self.cleanup_verified or inputs.is_symlink():
            raise ValueError(
                "Compiler input disposal requires verified private ownership and cleanup"
            )
        _remove_private_tree(inputs)


def _remove_private_tree(path: Path) -> None:
    """Unseal owned directory modes while leaving symlink targets untouched."""

    def unseal(directory: Path) -> None:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return
        directory.chmod(stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for child in directory.iterdir():
            unseal(child)

    unseal(path)
    shutil.rmtree(path)


@contextmanager
def private_stage(root: Path) -> Iterator[PrivateStage]:
    """Create a disjoint private stage and remove verified stopped inputs on every exit."""
    creator = process_states().get(os.getpid())
    if creator is None:
        raise ValueError("Private preparation requires an observed controller process identity")
    directory = preparation_directory(root) / "stages" / uuid.uuid4().hex
    path = directory / "project"
    if path.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(
        path.resolve()
    ):
        raise ValueError("Preparation needs a disjoint private state directory")
    directory.mkdir(parents=True, mode=0o700)
    path.mkdir(mode=0o700)
    info = directory.stat()
    stage_info = path.stat()
    owner: dict[str, int | str] = {
        "schema_version": 1,
        "stage_id": directory.name,
        "pid": os.getpid(),
        "start": creator[2],
        "device": info.st_dev,
        "inode": info.st_ino,
        "stage_device": stage_info.st_dev,
        "stage_inode": stage_info.st_ino,
        "project": str(root.resolve()),
    }
    durable_json(directory / "owner.json", owner)
    lease = PrivateStage(path, directory, owner)
    try:
        yield lease
    finally:
        if lease.cleanup_verified:
            current = directory.lstat()
            if (
                directory.is_symlink()
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or (directory / "owner.json").is_symlink()
                or json.loads((directory / "owner.json").read_text()) != owner
            ):
                raise ValueError("Private preparation ownership changed; retain its inputs")
            _remove_private_tree(directory)
