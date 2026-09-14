"""Bound concurrent RCP requests by credential before spending job admissions."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from urllib.parse import urlsplit

from core.home import leanflow_home


class ProviderQueueStopped(Exception):
    """Stop a waiting session without classifying it as a failed provider request."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Provider request queue {status}")
        self.status = status


@contextmanager
def provider_request_slot(
    agent: Any,
    *,
    deadline: float,
    cancelled: Callable[[], bool],
    on_wait: Callable[[], None],
) -> Iterator[None]:
    """Hold one cross-process RCP slot through the response, releasing it on exit.

    RCP virtual keys can allow only one in-flight request even when separate
    theorem cells run concurrently. A lock shared under LeanFlow home covers
    every model using the same key; neither the key nor its alias is written.
    LEANFLOW_RCP_MAX_CONCURRENT_REQUESTS raises the default one-slot allowance
    for a higher-capacity key. Set the same allowance in every sharing process.
    Waiting obeys the session wall deadline and cancellation, and emits progress
    before durable request admission. Other providers retain their concurrency.
    """
    hostname = urlsplit(str(getattr(agent, "base_url", ""))).hostname
    if hostname not in {"inference.rcp.epfl.ch", "inference-rcp.epfl.ch"}:
        yield
        return
    key = str(getattr(agent, "api_key", ""))
    if not key:
        raise ValueError("An RCP request requires a credential")
    capacity = int(os.getenv("LEANFLOW_RCP_MAX_CONCURRENT_REQUESTS", "1"))
    if not 1 <= capacity <= 256:
        raise ValueError("LEANFLOW_RCP_MAX_CONCURRENT_REQUESTS must be between 1 and 256")
    identity = hashlib.sha256(key.encode()).hexdigest()
    directory = leanflow_home() / "runtime" / "rcp-request-slots"
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        handles = [
            stack.enter_context(
                (directory / (identity + (f".{slot}" if slot else "") + ".lock")).open("a")
            )
            for slot in range(capacity)
        ]
        next_notice = 0.0
        while True:
            if cancelled():
                raise ProviderQueueStopped("interrupted")
            now = time.monotonic()
            if now >= deadline:
                raise ProviderQueueStopped("timeout")
            acquired = None
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = handle
                    break
                except BlockingIOError:
                    continue
            if acquired is not None:
                break
            if now >= next_notice:
                on_wait()
                next_notice = now + 5.0
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(acquired, fcntl.LOCK_UN)
