"""Preserve original source coordinates while masking nonexecuted TeX material."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


def digest(data: bytes | str) -> str:
    """Return a qualified content identity independent of source item identity."""
    return "sha256:" + hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def identity(prefix: str, value: str) -> str:
    """Derive a deterministic identifier from structural identity, not content."""
    return prefix + "-" + hashlib.sha256(value.encode()).hexdigest()[:24]


def group(text: str, start: int, opener: str = "{") -> tuple[str, int] | None:
    """Read a balanced TeX argument while retaining its original end offset."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != opener:
        return None
    closer = "}" if opener == "{" else "]"
    depth, pos = 1, start + 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == opener:
            depth += 1
        elif text[pos] == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1 : pos], pos + 1
        pos += 1
    return None


@dataclass
class SourceText:
    """Keep decoded text and exact byte positions for one immutable read."""

    file: str
    raw: bytes
    text: str = field(init=False)
    encoding: str = field(init=False)
    offsets: list[int] = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            self.text = self.raw.decode("utf-8")
            self.encoding = "utf-8"
        except UnicodeDecodeError:
            # Latin-1 is reversible; the caller blocks until the encoding is repaired.
            self.text = self.raw.decode("latin-1")
            self.encoding = "latin-1"
        self.sha256 = digest(self.raw)
        self.offsets = [0]
        for char in self.text:
            self.offsets.append(self.offsets[-1] + len(char.encode(self.encoding)))

    def span(self, start: int, end: int, **extra: Any) -> dict[str, Any]:
        """Return a half-open byte span reconstructible from the frozen file."""
        return {
            "file": self.file,
            "byte_start": self.offsets[start],
            "byte_end": self.offsets[end],
            "line_start": self.text.count("\n", 0, start) + 1,
            "line_end": self.text.count("\n", 0, max(start, end - 1)) + 1,
            "file_hash": self.sha256,
            **extra,
        }


@dataclass
class TexScan:
    """Expose executable lexical candidates and explicitly classified ranges."""

    source: SourceText
    visible: str
    marks: list[tuple[int, int, str, str]]
    definitions: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]


COMMAND = re.compile(r"\\([a-zA-Z@]+\*?|.)", re.DOTALL)
DEFINITION_COMMANDS = {
    "newcommand",
    "renewcommand",
    "providecommand",
    "DeclareRobustCommand",
    "newenvironment",
    "renewenvironment",
    "newtheorem",
    "declaretheorem",
    "newmdtheoremenv",
    "mdtheorem",
    "newtcbtheorem",
    "NewTcbTheorem",
    "spnewtheorem",
    "def",
    "gdef",
    "edef",
    "xdef",
}


def _definition(text: str, command: str, start: int) -> tuple[int, str, str] | None:
    """Locate a declaration without treating its replacement text as a claim."""
    cursor = start
    if command in {"def", "gdef", "edef", "xdef"}:
        macro = COMMAND.match(text, cursor)
        if macro is None:
            return None
        brace = text.find("{", macro.end())
        result = group(text, brace) if brace >= 0 else None
        return (result[1], macro.group(1), result[0]) if result else None
    option = group(text, cursor, "[")
    if option:
        cursor = option[1]
    first = group(text, cursor)
    if first is None:
        macro = COMMAND.match(text, cursor)
        if macro is None:
            return None
        first = ("\\" + macro.group(1), macro.end())
    name, cursor = first
    if command == "declaretheorem":
        return cursor, name.strip(), option[0] if option else ""
    for _ in range(2):
        option = group(text, cursor, "[")
        if option:
            cursor = option[1]
    body = group(text, cursor)
    if body is None:
        return None
    cursor = body[1]
    if command in {"newenvironment", "renewenvironment"}:
        following = group(text, cursor)
        if following is None:
            return None
        cursor = following[1]
    return cursor, name.strip().lstrip("\\"), body[0]


