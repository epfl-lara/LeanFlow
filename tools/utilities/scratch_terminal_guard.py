"""Restrict scratch research terminals to deterministic read-only commands.

Process-isolated research workers share the user's project checkout.  Their
``scratch_only`` contract therefore has to be enforced below the model and
delegation layers: prompts and filtered file tools cannot prevent a terminal
command from editing the same checkout.  This module parses the small shell
surface that remains useful for diagnostics and rejects everything else.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class ScratchTerminalDecision:
    """Describe whether one scratch-only terminal request may execute."""

    allowed: bool
    reason: str = ""
    command: str = ""
    workdir: str = ""


_READ_ONLY_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "basename",
        "cat",
        "cmp",
        "comm",
        "cut",
        "df",
        "diff",
        "diff3",
        "dirname",
        "du",
        "file",
        "grep",
        "head",
        "ls",
        "md5",
        "md5sum",
        "pgrep",
        "ps",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sha1sum",
        "sha256sum",
        "shasum",
        "stat",
        "tail",
        "tr",
        "uname",
        "wc",
        "which",
    }
)

_SHELL_CONTROL_TOKENS: Final[frozenset[str]] = frozenset(
    {"&", "&&", ";", ";;", "<", "<<", "<<<", ">", ">>", "&>", "|&", "(", ")"}
)
_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FIND_MUTATING_PREFIXES: Final[tuple[str, ...]] = (
    "-delete",
    "-exec",
    "-execdir",
    "-fprint",
    "-fprintf",
    "-fls",
    "-ok",
    "-okdir",
)
_GIT_READ_ONLY_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "cat-file",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "name-rev",
        "rev-parse",
        "shortlog",
        "show",
        "status",
    }
)
_GIT_EXEC_OR_WRITE_OPTIONS: Final[tuple[str, ...]] = (
    "--config-env",
    "--exec-path",
    "--ext-diff",
    "--no-index",
    "--open-files-in-pager",
    "--output",
    "--paginate",
    "--textconv",
)
_LEAN_WRITE_OR_EXEC_OPTIONS: Final[tuple[str, ...]] = (
    "--bc",
    "--c",
    "--load-dynlib",
    "--o",
    "--plugin",
    "--run",
    "--server",
    "--setup",
    "--stdin",
    "--worker",
    "--root",
    "-R",
    "-b",
    "-c",
    "-i",
    "-o",
)
_PATH_OPTIONS_BY_COMMAND: Final[dict[str, frozenset[str]]] = {
    "diff": frozenset({"--exclude-from"}),
    "file": frozenset({"--magic-file", "-m"}),
    "find": frozenset({"-files0-from"}),
    "grep": frozenset({"--exclude-from", "--file", "-f"}),
    "jq": frozenset({"--from-file", "--library-path", "-L", "-f"}),
    "pgrep": frozenset({"--logpidfile", "--pidfile", "-F", "-L"}),
    "rg": frozenset({"--file", "--ignore-file", "-f"}),
}
_IMPLICIT_CWD_READ_COMMANDS: Final[frozenset[str]] = frozenset({"du", "find", "ls", "rg"})
_SECOND_ORDER_READ_OR_EXEC_OPTIONS: Final[dict[str, tuple[str, ...]]] = {
    # These modes interpret file contents as more paths, so confining the
    # option file itself is insufficient: a project-local list can name an
    # arbitrary host file.
    "du": ("--files0-from",),
    "file": (
        "-f",
        "--files-from",
        "-z",
        "-Z",
        "--uncompress",
        "--uncompress-noreport",
    ),
    "find": ("-files0-from",),
    "md5sum": ("-c", "--check"),
    "sha1sum": ("-c", "--check"),
    "sha256sum": ("-c", "--check"),
    "shasum": ("-c", "--check"),
    "wc": ("--files0-from",),
    # diff3 may otherwise execute an arbitrary comparison program.
    "diff3": ("--diff-program",),
    # Follow/retry modes turn a bounded diagnostic into a persistent watcher;
    # --pid also exposes a host-process synchronization surface.
    "tail": ("-f", "-F", "--follow", "--pid", "--retry"),
}
_PS_FORMAT_FIELDS: Final[frozenset[str]] = frozenset(
    {"comm", "etime", "etimes", "pgid", "pid", "ppid", "sid", "stat", "state"}
)
_SYMLINK_FOLLOW_SHORT_FLAGS: Final[dict[str, frozenset[str]]] = {
    "du": frozenset({"H", "L"}),
    "file": frozenset({"L"}),
    "find": frozenset({"H", "L"}),
    "grep": frozenset({"R"}),
    "ls": frozenset({"L"}),
    "rg": frozenset({"L"}),
}
_SYMLINK_FOLLOW_LONG_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--dereference",
        "--dereference-args",
        "--dereference-command-line",
        "--dereference-command-line-symlink-to-dir",
        "--dereference-recursive",
        "--follow",
        "-follow",
    }
)


def _deny(reason: str) -> ScratchTerminalDecision:
    """Return one stable denied-command verdict."""
    return ScratchTerminalDecision(False, reason)


def _option_present(tokens: list[str], options: tuple[str, ...]) -> str:
    """Return the first exact or assignment-style forbidden option."""
    for token in tokens:
        for option in options:
            if token == option or token.startswith(option + "="):
                return option
            if (
                option.startswith("-")
                and not option.startswith("--")
                and token.startswith(option)
                and len(token) > len(option)
            ):
                return option
    return ""


def _path_within(path: str, root: str, *, relative_to: str) -> bool:
    """Return whether a possibly relative path resolves beneath one root."""
    try:
        resolved_root = Path(root).expanduser().resolve(strict=False)
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(relative_to).expanduser() / candidate
        candidate.resolve(strict=False).relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _looks_like_path(value: str) -> bool:
    """Return whether one shell token syntactically names a path."""
    return bool(
        value in {".", "..", "~"}
        or value.startswith(("./", "../", "~/", "/"))
        or "/" in value
        or "\\" in value
    )


def _existing_path(value: str, *, effective_workdir: str) -> bool:
    """Return whether a bare operand resolves to an existing file or symlink."""
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path(effective_workdir) / candidate
        return candidate.exists() or candidate.is_symlink()
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_read_paths(
    tokens: list[str],
    *,
    project_root: str,
    effective_workdir: str,
) -> ScratchTerminalDecision:
    """Keep all user-selected read operands inside the assigned project."""
    if not project_root:
        return _deny("scratch read commands require an assigned project root")
    executable = tokens[0]
    path_options = _PATH_OPTIONS_BY_COMMAND.get(executable, frozenset())
    expects_path = False
    saw_read_path = False
    for token in tokens[1:]:
        candidates: list[str] = []
        if expects_path:
            candidates.append(token)
            expects_path = False
        elif token in path_options:
            expects_path = True
            continue
        else:
            matched_attached = False
            for option in path_options:
                if token.startswith(option + "="):
                    candidates.append(token.partition("=")[2])
                    matched_attached = True
                    break
                if (
                    option.startswith("-")
                    and not option.startswith("--")
                    and token.startswith(option)
                    and len(token) > len(option)
                ):
                    candidates.append(token[len(option) :])
                    matched_attached = True
                    break
            if not matched_attached:
                value = token.partition("=")[2] if "=" in token else token
                if _looks_like_path(value) or (
                    not token.startswith("-")
                    and _existing_path(value, effective_workdir=effective_workdir)
                ):
                    candidates.append(value)
        for candidate in candidates:
            if not candidate or candidate == "-":
                continue
            saw_read_path = True
            if not _path_within(
                candidate,
                project_root,
                relative_to=effective_workdir,
            ):
                return _deny("read operands must remain inside the assigned project")
            try:
                from tools.utilities.repository_research_policy import (
                    clean_room_terminal_path_block_reason,
                )

                clean_room_reason = clean_room_terminal_path_block_reason(
                    candidate,
                    cwd=effective_workdir,
                )
            except Exception:
                clean_room_reason = ""
            if clean_room_reason:
                return _deny(clean_room_reason)
    if expects_path:
        return _deny("a read-path option is missing its project-local operand")
    if executable in _IMPLICIT_CWD_READ_COMMANDS and not saw_read_path:
        try:
            from tools.utilities.repository_research_policy import (
                clean_room_terminal_path_block_reason,
            )

            clean_room_reason = clean_room_terminal_path_block_reason(
                effective_workdir,
                cwd=effective_workdir,
            )
        except Exception:
            clean_room_reason = ""
        if clean_room_reason:
            return _deny(clean_room_reason)
    return ScratchTerminalDecision(True)


def _validate_ps(tokens: list[str]) -> ScratchTerminalDecision:
    """Allow only PID/topology process views without arguments or environments."""
    args = tokens[1:]
    expect: str = ""
    allowed_switches = {"-A", "-a", "-ax", "-e", "-o", "-p", "-x", "-ao", "-axo", "-eo"}
    for token in args:
        if expect == "pid":
            if not all(part.isdigit() for part in token.split(",") if part):
                return _deny("ps -p accepts only numeric process identifiers")
            expect = ""
            continue
        if expect == "format":
            fields = {part.strip().rstrip("=").lower() for part in token.split(",") if part.strip()}
            if not fields or not fields.issubset(_PS_FORMAT_FIELDS):
                return _deny("ps output is limited to PID and process-topology fields")
            expect = ""
            continue
        if token.isdigit():
            continue
        if token not in allowed_switches:
            return _deny("ps arguments and environment display modes are not allowed")
        if token == "-p":
            expect = "pid"
        elif token == "-o" or token.endswith("o"):
            expect = "format"
    if expect:
        return _deny(f"ps option requires a {expect} operand")
    return ScratchTerminalDecision(True)


def _validate_pgrep(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject pgrep modes that print full process arguments."""
    for token in tokens[1:]:
        if token in {"-a", "--list-full"} or (
            token.startswith("-") and not token.startswith("--") and "a" in token[1:]
        ):
            return _deny("pgrep may not display full process arguments")
    return ScratchTerminalDecision(True)


