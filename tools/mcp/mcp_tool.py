#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Client Support

Connects to external MCP servers via stdio or HTTP/StreamableHTTP transport,
discovers their tools, and registers them into the LeanFlow tool registry
so the agent can call them like any built-in tool.

Configuration is read from ~/.leanflow/config.yaml under the ``mcp_servers`` key.
The ``mcp`` Python package is optional -- if not installed, this module is a
no-op and logs a debug message.

Example config::

    mcp_servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        timeout: 120         # per-tool-call timeout in seconds (default: 120)
        connect_timeout: 60  # initial connection timeout (default: 60)
      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
      remote_api:
        url: "https://my-mcp-server.example.com/mcp"
        headers:
          Authorization: "Bearer sk-..."
        timeout: 180
      analysis:
        command: "npx"
        args: ["-y", "analysis-server"]
        sampling:                    # server-initiated LLM requests
          enabled: true              # default: true
          model: "gemini-3-flash"    # override model (optional)
          max_tokens_cap: 4096       # max tokens per request
          timeout: 30                # LLM call timeout (seconds)
          max_rpm: 10                # max requests per minute
          allowed_models: []         # model whitelist (empty = all)
          max_tool_rounds: 5         # tool loop limit (0 = disable)
          log_level: "info"          # audit verbosity

Features:
    - Stdio transport (command + args) and HTTP/StreamableHTTP transport (url)
    - Automatic reconnection with exponential backoff (up to 5 retries)
    - Environment variable filtering for stdio subprocesses (security)
    - Credential stripping in error messages returned to the LLM
    - Configurable per-server timeouts for tool calls and connections
    - Thread-safe architecture with dedicated background event loop
    - Sampling support: MCP servers can request LLM completions via
      sampling/createMessage (text and tool-use responses)

Architecture:
    A dedicated background event loop (_mcp_loop) runs in a daemon thread.
    Each MCP server runs as a long-lived asyncio Task on this loop, keeping
    its transport context alive. Tool call coroutines are scheduled onto the
    loop via ``run_coroutine_threadsafe()``.

    On shutdown, each server Task is signalled to exit its ``async with``
    block, ensuring the anyio cancel-scope cleanup happens in the *same*
    Task that opened the connection (required by anyio).

Thread safety:
    _servers and _mcp_loop/_mcp_thread are accessed from both the MCP
    background thread and caller threads.  All mutations are protected by
    _lock so the code is safe regardless of GIL presence (e.g. Python 3.13+
    free-threading).
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful import -- MCP SDK is an optional dependency
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
_MCP_SAMPLING_TYPES = False
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_AVAILABLE = True
    try:
        from mcp.client.streamable_http import streamablehttp_client

        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        _MCP_HTTP_AVAILABLE = False
    # Sampling types -- separated so older SDK versions don't break MCP support. This import is an
    # availability probe: it only sets _MCP_SAMPLING_TYPES (mcp_sampling imports + uses these names
    # itself), so the names are unused here on purpose -- hence the per-line noqa.
    try:
        from mcp.types import (
            CreateMessageResult,  # noqa: F401
            CreateMessageResultWithTools,  # noqa: F401
            ErrorData,  # noqa: F401
            TextContent,  # noqa: F401
            ToolUseContent,  # noqa: F401
        )

        _MCP_SAMPLING_TYPES = True
    except ImportError:
        logger.debug("MCP sampling types not available -- sampling disabled")
except ImportError:
    logger.debug("mcp package not installed -- MCP tool support disabled")

