"""Admit static Lake templates and preserve a typed set of compiler options."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.source import lean_code_mask

_BOOLEAN_OPTIONS = {
    "autoImplicit",
    "relaxedAutoImplicit",
    "pp.unicode.fun",
    "weak.linter.mathlibStandardSet",
}
_NATURAL_OPTIONS = {"maxSynthPendingDepth", "maxRecDepth", "maxHeartbeats"}
_STRING = r'"(?:[^"\\\n]|\\["\\nrt])*"'
_NAME = r"[A-Za-z_][A-Za-z_0-9]*"
_IDENT = rf"(?:{_NAME}|«{_NAME}»)"
_QUOTED_NAME = rf"`{_IDENT}(?:\.{_IDENT})*"


def validate_options(value: Any) -> dict[str, bool | int]:
    """Accept only explicitly supported elaborator/diagnostic options and exact types."""
    if not isinstance(value, dict):
        raise ValueError("Lean options must be a static object")
    result: dict[str, bool | int] = {}
    for name, option in value.items():
        valid = (name in _BOOLEAN_OPTIONS and type(option) is bool) or (
            name in _NATURAL_OPTIONS and type(option) is int and option >= 0
        )
        if not valid:
            raise ValueError(f"Unsupported Lean compiler option: {name}")
        result[name] = option
    return result


def _toml_options(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten TOML dotted option names into Lean's actual option-name map."""
    result: dict[str, Any] = {}
    for name, child in value.items():
        key = prefix + name
        if isinstance(child, dict):
            result.update(_toml_options(child, key + "."))
        else:
            result[key] = child
    return result


def static_configuration(path: Path, content: str | None = None) -> None:
    """Reject executable settings before evaluating a bounded static Lake configuration."""
    content = path.read_text(encoding="utf-8") if content is None else content
    if path.suffix == ".toml":
        config = tomllib.loads(content)
        allowed = {
            "name",
            "version",
            "keywords",
            "defaultTargets",
            "srcDir",
            "roots",
            "globs",
            "root",
            "buildDir",
            "leanLibDir",
            "buildType",
            "leanOptions",
            "lean_lib",
            "lean_exe",
            "require",
            "scope",
            "git",
            "rev",
            "path",
            "subDir",
        }

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "buildType" and child != "release":
                        raise ValueError(
                            "Non-release Lake buildType requires an explicit compiler-option adapter"
                        )
                    if key not in allowed:
                        raise ValueError(f"Unsupported dynamic Lake setting: {key}")
                    if key == "leanOptions":
                        if not isinstance(child, dict):
                            raise ValueError("Lean options must be a static object")
                        validate_options(_toml_options(child))
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(config)
        return
    # Preserve literal strings, but remove comments using the shared Lean lexer.
    text = lean_code_mask(content, preserve_literals=True)
    option_array = re.compile(r"leanOptions\s*:=\s*#\[(.*?)\]", re.S)
    for match in option_array.finditer(text):
        values: dict[str, bool | int] = {}
        remaining = match[1].strip()
        entry = re.compile(
            r"⟨\s*`([A-Za-z_][\w.]*)\s*,\s*(true|false|(?:\.ofNat\s+)?[0-9]+)\s*⟩\s*,?\s*"
        )
        while remaining:
            parsed = entry.match(remaining)
            if parsed is None:
                raise ValueError("Lean options require literal named boolean/natural entries")
            name, raw = parsed.groups()
            values[name] = (
                raw == "true"
                if raw in {"true", "false"}
                else int(raw.removeprefix(".ofNat").strip())
            )
            remaining = remaining[parsed.end() :]
        validate_options(values)
    text = option_array.sub("", text)
    for line in text.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"import Lake|open (?:Lake |Lean |DSL\s*)+", line):
            continue
        if line == "@[default_target]" or re.fullmatch(
            rf"(?:@\[default_target\]\s*)?(?:package|lean_lib|lean_exe)\s+(?:{_IDENT}|{_STRING})(?:\s+where)?",
            line,
        ):
            continue
        if re.fullmatch(
            rf"require\s+(?:{_NAME}\s+from\s+git\s+{_STRING}(?:\s*@\s*{_STRING})?|{_STRING}\s*/\s*{_STRING}\s*@\s*git\s+{_STRING})",
            line,
        ):
            continue
        if re.fullmatch(r'version\s*:=\s*v!"[0-9]+\.[0-9]+\.[0-9]+"', line):
            continue
        if re.fullmatch(rf"keywords\s*:=\s*#\[\s*(?:{_STRING}\s*,?\s*)*\]", line):
            continue
        if re.fullmatch(r"buildType\s*:=\s*\.release", line):
            continue
        if ":=" in line:
            key, value = (part.strip() for part in line.split(":=", 1))
            if key in {"srcDir", "buildDir", "leanLibDir"} and re.fullmatch(_STRING, value):
                continue
            if key == "root" and re.fullmatch(_QUOTED_NAME, value):
                continue
            name_entry = _QUOTED_NAME
            if key == "globs":
                name_entry = rf"(?:\.(?:one|submodules|andSubmodules)\s+)?{_QUOTED_NAME}"
            if key in {"roots", "globs", "defaultTargets"} and re.fullmatch(
                rf"#\[\s*(?:{name_entry}\s*,?\s*)*\]", value
            ):
                continue
        raise ValueError(f"Unsupported dynamic Lake configuration line: {line[:100]}")
