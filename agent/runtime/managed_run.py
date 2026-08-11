"""Define the managed-run contract between the native runner and ``AIAgent``.

The protocol makes the runner's callbacks and one-shot tool-result guidance
explicit and typed while keeping runner-owned scratch state separate.

native_runner now drives the appendix exclusively through the explicit
``stage_/set_/clear_tool_result_appendix`` methods below — it no longer reaches into the private
``_post_tool_result_appendix`` attribute. That attribute remains AIAgent's internal backing store
(it is load-bearing: ``_apply_*`` clears it to ``None`` while an explicit ``clear`` removes it, a
one-shot distinction the tool loop and tests rely on), but it is now an implementation detail of
AIAgent rather than a cross-module contract.

Two distinct kinds of coupling exist — keep them separate:

1. Behavioral contract (AIAgent's loop READS these):
   - ``post_tool_result_callback`` / ``pre_tool_call_callback`` / ``tool_progress_callback`` /
     ``step_callback`` — public callback attributes AIAgent invokes during the loop.
   - The **post-tool-result appendix**: a one-shot string the runner stages so that extra
     guidance is appended to the next tool result the model sees. AIAgent consumes it in BOTH
     the sequential and concurrent tool-execution paths via
     ``AIAgent._apply_post_tool_result_appendix``. The runner manages it through three methods:
     ``stage_tool_result_appendix(text)`` (accumulate), ``set_tool_result_appendix(text)``
     (replace), and ``clear_tool_result_appendix()`` (discard).

2. Runner-private scratch state (AIAgent NEVER reads these — native_runner just parks them on the
   agent object as a convenient per-run bag, read back inside its own callbacks):
   ``_managed_autonomy_state``, ``_managed_pending_theorem_feedback``,
   ``_managed_step_boundary_recorded_attempt``, ``_managed_step_boundary_closed``,
   ``_managed_tool_task_id``, ``_managed_base_reasoning_config``. These are not part of the
   AIAgent behavioral contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

# Callback signatures AIAgent invokes during the conversation loop.
PreToolCallCallback = Callable[[str, Mapping[str, Any]], str | None]
PostToolResultCallback = Callable[[str, Mapping[str, Any], str], None]
ToolResultProjectionCallback = Callable[[str, Mapping[str, Any], str], str]
ToolProgressCallback = Callable[..., None]
StepCallback = Callable[[int, "list[str]"], None]

# Names of the runner-private scratch attributes parked on the agent (see module docstring).
# Keep the complete list explicit so changes to the runner contract are reviewable.
MANAGED_SCRATCH_ATTRS: tuple[str, ...] = (
    "_managed_autonomy_state",
    "_managed_pending_theorem_feedback",
    "_managed_step_boundary_recorded_attempt",
    "_managed_step_boundary_closed",
    "_managed_tool_task_id",
    "_managed_base_reasoning_config",
)


@runtime_checkable
class ManagedRunAgent(Protocol):
    """The subset of ``AIAgent`` that ``native_runner`` depends on to drive a managed run.

    Structural (duck-typed) — ``AIAgent`` satisfies it without importing this module. Use it to
    type-annotate managed-runner helpers so the coupling is checked rather than implicit.
    """

    pre_tool_call_callback: PreToolCallCallback | None
    post_tool_result_callback: PostToolResultCallback | None
    tool_result_projection_callback: ToolResultProjectionCallback | None
    tool_progress_callback: ToolProgressCallback | None
    step_callback: StepCallback | None

    def stage_tool_result_appendix(self, text: str) -> None:
        """Stage one-shot guidance appended to the next tool result (accumulates if repeated)."""
        ...

    def set_tool_result_appendix(self, text: str) -> None:
        """Replace any staged appendix with ``text`` (empty text clears it)."""
        ...

    def clear_tool_result_appendix(self) -> None:
        """Discard any staged appendix so the next tool result is unmodified."""
        ...
