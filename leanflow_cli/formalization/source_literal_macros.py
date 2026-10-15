"""Bound required brace arguments for lexical macro effect refusal without executing TeX."""

from __future__ import annotations

import re

_COMMAND = re.compile(r"\\([a-zA-Z@]+|.)", re.DOTALL)
_SPACE = " \t\r\n"
MAX_ARGUMENT_CHARACTERS = 4096


def contains_paragraph_break(text: str) -> bool:
    """Notice blank physical lines without changing original source coordinates."""
    return re.search(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n")) is not None


def required_parameter_count(command: str, head: str, declaration: str) -> int | None:
    """Accept only contiguous plain def parameters or literal required newcommand arity."""
    if command == "def":
        match = re.fullmatch(r"[ \t\r\n]*\\[a-zA-Z]+[ \t\r\n]*((?:#[1-9])*)[ \t\r\n]*", head)
        if match is None:
            return None
        parameters = match[1]
        count = len(parameters) // 2
        return (
            count if parameters == "".join(f"#{index}" for index in range(1, count + 1)) else None
        )
    if command in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
        name = re.match(
            r"[ \t\r\n]*(?:\{[ \t\r\n]*\\[a-zA-Z]+[ \t\r\n]*\}|\\[a-zA-Z]+)", declaration
        )
        if name is None:
            return None
        remainder = declaration[name.end() :].lstrip(_SPACE)
        count = 0
        if remainder.startswith("["):
            arity = re.match(r"\[([0-9])\]", remainder)
            if arity is None:
                return None
            count = int(arity[1])
            remainder = remainder[arity.end() :].lstrip(_SPACE)
        # Optional defaults, delimited parameters and expansion-sensitive syntax
        # have no argument model here.
        return count if remainder.startswith("{") else None
    if command in {"newenvironment", "renewenvironment", "gdef", "edef", "xdef"}:
        return None
    return 0


def literal_arguments(text: str, start: int, count: int) -> list[str] | None:
    """Require bounded literal brace arguments and retain their exact replacement text."""
    cursor = start
    arguments: list[str] = []
    for _ in range(count):
        while cursor < len(text) and text[cursor] in _SPACE:
            cursor += 1
        if cursor >= len(text) or text[cursor] != "{" or cursor - start > MAX_ARGUMENT_CHARACTERS:
            return None
        begin = cursor + 1
        cursor, depth = begin, 1
        while cursor < len(text) and cursor - start <= MAX_ARGUMENT_CHARACTERS:
            char = text[cursor]
            if char == "%":
                return None
            if char == "\\":
                command = _COMMAND.match(text, cursor)
                if command is None:
                    return None
                cursor = command.end()
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    arguments.append(text[begin:cursor])
                    cursor += 1
                    break
            cursor += 1
        else:
            return None
    return arguments


def balanced_math_delimiters(text: str) -> bool:
    """Refuse helpers which can open or close a caller's literal math scope."""
    cursor = 0
    opened: str | None = None
    while cursor < len(text):
        if text[cursor] == "%":
            # Replacement text stores tokens after TeX comment processing;
            # raw delimiters in a comment cannot establish helper purity.
            return False
        if text[cursor] == "\\":
            command = _COMMAND.match(text, cursor)
            if command is None:
                return False
            cursor = command.end()
            continue
        if text[cursor] == "$":
            delimiter = "$$" if text.startswith("$$", cursor) else "$"
            if opened is None:
                opened = delimiter
            elif opened == delimiter:
                opened = None
            else:
                return False
            cursor += len(delimiter)
            continue
        cursor += 1
    return opened is None
