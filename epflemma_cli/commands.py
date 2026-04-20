"""Slash command definitions and completion for the EPFLemma shell."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from prompt_toolkit.completion import Completer, Completion


COMMANDS_BY_CATEGORY = {
    "Project": {
        "/project": "Initialize, create, or inspect the active EPFLemma project",
        "/cd": "Change the shell working directory",
        "/pwd": "Show the current shell working directory",
    },
    "Workflow": {
        "/draft": "Run the Lean draft workflow",
        "/review": "Run the Lean review workflow",
        "/checkpoint": "Run the Lean checkpoint workflow",
        "/refactor": "Run the Lean refactor workflow",
        "/golf": "Run the Lean proof golfing workflow",
        "/prove": "Run the autonomous Lean proving workflow; add --agents N for explicit swarm mode",
        "/formalize": "Run the autonomous Lean formalization workflow; add --agents N for explicit swarm mode",
    },
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
        "/skills": "List curated EPFLemma skills and active overlay sources",
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
                description = str(info.get("description", "EPFLemma skill"))
                source = str(info.get("source", "") or "")
                display_meta = f"{description} [{source}]" if source else description
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=display_meta,
                )
