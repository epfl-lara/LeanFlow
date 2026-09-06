"""Execute bounded exact arithmetic in a restricted Python subprocess.

The empirical research lane needs small integer and rational experiments, but
giving a scratch worker a general Python interpreter would restore project
write authority.  This module is both the AST validator and the isolated child
runtime used by ``empirical_compute``.  It deliberately depends only on the
standard library so the child can run with Python's isolated ``-I -S`` mode.

The accepted subset is described by one table, :data:`_MODULE_HELPERS`, plus the
builtin/method allowlists below.  :func:`capability_contract` renders that same
data for tool prompts and for the ``capabilities`` field of a denial, so the
model sees exactly what is supported instead of guessing from a terse error.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import math
import os
import reprlib
import resource
import sys
import types
from collections.abc import Callable
from fractions import Fraction
from typing import Any, Final

MAX_PROGRAM_BYTES: Final[int] = 16 * 1024
MAX_OUTPUT_BYTES: Final[int] = 32 * 1024
MAX_AST_NODES: Final[int] = 4_000
MEMORY_LIMIT_BYTES: Final[int] = 192 * 1024 * 1024
RECURSION_LIMIT: Final[int] = 500

#: Helper callables preloaded into the program's globals, grouped by the stdlib
#: module they may also be imported from (``from math import gcd``,
#: ``from fractions import Fraction as Q``, ``import itertools as it``).  This is
#: the single source of truth for the validator, the child's globals, and the
#: capability contract.
_MODULE_HELPERS: Final[dict[str, dict[str, Callable[..., Any]]]] = {
    "fractions": {"Fraction": Fraction},
    "math": {
        "comb": math.comb,
        "factorial": math.factorial,
        "gcd": math.gcd,
        "isqrt": math.isqrt,
        "lcm": math.lcm,
        "perm": math.perm,
        "prod": math.prod,
    },
    "itertools": {
        "combinations": itertools.combinations,
        "combinations_with_replacement": itertools.combinations_with_replacement,
        "permutations": itertools.permutations,
        "product": itertools.product,
    },
}
_SAFE_IMPORTS: Final[dict[str, frozenset[str]]] = {
    module: frozenset(helpers) for module, helpers in _MODULE_HELPERS.items()
}
_HELPER_NAMES: Final[frozenset[str]] = frozenset(
    name for helpers in _MODULE_HELPERS.values() for name in helpers
)
_SAFE_BUILTINS: Final[dict[str, Any]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "frozenset": frozenset,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "pow": pow,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
_SAFE_CALLS: Final[frozenset[str]] = frozenset(_SAFE_BUILTINS) | _HELPER_NAMES | {"print"}
_SAFE_METHOD_CALLS: Final[frozenset[str]] = frozenset(
    {
        # list
        "append",
        "clear",
        "copy",
        "count",
        "extend",
        "index",
        "insert",
        "pop",
        "remove",
        "reverse",
        "sort",
        # dict
        "get",
        "items",
        "keys",
        "setdefault",
        "update",
        "values",
        # set / frozenset
        "add",
        "difference",
        "discard",
        "intersection",
        "isdisjoint",
        "issubset",
        "issuperset",
        "symmetric_difference",
        "union",
        # int / Fraction / str
        "as_integer_ratio",
        "bit_count",
        "bit_length",
        "join",
        "limit_denominator",
    }
)
#: ``math.gcd(...)`` after ``import math`` is an attribute call on a helper namespace.
_SAFE_ATTRIBUTE_CALLS: Final[frozenset[str]] = _SAFE_METHOD_CALLS | _HELPER_NAMES
_SAFE_VALUE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"denominator", "numerator"})
_FORBIDDEN_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "type",
        "vars",
    }
)

_ALLOWED_NODE_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    ast.Expr,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Assign,
    ast.AugAssign,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Subscript,
    ast.Slice,
    ast.Starred,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.For,
    ast.While,
    ast.If,
    ast.FunctionDef,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Assert,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Invert,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)

#: User-facing names for constructs outside the subset, keyed by AST class name.
_CONSTRUCT_NAMES: Final[dict[str, str]] = {
    "AnnAssign": "annotated assignment (drop the annotation)",
    "AsyncFor": "async code",
    "AsyncFunctionDef": "async code",
    "AsyncWith": "async code",
    "Await": "async code",
    "ClassDef": "class definition",
    "Delete": "del statement",
    "Global": "global declaration",
    "Match": "match statement",
    "MatMult": "the @ operator",
    "NamedExpr": "walrus assignment (:=)",
    "Nonlocal": "nonlocal declaration",
    "Raise": "raise statement",
    "Try": "try/except",
    "TryStar": "try/except",
    "With": "with statement",
    "Yield": "generator function (yield)",
    "YieldFrom": "generator function (yield from)",
}


class EmpiricalProgramDenied(ValueError):
    """Report a deterministic source-policy rejection."""


class EmpiricalOutputLimitExceeded(RuntimeError):
    """Stop a computation before its structured output becomes unbounded."""


def _location(node: ast.AST) -> str:
    """Return a compact source location for one rejected AST node."""
    line = int(getattr(node, "lineno", 0) or 0)
    return f" at line {line}" if line else ""


def _identifier_allowed(name: str) -> bool:
    """Return whether user code may bind or reference one identifier."""
    return bool(name) and name not in _FORBIDDEN_IDENTIFIERS and not name.startswith("__")


def _supported_import_forms() -> str:
    """Render the import statements the subset accepts, for diagnostics."""
    return "; ".join(
        f"from {module} import {', '.join(sorted(names))}"
        for module, names in _SAFE_IMPORTS.items()
    )


def _validate_alias_binding(alias: ast.alias, node: ast.AST) -> None:
    """Reject aliases that would bind a forbidden or dunder identifier."""
    bound = alias.asname or alias.name
    if not _identifier_allowed(bound):
        raise EmpiricalProgramDenied(f"import alias {bound!r} is not allowed{_location(node)}")


def _validate_import_from(node: ast.ImportFrom) -> None:
    """Accept ``from module import name [as alias]`` for preloaded helper names only."""
    module = str(node.module or "")
    allowed_names = _SAFE_IMPORTS.get(module)
    if node.level or allowed_names is None:
        raise EmpiricalProgramDenied(
            f"module {module or '.'!r} is not available{_location(node)}; supported imports: "
            f"{_supported_import_forms()} (aliases such as `Fraction as Q` are fine)"
        )
    for alias in node.names:
        if alias.name not in allowed_names:
            raise EmpiricalProgramDenied(
                f"{module}.{alias.name} is not available{_location(node)}; "
                f"{module} offers {', '.join(sorted(allowed_names))}"
            )
        _validate_alias_binding(alias, node)


def _validate_import(node: ast.Import) -> None:
    """Accept ``import module [as alias]`` for the helper modules only."""
    for alias in node.names:
        if alias.name not in _SAFE_IMPORTS:
            raise EmpiricalProgramDenied(
                f"module {alias.name!r} is not available{_location(node)}; importable modules: "
                f"{', '.join(_SAFE_IMPORTS)} (each exposes only its preloaded helpers)"
            )
        _validate_alias_binding(alias, node)


def _validate_function_arguments(arguments: ast.arguments, node: ast.AST) -> None:
    """Reject annotations and forbidden parameter names on def/lambda arguments."""
    all_args = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *([arguments.vararg] if arguments.vararg else []),
        *([arguments.kwarg] if arguments.kwarg else []),
    ]
    for argument in all_args:
        if argument.annotation is not None:
            raise EmpiricalProgramDenied(f"function annotations are not allowed{_location(node)}")
        if not _identifier_allowed(argument.arg):
            raise EmpiricalProgramDenied(
                f"parameter name {argument.arg!r} is not allowed{_location(node)}"
            )


def _validate_call(node: ast.Call, callable_names: set[str]) -> None:
    """Allow direct calls to builtins, preloaded helpers, program-bound names, and safe methods.

    A name the program binds itself (a ``def``, an import alias such as ``Q``, a
    lambda assigned to a variable, a loop variable) can only hold values built
    from the allowed subset, so calling it adds no capability.
    """
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if not _identifier_allowed(name):
            raise EmpiricalProgramDenied(f"identifier {name!r} is not allowed{_location(node)}")
        if name not in _SAFE_CALLS and name not in callable_names:
            raise EmpiricalProgramDenied(
                f"call to {name!r} is not available{_location(node)}; callable names are the "
                f"preloaded helpers ({', '.join(sorted(_HELPER_NAMES))}), the builtins "
                f"({', '.join(sorted(_SAFE_BUILTINS))}, print), and names the program binds"
            )
        if node.func.id == "print":
            unexpected = [item.arg for item in node.keywords if item.arg not in {"end", "sep"}]
            if unexpected:
                raise EmpiricalProgramDenied(
                    f"print keyword {unexpected[0]!r} is not allowed{_location(node)}"
                )
    elif isinstance(node.func, ast.Attribute):
        attribute = node.func.attr
        if attribute.startswith("__"):
            raise EmpiricalProgramDenied(f"attribute {attribute!r} is not allowed{_location(node)}")
        if attribute not in _SAFE_ATTRIBUTE_CALLS:
            raise EmpiricalProgramDenied(
                f"method {attribute!r} is not allowed{_location(node)}; supported methods: "
                f"{', '.join(sorted(_SAFE_METHOD_CALLS))}"
            )
    else:
        raise EmpiricalProgramDenied(f"indirect function calls are not allowed{_location(node)}")


def validate_empirical_program(program: str) -> ast.Module:
    """Parse and validate the bounded arithmetic subset accepted by the child."""
    source = str(program or "")
    if not source.strip():
        raise EmpiricalProgramDenied("program must contain an exact arithmetic experiment")
    if len(source.encode("utf-8")) > MAX_PROGRAM_BYTES:
        raise EmpiricalProgramDenied(f"program exceeds the {MAX_PROGRAM_BYTES}-byte limit")
    try:
        tree = ast.parse(source, filename="<empirical-compute>", mode="exec")
    except SyntaxError as exc:
        raise EmpiricalProgramDenied(
            f"invalid Python syntax at line {int(exc.lineno or 0)}: {exc.msg}"
        ) from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise EmpiricalProgramDenied(f"program exceeds the {MAX_AST_NODES}-node AST limit")
    function_names = {node.name for node in nodes if isinstance(node, ast.FunctionDef)}
    if any(not _identifier_allowed(name) for name in function_names):
        raise EmpiricalProgramDenied("function names may not shadow interpreter capabilities")
    # Every name the program binds: assignment and loop targets, parameters, and
    # import aliases such as ``Fraction as Q``.
    callable_names = set(function_names)
    callable_names.update(
        node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
    callable_names.update(node.arg for node in nodes if isinstance(node, ast.arg))
    callable_names.update(
        alias.asname or alias.name
        for node in nodes
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )

    for node in nodes:
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            kind = type(node).__name__
            label = _CONSTRUCT_NAMES.get(kind, f"syntax {kind}")
            raise EmpiricalProgramDenied(
                f"{label} is outside the exact-arithmetic subset{_location(node)}; "
                "see the capabilities summary for the supported statements and operators"
            )
        if isinstance(node, ast.Name) and not _identifier_allowed(node.id):
            raise EmpiricalProgramDenied(f"identifier {node.id!r} is not allowed{_location(node)}")
        if isinstance(node, ast.ImportFrom):
            _validate_import_from(node)
        if isinstance(node, ast.Import):
            _validate_import(node)
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise EmpiricalProgramDenied(
                    f"function decorators are not allowed{_location(node)}"
                )
            if node.returns is not None:
                raise EmpiricalProgramDenied(
                    f"function annotations are not allowed{_location(node)}"
                )
            _validate_function_arguments(node.args, node)
        if isinstance(node, ast.Lambda):
            _validate_function_arguments(node.args, node)
        if isinstance(node, ast.Attribute):
            allowed = _SAFE_ATTRIBUTE_CALLS | _SAFE_VALUE_ATTRIBUTES
            if node.attr not in allowed or isinstance(node.ctx, ast.Store):
                raise EmpiricalProgramDenied(
                    f"attribute {node.attr!r} is not allowed{_location(node)}; supported "
                    f"attributes: {', '.join(sorted(_SAFE_VALUE_ATTRIBUTES))} and the methods "
                    f"{', '.join(sorted(_SAFE_METHOD_CALLS))}"
                )
        if isinstance(node, ast.Call):
            _validate_call(node, callable_names)
    return tree


class _BindCompatibilityImports(ast.NodeTransformer):
    """Turn validated imports into bindings of the preloaded helper objects.

    The child has no ``__import__``; helpers and their module namespaces are
    already present in the globals.  A plain ``from math import gcd`` therefore
    becomes ``pass`` and an aliased form such as ``Fraction as Q`` becomes the
    assignment ``Q = Fraction``.
    """

    @staticmethod
    def _bindings(pairs: list[tuple[str, str]], node: ast.AST) -> ast.AST:
        renamed = [(bound, source) for bound, source in pairs if bound != source]
        if not renamed:
            return ast.copy_location(ast.Pass(), node)
        # A statement position holds one node, so several aliases become one
        # tuple assignment: ``(a, b) = (x, y)``.
        targets: list[ast.expr] = [ast.Name(id=bound, ctx=ast.Store()) for bound, _ in renamed]
        values: list[ast.expr] = [ast.Name(id=source, ctx=ast.Load()) for _, source in renamed]
        assignment = ast.Assign(
            targets=[targets[0] if len(renamed) == 1 else ast.Tuple(elts=targets, ctx=ast.Store())],
            value=values[0] if len(renamed) == 1 else ast.Tuple(elts=values, ctx=ast.Load()),
        )
        return ast.copy_location(assignment, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:  # noqa: N802
        return self._bindings(
            [(alias.asname or alias.name, alias.name) for alias in node.names], node
        )

    def visit_Import(self, node: ast.Import) -> ast.AST:  # noqa: N802
        return self._bindings(
            [(alias.asname or alias.name, alias.name) for alias in node.names], node
        )


class _BoundedPrinter:
    """Collect deterministic stdout while enforcing its byte limit eagerly."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = 0
        self._repr = reprlib.Repr()
        self._repr.maxlevel = 6
        self._repr.maxlist = 80
        self._repr.maxtuple = 80
        self._repr.maxset = 80
        self._repr.maxfrozenset = 80
        self._repr.maxdict = 40
        self._repr.maxstring = 2_000
        self._repr.maxother = 2_000

    def _render(self, value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, bool, type(None), Fraction)):
            return str(value)
        return self._repr.repr(value)

    def __call__(self, *values: object, sep: str = " ", end: str = "\n") -> None:
        if not isinstance(sep, str) or not isinstance(end, str):
            raise TypeError("print sep and end must be strings")
        rendered = sep.join(self._render(value) for value in values) + end
        encoded_size = len(rendered.encode("utf-8"))
        if self._bytes + encoded_size > MAX_OUTPUT_BYTES:
            raise EmpiricalOutputLimitExceeded(f"output exceeds the {MAX_OUTPUT_BYTES}-byte limit")
        self._parts.append(rendered)
        self._bytes += encoded_size

    def output(self) -> str:
        """Return accumulated bounded stdout."""
        return "".join(self._parts)