# ---------------------------------------------------------------------------
# Stdio/HTTP transport plumbing -- extracted to tools/mcp_transport.py and
# re-exported so callers/tests that resolve tools.mcp.mcp_tool.<name> keep working.
# mcp_transport does NOT import mcp_tool, so this introduces no import cycle.
# ---------------------------------------------------------------------------
from tools.mcp.mcp_reclaim import should_recycle_after_tool  # noqa: E402
from tools.mcp.mcp_transport import (  # noqa: E402
    _DEFAULT_CONNECT_TIMEOUT,
    _augment_lean_stdio_env,
    _build_safe_env,
    _disable_incompatible_local_loogle,
    _effective_connect_timeout,
    _format_connect_error,
    _repair_loogle_cache_if_needed,
    _resolve_stdio_command,
    _resolve_stdio_cwd,
    _sanitize_error,
    open_mcp_stderr_log,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT = 120  # seconds for tool calls
_MAX_RECONNECT_RETRIES = 5
_MAX_BACKOFF_SECONDS = 60
_PROOF_AUTO_SERVER_NAME = "lean-proof-auto"
_PROOF_AUTO_SEARCH_TOOL_NAME = "search_automated_proof"
_LEAN_LSP_SERVER_NAME = "lean-lsp"
_LEAN_LSP_INTERACTIVE_STATE_TOOLS = frozenset(
    {"lean_diagnostic_messages", "lean_goal", "lean_term_goal"}
)
_LEAN_LSP_INTERACTIVE_STATE_TIMEOUT_S = 60.0

_RequestResult = TypeVar("_RequestResult")


def _is_proof_auto_search(server_name: str, tool_name: str) -> bool:
    """Return whether one MCP call is the managed proof-auto search route."""
    return (
        str(server_name or "").strip() == _PROOF_AUTO_SERVER_NAME
        and str(tool_name or "").strip() == _PROOF_AUTO_SEARCH_TOOL_NAME
    )


def _effective_tool_request_timeout(
    server_name: str,
    tool_name: str,
    args: dict[str, Any],
    configured_timeout: float,
) -> float:
    """Return a workload-specific bounded transport deadline."""
    try:
        configured = max(0.001, float(configured_timeout))
    except (TypeError, ValueError):
        configured = float(_DEFAULT_TOOL_TIMEOUT)
    if (
        str(server_name or "").strip() == _LEAN_LSP_SERVER_NAME
        and str(tool_name or "").strip() in _LEAN_LSP_INTERACTIVE_STATE_TOOLS
    ):
        return min(configured, _LEAN_LSP_INTERACTIVE_STATE_TIMEOUT_S)
    if not _is_proof_auto_search(server_name, tool_name):
        return configured
    try:
        requested = float(args.get("search_budget_s", configured))
    except (TypeError, ValueError):
        return configured
    if requested <= 0:
        return configured
    return min(configured, requested)


class _MCPServerRecycling(RuntimeError):
    """Signal that an operation was rejected before dispatch during retirement."""

    def __init__(self, server: "MCPServerTask"):
        super().__init__(f"MCP server '{server.name}' is recycling")
        self.server = server


class _MCPServerRecyclePending(RuntimeError):
    """Signal a bounded wait for a replacement server without backend failure."""


class _MCPServerRecycleFailed(_MCPServerRecyclePending):
    """Signal that fail-closed retirement retained the old server ownership."""


# ---------------------------------------------------------------------------
# Sampling -- server-initiated LLM requests (MCP sampling/createMessage).
# The SamplingHandler callback plus its numeric/audit-path helpers live in
# tools/mcp_sampling.py and are re-exported here so callers/tests that resolve
# tools.mcp.mcp_tool.<name> keep working.  mcp_sampling does NOT import mcp_tool, so
# this introduces no import cycle.  MCPServerTask.run() (below) instantiates
# SamplingHandler via this re-export.
# ---------------------------------------------------------------------------
import contextlib

from tools.mcp.mcp_sampling import (  # noqa: E402
    SamplingHandler,
)

# ---------------------------------------------------------------------------
# Server task -- each MCP server lives in one long-lived asyncio Task
# ---------------------------------------------------------------------------


class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task.

    The entire connection lifecycle (connect, discover, serve, disconnect)
    runs inside one asyncio Task so that anyio cancel-scopes created by
    the transport client are entered and exited in the same Task context.

    Supports both stdio and HTTP/StreamableHTTP transports.
    """

    __slots__ = (
        "name",
        "session",
        "tool_timeout",
        "_task",
        "_ready",
        "_shutdown_event",
        "_tools",
        "_error",
        "_config",
        "_sampling",
        "_registered_tool_names",
        "_accepting_requests",
        "_active_requests",
        "_requests_idle",
        "_recycle_requested",
        "_recycle_complete",
        "_recycle_finished",
        "_recycle_error",
        "_retire_task",
    )

    def __init__(self, name: str):
        self.name = name
        self.session: Any | None = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._tools: list = []
        self._error: Exception | None = None
        self._config: dict = {}
        self._sampling: SamplingHandler | None = None
        self._registered_tool_names: list[str] = []
        self._accepting_requests = True
        self._active_requests = 0
        self._requests_idle = asyncio.Event()
        self._requests_idle.set()
        self._recycle_requested = False
        self._recycle_complete = threading.Event()
        self._recycle_finished = threading.Event()
        self._recycle_error: BaseException | None = None
        self._retire_task: asyncio.Task | None = None

    def _is_http(self) -> bool:
        """Check if this server uses HTTP transport."""
        return "url" in self._config

    async def _run_stdio(self, config: dict):
        """Run the server using stdio transport."""
        command = config.get("command")
        args = config.get("args", [])
        user_env = config.get("env")

        if not command:
            raise ValueError(f"MCP server '{self.name}' has no 'command' in config")

        safe_env = _build_safe_env(user_env)
        command, safe_env = _resolve_stdio_command(command, safe_env)
        cwd = _resolve_stdio_cwd(self.name, config)
        safe_env = _augment_lean_stdio_env(self.name, safe_env, cwd)
        safe_env = _disable_incompatible_local_loogle(self.name, safe_env, cwd)
        _repair_loogle_cache_if_needed(self.name, safe_env)
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=safe_env if safe_env else None,
            cwd=cwd,
        )

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        # Route noisy managed-Lean server stderr to a log file instead of the workflow
        # console (see open_mcp_stderr_log). None -> inherit sys.stderr as before.
        errlog = open_mcp_stderr_log(self.name, cwd)
        stdio_ctx = (
            stdio_client(server_params, errlog)
            if errlog is not None
            else stdio_client(server_params)
        )
        try:
            async with stdio_ctx as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                    await session.initialize()
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    await self._shutdown_event.wait()
        finally:
            if errlog is not None:
                try:
                    errlog.close()
                except Exception:
                    pass

    async def _run_http(self, config: dict):
        """Run the server using HTTP/StreamableHTTP transport."""
        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                f"MCP server '{self.name}' requires HTTP transport but "
                "mcp.client.streamable_http is not available. "
                "Upgrade the mcp package to get HTTP support."
            )

        url = config["url"]
        headers = config.get("headers")
        connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=float(connect_timeout),
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                await session.initialize()
                self.session = session
                await self._discover_tools()
                self._ready.set()
                await self._shutdown_event.wait()

    async def _discover_tools(self):
        """Discover tools from the connected session."""
        if self.session is None:
            return
        tools_result = await self.session.list_tools()
        self._tools = tools_result.tools if hasattr(tools_result, "tools") else []

    async def run(self, config: dict):
        """Long-lived coroutine: connect, discover tools, wait, disconnect.

        Includes automatic reconnection with exponential backoff if the
        connection drops unexpectedly (unless shutdown was requested).
        """
        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)

        # Set up sampling handler if enabled and SDK types are available
        sampling_config = config.get("sampling", {})
        if sampling_config.get("enabled", True) and _MCP_SAMPLING_TYPES:
            self._sampling = SamplingHandler(self.name, sampling_config)
        else:
            self._sampling = None

        # Validate: warn if both url and command are present
        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' in config. "
                "Using HTTP transport ('url'). Remove 'command' to silence "
                "this warning.",
                self.name,
            )
        retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    await self._run_http(config)
                else:
                    await self._run_stdio(config)
                # Normal exit (shutdown requested) -- break out
                break
            except Exception as exc:
                self.session = None

                # If this is the first connection attempt, report the error
                if not self._ready.is_set():
                    self._error = exc
                    self._ready.set()
                    return

                # If shutdown was requested, don't reconnect
                if self._shutdown_event.is_set():
                    logger.debug(
                        "MCP server '%s' disconnected during shutdown: %s",
                        self.name,
                        exc,
                    )
                    return

                retries += 1
                if retries > _MAX_RECONNECT_RETRIES:
                    logger.warning(
                        "MCP server '%s' failed after %d reconnection attempts, giving up: %s",
                        self.name,
                        _MAX_RECONNECT_RETRIES,
                        exc,
                    )
                    return

                logger.warning(
                    "MCP server '%s' connection lost (attempt %d/%d), reconnecting in %.0fs: %s",
                    self.name,
                    retries,
                    _MAX_RECONNECT_RETRIES,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                # Check again after sleeping
                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None

    async def start(self, config: dict):
        """Create the background Task and wait until ready (or failed)."""
        self._task = asyncio.ensure_future(self.run(config))
        await self._ready.wait()
        if self._error:
            raise self._error

    async def request(
        self,
        operation: Callable[[Any], Awaitable[_RequestResult]],
    ) -> _RequestResult:
        """Run one session request while exposing an idle barrier for retirement."""
        session = self.session
        if not self._accepting_requests or session is None:
            if self._recycle_requested:
                raise _MCPServerRecycling(self)
            raise RuntimeError(f"MCP server '{self.name}' is shutting down")
        self._active_requests += 1
        self._requests_idle.clear()
        try:
            return await operation(session)
        finally:
            self._active_requests = max(0, self._active_requests - 1)
            if self._active_requests == 0:
                self._requests_idle.set()

    async def retire(self) -> None:
        """Drain admitted calls, stop new requests atomically, and shut down.

        Every registered handler has its own timeout, and the MCP-loop bridge
        cancels a timed-out coroutine. Waiting for the idle barrier therefore
        remains bounded by those call contracts without killing useful
        concurrent evidence at an arbitrary shorter grace period.
        """
        self._recycle_requested = True
        self._accepting_requests = False
        while self._active_requests:
            await self._requests_idle.wait()
        await self.shutdown()
        # Registered tool handlers may retain this object after registry
        # replacement. Drop completed transport state so those harmless stale
        # closures cannot retain the retired connection's object graph.
        self._task = None
        self._sampling = None
        self._tools.clear()
        self._config.clear()

    async def shutdown(self):
        """Signal the Task to exit and wait for clean resource teardown."""
        self._accepting_requests = False
        self._shutdown_event.set()
        if not self._ready.is_set():
            # Unblock ``start()`` when final shutdown races initial transport
            # setup. Otherwise the discovery coroutine can survive after its
            # owned server task has already been canceled.
            self._error = RuntimeError(f"MCP server '{self.name}' was shut down during startup")
            self._ready.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                logger.warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        self.session = None


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_servers: dict[str, MCPServerTask] = {}
# Own servers from construction until registration or confirmed teardown. A
# startup is intentionally visible here before it reaches ``_servers`` so final
# runtime cleanup cannot overlook an unregistered stdio process.
_starting_servers: dict[str, MCPServerTask] = {}

# Dedicated event loop running in a background daemon thread.
_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None

# Protects _mcp_loop, _mcp_thread, and _servers from concurrent access.
_lock = threading.Lock()
_mcp_shutting_down = False


def _ensure_mcp_loop():
    """Start the background event loop thread if not already running."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _mcp_thread.start()


def _run_on_mcp_loop(
    coro_or_factory,
    timeout: float = 30,
    *,
    completion_event: threading.Event | None = None,
):
    """Schedule one coroutine on the MCP event loop and block until done.

    A zero-argument coroutine factory delays construction until this bridge
    actually owns the operation. Tests and failure shims that intercept the
    bridge can therefore decline a call without leaking an un-awaited
    coroutine, while submitted operations retain the bridge's cancellation
    ownership on timeout.
    """
    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        raise RuntimeError("MCP event loop is not running")
    if completion_event is not None:

        async def _tracked():
            try:
                operation = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
                return await operation
            finally:
                # Unlike ConcurrentFuture.done(), this runs only after async
                # cancellation has unwound the transport's cleanup path.
                completion_event.set()

        coro = _tracked()
    else:
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except BaseException:
        with contextlib.suppress(Exception):
            coro.close()
        if completion_event is not None:
            completion_event.set()
        raise
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        # A timed-out handler must release MCPServerTask.request's active-call
        # barrier; otherwise a later research recycle can wait forever on a
        # coroutine whose caller already gave up.
        future.cancel()
        raise


_mcp_reconnect_lock = threading.Lock()
_mcp_discovery_fences: dict[str, threading.Event] = {}


def _remaining_timeout(deadline: float) -> float:
    """Return positive remaining wall-clock time or raise a normal timeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("MCP request timed out while waiting for server recycle")
    return remaining


def _wait_for_discovery_cleanup(name: str, deadline: float) -> None:
    """Wait for one canceled startup to finish teardown within this caller's budget."""
    while True:
        with _lock:
            fence = _mcp_discovery_fences.get(name)
        if fence is None:
            return
        if not fence.wait(timeout=_remaining_timeout(deadline)):
            raise TimeoutError(f"MCP server '{name}' startup cleanup is still running")
        with _lock:
            if _mcp_discovery_fences.get(name) is fence:
                _mcp_discovery_fences.pop(name, None)


def _discover_replacement_server(
    name: str,
    *,
    timeout: float,
    completion_event: threading.Event,
) -> None:
    """Discover one exact replacement under an async-cleanup completion fence."""
    try:
        servers = _load_mcp_config()
        config = servers.get(name)
        if not isinstance(config, dict) or not _parse_boolish(
            config.get("enabled", True), default=True
        ):
            raise RuntimeError(f"MCP server '{name}' is not configured and enabled")
        _ensure_mcp_loop()
    except BaseException:
        # No async startup was submitted, so there is no deferred cleanup to
        # fence. Release the caller immediately on configuration/loop errors.
        completion_event.set()
        raise

    async def _connect_exact() -> None:
        try:
            await _discover_and_register_server(name, config)
        except BaseException:
            # Cancellation can arrive after registration inserted the server
            # but before tool registration completed. Remove and reap only the
            # exact in-flight identity before releasing the cleanup fence.
            with _lock:
                partial = _servers.get(name)
            if partial is not None:
                partial._accepting_requests = False
                try:
                    await partial.shutdown()
                except BaseException as cleanup_exc:
                    partial._recycle_error = cleanup_exc
                    raise _MCPServerRecycleFailed(
                        f"MCP server '{name}' replacement cleanup failed; "
                        "retained replacement ownership"
                    ) from cleanup_exc
                with _lock:
                    if _servers.get(name) is partial:
                        _servers.pop(name, None)
            raise

    _run_on_mcp_loop(
        _connect_exact,
        timeout=max(0.001, float(timeout)),
        completion_event=completion_event,
    )


def _replacement_mcp_server(
    name: str,
    *,
    previous: MCPServerTask,
    timeout: float,
) -> MCPServerTask:
    """Return a replacement within the caller's remaining tool deadline."""
    deadline = time.monotonic() + max(0.001, float(timeout))

    def current_replacement() -> MCPServerTask | None:
        with _lock:
            candidate = _servers.get(name)
        if (
            candidate is not None
            and candidate is not previous
            and candidate.session is not None
            and candidate._accepting_requests
        ):
            return candidate
        if candidate is not None and candidate is not previous:
            raise _MCPServerRecycleFailed(
                f"MCP server '{name}' has a retained replacement whose cleanup " "has not completed"
            )
        return None

    current = current_replacement()
    if current is not None:
        return current
    _wait_for_discovery_cleanup(name, deadline)
    if not _mcp_reconnect_lock.acquire(timeout=_remaining_timeout(deadline)):
        raise TimeoutError(f"MCP server '{name}' reconnect is already in progress")
    try:
        _wait_for_discovery_cleanup(name, deadline)
        current = current_replacement()
        if current is not None:
            return current
        fence = threading.Event()
        with _lock:
            _mcp_discovery_fences[name] = fence
        try:
            _discover_replacement_server(
                name,
                timeout=_remaining_timeout(deadline),
                completion_event=fence,
            )
        finally:
            # A timeout deliberately leaves an unset fence installed. The
            # tracked async wrapper sets it only after canceled startup cleanup
            # completes; a later caller removes it after waiting.
            if fence.is_set():
                with _lock:
                    if _mcp_discovery_fences.get(name) is fence:
                        _mcp_discovery_fences.pop(name, None)
        current = current_replacement()
        if current is None:
            raise RuntimeError(f"MCP server '{name}' did not reconnect after recycle")
        return current
    finally:
        _mcp_reconnect_lock.release()


def _run_server_operation(
    server_name: str,
    server: MCPServerTask,
    operation: Callable[[Any], Awaitable[_RequestResult]],
    *,
    timeout: float,
    observe_server: Callable[[MCPServerTask], None] | None = None,
) -> _RequestResult:
    """Run an operation and transparently cross server-recycle boundaries.

    A recycling rejection occurs before ``operation`` is invoked, so retrying
    it on a replacement server cannot duplicate side effects.
    """
    deadline = time.monotonic() + max(0.001, float(timeout))
    current = server
    attempt = 0
    while True:

        async def _call() -> _RequestResult:
            return await current.request(operation)

        try:
            if observe_server is not None:
                observe_server(current)
            call_timeout = float(timeout) if attempt == 0 else _remaining_timeout(deadline)
            return _run_on_mcp_loop(_call, timeout=call_timeout)
        except _MCPServerRecycling as exc:
            try:
                remaining = _remaining_timeout(deadline)
            except TimeoutError as timeout_exc:
                raise _MCPServerRecyclePending(
                    f"MCP server '{server_name}' recycle is still draining an active request"
                ) from timeout_exc
            if not exc.server._recycle_finished.wait(timeout=remaining):
                raise _MCPServerRecyclePending(
                    f"MCP server '{server_name}' recycle is still draining an active request"
                ) from exc
            if exc.server._recycle_error is not None:
                raise _MCPServerRecycleFailed(
                    f"MCP server '{server_name}' recycle failed; retained old server ownership: "
                    f"{type(exc.server._recycle_error).__name__}: {exc.server._recycle_error}"
                ) from exc.server._recycle_error
            try:
                current = _replacement_mcp_server(
                    server_name,
                    previous=exc.server,
                    timeout=_remaining_timeout(deadline),
                )
                _remaining_timeout(deadline)
            except TimeoutError as timeout_exc:
                raise _MCPServerRecyclePending(
                    f"MCP server '{server_name}' replacement exceeded the tool deadline"
                ) from timeout_exc
            attempt += 1


def _resolve_handler_server(
    server_name: str,
    registered_server: MCPServerTask | None,
    *,
    timeout: float,
) -> MCPServerTask | None:
    """Resolve a live server, crossing a recycle after schema discovery if needed."""
    with _lock:
        current = _servers.get(server_name)
    if current is not None and (current.session is not None or current._recycle_requested):
        return current
    if registered_server is None or not registered_server._recycle_requested:
        return None
    deadline = time.monotonic() + max(0.001, float(timeout))
    if not registered_server._recycle_finished.wait(timeout=max(0.001, float(timeout))):
        raise _MCPServerRecyclePending(
            f"MCP server '{server_name}' recycle is still draining an active request"
        )
    if registered_server._recycle_error is not None:
        raise _MCPServerRecycleFailed(
            f"MCP server '{server_name}' recycle failed; retained old server ownership: "
            f"{type(registered_server._recycle_error).__name__}: "
            f"{registered_server._recycle_error}"
        ) from registered_server._recycle_error
    try:
        replacement = _replacement_mcp_server(
            server_name,
            previous=registered_server,
            timeout=_remaining_timeout(deadline),
        )
        _remaining_timeout(deadline)
        return replacement
    except TimeoutError as timeout_exc:
        raise _MCPServerRecyclePending(
            f"MCP server '{server_name}' replacement exceeded the tool deadline"
        ) from timeout_exc


def _recycle_pending_result(exc: _MCPServerRecyclePending) -> str:
    """Return a retryable transport result that wrappers must not circuit-break."""
    payload: dict[str, object] = {
        "error": _sanitize_error(str(exc)),
        "mcp_recycling": True,
        "retryable": True,
    }
    if isinstance(exc, _MCPServerRecycleFailed):
        payload["cleanup_failed"] = True
    return json.dumps(payload)


def recycle_mcp_server(
    name: str,
    *,
    expected_server: MCPServerTask | None = None,
) -> bool:
    """Schedule retirement and make a server eligible for lazy rediscovery.

    The identity guard prevents delayed cleanup from closing a replacement
    connection. Other MCP servers and the shared event loop remain live.
    """
    with _lock:
        if _mcp_shutting_down:
            return False
        server = _servers.get(name)
    if server is None or (expected_server is not None and server is not expected_server):
        return False

    async def _retire() -> None:
        try:
            await server.retire()
        except BaseException as exc:
            # Keep the exact server in the ownership map and do not advertise
            # successful completion. Otherwise a replacement could overlap an
            # orphaned heavy process after a teardown failure.
            server._recycle_error = exc
            logger.error(
                "Failed to retire MCP server '%s'; retaining fail-closed ownership: %s",
                name,
                exc,
            )
        else:
            with _lock:
                if _servers.get(name) is server:
                    _servers.pop(name, None)
            server._recycle_complete.set()
        finally:
            server._retire_task = None
            server._recycle_finished.set()

    async def _schedule() -> bool:
        with _lock:
            if _mcp_shutting_down:
                return False
        if server._recycle_requested and not (
            server._recycle_finished.is_set() and server._recycle_error is not None
        ):
            return False
        server._recycle_requested = True
        server._accepting_requests = False
        server._recycle_error = None
        server._recycle_complete.clear()
        server._recycle_finished.clear()
        server._retire_task = asyncio.create_task(_retire())
        return True

    try:
        return bool(_run_on_mcp_loop(_schedule, timeout=2.0))
    except Exception as exc:
        logger.warning("Failed to recycle MCP server '%s': %s", name, exc)
        return False


# ---------------------------------------------------------------------------
# Config loading -- _load_mcp_config lives in tools/mcp_config.py and is
# re-exported so callers/tests that resolve tools.mcp.mcp_tool._load_mcp_config
# (including the patch sites in tests) keep working.  Its callers
# (discover_mcp_tools, get_mcp_status) stay here.  mcp_config does NOT import
# mcp_tool, so this introduces no import cycle.
# ---------------------------------------------------------------------------
from tools.mcp.mcp_config import _load_mcp_config  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Server connection helper
# ---------------------------------------------------------------------------


async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """Create an MCPServerTask, start it, and return when ready.

    The server Task keeps the connection alive in the background.
    Call ``server.shutdown()`` (on the same event loop) to tear it down.

    Raises:
        ValueError: if required config keys are missing.
        ImportError: if HTTP transport is needed but not available.
        Exception: on connection or initialization failure.
    """
    server = MCPServerTask(name)
    with _lock:
        if _mcp_shutting_down:
            raise RuntimeError("MCP runtime shutdown is in progress")
        existing = _starting_servers.get(name)
        if existing is not None and existing is not server:
            raise RuntimeError(f"MCP server '{name}' startup is already in progress")
        _starting_servers[name] = server
    try:
        await server.start(config)
        return server
    except BaseException:
        # A caller-bounded reconnect can cancel startup before ``start`` owns
        # the long-lived transport task. Reap that task here so timeout
        # enforcement never leaves a second unregistered server tree behind.
        try:
            await server.shutdown()
        except BaseException as cleanup_exc:
            # Keep ``_starting_servers[name]`` as fail-closed ownership. A
            # later reconnect and finalizer can see and retry this exact task.
            raise RuntimeError(
                f"MCP server '{name}' startup cleanup failed: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ) from cleanup_exc
        with _lock:
            if _starting_servers.get(name) is server:
                _starting_servers.pop(name, None)
        raise


# ---------------------------------------------------------------------------
# Handler / check-fn factories
# ---------------------------------------------------------------------------


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop.

    The handler conforms to the registry's dispatch interface:
    ``handler(args_dict, **kwargs) -> str``
    """

    with _lock:
        registered_server = _servers.get(server_name)

    def _handler(args: dict, **kwargs) -> str:
        request_timeout = _effective_tool_request_timeout(
            server_name,
            tool_name,
            args,
            tool_timeout,
        )
        bounded_search = _is_proof_auto_search(server_name, tool_name)
        deadline = time.monotonic() + request_timeout

        def remaining_timeout() -> float:
            if bounded_search:
                return _remaining_timeout(deadline)
            return request_timeout

        try:
            server = _resolve_handler_server(
                server_name,
                registered_server,
                timeout=remaining_timeout(),
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except TimeoutError as exc:
            return json.dumps({"error": _sanitize_error(f"MCP call failed: TimeoutError: {exc}")})
        if server is None:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})
        used_server = server
        timed_out = False

        def remember_server(current: MCPServerTask) -> None:
            nonlocal used_server
            used_server = current

        async def _operation(session: Any) -> str:
            result = await session.call_tool(tool_name, arguments=args)
            # MCP CallToolResult has .content (list of content blocks) and .isError
            if result.isError:
                error_text = ""
                for block in result.content or []:
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps(
                    {"error": _sanitize_error(error_text or "MCP tool returned an error")}
                )

            # Collect text from content blocks
            parts: list[str] = []
            for block in result.content or []:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return json.dumps({"result": "\n".join(parts) if parts else ""})

        try:
            return _run_server_operation(
                server_name,
                server,
                _operation,
                timeout=remaining_timeout(),
                observe_server=remember_server,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except TimeoutError as exc:
            timed_out = True
            logger.error(
                "MCP tool %s/%s call timed out: %s",
                server_name,
                tool_name,
                exc,
            )
            return json.dumps({"error": _sanitize_error(f"MCP call failed: TimeoutError: {exc}")})
        except Exception as exc:
            logger.error(
                "MCP tool %s/%s call failed: %s",
                server_name,
                tool_name,
                exc,
            )
            return json.dumps(
                {"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")}
            )
        finally:
            if (
                timed_out and _is_proof_auto_search(server_name, tool_name)
            ) or should_recycle_after_tool(server_name, tool_name):
                recycled = recycle_mcp_server(
                    server_name,
                    expected_server=used_server,
                )
                if recycled:
                    logger.info(
                        "Recycled MCP server '%s' after %s to bound research-mode "
                        "Lean worker retention",
                        server_name,
                        tool_name,
                    )

    return _handler


def _make_list_resources_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists resources from an MCP server."""

    with _lock:
        registered_server = _servers.get(server_name)

    def _handler(args: dict, **kwargs) -> str:
        try:
            server = _resolve_handler_server(
                server_name,
                registered_server,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        if server is None:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})

        async def _operation(session: Any) -> str:
            result = await session.list_resources()
            resources = []
            for r in result.resources if hasattr(result, "resources") else []:
                entry = {}
                if hasattr(r, "uri"):
                    entry["uri"] = str(r.uri)
                if hasattr(r, "name"):
                    entry["name"] = r.name
                if hasattr(r, "description") and r.description:
                    entry["description"] = r.description
                if hasattr(r, "mimeType") and r.mimeType:
                    entry["mimeType"] = r.mimeType
                resources.append(entry)
            return json.dumps({"resources": resources})

        try:
            return _run_server_operation(
                server_name,
                server,
                _operation,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except Exception as exc:
            logger.error(
                "MCP %s/list_resources failed: %s",
                server_name,
                exc,
            )
            return json.dumps(
                {"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")}
            )

    return _handler


def _make_read_resource_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that reads a resource by URI from an MCP server."""

    with _lock:
        registered_server = _servers.get(server_name)

    def _handler(args: dict, **kwargs) -> str:
        try:
            server = _resolve_handler_server(
                server_name,
                registered_server,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        if server is None:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})

        uri = args.get("uri")
        if not uri:
            return json.dumps({"error": "Missing required parameter 'uri'"})

        async def _operation(session: Any) -> str:
            result = await session.read_resource(uri)
            # read_resource returns ReadResourceResult with .contents list
            parts: list[str] = []
            contents = result.contents if hasattr(result, "contents") else []
            for block in contents:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "blob"):
                    parts.append(f"[binary data, {len(block.blob)} bytes]")
            return json.dumps({"result": "\n".join(parts) if parts else ""})

        try:
            return _run_server_operation(
                server_name,
                server,
                _operation,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except Exception as exc:
            logger.error(
                "MCP %s/read_resource failed: %s",
                server_name,
                exc,
            )
            return json.dumps(
                {"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")}
            )

    return _handler


def _make_list_prompts_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists prompts from an MCP server."""

    with _lock:
        registered_server = _servers.get(server_name)

    def _handler(args: dict, **kwargs) -> str:
        try:
            server = _resolve_handler_server(
                server_name,
                registered_server,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        if server is None:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})

        async def _operation(session: Any) -> str:
            result = await session.list_prompts()
            prompts = []
            for p in result.prompts if hasattr(result, "prompts") else []:
                entry = {}
                if hasattr(p, "name"):
                    entry["name"] = p.name
                if hasattr(p, "description") and p.description:
                    entry["description"] = p.description
                if hasattr(p, "arguments") and p.arguments:
                    entry["arguments"] = [
                        {
                            "name": a.name,
                            **(
                                {"description": a.description}
                                if hasattr(a, "description") and a.description
                                else {}
                            ),
                            **({"required": a.required} if hasattr(a, "required") else {}),
                        }
                        for a in p.arguments
                    ]
                prompts.append(entry)
            return json.dumps({"prompts": prompts})

        try:
            return _run_server_operation(
                server_name,
                server,
                _operation,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except Exception as exc:
            logger.error(
                "MCP %s/list_prompts failed: %s",
                server_name,
                exc,
            )
            return json.dumps(
                {"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")}
            )

    return _handler


