"""Capability-contract regressions for the isolated empirical compute runtime.

The runtime applies process resource limits when it executes a program, so every
execution here goes through the same isolated subprocess the prover session and
the ``empirical_compute`` tool use. Validation-only checks call the validator
directly because it is pure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.utilities import empirical_compute_runtime as runtime

RUNTIME_PATH = Path(runtime.__file__).resolve()


def _run(program: str, tmp_path: Path) -> dict:
    process = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(RUNTIME_PATH), "--timeout-s", "3"],
        input=program,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
        timeout=20,
    )
    return json.loads(process.stdout)


def test_fraction_alias_import_is_supported(tmp_path):
    """Both planning contexts of the Beck–Fiala campaign were rejected on this alias."""
    result = _run("from fractions import Fraction as Q\nprint(Q(1, 3) + Q(1, 6))\n", tmp_path)

    assert result["status"] == "empirical_compute_ok", result
    assert result["output"] == "1/2\n"


def test_membership_and_bitwise_operators_are_supported(tmp_path):
    """``In`` and ``BitAnd`` were the next two rejected constructs after the alias."""
    result = _run(
        "print(3 in [1, 2, 3], 2 not in {1}, 5 & 3, 5 | 2, 5 ^ 1, 1 << 4, 32 >> 2, ~0, "
        "None is None)\n",
        tmp_path,
    )

    assert result["status"] == "empirical_compute_ok", result
    assert result["output"] == "True True 1 7 4 16 8 -1 True\n"


def test_math_and_itertools_helpers_are_preloaded_and_importable(tmp_path):
    result = _run(
        "import math\n"
        "import itertools as it\n"
        "from itertools import product\n"
        "from math import comb as choose\n"
        "print(math.comb(5, 2), choose(6, 3), factorial(5), perm(4, 2), "
        "len(list(it.combinations(range(4), 2))), sum(1 for _ in product((0, 1), repeat=3)))\n",
        tmp_path,
    )

    assert result["status"] == "empirical_compute_ok", result
    assert result["output"] == "10 20 120 12 6 8\n"


def test_lambda_dict_set_and_integer_methods_are_supported(tmp_path):
    result = _run(
        "pairs = sorted([(2, 'b'), (1, 'a')], key=lambda item: item[0])\n"
        "counts = {}\n"
        "for _, letter in pairs:\n"
        "    counts[letter] = counts.get(letter, 0) + 1\n"
        "seen = set()\n"
        "seen.add(3)\n"
        "print(pairs, sorted(counts.items()), seen.union({4}), (0b1011).bit_count(), "
        "Fraction(355, 113).limit_denominator(10), pow(2, 10, 1000), ', '.join(['a', 'b']))\n",
        tmp_path,
    )

    assert result["status"] == "empirical_compute_ok", result
    expected = "[(1, 'a'), (2, 'b')] [('a', 1), ('b', 1)] {3, 4} 3 22/7 24 a, b\n"
    assert result["output"] == expected


def test_program_bound_names_are_callable(tmp_path):
    """An alias, a stored lambda, or a loop variable holding a helper may be called."""
    result = _run(
        "from fractions import Fraction as Q\n"
        "square = lambda x: x * x\n"
        "for helper in (gcd, lcm):\n"
        "    print(helper(12, 18), end=' ')\n"
        "print(square(Q(1, 2)))\n",
        tmp_path,
    )

    assert result["status"] == "empirical_compute_ok", result
    assert result["output"] == "6 36 1/4\n"


@pytest.mark.parametrize(
    "example", runtime.capability_contract()["examples"], ids=lambda item: item["title"]
)
def test_every_contract_example_runs(example, tmp_path):
    """The examples shown to the model must be programs the validator accepts."""
    result = _run(example["program"], tmp_path)

    assert result["status"] == "empirical_compute_ok", result
    assert result["output"].strip()


def test_contract_names_agree_with_the_validator():
    contract = runtime.capability_contract()
    json.dumps(contract)
    for name in [*contract["preloaded"], *contract["builtins"]]:
        runtime.validate_empirical_program(f"{name}\n")
    for form in contract["import_forms"]:
        runtime.validate_empirical_program(form + "\n")
    for module, names in contract["imports"].items():
        runtime.validate_empirical_program(f"import {module}\nfrom {module} import {names[0]}\n")
    summary = runtime.capability_summary()
    assert "Preloaded helpers" in summary
    assert "not proofs" in summary
    assert len(summary) < 2500


@pytest.mark.parametrize(
    ("program", "fragment"),
    [
        ("import os\n", "module 'os' is not available"),
        ("from math import sqrt\n", "math.sqrt is not available"),
        ("from fractions import Fraction as __q\n", "import alias '__q' is not allowed"),
        ("try:\n    pass\nexcept Exception:\n    pass\n", "try/except is outside"),
        ("class A:\n    pass\n", "class definition is outside"),
        ("a = 1\nb = 2\nprint(a @ b)\n", "the @ operator is outside"),
        ("x = [1]\nprint(x.__class__)\n", "attribute '__class__' is not allowed"),
        ('print("{}".format(1))\n', "method 'format' is not allowed"),
        ("import math\nprint(math.sqrt(4))\n", "method 'sqrt' is not allowed"),
        ('open("x")\n', "identifier 'open' is not allowed"),
        ("sqrt(4)\n", "call to 'sqrt' is not available"),
        ("x = 1\nx.__class__()\n", "attribute '__class__' is not allowed"),
        ("def f(x: int):\n    return x\n", "function annotations are not allowed"),
    ],
)
def test_unsupported_constructs_are_named_precisely(program, fragment):
    with pytest.raises(runtime.EmpiricalProgramDenied) as excinfo:
        runtime.validate_empirical_program(program)
    assert fragment in str(excinfo.value)


def test_denial_carries_the_capability_summary(tmp_path):
    result = _run("import os\nprint(os.getcwd())\n", tmp_path)

    assert result["status"] == "empirical_compute_denied"
    assert "module 'os' is not available" in result["error"]
    assert "Preloaded helpers" in result["capabilities"]


def test_success_and_runtime_errors_do_not_repeat_the_contract(tmp_path):
    ok = _run("print(1)\n", tmp_path)
    failed = _run("print(1 // 0)\n", tmp_path)

    assert "capabilities" not in ok
    assert failed["status"] == "empirical_compute_error"
    assert "ZeroDivisionError" in failed["error"]
    assert "capabilities" not in failed


def test_module_namespaces_never_expose_the_real_module(tmp_path):
    """``import math`` binds a namespace of preloaded helpers, not the stdlib module."""
    result = _run("import math\nprint(sorted(k for k in dir(math)))\n", tmp_path)

    assert result["status"] == "empirical_compute_denied"
    assert "call to 'dir' is not available" in result["error"]
