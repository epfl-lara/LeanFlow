"""Evaluate only bounded literal TeX integers while retaining unknown expansion as uncertainty."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .source_builtins import LATEX_BUILTINS
from .source_literal_macros import (
    balanced_math_delimiters,
    contains_paragraph_break,
    literal_arguments,
    required_parameter_count,
)

_COMMAND = re.compile(r"\\([a-zA-Z@]+|.)", re.DOTALL)
_LITERAL = re.compile(r"[+-]?[0-9]{1,10}\Z")
MAX_INTEGER = 2**31 - 1
MAX_EXPANSIONS = 16
MAX_OPERAND_CHARACTERS = 128
MAX_GROUP_DEPTH = 128
_LOADERS = {"documentclass", "usepackage", "RequirePackage"}
CONDITIONAL_PRIMITIVES = frozenset(
    {
        "if",
        "ifcat",
        "ifnum",
        "ifdim",
        "ifodd",
        "ifvmode",
        "ifhmode",
        "ifmmode",
        "ifinner",
        "ifvoid",
        "ifhbox",
        "ifvbox",
        "ifx",
        "ifeof",
        "iftrue",
        "iffalse",
        "ifcase",
        "ifdefined",
        "ifcsname",
        "iffontchar",
    }
)
_UNSAFE = {
    *_LOADERS,
    "bibliography",
    "tableofcontents",
    "catcode",
    "csname",
    "expandafter",
    "scantokens",
    "inputencoding",
    "let",
    "futurelet",
    "unless",
    "global",
    "aftergroup",
    "afterassignment",
    "noexpand",
    "input",
    "include",
    "subfile",
    "directlua",
    "luaexec",
    "write",
    "read",
    "loop",
    "repeat",
    "numexpr",
    "makeatletter",
    "makeatother",
    "newlength",
    "setlength",
    "addtolength",
    "newcounter",
    "setcounter",
    "addtocounter",
    "newcount",
    "countdef",
    "dimendef",
    "toksdef",
    "newtoks",
    "advance",
    "multiply",
    "divide",
    "chardef",
    "mathchardef",
    "font",
    "newsavebox",
    "setbox",
}
_DEFINERS = {
    "def",
    "gdef",
    "edef",
    "xdef",
    "newcommand",
    "renewcommand",
    "providecommand",
    "DeclareRobustCommand",
    "newenvironment",
    "renewenvironment",
    "newif",
    "newtheorem",
    "declaretheorem",
    "newmdtheoremenv",
    "mdtheorem",
    "newtcbtheorem",
    "NewTcbTheorem",
    "spnewtheorem",
    "DeclareMathOperator",
}
LITERAL_ENVIRONMENTS = frozenset({"comment", "verbatim", "Verbatim", "lstlisting", "minted"})
SCANNER_CONTROL_NAMES = frozenset(
    _DEFINERS
    | _LOADERS
    | CONDITIONAL_PRIMITIVES
    | LITERAL_ENVIRONMENTS
    | {"end" + name for name in LITERAL_ENVIRONMENTS}
    | {
        "ifnum",
        "iftrue",
        "iffalse",
        "else",
        "fi",
        "begin",
        "end",
        "endinput",
        "includeonly",
        "begingroup",
        "endgroup",
        "(",
        ")",
        "[",
        "]",
    }
)
_RESERVED = _UNSAFE | SCANNER_CONTROL_NAMES | {"relax"}
# These public rendering commands emit non-digit/grouping tokens. A locally
# redefined spelling never receives this termination privilege.
_RENDERING_TERMINATORS = {
    "qed",
    "qedhere",
    "begin",
    "end",
    "quad",
    "qquad",
    "text",
    "mathbb",
    "mathsf",
}


@dataclass(frozen=True)
class _Macro:
    """Retain exact body and supported constant status separately from effect analysis."""

    body: str
    literal: str | None
    arguments: int | None
    effect_unknown: bool = False
    conditional_token: bool = False
    boolean_setter: bool = False


class LiteralTexNumbers:
    """Track a small scoped subset; never run TeX, recursive expansion, or package code."""

    def __init__(self) -> None:
        self.macros: dict[str, _Macro] = {}
        self.groups: list[tuple[str, dict[str, _Macro | None]]] = []
        self.trusted = True

    def loader(self) -> None:
        """Refuse external effects even for standard packages and classes."""
        self.trusted = False

    def enter(self, kind: str, *, uncertain: bool = False) -> None:
        """Record lexical ownership without copying the complete macro environment."""
        if uncertain or len(self.groups) >= MAX_GROUP_DEPTH:
            self.trusted = False
            return
        self.groups.append((kind, {}))

    def leave(self, kind: str, *, uncertain: bool = False) -> None:
        """Restore exactly local assignments, refusing mismatched or unknown scopes."""
        if uncertain or not self.groups or self.groups[-1][0] != kind:
            self.trusted = False
            return
        _, previous = self.groups.pop()
        for name, value in previous.items():
            if value is None:
                self.macros.pop(name, None)
            else:
                self.macros[name] = value

    def text_groups(self, text: str, *, uncertain: bool) -> None:
        """Notice unescaped literal group boundaries in already scanned source text."""
        cursor = 0
        while cursor < len(text):
            char = text[cursor]
            if char == "{":
                self.enter("brace", uncertain=uncertain)
            elif char == "}":
                self.leave("brace", uncertain=uncertain)
            elif char == "$":
                delimiter = "$$" if text.startswith("$$", cursor) else "$"
                kind = "math:" + delimiter
                if self.groups and self.groups[-1][0] == kind:
                    self.leave(kind, uncertain=uncertain)
                else:
                    self.enter(kind, uncertain=uncertain)
                cursor += len(delimiter) - 1
            cursor += 1

    def define(
        self,
        name: str,
        body: str,
        command: str,
        head: str,
        *,
        uncertain: bool,
        declaration: str = "",
        conditional_token: bool = False,
        boolean_setter: bool = False,
    ) -> None:
        """Admit only parameter-free plain def bodies consisting of one decimal integer."""
        if name in _RESERVED or command in {"gdef", "edef", "xdef"}:
            self.trusted = False
        # Math has additional implicit groups (for example left/right) which
        # this scanner does not execute. Never resolve numeric branches after
        # source assignments inside math from a guessed grouping model.
        if any(kind.startswith("math:") for kind, _ in self.groups):
            self.trusted = False
        plain = (
            command == "def" and re.fullmatch(r"[ \t\r\n]*\\[a-zA-Z]+[ \t\r\n]*", head) is not None
        )
        literal = (
            body
            if plain and not uncertain and name not in LATEX_BUILTINS and _LITERAL.fullmatch(body)
            else None
        )
        if literal is not None and abs(int(literal)) > MAX_INTEGER:
            literal = None
        if self.groups and name not in self.groups[-1][1]:
            self.groups[-1][1][name] = self.macros.get(name)
        environment_definer = command in {
            "newenvironment",
            "renewenvironment",
            "newtheorem",
            "declaretheorem",
            "newmdtheoremenv",
            "mdtheorem",
            "newtcbtheorem",
            "NewTcbTheorem",
            "spnewtheorem",
        }
        self.macros[name] = _Macro(
            body,
            literal,
            required_parameter_count(command, head, declaration),
            uncertain or environment_definer,
            conditional_token,
            boolean_setter,
        )
        generated = (
            ["end" + name]
            if command in {"newenvironment", "renewenvironment"}
            else (
                ["the" + name, "end" + name]
                if command
                in {
                    "newtheorem",
                    "declaretheorem",
                    "newmdtheoremenv",
                    "mdtheorem",
                    "newtcbtheorem",
                    "NewTcbTheorem",
                    "spnewtheorem",
                }
                else []
            )
        )
        for control in generated:
            if self.groups and control not in self.groups[-1][1]:
                self.groups[-1][1][control] = self.macros.get(control)
            # LaTeX generates these meanings. Never keep an earlier literal
            # definition or guess the generated counter/end implementation.
            self.macros[control] = _Macro("", None, None, True)

    def is_conditional(self, name: str) -> bool:
        """Recognize current scoped conditional tokens, never a spelling alone."""
        macro = self.macros.get(name)
        return (
            macro.conditional_token and not macro.effect_unknown
            if macro is not None
            else name in CONDITIONAL_PRIMITIVES
        )

    def is_boolean_setter(self, name: str) -> bool:
        """Recognize only a currently scoped setter produced by a supported newif."""
        macro = self.macros.get(name)
        return macro is not None and macro.boolean_setter and not macro.effect_unknown

    def command(self, name: str, text: str | None = None, start: int | None = None) -> None:
        """Invalidate numeric selection when an invocation may change macro meaning or scope."""
        if name in _UNSAFE or (
            name not in self.macros
            and name not in LATEX_BUILTINS
            and any(char.isalpha() for char in name)
        ):
            self.trusted = False
        elif name in self.macros:
            if not self._pure(name, set(), [0]):
                self.trusted = False
                return
            count = self.macros[name].arguments
            if count:
                arguments = (
                    literal_arguments(text, start, count)
                    if text is not None and start is not None
                    else None
                )
                if arguments is None or any(
                    not self._pure_text(value, set(), [0]) for value in arguments
                ):
                    self.trusted = False

    def _pure(self, name: str, ancestors: set[str], budget: list[int]) -> bool:
        """Bound effect inspection of source-defined helper calls without expanding replacement text."""
        budget[0] += 1
        if name in ancestors or budget[0] > MAX_EXPANSIONS:
            return False
        macro = self.macros[name]
        if macro.effect_unknown or macro.arguments is None or len(macro.body) > 4096:
            return False
        return self._pure_text(macro.body, ancestors | {name}, budget)

    def _pure_text(self, text: str, ancestors: set[str], budget: list[int]) -> bool:
        """Refuse assignments and hidden scope/loader effects, including through helper calls."""
        if not balanced_math_delimiters(text):
            return False
        for match in _COMMAND.finditer(text):
            child = match[1]
            if child in _UNSAFE | SCANNER_CONTROL_NAMES:
                return False
            if child in self.macros:
                if not self._pure(child, ancestors, budget):
                    return False
            elif any(char.isalpha() for char in child) and child not in LATEX_BUILTINS:
                return False
        return True

    def _integer(self, text: str, start: int) -> tuple[int, int] | None:
        """Read decimal tokens with bounded direct literal macro substitution and a known terminator."""
        cursor = start
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
            if cursor - start > MAX_OPERAND_CHARACTERS:
                return None
        if contains_paragraph_break(text[start:cursor]):
            return None
        digits = ""
        substitutions = 0
        while cursor < len(text) and cursor - start <= MAX_OPERAND_CHARACTERS:
            char = text[cursor]
            if char == "%":
                return None
            if char in "+-" and not digits:
                digits = char
                cursor += 1
            elif char.isascii() and char.isdigit():
                digits += char
                cursor += 1
            elif char == "\\":
                match = _COMMAND.match(text, cursor)
                if match is None:
                    return None
                name = match[1]
                macro = self.macros.get(name)
                if macro and macro.literal is not None:
                    substitutions += 1
                    if substitutions > MAX_EXPANSIONS or (
                        digits and macro.literal.startswith(("+", "-"))
                    ):
                        return None
                    digits += macro.literal
                    cursor = match.end()
                    # TeX discards spaces following a control word before it
                    # expands the replacement; a following digit still joins it.
                    whitespace = cursor
                    while cursor < len(text) and text[cursor] in " \t\r\n":
                        cursor += 1
                        if cursor - start > MAX_OPERAND_CHARACTERS:
                            return None
                    if contains_paragraph_break(text[whitespace:cursor]):
                        return None
                elif name == "relax" and name not in self.macros:
                    cursor = match.end()
                    break
                elif name in _RENDERING_TERMINATORS and name not in self.macros:
                    break
                else:
                    return None
            else:
                if char.isspace() and char not in " \t\r\n":
                    return None
                if char in " \t\r\n":
                    cursor += 1
                break
            if len(digits.lstrip("+-")) > 10:
                return None
        if cursor - start > MAX_OPERAND_CHARACTERS or _LITERAL.fullmatch(digits) is None:
            return None
        number = int(digits)
        return (number, cursor) if abs(number) <= MAX_INTEGER else None

    def conditional(self, text: str, start: int) -> tuple[bool, int] | None:
        """Return a known comparison and original end offset, or preserve unresolved structure."""
        if not self.trusted:
            return None
        left = self._integer(text, start)
        if left is None:
            return None
        cursor = left[1]
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
            if cursor - left[1] > MAX_OPERAND_CHARACTERS:
                return None
        if contains_paragraph_break(text[start:cursor]):
            return None
        if cursor >= len(text) or text[cursor] not in "<=>":
            return None
        relation = text[cursor]
        right = self._integer(text, cursor + 1)
        if right is None:
            return None
        value = {"<": left[0] < right[0], "=": left[0] == right[0], ">": left[0] > right[0]}[
            relation
        ]
        return value, right[1]