def _make_get_prompt_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that gets a prompt by name from an MCP server."""

    with _lock:
        registered_server = _servers.get(server_name)

    def _handler(args: dict, **kwargs) -> str:
        try:
            server = _resolve_handler_server(
                server_name,
                registered_server,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        if server is None:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})

        name = args.get("name")
        if not name:
            return json.dumps({"error": "Missing required parameter 'name'"})
        arguments = args.get("arguments", {})

        async def _operation(session: Any) -> str:
            result = await session.get_prompt(name, arguments=arguments)
            # GetPromptResult has .messages list
            messages = []
            for msg in result.messages if hasattr(result, "messages") else []:
                entry = {}
                if hasattr(msg, "role"):
                    entry["role"] = msg.role
                if hasattr(msg, "content"):
                    content = msg.content
                    if hasattr(content, "text"):
                        entry["content"] = content.text
                    elif isinstance(content, str):
                        entry["content"] = content
                    else:
                        entry["content"] = str(content)
                messages.append(entry)
            resp = {"messages": messages}
            if hasattr(result, "description") and result.description:
                resp["description"] = result.description
            return json.dumps(resp)

        try:
            return _run_server_operation(
                server_name,
                server,
                _operation,
                timeout=tool_timeout,
            )
        except _MCPServerRecyclePending as exc:
            return _recycle_pending_result(exc)
        except Exception as exc:
            logger.error(
                "MCP %s/get_prompt failed: %s",
                server_name,
                exc,
            )
            return json.dumps(
                {"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")}
            )

    return _handler


def _make_check_fn(server_name: str):
    """Return a check function that verifies the MCP connection is alive."""

    def _check() -> bool:
        with _lock:
            server = _servers.get(server_name)
        return server is not None and server.session is not None

    return _check


# ---------------------------------------------------------------------------
# Discovery & registration
# ---------------------------------------------------------------------------

# Schema conversion + utility-schema selection helpers live in
# tools/mcp_schema.py and are re-exported so callers/tests that resolve
# tools.mcp.mcp_tool.<name> keep working.  mcp_schema does NOT import mcp_tool, so
# this introduces no import cycle.
import shutil  # noqa: F401
import subprocess  # noqa: F401

from tools.mcp.mcp_sampling import _safe_numeric  # noqa: F401
from tools.mcp.mcp_schema import (  # noqa: E402,F401
    _UTILITY_CAPABILITY_METHODS,
    _build_utility_schemas,
    _convert_mcp_schema,
    _normalize_name_filter,
    _parse_boolish,
    _select_utility_schemas,
)
from tools.mcp.mcp_transport import (
    _CREDENTIAL_PATTERN,  # noqa: F401
    _LEAN_MODULE_PART_PATTERN,  # noqa: F401
    _LOCAL_LOOGLE_CONNECT_TIMEOUT,  # noqa: F401
    _LOOGLE_STALE_ARTIFACT_SCAN_LIMIT,  # noqa: F401
    _SAFE_ENV_KEYS,  # noqa: F401
    _prepend_path,  # noqa: F401
    _read_lean_toolchain_from_root,  # noqa: F401
    _truthy_env_value,  # noqa: F401
)


def _existing_tool_names() -> list[str]:
    """Return tool names for all currently connected servers."""
    names: list[str] = []
    for _sname, server in _servers.items():
        if hasattr(server, "_registered_tool_names"):
            names.extend(server._registered_tool_names)
            continue
        for mcp_tool in server._tools:
            schema = _convert_mcp_schema(server.name, mcp_tool)
            names.append(schema["name"])
    return names


async def _discover_and_register_server(name: str, config: dict) -> list[str]:
    """Connect to a single MCP server, discover tools, and register them.

    Also registers utility tools for MCP Resources and Prompts support
    (list_resources, read_resource, list_prompts, get_prompt).

    Returns list of registered tool names.
    """
    from tools.registry import registry
    from toolsets import create_custom_toolset

    connect_timeout = _effective_connect_timeout(name, config)
    server = await asyncio.wait_for(
        _connect_server(name, config),
        timeout=connect_timeout,
    )
    with _lock:
        shutting_down = _mcp_shutting_down
        if not shutting_down:
            _servers[name] = server
            if _starting_servers.get(name) is server:
                _starting_servers.pop(name, None)
    if shutting_down:
        try:
            await server.shutdown()
        except BaseException:
            # Preserve startup ownership when cleanup fails; the finalizer
            # will report the exact server name and can retry teardown.
            raise
        with _lock:
            if _starting_servers.get(name) is server:
                _starting_servers.pop(name, None)
        raise RuntimeError("MCP runtime shutdown began during server discovery")

    registered_names: list[str] = []
    toolset_name = f"mcp-{name}"

    # Selective tool loading: honour include/exclude lists from config.
    # Rules (matching issue #690 spec):
    #   tools.include — whitelist: only these tool names are registered
    #   tools.exclude — blacklist: all tools EXCEPT these are registered
    #   include takes precedence over exclude
    #   Neither set → register all tools (backward-compatible default)
    tools_filter = config.get("tools") or {}
    include_set = _normalize_name_filter(
        tools_filter.get("include"), f"mcp_servers.{name}.tools.include"
    )
    exclude_set = _normalize_name_filter(
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_set:
            return tool_name in include_set
        if exclude_set:
            return tool_name not in exclude_set
        return True

    for mcp_tool in server._tools:
        if not _should_register(mcp_tool.name):
            logger.debug(
                "MCP server '%s': skipping tool '%s' (filtered by config)", name, mcp_tool.name
            )
            continue
        schema = _convert_mcp_schema(name, mcp_tool)
        tool_name_prefixed = schema["name"]

        registry.register(
            name=tool_name_prefixed,
            toolset=toolset_name,
            schema=schema,
            handler=_make_tool_handler(name, mcp_tool.name, server.tool_timeout),
            check_fn=_make_check_fn(name),
            is_async=False,
            description=schema["description"],
        )
        registered_names.append(tool_name_prefixed)

    # Register MCP Resources & Prompts utility tools, filtered by config and
    # only when the server actually supports the corresponding capability.
    _handler_factories = {
        "list_resources": _make_list_resources_handler,
        "read_resource": _make_read_resource_handler,
        "list_prompts": _make_list_prompts_handler,
        "get_prompt": _make_get_prompt_handler,
    }
    check_fn = _make_check_fn(name)
    for entry in _select_utility_schemas(name, server, config):
        schema = entry["schema"]
        handler_key = entry["handler_key"]
        handler = _handler_factories[handler_key](name, server.tool_timeout)

        registry.register(
            name=schema["name"],
            toolset=toolset_name,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            is_async=False,
            description=schema["description"],
        )
        registered_names.append(schema["name"])

    server._registered_tool_names = list(registered_names)

    # Create a custom toolset so these tools are discoverable
    if registered_names:
        create_custom_toolset(
            name=toolset_name,
            description=f"MCP tools from {name} server",
            tools=registered_names,
        )

    transport_type = "HTTP" if "url" in config else "stdio"
    logger.info(
        "MCP server '%s' (%s): registered %d tool(s): %s",
        name,
        transport_type,
        len(registered_names),
        ", ".join(registered_names),
    )
    return registered_names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_mcp_tools(*, timeout: float | None = None) -> list[str]:
    """Entry point: load config, connect to MCP servers, register tools.

    Called from ``model_tools._discover_tools()``. Safe to call even when
    the ``mcp`` package is not installed (returns empty list).

    Idempotent for already-connected servers. If some servers failed on a
    previous call, only the missing ones are retried.

    Returns:
        List of all registered MCP tool names.
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []

    servers = _load_mcp_config()
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    # Only attempt servers that aren't already connected and are enabled
    # (enabled: false skips the server entirely without removing its config)
    with _lock:
        new_servers = {
            k: v
            for k, v in servers.items()
            if k not in _servers and _parse_boolish(v.get("enabled", True), default=True)
        }

    if not new_servers:
        return _existing_tool_names()

    # Start the background event loop for MCP connections
    _ensure_mcp_loop()

    all_tools: list[str] = []
    failed_count = 0

    async def _discover_one(name: str, cfg: dict) -> list[str]:
        """Connect to a single server and return its registered tool names."""
        return await _discover_and_register_server(name, cfg)

    async def _discover_all():
        nonlocal failed_count
        server_names = list(new_servers.keys())
        # Connect to all servers in PARALLEL
        results = await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in new_servers.items()),
            return_exceptions=True,
        )
        for name, result in zip(server_names, results):
            if isinstance(result, Exception):
                failed_count += 1
                command = new_servers.get(name, {}).get("command")
                logger.warning(
                    "Failed to connect to MCP server '%s'%s: %s",
                    name,
                    f" (command={command})" if command else "",
                    _format_connect_error(result),
                )
            elif isinstance(result, list):
                all_tools.extend(result)
            else:
                failed_count += 1

    # Per-server timeouts are handled inside _discover_and_register_server.
    # The outer timeout must be at least as large as the slowest managed Lean
    # startup path, because first-run local Loogle indexing can take minutes.
    outer_timeout = max(
        120.0,
        *(float(_effective_connect_timeout(name, cfg)) for name, cfg in new_servers.items()),
    )
    bridge_timeout = outer_timeout + 5
    if timeout is not None:
        bridge_timeout = min(bridge_timeout, max(0.001, float(timeout)))
    _run_on_mcp_loop(_discover_all, timeout=bridge_timeout)

    # Print summary
    total_servers = len(new_servers)
    ok_servers = total_servers - failed_count
    if all_tools or failed_count:
        summary = f"  MCP: {len(all_tools)} tool(s) from {ok_servers} server(s)"
        if failed_count:
            summary += f" ({failed_count} failed)"
        logger.info(summary)

    # Return ALL registered tools (existing + newly discovered)
    return _existing_tool_names()


