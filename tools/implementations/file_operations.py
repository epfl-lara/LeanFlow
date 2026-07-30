#!/usr/bin/env python3
"""
File Operations Module

Provides file manipulation capabilities (read, write, patch, search) that work
across all terminal backends (local, docker, singularity, ssh, modal, daytona).

The key insight is that all file operations can be expressed as shell commands,
so we wrap the terminal backend's execute() interface to provide a unified file API.

Usage:
    from tools.implementations.file_operations import ShellFileOperations
    from tools.implementations.terminal_tool import _active_environments

    # Get file operations for a terminal environment
    file_ops = ShellFileOperations(terminal_env)

    # Read a file
    result = file_ops.read_file("/path/to/file.py")

    # Write a file
    result = file_ops.write_file("/path/to/new.py", "print('hello')")

    # Search for content
    result = file_ops.search("TODO", path=".", file_glob="*.py")
"""

import contextlib
import difflib
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.utilities.workflow_artifact_guard import (
    diagnostic_workflow_file_access_enabled,
    is_leanflow_internal_path,
    workflow_log_read_error,
    workflow_state_search_error,
)

_RG_FIELD_SEPARATOR = ":::LEANFLOW-RG-FIELD:::"


def _explicit_package_dependency_search(path: str) -> bool:
    """Return whether a search root explicitly enters Lake's package cache."""
    parts = Path(os.path.normpath(os.path.expanduser(str(path or "")))).parts
    return any(
        parts[index] == ".lake" and parts[index + 1] == "packages"
        for index in range(len(parts) - 1)
    )


# ---------------------------------------------------------------------------
# Write-path deny list — blocks writes to sensitive system/credential files
# ---------------------------------------------------------------------------

_HOME = str(Path.home())

WRITE_DENIED_PATHS = {
    os.path.realpath(p)
    for p in [
        os.path.join(_HOME, ".ssh", "authorized_keys"),
        os.path.join(_HOME, ".ssh", "id_rsa"),
        os.path.join(_HOME, ".ssh", "id_ed25519"),
        os.path.join(_HOME, ".ssh", "config"),
        os.path.join(_HOME, ".leanflow", ".env"),
        os.path.join(_HOME, ".bashrc"),
        os.path.join(_HOME, ".zshrc"),
        os.path.join(_HOME, ".profile"),
        os.path.join(_HOME, ".bash_profile"),
        os.path.join(_HOME, ".zprofile"),
        os.path.join(_HOME, ".netrc"),
        os.path.join(_HOME, ".pgpass"),
        os.path.join(_HOME, ".npmrc"),
        os.path.join(_HOME, ".pypirc"),
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
    ]
}

WRITE_DENIED_PREFIXES = [
    os.path.realpath(p) + os.sep
    for p in [
        os.path.join(_HOME, ".ssh"),
        os.path.join(_HOME, ".aws"),
        os.path.join(_HOME, ".gnupg"),
        os.path.join(_HOME, ".kube"),
        "/etc/sudoers.d",
        "/etc/systemd",
    ]
]


def _is_write_denied(path: str) -> bool:
    """Return True if path is on the write deny list."""
    resolved = os.path.realpath(os.path.expanduser(path))
    if resolved in WRITE_DENIED_PATHS:
        return True
    for prefix in WRITE_DENIED_PREFIXES:
        if resolved.startswith(prefix):
            return True
    return False


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class ReadResult:
    """Result from reading a file."""

    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: str | None = None
    is_binary: bool = False
    is_image: bool = False
    error: str | None = None
    similar_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != []}


@dataclass
class WriteResult:
    """Result from writing a file."""

    bytes_written: int = 0
    dirs_created: bool = False
    error: str | None = None
    warning: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class PatchResult:
    """Result from patching a file."""

    success: bool = False
    diff: str = ""
    files_modified: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    lint: dict[str, Any] | None = None
    error: str | None = None
    # Observability (F2): which fuzzy strategy matched and how confidently.
    # A low-similarity block_anchor/context_aware hit is then distinguishable
    # from a clean exact match in results and logs.
    matched_via: str | None = None
    similarity: float | None = None
    # Soft read-before-edit nudge (D2): set when the file was edited without
    # having been read first. Non-fatal — the edit still applied.
    freshness_warning: str | None = None

    def to_dict(self) -> dict:
        result = {"success": self.success}
        if self.diff:
            result["diff"] = self.diff
        if self.files_modified:
            result["files_modified"] = self.files_modified
        if self.files_created:
            result["files_created"] = self.files_created
        if self.files_deleted:
            result["files_deleted"] = self.files_deleted
        if self.lint:
            result["lint"] = self.lint
        if self.matched_via:
            result["matched_via"] = self.matched_via
        if self.similarity is not None:
            result["similarity"] = self.similarity
        if self.freshness_warning:
            result["freshness_warning"] = self.freshness_warning
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class SearchMatch:
    """A single search match."""

    path: str
    line_number: int
    content: str
    mtime: float = 0.0  # Modification time for sorting


