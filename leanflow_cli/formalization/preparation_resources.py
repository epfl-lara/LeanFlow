"""Supervise new preparation descendants across acquisition, independent checking, and fingerprinting."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from agent.runtime.harness.processes import ProcessTree, _processes
from agent.runtime.harness.resources import observed_descendant_rss_bytes
from leanflow_cli.workflows.project_setup.cleanup import verify_owned_stopped
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import SetupFailure

_campaign_owner = threading.Lock()


@contextmanager
def supervise_preparation(
    policy: SetupExecutionPolicy,
    *,
    cancel: threading.Event | None = None,
    interval_s: float = 0.25,
) -> Iterator[threading.Event]:
    """Guard a dedicated serial controller campaign and stop only its new owned descendants.

    Existing descendants and their later children are excluded. Callers must pass
    the yielded cancellation event to every independent check and poll it before
    infrastructure commitment. Limits are sampled; they are not kernel RSS quotas.
    """
    if not 0.05 <= interval_s <= 1:
        raise ValueError("Preparation supervision requires a bounded sampling interval")
    if not _campaign_owner.acquire(blocking=False):
        raise SetupFailure(
            "setup_resource_concurrency",
            "Another preparation owns this controller process.",
            "Run preparation serially in a dedicated controller.",
        )
    event = cancel if cancel is not None else threading.Event()
    stopped = threading.Event()
    failures: list[BaseException] = []
    worker: threading.Thread | None = None
    tree = ProcessTree(os.getpid())
    excluded: dict[int, str] = {}

    def owned() -> ProcessTree:
        tree.refresh()
        table = _processes()
        excluded_parents = {
            pid for pid, start in excluded.items() if table.get(pid, (0, ""))[1] == start
        }
        while True:
            added = {
                pid
                for pid, (parent, _) in table.items()
                if parent in excluded_parents and pid not in excluded_parents
            }
            if not added:
                break
            for pid in added:
                excluded[pid] = table[pid][1]
            excluded_parents.update(added)
        return ProcessTree(
            tree.root,
            {pid: start for pid, start in tree.descendants.items() if excluded.get(pid) != start},
        )

    def stop_owned() -> None:
        try:
            tracked = owned()
            tracked.signal(signal.SIGTERM)
            threading.Event().wait(0.1)
            tracked = owned()
            tracked.signal(signal.SIGKILL)
            if not verify_owned_stopped(tracked):
                policy.usage.cleanup_verified = False
        except (OSError, ValueError, subprocess.SubprocessError):
            policy.usage.cleanup_verified = False

    def inspect() -> None:
        while not stopped.is_set():
            try:
                policy.check(event)
                observed = owned()
                # The parent controller is deliberately excluded; only its new
                # tracked invocation trees belong to this preparation budget.
                policy.observe_rss(observed_descendant_rss_bytes(observed.descendants))
                policy.observe_disk()
            except BaseException as error:
                failures.append(error)
                event.set()
                stop_owned()
                return
            if stopped.wait(interval_s):
                return

    try:
        policy.check(event)
        policy.observe_disk()
        tree.refresh()
        excluded.update(tree.descendants)
        worker = threading.Thread(
            target=inspect, name="leanflow-preparation-resources", daemon=True
        )
        worker.start()
        yield event
    finally:
        stopped.set()
        try:
            if worker is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    policy.usage.cleanup_verified = False
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation resource supervision did not finish.",
                        "Retain the private stage and inspect controller-owned processes before retrying.",
                    )
            if worker is not None:
                stop_owned()
                if not policy.usage.cleanup_verified:
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation descendant cleanup could not be verified.",
                        "Retain the private stage and inspect controller-owned processes before retrying.",
                    )
            if failures:
                error = failures[0]
                if isinstance(error, (OSError, subprocess.SubprocessError)):
                    raise SetupFailure(
                        "setup_resource_inspection_failed",
                        "Preparation process identities could not be supervised.",
                        "Use a supported host with process inspection available.",
                    ) from error
                raise error
        finally:
            _campaign_owner.release()
