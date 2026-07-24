"""Execute bounded exact arithmetic in a restricted Python subprocess.

The empirical research lane needs small integer and rational experiments, but
giving a scratch worker a general Python interpreter would restore project
write authority.  This module is both the AST validator and the isolated child
runtime used by ``empirical_compute``.  It deliberately depends only on the
standard library so the child can run with Python's isolated ``-I -S`` mode.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import reprlib
import resource
import sys
from fractions import Fraction
from typing import Any, Final

MAX_PROGRAM_BYTES: Final[int] = 16 * 1024
MAX_OUTPUT_BYTES: Final[int] = 32 * 1024
MAX_AST_NODES: Final[int] = 4_000
MEMORY_LIMIT_BYTES: Final[int] = 192 * 1024 * 1024

_SAFE_CALLS: Final[frozenset[str]] = frozenset(
    {
        "Fraction",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "divmod",
        "enumerate",
        "gcd",
        "int",
        "isqrt",
        "lcm",
        "len",
        "list",
        "max",
        "min",
        "print",
        "prod",
        "range",
        "repr",
        "reversed",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)
_SAFE_METHOD_CALLS: Final[frozenset[str]] = frozenset(
    {"append", "clear", "copy", "count", "extend", "index", "pop", "reverse", "sort"}
)
_SAFE_VALUE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"denominator", "numerator"})
_SAFE_IMPORTS: Final[dict[str, frozenset[str]]] = {
    "fractions": frozenset({"Fraction"}),
    "math": frozenset({"gcd", "isqrt", "lcm", "prod"}),
}
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
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.For,
    ast.While,
    ast.If,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Assert,
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
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


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


def _validate_import(node: ast.ImportFrom) -> None:
    """Accept only compatibility imports for already-injected arithmetic names."""
    allowed_names = _SAFE_IMPORTS.get(str(node.module or ""))
    if node.level or allowed_names is None:
        raise EmpiricalProgramDenied(
            f"imports are limited to Fraction and selected math helpers{_location(node)}"
        )
    for alias in node.names:
        if alias.name not in allowed_names or alias.asname not in {None, alias.name}:
            raise EmpiricalProgramDenied(
                f"import {node.module}.{alias.name} is not allowed{_location(node)}"
            )


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

    for node in nodes:
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise EmpiricalProgramDenied(
                f"syntax {type(node).__name__} is outside the arithmetic subset{_location(node)}"
            )
        if isinstance(node, ast.Name) and not _identifier_allowed(node.id):
            raise EmpiricalProgramDenied(f"identifier {node.id!r} is not allowed{_location(node)}")
        if isinstance(node, ast.ImportFrom):
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
            if any(argument.annotation is not None for argument in node.args.args):
                raise EmpiricalProgramDenied(
                    f"function annotations are not allowed{_location(node)}"
                )
        if isinstance(node, ast.Attribute):
            allowed = _SAFE_METHOD_CALLS | _SAFE_VALUE_ATTRIBUTES
            if node.attr not in allowed or isinstance(node.ctx, ast.Store):
                raise EmpiricalProgramDenied(
                    f"attribute {node.attr!r} is not allowed{_location(node)}"
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_CALLS and node.func.id not in function_names:
                    raise EmpiricalProgramDenied(
                        f"call to {node.func.id!r} is not allowed{_location(node)}"
                    )
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr not in _SAFE_METHOD_CALLS:
                    raise EmpiricalProgramDenied(
                        f"method {node.func.attr!r} is not allowed{_location(node)}"
                    )
            else:
                raise EmpiricalProgramDenied(
                    f"indirect function calls are not allowed{_location(node)}"
                )
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                unexpected = [item.arg for item in node.keywords if item.arg not in {"end", "sep"}]
                if unexpected:
                    raise EmpiricalProgramDenied(
                        f"print keyword {unexpected[0]!r} is not allowed{_location(node)}"
                    )
    return tree


class _StripCompatibilityImports(ast.NodeTransformer):
    """Remove validated imports because helpers are injected without ``__import__``."""

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Pass(), node)


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
    safe_builtins: dict[str, Any] = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "divmod": divmod,
        "enumerate": enumerate,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": printer,
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
    return {
        "__builtins__": safe_builtins,
        "Fraction": Fraction,
        "gcd": math.gcd,
        "isqrt": math.isqrt,
        "lcm": math.lcm,
        "prod": math.prod,
    }


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
        }
    _apply_resource_limits(timeout_s)
    sys.setrecursionlimit(500)
    printer = _BoundedPrinter()
    stripped = _StripCompatibilityImports().visit(tree)
    ast.fix_missing_locations(stripped)
    try:
        code = compile(stripped, "<empirical-compute>", "exec", dont_inherit=True, optimize=2)
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
    "execute_validated_program",
    "validate_empirical_program",
]