def scan_tex(source: SourceText) -> TexScan:
    """Mask comments, literal blocks, definitions, and definitely inactive branches."""
    text, visible = source.text, list(source.text)
    marks: list[tuple[int, int, str, str]] = []
    definitions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    stack: list[tuple[bool | None, bool, int]] = []
    boolean_setters: set[str] = set()

    def mark(start: int, end: int, disposition: str, reason: str, mask: bool = True) -> None:
        marks.append((start, end, disposition, reason))
        if mask:
            for index in range(start, end):
                if visible[index] not in "\r\n":
                    visible[index] = " "

    def diagnostic(code: str, message: str, start: int, end: int) -> None:
        diagnostics.append(
            {
                "code": code,
                "message": message,
                "file": source.file,
                "blocking": True,
                "span": source.span(start, end),
            }
        )

    pos = 0
    while pos < len(text):
        if text[pos] == "%":
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            mark(pos, end, "non_mathematical", "TeX comment")
            pos = end
            continue
        command = COMMAND.match(text, pos)
        inactive = any(value is not None and value == otherwise for value, otherwise, _ in stack)
        uncertain = any(value is None for value, _, _ in stack)
        if command is None:
            end = pos + 1
            while end < len(text) and text[end] not in "%\\":
                end += 1
            if inactive:
                mark(pos, end, "excluded", "Definitely inactive conditional branch")
            elif uncertain:
                mark(pos, end, "unresolved", "Conditional branch requires evaluation", False)
            pos = end
            continue
        name = command.group(1).rstrip("*")
        end = command.end()
        if name == "newif":
            cursor = end
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            declared = COMMAND.match(text, cursor)
            if declared and declared[1].startswith("if") and len(declared[1]) > 2:
                end = declared.end()
                if not inactive:
                    base = declared[1][2:]
                    for macro in (declared[1], base + "true", base + "false"):
                        definitions.append(
                            {
                                "id": identity("macro", source.file + ":" + macro + ":" + str(pos)),
                                "name": macro,
                                "command": "newif",
                                "body": "",
                                "span": source.span(pos, end),
                                "uncertain": uncertain,
                            }
                        )
                    boolean_setters.update({base + "true", base + "false"})
                mark(
                    pos,
                    end,
                    "excluded" if inactive else "supporting_context",
                    "Definitely inactive conditional branch" if inactive else "Boolean declaration",
                )
            else:
                diagnostic("unparsed_definition", "Cannot bound \\newif declaration", pos, end)
        elif name in boolean_setters:
            # Recognize declarations/setters without pretending to execute TeX scoping
            # or macro expansion. A later custom conditional still needs reconciliation.
            mark(
                pos,
                end,
                "excluded" if inactive else "supporting_context",
                "Definitely inactive conditional branch" if inactive else "Boolean assignment",
            )
        elif name.startswith("if") and name not in {
            "iff",
            "ifthenelse",
            "ifdef",
            "ifstrempty",
            "ifstrequal",
        }:
            value = {"iftrue": True, "iffalse": False}.get(name)
            stack.append((value, False, pos))
            mark(pos, end, "supporting_context", "Conditional delimiter")
            if value is None and not inactive:
                diagnostic(
                    "unresolved_conditional", f"Cannot evaluate \\{name} lexically", pos, end
                )
        elif name == "else" and stack:
            value, _, begin = stack[-1]
            stack[-1] = value, True, begin
            mark(pos, end, "supporting_context", "Conditional delimiter")
        elif name == "fi" and stack:
            stack.pop()
            mark(pos, end, "supporting_context", "Conditional delimiter")
        elif name in DEFINITION_COMMANDS:
            definition = _definition(text, name, end)
            if definition:
                end, macro, body = definition
                definitions.append(
                    {
                        "id": identity(
                            "macro", source.file + ":" + macro + ":" + str(len(definitions))
                        ),
                        "name": macro,
                        "command": name,
                        "body": body,
                        "span": source.span(pos, end),
                        "uncertain": uncertain,
                    }
                )
                if inactive:
                    definitions.pop()
                mark(pos, end, "supporting_context", "Macro or environment definition")
                if inactive:
                    mark(pos, end, "excluded", "Definitely inactive conditional branch")
            else:
                diagnostic("unparsed_definition", f"Cannot bound \\{name} declaration", pos, end)
        elif inactive:
            mark(pos, end, "excluded", "Definitely inactive conditional branch")
        elif name in {"verb", "lstinline"} and end < len(text):
            delimiter = text[end]
            closing = text.find(delimiter, end + 1)
            end = len(text) if closing < 0 else closing + 1
            mark(pos, end, "unresolved", "Literal inline material requires scope audit")
        elif name == "begin":
            env = group(text, end)
            if env and env[0] in {"comment", "verbatim", "Verbatim", "lstlisting", "minted"}:
                closing_match = re.search(r"\\end\s*\{" + re.escape(env[0]) + r"\}", text[env[1] :])
                end = env[1] + closing_match.end() if closing_match else len(text)
                disposition = "non_mathematical" if env[0] == "comment" else "unresolved"
                mark(
                    pos,
                    end,
                    disposition,
                    (
                        "Comment environment"
                        if env[0] == "comment"
                        else "Literal block requires scope audit"
                    ),
                )
                if closing_match is None:
                    diagnostic("unclosed_environment", f"Unclosed {env[0]} environment", pos, end)
        elif name == "endinput":
            mark(pos, len(text), "excluded", "Material after endinput is not processed")
            end = len(text)
        elif name == "end" and not uncertain:
            env = group(text, end)
            if env and env[0] == "document":
                mark(
                    env[1],
                    len(text),
                    "excluded",
                    "Material after the document end is not processed",
                )
                end = len(text)
        elif name in {
            "catcode",
            "csname",
            "expandafter",
            "scantokens",
            "inputencoding",
            "let",
            "futurelet",
            "unless",
        }:
            diagnostic(
                "unsupported_expansion",
                f"\\{name} requires structural or manual reconciliation",
                pos,
                end,
            )
        elif name in {"else", "fi"}:
            diagnostic("unmatched_conditional", f"Unmatched \\{name}", pos, end)
        if uncertain and not inactive and not any(a <= pos and end <= b for a, b, _, _ in marks):
            mark(pos, end, "unresolved", "Conditional branch requires evaluation", False)
        pos = end
    for _, _, start in stack:
        diagnostic("unclosed_conditional", "Conditional is not closed", start, len(text))
    return TexScan(source, "".join(visible), marks, definitions, diagnostics)
