"""Tests for campaign-scoped deterministic environment failure memory."""

from __future__ import annotations

import json
from types import SimpleNamespace

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import campaign_epoch, environment_memory


def _missing_sympy_result() -> str:
    return json.dumps(
        {
            "output": (
                "Traceback (most recent call last):\n"
                '  File "<stdin>", line 1, in <module>\n'
                "ModuleNotFoundError: No module named 'sympy'"
            ),
            "exit_code": 1,
            "error": None,
        }
    )


def test_extracts_only_exact_missing_python_modules():
    command = "python3 - <<'PY'\nfrom fractions import Fraction\nimport sympy as sp\nPY"

    assert environment_memory.python_interpreter(command) == "python3"
    assert environment_memory.imported_python_modules(command) == ("fractions", "sympy")
    assert environment_memory.missing_python_modules(_missing_sympy_result()) == ("sympy",)
    assert environment_memory.missing_python_modules("ordinary command failed") == ()


def test_environment_failure_survives_epoch_and_process_handoff(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-environment-memory")
    first_state: dict = {}
    campaign_epoch.ensure_campaign(first_state)

    observed = environment_memory.observe_terminal_result(
        first_state,
        function_name="terminal",
        args={"command": "python3 -c 'import sympy as sp'"},
        result=_missing_sympy_result(),
    )

    assert [entry["signature"] for entry in observed] == ["missing-python-module:python3:sympy"]
    assert environment_memory.blocked_imports(
        first_state, {"command": "python3 -c 'import sympy'"}
    ) == ("sympy",)
    handoff = campaign_epoch.roll_epoch(
        first_state,
        reason="context-pressure",
        cycle=12,
        target_symbol="demo",
        active_file="Main.lean",
    )
    assert "fresh epoch: 2" in handoff

    resumed_state: dict = {}
    campaign_epoch.ensure_campaign(resumed_state)
    entries = environment_memory.hydrate(resumed_state)

    assert entries[0]["signature"] == "missing-python-module:python3:sympy"
    prompt = environment_memory.prompt_block(resumed_state)
    assert "survive context/epoch rollover" in prompt
    assert "`python3` cannot import `sympy`" in prompt
    assert environment_memory.blocked_imports(
        resumed_state, {"command": "python3 - <<'PY'\nimport sympy\nPY"}
    ) == ("sympy",)
    # A different interpreter is not assumed to share python3's package set.
    assert (
        environment_memory.blocked_imports(resumed_state, {"command": "python -c 'import sympy'"})
        == ()
    )


def test_native_hook_records_failure_and_blocks_unchanged_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-native-environment-memory")
    monkeypatch.setattr(runner, "_workflow_kind", lambda: "prove")
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: False)
    monkeypatch.setattr(runner, "_sync_disabled_tools_from_result", lambda *_args: None)
    activities: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner,
        "_record_agent_activity",
        lambda _agent, event_type, message, **_details: activities.append((event_type, message)),
    )
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    appendices: list[str] = []
    agent = SimpleNamespace(
        _managed_autonomy_state=state,
        stage_tool_result_appendix=appendices.append,
    )

    runner._handle_managed_tool_result(
        agent,
        "terminal",
        {"command": "python3 -c 'import sympy as sp'"},
        _missing_sympy_result(),
    )

    assert state[environment_memory.ENVIRONMENT_FAILURES_KEY][0]["module"] == "sympy"
    assert appendices and "do not retry the unchanged import" in appendices[-1]
    assert activities[-1][0] == "campaign-environment-failure-recorded"

    blocked = runner._managed_pre_tool_call(
        agent,
        "terminal",
        {"command": "python3 - <<'PY'\nimport sympy as s\nPY"},
    )
    payload = json.loads(str(blocked))
    assert payload["success"] is False
    assert payload["blocked_by"] == "campaign_environment_memory"
    assert payload["modules"] == ["sympy"]
    assert activities[-1][0] == "campaign-environment-repeat-blocked"


def test_native_continuation_includes_environment_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(runner, "_runner_lean_prompt_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(
        runner, "route_workflow_step", lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {})
    )
    monkeypatch.setattr(
        runner, "_document_formalization_organization_phase_active", lambda *_args: False
    )
    monkeypatch.setattr(runner, "_queue_needs_final_file_sweep", lambda *_args: False)
    monkeypatch.setattr(runner, "_queue_assignment_block", lambda *_args: "")
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(runner, "artifact_paths_block", lambda: "")
    monkeypatch.setattr(runner, "frontier_digest_block", lambda: "")
    monkeypatch.setattr(runner, "_rcp_prefix_cache_enabled", lambda: False)
    state = {
        environment_memory.ENVIRONMENT_FAILURES_KEY: [
            {
                "kind": "missing_python_module",
                "interpreter": "python3",
                "module": "sympy",
                "count": 1,
            }
        ]
    }

    prompt = runner._autonomous_continuation_prompt({}, 3, state)

    assert "[LEANFLOW CAMPAIGN ENVIRONMENT MEMORY]" in prompt
    assert "`python3` cannot import `sympy`" in prompt
