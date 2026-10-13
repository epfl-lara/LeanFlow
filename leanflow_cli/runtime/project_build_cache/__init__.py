"""Restore opt-in controller-built local modules into independent fresh project copies."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.file_copy import copy_file
from core.home import leanflow_home
from core.utils import atomic_json_write
from leanflow_cli.runtime.dependency_cache.checkout import _remove_stage, _seal
from leanflow_cli.runtime.project_build_cache.freshness import InputGuard
from leanflow_cli.runtime.project_build_cache.identity import (
    build_identity,
    file_digest,
    identity_key,
    tree_manifest,
)
from leanflow_cli.runtime.storage import (
    StorageSafetyError,
    ensure_storage_capacity,
    lease_entry,
    register_entry,
    storage_lock,
    touch_entry,
)
from leanflow_cli.runtime.storage.entries import read_entry


def _cache_root() -> Path:
    """Resolve managed project cache ownership without traversing symbolic links."""
    home = leanflow_home().expanduser().resolve()
    root = home / "cache/project-builds/v1"
    for path in (root, root.parent, root.parent.parent):
        if path.is_symlink():
            raise StorageSafetyError("Project build cache directories cannot be symbolic links")
    return root


@contextlib.contextmanager
def _publication_lock(entry: Path) -> Iterator[None]:
    """Serialize only one cache key's publication without holding the storage lock."""
    directory = leanflow_home().expanduser().resolve() / "storage/project-build-locks"
    if directory.is_symlink() or directory.parent.is_symlink():
        raise StorageSafetyError("Project build lock directories cannot be symbolic links")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        directory / f"{entry.name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _copy_build(source: Path, destination: Path) -> None:
    """Clone all regular Lake outputs and traces into distinct writable private inodes."""
    tree_manifest(source)
    shutil.copytree(source, destination, copy_function=copy_file)
    for directory, _, files in os.walk(destination, followlinks=False):
        parent = Path(directory)
        parent.chmod(parent.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
        for name in files:
            file = parent / name
            file.chmod(file.stat().st_mode | stat.S_IWUSR)


class ProjectBuildCache:
    """Hold one fresh controller setup's immutable identity and optional reuse evidence."""

    def __init__(
        self, project: Path, modules: Sequence[str], environment: Mapping[str, str]
    ) -> None:
        self.project = project
        self.modules = list(modules)
        self.environment = dict(environment)
        self.input_guard = InputGuard()
        self.identity = build_identity(project, modules, environment, guard=self.input_guard)
        self.entry = _cache_root() / identity_key(self.identity)
        self.metadata: dict[str, Any] = {
            "eligible": True,
            "hit": False,
            "published": False,
            "key": self.entry.name,
        }

    def _valid_entry(self) -> bool:
        """Validate sealed ownership and every output against its cryptographic manifest."""
        try:
            if self.entry.is_symlink() or not self.entry.is_dir():
                return False
            record = read_entry(self.entry)
            if (
                record is None
                or record.get("kind") != "project-build"
                or record.get("identity_sha256") != self.entry.name
                or record.get("ready") is not True
                or record.get("manifest_sha256") != file_digest(self.entry / "manifest.json")
                or record.get("identity_file_sha256") != file_digest(self.entry / "identity.json")
            ):
                return False
            if (
                identity_key(json.loads((self.entry / "identity.json").read_bytes()))
                != self.entry.name
            ):
                return False
            manifest = json.loads((self.entry / "manifest.json").read_bytes())
            return manifest == tree_manifest(self.entry / "build")
        except (OSError, ValueError, StorageSafetyError):
            return False

    def restore(self, build: Path) -> bool:
        """Restore complete validated outputs before native admission; leave Lake to check traces."""
        if build.is_symlink() or (build.exists() and any(build.iterdir())):
            self.metadata["reason"] = (
                "Fresh cache restoration requires an empty private build directory"
            )
            return False
        with lease_entry(self.entry):
            if not self._valid_entry():
                return False
            ensure_storage_capacity(build.parent)
            with tempfile.TemporaryDirectory(
                prefix=".restore-project-build-", dir=build.parent
            ) as temporary:
                copied = Path(temporary) / "build"
                _copy_build(self.entry / "build", copied)
                if tree_manifest(copied) != json.loads((self.entry / "manifest.json").read_bytes()):
                    raise ValueError("Project cache bytes changed while restoring")
                if build.exists():
                    build.rmdir()
                copied.rename(build)
            touch_entry(self.entry)
            self.metadata["hit"] = True
            return True

    def publish(self, build: Path) -> bool:
        """Publish only outputs of the completed controller build with unchanged exact inputs."""
        if self.metadata["hit"]:
            return False
        if not self.input_guard.unchanged():
            self.metadata["reason"] = "Project inputs changed during the private prerequisite build"
            return False
        manifest = tree_manifest(build)
        if not manifest["files"]:
            return False
        ensure_storage_capacity(build)
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        with _publication_lock(self.entry), lease_entry(self.entry):
            if self._valid_entry():
                touch_entry(self.entry)
                return False
            stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.entry.parent))
            try:
                register_entry(stage, kind="staging")
                with lease_entry(stage):
                    _copy_build(build, stage / "build")
                    if tree_manifest(stage / "build") != manifest:
                        raise ValueError("Project build outputs changed during cache publication")
                    atomic_json_write(stage / "identity.json", self.identity)
                    identity_file_hash = file_digest(stage / "identity.json")
                    atomic_json_write(stage / "manifest.json", manifest)
                    manifest_hash = file_digest(stage / "manifest.json")
                    _seal(stage / "build")
                    (stage / "manifest.json").chmod(0o400)
                    (stage / "identity.json").chmod(0o400)
                    # Rename and ownership update form one reclamation transaction.
                    with storage_lock():
                        if self.entry.exists():
                            if read_entry(self.entry) is None:
                                raise StorageSafetyError(
                                    "Unowned project build cache entry cannot be replaced"
                                )
                            _remove_stage(self.entry)
                        stage.rename(self.entry)
                        try:
                            register_entry(
                                self.entry,
                                kind="project-build",
                                metadata={
                                    "ready": True,
                                    "manifest_sha256": manifest_hash,
                                    "identity_sha256": self.entry.name,
                                    "identity_file_sha256": identity_file_hash,
                                },
                            )
                        except BaseException:
                            _remove_stage(self.entry)
                            raise
                    self.metadata["published"] = True
                    return True
            finally:
                if stage.exists():
                    _remove_stage(stage)