def get_mcp_status() -> list[dict]:
    """Return status of all configured MCP servers for banner display.

    Returns a list of dicts with keys: name, transport, tools, connected.
    Includes both successfully connected servers and configured-but-failed ones.
    """
    configured = _load_mcp_config()
    try:
        from leanflow_cli.cli.mcp_bootstrap import managed_mcp_server_status

        managed_status = managed_mcp_server_status()
    except Exception:
        managed_status = {}

    with _lock:
        active_servers = dict(_servers)

    result: list[dict] = []
    all_names = list(dict.fromkeys([*configured.keys(), *managed_status.keys()]))
    for name in all_names:
        cfg = configured.get(name, {})
        cfg = cfg if isinstance(cfg, dict) else {}
        managed = managed_status.get(name, {})
        transport = "http" if "url" in cfg else "stdio"
        server = active_servers.get(name)
        if server and server.session is not None:
            entry = {
                "name": name,
                "transport": transport,
                "tools": (
                    len(server._registered_tool_names)
                    if hasattr(server, "_registered_tool_names")
                    else len(server._tools)
                ),
                "connected": True,
            }
            if server._sampling:
                entry["sampling"] = dict(server._sampling.metrics)
        else:
            entry = {
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
            }
        role = str(cfg.get("role", "") or managed.get("role", "") or "")
        if role:
            entry["role"] = role
        if managed:
            entry["managed"] = bool(managed.get("managed", True))
            entry["installed"] = bool(managed.get("installed", False))
            entry["configured"] = bool(managed.get("configured", bool(cfg)))
            entry["healthy"] = bool(entry["connected"])
            entry["bootstrap_recommended"] = bool(managed.get("bootstrap_recommended", False))
            if isinstance(managed.get("power_modes"), dict):
                entry["power_modes"] = dict(managed.get("power_modes") or {})
        else:
            entry["managed"] = bool(cfg.get("managed", False))
            entry["configured"] = bool(cfg)
            entry["healthy"] = bool(entry["connected"])
        result.append(entry)

    return result


