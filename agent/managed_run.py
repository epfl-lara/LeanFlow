"""The managed-run contract between ``epflemma_cli.native_runner`` and ``run_agent.AIAgent``.

native_runner drives an ``AIAgent`` instance through an autonomous Lean proving workflow. Today
that coupling is expressed implicitly via private-attribute injection on the agent object. This
module makes the contract **explicit and typed** so the upcoming decomposition of both monoliths
(native_runner in Phase 2, AIAgent in Phase 4) has a stable, documented surface to preserve.

Nothing here changes behavior. It is a typed description plus the names of the concrete helpers
``AIAgent`` now exposes. The legacy private attributes remain the backing store, so existing
native_runner code keeps working; the explicit migration of native_runner onto these methods is
deferred to a later phase.

Two distinct kinds of coupling exist — keep them separate:

1. Behavioral contract (AIAgent's loop READS these):
   - ``post_tool_result_callback`` / ``pre_tool_call_callback`` / ``tool_progress_callback`` /
     ``step_callback`` — public callback attributes AIAgent invokes during the loop.
   - The **post-tool-result appendix**: a one-shot string the runner stages so that extra
     guidance is appended to the next tool result the model sees. AIAgent consumes it in BOTH
     the sequential and concurrent tool-execution paths via
     ``AIAgent._apply_post_tool_result_appendix`` and exposes
     ``AIAgent.stage_tool_result_appendix(text)`` to stage it. Backed by the
     ``_post_tool_result_appendix`` attribute for backwards compatibility.

2. Runner-private scratch state (AIAgent NEVER reads these — native_runner just parks them on the
   agent object as a convenient per-run bag, read back inside its own callbacks):
   ``_managed_autonomy_state``, ``_managed_pending_theorem_feedback``,
   ``_managed_step_boundary_recorded_attempt``, ``_managed_step_boundary_closed``,
   ``_managed_tool_task_id``, ``_managed_base_reasoning_config``. These are not part of the
   AIAgent behavioral contract; Phase 2 may relocate them into a runner-owned context object.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# Callback signatures AIAgent invokes during the conversation loop.
PreToolCallCallback = Callable[[str, Mapping[str, Any]], Optional[str]]
PostToolResultCallback = Callable[[str, Mapping[str, Any], str], None]
ToolProgressCallback = Callable[..., None]
StepCallback = Callable[[int, "list[str]"], None]

# Names of the runner-private scratch attributes parked on the agent (see module docstring).
# Documented here so a future refactor can find/relocate them deliberately rather than by grep.
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

    pre_tool_call_callback: Optional[PreToolCallCallback]
    post_tool_result_callback: Optional[PostToolResultCallback]
    tool_progress_callback: Optional[ToolProgressCallback]
    step_callback: Optional[StepCallback]

    def stage_tool_result_appendix(self, text: str) -> None:
        """Stage one-shot guidance appended to the next tool result (accumulates if repeated)."""
        ...
