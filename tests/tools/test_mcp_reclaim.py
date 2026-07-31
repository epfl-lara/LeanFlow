"""Research-mode MCP reclamation policy and lifecycle tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp.mcp_reclaim import (
    RESEARCH_MODE_ENV,
    RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV,
    RESEARCH_STATEFUL_LEAN_LSP_RECYCLE_ENV,
    should_recycle_after_tool,
)


def test_research_recycles_stateful_managed_lean_lsp_calls() -> None:
    """Retire stateful LSP workers while keeping stateless search warm."""
    env = {RESEARCH_MODE_ENV: "1"}

    assert should_recycle_after_tool("lean-lsp", "lean_multi_attempt", environ=env)
    assert should_recycle_after_tool("lean-lsp", "lean_goal", environ=env)
    assert should_recycle_after_tool("lean-lsp", "lean_diagnostic_messages", environ=env)
    assert not should_recycle_after_tool("lean-lsp", "lean_loogle", environ=env)
    assert not should_recycle_after_tool("other", "lean_multi_attempt", environ=env)
    assert not should_recycle_after_tool("lean-lsp", "lean_multi_attempt", environ={})


@pytest.mark.parametrize("disabled", ["0", "false", "off"])
def test_research_stateful_lsp_recycle_has_explicit_benchmark_opt_out(
    disabled: str,
) -> None:
    """Allow controlled benchmarks to retain stateful LSP workers."""
    env = {
        RESEARCH_MODE_ENV: "1",
        RESEARCH_STATEFUL_LEAN_LSP_RECYCLE_ENV: disabled,
    }

    assert not should_recycle_after_tool("lean-lsp", "lean_goal", environ=env)
    assert should_recycle_after_tool("lean-lsp", "lean_multi_attempt", environ=env)


@pytest.mark.parametrize("disabled", ["0", "false", "off"])
def test_research_multi_attempt_recycle_has_explicit_benchmark_opt_out(
    disabled: str,
) -> None:
    """Allow controlled short-run benchmarks to retain the warmed server."""
    env = {
        RESEARCH_MODE_ENV: "1",
        RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV: disabled,
    }

    assert not should_recycle_after_tool("lean-lsp", "lean_multi_attempt", environ=env)


def test_multi_attempt_handler_preserves_result_then_retires_server(monkeypatch) -> None:
    """Return all tactic evidence before reclaiming the heavy server tree."""
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setenv(RESEARCH_MODE_ENV, "1")
    monkeypatch.delenv(RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV, raising=False)
    session = MagicMock()
    result_payload = '{"items":[{"snippet":"ring","goals":[]}]}'
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text=result_payload)],
            isError=False,
        )
    )
    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = session
    server._tools = [MagicMock()]
    server._sampling = MagicMock()
    server._config = {"command": "lean-lsp-mcp"}
    other_server = mcp_tool.MCPServerTask("lean-proof-auto")
    other_server.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = server
    mcp_tool._servers["lean-proof-auto"] = other_server

    mcp_tool._ensure_mcp_loop()
    try:
        handler = mcp_tool._make_tool_handler("lean-lsp", "lean_multi_attempt", 120)
        returned = json.loads(handler({"snippets": ["ring", "omega"]}))

        assert returned == {"result": result_payload}
        session.call_tool.assert_awaited_once_with(
            "lean_multi_attempt",
            arguments={"snippets": ["ring", "omega"]},
        )
        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-lsp" not in mcp_tool._servers
        assert server.session is None
        assert server._task is None
        assert server._sampling is None
        assert server._tools == []
        assert server._config == {}
        assert mcp_tool._servers["lean-proof-auto"] is other_server
        assert other_server.session is not None
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._servers.pop("lean-proof-auto", None)
        mcp_tool._stop_mcp_loop()


def test_failed_multi_attempt_still_retires_memory_heavy_server(monkeypatch) -> None:
    """Reclaim a wedged backend even when its mathematical call reports an error."""
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setenv(RESEARCH_MODE_ENV, "1")
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="backend timeout")],
            isError=True,
        )
    )
    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = session
    mcp_tool._servers["lean-lsp"] = server

    mcp_tool._ensure_mcp_loop()
    try:
        handler = mcp_tool._make_tool_handler("lean-lsp", "lean_multi_attempt", 120)
        returned = json.loads(handler({"snippets": ["ring", "omega"]}))

        assert returned == {"error": "backend timeout"}
        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-lsp" not in mcp_tool._servers
        assert server.session is None
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_timed_out_proof_auto_search_retires_its_live_server() -> None:
    """Cancel the bounded request and reclaim its exact proof-auto process tree."""
    import tools.mcp.mcp_tool as mcp_tool

    started = threading.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=never_finishes)
    server = mcp_tool.MCPServerTask("lean-proof-auto")
    server.session = session
    mcp_tool._servers["lean-proof-auto"] = server

    mcp_tool._ensure_mcp_loop()
    try:
        handler = mcp_tool._make_tool_handler("lean-proof-auto", "search_automated_proof", 600)
        payload = json.loads(handler({"search_budget_s": 0.05}))

        assert started.is_set()
        assert "TimeoutError" in payload["error"]
        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-proof-auto" not in mcp_tool._servers
        assert server.session is None
    finally:
        mcp_tool._servers.pop("lean-proof-auto", None)
        mcp_tool._stop_mcp_loop()


def test_retirement_drains_existing_request_and_rejects_new_work() -> None:
    """Bound retirement without cutting off evidence admitted before its boundary."""
    from tools.mcp.mcp_tool import MCPServerTask

    async def exercise() -> None:
        server = MCPServerTask("lean-lsp")
        server.session = MagicMock()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_request(_session):
            started.set()
            await release.wait()
            return "complete"

        active = asyncio.create_task(server.request(slow_request))
        await started.wait()
        retirement = asyncio.create_task(server.retire())
        await asyncio.sleep(0)

        assert not retirement.done()
        with pytest.raises(RuntimeError, match="recycling"):
            await server.request(lambda _session: asyncio.sleep(0, result="new"))

        release.set()
        assert await active == "complete"
        await retirement
        assert server.session is None

    asyncio.run(exercise())


def test_concurrent_registered_handler_finishes_before_multi_attempt_recycle(
    monkeypatch,
) -> None:
    """A long peer tool call survives retirement triggered by multi-attempt."""
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setenv(RESEARCH_MODE_ENV, "1")
    multi_started = threading.Event()
    goal_started = threading.Event()
    release_multi = threading.Event()
    release_goal = threading.Event()

    async def call_tool(tool_name, arguments):
        if tool_name == "lean_multi_attempt":
            multi_started.set()
            while not release_multi.is_set():
                await asyncio.sleep(0.005)
            return SimpleNamespace(
                content=[SimpleNamespace(text='{"items":[]}')],
                isError=False,
            )
        goal_started.set()
        while not release_goal.is_set():
            await asyncio.sleep(0.005)
        return SimpleNamespace(
            content=[SimpleNamespace(text='{"goals":["G"]}')],
            isError=False,
        )

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=call_tool)
    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = session
    mcp_tool._servers["lean-lsp"] = server
    multi_handler = mcp_tool._make_tool_handler("lean-lsp", "lean_multi_attempt", 120)
    goal_handler = mcp_tool._make_tool_handler("lean-lsp", "lean_goal", 120)
    mcp_tool._ensure_mcp_loop()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            multi_future = pool.submit(multi_handler, {"snippets": ["ring", "omega"]})
            assert multi_started.wait(timeout=2.0)
            goal_future = pool.submit(goal_handler, {"line": 10})
            assert goal_started.wait(timeout=2.0)

            release_multi.set()
            assert json.loads(multi_future.result(timeout=2.0)) == {"result": '{"items":[]}'}
            # Retirement is pending on the useful concurrent goal request; it
            # must not force-close that request after an arbitrary short grace.
            time.sleep(0.05)
            assert not goal_future.done()
            assert not server._recycle_complete.is_set()

            release_goal.set()
            assert json.loads(goal_future.result(timeout=2.0)) == {"result": '{"goals":["G"]}'}

        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-lsp" not in mcp_tool._servers
    finally:
        release_multi.set()
        release_goal.set()
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_preprobed_registered_handler_reconnects_after_recycle() -> None:
    """Do not turn a normal recycle race into a run-wide capability failure."""
    import tools.mcp.mcp_tool as mcp_tool

    old = mcp_tool.MCPServerTask("lean-lsp")
    old.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = old
    stale_handler = mcp_tool._make_tool_handler("lean-lsp", "lean_goal", 120)

    old._recycle_requested = True
    old._recycle_complete.set()
    old._recycle_finished.set()
    mcp_tool._servers.pop("lean-lsp", None)

    replacement_session = MagicMock()
    replacement_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text='{"goals":["fresh"]}')],
            isError=False,
        )
    )
    replacement = mcp_tool.MCPServerTask("lean-lsp")
    replacement.session = replacement_session

    def reconnect(_name, *, timeout, completion_event) -> None:
        assert timeout > 0
        mcp_tool._servers["lean-lsp"] = replacement
        completion_event.set()

    mcp_tool._ensure_mcp_loop()
    try:
        with patch(
            "tools.mcp.mcp_tool._discover_replacement_server",
            side_effect=reconnect,
        ):
            returned = json.loads(stale_handler({"line": 12}))

        assert returned == {"result": '{"goals":["fresh"]}'}
        replacement_session.call_tool.assert_awaited_once_with("lean_goal", arguments={"line": 12})
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_registered_handler_waits_for_active_recycle_then_reconnects() -> None:
    """Keep a concurrent native probe available across an active drain."""
    import tools.mcp.mcp_tool as mcp_tool

    first_started = threading.Event()
    release_first = threading.Event()

    async def old_call_tool(_tool_name, *, arguments):
        assert arguments == {"line": 10}
        first_started.set()
        while not release_first.is_set():
            await asyncio.sleep(0.005)
        return SimpleNamespace(
            content=[SimpleNamespace(text='{"goals":["old"]}')],
            isError=False,
        )

    old_session = MagicMock()
    old_session.call_tool = AsyncMock(side_effect=old_call_tool)
    old = mcp_tool.MCPServerTask("lean-lsp")
    old.session = old_session
    mcp_tool._servers["lean-lsp"] = old
    handler = mcp_tool._make_tool_handler("lean-lsp", "lean_goal", 120)

    replacement_session = MagicMock()
    replacement_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text='{"goals":["fresh"]}')],
            isError=False,
        )
    )
    replacement = mcp_tool.MCPServerTask("lean-lsp")
    replacement.session = replacement_session

    def reconnect(_name, *, timeout, completion_event) -> None:
        assert timeout > 0
        mcp_tool._servers["lean-lsp"] = replacement
        completion_event.set()

    mcp_tool._ensure_mcp_loop()
    try:
        with (
            patch(
                "tools.mcp.mcp_tool._discover_replacement_server",
                side_effect=reconnect,
            ),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            admitted = pool.submit(handler, {"line": 10})
            assert first_started.wait(timeout=2.0)
            assert mcp_tool.recycle_mcp_server("lean-lsp", expected_server=old)
            waiting = pool.submit(handler, {"line": 11})

            time.sleep(0.05)
            assert not waiting.done()
            assert not old._recycle_complete.is_set()

            release_first.set()
            assert json.loads(admitted.result(timeout=2.0)) == {"result": '{"goals":["old"]}'}
            assert json.loads(waiting.result(timeout=2.0)) == {"result": '{"goals":["fresh"]}'}

        assert old._recycle_complete.is_set()
        assert mcp_tool._servers["lean-lsp"] is replacement
        replacement_session.call_tool.assert_awaited_once_with(
            "lean_goal",
            arguments={"line": 11},
        )
    finally:
        release_first.set()
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_reconnected_multi_attempt_retires_the_replacement(monkeypatch) -> None:
    """Apply post-call policy to the server that actually ran a retried call."""
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setenv(RESEARCH_MODE_ENV, "1")
    old = mcp_tool.MCPServerTask("lean-lsp")
    old.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = old
    stale_handler = mcp_tool._make_tool_handler("lean-lsp", "lean_multi_attempt", 120)
    old._recycle_requested = True
    old._accepting_requests = False
    old._recycle_complete.set()
    old._recycle_finished.set()
    mcp_tool._servers.pop("lean-lsp", None)

    replacement_session = MagicMock()
    replacement_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text='{"items":[]}')],
            isError=False,
        )
    )
    replacement = mcp_tool.MCPServerTask("lean-lsp")
    replacement.session = replacement_session

    def reconnect(_name, *, timeout, completion_event) -> None:
        assert timeout > 0
        mcp_tool._servers["lean-lsp"] = replacement
        completion_event.set()

    mcp_tool._ensure_mcp_loop()
    try:
        with patch(
            "tools.mcp.mcp_tool._discover_replacement_server",
            side_effect=reconnect,
        ):
            returned = json.loads(stale_handler({"snippets": ["ring", "omega"]}))

        assert returned == {"result": '{"items":[]}'}
        assert replacement._recycle_complete.wait(timeout=2.0)
        assert replacement.session is None
        assert "lean-lsp" not in mcp_tool._servers
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_timed_out_request_releases_recycle_idle_barrier() -> None:
    """Cancel abandoned MCP coroutines so retirement cannot wait forever."""
    import tools.mcp.mcp_tool as mcp_tool

    started = threading.Event()
    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = server

    async def never_finishes(_session):
        started.set()
        await asyncio.Event().wait()

    mcp_tool._ensure_mcp_loop()
    try:
        with pytest.raises(TimeoutError):
            mcp_tool._run_server_operation(
                "lean-lsp",
                server,
                never_finishes,
                timeout=0.05,
            )
        assert started.is_set()
        deadline = time.monotonic() + 1.0
        while server._active_requests and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server._active_requests == 0

        assert mcp_tool.recycle_mcp_server("lean-lsp", expected_server=server)
        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-lsp" not in mcp_tool._servers
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_intercepted_operation_bridge_does_not_construct_coroutine() -> None:
    """Keep coroutine ownership lazy when a test or shim declines dispatch."""
    import tools.mcp.mcp_tool as mcp_tool

    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    operation = AsyncMock(return_value="unused")

    with patch("tools.mcp.mcp_tool._run_on_mcp_loop", return_value="intercepted") as bridge:
        assert (
            mcp_tool._run_server_operation(
                "lean-lsp",
                server,
                operation,
                timeout=3.0,
            )
            == "intercepted"
        )

    submitted = bridge.call_args.args[0]
    assert callable(submitted)
    operation.assert_not_awaited()


def test_intercepted_recycle_bridge_owns_a_lazy_schedule_factory() -> None:
    """Do not leak a schedule coroutine when dispatch is rejected before submission."""
    import tools.mcp.mcp_tool as mcp_tool

    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = server
    try:
        with patch("tools.mcp.mcp_tool._run_on_mcp_loop", return_value=False) as bridge:
            assert not mcp_tool.recycle_mcp_server("lean-lsp", expected_server=server)

        assert callable(bridge.call_args.args[0])
        assert server._retire_task is None
    finally:
        mcp_tool._servers.pop("lean-lsp", None)


def test_final_shutdown_rejects_late_recycle_schedule() -> None:
    """Keep the finalizer as the sole transport teardown owner."""
    import tools.mcp.mcp_tool as mcp_tool

    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = server
    with mcp_tool._lock:
        mcp_tool._mcp_shutting_down = True
    try:
        with patch("tools.mcp.mcp_tool._run_on_mcp_loop") as bridge:
            assert not mcp_tool.recycle_mcp_server("lean-lsp", expected_server=server)
        bridge.assert_not_called()
        assert server._retire_task is None
    finally:
        with mcp_tool._lock:
            mcp_tool._mcp_shutting_down = False
        mcp_tool._servers.pop("lean-lsp", None)


def test_final_shutdown_keeps_startup_gate_closed_through_loop_stop() -> None:
    """Linearize event-loop stop before discovery may construct another server."""
    import tools.mcp.mcp_tool as mcp_tool

    stop_entered = threading.Event()
    release_stop = threading.Event()
    original_stop = mcp_tool._stop_mcp_loop

    def blocked_stop() -> None:
        stop_entered.set()
        assert release_stop.wait(timeout=2.0)
        original_stop()

    mcp_tool._servers.clear()
    mcp_tool._starting_servers.clear()
    mcp_tool._mcp_discovery_fences.clear()
    mcp_tool._ensure_mcp_loop()
    try:
        with (
            patch("tools.mcp.mcp_tool._stop_mcp_loop", side_effect=blocked_stop),
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
        ):
            shutdown = pool.submit(mcp_tool.shutdown_mcp_servers)
            assert stop_entered.wait(timeout=2.0)
            with pytest.raises(RuntimeError, match="shutdown is in progress"):
                asyncio.run(
                    mcp_tool._connect_server(
                        "late-startup",
                        {"command": "unused"},
                    )
                )
            assert "late-startup" not in mcp_tool._starting_servers
            release_stop.set()
            assert shutdown.result(timeout=2.0) == ()
    finally:
        release_stop.set()
        with mcp_tool._lock:
            mcp_tool._mcp_shutting_down = False
        mcp_tool._starting_servers.pop("late-startup", None)
        original_stop()


def test_reconnect_consumes_only_original_tool_deadline() -> None:
    """Do not inherit discovery's long startup timeout after a recycle wait."""
    import tools.mcp.mcp_tool as mcp_tool

    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    server._recycle_requested = True
    server._accepting_requests = False
    server._recycle_complete.set()
    server._recycle_finished.set()
    operation = AsyncMock(return_value="unused")
    observed: list[float] = []

    def timed_out_replacement(_name, *, previous, timeout):
        assert previous is server
        observed.append(timeout)
        raise TimeoutError("bounded reconnect")

    mcp_tool._ensure_mcp_loop()
    started = time.monotonic()
    try:
        with patch(
            "tools.mcp.mcp_tool._replacement_mcp_server",
            side_effect=timed_out_replacement,
        ):
            with pytest.raises(mcp_tool._MCPServerRecyclePending, match="tool deadline"):
                mcp_tool._run_server_operation(
                    "lean-lsp",
                    server,
                    operation,
                    timeout=0.05,
                )
    finally:
        mcp_tool._stop_mcp_loop()

    assert observed and 0 < observed[0] <= 0.05
    assert time.monotonic() - started < 0.5
    operation.assert_not_awaited()