def _validate_no_symlink_follow(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject traversal modes that can escape through a project-local symlink."""
    forbidden_flags = _SYMLINK_FOLLOW_SHORT_FLAGS.get(tokens[0], frozenset())
    for token in tokens[1:]:
        if token in _SYMLINK_FOLLOW_LONG_OPTIONS:
            return _deny("symlink-following read modes may escape the assigned project")
        if token.startswith("-") and not token.startswith("--"):
            short_flags = token[1:]
            if any(flag in short_flags for flag in forbidden_flags):
                return _deny("symlink-following read modes may escape the assigned project")
    return ScratchTerminalDecision(True)


def _validate_no_second_order_reads_or_exec(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject modes whose apparently local input can select host paths or code."""
    forbidden = _option_present(
        tokens[1:],
        _SECOND_ORDER_READ_OR_EXEC_OPTIONS.get(tokens[0], ()),
    )
    if forbidden:
        return _deny(
            f"{tokens[0]} option {forbidden} can select additional paths, execute helpers, "
            "or outlive a bounded diagnostic"
        )
    return ScratchTerminalDecision(True)


def _validate_git(tokens: list[str]) -> ScratchTerminalDecision:
    """Allow only Git porcelain/plumbing operations without write or exec modes."""
    args = tokens[1:]
    if not args:
        return _deny("git requires an explicitly read-only subcommand")
    subcommand_index = next(
        (index for index, token in enumerate(args) if not token.startswith("-")),
        -1,
    )
    if subcommand_index < 0:
        return _deny("git requires an explicitly read-only subcommand")
    global_options = args[:subcommand_index]
    if any(token == "-C" or token.startswith("-C") for token in global_options):
        return _deny("git -C may escape the assigned project directory")
    if any(token == "-c" or token.startswith("-c") for token in global_options):
        return _deny("git configuration overrides are not allowed")
    allowed_global_options = {
        "--glob-pathspecs",
        "--icase-pathspecs",
        "--literal-pathspecs",
        "--no-pager",
        "--noglob-pathspecs",
    }
    unexpected_global = next(
        (token for token in global_options if token not in allowed_global_options),
        "",
    )
    if unexpected_global:
        return _deny(f"git global option {unexpected_global} is not allowed")
    forbidden = _option_present(args, _GIT_EXEC_OR_WRITE_OPTIONS)
    if forbidden:
        return _deny(f"git option {forbidden} can execute helpers or write output")
    if any(
        token in {"-O", "-h", "--filters", "--help", "--show-signature"}
        or token.startswith("-O")
        or token.startswith("--filters=")
        for token in args[subcommand_index + 1 :]
    ):
        return _deny("git helper, filter, pager, and help execution modes are not allowed")

    subcommand = args[subcommand_index]
    if subcommand not in _GIT_READ_ONLY_SUBCOMMANDS:
        return _deny(f"git subcommand {subcommand or '(missing)'} is not read-only")
    return ScratchTerminalDecision(True)


def _validate_find(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject find actions that delete, execute, or open output files."""
    for token in tokens[1:]:
        if token.startswith(_FIND_MUTATING_PREFIXES):
            return _deny(f"find action {token} is not read-only")
    return ScratchTerminalDecision(True)


def _validate_ripgrep(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject ripgrep's external preprocessor escape hatch."""
    for token in tokens[1:]:
        if token == "--pre" or token.startswith("--pre="):
            return _deny("ripgrep preprocessors may execute arbitrary commands")
    return ScratchTerminalDecision(True)


def _validate_file(tokens: list[str]) -> ScratchTerminalDecision:
    """Reject file(1)'s compiled-magic output mode."""
    if any(
        token == "-C" or token.startswith("-C") or token.startswith("--compile")
        for token in tokens[1:]
    ):
        return _deny("file --compile writes a magic database")
    return ScratchTerminalDecision(True)


def _validate_lean(
    tokens: list[str],
    *,
    project_root: str,
    effective_workdir: str,
) -> ScratchTerminalDecision:
    """Allow Lean elaboration while rejecting output, plugin, and run modes."""
    args = tokens[1:]
    forbidden = _option_present(args, _LEAN_WRITE_OR_EXEC_OPTIONS)
    if forbidden:
        return _deny(f"Lean option {forbidden} may write output or execute a program")
    for token in args:
        if token.startswith(("-R", "-b", "-c", "-i", "-o")) and token not in {
            "-R",
            "-b",
            "-c",
            "-i",
            "-o",
        }:
            return _deny(f"Lean option {token[:2]} may write compiler output")

    # Lean source arguments are the only user-controlled programs accepted by
    # this guard.  Keep them inside the assigned project so ``../../`` and
    # symlink escapes cannot elaborate an arbitrary host script.
    if project_root:
        for token in args:
            if token.startswith("-"):
                continue
            if not _path_within(token, project_root, relative_to=effective_workdir):
                return _deny("Lean input paths must remain inside the assigned project")
    return ScratchTerminalDecision(True)


def _validate_segment(
    tokens: list[str],
    *,
    project_root: str,
    effective_workdir: str,
) -> ScratchTerminalDecision:
    """Validate one pipeline segment against the read-only command surface."""
    if not tokens:
        return _deny("empty pipeline segment")
    executable = tokens[0]
    if _ASSIGNMENT_RE.match(executable):
        return _deny("environment assignments and command wrappers are not allowed")
    if "/" in executable or "\\" in executable:
        return _deny("commands must use a known executable name, not a path")

    path_decision = _validate_read_paths(
        tokens,
        project_root=project_root,
        effective_workdir=effective_workdir,
    )
    if not path_decision.allowed:
        return path_decision
    symlink_decision = _validate_no_symlink_follow(tokens)
    if not symlink_decision.allowed:
        return symlink_decision
    secondary_decision = _validate_no_second_order_reads_or_exec(tokens)
    if not secondary_decision.allowed:
        return secondary_decision

    if executable == "git":
        return _validate_git(tokens)
    if executable == "find":
        return _validate_find(tokens)
    if executable == "rg":
        return _validate_ripgrep(tokens)
    if executable == "file":
        return _validate_file(tokens)
    if executable == "ps":
        return _validate_ps(tokens)
    if executable == "pgrep":
        return _validate_pgrep(tokens)
    if executable == "lean":
        return _validate_lean(
            tokens,
            project_root=project_root,
            effective_workdir=effective_workdir,
        )
    if executable == "lake":
        if len(tokens) < 3 or tokens[1:3] != ["env", "lean"]:
            return _deny("lake is allowed only as `lake env lean` for elaboration")
        return _validate_lean(
            ["lean", *tokens[3:]],
            project_root=project_root,
            effective_workdir=effective_workdir,
        )
    if executable not in _READ_ONLY_COMMANDS:
        return _deny(f"command {executable} is outside the read-only diagnostic allowlist")
    return ScratchTerminalDecision(True)


def _render_segment(tokens: list[str], *, executable: str) -> str:
    """Render one validated segment with deterministic read-only Git modes."""
    resolved_tokens = [executable, *tokens[1:]]
    if tokens[0] == "rg":
        # Ignore a host RIPGREP_CONFIG_PATH that could otherwise reintroduce
        # the external ``--pre`` escape hatch after validation.
        resolved_tokens.insert(1, "--no-config")
    if tokens[0] != "git":
        return shlex.join(resolved_tokens)

    subcommand = next(token for token in tokens[1:] if token in _GIT_READ_ONLY_SUBCOMMANDS)
    hardened = resolved_tokens
    if "--no-pager" not in hardened[1:]:
        hardened.insert(1, "--no-pager")
    hardened[2:2] = ["-c", "core.fsmonitor=false"]
    subcommand_index = hardened.index(subcommand)
    if subcommand in {"diff", "log", "show"}:
        hardened[subcommand_index + 1 : subcommand_index + 1] = [
            "--no-ext-diff",
            "--no-textconv",
        ]
    if subcommand == "status":
        hardened.insert(subcommand_index + 1, "--ignore-submodules=all")
    return f"GIT_OPTIONAL_LOCKS=0 {shlex.join(hardened)}"


def validate_scratch_terminal_command(
    command: str,
    *,
    workdir: str = "",
    project_root: str = "",
) -> ScratchTerminalDecision:
    """Return whether a scratch worker may execute one terminal command.

    Only pipelines composed entirely of audited read-only commands are
    accepted.  Shell chaining, redirects, substitutions, wrappers, background
    jobs, and arbitrary interpreters fail closed.  The terminal boundary calls
    this function before creating an execution environment, and user approval
    or ``force=True`` cannot override it.
    """
    text = str(command or "").strip()
    if not text:
        return _deny("empty commands are not useful diagnostics")
    if "\x00" in text or "\n" in text or "\r" in text:
        return _deny("multiline shell input is not allowed")
    if "`" in text or "$" in text:
        return _deny("shell expansion and substitution are not allowed")

    effective_root = str(project_root or "").strip()
    effective_workdir = str(workdir or effective_root or os.getcwd()).strip()
    if effective_root and not _path_within(
        effective_workdir,
        effective_root,
        relative_to=effective_root,
    ):
        return _deny("working directory must remain inside the assigned project")
    try:
        canonical_workdir = Path(effective_workdir).expanduser()
        if not canonical_workdir.is_absolute():
            canonical_workdir = Path(effective_root or os.getcwd()).expanduser() / canonical_workdir
        effective_workdir = str(canonical_workdir.resolve(strict=False))
    except (OSError, RuntimeError):
        return _deny("working directory could not be resolved safely")

    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        parsed = list(lexer)
    except ValueError:
        return _deny("command contains malformed shell quoting")
    if not parsed:
        return _deny("empty commands are not useful diagnostics")

    segments: list[list[str]] = [[]]
    for token in parsed:
        if token == "|":
            if not segments[-1]:
                return _deny("empty pipeline segment")
            segments.append([])
            continue
        if token in _SHELL_CONTROL_TOKENS or set(token) <= {"&", ";", "<", ">", "(", ")"}:
            return _deny(f"shell control or redirection token {token!r} is not allowed")
        segments[-1].append(token)
    if not segments[-1]:
        return _deny("empty pipeline segment")

    for segment in segments:
        decision = _validate_segment(
            segment,
            project_root=effective_root,
            effective_workdir=effective_workdir,
        )
        if not decision.allowed:
            return decision

    # Re-render parsed arguments instead of executing the model's original
    # shell spelling. This preserves the one audited pipeline operator while
    # quoting wildcard/whitespace/comment characters so the shell cannot turn
    # a filename into an unvalidated option or command fragment. Git status
    # receives the documented no-optional-locks mode, preventing an otherwise
    # read-only query from refreshing the shared index.
    rendered_segments: list[str] = []
    for segment in segments:
        executable = str(shutil.which(segment[0]) or "").strip()
        if not executable:
            return _deny(f"read-only executable {segment[0]} is unavailable")
        if effective_root and _path_within(
            executable,
            effective_root,
            relative_to=effective_workdir,
        ):
            return _deny("project-local executables are not trusted in scratch terminals")
        rendered_segments.append(_render_segment(segment, executable=executable))
    return ScratchTerminalDecision(
        True,
        command=" | ".join(rendered_segments),
        workdir=effective_workdir,
    )


__all__ = ["ScratchTerminalDecision", "validate_scratch_terminal_command"]
