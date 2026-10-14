"""Add dedicated formalization build membership while retaining existing configuration text and defaults."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained
from leanflow_cli.workflows.project_setup.scaffold import scaffold


def _toml_mask(text: str) -> str:
    """Mask strings and comments so array/table boundaries cannot come from literal content."""
    result = list(text)
    index = 0
    while index < len(text):
        character = text[index]
        if character == "#":
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
        elif character in {"'", '"'}:
            delimiter = character * (3 if text.startswith(character * 3, index) else 1)
            end = index + len(delimiter)
            while end < len(text):
                if character == '"' and text[end] == "\\":
                    end += 2
                elif text.startswith(delimiter, end):
                    end += len(delimiter)
                    break
                else:
                    end += 1
        else:
            index += 1
            continue
        for position in range(index, min(end, len(text))):
            if text[position] not in "\r\n":
                result[position] = " "
        index = end
    return "".join(result)


def _default_target(text: str, target: str) -> str:
    """Append a root target with a bounded text edit and verify all original defaults survive."""
    parsed = tomllib.loads(text)
    defaults = parsed.get("defaultTargets", [])
    if not isinstance(defaults, list) or any(not isinstance(name, str) for name in defaults):
        raise ValueError("Lake defaultTargets must be a static array of target names")
    if target in defaults:
        return text
    mask = _toml_mask(text)
    depth, offset, first_table = 0, 0, len(text)
    assignment: int | None = None
    for line in text.splitlines(keepends=True):
        masked = mask[offset : offset + len(line)]
        if depth == 0:
            if masked.lstrip().startswith("["):
                first_table = offset
                break
            matched = re.match(
                r"\s*(?:defaultTargets|\"defaultTargets\"|'defaultTargets')\s*=", line
            )
            if matched and masked[matched.end() - 1] == "=":
                assignment = offset + matched.end()
                break
        depth += sum(character in "[{" for character in masked)
        depth -= sum(character in "]}" for character in masked)
        offset += len(line)
    if assignment is None:
        if "defaultTargets" in parsed:
            raise ValueError("Lake defaultTargets representation needs a field-span adapter")
        prefix = text[:first_table]
        prefix += "" if not prefix or prefix.endswith("\n") else "\n"
        result = prefix + "defaultTargets = [" + json.dumps(target) + "]\n" + text[first_table:]
    else:
        start = assignment
        while start < len(mask) and mask[start].isspace():
            start += 1
        if start >= len(mask) or mask[start] != "[":
            raise ValueError("Lake defaultTargets requires a literal array")
        end, level = start + 1, 1
        while end < len(mask) and level:
            level += int(mask[end] == "[") - int(mask[end] == "]")
            end += 1
        if level:
            raise ValueError("Lake defaultTargets array is incomplete")
        close = end - 1
        separator = "," if defaults and not mask[start + 1 : close].rstrip().endswith(",") else ""
        result = text[:close] + separator + " " + json.dumps(target) + text[close:]
    if tomllib.loads(result) != {**parsed, "defaultTargets": [*defaults, target]}:
        raise ValueError("Dedicated library registration changed unrelated Lake configuration")
    return result


def configure_layout(
    stage: Path, root: Path, plan: dict[str, Any], baseline: dict[str, str | None]
) -> None:
    """Create import-only roots and include dedicated libraries in the ordinary Lake build."""
    if not (stage / "lakefile.toml").exists() and not (stage / "lakefile.lean").exists():
        if not (stage / "Main.lean").exists():
            (stage / "Main.lean").write_text(
                "import Mathlib\n" if plan["preset"] == "mathlib" else "import Init\n"
            )
            baseline["Main.lean"] = None
    scaffold(stage, name=root.name, preset=plan["preset"], source_file=None, changed=[])
    layout = plan["layout"]
    root_file = contained(stage, layout["root_file"])
    if layout["mode"] == "dedicated_library":
        configuration = stage / (
            "lakefile.lean" if (stage / "lakefile.lean").is_file() else "lakefile.toml"
        )
        content = configuration.read_text()
        if configuration.suffix == ".toml":
            content = _default_target(content, layout["library"])
            content += (
                "\n[[lean_lib]]\nname = "
                + json.dumps(layout["library"])
                + '\nsrcDir = "."\nroots = '
                + json.dumps([layout["root_module"]])
                + "\n"
            )
        else:
            content += (
                "\n@[default_target]\nlean_lib "
                + layout["library"]
                + " where\n"
                + '  srcDir := "."\n  roots := #[\x60'
                + layout["root_module"]
                + "]\n"
            )
        configuration.write_text(content)
    if not root_file.exists():
        root_file.parent.mkdir(parents=True, exist_ok=True)
        root_file.write_text("import Mathlib\n" if plan["preset"] == "mathlib" else "import Init\n")
        baseline[layout["root_file"]] = None
