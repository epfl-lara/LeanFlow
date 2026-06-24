"""Interrupt signaling extracted from run_agent.AIAgent.

``InterruptController`` owns the per-agent interrupt state that previously lived
directly on ``AIAgent``: the "interrupt requested" flag, the optional message that
triggered the interrupt, and the registry of running child agents used to
propagate interrupts down a delegation tree. It mirrors the ``TokenAccounter``
(65d037d), ``ConversationManager`` (c3bbb31), ``ProviderClientFactory`` (6027ca4)
and ``ToolExecutor`` (74e006b) extractions.

Thread-safety improvement: the requested flag is now backed by a
``threading.Event`` rather than a plain bool. The flag is written from the main
loop and from interrupt handlers (input thread, message receiver) and read from
the main loop, the tool-execution threads (``ToolExecutor`` reads
``agent._interrupt_requested``) and the API-call paths. A ``threading.Event``
gives a memory-safe set/clear/read across all of those without a separate lock,
and ``Event.is_set()`` returns a real ``bool`` (``True``/``False``) so the
existing ``is True`` / ``is False`` assertions in the interrupt tests keep
holding. The child registry is guarded by its own lock.

AIAgent exposes the controller's state through thin delegating ``@property``
shims so that every existing read, write and in-place mutation keeps working
byte-for-byte:
- ``agent._interrupt_requested`` -> getter returns ``is_requested()``; the SETTER
  routes ``= True`` (or any truthy value) to ``request()`` and ``= False``/``None``
  to ``clear()``, preserving the exact ``self._interrupt_requested = True/False``
  assignments used throughout run_agent and the tests.
- ``agent._interrupt_message`` -> the stored message.
- ``agent._active_children`` -> the live child list (so ``.append()``/``.remove()``/
  ``len()``/iteration/indexing all operate on the real list, as the delegate tool
  and tests require); assigning ``= []`` replaces the list.

Monkeypatch handling: the global tool-interrupt signal is reached through
``run_agent._set_interrupt`` *by attribute lookup at call time*, because the
interrupt unit tests do ``with patch("run_agent._set_interrupt"):`` to keep
``agent.interrupt()`` from flipping the real process-wide event. The import of
``run_agent`` is deferred to call time, so this module never imports ``run_agent``
at load and creating a controller never triggers an import cycle.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


def _signal_global_interrupt(value: bool) -> None:
    """Flip the process-wide tool-interrupt signal via ``run_agent._set_interrupt``.

    Looked up on the ``run_agent`` module at call time (not bound at import) so
    that ``with patch("run_agent._set_interrupt"):`` in the interrupt tests
    intercepts this call and prevents flipping the real global event. Imported
    lazily to avoid an import cycle (run_agent imports this module).
    """
    import run_agent

    run_agent._set_interrupt(value)


class InterruptController:
    """Owns the interrupt-requested flag, message, and child registry for one agent."""

    def __init__(self) -> None:
        # Backing the requested flag with an Event makes set/clear/read safe across
        # the main loop, tool threads and API-call threads without a dedicated lock.
        self._requested: threading.Event = threading.Event()
        self._message: Any = None
        # Running child AIAgents, for interrupt propagation down a delegation tree.
        self._children: list[Any] = []
        self._children_lock: threading.Lock = threading.Lock()

    # -- requested flag ----------------------------------------------------

    def is_requested(self) -> bool:
        """Return whether an interrupt is currently requested (a real ``bool``)."""
        return self._requested.is_set()

    def request(self, message: Any = None, *, signal_global: bool = True) -> None:
        """Record an interrupt request: set the event, store the message, signal tools.

        Does NOT propagate to children itself; AIAgent.interrupt() owns the
        propagation order/targets so child-agent semantics stay exactly as before.
        """
        self._requested.set()
        self._message = message
        if signal_global:
            _signal_global_interrupt(True)

    def clear(self, *, signal_global: bool = True) -> None:
        """Clear the interrupt request: reset the event, drop the message, clear tools."""
        self._requested.clear()
        self._message = None
        if signal_global:
            _signal_global_interrupt(False)

    def set_requested(self, value: bool) -> None:
        """Set/clear ONLY the requested event, leaving the message untouched.

        Backs the ``agent._interrupt_requested = True/False`` assignment, which
        historically toggled just the bool flag (no message change, no global
        signal) — those side effects belonged to ``interrupt()``/``clear_interrupt()``.
        """
        if value:
            self._requested.set()
        else:
            self._requested.clear()

    # -- message -----------------------------------------------------------

    @property
    def message(self) -> Any:
        return self._message

    @message.setter
    def message(self, value: Any) -> None:
        self._message = value

    # -- child registry ----------------------------------------------------

    @property
    def children(self) -> list[Any]:
        """The live list of registered child agents.

        Returned by reference so callers (the delegate tool, tests) can
        ``append``/``remove``/index/iterate it directly, exactly as when
        ``_active_children`` was a plain list attribute.
        """
        return self._children

    @children.setter
    def children(self, value: list[Any]) -> None:
        self._children = value

    def register_child(self, child: Any) -> None:
        """Register a running child agent for interrupt propagation (thread-safe)."""
        with self._children_lock:
            self._children.append(child)

    def unregister_child(self, child: Any) -> None:
        """Unregister a child agent (thread-safe); a missing child is ignored."""
        with self._children_lock:
            with contextlib.suppress(ValueError):
                self._children.remove(child)

    def propagate(self, message: Any = None) -> None:
        """Forward an interrupt to every registered child agent.

        Iterates a snapshot so a child unregistering itself mid-propagation
        cannot break the loop; mirrors the original in-line propagation.
        """
        for child in list(self._children):
            try:
                child.interrupt(message)
            except Exception as e:
                logger.debug("Failed to propagate interrupt to child agent: %s", e)
