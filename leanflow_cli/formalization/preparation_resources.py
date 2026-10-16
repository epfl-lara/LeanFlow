"""Supervise new preparation descendants across acquisition, independent checking, and fingerprinting."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agent.runtime.harness.processes import ProcessTree, _processes, start_identity_matches
from agent.runtime.harness.resources import observed_descendant_rss_bytes
from leanflow_cli.formalization.preparation_inspection import (
    PreparationInspection,
    observe_metadata,
)
from leanflow_cli.workflows.project_setup.cleanup import verify_owned_stopped
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import SetupFailure

_campaign_owner = threading.Lock()


class _PreparationOwnership:
    """Release serial ownership after every old monitor process effect has finished."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._guard = threading.Lock()
        self._transferred = False
        self._relinquished = False
        self._released = False

    def _release_once(self) -> None:
        """Release the campaign while the small coordination lock is held."""
        if not self._released:
            self._released = True
            self._release()

    def transfer(self) -> bool:
        """Defer ownership and return whether the parent owns terminal diagnostics."""
        with self._guard:
            if self._transferred:
                raise RuntimeError("Preparation ownership was already transferred")
            self._transferred = True
            if self._relinquished:
                self._release_once()
                return True
            return False

    def relinquish(self) -> bool:
        """Finish all monitor process effects and assign its deferred diagnostic writer."""
        with self._guard:
            if self._relinquished:
                raise RuntimeError("Preparation monitor already relinquished ownership")
            self._relinquished = True
            if self._transferred:
                self._release_once()
                return True
            return False

    def release_normal(self) -> None:
        """Release a normally joined campaign without transferring process authority."""
        with self._guard:
            if self._transferred:
                raise RuntimeError("Deferred preparation owns its serial release")
            self._release_once()


@contextmanager
def supervise_preparation(
    policy: SetupExecutionPolicy,
    *,
    cancel: threading.Event | None = None,
    interval_s: float = 0.25,
    diagnostic_path: Path | None = None,
) -> Iterator[threading.Event]:
    """Guard a dedicated serial controller campaign and stop only its new owned descendants.

    Existing descendants and their later children are excluded. Callers must pass
    the yielded cancellation event to every independent check and poll it before
    infrastructure commitment. Metadata gaps never authorize context completion:
    a fresh healthy final sample and strict cleanup precede every successful exit.
    Limits are sampled; they are not kernel RSS quotas.
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
    root_start: str | None = None
    inspection: PreparationInspection | None = None
    body_failed = False
    owner_transferred = False
    ownership = _PreparationOwnership(_campaign_owner.release)

    def owned(*, sampling: bool = False) -> ProcessTree:
        """Retain original controller identity in both frames and exclude preexisting branches."""
        nonlocal root_start
        if sampling:
            observe_metadata("parentage_refresh", tree.refresh)
        else:
            tree.refresh()
        if tree._root_retired or tree.root_identity is None:
            raise ValueError("Preparation controller identity became unavailable")
        if root_start is None:
            root_start = tree.root_identity
            excluded.update(tree.descendants)
        elif not start_identity_matches(tree.root_identity, root_start):
            raise ValueError("Preparation controller identity changed")
        table = observe_metadata("parentage_exclusion", _processes) if sampling else _processes()
        if tree.root not in table or not start_identity_matches(table[tree.root][1], root_start):
            raise ValueError("Preparation controller identity changed between observations")
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

    def measure() -> None:
        """Keep policy/disk/cap failures immediate; tolerate gaps only inside metadata reads."""
        policy.check(event)
        observed = owned(sampling=True)
        # The aggregate workflow window separately accounts for the controller.
        rss = observe_metadata("rss", lambda: observed_descendant_rss_bytes(observed.descendants))
        policy.observe_rss(rss)
        policy.observe_disk()

    def require_healthy() -> None:
        """Consume the same remaining gap budget before initial admission or final success."""
        assert inspection is not None
        while not inspection.sample(measure):
            policy.check(event)
            event.wait(interval_s)

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

    def finalize_deferred_diagnostic() -> None:
        """Contain non-authorizing diagnostic IO after the failed context relinquishes authority."""
        if inspection is not None:
            try:
                inspection.abort(cleanup_verified=False)
            except BaseException:
                # Setup already failed closed. Do not leak an asynchronous
                # exception payload or hold serial ownership on diagnostic IO.
                pass

    def inspect() -> None:
        try:
            while not stopped.is_set():
                try:
                    assert inspection is not None
                    inspection.sample(measure)
                except BaseException as error:
                    failures.append(error)
                    event.set()
                    stop_owned()
                    return
                if stopped.wait(interval_s):
                    return
        finally:
            # No process observation, signalling, cleanup, or sample mutation
            # may follow this handoff. Only its single diagnostic writer remains.
            if ownership.relinquish():
                finalize_deferred_diagnostic()

    try:
        inspection = PreparationInspection(diagnostic_path)
        require_healthy()
        worker = threading.Thread(
            target=inspect, name="leanflow-preparation-resources", daemon=True
        )
        worker.start()
        yield event
    except BaseException:
        body_failed = True
        event.set()
        raise
    finally:
        stopped.set()
        try:
            if worker is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    policy.usage.cleanup_verified = False
                    owner_transferred = True
                    if ownership.transfer():
                        finalize_deferred_diagnostic()
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation resource supervision did not finish.",
                        "Retain the private stage and inspect controller-owned processes before retrying.",
                    )
            final_error: BaseException | None = None
            if inspection is not None and not body_failed and not failures:
                try:
                    require_healthy()
                except BaseException as error:
                    final_error = error
                    event.set()
            if inspection is not None:
                stop_owned()
                if not policy.usage.cleanup_verified:
                    inspection.abort(cleanup_verified=False)
                    raise SetupFailure(
                        "setup_cleanup_unverified",
                        "Preparation descendant cleanup could not be verified.",
                        "Retain the private stage and inspect controller-owned processes before retrying.",
                    )
            if failures:
                failure = failures[0]
                if inspection is not None:
                    inspection.abort(cleanup_verified=policy.usage.cleanup_verified)
                raise failure
            if final_error is not None:
                assert inspection is not None
                inspection.abort(cleanup_verified=policy.usage.cleanup_verified)
                raise final_error
            if inspection is not None:
                if body_failed:
                    inspection.abort(cleanup_verified=policy.usage.cleanup_verified)
                else:
                    inspection.finish(cleanup_verified=policy.usage.cleanup_verified)
        finally:
            if not owner_transferred:
                ownership.release_normal()
