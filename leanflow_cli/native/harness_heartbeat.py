"""Checkpoint author accounting with finite shutdown and exact deferred writer ownership."""

from __future__ import annotations

import threading
from collections.abc import Callable

from core.campaign_limits import AccountingError, CampaignLimits
from leanflow_cli.formalization.preparation_campaign import CampaignOwner


class AuthorHeartbeat:
    """Keep a failed or outstanding checkpoint from admitting further paid work."""

    def __init__(
        self,
        budget: CampaignLimits,
        cancelled: threading.Event,
        checkpoint: Callable[[], None],
        *,
        owner: CampaignOwner | None = None,
        interval_s: float = 1.0,
    ) -> None:
        self.budget, self.cancelled, self.checkpoint = budget, cancelled, checkpoint
        self.owner, self.interval_s = owner, interval_s
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Durably mark and attach this exact writer before starting its thread."""
        self.budget.begin_checkpoints()
        if self.owner is not None:
            self.owner.attach_worker()
        self._thread = threading.Thread(
            target=self._run, name="leanflow-author-heartbeat", daemon=True
        )
        try:
            self._thread.start()
        except BaseException as error:
            self._thread = None
            self._fail(error)
            if self.owner is not None:
                self.owner.close(worker=True)
            raise

    def _fail(self, error: BaseException) -> None:
        self._error = error
        self.cancelled.set()
        self.budget.fail_accounting(error)

    def _run(self) -> None:
        try:
            while not self._finished.wait(self.interval_s):
                self.budget.persist()
                self.checkpoint()
            # The parent still has terminal accounting effects. It alone may
            # clear the crash marker after this worker has actually stopped.
            self.budget.persist()
        except BaseException as error:
            self._fail(error)
        finally:
            # No reaper and no is_alive snapshot: this worker relinquishes its
            # own effects, whether the parent returns before or after it does.
            if self.owner is not None:
                self.owner.close(worker=True)

    def check(self) -> None:
        """Fail before review, publication or another invocation after a worker failure."""
        if self._error is not None:
            raise AccountingError("Author accounting checkpoint failed") from self._error

    def close(self, timeout_s: float = 3.0) -> bool:
        """Stop finitely, retaining the active marker and owner while effects remain possible."""
        self._finished.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                self.cancelled.set()
                return False
        return True
