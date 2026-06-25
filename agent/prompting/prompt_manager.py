"""System-prompt build/cache/invalidate logic extracted from run_agent.AIAgent.

``PromptManager`` owns the per-session system-prompt lifecycle that previously
lived directly on ``AIAgent``: assembling the layered system prompt, caching it
(so it is stable across all turns in a session for maximal prefix-cache hits),
invalidating the cache after context-compression events, and — for continuing
sessions — reusing the stored prompt from the session DB instead of rebuilding.
It mirrors the ``TokenAccounter`` (65d037d), ``ConversationManager`` (c3bbb31),
``ProviderClientFactory`` (6027ca4), ``ToolExecutor`` (74e006b),
``InterruptController`` (06e67ca), ``ResponseNormalizer`` and
``ReasoningProcessor`` extractions.

What moved here (behavior-preserving, logic copied verbatim):
- ``build_system_prompt`` — assemble the full system prompt from all layers
  (identity, tool-aware guidance, user/gateway message, memory, skills, context
  files, timestamp, platform hint). Byte-for-byte identical to the old method.
- the cache itself (``cached`` get/set, ``invalidate``) — same lifetime and
  invalidation semantics (invalidate also reloads memory from disk).
- ``resolve_active_system_prompt`` — the continuation reload-from-DB alignment:
  on a None cache, reuse the stored prompt for a continuing session, else build
  fresh and persist it to the session DB.

How it reaches AIAgent: PromptManager holds an ``_agent`` reference and reads
the agent's config/state (``valid_tool_names``, ``_memory_store``,
``_memory_enabled``, ``_user_profile_enabled``, ``skip_context_files``,
``pass_session_id``, ``session_id``, ``platform``, ``_session_db``). For the
prompt-text layer helpers it does a lazy ``import run_agent`` and resolves
``run_agent.<name>`` at call time, so tests that monkeypatch e.g.
``run_agent.check_toolset_requirements`` / ``run_agent.build_skills_system_prompt``
still intercept. ``resolve_active_system_prompt`` routes its build step back
through ``agent._build_system_prompt`` so ``patch.object(AIAgent,
'_build_system_prompt', ...)`` still intercepts.

The module does NOT import run_agent at load (only lazily, inside methods), so
creating a manager never triggers an import cycle.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PromptManager:
    """Owns the per-session system-prompt build/cache/invalidate lifecycle."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._cached: str | None = None

    # -- cache --------------------------------------------------------------

    @property
    def cached(self) -> str | None:
        return self._cached

    @cached.setter
    def cached(self, value: str | None) -> None:
        self._cached = value

    def invalidate(self) -> None:
        """
        Invalidate the cached system prompt, forcing a rebuild on the next turn.

        Called after context compression events. Also reloads memory from disk
        so the rebuilt prompt captures any writes from this session.
        """
        self._cached = None
        agent = self._agent
        if agent._memory_store:
            agent._memory_store.load_from_disk()

    # -- build --------------------------------------------------------------

    def build_system_prompt(self, system_message: str | None = None) -> str:
        """
        Assemble the full system prompt from all layers.

        Called once per session (cached on the manager) and only rebuilt after
        context compression events. This ensures the system prompt is stable
        across all turns in a session, maximizing prefix cache hits.
        """
        # Resolve the prompt-text layer helpers on run_agent at call time so
        # tests that monkeypatch them (run_agent.check_toolset_requirements,
        # run_agent.build_skills_system_prompt, run_agent.build_context_files_prompt)
        # still intercept. Imported lazily to avoid an import cycle.
        import run_agent

        agent = self._agent

        # Layers (in order):
        #   1. Default agent identity (always present)
        #   2. User / gateway system prompt (if provided)
        #   3. Persistent memory (frozen snapshot)
        #   4. Skills guidance (if skills tools are loaded)
        #   5. Context files (SOUL.md, AGENTS.md, .cursorrules)
        #   6. Current date & time (frozen at build time)
        #   7. Platform-specific formatting hint
        prompt_parts = [run_agent.DEFAULT_AGENT_IDENTITY]

        # Tool-aware behavioral guidance: only inject when the tools are loaded
        tool_guidance = []
        if "memory" in agent.valid_tool_names:
            tool_guidance.append(run_agent.MEMORY_GUIDANCE)
        if "session_search" in agent.valid_tool_names:
            tool_guidance.append(run_agent.SESSION_SEARCH_GUIDANCE)
        if "skill_manage" in agent.valid_tool_names:
            tool_guidance.append(run_agent.SKILLS_GUIDANCE)
        if tool_guidance:
            prompt_parts.append(" ".join(tool_guidance))

        # Note: ephemeral_system_prompt is NOT included here. It's injected at
        # API-call time only so it stays out of the cached/stored system prompt.
        if system_message is not None:
            prompt_parts.append(system_message)

        if agent._memory_store:
            if agent._memory_enabled:
                mem_block = agent._memory_store.format_for_system_prompt("memory")
                if mem_block:
                    prompt_parts.append(mem_block)
            # USER.md is always included when enabled.
            if agent._user_profile_enabled:
                user_block = agent._memory_store.format_for_system_prompt("user")
                if user_block:
                    prompt_parts.append(user_block)

        has_skills_tools = any(
            name in agent.valid_tool_names for name in ["skills_list", "skill_view", "skill_manage"]
        )
        if has_skills_tools:
            avail_toolsets = {
                ts for ts, avail in run_agent.check_toolset_requirements().items() if avail
            }
            skills_prompt = run_agent.build_skills_system_prompt(
                available_tools=agent.valid_tool_names,
                available_toolsets=avail_toolsets,
            )
        else:
            skills_prompt = ""
        if skills_prompt:
            prompt_parts.append(skills_prompt)

        if not agent.skip_context_files:
            context_files_prompt = run_agent.build_context_files_prompt()
            if context_files_prompt:
                prompt_parts.append(context_files_prompt)

        from core.time import now as _now

        now = _now()
        timestamp_line = f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"
        if agent.pass_session_id and agent.session_id:
            timestamp_line += f"\nSession ID: {agent.session_id}"
        prompt_parts.append(timestamp_line)

        platform_key = (agent.platform or "").lower().strip()
        if platform_key in run_agent.PLATFORM_HINTS:
            prompt_parts.append(run_agent.PLATFORM_HINTS[platform_key])

        return "\n\n".join(prompt_parts)

    # -- continuation reload-from-DB alignment ------------------------------

    def resolve_active_system_prompt(
        self,
        system_message: str | None = None,
        conversation_history: list[dict] | None = None,
    ) -> str:
        """Return the active system prompt, building + persisting it if needed.

        System prompt is cached per session for prefix caching. Built once on
        first call, reused for all subsequent calls. Only rebuilt after context
        compression events (which invalidate the cache and reload memory).

        For continuing sessions (gateway creates a fresh AIAgent per message),
        we load the stored system prompt from the session DB instead of
        rebuilding. Rebuilding would pick up memory changes from disk that the
        model already knows about (it wrote them!), producing a different system
        prompt and breaking the Anthropic prefix cache.

        Routes the build step back through ``agent._build_system_prompt`` so
        ``patch.object(AIAgent, '_build_system_prompt', ...)`` still intercepts.
        """
        agent = self._agent
        if self._cached is None:
            stored_prompt = None
            if conversation_history and agent._session_db:
                try:
                    session_row = agent._session_db.get_session(agent.session_id)
                    if session_row:
                        stored_prompt = session_row.get("system_prompt") or None
                except Exception:
                    pass  # Fall through to build fresh

            if stored_prompt:
                # Continuing session — reuse the exact system prompt from
                # the previous turn so the Anthropic cache prefix matches.
                self._cached = stored_prompt
            else:
                # First turn of a new session — build from scratch.
                self._cached = agent._build_system_prompt(system_message)
                # Store the system prompt snapshot in SQLite
                if agent._session_db:
                    try:
                        agent._session_db.update_system_prompt(agent.session_id, self._cached)
                    except Exception as e:
                        logger.debug("Session DB update_system_prompt failed: %s", e)

        return self._cached