def shutdown_mcp_servers() -> tuple[str, ...]:
    """Close all MCP server connections and stop the background loop.

    Each server Task is signalled to exit its ``async with`` block so that
    the anyio cancel-scope cleanup happens in the same Task that opened it.
    All owned registered and starting servers are shut down in parallel. Return
    server names whose teardown failed while retaining their exact identities
    for a later retry. A bridge timeout raises because ownership is uncertain.
    """
    global _mcp_shutting_down

    deadline = time.monotonic() + 15.0
    with _lock:
        _mcp_shutting_down = True
        loop = _mcp_loop
        has_owned_servers = bool(_servers or _starting_servers)
        has_discovery = bool(_mcp_discovery_fences)

    # An unset discovery fence owns a possibly not-yet-registered startup, so
    # it participates in the shutdown path even before either server map does.
    if not has_owned_servers and not has_discovery:
        _stop_mcp_loop()
        with _lock:
            _mcp_shutting_down = False
        return ()

    if loop is None or not loop.is_running():
        with _lock:
            retained_names = tuple(
                sorted(
                    {
                        *_servers.keys(),
                        *_starting_servers.keys(),
                        *_mcp_discovery_fences.keys(),
                    }
                )
            )
        return retained_names

    async def _shutdown() -> list[tuple[str, BaseException]]:
        # Let already-submitted discovery wrappers observe the shutdown flag.
        # They either register startup ownership or finish their fence before
        # this snapshot is taken.
        await asyncio.sleep(0)
        with _lock:
            owned: list[tuple[str, MCPServerTask, bool, bool]] = []
            seen: set[int] = set()
            for name, server in [*_servers.items(), *_starting_servers.items()]:
                identity = id(server)
                if identity in seen:
                    continue
                seen.add(identity)
                owned.append(
                    (
                        name,
                        server,
                        _servers.get(name) is server,
                        _starting_servers.get(name) is server,
                    )
                )

        async def _close_owned(
            name: str,
            server: MCPServerTask,
            was_registered: bool,
            was_starting: bool,
        ) -> BaseException | None:
            retire_task = getattr(server, "_retire_task", None)
            if isinstance(retire_task, asyncio.Task) and not retire_task.done():
                # Final runtime shutdown supersedes the graceful request drain.
                # Cancel and join the retire task before touching transport
                # teardown so two cleanup paths never overlap.
                retire_task.cancel()
                with contextlib.suppress(BaseException):
                    await retire_task
            try:
                await server.shutdown()
            except BaseException as exc:
                with _lock:
                    if was_registered:
                        _servers.setdefault(name, server)
                    if was_starting:
                        _starting_servers.setdefault(name, server)
                    if not was_registered and not was_starting:
                        _servers.setdefault(name, server)
                return exc
            with _lock:
                if _servers.get(name) is server:
                    _servers.pop(name, None)
                if _starting_servers.get(name) is server:
                    _starting_servers.pop(name, None)
            return None

        results = await asyncio.gather(
            *(_close_owned(*entry) for entry in owned),
        )
        return [
            (name, result)
            for (name, _server, _registered, _starting), result in zip(owned, results)
            if isinstance(result, BaseException)
        ]

    future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
    try:
        shutdown_failures = future.result(timeout=_remaining_timeout(deadline))
    except TimeoutError as exc:
        future.cancel()
        raise RuntimeError(
            "MCP shutdown timed out; retained server and discovery ownership"
        ) from exc

    failures = {name for name, _exc in shutdown_failures}
    for name, cleanup_error in shutdown_failures:
        logger.warning("Error closing MCP server '%s': %s", name, cleanup_error)

    # The async wrapper sets each fence only after cancellation cleanup has
    # fully unwound. Never close the shared loop merely because its concurrent
    # Future entered the canceled state.
    while True:
        with _lock:
            pending_fences = list(_mcp_discovery_fences.items())
        if not pending_fences:
            break
        for name, fence in pending_fences:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not fence.wait(timeout=remaining):
                raise RuntimeError(f"MCP shutdown timed out waiting for startup cleanup: {name}")
            with _lock:
                if _mcp_discovery_fences.get(name) is fence:
                    _mcp_discovery_fences.pop(name, None)

    with _lock:
        failures.update(_servers)
        failures.update(_starting_servers)
        can_stop = not failures and not _mcp_discovery_fences

    if can_stop:
        _stop_mcp_loop()
        with _lock:
            _mcp_shutting_down = False
    return tuple(sorted(failures))


def _stop_mcp_loop():
    """Stop the background event loop and join its thread."""
    global _mcp_loop, _mcp_thread
    with _lock:
        loop = _mcp_loop
        thread = _mcp_thread
        _mcp_loop = None
        _mcp_thread = None
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        loop.close()
