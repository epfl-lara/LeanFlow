"""Tests for campaign-scoped deterministic environment failure memory."""

from __future__ import annotations

import json

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