def _apply_resource_limits(timeout_s: int) -> None:
    """Apply child-only CPU, memory, file, descriptor, and process ceilings."""
    cpu_s = max(1, int(timeout_s))
    for limit_name, limits in (
        ("RLIMIT_CPU", (cpu_s, cpu_s + 1)),
        ("RLIMIT_FSIZE", (0, 0)),
        ("RLIMIT_NOFILE", (16, 16)),
        ("RLIMIT_NPROC", (0, 0)),
        ("RLIMIT_STACK", (16 * 1024 * 1024, 16 * 1024 * 1024)),
        ("RLIMIT_DATA", (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES)),
    ):
        resource_id = getattr(resource, limit_name, None)
        if resource_id is None:
            continue
        try:
            resource.setrlimit(resource_id, limits)
        except (OSError, ValueError):
            # Some platforms expose but do not enforce a particular resource.
            # The parent still owns the hard wall-clock kill boundary.
            continue
    if sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_AS"):
        try:
            resource.setrlimit(
                resource.RLIMIT_AS,
                (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES),
            )
        except (OSError, ValueError):
            pass


def _safe_globals(printer: _BoundedPrinter) -> dict[str, Any]:
    """Build the only globals visible to validated empirical source."""
    safe_builtins: dict[str, Any] = dict(_SAFE_BUILTINS)
    safe_builtins["print"] = printer
    environment: dict[str, Any] = {"__builtins__": safe_builtins}
    for module, helpers in _MODULE_HELPERS.items():
        environment.update(helpers)
        # ``import math`` / ``math.gcd(...)`` resolve to a namespace holding only the
        # preloaded helpers, never the real module.
        environment[module] = types.SimpleNamespace(**helpers)
    return environment