def test_lazy_discovery_caps_bridge_by_remaining_deadline() -> None:
    """Override the normal 125-second discovery floor for a waiting tool call."""
    import tools.mcp.mcp_tool as mcp_tool

    with (
        patch("tools.mcp.mcp_tool._servers", {}),
        patch("tools.mcp.mcp_tool._MCP_AVAILABLE", True),
        patch(
            "tools.mcp.mcp_tool._load_mcp_config",
            return_value={"bounded-reconnect": {"command": "unused"}},
        ),
        patch("tools.mcp.mcp_tool._ensure_mcp_loop"),
        patch("tools.mcp.mcp_tool._run_on_mcp_loop", return_value=None) as bridge,
    ):
        assert mcp_tool.discover_mcp_tools(timeout=0.05) == []

    assert callable(bridge.call_args.args[0])
    assert bridge.call_args.kwargs["timeout"] == pytest.approx(0.05)


def test_timed_out_startup_fences_retry_until_cancellation_cleanup_finishes() -> None:
    """Never overlap a replacement with the canceled startup's process cleanup."""
    import tools.mcp.mcp_tool as mcp_tool

    old = mcp_tool.MCPServerTask("lean-lsp")
    old._recycle_requested = True
    old._recycle_finished.set()
    startup_started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    calls = 0
    replacement = mcp_tool.MCPServerTask("lean-lsp")
    replacement.session = MagicMock()

    async def discover(name, _config):
        nonlocal calls
        assert name == "lean-lsp"
        calls += 1
        if calls == 1:
            startup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                while not release_cleanup.is_set():
                    await asyncio.sleep(0.005)
                raise
        with mcp_tool._lock:
            mcp_tool._servers[name] = replacement
        return []

    mcp_tool._servers.pop("lean-lsp", None)
    mcp_tool._mcp_discovery_fences.pop("lean-lsp", None)
    mcp_tool._ensure_mcp_loop()
    try:
        with (
            patch(
                "tools.mcp.mcp_tool._load_mcp_config",
                return_value={"lean-lsp": {"command": "unused"}},
            ),
            patch(
                "tools.mcp.mcp_tool._discover_and_register_server",
                side_effect=discover,
            ),
        ):
            with pytest.raises(TimeoutError):
                mcp_tool._replacement_mcp_server(
                    "lean-lsp",
                    previous=old,
                    timeout=0.05,
                )
            assert startup_started.is_set()
            assert cleanup_started.wait(timeout=1.0)

            with pytest.raises(TimeoutError, match="cleanup is still running"):
                mcp_tool._replacement_mcp_server(
                    "lean-lsp",
                    previous=old,
                    timeout=0.05,
                )
            assert calls == 1

            release_cleanup.set()
            with mcp_tool._lock:
                first_fence = mcp_tool._mcp_discovery_fences["lean-lsp"]
            assert first_fence.wait(timeout=2.0)

            assert (
                mcp_tool._replacement_mcp_server(
                    "lean-lsp",
                    previous=old,
                    timeout=1.0,
                )
                is replacement
            )
            assert calls == 2
    finally:
        release_cleanup.set()
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._starting_servers.pop("lean-lsp", None)
        mcp_tool._mcp_discovery_fences.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_retirement_failure_retains_ownership_and_blocks_replacement() -> None:
    """Never advertise completion while the old heavy process may be live."""
    import tools.mcp.mcp_tool as mcp_tool

    server = mcp_tool.MCPServerTask("lean-lsp")
    server.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = server
    handler = mcp_tool._make_tool_handler("lean-lsp", "lean_goal", 0.2)

    async def fail_retire(current):
        assert current is server
        raise RuntimeError("injected shutdown failure")

    mcp_tool._ensure_mcp_loop()
    try:
        with patch.object(mcp_tool.MCPServerTask, "retire", fail_retire):
            assert mcp_tool.recycle_mcp_server("lean-lsp", expected_server=server)
            assert server._recycle_finished.wait(timeout=2.0)

        assert not server._recycle_complete.is_set()
        assert isinstance(server._recycle_error, RuntimeError)
        assert mcp_tool._servers["lean-lsp"] is server
        assert server.session is not None
        with patch("tools.mcp.mcp_tool.discover_mcp_tools") as discover:
            payload = json.loads(handler({"line": 12}))
        assert "retained old server ownership" in payload["error"]
        assert payload["mcp_recycling"] is True
        assert payload["retryable"] is True
        assert payload["cleanup_failed"] is True
        discover.assert_not_called()

        # A later explicit recycle retries the same owned server instead of
        # spawning a replacement alongside it.
        assert mcp_tool.recycle_mcp_server("lean-lsp", expected_server=server)
        assert server._recycle_complete.wait(timeout=2.0)
        assert "lean-lsp" not in mcp_tool._servers
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
        mcp_tool._stop_mcp_loop()


def test_recycle_identity_guard_never_closes_replacement() -> None:
    """A delayed cleanup cannot retire a newly discovered server instance."""
    import tools.mcp.mcp_tool as mcp_tool

    old = mcp_tool.MCPServerTask("lean-lsp")
    replacement = mcp_tool.MCPServerTask("lean-lsp")
    replacement.session = MagicMock()
    mcp_tool._servers["lean-lsp"] = replacement
    try:
        assert not mcp_tool.recycle_mcp_server("lean-lsp", expected_server=old)
        assert mcp_tool._servers["lean-lsp"] is replacement
        assert replacement.session is not None
    finally:
        mcp_tool._servers.pop("lean-lsp", None)
