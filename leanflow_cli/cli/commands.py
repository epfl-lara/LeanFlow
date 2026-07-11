"""Slash command definitions and completion for the LeanFlow shell."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from prompt_toolkit.completion import Completer, Completion


@dataclass(frozen=True)
class WorkflowCommandSpec:
    """Single source of truth for one LeanFlow workflow slash command.

    ``command`` is the canonical frontend slash command (e.g. ``/prove``).
    ``aliases`` are additional long-form frontend slash commands that route to the
    same workflow (e.g. ``/autoprove``). ``workflow_kind`` / ``backend_command`` mirror
    the routing tuple consumed by the native runner, and ``description`` is the help
    text shown in the shell command help.
    """

    command: str
    workflow_kind: str
    backend_command: str
    description: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


# The canonical, ordered registry of workflow commands. Every workflow routing table
# in the CLI (COMMANDS_BY_CATEGORY["Workflow"], main.WORKFLOW_COMMANDS,
# workflow.WORKFLOW_ALIAS_MAP, workflow.FORGIVING_WORKFLOW_ALIAS_MAP) is DERIVED from
# this single structure so the command surface stays consistent in one place.
COMMAND_REGISTRY: tuple[WorkflowCommandSpec, ...] = (
    WorkflowCommandSpec("/draft", "draft", "/draft", "Run the Lean draft workflow"),
    WorkflowCommandSpec("/review", "review", "/review", "Run the Lean review workflow"),
    WorkflowCommandSpec("/refactor", "refactor", "/refactor", "Run the Lean refactor workflow"),
    WorkflowCommandSpec("/golf", "golf", "/golf", "Run the Lean proof golfing workflow"),
    WorkflowCommandSpec(
        "/prove",
        "prove",
        "/prove",
        "Run the autonomous Lean proving workflow; add --agents N for explicit swarm mode",
        aliases=("/autoprove",),
    ),
    WorkflowCommandSpec(
        "/formalize",
        "formalize",
        "/formalize",
        "Formalize a project-local .tex/.pdf document; add --agents N for explicit swarm mode",
        aliases=("/autoformalize",),
    ),
)


def _workflow_category_commands() -> dict[str, str]:
    """Build the Workflow help section (canonical commands only) from the registry."""
    return {spec.command: spec.description for spec in COMMAND_REGISTRY}


def build_workflow_alias_map() -> dict[str, tuple[str, str, str]]:
    """Derive the frontend-command -> (kind, canonical, backend) routing table.

    Canonical commands are emitted first (in registry order), then their long-form
    aliases, preserving the historical iteration order of ``WORKFLOW_ALIAS_MAP``.
    """
    alias_map: dict[str, tuple[str, str, str]] = {}
    for spec in COMMAND_REGISTRY:
        alias_map[spec.command] = (spec.workflow_kind, spec.command, spec.backend_command)
    for spec in COMMAND_REGISTRY:
        for alias in spec.aliases:
            alias_map[alias] = (spec.workflow_kind, spec.command, spec.backend_command)
    return alias_map


def build_forgiving_workflow_alias_map() -> dict[str, str]:
    """Derive the slash-less-name -> canonical-slash-command forgiving routing table."""
    forgiving: dict[str, str] = {}
    for spec in COMMAND_REGISTRY:
        forgiving[spec.command[1:]] = spec.command
        for alias in spec.aliases:
            forgiving[alias[1:]] = spec.command
    return forgiving


def build_workflow_command_set() -> set[str]:
    """Derive the set of all frontend workflow slash commands (canonical + aliases)."""
    commands: set[str] = set()
    for spec in COMMAND_REGISTRY:
        commands.add(spec.command)
        commands.update(spec.aliases)
    return commands


COMMANDS_BY_CATEGORY = {
    "Project": {
        "/project": "Initialize, create, or inspect the active LeanFlow project",
        "/cd": "Change the shell working directory",
        "/pwd": "Show the current shell working directory",
    },
    "Workflow": _workflow_category_commands(),
    "Runtime": {
        "/status": "Show the current project, provider, model, and runtime summary",
        "/kill": "Interrupt a workflow agent by id",
        "/swarm": "List workflow agents, inspect one agent, or kill an active agent",
        "/workflow": "Inspect the latest managed workflow status, history, activity, or run log",
        "/goals": "Show the latest persisted Lean goals from the managed workflow",
        "/diagnostics": "Show the latest persisted Lean diagnostics from the managed workflow",
        "/proof-state": "Show the latest persisted full Lean proof-state snapshot",
        "/provider": "Show the resolved provider or check a requested provider",
        "/models": "Manage local runtimes such as vllm, ollama, or llama.cpp",
        "/doctor": "Run local environment and provider checks",
        "/config": "Show, read, or write config values",
    },
    "Skills": {
        "/skills": "List curated LeanFlow skills and active overlay sources",
        "/skill": "Activate a skill, inspect the active skill, or reload overlays",
    },
    "Session": {
        "/banner": "Show the startup banner again",
        "/clear": "Clear the screen and redraw the shell banner",
        "/help": "Show command help",
        "/quit": "Exit the shell",
    },
}

COMMANDS: dict[str, str] = {}
for commands in COMMANDS_BY_CATEGORY.values():
    COMMANDS.update(commands)


class SlashCommandCompleter(Completer):
    """Autocomplete built-in slash commands and path-like arguments."""

    def __init__(
        self,
        skill_commands_provider: Callable[[], Mapping[str, dict[str, str]]] | None = None,
    ) -> None:
        self._skill_commands_provider = skill_commands_provider

    def _iter_skill_commands(self) -> Mapping[str, dict[str, str]]:
        if self._skill_commands_provider is None:
            return {}
        try:
            return self._skill_commands_provider() or {}
        except Exception:
            return {}

    @staticmethod
    def _completion_text(cmd_name: str, word: str) -> str:
        return f"{cmd_name} " if cmd_name == word else cmd_name

    @staticmethod
    def _extract_path_word(text: str) -> str | None:
        if not text:
            return None
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1 :]
        if not word:
            return None
        if word.startswith(("./", "../", "~/", "/")) or "/" in word:
            return word
        return None

    @staticmethod
    def _path_completions(word: str, limit: int = 30):
        expanded = os.path.expanduser(word)
        if expanded.endswith("/"):
            search_dir = expanded
            prefix = ""
        else:
            search_dir = os.path.dirname(expanded) or "."
            prefix = os.path.basename(expanded)

        try:
            entries = os.listdir(search_dir)
        except OSError:
            return

        count = 0
        prefix_lower = prefix.lower()
        for entry in sorted(entries):
            if prefix and not entry.lower().startswith(prefix_lower):
                continue
            if count >= limit:
                break

            full_path = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full_path)
            if word.startswith("~"):
                display_path = "~/" + os.path.relpath(full_path, os.path.expanduser("~"))
            elif os.path.isabs(word):
                display_path = full_path
            else:
                display_path = os.path.relpath(full_path)
            if is_dir:
                display_path += "/"

            yield Completion(
                display_path,
                start_position=-len(word),
                display=entry + ("/" if is_dir else ""),
                display_meta="dir" if is_dir else "",
            )
            count += 1

    def get_completions(self, document, complete_event):
        """Yield slash-command and path completions for the prompt_toolkit shell. Completes built-in and skill commands with descriptions when text starts with '/', otherwise completes file paths if detected in the trailing word."""
        text = document.text_before_cursor
        if not text.startswith("/"):
            path_word = self._extract_path_word(text)
            if path_word is not None:
                yield from self._path_completions(path_word)
            return

        word = text[1:]
        for cmd, desc in COMMANDS.items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=desc,
                )
        for cmd, info in self._iter_skill_commands().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "LeanFlow skill"))
                source = str(info.get("source", "") or "")
                display_meta = f"{description} [{source}]" if source else description
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=display_meta,
                )