_EXAMPLE_PROGRAMS: Final[tuple[tuple[str, str], ...]] = (
    (
        "exact rational arithmetic",
        "from fractions import Fraction as Q\n"
        "partial = sum(Q(1, k * k) for k in range(1, 6))\n"
        "print(partial, partial.limit_denominator(100))\n",
    ),
    (
        "sign vectors over a Boolean cube with bit masks",
        "from itertools import product\n"
        "rows = [0b1011, 0b0110, 0b1101]\n"
        "best = None\n"
        "for signs in product((-1, 1), repeat=4):\n"
        "    disc = max(abs(sum(s for j, s in enumerate(signs) if row >> j & 1)) for row in rows)\n"
        "    best = disc if best is None or disc < best else best\n"
        'print("min discrepancy", best)\n',
    ),
    (
        "modular and combinatorial integers",
        "from math import comb, gcd\n"
        "p = 1_000_003\n"
        "print(pow(2, p - 1, p), comb(20, 10) % p, gcd(84, 36), 84 in {84, 36})\n",
    ),
)


def capability_contract() -> dict[str, Any]:
    """Return the accepted subset as JSON-friendly data for tool prompts and denials."""
    return {
        "purpose": (
            "bounded exact-arithmetic experiments in an isolated process; output is "
            "empirical evidence for planning, never a proof"
        ),
        "preloaded": sorted(_HELPER_NAMES),
        "imports": {module: sorted(names) for module, names in _SAFE_IMPORTS.items()},
        "import_forms": [
            "from fractions import Fraction as Q",
            "from math import gcd, isqrt",
            "import itertools as it",
        ],
        "builtins": sorted([*_SAFE_BUILTINS, "print"]),
        "methods": sorted(_SAFE_METHOD_CALLS),
        "attributes": sorted(_SAFE_VALUE_ATTRIBUTES),
        "operators": (
            "+ - * / // % ** and unary -, comparisons == != < <= > >=, in / not in, "
            "is / is not, and / or / not, bitwise & | ^ << >> ~, conditional expressions"
        ),
        "statements": (
            "assignment (augmented and tuple unpacking), for / while / break / continue, "
            "if / elif / else, def without decorators or annotations, lambda, return, "
            "assert, pass, comprehensions and generator expressions, f-strings, "
            "print(..., sep=, end=)"
        ),
        "not_supported": [
            "class definitions, try/except, with, del, global/nonlocal, yield, match",
            "any other module (os, sys, numpy, sympy, random, time are unavailable)",
            "float-only helpers such as math.sqrt; use isqrt or Fraction",
            "str.format and attribute access outside the allowlist; use f-strings",
            "files, network, processes, environment variables, and the clock",
        ],
        "limits": {
            "program_bytes": MAX_PROGRAM_BYTES,
            "ast_nodes": MAX_AST_NODES,
            "output_bytes": MAX_OUTPUT_BYTES,
            "recursion_limit": RECURSION_LIMIT,
            "memory_bytes": MEMORY_LIMIT_BYTES,
        },
        "examples": [{"title": title, "program": program} for title, program in _EXAMPLE_PROGRAMS],
    }


