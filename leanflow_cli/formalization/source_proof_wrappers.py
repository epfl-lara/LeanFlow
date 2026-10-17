"""Expose literal custom proof wrappers as source candidates without asserting TeX execution."""

from __future__ import annotations

import re
from typing import Any

from .source_provenance import COMMAND, group
from .source_tex import TexBundle

_BEGIN = re.compile(r"\\begin\s*\{proof\}")
_END = re.compile(r"\\end\s*\{proof\}")


def _lexical_comments(text: str) -> str:
    """Read ordinary percent comments only for a provisional wrapper shape, never masking sources."""
    result = []
    pos = 0
    while pos < len(text):
        if text[pos] == "\\":
            command = COMMAND.match(text, pos)
            assert command is not None
            result.append(command[0])
            pos = command.end()
        elif text[pos] == "%":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end
        else:
            result.append(text[pos])
            pos += 1
    return "".join(result)


def _shape(text: str) -> tuple[str, int] | None:
    """Recognize only a literal newenvironment alias with a plain optional proof title."""
    text = _lexical_comments(text)
    command = COMMAND.match(text)
    if command is None or command[1] != "newenvironment":
        return None
    name = group(text, command.end())
    if name is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\*?", name[0]):
        return None
    cursor, arity = name[1], 0
    arguments = group(text, cursor, "[")
    if arguments:
        if arguments[0] not in {"0", "1"}:
            return None
        arity, cursor = int(arguments[0]), arguments[1]
        if group(text, cursor, "["):
            return None
    opening = group(text, cursor)
    closing = group(text, opening[1]) if opening else None
    if opening is None or closing is None or text[closing[1] :].strip():
        return None
    begin = _BEGIN.match(opening[0].strip())
    if begin is None or _END.fullmatch(closing[0].strip()) is None:
        return None
    suffix = opening[0].strip()[begin.end() :]
    title = group(suffix, 0, "[")
    if title:
        if suffix[title[1] :].strip() or "\\" in title[0]:
            return None
        parameters = re.findall(r"#(.)", title[0])
        if any(value != "1" or arity != 1 for value in parameters):
            return None
        if "#" in re.sub(r"#1", "", title[0]):
            return None
    elif suffix.strip():
        return None
    return name[0], arity


def proof_wrappers(bundle: TexBundle) -> dict[str, dict[str, Any]]:
    """Retain exact unambiguous wrapper declarations for independent source review."""
    definitions = [definition for scan in bundle.scans.values() for definition in scan.definitions]
    if any(definition["name"] in {"proof", "endproof"} for definition in definitions):
        return {}
    counts: dict[str, int] = {}
    for definition in definitions:
        counts[definition["name"]] = counts.get(definition["name"], 0) + 1
    result = {}
    for definition in definitions:
        if definition["command"] != "newenvironment" or counts[definition["name"]] != 1:
            continue
        span = definition["span"]
        source = bundle.scans[span["file"]].source
        original = source.raw[span["byte_start"] : span["byte_end"]].decode(source.encoding)
        shape = _shape(original)
        if shape is None or shape[0] != definition["name"]:
            continue
        result[shape[0]] = {
            "definition_id": definition["id"],
            "source_span": span,
            "original_definition": original,
            "argument_count": shape[1],
            "association_authority": "lexical-candidate-requires-independent-review",
        }
    return result


def wrapper_candidates(
    bundle: TexBundle, expanded: str, blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep literal bounded invocations and any explicit reference without evaluating arguments."""
    wrappers = proof_wrappers(bundle)
    result = []
    for block in blocks:
        wrapper = wrappers.get(block["environment"])
        if wrapper is None:
            continue
        cursor = block["body_start"]
        arguments = []
        for _ in range(wrapper["argument_count"]):
            argument = group(expanded, cursor)
            if argument is None or argument[1] > block["body_end"]:
                break
            arguments.append(argument[0])
            cursor = argument[1]
        else:
            target = (
                re.fullmatch(r"\s*\\ref\s*\{([^{}]+)\}\s*", arguments[0]) if arguments else None
            )
            # Macro arguments can execute arbitrary effects. Retain only literal
            # text or a sole label reference as a candidate attachment hint.
            if arguments and ("\\" in arguments[0] or "#" in arguments[0]) and target is None:
                continue
            result.append(
                {
                    **block,
                    "wrapper": wrapper,
                    "explicit_target_label": target[1] if target else None,
                }
            )
    return result
