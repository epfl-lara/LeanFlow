"""Security and arithmetic regressions for the empirical compute surface."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from core.model_tools import get_tool_definitions
from core.runtime_modes import empirical_dispatch_worker_enabled, planner_empirical_lane
from tools.implementations.empirical_compute import (
    EMPIRICAL_COMPUTE_MAX_TIMEOUT_S,
    EMPIRICAL_COMPUTE_SCHEMA,
    empirical_compute_tool,
)


def _enable_empirical_worker(monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ARCHETYPE", "empirical")


def _payload(program: str, *, timeout_s: int = 3) -> dict[str, object]:
    return json.loads(empirical_compute_tool(program, timeout_s=timeout_s))


@pytest.mark.parametrize(
    ("worker", "scratch", "archetype", "expected"),
    [
        ("1", "1", "empirical", True),
        ("0", "1", "empirical", False),
        ("1", "0", "empirical", False),
        ("1", "1", "deep_search", False),
        ("1", "1", "negation_probe", False),
    ],
)
def test_compute_mode_requires_exact_empirical_scratch_worker(
    monkeypatch, worker, scratch, archetype, expected
):
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", worker)
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", scratch)
    monkeypatch.setenv("LEANFLOW_DISPATCH_ARCHETYPE", archetype)

    assert empirical_dispatch_worker_enabled() is expected


def test_tool_is_exposed_only_to_empirical_worker(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ARCHETYPE", "deep_search")
    deep_tools = get_tool_definitions(["empirical-compute"], quiet_mode=True)
    _enable_empirical_worker(monkeypatch)
    empirical_tools = get_tool_definitions(["empirical-compute"], quiet_mode=True)

    assert deep_tools == []
    assert [item["function"]["name"] for item in empirical_tools] == ["empirical_compute"]


def test_tool_is_exposed_inside_synchronous_planner_empirical_context(monkeypatch):
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.delenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", raising=False)
    monkeypatch.delenv("LEANFLOW_DISPATCH_ARCHETYPE", raising=False)

    assert get_tool_definitions(["empirical-compute"], quiet_mode=True) == []
    with planner_empirical_lane():
        tools = get_tool_definitions(["empirical-compute"], quiet_mode=True)
        result = _payload("print(2 + 2)")
    assert [item["function"]["name"] for item in tools] == ["empirical_compute"]
    assert result["success"] is True
    assert result["output"] == "4\n"
    assert get_tool_definitions(["empirical-compute"], quiet_mode=True) == []


def test_erdos_fraction_and_factor_computation_runs_exactly(monkeypatch):
    _enable_empirical_worker(monkeypatch)
    result = _payload("""from fractions import Fraction
from math import gcd, isqrt

def divisors_of_square(q):
    factors = []
    d = 2
    while d * d <= q:
        exponent = 0
        while q % d == 0:
            q //= d
            exponent += 1
        if exponent:
            factors.append((d, 2 * exponent))
        d += 1 if d == 2 else 2
    if q > 1:
        factors.append((q, 2))
    divisors = [1]
    for p, exponent in factors:
        divisors = [x * p ** i for x in divisors for i in range(exponent + 1)]
    return divisors

for residue in [2, 4]:
    for s in range(3):
        if residue == 2:
            n = 840 * s + 457
            x = 224 * s + 122
            y = 4 * n
            z = 4 * n * (112 * s + 61)
        else:
            n = 840 * s + 793
            x = 224 * s + 212
            y = 4 * n
            z = 2 * n * (56 * s + 53)
        print(residue, s, Fraction(4, n) == Fraction(1, x) + Fraction(1, y) + Fraction(1, z))

print(gcd(168, 61), isqrt(61 * 61), len(divisors_of_square(45)))
""")

    assert result["success"] is True
    assert result["status"] == "empirical_compute_ok"
    assert result["output"] == (
        "2 0 True\n2 1 True\n2 2 True\n" "4 0 True\n4 1 True\n4 2 True\n1 61 15\n"
    )
    assert result["process_isolated"] is True
    assert result["project_mutation_authority"] is False


@pytest.mark.parametrize(
    "program",
    [
        "open('Main.lean', 'w').write('sorry')",
        "import os\nos.unlink('Main.lean')",
        "from pathlib import Path\nPath('Main.lean').rename('Moved.lean')",
        "__import__('subprocess').run(['touch', 'owned'])",
        'eval("1 + 1")',
    ],
)
def test_filesystem_process_and_dynamic_execution_are_denied(monkeypatch, tmp_path, program):
    _enable_empirical_worker(monkeypatch)
    project_file = tmp_path / "Main.lean"
    project_file.write_text("theorem stable : True := by trivial\n", encoding="utf-8")
    before = hashlib.sha256(project_file.read_bytes()).hexdigest()

    result = _payload(program)

    assert result["success"] is False
    assert result["status"] == "empirical_compute_denied"
    assert result["error"]
    assert hashlib.sha256(project_file.read_bytes()).hexdigest() == before
    assert not (tmp_path / "Moved.lean").exists()
    assert not (tmp_path / "owned").exists()


def test_infinite_computation_is_killed_at_hard_timeout(monkeypatch):
    _enable_empirical_worker(monkeypatch)
    started = time.monotonic()

    result = _payload("while True:\n    pass\n", timeout_s=1)

    assert result["success"] is False
    assert result["status"] == "empirical_compute_timeout"
    assert result["process_reaped"] is True
    assert time.monotonic() - started < 4


def test_output_is_bounded_in_the_child(monkeypatch):
    _enable_empirical_worker(monkeypatch)

    result = _payload("for _ in range(40000):\n    print('evidence')\n")

    assert result["success"] is False
    assert result["status"] == "empirical_compute_output_limit"
    assert len(str(result["output"]).encode("utf-8")) <= 32 * 1024


def test_schema_has_no_background_pty_or_filesystem_surface():
    properties = EMPIRICAL_COMPUTE_SCHEMA["parameters"]["properties"]

    assert set(properties) == {"program", "timeout_s"}
    assert properties["timeout_s"]["maximum"] == EMPIRICAL_COMPUTE_MAX_TIMEOUT_S


def test_direct_call_outside_empirical_worker_is_structured_denial(monkeypatch):
    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
    monkeypatch.delenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", raising=False)
    monkeypatch.delenv("LEANFLOW_DISPATCH_ARCHETYPE", raising=False)

    result = _payload("print(2 + 2)")

    assert result == {
        "success": False,
        "status": "empirical_compute_denied",
        "output": "",
        "error": ("empirical_compute is available only inside an isolated empirical planner actor"),
    }
