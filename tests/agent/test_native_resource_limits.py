"""Bound real native processes with sampled, identity-bound aggregate memory limits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.runtime.harness import discovery, resources, runner
from agent.runtime.harness.commands import child_environment
from agent.runtime.harness.models import NativeCleanupUnverified, NativeLaunchFailure
from agent.runtime.harness.processes import ProcessTree
from tests.agent.test_native_harness import fake_backend as fake_backend


def test_native_memory_limit_stops_real_root_and_descendant_preserving_usage(
    fake_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ready = tmp_path / "descendant-ready"
    native_ready = tmp_path / "native-ready"
    child = (
        "import os,time; from pathlib import Path; allocation=bytearray(1024*1024); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(2)"
    )
    request = fake_backend(
        "import json,subprocess,time\n"
        f"child=subprocess.Popen([{sys.executable!r}, '-c', {child!r}])\n"
        f"while not __import__('pathlib').Path({str(ready)!r}).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'thread.started','thread_id':'memory-session'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':3}}),flush=True)\n"
        f"__import__('pathlib').Path({str(native_ready)!r}).touch()\n"
        "child.wait()\n"
    )
    # A tiny injected byte ceiling exercises actual RSS without allocating gigabytes.
    monkeypatch.setattr(runner, "native_memory_limit_bytes", lambda env: 1, raising=False)
    starts: list[bool] = []

    def admitted() -> None:
        starts.append(True)
        deadline = time.monotonic() + 5
        while not native_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert native_ready.exists()

    result = runner.run_harness(replace(request, on_started=admitted))
    assert result.status == "failed"
    assert "memory limit" in result.error
    assert result.cleanup_verified
    assert result.session_id == "memory-session"
    assert result.usage == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    assert starts == [True]
    assert result.peak_owned_rss_bytes > 1
    assert result.max_owned_rss_bytes == 1
    assert json.loads(ready.read_text()) > 0


@pytest.mark.parametrize(
    ("environment", "gib"),
    [
        ({}, 16),
        ({"LEANFLOW_SETUP_MAX_RSS_GIB": "20"}, 20),
        ({"LEANFLOW_NATIVE_MAX_RSS_GIB": "1"}, 1),
        ({"LEANFLOW_NATIVE_MAX_RSS_GIB": "20", "LEANFLOW_SETUP_MAX_RSS_GIB": "invalid"}, 20),
        ({"LEANFLOW_NATIVE_MAX_RSS_GIB": "16", "LEANFLOW_SETUP_MAX_RSS_GIB": "20"}, 16),
    ],
)
def test_native_cap_resolves_native_then_setup_fallback_then_default(environment, gib):
    assert resources.native_memory_limit_bytes(environment) == gib * 1024**3


@pytest.mark.parametrize("raw", [True, False, 16, 1.0, None])
def test_native_cap_rejects_nonstring_environment_values(raw):
    with pytest.raises(runner.BackendError, match="whole number"):
        resources.native_memory_limit_bytes({"LEANFLOW_NATIVE_MAX_RSS_GIB": raw})


@pytest.mark.parametrize("raw", ["", "0", "-1", "21", "50", "1.5", "nan", "inf", "true"])
@pytest.mark.parametrize("key", ["LEANFLOW_NATIVE_MAX_RSS_GIB", "LEANFLOW_SETUP_MAX_RSS_GIB"])
def test_invalid_native_limit_never_launches_helper_or_author(
    fake_backend, monkeypatch: pytest.MonkeyPatch, key: str, raw: str
):
    request = fake_backend("raise AssertionError('must not launch')\n")
    monkeypatch.delenv("LEANFLOW_NATIVE_MAX_RSS_GIB", raising=False)
    monkeypatch.delenv("LEANFLOW_SETUP_MAX_RSS_GIB", raising=False)
    popen = Mock(side_effect=AssertionError("must not launch even MCP discovery"))
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    admitted = Mock()
    with pytest.raises(NativeLaunchFailure, match="whole number from 1 to 20") as caught:
        runner.run_harness(replace(request, env={key: raw}, on_admitting=admitted))
    assert caught.value.result.cleanup_verified
    assert caught.value.result.status == "startup_error"
    popen.assert_not_called()
    admitted.assert_not_called()


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_request_cap_environment_overrides_inheritance_without_changing_auth(
    fake_backend, monkeypatch: pytest.MonkeyPatch, backend: str
):
    monkeypatch.setenv("LEANFLOW_NATIVE_MAX_RSS_GIB", "18")
    monkeypatch.setenv("LEANFLOW_SETUP_MAX_RSS_GIB", "20")
    monkeypatch.setenv("CODEX_HOME", "/native/auth/location")
    request = fake_backend("print('unused')\n", backend=backend)
    inherited = child_environment(request)
    assert resources.native_memory_limit_bytes(inherited) == 18 * 1024**3
    override = child_environment(replace(request, env={"LEANFLOW_NATIVE_MAX_RSS_GIB": "16"}))
    assert resources.native_memory_limit_bytes(override) == 16 * 1024**3
    assert override["CODEX_HOME"] == inherited["CODEX_HOME"] == "/native/auth/location"
    monkeypatch.delenv("LEANFLOW_NATIVE_MAX_RSS_GIB")
    assert resources.native_memory_limit_bytes(child_environment(request)) == 20 * 1024**3


def _memory_table(monkeypatch: pytest.MonkeyPatch, output: str, *, reaped: bool = False):
    """Keep a cached original identity while supplying a controlled second process table."""
    start = "Wed Oct 7 12:00:00 2026"
    process = SimpleNamespace(pid=101, returncode=0 if reaped else None)
    process.poll = Mock(side_effect=AssertionError("RSS inspection must not reap"))
    tree = ProcessTree(101, {202: start, 303: "older child"}, process=process)
    tree.root_identity = start
    monkeypatch.setattr(tree, "refresh", Mock())
    monkeypatch.setattr(
        resources.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=output))
    )
    return process, tree


def test_memory_counts_matching_root_and_descendant_but_not_reused_or_foreign_ids(monkeypatch):
    process, tree = _memory_table(
        monkeypatch,
        "101 1 Wed Oct 7 12:00:00 2026\n"
        "202 2 Wed Oct 7 12:00:00 2026\n"
        "303 999 Wed Oct 7 12:00:00 2026\n"
        "404 999 Wed Oct 7 12:00:00 2026\n",
    )
    assert resources.owned_rss_bytes(process, tree) == 3 * 1024
    process.poll.assert_not_called()


def test_memory_never_adopts_reused_root_after_cached_reaping(monkeypatch):
    process, tree = _memory_table(
        monkeypatch,
        "101 999999 Thu Oct 8 12:00:00 2026\n" "202 2 Wed Oct 7 12:00:00 2026\n",
        reaped=True,
    )
    assert resources.owned_rss_bytes(process, tree) == 2 * 1024
    process.poll.assert_not_called()


def test_memory_refuses_an_unbound_or_different_original_popen():
    process = SimpleNamespace(pid=101, returncode=None)
    for tree in (ProcessTree(101), ProcessTree(101, process=SimpleNamespace(pid=101))):
        with pytest.raises(ValueError, match="original bound root Popen"):
            resources.owned_rss_bytes(process, tree)


def test_setup_rss_measurement_is_the_shared_lower_layer_implementation():
    from leanflow_cli.workflows.project_setup.execution import owned_rss_bytes

    assert owned_rss_bytes is resources.owned_rss_bytes


def test_observed_descendants_do_not_add_controller_or_reused_or_foreign_ids(monkeypatch):
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                stdout="101 999999 Wed Oct 7 12:00:00 2026\n"
                "202 2 Wed Oct 7 12:00:00 2026\n"
                "303 999999 Wed Oct 7 12:00:00 2026\n"
                "404 999999 Wed Oct 7 12:00:00 2026\n"
            )
        ),
    )
    observed = {202: "Wed Oct  7 12:00:00 2026", 303: "older child"}
    assert resources.observed_descendant_rss_bytes(observed) == 2 * 1024
    assert observed == {202: "Wed Oct  7 12:00:00 2026", 303: "older child"}


@pytest.mark.parametrize("output", ["", "malformed row\n", "101 1 Wed Oct 7 12:00:00 2026\nbad\n"])
@pytest.mark.parametrize("identities", [{}, {202: "Wed Oct 7 12:00:00 2026"}])
def test_observed_descendant_sampling_never_masks_invalid_metadata_as_zero(
    monkeypatch, output, identities
):
    monkeypatch.setattr(
        resources.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=output))
    )
    with pytest.raises(ValueError, match="Native memory measurement"):
        resources.observed_descendant_rss_bytes(identities)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "   ",
        "malformed row\n",
        "202 2 Wed Oct 7 12:00:00 2026\n",
        "101 1 Thu Oct 8 12:00:00 2026\n",
        "101 -1 Wed Oct 7 12:00:00 2026\n",
        "101 1 Wed Oct 7 12:00:00 2026\n101 2 Wed Oct 7 12:00:00 2026\n",
        "101 1 Wed Oct 7 12:00:00 2026\nmalformed unrelated row\n",
    ],
)
def test_missing_malformed_or_reused_root_metadata_never_establishes_zero_rss(monkeypatch, output):
    process, tree = _memory_table(monkeypatch, output)
    with pytest.raises(ValueError, match="Native memory measurement"):
        resources.owned_rss_bytes(process, tree)


def test_sampler_recovers_transient_gaps_and_resets_five_failure_bound(monkeypatch):
    measure = Mock(side_effect=[OSError(), ValueError(), 1024, *([ValueError()] * 5)])
    monkeypatch.setattr(resources, "owned_rss_bytes", measure)
    sampler = resources.NativeResourceSampler(Mock(), Mock(), 2048)
    assert [sampler.sample() for _ in range(7)] == [False, False, True, False, False, False, False]
    with pytest.raises(resources.NativeResourceUnavailable, match="bounded retries"):
        sampler.sample()
    assert sampler.peak_owned_rss_bytes == 1024
    assert measure.call_count == 8


@pytest.mark.parametrize("error", [OSError(), subprocess.TimeoutExpired("ps", 2), ValueError()])
def test_real_native_stops_after_five_measurement_gaps_with_verified_cleanup(
    fake_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error
):
    ready = tmp_path / "ready"
    request = fake_backend(
        "import json,time\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':9,'output_tokens':0}}),flush=True)\n"
        f"__import__('pathlib').Path({str(ready)!r}).touch()\n"
        "time.sleep(5)\n"
    )
    original = resources.owned_rss_bytes
    calls = []

    def fail_main(process, tree):
        if "mcp" in process.args:
            return original(process, tree)
        calls.append(process.pid)
        raise error

    monkeypatch.setattr(resources, "owned_rss_bytes", fail_main)
    started = time.monotonic()
    result = runner.run_harness(request)
    assert result.status == "failed"
    assert "metadata is unavailable after bounded retries" in result.error
    assert result.cleanup_verified
    assert result.usage_total["input_tokens"] == 9
    assert len(calls) == 5
    assert time.monotonic() - started < 5


def test_resource_failure_does_not_claim_cleanup_if_independent_cleanup_fails(
    fake_backend, monkeypatch: pytest.MonkeyPatch
):
    request = fake_backend("import time; time.sleep(5)\n")
    monkeypatch.setattr(runner, "native_memory_limit_bytes", lambda env: 1)
    original = runner.cleanup_group

    def fail_after_stop(process, tree):
        original(process, tree)
        raise NativeCleanupUnverified("retain uncertain inputs")

    monkeypatch.setattr(runner, "cleanup_group", fail_after_stop)
    with pytest.raises(NativeCleanupUnverified) as caught:
        runner.run_harness(request)
    assert caught.value.result.status == "cleanup_unverified"
    assert caught.value.result.cleanup_verified is False
    assert caught.value.result.peak_owned_rss_bytes > 1
    assert caught.value.result.max_owned_rss_bytes == 1


def test_native_discovery_helper_has_the_same_memory_ceiling(
    fake_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = fake_backend("raise AssertionError('author must not launch')\n")
    executable = tmp_path / "expensive-mcp-list"
    executable.write_text(f"#!{sys.executable}\nimport time; time.sleep(5)\n")
    executable.chmod(0o700)
    monkeypatch.setattr(discovery, "native_memory_limit_bytes", lambda env: 1)
    admitted = Mock()
    with pytest.raises(NativeLaunchFailure, match="memory limit") as caught:
        runner.run_harness(replace(request, executable=str(executable), on_started=admitted))
    assert caught.value.result.cleanup_verified
    admitted.assert_not_called()


def test_discovery_preserves_original_group_pid_until_owned_cleanup(tmp_path, monkeypatch):
    from agent.runtime.harness.models import HarnessRequest

    observed_returncodes = []
    original = discovery.cleanup_group

    def observe(process, tree):
        observed_returncodes.append(process.returncode)
        return original(process, tree)

    monkeypatch.setattr(discovery, "cleanup_group", observe)
    request = HarnessRequest("codex", "unused", tmp_path)
    assert (
        discovery.discover_mcp_servers(
            request, [sys.executable, "-c", "print('[]', flush=True)"], os.environ
        )
        == {}
    )
    assert observed_returncodes == [None]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_discovery_bounds_combined_output_before_accepting_metadata(tmp_path, monkeypatch, stream):
    from agent.runtime.harness.models import HarnessRequest

    monkeypatch.setattr(discovery, "MAX_OUTPUT_BYTES", 128)
    request = HarnessRequest("codex", "unused", tmp_path)
    command = [
        sys.executable,
        "-c",
        f"import sys; print('x'*1024, file=sys.{stream}, flush=True); print('[]',flush=True)",
    ]
    with pytest.raises(runner.BackendError, match="output limit"):
        discovery.discover_mcp_servers(request, command, os.environ)


def test_discovery_has_a_finite_helper_deadline_without_campaign_deadline(tmp_path, monkeypatch):
    from agent.runtime.harness.models import HarnessRequest

    monkeypatch.setattr(discovery, "DISCOVERY_TIMEOUT_S", 0.05)
    request = HarnessRequest("codex", "unused", tmp_path)
    started = time.monotonic()
    with pytest.raises(runner.BackendError, match="exceeded its deadline"):
        discovery.discover_mcp_servers(
            request, [sys.executable, "-c", "import time; time.sleep(5)"], os.environ
        )
    assert time.monotonic() - started < 3


def test_discovery_respects_already_expired_campaign_deadline(tmp_path):
    from agent.runtime.harness.models import HarnessRequest

    request = HarnessRequest("codex", "unused", tmp_path, deadline=0)
    with pytest.raises(runner.BackendError, match="exceeded its deadline"):
        discovery.discover_mcp_servers(
            request, [sys.executable, "-c", "import time; time.sleep(5)"], os.environ
        )


def test_discovery_stops_child_pipe_writer_after_fast_leader_exit(tmp_path):
    from agent.runtime.harness.models import HarnessRequest

    command = [
        sys.executable,
        "-c",
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']); "
        "print('[]',flush=True)",
    ]
    started = time.monotonic()
    assert (
        discovery.discover_mcp_servers(
            HarnessRequest("codex", "unused", tmp_path), command, os.environ
        )
        == {}
    )
    assert time.monotonic() - started < 3