@dataclass
class SearchResult:
    """Result from searching."""

    matches: list[SearchMatch] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    total_count: int = 0
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        result = {"total_count": self.total_count}
        if self.matches:
            result["matches"] = [
                {"path": m.path, "line": m.line_number, "content": m.content} for m in self.matches
            ]
        if self.files:
            result["files"] = self.files
        if self.counts:
            result["counts"] = self.counts
        if self.truncated:
            result["truncated"] = True
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class LintResult:
    """Result from linting a file."""

    success: bool = True
    skipped: bool = False
    output: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "message": self.message}
        return {"status": "ok" if self.success else "error", "output": self.output}


@dataclass
class ExecuteResult:
    """Result from executing a shell command."""

    stdout: str = ""
    exit_code: int = 0


# =============================================================================
# Abstract Interface
# =============================================================================


class FileOperations(ABC):
    """Abstract interface for file operations across terminal backends."""

    @abstractmethod
    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> ReadResult:
        """Read a file with pagination support."""
        ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> WriteResult:
        """Write content to a file, creating directories as needed."""
        ...

    @abstractmethod
    def patch_replace(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> PatchResult:
        """Replace text in a file using fuzzy matching."""
        ...

    @abstractmethod
    def patch_v4a(self, patch_content: str, strict: bool = False) -> PatchResult:
        """Apply a V4A format patch (strict=exact-or-fail for high-risk edits)."""
        ...

    @abstractmethod
    def search(
        self,
        pattern: str,
        path: str = ".",
        target: str = "content",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
    ) -> SearchResult:
        """Search for content or files."""
        ...


# =============================================================================
# Shell-based Implementation
# =============================================================================

# Binary file extensions (fast path check)
BINARY_EXTENSIONS = {
    # Images
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".tiff",
    ".tif",
    ".svg",  # SVG is text but often treated as binary
    # Audio/Video
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".mkv",
    ".flac",
    ".ogg",
    ".webm",
    # Archives
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    # Compiled/Binary
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".o",
    ".a",
    ".pyc",
    ".pyo",
    ".class",
    ".wasm",
    ".bin",
    # Fonts
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    # Other
    ".db",
    ".sqlite",
    ".sqlite3",
}

# Image extensions (subset of binary that we can return as base64)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}

# Linters by file extension
LINTERS = {
    ".py": "python -m py_compile {file} 2>&1",
    ".js": "node --check {file} 2>&1",
    ".ts": "npx tsc --noEmit {file} 2>&1",
    ".go": "go vet {file} 2>&1",
    ".rs": "rustfmt --check {file} 2>&1",
}

# Max limits for read operations
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_FILE_SIZE = 50 * 1024  # 50KB


class ShellFileOperations(FileOperations):
    """
    File operations implemented via shell commands.

    Works with ANY terminal backend that has execute(command, cwd) method.
    This includes local, docker, singularity, ssh, modal, and daytona environments.
    """

    def __init__(self, terminal_env, cwd: str = None):
        """
        Initialize file operations with a terminal environment.

        Args:
            terminal_env: Any object with execute(command, cwd) method.
                         Returns {"output": str, "returncode": int}
            cwd: Working directory (defaults to env's cwd or current directory)
        """
        self.env = terminal_env
        # Determine cwd from various possible sources.
        # IMPORTANT: do NOT fall back to os.getcwd() -- that's the HOST's local
        # path which doesn't exist inside container/cloud backends (modal, docker).
        # If nothing provides a cwd, use "/" as a safe universal default.
        self.cwd = (
            cwd
            or getattr(terminal_env, "cwd", None)
            or getattr(getattr(terminal_env, "config", None), "cwd", None)
            or "/"
        )

        # Cache for command availability checks
        self._command_cache: dict[str, bool] = {}

    def _exec(
        self, command: str, cwd: str = None, timeout: int = None, stdin_data: str = None
    ) -> ExecuteResult:
        """Execute command via terminal backend.

        Args:
            stdin_data: If provided, piped to the process's stdin instead of
                        embedding in the command string. Bypasses ARG_MAX.
        """
        kwargs = {}
        if timeout:
            kwargs["timeout"] = timeout
        if stdin_data is not None:
            kwargs["stdin_data"] = stdin_data

        result = self.env.execute(command, cwd=cwd or self.cwd, **kwargs)
        return ExecuteResult(stdout=result.get("output", ""), exit_code=result.get("returncode", 0))

    def _has_command(self, cmd: str) -> bool:
        """Check if a command exists in the environment (cached)."""
        if cmd not in self._command_cache:
            result = self._exec(
                f"command -v {self._escape_shell_arg(cmd)} >/dev/null 2>&1 && echo 'yes'"
            )
            self._command_cache[cmd] = result.stdout.strip() == "yes"
        return self._command_cache[cmd]

    def _is_likely_binary(self, path: str, content_sample: str = None) -> bool:
        """
        Check if a file is likely binary.

        Uses extension check (fast) + content analysis (fallback).
        """
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True

        # Content analysis: >30% non-printable chars = binary
        if content_sample:
            if not content_sample:
                return False
            non_printable = sum(
                1 for c in content_sample[:1000] if ord(c) < 32 and c not in "\n\r\t"
            )
            return non_printable / min(len(content_sample), 1000) > 0.30

        return False

    def _is_image(self, path: str) -> bool:
        """Check if file is an image we can return as base64."""
        ext = os.path.splitext(path)[1].lower()
        return ext in IMAGE_EXTENSIONS

    def _add_line_numbers(self, content: str, start_line: int = 1) -> str:
        """Add line numbers to content in LINE_NUM|CONTENT format."""
        lines = content.split("\n")
        numbered = []
        for i, line in enumerate(lines, start=start_line):
            # Truncate long lines
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "... [truncated]"
            numbered.append(f"{i:6d}|{line}")
        return "\n".join(numbered)

    def _expand_path(self, path: str) -> str:
        """
        Expand shell-style paths like ~ and ~user to absolute paths.

        This must be done BEFORE shell escaping, since ~ doesn't expand
        inside single quotes.
        """
        if not path:
            return path

        # Handle ~ and ~user
        if path.startswith("~"):
            # Get home directory via the terminal environment
            result = self._exec("echo $HOME")
            if result.exit_code == 0 and result.stdout.strip():
                home = result.stdout.strip()
                if path == "~":
                    return home
                elif path.startswith("~/"):
                    return home + path[1:]  # Replace ~ with home
                # ~username format - extract and validate username before
                # letting shell expand it (prevent shell injection via
                # paths like "~; rm -rf /").
                rest = path[1:]  # strip leading ~
                slash_idx = rest.find("/")
                username = rest[:slash_idx] if slash_idx >= 0 else rest
                if username and re.fullmatch(r"[a-zA-Z0-9._-]+", username):
                    # Expand ONLY the validated ~username via the shell; append the (unvalidated)
                    # path remainder in Python so a tail like "~root/$(...)" can't inject. (B3)
                    expand_result = self._exec(f"echo ~{username}")
                    if expand_result.exit_code == 0 and expand_result.stdout.strip():
                        user_home = expand_result.stdout.strip()
                        return user_home + (rest[slash_idx:] if slash_idx >= 0 else "")

        return path

    def _escape_shell_arg(self, arg: str) -> str:
        """Escape a string for safe use in shell commands."""
        # Use single quotes and escape any single quotes in the string
        return "'" + arg.replace("'", "'\"'\"'") + "'"

    def read_raw(self, path: str) -> str | None:
        """Return the raw, undecorated on-disk content, or None if unreadable.

        Used by the read-before-edit freshness check, which must hash the file
        exactly as it sits on disk — not the paginated, line-numbered view.
        """
        path = self._expand_path(path)
        result = self._exec(f"cat {self._escape_shell_arg(path)} 2>/dev/null")
        if result.exit_code != 0:
            return None
        return result.stdout

    def _unified_diff(self, old_content: str, new_content: str, filename: str) -> str:
        """Generate unified diff between old and new content."""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        display_name = str(filename or "")
        try:
            path = Path(display_name).expanduser()
            if path.is_absolute():
                try:
                    display_name = str(
                        path.resolve().relative_to(Path(self.cwd).expanduser().resolve())
                    )
                except Exception:
                    display_name = path.name
        except Exception:
            display_name = display_name.lstrip("/")
        diff = difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{display_name}", tofile=f"b/{display_name}"
        )
        return "".join(diff)

    def _validate_lean_statement_write(self, path: str, content: str) -> str | None:
        """Return an error when a Lean write would alter protected statements."""
        from leanflow_cli.lean.lean_statement_guard import (
            should_guard_lean_statement_path,
            validate_lean_statement_edit,
        )

        if not should_guard_lean_statement_path(path):
            return None

        read_result = self._exec(f"cat {self._escape_shell_arg(path)} 2>/dev/null")
        if read_result.exit_code != 0:
            return None

        guard_result = validate_lean_statement_edit(read_result.stdout, content)
        if guard_result.ok:
            return None
        return guard_result.error

    # =========================================================================
    # READ Implementation
    # =========================================================================

    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> ReadResult:
        """
        Read a file with pagination, binary detection, and line numbers.

        Args:
            path: File path (absolute or relative to cwd)
            offset: Line number to start from (1-indexed, default 1)
            limit: Maximum lines to return (default 500, max 2000)

        Returns:
            ReadResult with content, metadata, or error info
        """
        guard_error = workflow_log_read_error(path)
        if guard_error:
            return ReadResult(error=guard_error)

        # Expand ~ and other shell paths
        path = self._expand_path(path)

        # Clamp limit
        limit = min(limit, MAX_LINES)

        # Check if file exists and get size (wc -c is POSIX, works on Linux + macOS)
        stat_cmd = f"wc -c < {self._escape_shell_arg(path)} 2>/dev/null"
        stat_result = self._exec(stat_cmd)

        if stat_result.exit_code != 0:
            # File not found - try to suggest similar files
            return self._suggest_similar_files(path)

        try:
            file_size = int(stat_result.stdout.strip())
        except ValueError:
            file_size = 0

        # Check if file is too large
        if file_size > MAX_FILE_SIZE:
            # Still try to read, but warn
            pass

        # Image content cannot be displayed as text; report metadata only.
        if self._is_image(path):
            return ReadResult(
                is_image=True,
                is_binary=True,
                file_size=file_size,
                hint="Image file detected. Image contents cannot be displayed as text; reference it by path.",
            )

        # Read a sample to check for binary content
        sample_cmd = f"head -c 1000 {self._escape_shell_arg(path)} 2>/dev/null"
        sample_result = self._exec(sample_cmd)

        if self._is_likely_binary(path, sample_result.stdout):
            return ReadResult(
                is_binary=True,
                file_size=file_size,
                error="Binary file - cannot display as text. Use appropriate tools to handle this file type.",
            )

        # Read with pagination using sed
        end_line = offset + limit - 1
        read_cmd = f"sed -n '{offset},{end_line}p' {self._escape_shell_arg(path)}"
        read_result = self._exec(read_cmd)

        if read_result.exit_code != 0:
            return ReadResult(error=f"Failed to read file: {read_result.stdout}")

        # Get total line count
        wc_cmd = f"wc -l < {self._escape_shell_arg(path)}"
        wc_result = self._exec(wc_cmd)
        try:
            total_lines = int(wc_result.stdout.strip())
        except ValueError:
            total_lines = 0

        # Check if truncated
        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = f"Use offset={end_line + 1} to continue reading (showing {offset}-{end_line} of {total_lines} lines)"

        return ReadResult(
            content=self._add_line_numbers(read_result.stdout, offset),
            total_lines=total_lines,
            file_size=file_size,
            truncated=truncated,
            hint=hint,
        )

    def _suggest_similar_files(self, path: str) -> ReadResult:
        """Suggest similar files when the requested file is not found."""
        # Get directory and filename
        dir_path = os.path.dirname(path) or "."
        filename = os.path.basename(path)

        # List files in directory
        ls_cmd = f"ls -1 {self._escape_shell_arg(dir_path)} 2>/dev/null | head -20"
        ls_result = self._exec(ls_cmd)

        similar = []
        if ls_result.exit_code == 0 and ls_result.stdout.strip():
            files = ls_result.stdout.strip().split("\n")
            # Simple similarity: files that share some characters with the target
            for f in files:
                # Check if filenames share significant overlap
                common = set(filename.lower()) & set(f.lower())
                if len(common) >= len(filename) * 0.5:  # 50% character overlap
                    similar.append(os.path.join(dir_path, f))

        return ReadResult(
            error=f"File not found: {path}",
            similar_files=similar[:5],  # Limit to 5 suggestions
        )

    # =========================================================================
    # WRITE Implementation
    # =========================================================================

    def write_file(self, path: str, content: str) -> WriteResult:
        """Write content, atomically when the selected environment supports it."""
        return self._write_file(path, content, complete_on_interrupt=False)

    def write_file_transactional(self, path: str, content: str) -> WriteResult:
        """Complete one bounded recovery write despite an existing interrupt.

        Managed-artifact reconciliation uses this path for normalization and
        rollback. Remote backends retain their existing shell write behavior.
        """
        return self._write_file(path, content, complete_on_interrupt=True)

    def _write_file(self, path: str, content: str, *, complete_on_interrupt: bool) -> WriteResult:
        """
        Write content to a file, creating parent directories as needed.

        Pipes content through stdin to avoid OS ARG_MAX limits on large
        files. The content never appears in the shell command string —
        only the file path does.

        Args:
            path: File path to write
            content: Content to write

        Returns:
            WriteResult with bytes written or error
        """
        # Expand ~ and other shell paths
        path = self._expand_path(path)

        # Block writes to sensitive paths
        if _is_write_denied(path):
            return WriteResult(
                error=f"Write denied: '{path}' is a protected system/credential file."
            )

        guard_error = self._validate_lean_statement_write(path, content)
        if guard_error:
            return WriteResult(error=guard_error)

        atomic_writer = getattr(self.env, "write_text_atomic", None)
        if getattr(self.env, "supports_atomic_text_writes", False) is True and callable(
            atomic_writer
        ):
            atomic_result = atomic_writer(
                path,
                content,
                cwd=self.cwd,
                complete_on_interrupt=complete_on_interrupt,
            )
            if atomic_result.get("returncode", 0) != 0:
                return WriteResult(error=f"Failed to write file: {atomic_result.get('output', '')}")
            return WriteResult(
                bytes_written=int(atomic_result.get("bytes_written", len(content.encode("utf-8")))),
                dirs_created=bool(atomic_result.get("dirs_created", False)),
            )

        # Create parent directories
        parent = os.path.dirname(path)
        dirs_created = False

        if parent:
            mkdir_cmd = f"mkdir -p {self._escape_shell_arg(parent)}"
            mkdir_result = self._exec(mkdir_cmd)
            if mkdir_result.exit_code == 0:
                dirs_created = True

        # Write via stdin pipe — content bypasses shell arg parsing entirely,
        # so there's no ARG_MAX limit regardless of file size.
        write_cmd = f"cat > {self._escape_shell_arg(path)}"
        write_result = self._exec(write_cmd, stdin_data=content)

        if write_result.exit_code != 0:
            return WriteResult(error=f"Failed to write file: {write_result.stdout}")

        # Get bytes written (wc -c is POSIX, works on Linux + macOS)
        stat_cmd = f"wc -c < {self._escape_shell_arg(path)} 2>/dev/null"
        stat_result = self._exec(stat_cmd)

        try:
            bytes_written = int(stat_result.stdout.strip())
        except ValueError:
            bytes_written = len(content.encode("utf-8"))

        return WriteResult(bytes_written=bytes_written, dirs_created=dirs_created)

    # =========================================================================
    # PATCH Implementation (Replace Mode)
    # =========================================================================

    def patch_replace(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        strict: bool = False,
    ) -> PatchResult:
        """
        Replace text in a file using fuzzy matching.

        Args:
            path: File path to modify
            old_string: Text to find (must be unique unless replace_all=True)
            new_string: Replacement text
            replace_all: If True, replace all occurrences
            strict: When True, only exact/structural matches apply (no fuzzy relocation).

        Returns:
            PatchResult with diff and lint results
        """
        # Expand ~ and other shell paths
        path = self._expand_path(path)

        # Block writes to sensitive paths
        if _is_write_denied(path):
            return PatchResult(
                error=f"Write denied: '{path}' is a protected system/credential file."
            )

        # Read current content
        read_cmd = f"cat {self._escape_shell_arg(path)} 2>/dev/null"
        read_result = self._exec(read_cmd)

        if read_result.exit_code != 0:
            return PatchResult(error=f"Failed to read file: {path}")

        content = read_result.stdout

        # Import and use fuzzy matching (observable variant: also tells us which
        # strategy matched and how confidently, so a low-similarity fuzzy hit is
        # distinguishable from a clean exact match in the result/logs).
        from tools.utilities.fuzzy_match import (
            STRICT_CONFIG,
            fuzzy_find_and_replace_ex,
        )

        match = fuzzy_find_and_replace_ex(
            content,
            old_string,
            new_string,
            replace_all,
            config=STRICT_CONFIG if strict else None,
        )

        if match.error:
            # `match.error` already embeds the near-miss snippet when the chain found a
            # close-but-rejected region, so the failure is actionable, not generic.
            return PatchResult(error=match.error)

        if match.count == 0:
            return PatchResult(error=f"Could not find match for old_string in {path}")

        # Write back
        write_result = self.write_file(path, match.content)
        if write_result.error:
            return PatchResult(error=f"Failed to write changes: {write_result.error}")

        # Generate diff
        diff = self._unified_diff(content, match.content, path)

        # Auto-lint
        lint_result = self._check_lint(path)

        return PatchResult(
            success=True,
            diff=diff,
            files_modified=[path],
            lint=lint_result.to_dict() if lint_result else None,
            matched_via=match.strategy,
            similarity=match.similarity,
        )

    def patch_v4a(self, patch_content: str, strict: bool = False) -> PatchResult:
        """
        Apply a V4A format patch.

        V4A format:
            *** Begin Patch
            *** Update File: path/to/file.py
            @@ context hint @@
             context line
            -removed line
            +added line
            *** End Patch

        Args:
            patch_content: V4A format patch string
            strict: When True, UPDATE hunks are exact-or-fail (no fuzzy relocation).

        Returns:
            PatchResult with changes made
        """
        # Import patch parser
        from tools.utilities.patch_parser import apply_v4a_operations, parse_v4a_patch

        operations, parse_error = parse_v4a_patch(patch_content)
        if parse_error:
            return PatchResult(error=f"Failed to parse patch: {parse_error}")

        # Apply operations
        result = apply_v4a_operations(operations, self, strict=strict)
        return result

    def _check_lint(self, path: str) -> LintResult:
        """
        Run syntax check on a file after editing.

        Args:
            path: File path to lint

        Returns:
            LintResult with status and any errors
        """
        ext = os.path.splitext(path)[1].lower()

        if ext not in LINTERS:
            return LintResult(skipped=True, message=f"No linter for {ext} files")

        # Check if linter command is available
        linter_cmd = LINTERS[ext]
        # Extract the base command (first word)
        base_cmd = linter_cmd.split()[0]

        if not self._has_command(base_cmd):
            return LintResult(skipped=True, message=f"{base_cmd} not available")

        # Run linter
        cmd = linter_cmd.format(file=self._escape_shell_arg(path))
        result = self._exec(cmd, timeout=30)

        return LintResult(
            success=result.exit_code == 0,
            output=result.stdout.strip() if result.stdout.strip() else "",
        )

    # =========================================================================
    # SEARCH Implementation
    # =========================================================================

    def search(
        self,
        pattern: str,
        path: str = ".",
        target: str = "content",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
    ) -> SearchResult:
        """
        Search for content or files.

        Args:
            pattern: Regex (for content) or glob pattern (for files)
            path: Directory/file to search (default: cwd)
            target: "content" (grep) or "files" (glob)
            file_glob: File pattern filter for content search (e.g., "*.py")
            limit: Max results (default 50)
            offset: Skip first N results
            output_mode: "content", "files_only", or "count"
            context: Lines of context around matches

        Returns:
            SearchResult with matches or file list
        """
        guard_error = workflow_state_search_error(path)
        if guard_error:
            return SearchResult(error=guard_error, total_count=0)

        # Expand ~ and other shell paths
        path = self._expand_path(path)

        # Validate that the path exists before searching
        check = self._exec(
            f"test -e {self._escape_shell_arg(path)} && echo exists || echo not_found"
        )
        if "not_found" in check.stdout:
            return SearchResult(
                error=f"Path not found: {path}. Verify the path exists (use 'terminal' to check).",
                total_count=0,
            )

        if target == "files":
            return self._search_files(pattern, path, limit, offset)
        else:
            return self._search_content(
                pattern, path, file_glob, limit, offset, output_mode, context
            )

    def _search_files(self, pattern: str, path: str, limit: int, offset: int) -> SearchResult:
        """Search for files by name pattern (glob-like)."""
        # Check if find is available (not on Windows without Git Bash/WSL)
        if not self._has_command("find"):
            return SearchResult(
                error="File search requires 'find' command. "
                "On Windows, use Git Bash, WSL, or install Unix tools."
            )

        # Auto-prepend **/ for recursive search if not already present
        if not pattern.startswith("**/") and "/" not in pattern:
            search_pattern = pattern
        else:
            search_pattern = pattern.split("/")[-1]

        # Use find with modification time sorting
        # -printf '%T@ %p\n' outputs: timestamp path
        # sort -rn sorts by timestamp descending (newest first)
        state_prune = ""
        if not diagnostic_workflow_file_access_enabled():
            state_prune = (
                "\\( -type d -path '*/.leanflow/workflow-state' "
                "-o -type d -path '*/.leanflow/cache' "
                "-o -type d -path '*/.leanflow/downloads' \\) -prune -o "
            )
        cmd = (
            f"find {self._escape_shell_arg(path)} {state_prune}"
            f"-type f -name {self._escape_shell_arg(search_pattern)} "
            f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | tail -n +{offset + 1} | head -n {limit}"
        )

        result = self._exec(cmd, timeout=60)

        visible_primary_lines = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2 or not parts[0].replace(".", "").isdigit():
                continue
            candidate = parts[1]
            if diagnostic_workflow_file_access_enabled() or not is_leanflow_internal_path(
                candidate
            ):
                visible_primary_lines.append(line)

        if not visible_primary_lines:
            # Try without -printf (BSD find compatibility -- macOS)
            cmd_simple = (
                f"find {self._escape_shell_arg(path)} {state_prune}"
                f"-type f -name {self._escape_shell_arg(search_pattern)} -print "
                f"2>/dev/null | head -n {limit + offset} | tail -n +{offset + 1}"
            )
            result = self._exec(cmd_simple, timeout=60)
        else:
            result.stdout = "\n".join(visible_primary_lines)

        files = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            # Parse "timestamp path" format
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0].replace(".", "").isdigit():
                candidate = parts[1]
            else:
                candidate = line
            if not diagnostic_workflow_file_access_enabled() and is_leanflow_internal_path(
                candidate
            ):
                continue
            files.append(candidate)

        return SearchResult(files=files, total_count=len(files))

    def _search_content(
        self,
        pattern: str,
        path: str,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> SearchResult:
        """Search for content inside files (grep-like)."""
        # Try ripgrep first (fast), fallback to grep (slower but works)
        if self._has_command("rg"):
            return self._search_with_rg(
                pattern, path, file_glob, limit, offset, output_mode, context
            )
        elif self._has_command("grep"):
            return self._search_with_grep(
                pattern, path, file_glob, limit, offset, output_mode, context
            )
        else:
            # Neither rg nor grep available (Windows without Git Bash, etc.)
            return SearchResult(
                error="Content search requires ripgrep (rg) or grep. "
                "Install ripgrep: https://github.com/BurntSushi/ripgrep#installation"
            )

    def _search_with_rg(
        self,
        pattern: str,
        path: str,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> SearchResult:
        """Search using ripgrep."""
        cmd_parts = [
            "rg",
            "--hidden",
            "--line-number",
            "--no-heading",
            "--with-filename",
        ]
        explicit_package_search = _explicit_package_dependency_search(path)
        if explicit_package_search:
            # Lake package roots are intentionally ignored by the project
            # checkout. An explicit lookup there must override ignore rules
            # and follow a package symlink instead of silently returning zero.
            cmd_parts.extend(["--no-ignore", "--follow"])
        if not diagnostic_workflow_file_access_enabled():
            for excluded in (
                "!**/.git/**",
                "!**/.leanflow/workflow-state/**",
                "!**/.leanflow/cache/**",
                "!**/.leanflow/downloads/**",
            ):
                cmd_parts.extend(["--glob", self._escape_shell_arg(excluded)])
            if not explicit_package_search:
                cmd_parts.extend(["--glob", self._escape_shell_arg("!**/.lake/**")])

        # Ripgrep normally separates context records as ``path-line-content``. Absolute paths and
        # checkout names routinely contain ``-<digits>-`` themselves, so parsing that format with
        # a regex can silently turn a path component into the reported line number. Use an explicit
        # field separator for structured content output instead.
        if output_mode == "content":
            escaped_separator = self._escape_shell_arg(_RG_FIELD_SEPARATOR)
            cmd_parts.extend(
                [
                    "--field-match-separator",
                    escaped_separator,
                    "--field-context-separator",
                    escaped_separator,
                ]
            )

        # Add context if requested
        if context > 0:
            cmd_parts.extend(["-C", str(context)])

        # Add file glob filter (must be quoted to prevent shell expansion)
        if file_glob:
            cmd_parts.extend(["--glob", self._escape_shell_arg(file_glob)])

        # Output mode handling
        if output_mode == "files_only":
            cmd_parts.append("-l")  # Files only
        elif output_mode == "count":
            cmd_parts.append("-c")  # Count per file

        # Add pattern and path
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))

        # Fetch extra rows so we can report the true total before slicing.
        # For context mode, rg emits separator lines ("--") between groups,
        # so we grab generously and filter in Python.
        fetch_limit = limit + offset + 200 if context > 0 else limit + offset
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])

        cmd = " ".join(cmd_parts)
        result = self._exec(cmd, timeout=60)

        # rg exit codes: 0=matches found, 1=no matches, 2=error
        if result.exit_code == 2 and not result.stdout.strip():
            error_msg = (
                result.stderr.strip()
                if hasattr(result, "stderr") and result.stderr
                else "Search error"
            )
            return SearchResult(error=f"Search failed: {error_msg}", total_count=0)

        # Parse results based on output mode
        if output_mode == "files_only":
            all_files = [f for f in result.stdout.strip().split("\n") if f]
            total = len(all_files)
            page = all_files[offset : offset + limit]
            return SearchResult(files=page, total_count=total)

        elif output_mode == "count":
            counts = {}
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    parts = line.rsplit(":", 1)
                    if len(parts) == 2:
                        with contextlib.suppress(ValueError):
                            counts[parts[0]] = int(parts[1])
            return SearchResult(counts=counts, total_count=sum(counts.values()))

        else:
            # Match and context records use the same unambiguous separator configured above.
            matches = []
            for line in result.stdout.strip().split("\n"):
                if not line or line == "--":
                    continue
                fields = line.split(_RG_FIELD_SEPARATOR, 2)
                if len(fields) != 3:
                    continue
                with contextlib.suppress(ValueError):
                    matches.append(
                        SearchMatch(
                            path=fields[0],
                            line_number=int(fields[1]),
                            content=fields[2][:500],
                        )
                    )

            total = len(matches)
            page = matches[offset : offset + limit]
            return SearchResult(matches=page, total_count=total, truncated=total > offset + limit)

    def _search_with_grep(
        self,
        pattern: str,
        path: str,
        file_glob: str | None,
        limit: int,
        offset: int,
        output_mode: str,
        context: int,
    ) -> SearchResult:
        """Fallback search using grep."""
        cmd_parts = ["grep", "-rnH"]  # -H forces filename even for single-file searches
        if not diagnostic_workflow_file_access_enabled():
            cmd_parts.extend(
                [
                    "--exclude-dir=workflow-state",
                    "--exclude-dir=cache",
                    "--exclude-dir=downloads",
                ]
            )

        # Add context if requested
        if context > 0:
            cmd_parts.extend(["-C", str(context)])

        # Add file pattern filter (must be quoted to prevent shell expansion)
        if file_glob:
            cmd_parts.extend(["--include", self._escape_shell_arg(file_glob)])

        # Output mode handling
        if output_mode == "files_only":
            cmd_parts.append("-l")
        elif output_mode == "count":
            cmd_parts.append("-c")

        # Add pattern and path
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))

        # Fetch generously so we can compute total before slicing
        fetch_limit = limit + offset + (200 if context > 0 else 0)
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])

        cmd = " ".join(cmd_parts)
        result = self._exec(cmd, timeout=60)

        # grep exit codes: 0=matches found, 1=no matches, 2=error
        if result.exit_code == 2 and not result.stdout.strip():
            error_msg = (
                result.stderr.strip()
                if hasattr(result, "stderr") and result.stderr
                else "Search error"
            )
            return SearchResult(error=f"Search failed: {error_msg}", total_count=0)

        if output_mode == "files_only":
            all_files = [f for f in result.stdout.strip().split("\n") if f]
            total = len(all_files)
            page = all_files[offset : offset + limit]
            return SearchResult(files=page, total_count=total)

        elif output_mode == "count":
            counts = {}
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    parts = line.rsplit(":", 1)
                    if len(parts) == 2:
                        with contextlib.suppress(ValueError):
                            counts[parts[0]] = int(parts[1])
            return SearchResult(counts=counts, total_count=sum(counts.values()))

        else:
            # grep match lines:   "file:lineno:content" (colon)
            # grep context lines: "file-lineno-content"  (dash)
            # grep group seps:    "--"
            # Note: on Windows, paths contain drive letters (e.g. C:\path),
            # so naive split(":") breaks. Use regex to handle both platforms.
            _match_re = re.compile(r"^([A-Za-z]:)?(.*?):(\d+):(.*)$")
            _ctx_re = re.compile(r"^([A-Za-z]:)?(.*?)-(\d+)-(.*)$")
            matches = []
            for line in result.stdout.strip().split("\n"):
                if not line or line == "--":
                    continue

                m = _match_re.match(line)
                if m:
                    matches.append(
                        SearchMatch(
                            path=(m.group(1) or "") + m.group(2),
                            line_number=int(m.group(3)),
                            content=m.group(4)[:500],
                        )
                    )
                    continue

                if context > 0:
                    m = _ctx_re.match(line)
                    if m:
                        matches.append(
                            SearchMatch(
                                path=(m.group(1) or "") + m.group(2),
                                line_number=int(m.group(3)),
                                content=m.group(4)[:500],
                            )
                        )

            total = len(matches)
            page = matches[offset : offset + limit]
            return SearchResult(matches=page, total_count=total, truncated=total > offset + limit)
