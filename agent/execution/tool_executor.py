"""Tool-call execution strategy extracted from run_agent.AIAgent.

``ToolExecutor`` owns the dispatch logic for a turn's tool calls: it decides
between concurrent and sequential execution, invokes individual tools, applies
the post-tool-result appendix, honors the interrupt flag, and injects budget
warnings -- exactly as the inline AIAgent methods did before this extraction.

The executor holds a reference to the owning ``AIAgent`` and reaches the
turn-local loop state through it (callbacks, the interrupt flag, the checkpoint
manager, the stores, ``quiet_mode``/``log_prefix``, ``stage``/``_apply`` of the
appendix, ``_get_budget_warning``, ``_max_tool_result_chars``). That state is
intrinsically part of the agent's run loop, so reaching it via the agent is a
genuine boundary -- the dispatch *strategy* (the ~600 lines below) has left the
god class.

Module-level helpers and the monkeypatch-sensitive ``handle_function_call`` live
in ``run_agent``. To preserve test patches like ``patch("run_agent.handle_function_call")``
and ``patch("run_agent.OpenAI")`` (and to avoid an import cycle at module load),
this module does NOT import ``run_agent`` at the top. Instead each method that
needs a run_agent-level name does a lazy ``import run_agent`` and resolves the
name through the module namespace at call time, so the currently-installed patch
always wins.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from run_agent import AIAgent

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes a turn's tool calls on behalf of an :class:`AIAgent`.

    Constructed with the owning agent; all turn state is reached through it.
    The dispatch logic is behavior-preserving relative to the former
    ``AIAgent._execute_tool_calls*`` / ``_invoke_tool`` / ``_preflight_tool_call``
    methods.
    """

    def __init__(self, agent: AIAgent) -> None:
        self._agent = agent

    # ── Dispatcher ──────────────────────────────────────────────────────────
    def execute(
        self,
        assistant_message: Any,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Execute tool calls from the assistant message and append results.

        Dispatches to concurrent execution when multiple independent tool calls
        are present, falling back to sequential execution for single calls or
        when interactive tools (e.g. clarify) are in the batch.
        """
        import run_agent

        agent = self._agent
        tool_calls = assistant_message.tool_calls

        # Route the chosen strategy through the AGENT's wrapper methods (not
        # ``self.execute_*`` directly) so callers/tests that patch
        # ``agent._execute_tool_calls_sequential`` / ``_execute_tool_calls_concurrent``
        # still intercept the dispatch, matching the pre-extraction behavior.

        # Single tool call or interactive tool present → sequential
        if len(tool_calls) <= 1 or any(
            tc.function.name in run_agent._NEVER_PARALLEL_TOOLS for tc in tool_calls
        ):
            return agent._execute_tool_calls_sequential(
                assistant_message, messages, effective_task_id, api_call_count
            )

        # Multiple non-interactive tools → concurrent
        return agent._execute_tool_calls_concurrent(
            assistant_message, messages, effective_task_id, api_call_count
        )

    # ── Single-tool invocation ──────────────────────────────────────────────
    def invoke_tool(self, function_name: str, function_args: dict, effective_task_id: str) -> str:
        """Invoke a single tool and return the result string. No display logic.

        Handles both agent-level tools (todo, memory, etc.) and registry-dispatched
        tools. Used by the concurrent execution path; the sequential path retains
        its own inline invocation for backward-compatible display handling.
        """
        import run_agent

        agent = self._agent
        preflight_result = self.preflight_tool_call(function_name, function_args)
        if preflight_result is not None:
            return preflight_result
        if function_name == "todo":
            from tools.implementations.todo_tool import todo_tool as _todo_tool

            return _todo_tool(
                todos=function_args.get("todos"),
                merge=function_args.get("merge", False),
                store=agent._todo_store,
            )
        elif function_name == "session_search":
            if not agent._session_db:
                return json.dumps({"success": False, "error": "Session database not available."})
            from tools.implementations.session_search_tool import session_search as _session_search

            return _session_search(
                query=function_args.get("query", ""),
                role_filter=function_args.get("role_filter"),
                limit=function_args.get("limit", 3),
                db=agent._session_db,
                current_session_id=agent.session_id,
            )
        elif function_name == "memory":
            target = function_args.get("target", "memory")
            from tools.implementations.memory_tool import memory_tool as _memory_tool

            result = _memory_tool(
                action=function_args.get("action"),
                target=target,
                content=function_args.get("content"),
                old_text=function_args.get("old_text"),
                store=agent._memory_store,
            )
            return result
        elif function_name == "clarify":
            from tools.implementations.clarify_tool import clarify_tool as _clarify_tool

            return _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                callback=agent.clarify_callback,
            )
        elif function_name == "delegate_task":
            from tools.implementations.delegate_tool import delegate_task as _delegate_task

            return _delegate_task(
                goal=function_args.get("goal"),
                context=function_args.get("context"),
                toolsets=function_args.get("toolsets"),
                tasks=function_args.get("tasks"),
                max_iterations=function_args.get("max_iterations"),
                parent_agent=agent,
            )
        else:
            return run_agent.handle_function_call(
                function_name,
                function_args,
                effective_task_id,
                enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                owner_id=agent.session_id,
                parent_agent=agent,
            )

    def preflight_tool_call(self, function_name: str, function_args: dict) -> str | None:
        agent = self._agent
        callback = getattr(agent, "pre_tool_call_callback", None)
        if not callback:
            return None
        try:
            result = callback(function_name, function_args)
        except Exception as cb_err:
            logger.debug("pre_tool_call_callback error: %s", cb_err)
            return None
        if result is None or result is False:
            return None
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return str(result)

    # ── Concurrent strategy ─────────────────────────────────────────────────
    def execute_concurrent(
        self,
        assistant_message: Any,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Execute multiple tool calls concurrently using a thread pool.

        Results are collected in the original tool-call order and appended to
        messages so the API sees them in the expected sequence.
        """
        import run_agent

        agent = self._agent
        tool_calls = assistant_message.tool_calls
        num_tools = len(tool_calls)

        # ── Pre-flight: interrupt check ──────────────────────────────────
        if agent._interrupt_requested:
            print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
            for tc in tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "content": f"[Tool execution cancelled — {tc.function.name} was skipped due to user interrupt]",
                        "tool_call_id": tc.id,
                    }
                )
            return

        # ── Parse args + pre-execution bookkeeping ───────────────────────
        parsed_calls = []  # list of (tool_call, function_name, function_args)
        for tool_call in tool_calls:
            function_name = tool_call.function.name

            # Reset nudge counters
            if function_name == "memory":
                agent._turns_since_memory = 0
            elif function_name == "skill_manage":
                agent._iters_since_skill = 0

            try:
                function_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                function_args = {}
            if not isinstance(function_args, dict):
                function_args = {}

            # Checkpoint for file-mutating tools
            if (
                function_name in ("write_file", "patch", "apply_verified_patch")
                and agent._checkpoint_mgr.enabled
            ):
                try:
                    file_path = function_args.get("path", "")
                    if file_path:
                        work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                        agent._checkpoint_mgr.ensure_checkpoint(work_dir, f"before {function_name}")
                except Exception:
                    logger.warning(
                        "Failed to create safety checkpoint before %s; proceeding without it",
                        function_name,
                        exc_info=True,
                    )

            # Checkpoint before destructive terminal commands
            if function_name == "terminal" and agent._checkpoint_mgr.enabled:
                try:
                    cmd = function_args.get("command", "")
                    if run_agent._is_destructive_command(cmd):
                        cwd = function_args.get("workdir") or os.getenv("TERMINAL_CWD", os.getcwd())
                        agent._checkpoint_mgr.ensure_checkpoint(cwd, f"before terminal: {cmd[:60]}")
                except Exception:
                    logger.warning(
                        "Failed to create safety checkpoint before destructive terminal command; proceeding without it",
                        exc_info=True,
                    )

            parsed_calls.append((tool_call, function_name, function_args))

        # ── Logging / callbacks ──────────────────────────────────────────
        tool_names_str = ", ".join(name for _, name, _ in parsed_calls)
        if not agent.quiet_mode:
            print(
                f"\n{agent.log_prefix}┌─ Tools: {num_tools} concurrent call(s) — {tool_names_str}"
            )
            for i, (tc, name, args) in enumerate(parsed_calls, 1):
                if agent.verbose_logging:
                    print(f"{agent.log_prefix}│  {i}. {name}")
                    for line in run_agent._format_tool_args_for_log(name, args):
                        print(f"{agent.log_prefix}│     {line}")
                else:
                    print(f"{agent.log_prefix}│  {i}. {name}")
                    for line in run_agent._format_tool_args_for_log(name, args):
                        print(f"{agent.log_prefix}│     {line}")

        for _, name, args in parsed_calls:
            if agent.tool_progress_callback:
                try:
                    preview = run_agent._build_tool_preview(name, args)
                    agent.tool_progress_callback(name, preview, args)
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error: {cb_err}")
            run_agent._emit_workflow_event(
                "tool-call",
                f"Concurrent tool call: {name}",
                **run_agent._workflow_agent_event_details(
                    agent,
                    tool=name,
                    arguments=args,
                    concurrent=True,
                    iteration=api_call_count,
                ),
            )

        # ── Concurrent execution ─────────────────────────────────────────
        # Each slot holds (function_name, function_args, function_result, duration, error_flag)
        results: list = [None] * num_tools

        def _run_tool(index, tool_call, function_name, function_args):
            """Worker function executed in a thread."""
            start = time.time()
            try:
                result = self.invoke_tool(function_name, function_args, effective_task_id)
            except Exception as tool_error:
                result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error(
                    "_invoke_tool raised for %s: %s", function_name, tool_error, exc_info=True
                )
            duration = time.time() - start
            is_error, _ = run_agent._detect_tool_failure(function_name, result)
            results[index] = (function_name, function_args, result, duration, is_error)

        # Start spinner for CLI mode
        spinner = None
        if agent.quiet_mode:
            face = random.choice(run_agent.KawaiiSpinner.KAWAII_WAITING)
            spinner = run_agent.KawaiiSpinner(
                f"{face} ⚡ running {num_tools} tools concurrently", spinner_type="dots"
            )
            spinner.start()

        try:
            max_workers = min(num_tools, run_agent._MAX_TOOL_WORKERS)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for i, (tc, name, args) in enumerate(parsed_calls):
                    f = executor.submit(_run_tool, i, tc, name, args)
                    futures.append(f)

                # Wait for all to complete (exceptions are captured inside _run_tool)
                concurrent.futures.wait(futures)
        finally:
            if spinner:
                # Build a summary message for the spinner stop
                completed = sum(1 for r in results if r is not None)
                total_dur = sum(r[3] for r in results if r is not None)
                spinner.stop(
                    f"⚡ {completed}/{num_tools} tools completed in {total_dur:.1f}s total"
                )

        # ── Post-execution: display per-tool results ─────────────────────
        for i, (tc, name, args) in enumerate(parsed_calls):
            r = results[i]
            if r is None:
                # Shouldn't happen, but safety fallback
                function_result = f"Error executing tool '{name}': thread did not return a result"
                tool_duration = 0.0
            else:
                function_name, function_args, function_result, tool_duration, is_error = r

                if is_error:
                    result_preview = (
                        function_result[:200] if len(function_result) > 200 else function_result
                    )
                    logger.warning(
                        "Tool %s returned error (%.2fs): %s",
                        function_name,
                        tool_duration,
                        result_preview,
                    )

                if agent.verbose_logging:
                    logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
                    logging.debug(f"Tool result ({len(function_result)} chars): {function_result}")

            # Print cute message per tool
            if agent.quiet_mode:
                cute_msg = run_agent._get_cute_tool_message_impl(
                    name, args, tool_duration, result=function_result
                )
                print(f"  {cute_msg}")
            elif not agent.quiet_mode:
                print(f"{agent.log_prefix}│  {i + 1}. {name} done in {tool_duration:.2f}s")
                for line in run_agent._format_tool_result_for_log(name, function_result):
                    print(f"{agent.log_prefix}│     {line}")
            run_agent._emit_workflow_event(
                "tool-result",
                f"Concurrent tool result: {name}",
                **run_agent._workflow_agent_event_details(
                    agent,
                    tool=name,
                    arguments=args,
                    result=function_result,
                    duration_seconds=tool_duration,
                    concurrent=True,
                    iteration=api_call_count,
                    is_error=run_agent._detect_tool_failure(name, function_result)[0],
                ),
            )

            # Truncate oversized results. Lean advisor/decomposition tools get
            # a larger cap because long proof-strategy notes are intentional.
            max_tool_result_chars = agent._max_tool_result_chars(function_name)
            if len(function_result) > max_tool_result_chars:
                original_len = len(function_result)
                function_result = (
                    function_result[:max_tool_result_chars]
                    + f"\n\n[Truncated: tool response was {original_len:,} chars, "
                    f"exceeding the {max_tool_result_chars:,} char limit]"
                )

            # Append tool result message in order
            tool_msg = {
                "role": "tool",
                "content": function_result,
                "tool_call_id": tc.id,
            }
            messages.append(tool_msg)

            if agent.post_tool_result_callback:
                try:
                    agent.post_tool_result_callback(name, args, function_result)
                except Exception as cb_err:
                    logger.debug("post_tool_result_callback error: %s", cb_err)
                agent._apply_post_tool_result_appendix(tool_msg)

        if not agent.quiet_mode:
            print(f"{agent.log_prefix}└─ Tool batch complete")

        # ── Budget pressure injection ────────────────────────────────────
        budget_warning = agent._get_budget_warning(api_call_count)
        if budget_warning and messages and messages[-1].get("role") == "tool":
            last_content = messages[-1]["content"]
            try:
                parsed = json.loads(last_content)
                if isinstance(parsed, dict):
                    parsed["_budget_warning"] = budget_warning
                    messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
                else:
                    messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
            except (json.JSONDecodeError, TypeError):
                messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
            if not agent.quiet_mode:
                remaining = agent.max_iterations - api_call_count
                tier = "⚠️  WARNING" if remaining <= agent.max_iterations * 0.1 else "💡 CAUTION"
                print(f"{agent.log_prefix}{tier}: {remaining} iterations remaining")

    # ── Sequential strategy ─────────────────────────────────────────────────
    def execute_sequential(
        self,
        assistant_message: Any,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools."""
        import run_agent

        agent = self._agent
        for i, tool_call in enumerate(assistant_message.tool_calls, 1):
            # SAFETY: check interrupt BEFORE starting each tool.
            # If the user sent "stop" during a previous tool's execution,
            # do NOT start any more tools -- skip them all immediately.
            if agent._interrupt_requested:
                remaining_calls = assistant_message.tool_calls[i - 1 :]
                if remaining_calls:
                    agent._vprint(
                        f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)",
                        force=True,
                    )
                for skipped_tc in remaining_calls:
                    skipped_name = skipped_tc.function.name
                    skip_msg = {
                        "role": "tool",
                        "content": f"[Tool execution cancelled — {skipped_name} was skipped due to user interrupt]",
                        "tool_call_id": skipped_tc.id,
                    }
                    messages.append(skip_msg)
                break

            function_name = tool_call.function.name

            # Reset nudge counters when the relevant tool is actually used
            if function_name == "memory":
                agent._turns_since_memory = 0
            elif function_name == "skill_manage":
                agent._iters_since_skill = 0

            try:
                function_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as e:
                logging.warning(f"Unexpected JSON error after validation: {e}")
                function_args = {}
            if not isinstance(function_args, dict):
                function_args = {}

            if not agent.quiet_mode:
                print(f"\n{agent.log_prefix}┌─ Tool {i}: {function_name}")
                for line in run_agent._format_tool_args_for_log(function_name, function_args):
                    print(f"{agent.log_prefix}│  {line}")

            if agent.tool_progress_callback:
                try:
                    preview = run_agent._build_tool_preview(function_name, function_args)
                    agent.tool_progress_callback(function_name, preview, function_args)
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error: {cb_err}")
            run_agent._emit_workflow_event(
                "tool-call",
                f"Tool call: {function_name}",
                **run_agent._workflow_agent_event_details(
                    agent,
                    tool=function_name,
                    arguments=function_args,
                    concurrent=False,
                    iteration=api_call_count,
                ),
            )

            # Checkpoint: snapshot working dir before file-mutating tools
            if (
                function_name in ("write_file", "patch", "apply_verified_patch")
                and agent._checkpoint_mgr.enabled
            ):
                try:
                    file_path = function_args.get("path", "")
                    if file_path:
                        work_dir = agent._checkpoint_mgr.get_working_dir_for_path(file_path)
                        agent._checkpoint_mgr.ensure_checkpoint(work_dir, f"before {function_name}")
                except Exception:
                    # never block tool execution
                    logger.warning(
                        "Failed to create safety checkpoint before %s; proceeding without it",
                        function_name,
                        exc_info=True,
                    )

            # Checkpoint before destructive terminal commands
            if function_name == "terminal" and agent._checkpoint_mgr.enabled:
                try:
                    cmd = function_args.get("command", "")
                    if run_agent._is_destructive_command(cmd):
                        cwd = function_args.get("workdir") or os.getenv("TERMINAL_CWD", os.getcwd())
                        agent._checkpoint_mgr.ensure_checkpoint(cwd, f"before terminal: {cmd[:60]}")
                except Exception:
                    # never block tool execution
                    logger.warning(
                        "Failed to create safety checkpoint before destructive terminal command; proceeding without it",
                        exc_info=True,
                    )

            tool_start_time = time.time()
            preflight_result = self.preflight_tool_call(function_name, function_args)

            if preflight_result is not None:
                function_result = preflight_result
                tool_duration = time.time() - tool_start_time
            elif function_name == "todo":
                from tools.implementations.todo_tool import todo_tool as _todo_tool

                function_result = _todo_tool(
                    todos=function_args.get("todos"),
                    merge=function_args.get("merge", False),
                    store=agent._todo_store,
                )
                tool_duration = time.time() - tool_start_time
                if agent.quiet_mode:
                    agent._vprint(
                        f"  {run_agent._get_cute_tool_message_impl('todo', function_args, tool_duration, result=function_result)}"
                    )
            elif function_name == "session_search":
                if not agent._session_db:
                    function_result = json.dumps(
                        {"success": False, "error": "Session database not available."}
                    )
                else:
                    from tools.implementations.session_search_tool import (
                        session_search as _session_search,
                    )

                    function_result = _session_search(
                        query=function_args.get("query", ""),
                        role_filter=function_args.get("role_filter"),
                        limit=function_args.get("limit", 3),
                        db=agent._session_db,
                        current_session_id=agent.session_id,
                    )
                tool_duration = time.time() - tool_start_time
                if agent.quiet_mode:
                    agent._vprint(
                        f"  {run_agent._get_cute_tool_message_impl('session_search', function_args, tool_duration, result=function_result)}"
                    )
            elif function_name == "memory":
                target = function_args.get("target", "memory")
                from tools.implementations.memory_tool import memory_tool as _memory_tool

                function_result = _memory_tool(
                    action=function_args.get("action"),
                    target=target,
                    content=function_args.get("content"),
                    old_text=function_args.get("old_text"),
                    store=agent._memory_store,
                )
                tool_duration = time.time() - tool_start_time
                if agent.quiet_mode:
                    agent._vprint(
                        f"  {run_agent._get_cute_tool_message_impl('memory', function_args, tool_duration, result=function_result)}"
                    )
            elif function_name == "clarify":
                from tools.implementations.clarify_tool import clarify_tool as _clarify_tool

                function_result = _clarify_tool(
                    question=function_args.get("question", ""),
                    choices=function_args.get("choices"),
                    callback=agent.clarify_callback,
                )
                tool_duration = time.time() - tool_start_time
                if agent.quiet_mode:
                    agent._vprint(
                        f"  {run_agent._get_cute_tool_message_impl('clarify', function_args, tool_duration, result=function_result)}"
                    )
            elif function_name == "delegate_task":
                from tools.implementations.delegate_tool import delegate_task as _delegate_task

                tasks_arg = function_args.get("tasks")
                if tasks_arg and isinstance(tasks_arg, list):
                    spinner_label = f"🔀 delegating {len(tasks_arg)} tasks"
                else:
                    goal_preview = (function_args.get("goal") or "")[:30]
                    spinner_label = f"🔀 {goal_preview}" if goal_preview else "🔀 delegating"
                spinner = None
                if agent.quiet_mode:
                    face = random.choice(run_agent.KawaiiSpinner.KAWAII_WAITING)
                    spinner = run_agent.KawaiiSpinner(
                        f"{face} {spinner_label}", spinner_type="dots"
                    )
                    spinner.start()
                agent._delegate_spinner = spinner
                _delegate_result = None
                try:
                    function_result = _delegate_task(
                        goal=function_args.get("goal"),
                        context=function_args.get("context"),
                        toolsets=function_args.get("toolsets"),
                        tasks=tasks_arg,
                        max_iterations=function_args.get("max_iterations"),
                        parent_agent=agent,
                    )
                    _delegate_result = function_result
                finally:
                    agent._delegate_spinner = None
                    tool_duration = time.time() - tool_start_time
                    cute_msg = run_agent._get_cute_tool_message_impl(
                        "delegate_task", function_args, tool_duration, result=_delegate_result
                    )
                    if spinner:
                        spinner.stop(cute_msg)
                    elif agent.quiet_mode:
                        agent._vprint(f"  {cute_msg}")
            elif agent.quiet_mode and agent._stream_callback is None:
                face = random.choice(run_agent.KawaiiSpinner.KAWAII_WAITING)
                emoji = run_agent._get_tool_emoji(function_name)
                preview = (
                    run_agent._build_tool_preview(function_name, function_args) or function_name
                )
                if len(preview) > 30:
                    preview = preview[:27] + "..."
                spinner = run_agent.KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type="dots")
                spinner.start()
                _spinner_result = None
                try:
                    function_result = run_agent.handle_function_call(
                        function_name,
                        function_args,
                        effective_task_id,
                        enabled_tools=(
                            list(agent.valid_tool_names) if agent.valid_tool_names else None
                        ),
                        owner_id=agent.session_id,
                        parent_agent=agent,
                    )
                    _spinner_result = function_result
                except Exception as tool_error:
                    function_result = f"Error executing tool '{function_name}': {tool_error}"
                    logger.error(
                        "handle_function_call raised for %s: %s",
                        function_name,
                        tool_error,
                        exc_info=True,
                    )
                finally:
                    tool_duration = time.time() - tool_start_time
                    cute_msg = run_agent._get_cute_tool_message_impl(
                        function_name, function_args, tool_duration, result=_spinner_result
                    )
                    spinner.stop(cute_msg)
            else:
                try:
                    function_result = run_agent.handle_function_call(
                        function_name,
                        function_args,
                        effective_task_id,
                        enabled_tools=(
                            list(agent.valid_tool_names) if agent.valid_tool_names else None
                        ),
                        owner_id=agent.session_id,
                        parent_agent=agent,
                    )
                except Exception as tool_error:
                    function_result = f"Error executing tool '{function_name}': {tool_error}"
                    logger.error(
                        "handle_function_call raised for %s: %s",
                        function_name,
                        tool_error,
                        exc_info=True,
                    )
                tool_duration = time.time() - tool_start_time

            result_preview = (
                function_result
                if agent.verbose_logging
                else (function_result[:200] if len(function_result) > 200 else function_result)
            )

            # Log tool errors to the persistent error log so [error] tags
            # in the UI always have a corresponding detailed entry on disk.
            _is_error_result, _ = run_agent._detect_tool_failure(function_name, function_result)
            if _is_error_result:
                logger.warning(
                    "Tool %s returned error (%.2fs): %s",
                    function_name,
                    tool_duration,
                    result_preview,
                )

            if agent.verbose_logging:
                logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
                logging.debug(f"Tool result ({len(function_result)} chars): {function_result}")

            # Guard against tools returning absurdly large content that would
            # blow up the context window. Most tools are capped at 100K chars;
            # Lean advisor/decomposition tools get a larger cap because long
            # proof-strategy output is an intentional use case.
            max_tool_result_chars = agent._max_tool_result_chars(function_name)
            if len(function_result) > max_tool_result_chars:
                original_len = len(function_result)
                function_result = (
                    function_result[:max_tool_result_chars]
                    + f"\n\n[Truncated: tool response was {original_len:,} chars, "
                    f"exceeding the {max_tool_result_chars:,} char limit]"
                )

            tool_msg = {"role": "tool", "content": function_result, "tool_call_id": tool_call.id}
            messages.append(tool_msg)

            if not agent.quiet_mode:
                print(f"{agent.log_prefix}│  done in {tool_duration:.2f}s")
                for line in run_agent._format_tool_result_for_log_with_limits(
                    function_name,
                    function_result,
                    multiline_head=agent.tool_output_head_lines,
                    multiline_tail=agent.tool_output_tail_lines,
                    wrapped_head=max(agent.tool_output_head_lines // 2, 1),
                    wrapped_tail=max(agent.tool_output_tail_lines // 2, 0),
                    plain_head=max(agent.tool_output_head_lines - 2, 1),
                    plain_tail=max(agent.tool_output_tail_lines - 2, 0),
                    string_char_threshold=agent.log_preview_chars,
                ):
                    print(f"{agent.log_prefix}│  {line}")
                print(f"{agent.log_prefix}└─")
            run_agent._emit_workflow_event(
                "tool-result",
                f"Tool result: {function_name}",
                **run_agent._workflow_agent_event_details(
                    agent,
                    tool=function_name,
                    arguments=function_args,
                    result=function_result,
                    duration_seconds=tool_duration,
                    concurrent=False,
                    iteration=api_call_count,
                    is_error=run_agent._detect_tool_failure(function_name, function_result)[0],
                ),
            )

            if agent.post_tool_result_callback:
                try:
                    agent.post_tool_result_callback(function_name, function_args, function_result)
                except Exception as cb_err:
                    logger.debug("post_tool_result_callback error: %s", cb_err)
                agent._apply_post_tool_result_appendix(tool_msg)

            if agent._interrupt_requested and i < len(assistant_message.tool_calls):
                remaining = len(assistant_message.tool_calls) - i
                agent._vprint(
                    f"{agent.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)",
                    force=True,
                )
                for skipped_tc in assistant_message.tool_calls[i:]:
                    skipped_name = skipped_tc.function.name
                    skip_msg = {
                        "role": "tool",
                        "content": f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                        "tool_call_id": skipped_tc.id,
                    }
                    messages.append(skip_msg)
                break

            if agent.tool_delay > 0 and i < len(assistant_message.tool_calls):
                time.sleep(agent.tool_delay)

        # ── Budget pressure injection ─────────────────────────────────
        # After all tool calls in this turn are processed, check if we're
        # approaching max_iterations. If so, inject a warning into the LAST
        # tool result's JSON so the LLM sees it naturally when reading results.
        budget_warning = agent._get_budget_warning(api_call_count)
        if budget_warning and messages and messages[-1].get("role") == "tool":
            last_content = messages[-1]["content"]
            try:
                parsed = json.loads(last_content)
                if isinstance(parsed, dict):
                    parsed["_budget_warning"] = budget_warning
                    messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
                else:
                    messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
            except (json.JSONDecodeError, TypeError):
                messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
            if not agent.quiet_mode:
                remaining = agent.max_iterations - api_call_count
                tier = "⚠️  WARNING" if remaining <= agent.max_iterations * 0.1 else "💡 CAUTION"
                print(f"{agent.log_prefix}{tier}: {remaining} iterations remaining")