def capability_summary() -> str:
    """Render the capability contract as compact prompt text."""
    contract = capability_contract()
    limits = contract["limits"]
    return "\n".join(
        [
            "empirical_compute accepts a bounded exact-arithmetic Python subset; results are "
            "experiments, not proofs.",
            "Preloaded helpers: "
            + ", ".join(contract["preloaded"])
            + " (also importable, aliases allowed: "
            + "; ".join(contract["import_forms"])
            + ").",
            "Builtins: " + ", ".join(contract["builtins"]) + ".",
            "Methods: " + ", ".join(contract["methods"]) + "; attributes numerator/denominator.",
            "Operators: " + contract["operators"] + ".",
            "Statements: " + contract["statements"] + ".",
            "Not supported: " + "; ".join(contract["not_supported"]) + ".",
            f"Limits: {limits['program_bytes']}-byte program, {limits['ast_nodes']} AST nodes, "
            f"{limits['output_bytes']}-byte output, recursion {limits['recursion_limit']}, "
            "CPU time and memory capped by the parent.",
        ]
    )


def execute_validated_program(program: str, *, timeout_s: int) -> dict[str, Any]:
    """Execute one validated program and return a structured child verdict."""
    try:
        tree = validate_empirical_program(program)
    except EmpiricalProgramDenied as exc:
        return {
            "success": False,
            "status": "empirical_compute_denied",
            "output": "",
            "error": str(exc),
            "capabilities": capability_summary(),
        }
    _apply_resource_limits(timeout_s)
    sys.setrecursionlimit(RECURSION_LIMIT)
    printer = _BoundedPrinter()
    bound = _BindCompatibilityImports().visit(tree)
    ast.fix_missing_locations(bound)
    try:
        code = compile(bound, "<empirical-compute>", "exec", dont_inherit=True, optimize=2)
        environment = _safe_globals(printer)
        exec(code, environment, environment)  # noqa: S102 - validated arithmetic subset
    except EmpiricalOutputLimitExceeded as exc:
        return {
            "success": False,
            "status": "empirical_compute_output_limit",
            "output": printer.output(),
            "error": str(exc),
        }
    except BaseException as exc:
        return {
            "success": False,
            "status": "empirical_compute_error",
            "output": printer.output(),
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }
    return {
        "success": True,
        "status": "empirical_compute_ok",
        "output": printer.output(),
        "error": None,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout-s", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    """Read a bounded program on stdin and emit exactly one JSON verdict."""
    args = _parse_args()
    payload = sys.stdin.buffer.read(MAX_PROGRAM_BYTES + 1)
    if len(payload) > MAX_PROGRAM_BYTES:
        result = {
            "success": False,
            "status": "empirical_compute_denied",
            "output": "",
            "error": f"program exceeds the {MAX_PROGRAM_BYTES}-byte limit",
        }
    else:
        try:
            program = payload.decode("utf-8")
        except UnicodeDecodeError:
            result = {
                "success": False,
                "status": "empirical_compute_denied",
                "output": "",
                "error": "program must be valid UTF-8",
            }
        else:
            result = execute_validated_program(program, timeout_s=max(1, args.timeout_s))
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    os.write(1, encoded.encode("utf-8"))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EmpiricalProgramDenied",
    "MAX_AST_NODES",
    "MAX_OUTPUT_BYTES",
    "MAX_PROGRAM_BYTES",
    "capability_contract",
    "capability_summary",
    "execute_validated_program",
    "validate_empirical_program",
]
