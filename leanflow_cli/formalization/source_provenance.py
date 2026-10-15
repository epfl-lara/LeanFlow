"""Preserve original source coordinates while masking nonexecuted TeX material."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .source_builtins import LATEX_BUILTINS
from .source_numeric_conditionals import (
    LITERAL_ENVIRONMENTS,
    SCANNER_CONTROL_NAMES,
    LiteralTexNumbers,
)


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
    includeonly_definitions: list[tuple[int, int, frozenset[str] | None]] = field(
        default_factory=list
    )
    conditional_events: list[tuple[int, int, str, bool | None]] = field(default_factory=list)


COMMAND = re.compile(r"\\([a-zA-Z@]+\*?|.)", re.DOTALL)
MAX_CONDITIONAL_EVENTS = 100000
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
    "DeclareMathOperator",
}


def _unbalanced_conditional_controls(declaration: str, numbers: LiteralTexNumbers) -> bool:
    """Detect controls that escape a declaration when TeX skips rather than executes it."""
    depth = 0
    for match in COMMAND.finditer(declaration):
        name = match[1]
        if numbers.is_conditional(name):
            depth += 1
        elif name in {"else", "or", "fi"}:
            if depth == 0:
                return True
            if name == "fi":
                depth -= 1
    return depth != 0


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


def scan_tex(source: SourceText, *, inherited_uncertain: bool = False) -> TexScan:
    """Mask comments, literal blocks, definitions, and definitely inactive branches."""
    text, visible = source.text, list(source.text)
    marks: list[tuple[int, int, str, str]] = []
    definitions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    includeonly_definitions: list[tuple[int, int, frozenset[str] | None]] = []
    conditional_events: list[tuple[int, int, str, bool | None]] = []
    conditional_indices: dict[int, int] = {}
    conditional_events_limited = False
    stack: list[tuple[bool | None, bool, int]] = []
    numbers = LiteralTexNumbers()
    structural_uncertain = inherited_uncertain

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

    def conditional_event(start: int, end: int, kind: str, value: bool | None) -> None:
        """Bound delimiter metadata before allocation and retain a strict overflow diagnostic."""
        nonlocal conditional_events_limited
        if len(conditional_events) >= MAX_CONDITIONAL_EVENTS:
            if not conditional_events_limited:
                diagnostic(
                    "environment_structure_limit",
                    "Conditional delimiter metadata exceeds its materialization bound",
                    start,
                    end,
                )
                conditional_events_limited = True
            return
        if kind in {"if", "ifcase"}:
            conditional_indices[start] = len(conditional_events)
        conditional_events.append((start, end, kind, value))

    def lose_structure(
        code: str, message: str, start: int, end: int, *, report: bool = True
    ) -> None:
        """Retain all later candidates once executable control meanings are unknown."""
        nonlocal structural_uncertain
        structural_uncertain = True
        numbers.trusted = False
        for index, (_, otherwise, begin) in enumerate(stack):
            stack[index] = None, otherwise, begin
            if begin in conditional_indices:
                event_index = conditional_indices[begin]
                first, last, kind, _ = conditional_events[event_index]
                conditional_events[event_index] = first, last, kind, None
        if report:
            diagnostic(code, message, start, end)
        mark(start, len(text), "unresolved", message, False)

    if inherited_uncertain:
        lose_structure(
            "inherited_tex_meanings",
            "Included source inherits executable TeX meanings which require reconciliation",
            0,
            len(text),
        )
    caret = text.find("^^")
    if caret >= 0:
        lose_structure(
            "unsupported_tex_preprocessing",
            "TeX caret preprocessing precedes tokenization; preserve source without guessed decoding",
            caret,
            caret + 2,
        )

    pos = 0
    while pos < len(text):
        if text[pos] == "%" and not structural_uncertain:
            line_end = re.search(r"\r\n|\r|\n", text[pos:])
            end = pos + line_end.start() if line_end else len(text)
            mark(pos, end, "non_mathematical", "TeX comment")
            pos = end
            continue
        command = COMMAND.match(text, pos)
        inactive = any(value is not None and value == otherwise for value, otherwise, _ in stack)
        uncertain = structural_uncertain or any(value is None for value, _, _ in stack)
        if command is None:
            end = pos + 1
            while end < len(text) and text[end] not in "%\\":
                end += 1
            if inactive:
                mark(pos, end, "excluded", "Definitely inactive conditional branch")
            elif uncertain:
                mark(pos, end, "unresolved", "Conditional branch requires evaluation", False)
            if not inactive:
                numbers.text_groups(text[pos:end], uncertain=uncertain)
            pos = end
            continue
        name = command.group(1).rstrip("*")
        end = command.end()
        if inactive:
            # TeX's skipped-token loop does not execute definitions or consume
            # their replacement arguments. Only known conditional tokens nest.
            if numbers.is_conditional(name):
                stack.append((None, False, pos))
                conditional_event(pos, end, "if", False)
            elif name == "else" and stack:
                value, _, begin = stack[-1]
                stack[-1] = value, True, begin
                conditional_event(pos, end, "else", None)
            elif name == "fi" and stack:
                stack.pop()
                conditional_event(pos, end, "fi", None)
            mark(pos, end, "excluded", "Definitely inactive conditional branch")
            pos = end
            continue
        if name in SCANNER_CONTROL_NAMES and name in numbers.macros and not inactive:
            numbers.command(name, text, end)
            mark(
                pos,
                end,
                "unresolved",
                "Source-redefined scanner control requires reconciliation",
                False,
            )
            pos = end
            continue
        if name == "newif":
            if uncertain and not structural_uncertain:
                lose_structure(
                    "unresolved_definition_effect",
                    "Conditional declaration may be executed or skipped; retain all later candidates",
                    pos,
                    end,
                )
            cursor = end
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            declared = COMMAND.match(text, cursor)
            if declared and declared[1].startswith("if") and len(declared[1]) > 2:
                end = declared.end()
                if not inactive:
                    base = declared[1][2:]
                    for macro in (declared[1], base + "true", base + "false"):
                        numbers.define(
                            macro,
                            "",
                            "newif",
                            "",
                            uncertain=uncertain,
                            conditional_token=macro == declared[1],
                            boolean_setter=macro != declared[1],
                        )
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
                mark(
                    pos,
                    end,
                    "excluded" if inactive else "supporting_context",
                    "Definitely inactive conditional branch" if inactive else "Boolean declaration",
                )
            else:
                lose_structure("unparsed_definition", "Cannot bound \\newif declaration", pos, end)
        elif numbers.is_boolean_setter(name):
            # Recognize declarations/setters without pretending to execute TeX scoping
            # or macro expansion. A later custom conditional still needs reconciliation.
            mark(
                pos,
                end,
                "excluded" if inactive else "supporting_context",
                "Definitely inactive conditional branch" if inactive else "Boolean assignment",
            )
        elif numbers.is_conditional(name):
            value = (
                {"iftrue": True, "iffalse": False}.get(name) if not structural_uncertain else None
            )
            if name == "ifnum" and not inactive:
                comparison = numbers.conditional(text, end)
                if comparison is not None:
                    value, end = comparison
            stack.append((value, False, pos))
            conditional_event(pos, end, "ifcase" if name == "ifcase" else "if", value)
            mark(pos, end, "supporting_context", "Conditional delimiter")
            if value is None and not inactive:
                diagnostic(
                    "unresolved_conditional", f"Cannot evaluate \\{name} lexically", pos, end
                )
        elif name.startswith("if") and name not in {
            "iff",
            "ifthenelse",
            "ifdef",
            "ifstrempty",
            "ifstrequal",
        }:
            lose_structure(
                "unresolved_conditional",
                f"Unknown command \\{name} cannot be assumed to be a conditional token",
                pos,
                end,
            )
        elif name == "else" and stack:
            value, previous_else, begin = stack[-1]
            if previous_else:
                value = None
                diagnostic(
                    "duplicate_conditional_else",
                    "Conditional contains more than one else",
                    pos,
                    end,
                )
            stack[-1] = value, True, begin
            conditional_event(pos, end, "else", None)
            mark(pos, end, "supporting_context", "Conditional delimiter")
        elif name == "fi" and stack:
            stack.pop()
            conditional_event(pos, end, "fi", None)
            mark(pos, end, "supporting_context", "Conditional delimiter")
        elif name == "or":
            conditional_event(pos, end, "or", None)
            diagnostic(
                "unsupported_environment_conditional",
                "Case conditional alternatives require unsupported structural semantics",
                pos,
                end,
            )
        elif name in DEFINITION_COMMANDS:
            if uncertain and not structural_uncertain:
                lose_structure(
                    "unresolved_definition_effect",
                    "Definition may be executed or skipped; retain all later candidates",
                    pos,
                    end,
                )
            definition = _definition(text, name, end)
            if definition:
                end, macro, body = definition
                if any(value is None for value, _, _ in stack) and _unbalanced_conditional_controls(
                    text[command.end() : end], numbers
                ):
                    diagnostic(
                        "unsupported_environment_conditional",
                        "Unbalanced conditional controls inside a possibly skipped definition require unsupported structural semantics",
                        pos,
                        end,
                    )
                if not inactive:
                    brace = text.find("{", command.end())
                    head = text[command.end() : brace] if brace >= 0 else ""
                    numbers.define(
                        macro,
                        body,
                        name,
                        head,
                        uncertain=uncertain,
                        declaration=text[command.end() : end],
                    )
                    if macro in SCANNER_CONTROL_NAMES:
                        lose_structure(
                            "redefined_scanner_control",
                            f"Source redefines scanner control \\{macro}; structural interpretation is uncertain",
                            pos,
                            end,
                        )
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
                mark(
                    pos,
                    end,
                    "supporting_context",
                    "Macro or environment definition",
                    not structural_uncertain,
                )
                if inactive:
                    mark(pos, end, "excluded", "Definitely inactive conditional branch")
            else:
                lose_structure(
                    "unparsed_definition", f"Cannot bound \\{name} declaration", pos, end
                )
        elif inactive:
            mark(pos, end, "excluded", "Definitely inactive conditional branch")
        elif name in {"verb", "lstinline"} and end < len(text) and not uncertain:
            delimiter = text[end]
            closing = text.find(delimiter, end + 1)
            end = len(text) if closing < 0 else closing + 1
            mark(pos, end, "unresolved", "Literal inline material requires scope audit")
        elif name == "begin":
            env = group(text, end)
            if (
                env
                and env[0] in LITERAL_ENVIRONMENTS
                and (env[0] in numbers.macros or "end" + env[0] in numbers.macros)
            ):
                mark(
                    pos,
                    env[1],
                    "unresolved",
                    "Source-redefined literal environment requires reconciliation",
                    False,
                )
                end = env[1]
            elif env and env[0] in LITERAL_ENVIRONMENTS and not uncertain:
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
            elif env:
                if env[0] in numbers.macros:
                    numbers.command(env[0])
                numbers.enter("environment:" + env[0], uncertain=uncertain)
        elif name == "begingroup":
            numbers.enter("explicit", uncertain=uncertain)
        elif name == "endgroup":
            numbers.leave("explicit", uncertain=uncertain)
        elif name in {"(", "["}:
            numbers.enter("math:" + name, uncertain=uncertain)
        elif name in {")", "]"}:
            numbers.leave("math:" + {")": "(", "]": "["}[name], uncertain=uncertain)
        elif name in {"documentclass", "usepackage", "RequirePackage"}:
            numbers.loader()
            lose_structure(
                "unresolved_loader_effect",
                f"\\{name} loads executable TeX meanings; later structure requires reconciliation",
                pos,
                end,
            )
        elif name == "includeonly":
            selection = group(text, end)
            allowed = (
                frozenset(value.strip().removesuffix(".tex") for value in selection[0].split(","))
                if selection
                and not uncertain
                and not numbers.groups
                and name not in numbers.macros
                and re.fullmatch(r"[A-Za-z0-9_./, \t\r\n-]*", selection[0])
                else None
            )
            includeonly_definitions.append((pos, selection[1] if selection else end, allowed))
        elif name in {"input", "include", "subfile"}:
            lose_structure(
                "inherited_tex_meanings",
                f"\\{name} can change caller TeX meanings; resumed source requires reconciliation",
                pos,
                end,
            )
        elif name == "endinput":
            if not uncertain:
                line_end = re.search(r"\r\n|\r|\n", text[end:])
                if line_end:
                    following_line = end + line_end.end()
                    mark(
                        following_line,
                        len(text),
                        "excluded",
                        "Material after endinput's current line is not processed",
                    )
                    text = text[:following_line]
                mark(pos, end, "supporting_context", "Endinput finishes the current physical line")
        elif name == "end":
            env = group(text, end)
            if env:
                if "end" + env[0] in numbers.macros:
                    numbers.command("end" + env[0])
                numbers.leave("environment:" + env[0], uncertain=uncertain)
            if env and env[0] == "document" and not uncertain:
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
            numbers.command(name)
            diagnostic(
                "unsupported_expansion",
                f"\\{name} requires structural or manual reconciliation",
                pos,
                end,
            )
        elif name in {"else", "fi"}:
            diagnostic("unmatched_conditional", f"Unmatched \\{name}", pos, end)
        else:
            numbers.command(name, text, end)
        if not numbers.trusted and not structural_uncertain:
            lose_structure(
                "unsupported_tex_effect",
                f"\\{name} has unsupported TeX effects; later structure requires reconciliation",
                pos,
                end,
                # Unknown commands already get one aggregate, source-bound
                # diagnostic from command_references. Local/builtin effects
                # need a separate blocker because those references are known.
                report=name in numbers.macros or name in LATEX_BUILTINS or not name.isalpha(),
            )
        if uncertain and not inactive and not any(a <= pos and end <= b for a, b, _, _ in marks):
            mark(pos, end, "unresolved", "Conditional branch requires evaluation", False)
        pos = end
    for _, _, start in stack:
        diagnostic("unclosed_conditional", "Conditional is not closed", start, len(text))
    return TexScan(
        source,
        "".join(visible),
        marks,
        definitions,
        diagnostics,
        includeonly_definitions,
        conditional_events,
    )
