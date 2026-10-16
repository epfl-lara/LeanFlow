"""Bound preparation metadata gaps and retain sanitized, non-authorizing diagnostics."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.workflows.project_setup.process import SetupFailure

_T = TypeVar("_T")
_OPERATIONS = frozenset({"parentage_refresh", "parentage_exclusion", "rss"})
MAX_CONSECUTIVE_GAPS = 5


class InspectionGap(RuntimeError):
    """Identify a failed metadata operation without exposing its command or error text."""

    def __init__(self, operation: str, error: OSError | subprocess.SubprocessError) -> None:
        if operation not in _OPERATIONS:
            raise ValueError("Unsupported preparation metadata operation")
        super().__init__("Preparation process metadata observation is unavailable")
        self.operation = operation
        self.category = (
            "timeout"
            if isinstance(error, subprocess.TimeoutExpired)
            else (
                "nonzero_exit"
                if isinstance(error, subprocess.CalledProcessError)
                else "os_error" if isinstance(error, OSError) else "subprocess_error"
            )
        )
        self.__cause__ = error


def observe_metadata(operation: str, callback: Callable[[], _T]) -> _T:
    """Classify only OS/subprocess inspection failures; preserve strict identity errors."""
    if operation not in _OPERATIONS:
        raise ValueError("Unsupported preparation metadata operation")
    try:
        return callback()
    except (OSError, subprocess.SubprocessError) as error:
        raise InspectionGap(operation, error) from error


class PreparationInspection:
    """Require positive samples and stop at the fifth consecutive metadata-only gap."""

    def __init__(self, diagnostic_path: Path | None) -> None:
        self.path = diagnostic_path
        self.total_samples = 0
        self.successful_samples = 0
        self.total_gaps = 0
        self.consecutive_gaps = 0
        self.first_failure: dict[str, str] | None = None
        self.last_failure: dict[str, str] | None = None
        self.state = "starting"
        self.cleanup_verified = False
        self._version: tuple[int, ...] | None = None
        self._diagnostic_error: BaseException | None = None
        self._persist()

    def _persist(self) -> None:
        """Fail closed after the first diagnostic IO error without retrying its write."""
        if self._diagnostic_error is not None:
            raise self._diagnostic_error
        try:
            self._write_diagnostic()
        except BaseException as error:
            self._diagnostic_error = error
            raise

    def _write_diagnostic(self) -> None:
        """Atomically flush a bounded private diagnostic, refusing aliases and external changes."""
        if self.path is None:
            return
        if not self.path.is_absolute():
            raise ValueError("Preparation inspection diagnostics need an absolute private path")
        parents = (self.path.parent, *self.path.parent.parents)
        versions = {}
        for parent in parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Preparation inspection diagnostics cannot traverse aliases")
            versions[parent] = info.st_dev, info.st_ino
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            if self._version is not None:
                raise ValueError("Preparation inspection diagnostic disappeared") from None
        else:
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or self._version is None
                or self._entry_version(info) != self._version
            ):
                raise ValueError("Preparation inspection diagnostic ownership changed")
        durable_json(self.path, self.evidence())
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or any(
                not stat.S_ISDIR((current := parent.lstat()).st_mode)
                or (current.st_dev, current.st_ino) != expected
                for parent, expected in versions.items()
            )
        ):
            raise ValueError("Preparation inspection diagnostic publication changed")
        self._version = self._entry_version(info)

    @staticmethod
    def _entry_version(info: os.stat_result) -> tuple[int, ...]:
        """Retain one owned diagnostic version between atomic replacements."""
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def evidence(self) -> dict[str, Any]:
        """Return fixed-schema counters and allowlisted categories, never exception payloads."""
        return {
            "schema_version": 1,
            "state": self.state,
            "total_samples": self.total_samples,
            "successful_samples": self.successful_samples,
            "total_gaps": self.total_gaps,
            "consecutive_gaps": self.consecutive_gaps,
            "max_consecutive_gaps": MAX_CONSECUTIVE_GAPS,
            "first_failure": self.first_failure,
            "last_failure": self.last_failure,
            "cleanup_verified": self.cleanup_verified,
        }

    def sample(self, observe: Callable[[], None]) -> bool:
        """Admit a complete positive observation or retain a bounded unresolved metadata gap."""
        if self.state in {"closed", "failed"}:
            raise SetupFailure(
                "setup_resource_inspection_failed",
                "Preparation process identities could not be supervised.",
                "Retain diagnostics and verify process inspection before retrying preparation.",
            )
        self.total_samples += 1
        try:
            observe()
        except InspectionGap as error:
            self.total_gaps += 1
            self.consecutive_gaps += 1
            row = {"operation": error.operation, "category": error.category}
            self.first_failure = self.first_failure or row
            self.last_failure = row
            exhausted = self.consecutive_gaps >= MAX_CONSECUTIVE_GAPS
            self.state = "failed" if exhausted else "gap"
            self._persist()
            if exhausted:
                raise SetupFailure(
                    "setup_resource_inspection_failed",
                    "Preparation process identities could not be supervised after bounded metadata gaps.",
                    "Retain diagnostics and verify process inspection before retrying preparation.",
                ) from error
            return False
        except BaseException:
            self.state = "failed"
            self._persist()
            raise
        previous = self.state
        self.successful_samples += 1
        self.consecutive_gaps = 0
        self.state = "healthy"
        if previous != "healthy":
            self._persist()
        return True

    def abort(self, *, cleanup_verified: bool) -> None:
        """Persist failed unwinding without converting it into positive supervision evidence."""
        self.state = "failed"
        self.cleanup_verified = cleanup_verified is True
        self._persist()

    def finish(self, *, cleanup_verified: bool) -> None:
        """Require a positive final sample and independently verified cleanup before success."""
        if cleanup_verified is not True:
            self.abort(cleanup_verified=False)
            raise SetupFailure(
                "setup_cleanup_unverified",
                "Preparation descendant cleanup could not be verified.",
                "Retain the private stage and inspect its owned processes before retrying.",
            )
        if self.state != "healthy" or self.consecutive_gaps or not self.successful_samples:
            self.abort(cleanup_verified=True)
            raise SetupFailure(
                "setup_resource_inspection_failed",
                "Preparation requires a complete healthy process observation before completion.",
                "Retain diagnostics and verify process inspection before retrying preparation.",
            )
        self.state = "closed"
        self.cleanup_verified = True
        self._persist()
