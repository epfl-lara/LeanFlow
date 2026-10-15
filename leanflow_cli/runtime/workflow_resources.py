"""Bound aggregate controller and workflow-owned process RSS without replacing actor closure gates."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Mapping
from typing import Any

from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree, _processes, start_identity_matches
from agent.runtime.harness.resources import native_memory_limit_bytes, observed_descendant_rss_bytes
from leanflow_cli.workflows.project_setup.cleanup import verify_owned_stopped


class WorkflowResourceFailure(RuntimeError):
    """Stop admission and acceptance after an aggregate limit or bounded inspection failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WorkflowResourceWindow:
    """Observe one controller plus new and explicitly owned children, excluding unrelated branches."""

    def __init__(
        self,
        environment: Mapping[str, str],
        cancelled: threading.Event,
        *,
        already_owned: Mapping[int, str] | None = None,
        interval_s: float = 0.25,
        max_failures: int = 5,
    ) -> None:
        """Resolve the shared budget before launching actors and bind one cooperative stop event."""
        if not 0.05 <= interval_s <= 1 or type(max_failures) is not int or max_failures <= 0:
            raise ValueError("Workflow resource sampling requires finite positive polling bounds")
        self.max_bytes = native_memory_limit_bytes(environment)
        self.cancelled = cancelled
        self.interval_s = interval_s
        self.max_failures = max_failures
        self.root = os.getpid()
        self.root_start = ""
        self.peak_bytes = 0
        self.sample_failures = 0
        self.samples = 0
        self.failure: WorkflowResourceFailure | None = None
        self.cleanup_verified = False
        self._seeds = dict(already_owned or {})
        self._tracked: dict[int, str] = {}
        self._excluded: dict[int, str] = {}
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False
        self._closed = False

    @staticmethod
    def _expand(table: dict[int, tuple[int, str]], identities: dict[int, str]) -> None:
        """Expand only descendants of still-matching cached identities from one exact table."""
        parents = {
            pid
            for pid, start in identities.items()
            if pid in table and start_identity_matches(table[pid][1], start)
        }
        while True:
            added = {
                pid
                for pid, (parent, _) in table.items()
                if parent in parents and pid not in parents
            }
            if not added:
                return
            identities.update({pid: table[pid][1] for pid in added})
            parents.update(added)

    def start(self) -> None:
        """Capture trusted self identity and preexisting exclusions before any owned actor spawns."""
        if self._started:
            raise ValueError("Workflow resource window has already started")
        try:
            table = _processes()
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self._fail_inspection(error)
            assert self.failure is not None
            raise self.failure from error
        if self.root not in table:
            raise WorkflowResourceFailure(
                "Workflow controller identity could not be observed",
                code="workflow_resource_inspection_failed",
            )
        self.root_start = table[self.root][1]
        current = {self.root: self.root_start}
        self._expand(table, current)
        owned = {
            pid: start
            for pid, start in self._seeds.items()
            if pid in table and start_identity_matches(table[pid][1], start)
        }
        self._expand(table, owned)
        self._tracked.update({pid: start for pid, start in owned.items() if pid != self.root})
        self._excluded.update(
            {
                pid: start
                for pid, start in current.items()
                if pid != self.root and owned.get(pid) != start
            }
        )
        self._started = True
        self.check(force=True)
        self._worker = threading.Thread(
            target=self._run, name="leanflow-workflow-resources", daemon=True
        )
        self._worker.start()

    def _sample(self) -> bool:
        with self._lock:
            if self.failure is not None:
                return False
            try:
                table = _processes()
                if self.root not in table or not start_identity_matches(
                    table[self.root][1], self.root_start
                ):
                    raise ValueError("Workflow controller identity changed")
                self._expand(table, self._excluded)
                # Preserve exclusion descendants before dropping dead parents.
                # Sampling helpers are short-lived children too; retaining their
                # absent identities forever would grow the monitor itself.
                self._excluded = {
                    pid: start
                    for pid, start in self._excluded.items()
                    if pid in table and start_identity_matches(table[pid][1], start)
                }
                self._tracked = {
                    pid: start
                    for pid, start in self._tracked.items()
                    if pid in table and start_identity_matches(table[pid][1], start)
                }
                owned = {self.root: self.root_start, **self._tracked}
                self._expand(table, owned)
                self._tracked.update(
                    {
                        pid: start
                        for pid, start in owned.items()
                        if pid != self.root and self._excluded.get(pid) != start
                    }
                )
                current = observed_descendant_rss_bytes(
                    {self.root: self.root_start, **self._tracked}
                )
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self.sample_failures += 1
                if self.sample_failures >= self.max_failures:
                    self.failure = WorkflowResourceFailure(
                        "Aggregate workflow memory or process identity inspection failed after bounded retries; retain inputs and inspect the host.",
                        code="workflow_resource_inspection_failed",
                    )
                    self.failure.__cause__ = error
                    self.cancelled.set()
                return False
            self.samples += 1
            self.sample_failures = 0
            self.peak_bytes = max(self.peak_bytes, current)
            if current > self.max_bytes:
                self.failure = WorkflowResourceFailure(
                    "The controller and its owned workflow processes exceeded the shared memory budget; reduce parallel work or select up to 20 GiB.",
                    code="workflow_memory_limit",
                )
                self.cancelled.set()
                return False
            return True

    def _run(self) -> None:
        while not self._stopped.wait(self.interval_s):
            try:
                self._sample()
            except Exception as error:
                # A dead monitoring thread must not silently disable admission.
                with self._lock:
                    self._fail_inspection(error)
            if self.failure is not None:
                # Cooperative cancellation covers every actor; this exact cached
                # fallback also stops children when the controller is busy hashing.
                try:
                    self._stop_children()
                except NativeCleanupUnverified:
                    self.cleanup_verified = False
                return

    def _fail_inspection(self, error: Exception) -> None:
        """Make unexpected monitor failures visible through the shared cancellation gate."""
        self.failure = WorkflowResourceFailure(
            "Aggregate workflow memory or process identity inspection failed; retain inputs and inspect the host.",
            code="workflow_resource_inspection_failed",
        )
        self.failure.__cause__ = error
        self.cancelled.set()

    def check(self, *, force: bool = False) -> None:
        """Reject failed resource ownership before launch, acceptance, or publication."""
        if not self._started or self._closed:
            raise ValueError("Workflow resource window is not active")
        if force:
            for _ in range(self.max_failures):
                if self._sample() or self.failure is not None:
                    break
        if self.failure is not None:
            raise self.failure

    def evidence(self) -> dict[str, Any]:
        """Report observed aggregate RSS without claiming unsampled peaks or physical pipe closure."""
        with self._lock:
            return {
                "scope": "controller_and_owned_workflow_processes",
                "max_owned_rss_bytes": self.max_bytes,
                "peak_owned_rss_bytes": self.peak_bytes,
                "sampling_interval_s": self.interval_s,
                "samples": self.samples,
                "consecutive_sample_failures": self.sample_failures,
                "error_code": self.failure.code if self.failure else None,
                "cleanup_verified": self.cleanup_verified,
                "physical_output_eof": "independently_required_per_actor",
            }

    def _stop_children(self) -> None:
        """Stop exact cached children while never signaling the controller or excluded branches."""
        with self._lock:
            tree = ProcessTree(self.root, dict(self._tracked))
        try:
            tree.signal(signal.SIGTERM)
            self._stopped.wait(0.1)
            tree.signal(signal.SIGKILL)
            if not verify_owned_stopped(tree):
                raise ValueError("Owned workflow descendants remain live")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise NativeCleanupUnverified(
                "Workflow descendant closure remains unverified; retain all workflow inputs"
            ) from error
        self.cleanup_verified = True

    def close(self) -> None:
        """Bound sampler teardown and verify child absence without inventing actor EOF evidence."""
        if self._closed:
            return
        self._stopped.set()
        if self._worker is not None:
            self._worker.join(timeout=7)
            if self._worker.is_alive():
                raise NativeCleanupUnverified(
                    "Workflow resource sampler did not stop; retain all workflow inputs"
                )
        if self._started:
            self._stop_children()
        self._closed = True
