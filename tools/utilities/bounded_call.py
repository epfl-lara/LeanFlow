"""Run blocking callbacks behind a process-safe wall-clock boundary."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Callable, Generic, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class BoundedCallResult(Generic[_T]):
    """Describe whether a blocking callback completed within its deadline."""

    completed: bool
    value: _T | None = None
    error: BaseException | None = None


def run_bounded_call(callback: Callable[[], _T], *, timeout_s: float) -> BoundedCallResult[_T]:
    """Run ``callback`` without waiting past ``timeout_s`` for its cleanup.

    A daemon thread is intentional: executor context managers wait for running
    work during shutdown and would defeat the outer wall-clock guarantee.
    """
    outcomes: Queue[tuple[_T | None, BaseException | None]] = Queue(maxsize=1)

    def _run() -> None:
        try:
            outcomes.put((callback(), None))
        except BaseException as exc:  # Preserve the wrapped callback's behavior.
            outcomes.put((None, exc))

    worker = Thread(target=_run, name="leanflow-bounded-call", daemon=True)
    worker.start()
    worker.join(max(0.0, float(timeout_s)))
    if worker.is_alive():
        return BoundedCallResult(completed=False)
    try:
        value, error = outcomes.get_nowait()
    except Empty:
        return BoundedCallResult(
            completed=True,
            error=RuntimeError("bounded callback exited without returning an outcome"),
        )
    return BoundedCallResult(completed=True, value=value, error=error)
